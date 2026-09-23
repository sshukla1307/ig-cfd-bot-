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
from datetime import datetime, timedelta, timezone

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
    """RSI-14, SMA-20/50, and recent price action for a continuous futures
    ticker, computed on HOURLY bars. Deliberately NOT daily bars: the
    trend-following (Brent) / mean-reversion (NG) edge this account trades
    on was validated by backtest.py against 2 years of HOURLY data (see
    config.py's PERSONA_PROMPT), and the live tick cadence itself is hourly
    (RULES.min_tick_interval_minutes). A daily RSI-14/SMA-20/50 measures a
    materially different, multi-week momentum/reversion state than what was
    actually backtested -- feeding the live agent a different indicator than
    the one its own strategy was validated on. 60 days of 1h bars gives a
    stable RSI-14/SMA-50 while staying well inside yfinance's 730-day cap
    for intraday intervals."""
    try:
        import yfinance as yf
        import pandas as pd

        hist = yf.Ticker(yf_ticker).history(period="60d", interval="1h")
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
                "time": idx.strftime("%Y-%m-%d %H:%M"),
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
            signals.append("SMA-20 above SMA-50 on hourly bars (short-term bullish)" if sma_20 > sma_50
                            else "SMA-20 below SMA-50 on hourly bars (short-term bearish)")
        if rsi_14:
            if rsi_14 > 70:
                signals.append(f"Hourly RSI {rsi_14:.1f} — overbought")
            elif rsi_14 < 30:
                signals.append(f"Hourly RSI {rsi_14:.1f} — oversold")
            else:
                signals.append(f"Hourly RSI {rsi_14:.1f} — neutral")

        return {
            "ticker": yf_ticker,
            "timeframe": "1h bars, last 60 days -- matches the backtest's validated timeframe, not daily bars",
            "current_price": round(current_price, 4),
            "sma_20": round(sma_20, 4) if sma_20 else None,
            "sma_50": round(sma_50, 4) if sma_50 else None,
            "rsi_14": round(rsi_14, 1) if rsi_14 else None,
            "price_history_last_10_bars": price_history,
            "signals": signals,
        }
    except Exception as e:
        logger.warning(f"get_technicals({yf_ticker}) failed: {e}")
        return {"ticker": yf_ticker, "error": str(e)}


def _brave_search(query: str, count: int, freshness: str) -> dict:
    """Shared Brave Search API call -- get_commodity_news and
    get_named_market_commentary both go through this, so the request/error
    shape can never drift between the two."""
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
        logger.warning(f"_brave_search({query!r}) failed: {e}")
        return {"query": query, "error": str(e)}


# SiftingIO (sifting.io) -- a separate, licensed multi-venue market data
# aggregator, added 2026-09-23 as an INDEPENDENT price cross-check distinct
# from yfinance (used by get_technicals/get_term_structure) -- catches a
# stale or diverging single-provider feed rather than trusting yfinance
# blindly. Symbols verified live against a real key before writing this:
# WTIUSD (WTI), UKOUSD (Brent -- "UK Oil"), NATGAS (Henry Hub natural gas).
# NGASUSD and BRENTUSD do NOT exist on this API (confirmed via a live 404)
# despite being the more "obvious" guesses -- use the mapping below, not
# those. Requires "Accept-Encoding: gzip" on every request or the API
# rejects it outright with a "gzip_required" error (also confirmed live).
#
# Live gap observed 2026-09-23: WTIUSD's own feed was running ~19h stale
# (its most recent bar was from the prior day) while UKOUSD/NATGAS were
# current to the last hour -- i.e. SiftingIO's own per-symbol freshness
# isn't uniform. A narrow lookback window (e.g. "last few hours") can
# spuriously 404 for a symbol with a gap like this even though older data
# exists, so this deliberately requests a wide 48h window and reports the
# actual bar's age explicitly (is_stale) rather than assuming freshness.
_SIFTING_SYMBOLS = {"BRENT_OIL": "UKOUSD", "WTI_OIL": "WTIUSD", "NATURAL_GAS": "NATGAS"}
_SIFTING_STALE_THRESHOLD_HOURS = 4.0


