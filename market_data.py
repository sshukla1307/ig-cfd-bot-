"""
IG CFD Trading Bot — Market Data

Deliberately narrow: this bot trades exactly 3 instruments (Brent, WTI,
Natural Gas -- Palladium excluded, see config.py), not a scanned universe, so
there's no need for the broad watchlist-scanning engine the Alpaca project
used. Just deep, focused context on these three:
  - Technicals via yfinance continuous futures tickers (independent of IG's
    epics -- used only for RSI/SMA/price-history context, never execution).
  - Commodity-specific news/catalysts via Brave Search (inventory reports,
    OPEC+ decisions, geopolitical supply shocks).
  - Macro backdrop via FRED (dollar strength, rates -- both move commodities).
"""

import logging
import os
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

# NYMEX/futures month codes: Jan=F Feb=G Mar=H Apr=J May=K Jun=M Jul=N Aug=Q Sep=U Oct=V Nov=X Dec=Z
_MONTH_CODES = "FGHJKMNQUVXZ"

# Root symbols for dated futures contracts (front-month continuous ticker is
# in config.YFINANCE_TICKERS -- this is the SAME underlying, just for
# constructing a specific expiry's ticker for term-structure comparison).
_DATED_CONTRACT_ROOT = {
    "BRENT_OIL": "BZ",
    "WTI_OIL": "CL",
    "NATURAL_GAS": "NG",
}


def _dated_futures_ticker(root: str, months_out: int) -> str:
    """Builds a Yahoo Finance dated-contract ticker N months out (e.g. CLX26.NYM).
    Empirically verified against yfinance -- the near-term month can occasionally
    already be expired/delisted close to roll dates, so callers should use a
    few months out (3+) for reliability, not the very next calendar month."""
    now = datetime.now()
    total_month_index = (now.year * 12 + (now.month - 1)) + months_out
    year = total_month_index // 12
    month_idx = total_month_index % 12  # 0-11
    code = _MONTH_CODES[month_idx]
    yy = str(year)[-2:]
    return f"{root}{code}{yy}.NYM"


def get_seasonality(instrument: str) -> dict:
    """Deterministic, calendar-based seasonal demand bias -- no external API,
    can't fail/be unavailable. Natural Gas has the strongest, best-documented
    seasonal pattern (winter heating withdrawal season vs shoulder-month
    injection season); oil's seasonality (summer driving season, winter
    heating oil) is real but considerably weaker/less reliable, flagged as such."""
    month = datetime.now().month

    if instrument == "NATURAL_GAS":
        if month in (12, 1, 2):
            return {"instrument": instrument, "month": month, "phase": "peak winter withdrawal season",
                    "historical_bias": "bullish",
                    "note": "Heating demand draws down storage -- historically the strongest seasonal support for NG."}
        if month in (11, 3):
            return {"instrument": instrument, "month": month, "phase": "early/late withdrawal season",
                    "historical_bias": "mildly bullish",
                    "note": "Heating-driven withdrawals, but less extreme than peak winter."}
        if month in (6, 7, 8):
            return {"instrument": instrument, "month": month, "phase": "summer cooling demand",
                    "historical_bias": "mildly bullish",
                    "note": "Power-generation demand for A/C offsets some injection-season softness, but weaker than winter."}
        return {"instrument": instrument, "month": month, "phase": "shoulder / injection season",
                "historical_bias": "bearish",
                "note": "Neither heating nor cooling demand is high -- storage builds fastest, historically the weakest seasonal period."}

    if instrument in ("BRENT_OIL", "WTI_OIL"):
        if month in (5, 6, 7, 8):
            return {"instrument": instrument, "month": month, "phase": "US summer driving season",
                    "historical_bias": "mildly bullish",
                    "note": "Higher gasoline demand provides some seasonal support via refined-product demand -- WEAKER and less reliable than NG's seasonality, easily overridden by macro/geopolitical factors."}
        if month in (11, 12, 1, 2):
            return {"instrument": instrument, "month": month, "phase": "winter heating oil demand",
                    "historical_bias": "mildly bullish",
                    "note": "Some support from heating oil/diesel demand -- WEAKER and less reliable than NG's seasonality."}
        return {"instrument": instrument, "month": month, "phase": "shoulder season",
                "historical_bias": "neutral",
                "note": "No strong seasonal demand driver either way for crude oil in this period."}

    return {"instrument": instrument, "month": month, "phase": "unknown", "historical_bias": "neutral", "note": "No seasonality model for this instrument."}


