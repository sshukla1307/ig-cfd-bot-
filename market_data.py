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