def _sifting_price_check(instrument: str) -> dict:
    api_key = os.getenv("SIFTING_API_KEY")
    if not api_key:
        return {"error": "SIFTING_API_KEY not set. Get one at https://sifting.io"}

    symbol = _SIFTING_SYMBOLS.get(instrument)
    if not symbol:
        return {"instrument": instrument, "error": f"No SiftingIO symbol mapped for {instrument}"}

    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=48)
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                f"https://api.sifting.io/v1/hist/commodities/{symbol}/bars",
                headers={"X-API-Key": api_key, "Accept-Encoding": "gzip"},
                params={
                    "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "interval": "1h", "order": "desc", "limit": 48,
                },
            )
        if resp.status_code == 404:
            return {"instrument": instrument, "symbol": symbol,
                    "error": "SiftingIO has no recent bars for this symbol right now (provider gap)"}
        resp.raise_for_status()
        rows = resp.json().get("data", [])
        if not rows:
            return {"instrument": instrument, "symbol": symbol, "error": "No bars returned"}

        latest = rows[0]
        latest_time = datetime.fromtimestamp(latest["t"] / 1000, tz=timezone.utc)
        age_hours = (now - latest_time).total_seconds() / 3600

        oldest = rows[-1]
        change_pct = (
            (latest["c"] - oldest["o"]) / oldest["o"] * 100 if oldest.get("o") else None
        )

        return {
            "instrument": instrument, "symbol": symbol, "source": "SiftingIO",
            "current_price": round(latest["c"], 4),
            "as_of": latest_time.strftime("%Y-%m-%d %H:%M UTC"),
            "age_hours": round(age_hours, 1),
            "is_stale": age_hours > _SIFTING_STALE_THRESHOLD_HOURS,
            "trailing_window_change_pct": round(change_pct, 3) if change_pct is not None else None,
            "bars_in_window": len(rows),
        }
    except Exception as e:
        logger.warning(f"_sifting_price_check({instrument}) failed: {e}")
        return {"instrument": instrument, "symbol": symbol, "error": str(e)}


# OilPriceAPI (oilpriceapi.com) -- a second, INDEPENDENT price source from
# both yfinance and SiftingIO, added 2026-09-23. Symbols verified live
# against a real key before writing this: WTI_USD, BRENT_CRUDE_USD,
# NATURAL_GAS_USD. Unlike SiftingIO, this API reports its own staleness
# directly (data.stale, data.freshness.age_seconds) and a ready-made 24h %
# change (data.changes["24h"].percent) -- no need to compute either
# ourselves. Free plan is capped at 50 requests/day (confirmed in their
# docs) -- meaningfully tighter than the other sources here, so this is
# folded into the SAME get_independent_price_check call as SiftingIO
# (one combined tool call per instrument) rather than its own separate
# tool, to avoid burning through the daily cap across repeated ticks.
_OILPRICEAPI_CODES = {"BRENT_OIL": "BRENT_CRUDE_USD", "WTI_OIL": "WTI_USD", "NATURAL_GAS": "NATURAL_GAS_USD"}


def _oilpriceapi_price_check(instrument: str) -> dict:
    api_key = os.getenv("OILPRICEAPI_KEY")
    if not api_key:
        return {"error": "OILPRICEAPI_KEY not set. Get one at https://www.oilpriceapi.com"}

    code = _OILPRICEAPI_CODES.get(instrument)
    if not code:
        return {"instrument": instrument, "error": f"No OilPriceAPI code mapped for {instrument}"}

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                "https://api.oilpriceapi.com/v1/prices/latest",
                headers={"Authorization": f"Token {api_key}"},
                params={"by_code": code},
            )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        if not data:
            return {"instrument": instrument, "code": code, "error": "No data returned"}

        change_24h = data.get("changes", {}).get("24h", {})
        return {
            "instrument": instrument, "code": code, "source": "OilPriceAPI",
            "current_price": data.get("price"),
            "as_of": data.get("as_of"),
            "is_stale": bool(data.get("stale")),
            "age_seconds": data.get("freshness", {}).get("age_seconds"),
            "change_24h_pct": change_24h.get("percent"),
        }
    except Exception as e:
        logger.warning(f"_oilpriceapi_price_check({instrument}) failed: {e}")
        return {"instrument": instrument, "code": code, "error": str(e)}