def get_term_structure(instrument: str) -> dict:
    """Contango (far month priced above near month -- typically signals ample/
    building supply, bearish) vs backwardation (far below near -- typically
    signals tight supply, bullish). A genuinely different signal from spot
    RSI/SMA: it reflects the market's own forward supply/demand expectation,
    not backward-looking price action. Compares the current continuous
    front-month price against a dated contract 3 months out (near-term dated
    contracts can already be expired/delisted close to roll dates -- verified
    empirically against yfinance, 3 months out is reliable)."""
    from config import YFINANCE_TICKERS
    import yfinance as yf

    root = _DATED_CONTRACT_ROOT.get(instrument)
    front_ticker = YFINANCE_TICKERS.get(instrument)
    if not root or not front_ticker:
        return {"instrument": instrument, "error": f"No term-structure mapping for {instrument}"}

    far_ticker = _dated_futures_ticker(root, months_out=3)
    try:
        front_hist = yf.Ticker(front_ticker).history(period="5d")
        far_hist = yf.Ticker(far_ticker).history(period="5d")
        if front_hist.empty or far_hist.empty:
            return {"instrument": instrument, "front_ticker": front_ticker, "far_ticker": far_ticker,
                    "error": "One or both contracts returned no price history"}

        front_price = float(front_hist["Close"].iloc[-1])
        far_price = float(far_hist["Close"].iloc[-1])
        spread = far_price - front_price
        spread_pct = (spread / front_price * 100) if front_price else None

        if spread > 0:
            structure = "contango"
            interpretation = "far month priced above near month -- typically signals ample/building supply (bearish tilt)"
        elif spread < 0:
            structure = "backwardation"
            interpretation = "far month priced below near month -- typically signals tight supply (bullish tilt)"
        else:
            structure = "flat"
            interpretation = "no meaningful spread between near and far months"

        return {
            "instrument": instrument,
            "front_ticker": front_ticker, "front_price": round(front_price, 4),
            "far_ticker": far_ticker, "far_price": round(far_price, 4),
            "spread": round(spread, 4), "spread_pct": round(spread_pct, 3) if spread_pct is not None else None,
            "structure": structure, "interpretation": interpretation,
        }
    except Exception as e:
        logger.warning(f"get_term_structure({instrument}) failed: {e}")
        return {"instrument": instrument, "error": str(e)}


def get_technicals(yf_ticker: str) -> dict:
    """RSI-14, SMA-20/50, and recent price action for a continuous futures ticker."""
    try:
        import yfinance as yf
        import pandas as pd

        hist = yf.Ticker(yf_ticker).history(period="6mo")
        if hist.empty:
            return {"ticker": yf_ticker, "error": "No price history returned"}

        close = hist["Close"]
        current_price = float(close.iloc[-1])
        sma_20 = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None
        sma_50 = float(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None

        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, float("nan"))
        rsi_14 = float((100 - (100 / (1 + rs))).iloc[-1]) if len(close) >= 15 else None

        recent = hist.tail(10)
        price_history = [
            {
                "date": idx.strftime("%Y-%m-%d"),
                "open": round(float(r["Open"]), 4),
                "high": round(float(r["High"]), 4),
                "low": round(float(r["Low"]), 4),
                "close": round(float(r["Close"]), 4),
                "volume": int(r["Volume"]),
            }
            for idx, r in recent.iterrows()
        ]

        signals = []
        if sma_20 and sma_50:
            signals.append("SMA-20 above SMA-50 (short-term bullish)" if sma_20 > sma_50
                            else "SMA-20 below SMA-50 (short-term bearish)")
        if rsi_14:
            if rsi_14 > 70:
                signals.append(f"RSI {rsi_14:.1f} — overbought")
            elif rsi_14 < 30:
                signals.append(f"RSI {rsi_14:.1f} — oversold")
            else:
                signals.append(f"RSI {rsi_14:.1f} — neutral")

        return {
            "ticker": yf_ticker,
            "current_price": round(current_price, 4),
            "sma_20": round(sma_20, 4) if sma_20 else None,
            "sma_50": round(sma_50, 4) if sma_50 else None,
            "rsi_14": round(rsi_14, 1) if rsi_14 else None,
            "price_history_last_10d": price_history,
            "signals": signals,
        }
    except Exception as e:
        logger.warning(f"get_technicals({yf_ticker}) failed: {e}")
        return {"ticker": yf_ticker, "error": str(e)}


