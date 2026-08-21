"""
IG CFD Trading Bot — Broker Adapter

Thin wrapper around the `trading_ig` community Python library (IG has no
official first-party SDK). Method names/return shapes below were verified
directly against the installed trading_ig package's source (rest.py), not
assumed from memory. Session/login and epic resolution are confirmed
working against a real IG demo account -- see config.py for the resolved
epics (3 instruments; Palladium excluded, no rolling contract available).

Safety notes:
  - Defaults to acc_type="demo" unless explicitly told otherwise.
  - Every opening order requires BOTH a stop and a limit distance — IG's
    create_open_position enforces neither on its own, so cfd_runner.py's
    validation layer is what actually guarantees this, not IG.
  - "expiry" and "marginFactor" are read fresh from IG's own market snapshot
    for each instrument at order time, not hardcoded — CFD contract terms
    (rolling "-" vs dated) vary by instrument and we don't want to guess.
"""

import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)


class IGBroker:
    def __init__(self, username: str, password: str, api_key: str, live: bool = False):
        from trading_ig import IGService

        self.live = live
        acc_type = "live" if live else "demo"
        self.ig = IGService(username, password, api_key, acc_type=acc_type)
        self.ig.create_session()
        self._account_id = None
        self._currency = None
        logger.info(f"IGBroker session created against {acc_type.upper()} IG environment")

    # ─────────────────────────────────────
    # Account state
    # ─────────────────────────────────────

    def get_account_state(self) -> dict:
        """Returns {account_id, currency, balance, available, deposit, profit_loss}.
        Picks the CFD-type sub-account if the login has multiple (IG accounts can
        have separate CFD / spread-bet / share-dealing sub-accounts); falls back
        to the first account with a loud warning if none is explicitly typed 'CFD'
        so a wrong-account trade is never silent."""
        accounts = self.ig.fetch_accounts()

        row = None
        if "accountType" in accounts.columns:
            cfd_rows = accounts[accounts["accountType"].str.upper() == "CFD"]
            if not cfd_rows.empty:
                row = cfd_rows.iloc[0]
        if row is None:
            row = accounts.iloc[0]
            logger.warning(
                f"[IG] No account with accountType=='CFD' found; defaulting to the "
                f"first account (id={row.get('accountId')}, type={row.get('accountType')}). "
                f"Verify this is the intended account."
            )

        self._account_id = row["accountId"]
        self._currency = row["currency"]

        return {
            "account_id": row["accountId"],
            "currency": row["currency"],
            "balance": float(row["balance"]),
            "available": float(row["available"]),
            "deposit": float(row["deposit"]),
            "profit_loss": float(row["profitLoss"]),
        }

    # ─────────────────────────────────────
    # Positions
    # ─────────────────────────────────────

    def get_positions(self, epic_to_key: dict) -> dict:
        """Returns {instrument_key: {deal_id, direction, size, entry_level,
        stop_level, limit_level, current_bid, current_offer, epic}}.
        epic_to_key maps IG epic -> our internal instrument key (config.INSTRUMENTS),
        so only positions in our 4-instrument universe are surfaced -- any other
        open position on the account (e.g. from manual trading) is intentionally
        left completely alone and invisible to this bot, matching the same
        "hands off anything outside our scope" principle as the Alpaca bot's
        protected-tickers list, just inverted (allowlist of what we DO touch)."""
        df = self.ig.fetch_open_positions()
        positions = {}
        if df is None or len(df) == 0:
            return positions

        for _, row in df.iterrows():
            epic = row.get("epic")
            key = epic_to_key.get(epic)
            if not key:
                continue  # not one of our 4 tracked instruments -- ignore entirely
            positions[key] = {
                "deal_id": row.get("dealId"),
                "epic": epic,
                "direction": row.get("direction"),
                "size": float(row.get("size", 0)),
                "entry_level": float(row.get("level", 0)),
                "stop_level": float(row["stopLevel"]) if row.get("stopLevel") not in (None, "") else None,
                "limit_level": float(row["limitLevel"]) if row.get("limitLevel") not in (None, "") else None,
                "current_bid": float(row.get("bid")) if row.get("bid") not in (None, "") else None,
                "current_offer": float(row.get("offer")) if row.get("offer") not in (None, "") else None,
            }
        return positions

    # ─────────────────────────────────────
    # Market data
    # ─────────────────────────────────────

    def get_market_snapshot(self, epic: str) -> Optional[dict]:
        """Fresh market status + pricing + contract terms for one epic, read
        directly from IG rather than cached/assumed. Returns None on failure."""
        try:
            data = self.ig.fetch_market_by_epic(epic)
        except Exception as e:
            logger.warning(f"[IG] fetch_market_by_epic({epic}) failed: {e}")
            return None

        snapshot = data.get("snapshot", {}) if isinstance(data, dict) else {}
        instrument = data.get("instrument", {}) if isinstance(data, dict) else {}

        return {
            "epic": epic,
            "market_status": snapshot.get("marketStatus"),
            "bid": snapshot.get("bid"),
            "offer": snapshot.get("offer"),
            "expiry": instrument.get("expiry", "-"),
            "margin_factor": instrument.get("marginFactor"),
            "margin_factor_unit": instrument.get("marginFactorUnit"),
            "lot_size": instrument.get("lotSize"),
            "currencies": instrument.get("currencies", []),
        }

    def is_tradeable(self, market_snapshot: Optional[dict]) -> bool:
        return bool(market_snapshot) and market_snapshot.get("market_status") == "TRADEABLE"

    # ─────────────────────────────────────
    # Order submission
    # ─────────────────────────────────────

    @staticmethod
    def round_size(size: float, min_deal_size: float) -> float:
        """IG deal sizes are typically in increments of min_deal_size (often 0.1
        for these instruments, but verify per-instrument in config.py)."""
        if min_deal_size <= 0:
            return round(size, 2)
        steps = math.floor(size / min_deal_size)
        return round(steps * min_deal_size, 4)

    def open_position(self, epic: str, direction: str, size: float,
                       stop_distance: float, limit_distance: float, currency_code: str,
                       expiry: str = "-") -> dict:
        """Opens a new CFD position at market, with a mandatory stop and limit
        expressed as point distances from the fill price (IG computes the
        absolute levels itself from the live price at fill time, which is more
        robust than us pre-computing absolute levels off a snapshot price that
        may have moved by the time the order actually fills)."""
        if direction not in ("BUY", "SELL"):
            return {"status": "rejected", "reason": f"Invalid direction: {direction}"}
        if not stop_distance or not limit_distance:
            return {"status": "rejected", "reason": "Both stop_distance and limit_distance are mandatory"}

        try:
            result = self.ig.create_open_position(
                currency_code=currency_code,
                direction=direction,
                epic=epic,
                expiry=expiry,
                force_open=True,
                guaranteed_stop=False,
                level=None,
                limit_distance=limit_distance,
                limit_level=None,
                order_type="MARKET",
                quote_id=None,
                size=size,
                stop_distance=stop_distance,
                stop_level=None,
                trailing_stop=False,
                trailing_stop_increment=None,
            )
            deal_status = result.get("dealStatus") if isinstance(result, dict) else None
            if deal_status == "REJECTED":
                return {"status": "rejected", "reason": result.get("reason", "IG rejected the deal"), "raw": result}
            return {"status": "submitted", "deal_id": result.get("dealId") if isinstance(result, dict) else None, "raw": result}
        except Exception as e:
            logger.error(f"[IG] open_position failed for {epic}: {e}")
            return {"status": "rejected", "reason": str(e)}

    def close_position(self, deal_id: str, direction: str, epic: str, size: float,
                        expiry: str = "-") -> dict:
        """Closes (or partially closes) an existing position. direction here is
        the CLOSING direction -- opposite of the position's original direction
        (e.g. a LONG/BUY position closes with direction='SELL').

        IG identifies the position to close by EITHER dealId OR epic+expiry --
        never both (confirmed in production: sending both non-null gave
        "validation.mutual-exclusive-value.request" and rejected every close).
        Since we always know the exact deal_id, epic/expiry are intentionally
        left null here; the epic/expiry parameters only exist for logging."""
        close_direction = "SELL" if direction == "BUY" else "BUY"
        try:
            result = self.ig.close_open_position(
                deal_id=deal_id,
                direction=close_direction,
                epic=None,
                expiry=None,
                level=None,
                order_type="MARKET",
                quote_id=None,
                size=size,
            )
            deal_status = result.get("dealStatus") if isinstance(result, dict) else None
            if deal_status == "REJECTED":
                return {"status": "rejected", "reason": result.get("reason", "IG rejected the close"), "raw": result}
            return {"status": "submitted", "raw": result}
        except Exception as e:
            logger.error(f"[IG] close_position failed for {epic} (deal {deal_id}): {e}")
            return {"status": "rejected", "reason": str(e)}