def get_independent_price_check(instrument: str) -> dict:
    """Two independent real-time price cross-checks (SiftingIO and
    OilPriceAPI), neither the same as the yfinance feed get_technicals uses
    -- use this to sanity-check the price/direction get_technicals already
    showed you, not to replace it. If both agree closely with each other and
    with get_technicals, that's a real confidence boost; if any of the three
    disagree sharply, or either source here reports is_stale=True, treat
    that as a data-quality flag worth noting, not just redundant
    confirmation. sources_agree is only set when BOTH sources returned a
    usable, non-stale price -- absent otherwise rather than guessed at."""
    sifting = _sifting_price_check(instrument)
    oilpriceapi = _oilpriceapi_price_check(instrument)

    result = {"instrument": instrument, "sifting_io": sifting, "oilpriceapi": oilpriceapi}

    sifting_price = sifting.get("current_price")
    oilpriceapi_price = oilpriceapi.get("current_price")
    if (
        sifting_price and oilpriceapi_price
        and not sifting.get("is_stale") and not oilpriceapi.get("is_stale")
    ):
        diff_pct = abs(sifting_price - oilpriceapi_price) / oilpriceapi_price * 100
        result["price_diff_pct"] = round(diff_pct, 3)
        result["sources_agree"] = diff_pct < 1.0

    return result


def get_commodity_news(query: str, count: int = 5, freshness: str = "pd") -> dict:
    """Brave web search, scoped by the caller to a specific commodity catalyst query."""
    return _brave_search(query, count, freshness)


# User-requested watchlist of named market-commentary sources (2026-09-23).
# Fetched via Brave's own site: search operator rather than scraping these
# domains directly: investing.com and cmegroup.com both actively block
# direct automated fetches -- cmegroup.com's block page explicitly states
# scraping is "strictly prohibited by CME Group's website Data Terms of
# Use" (confirmed live: a plain GET returns HTTP 403 with that exact
# message), and investing.com returns a blanket 403 including on its own
# robots.txt. cnbc.com's edge/WAF also denies even a robots.txt fetch,
# suggesting the same posture. Rather than a fragile, ToS-risky per-site
# scraper (that would also need separate HTML-parsing logic per site, and
# break silently whenever any one of them changes their markup), a single
# site-scoped Brave query gets legitimate, already-licensed coverage of
# exactly these 5 domains' own indexed content in one call -- the same
# thing a human doing "site:investing.com crude oil" in a search bar gets.
_NAMED_MARKET_SOURCE_DOMAINS = [
    "investing.com", "tradingeconomics.com", "cmegroup.com", "oilprice.com", "cnbc.com",
]


def get_named_market_commentary(query: str, count: int = 5, freshness: str = "pd") -> dict:
    """Search specifically within investing.com, tradingeconomics.com,
    cmegroup.com, oilprice.com and cnbc.com for commentary on a given topic
    -- the trader's own requested watchlist of market-commentary sources,
    as a scoped Brave search rather than direct scraping (see module note
    above for why)."""
    site_filter = " OR ".join(f"site:{d}" for d in _NAMED_MARKET_SOURCE_DOMAINS)
    scoped_query = f"{query} ({site_filter})"
    return _brave_search(scoped_query, count, freshness)


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