def get_commodity_news(query: str, count: int = 5, freshness: str = "pd") -> dict:
    """Brave web search, scoped by the caller to a specific commodity catalyst query."""
    api_key = os.getenv("BRAVE_API_KEY")
    if not api_key:
        return {"error": "BRAVE_API_KEY not set. Get one free at https://brave.com/search/api/"}

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
                params={"q": query, "count": count, "freshness": freshness, "text_decorations": False},
            )
        response.raise_for_status()
        data = response.json()
        results = data.get("web", {}).get("results", [])
        return {
            "query": query,
            "results": [
                {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("description")}
                for r in results[:count]
            ],
        }
    except Exception as e:
        logger.warning(f"get_commodity_news({query!r}) failed: {e}")
        return {"query": query, "error": str(e)}


def get_macro() -> dict:
    """Dollar strength (commodities are priced in USD -- a stronger dollar
    typically pressures them) and the VIX, from FRED."""
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        return {"error": "FRED_API_KEY not set. Get one free at https://fred.stlouisfed.org/docs/api/api_key.html"}

    try:
        from fredapi import Fred
        fred = Fred(api_key=api_key)

        series = {"DTWEXBGS": "Trade-Weighted Dollar Index", "VIXCLS": "VIX", "DGS10": "10Y Treasury Yield"}
        summary_parts = []
        values = {}
        for series_id, label in series.items():
            data = fred.get_series(series_id).dropna()
            if data.empty:
                continue
            latest_val = float(data.iloc[-1])
            latest_date = data.index[-1].strftime("%Y-%m-%d")
            values[series_id] = {"value": latest_val, "date": latest_date}
            summary_parts.append(f"{label}: {latest_val} ({latest_date})")

        return {"summary": " | ".join(summary_parts), "values": values}
    except Exception as e:
        logger.warning(f"get_macro() failed: {e}")
        return {"error": str(e)}


# FRED series IDs for weekly EIA inventory data. WCESTUS1 (crude oil ending
# stocks) is a well-established, frequently-cited FRED series. The natural
# gas storage series ID here has NOT been verified against a live FRED key
# (no key available in this environment) -- if it's wrong, this fails
# gracefully (same try/except pattern as get_macro) and just returns an
# "error" field the agent can see, it will not crash the tick. Treat this
# the same as the margin-sizing-math disclaimer elsewhere in this repo:
# confirm against a real key before trusting it blindly.
_INVENTORY_SERIES = {
    "BRENT_OIL": ("WCESTUS1", "U.S. Ending Stocks of Crude Oil (thousand barrels)"),
    "WTI_OIL": ("WCESTUS1", "U.S. Ending Stocks of Crude Oil (thousand barrels)"),
    "NATURAL_GAS": ("NGWSTUS", "U.S. Natural Gas Storage (billion cubic feet) -- UNVERIFIED series ID"),
}


