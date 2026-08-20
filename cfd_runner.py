"""
IG CFD Trading Bot — Tick Runner

Invoked roughly every 5 minutes by GitHub Actions. Each tick:
  1. Kill-switch check.
  2. Connect to IG, fetch account state + our 4 tracked positions.
  3. Margin safety check: block ALL new opens account-wide if available
     margin has fallen below RULES.margin_safety_buffer_pct of balance.
  4. Per-instrument: skip any instrument whose market isn't currently
     TRADEABLE (read live from IG each tick, not a hardcoded calendar --
     commodity CFDs follow underlying futures session hours + maintenance
     windows + weekend closures, unlike 24/5 forex).
  5. Let the agent decide (or HOLD).
  6. Validate every proposed trade against the rules firewall, then execute.
  7. Log + export dashboard data.

Two independent switches must BOTH be explicitly true for any order to fire:
  IG_LIVE_TRADING_ENABLED=true   (master kill switch)
  IG_LIVE=true                   (live vs demo IG environment; defaults to demo)

*** MARGIN SIZING MATH STILL UNVERIFIED AGAINST A REAL FILL ***
Session/login and epic resolution ARE confirmed working (2026-08-20, demo
account SNDPM) -- see config.py for the resolved epics. Trades exactly 3
instruments: Brent Crude Oil, WTI Crude Oil, Natural Gas. Palladium is
deliberately excluded (no rolling contract available -- see config.py).

The margin/leverage sizing math in _compute_position_size uses IG's
marginFactor + lotSize fields, which is the officially documented mechanism,
but has never been checked against a real fill. Before IG_LIVE_TRADING_ENABLED
is ever set to "true", run this against the demo environment (once it has a
non-zero virtual balance) and manually confirm that an opened position's
actual margin used (via get_account_state's "deposit") is in the ballpark you
expect for a given allocation_pct -- do not trust the formula blindly.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

MIN_NOTIONAL_SAFETY_CAP = 50_000.0  # absolute backstop ceiling on any single position's
                                     # notional exposure, independent of the margin-factor
                                     # math above -- protects against that formula being wrong


def _log_order_event(event: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    event = dict(event)
    event["logged_at"] = datetime.now(timezone.utc).isoformat()
    with open(DATA_DIR / "order_log.jsonl", "a") as f:
        f.write(json.dumps(event, default=str) + "\n")
    logger.warning(f"[IG-CFD] {event}")


def _compute_position_size(equity: float, allocation_pct: float, snapshot: dict,
                            max_leverage_multiple: float, min_deal_size: float):
    """Returns (size, margin_allocated, effective_leverage, notional) or
    (None, reason) on failure. See module docstring -- UNVERIFIED formula."""
    price = snapshot.get("offer") or snapshot.get("bid")
    margin_factor = snapshot.get("margin_factor")
    lot_size = snapshot.get("lot_size") or 1

    if not price or not margin_factor:
        return None, f"Missing price or marginFactor from IG snapshot: {snapshot}"

    margin_allocated = equity * (allocation_pct / 100)
    ig_implied_leverage = 100 / margin_factor if margin_factor else 1
    effective_leverage = min(ig_implied_leverage, max_leverage_multiple)
    notional = margin_allocated * effective_leverage

    if notional > MIN_NOTIONAL_SAFETY_CAP:
        notional = MIN_NOTIONAL_SAFETY_CAP
        logger.warning(f"[IG-CFD] Notional capped at absolute safety ceiling ${MIN_NOTIONAL_SAFETY_CAP:.0f}")

    size = notional / (price * lot_size)
    size = round(size / min_deal_size) * min_deal_size
    if size <= 0:
        return None, f"Computed size rounds to 0 (notional=${notional:.2f}, price={price}, lot_size={lot_size})"

    actual_notional = size * price * lot_size
    return {
        "size": round(size, 4),
        "margin_allocated": round(margin_allocated, 2),
        "effective_leverage": round(effective_leverage, 2),
        "notional": round(actual_notional, 2),
        "price": price,
    }, None


def _validate_trade(trade: dict, account: dict, positions: dict, rules) -> tuple:
    action = trade.get("action", "").upper()
    instrument = trade.get("instrument", "").upper()

    if action not in ("OPEN_LONG", "OPEN_SHORT", "CLOSE"):
        return False, f"Invalid action: {action}"

    from config import INSTRUMENTS
    if instrument not in INSTRUMENTS:
        return False, f"Unknown instrument: {instrument}"
    if not INSTRUMENTS[instrument].epic:
        return False, f"{instrument}'s epic is not configured yet -- see config.py instructions"

    existing = positions.get(instrument)
    opening = action in ("OPEN_LONG", "OPEN_SHORT")

    if opening:
        if existing:
            return False, f"{instrument} already has an open position -- CLOSE it first"
        if len(positions) >= rules.max_positions:
            return False, f"Max positions ({rules.max_positions}) reached"
        allocation_pct = trade.get("allocation_pct", 0)
        if allocation_pct < rules.min_allocation_pct or allocation_pct > rules.max_allocation_pct:
            return False, f"Allocation {allocation_pct}% outside [{rules.min_allocation_pct}, {rules.max_allocation_pct}]%"
        if not trade.get("stop_loss_pct"):
            return False, "stop_loss_pct is mandatory"
        if not trade.get("take_profit_pct"):
            return False, "take_profit_pct is mandatory"
        if account["available"] < account["balance"] * (rules.margin_safety_buffer_pct / 100):
            return False, (
                f"Margin safety buffer breached: available ${account['available']:.2f} is below "
                f"{rules.margin_safety_buffer_pct}% of balance ${account['balance']:.2f} -- "
                f"all new opens blocked account-wide until margin recovers"
            )
    else:  # CLOSE
        if not existing:
            return False, f"No open position on {instrument} to close"

    return True, "OK"


def run_cfd_tick():
    enabled = os.getenv("IG_LIVE_TRADING_ENABLED", "").lower() == "true"
    if not enabled:
        logger.info("[IG-CFD] IG_LIVE_TRADING_ENABLED is not 'true'. Doing nothing.")
        return

    from config import RULES, INSTRUMENTS, PLAYBOOKS_DIR
    from ig_broker import IGBroker
    from agent_runner import get_agent_trades
    from dashboard_exporter import export_for_dashboard

    live = os.getenv("IG_LIVE", "").lower() == "true"
    username = os.getenv("IG_USERNAME")
    password = os.getenv("IG_PASSWORD")
    api_key = os.getenv("IG_API_KEY")
    if not (username and password and api_key):
        logger.error("[IG-CFD] IG_USERNAME / IG_PASSWORD / IG_API_KEY not fully set. Aborting.")
        return

    logger.warning(f"[IG-CFD] Tick starting against {'LIVE (REAL MONEY)' if live else 'DEMO'} IG environment.")

    try:
        broker = IGBroker(username, password, api_key, live=live)
    except Exception as e:
        logger.error(f"[IG-CFD] Could not create IG session: {e}")
        return

    account = broker.get_account_state()
    logger.warning(
        f"[IG-CFD] Account: id={account['account_id']} currency={account['currency']} "
        f"balance={account['balance']:.2f} available={account['available']:.2f} "
        f"deposit={account['deposit']:.2f} profit_loss={account['profit_loss']:.2f}"
    )

    epic_to_key = {inst.epic: key for key, inst in INSTRUMENTS.items() if inst.epic}
    if not epic_to_key:
        logger.error("[IG-CFD] No instrument epics configured in config.py yet. Nothing to trade. Aborting.")
        return

    positions = broker.get_positions(epic_to_key)

    # Per-instrument market status, read fresh every tick -- this is what
    # actually gates trading hours instead of a hardcoded calendar.
    snapshots = {}
    for key, inst in INSTRUMENTS.items():
        if not inst.epic:
            continue
        snap = broker.get_market_snapshot(inst.epic)
        snapshots[key] = snap
        status = snap.get("market_status") if snap else "UNKNOWN"
        logger.info(f"[IG-CFD] {key} ({inst.display_name}): market_status={status}")

    tradeable_instruments = {k: s for k, s in snapshots.items() if broker.is_tradeable(s)}
    if not tradeable_instruments:
        logger.info("[IG-CFD] No tracked instrument's market is currently TRADEABLE. Skipping this tick.")
        return

    portfolio_state = {
        "account": account,
        "positions": positions,
        "instruments_currently_tradeable": list(tradeable_instruments.keys()),
        "note": (
            "THIS IS A REAL IG CFD ACCOUNT. Every trade you propose executes immediately with "
            "real, leveraged capital. Only instruments listed in instruments_currently_tradeable "
            "can be acted on right now -- others are outside market hours."
        ),
    }

    playbook_path = PLAYBOOKS_DIR / "cfd_aggressive.md"
    playbook = playbook_path.read_text(encoding="utf-8") if playbook_path.exists() else "Default strategy: maximize risk-adjusted returns on momentum and catalyst-driven moves."

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    trades = get_agent_trades(playbook, portfolio_state, now_str)

    if not trades:
        logger.info("[IG-CFD] Agent proposed no trades this tick (HOLD).")
    else:
        for trade in trades:
            instrument = trade.get("instrument", "").upper()
            action = trade.get("action", "").upper()

            ok, reason = _validate_trade(trade, account, positions, RULES)
            if not ok:
                _log_order_event({"action": action, "instrument": instrument, "status": "REJECTED", "reason": reason})
                continue

            inst = INSTRUMENTS[instrument]
            snapshot = snapshots.get(instrument)
            if not broker.is_tradeable(snapshot):
                _log_order_event({"action": action, "instrument": instrument, "status": "REJECTED",
                                   "reason": "Market no longer tradeable (status changed since gating check)"})
                continue

            if action in ("OPEN_LONG", "OPEN_SHORT"):
                sizing, err = _compute_position_size(
                    account["balance"], trade["allocation_pct"], snapshot,
                    RULES.max_leverage_multiple, inst.min_deal_size,
                )
                if err:
                    _log_order_event({"action": action, "instrument": instrument, "status": "REJECTED", "reason": err})
                    continue

                direction = "BUY" if action == "OPEN_LONG" else "SELL"
                price = sizing["price"]
                stop_distance = round(price * (trade["stop_loss_pct"] / 100), 4)
                limit_distance = round(price * (trade["take_profit_pct"] / 100), 4)

                result = broker.open_position(
                    epic=inst.epic, direction=direction, size=sizing["size"],
                    stop_distance=stop_distance, limit_distance=limit_distance,
                    currency_code=account["currency"], expiry=snapshot.get("expiry", "-"),
                )
                _log_order_event({
                    "action": action, "instrument": instrument, "direction": direction,
                    "size": sizing["size"], "margin_allocated": sizing["margin_allocated"],
                    "effective_leverage": sizing["effective_leverage"], "notional": sizing["notional"],
                    "stop_distance": stop_distance, "limit_distance": limit_distance,
                    "reason": trade.get("reason", ""), **result,
                })

            else:  # CLOSE
                pos = positions[instrument]
                result = broker.close_position(
                    deal_id=pos["deal_id"], direction=pos["direction"], epic=pos["epic"],
                    size=pos["size"], expiry=snapshot.get("expiry", "-") if snapshot else "-",
                )
                _log_order_event({
                    "action": "CLOSE", "instrument": instrument, "deal_id": pos["deal_id"],
                    "reason": trade.get("reason", ""), **result,
                })

    # Refresh + snapshot final state for the dashboard.
    account = broker.get_account_state()
    positions = broker.get_positions(epic_to_key)
    _record_snapshot(account, positions)
    export_for_dashboard(DATA_DIR, DATA_DIR / "dashboard")


def _record_snapshot(account: dict, positions: dict):
    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "balance": account["balance"],
        "available": account["available"],
        "deposit": account["deposit"],
        "profit_loss": account["profit_loss"],
        "currency": account["currency"],
        "positions": [
            {"instrument": key, **pos} for key, pos in positions.items()
        ],
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "equity_history.jsonl", "a") as f:
        f.write(json.dumps(snapshot, default=str) + "\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env")
    except ImportError:
        pass
    run_cfd_tick()