# EIA's own API (api.eia.gov/v2), NOT FRED -- confirmed live in production
# that FRED simply doesn't carry weekly EIA petroleum/natural-gas inventory
# series at all (every FRED-based call failed with "the series does not
# exist"). Both routes/facets below were curl-verified directly against a
# real EIA key before writing this:
#   - Crude (Brent + WTI both trade off the same US commercial crude stock
#     print -- there's no separate "Brent inventory", it's a global-priced
#     benchmark): /v2/petroleum/stoc/wstk/data/, product=EPC0 (Crude Oil),
#     duoarea=NUS (U.S.), process=SAX (Ending Stocks Excluding SPR) --
#     series WCESTUS1, the actual headline "commercial crude stocks" number
#     the weekly EIA report moves markets on (process=SAE would include the
#     Strategic Petroleum Reserve, which isn't the market-moving figure).
#   - Natural gas: /v2/natural-gas/stor/wkly/data/, duoarea=R48 (Lower 48
#     States), process=SWO (Underground Storage - Working Gas) -- series
#     NW2_EPG0_SWO_R48_BCF, the headline weekly storage print.
_EIA_INVENTORY_CONFIG = {
    "BRENT_OIL": {
        "url": "https://api.eia.gov/v2/petroleum/stoc/wstk/data/",
        "facets": {"product": "EPC0", "duoarea": "NUS", "process": "SAX"},
        "label": "U.S. Commercial Crude Oil Stocks Excluding SPR (thousand barrels)",
        "units": "MBBL",
    },
    "WTI_OIL": {
        "url": "https://api.eia.gov/v2/petroleum/stoc/wstk/data/",
        "facets": {"product": "EPC0", "duoarea": "NUS", "process": "SAX"},
        "label": "U.S. Commercial Crude Oil Stocks Excluding SPR (thousand barrels)",
        "units": "MBBL",
    },
    "NATURAL_GAS": {
        "url": "https://api.eia.gov/v2/natural-gas/stor/wkly/data/",
        "facets": {"duoarea": "R48", "process": "SWO"},
        "label": "Lower 48 States Natural Gas Working Underground Storage (billion cubic feet)",
        "units": "BCF",
    },
}


def get_inventory_data(instrument: str) -> dict:
    """Real, structured week-over-week EIA inventory change (a build or a
    draw), compared against the trailing-8-week average change -- a
    genuinely different signal from a generic news-headline search, which
    only tells you a report existed, not its actual magnitude relative to
    what's typical. This is the same weekly print (crude stocks Wed, NG
    storage Thu) that headline commodity news search coverage is usually
    reporting on, but as an exact structured number instead of prose. Not
    the same as a true consensus-surprise figure (that needs a paid
    Street-estimates feed we don't have) -- this is the real print's size
    relative to recent history, which is still meaningfully more structured
    than a headline search."""
    api_key = os.getenv("EIA_API_KEY")
    if not api_key:
        return {"error": "EIA_API_KEY not set. Get one free at https://www.eia.gov/opendata/register.php"}

    cfg = _EIA_INVENTORY_CONFIG.get(instrument)
    if not cfg:
        return {"instrument": instrument, "error": f"No inventory series configured for {instrument}"}

    try:
        params = {
            "api_key": api_key, "frequency": "weekly", "data[0]": "value",
            "sort[0][column]": "period", "sort[0][direction]": "desc", "length": 10,
        }
        for facet, value in cfg["facets"].items():
            params[f"facets[{facet}][]"] = value

        with httpx.Client(timeout=15.0) as client:
            resp = client.get(cfg["url"], params=params)
        resp.raise_for_status()
        rows = resp.json().get("response", {}).get("data", [])
        if len(rows) < 2:
            return {"instrument": instrument, "label": cfg["label"], "error": "Not enough history returned"}

        # EIA returns most-recent-first (sort desc above).
        values = [float(r["value"]) for r in rows]
        latest, prior = values[0], values[1]
        latest_date = rows[0]["period"]
        change = latest - prior

        trailing_changes = [values[i] - values[i + 1] for i in range(min(8, len(values) - 1))]
        avg_change = sum(trailing_changes) / len(trailing_changes) if trailing_changes else None

        direction = "build" if change > 0 else ("draw" if change < 0 else "flat")
        larger_than_typical = (
            abs(change) > abs(avg_change) * 1.5 if avg_change not in (None, 0) else None
        )

        return {
            "instrument": instrument, "series_id": rows[0].get("series"), "label": cfg["label"],
            "latest_value": round(latest, 1), "latest_date": latest_date, "units": cfg["units"],
            "week_over_week_change": round(change, 1),
            "direction": direction,
            "trailing_8wk_avg_change": round(avg_change, 1) if avg_change is not None else None,
            "larger_than_typical_move": larger_than_typical,
        }
    except Exception as e:
        logger.warning(f"get_inventory_data({instrument}) failed: {e}")
        return {"instrument": instrument, "error": str(e)}


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