def get_inventory_data(instrument: str) -> dict:
    """Real, structured week-over-week inventory change (a build or a draw),
    compared against the trailing-8-week average change -- a genuinely
    different signal from a generic news-headline search, which only tells
    you a report existed, not its actual magnitude relative to what's typical.
    Not the same as a true consensus-surprise figure (that needs a paid
    Street-estimates feed we don't have) -- this is the real print's size
    relative to recent history, which is still meaningfully more structured
    than a headline search."""
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        return {"error": "FRED_API_KEY not set. Get one free at https://fred.stlouisfed.org/docs/api/api_key.html"}

    series_info = _INVENTORY_SERIES.get(instrument)
    if not series_info:
        return {"instrument": instrument, "error": f"No inventory series configured for {instrument}"}
    series_id, label = series_info

    try:
        from fredapi import Fred
        fred = Fred(api_key=api_key)

        data = fred.get_series(series_id).dropna()
        if len(data) < 2:
            return {"instrument": instrument, "series_id": series_id, "error": "Not enough history returned"}

        latest = float(data.iloc[-1])
        latest_date = data.index[-1].strftime("%Y-%m-%d")
        change = latest - float(data.iloc[-2])

        trailing = data.diff().dropna().tail(8)
        avg_change = float(trailing.mean()) if not trailing.empty else None

        direction = "build" if change > 0 else ("draw" if change < 0 else "flat")
        larger_than_typical = (
            abs(change) > abs(avg_change) * 1.5 if avg_change not in (None, 0) else None
        )

        return {
            "instrument": instrument, "series_id": series_id, "label": label,
            "latest_value": round(latest, 1), "latest_date": latest_date,
            "week_over_week_change": round(change, 1),
            "direction": direction,
            "trailing_8wk_avg_change": round(avg_change, 1) if avg_change is not None else None,
            "larger_than_typical_move": larger_than_typical,
        }
    except Exception as e:
        logger.warning(f"get_inventory_data({instrument}) failed: {e}")
        return {"instrument": instrument, "series_id": series_id, "error": str(e)}


# CFTC's public Commitment of Traders API (Socrata) -- free, no API key
# required, verified working directly (curl'd real data before writing this).
# Brent is intentionally NOT covered: it trades on ICE Futures Europe, outside
# CFTC's US jurisdiction, which only reports on US-regulated markets.
_COT_ENDPOINT = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
_COT_CONTRACT_CODES = {
    "WTI_OIL": ("067651", "WTI-PHYSICAL - NYMEX"),
    "NATURAL_GAS": ("023651", "NATURAL GAS - NYMEX"),
}


def get_positioning_data(instrument: str) -> dict:
    """Managed-money (speculator/hedge fund) net futures positioning from
    CFTC's weekly Commitment of Traders report -- extreme crowding in one
    direction is a real, well-documented contrarian signal (crowded longs
    tend to precede pullbacks, and vice versa), a genuinely different
    signal from spot technicals or a news search. Not available for
    BRENT_OIL (see module note above)."""
    contract = _COT_CONTRACT_CODES.get(instrument)
    if not contract:
        return {"instrument": instrument, "error": f"No CFTC COT data available for {instrument} (likely ICE-listed, outside CFTC jurisdiction)"}
    code, label = contract

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(_COT_ENDPOINT, params={
                "$where": f"cftc_contract_market_code='{code}'",
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": 52,
            })
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return {"instrument": instrument, "contract": label, "error": "No COT data returned"}

        nets = [float(r["m_money_positions_long_all"]) - float(r["m_money_positions_short_all"]) for r in rows]
        latest = nets[0]
        latest_date = rows[0]["report_date_as_yyyy_mm_dd"][:10]
        open_interest = float(rows[0].get("open_interest_all", 0) or 0)

        sorted_nets = sorted(nets)
        percentile = (sorted_nets.index(latest) / (len(sorted_nets) - 1) * 100) if len(sorted_nets) > 1 else 50.0

        if percentile >= 85:
            interpretation = "Managed-money net-long positioning is near a multi-month EXTREME -- crowded long, a contrarian bearish signal"
        elif percentile <= 15:
            interpretation = "Managed-money net-short positioning is near a multi-month EXTREME -- crowded short, a contrarian bullish signal"
        else:
            interpretation = "Managed-money positioning is within a normal historical range -- no extreme crowding signal"

        return {
            "instrument": instrument, "contract": label, "report_date": latest_date,
            "net_managed_money_position": int(latest),
            "pct_of_open_interest": round(latest / open_interest * 100, 2) if open_interest else None,
            "percentile_vs_trailing_year": round(percentile, 1),
            "interpretation": interpretation,
        }
    except Exception as e:
        logger.warning(f"get_positioning_data({instrument}) failed: {e}")
        return {"instrument": instrument, "contract": label, "error": str(e)}