# NOAA CPC's public degree-day text files -- free, no API key at all. Real
# current weather data, not the static calendar-only proxy get_seasonality
# uses. Verified working directly (curl'd real data before writing this).
_NOAA_HEATING_URL = "https://ftp.cpc.ncep.noaa.gov/htdocs/degree_days/weighted/daily_data/{year}/UtilityGas.Heating.txt"
_NOAA_COOLING_URL = "https://ftp.cpc.ncep.noaa.gov/htdocs/degree_days/weighted/daily_data/{year}/Population.Cooling.txt"


def _fetch_noaa_conus_series(url: str) -> dict:
    """Parses a NOAA CPC degree-day text file into {date_str: value} for the
    national (CONUS) row. Finds the header/CONUS rows by content rather than
    fixed line numbers, so it isn't fragile to incidental format changes."""
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(url)
    resp.raise_for_status()
    dates, values = None, None
    for line in resp.text.strip().splitlines():
        if line.startswith("Region|"):
            dates = line.split("|")[1:]
        elif line.startswith("CONUS|"):
            values = [int(v) for v in line.split("|")[1:]]
    if not dates or not values:
        raise ValueError("Could not find Region header or CONUS row in NOAA data")
    return dict(zip(dates, values))


def get_weather_demand(instrument: str) -> dict:
    """Real, current national weather-driven demand for Natural Gas --
    utility-gas-weighted Heating Degree Days (direct residential/commercial
    heating demand) and population-weighted Cooling Degree Days (a proxy for
    A/C-driven power-generation gas demand in summer). A genuinely different,
    real-time signal from get_seasonality's static calendar-only proxy.
    NATURAL_GAS only -- not meaningfully applicable to oil."""
    if instrument != "NATURAL_GAS":
        return {"instrument": instrument, "error": "Weather demand data only applies to NATURAL_GAS"}

    def recent_trend(series: dict) -> dict:
        dates_sorted = sorted(series.keys())
        recent_7 = [series[d] for d in dates_sorted[-7:]]
        prior_7 = [series[d] for d in dates_sorted[-14:-7]]
        recent_avg = sum(recent_7) / len(recent_7) if recent_7 else None
        prior_avg = sum(prior_7) / len(prior_7) if prior_7 else None
        if recent_avg is not None and prior_avg is not None and prior_avg > 0:
            trend = "rising" if recent_avg > prior_avg * 1.1 else ("falling" if recent_avg < prior_avg * 0.9 else "stable")
        else:
            trend = "stable"
        return {
            "latest_date": dates_sorted[-1], "latest_value": series[dates_sorted[-1]],
            "trailing_7day_avg": round(recent_avg, 1) if recent_avg is not None else None,
            "prior_7day_avg": round(prior_avg, 1) if prior_avg is not None else None,
            "trend": trend,
        }

    try:
        year = datetime.now().year
        heating = recent_trend(_fetch_noaa_conus_series(_NOAA_HEATING_URL.format(year=year)))
        cooling = recent_trend(_fetch_noaa_conus_series(_NOAA_COOLING_URL.format(year=year)))

        if heating["latest_value"] > 15 and heating["trend"] == "rising":
            interpretation = "Heating demand is elevated and rising -- bullish for NG"
        elif cooling["latest_value"] > 15 and cooling["trend"] == "rising":
            interpretation = "Cooling-driven power-generation demand is elevated and rising -- mildly bullish for NG"
        elif heating["latest_value"] < 5 and cooling["latest_value"] < 5:
            interpretation = "Neither heating nor cooling demand is significant right now -- shoulder-season conditions, bearish/neutral for NG"
        else:
            interpretation = "Weather-driven demand is moderate -- no strong signal either way"

        return {
            "instrument": instrument,
            "heating_degree_days": heating,
            "cooling_degree_days": cooling,
            "interpretation": interpretation,
        }
    except Exception as e:
        logger.warning(f"get_weather_demand({instrument}) failed: {e}")
        return {"instrument": instrument, "error": str(e)}
