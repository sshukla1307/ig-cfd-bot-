"""
IG CFD Trading Bot — Offline Strategy Backtest

Built after live trading showed persistent losses across every LLM
provider/persona tried (GPT-4o, Claude, aggressive rebalance) -- before
spending more real money testing hypotheses live, this replays the
QUANTITATIVE parts of the strategy (technical entry/exit rules) against
2+ years of real historical price data.

Important honest limitation: this can only test mechanical, rule-based
entries (RSI/SMA-based), not the qualitative confluence/catalyst
weighing the live LLM agent does (historical news sentiment, positioning
context, etc. aren't replicated here). It answers "does fading RSI
extremes or following the trend have ANY historical statistical edge on
these 3 instruments," not "would the exact live agent have been
profitable." That's the right question to answer first, since every
live iteration has assumed technicals-plus-more-signals could work at
all -- if even the simplified mechanical version has no edge, that's a
strong signal the fundamental approach doesn't, regardless of which LLM
or how many more signals get added.

Run: python backtest.py
"""

import logging

import numpy as np
import pandas as pd
import yfinance as yf

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

TICKERS = {"BRENT_OIL": "BZ=F", "WTI_OIL": "CL=F", "NATURAL_GAS": "NG=F"}

# Matches the real bot's rules firewall shape (RULES in config.py): mandatory
# stop-loss AND take-profit on every trade, roughly the same order of
# magnitude the live agent actually used (observed stop distances were
# ~3-5% of price, limits ~2x that).
STOP_PCT = 0.04
TARGET_PCT = 0.08
MAX_HOLD_BARS = 48  # hours -- 2 days, matches typical real holding periods observed live


def fetch_history(ticker: str, period: str = "730d", interval: str = "1h") -> pd.DataFrame:
    hist = yf.Ticker(ticker).history(period=period, interval=interval)
    hist = hist[["Open", "High", "Low", "Close"]].dropna()
    return hist


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Same formulas as market_data.get_technicals, vectorized over the whole
    history instead of just the latest point, so the backtest's technical
    reads are directly comparable to what the live agent actually saw."""
    df = df.copy()
    close = df["Close"]
    df["sma_20"] = close.rolling(20).mean()
    df["sma_50"] = close.rolling(50).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi_14"] = 100 - (100 / (1 + rs))
    return df.dropna()


def _run_trade(df: pd.DataFrame, entry_idx: int, direction: str) -> dict:
    """Walks forward from entry_idx until stop, target, or MAX_HOLD_BARS hits.
    direction: 'LONG' or 'SHORT'. Returns a trade record with pct P&L."""
    entry_price = df["Close"].iloc[entry_idx]
    sign = 1 if direction == "LONG" else -1
    stop_price = entry_price * (1 - sign * STOP_PCT)
    target_price = entry_price * (1 + sign * TARGET_PCT)

    end_idx = min(entry_idx + MAX_HOLD_BARS, len(df) - 1)
    for i in range(entry_idx + 1, end_idx + 1):
        bar = df.iloc[i]
        if direction == "LONG":
            if bar["Low"] <= stop_price:
                return _trade_record(df, entry_idx, i, direction, entry_price, stop_price)
            if bar["High"] >= target_price:
                return _trade_record(df, entry_idx, i, direction, entry_price, target_price)
        else:
            if bar["High"] >= stop_price:
                return _trade_record(df, entry_idx, i, direction, entry_price, stop_price)
            if bar["Low"] <= target_price:
                return _trade_record(df, entry_idx, i, direction, entry_price, target_price)

    # Neither hit within MAX_HOLD_BARS -- close at that bar's close price
    exit_price = df["Close"].iloc[end_idx]
    return _trade_record(df, entry_idx, end_idx, direction, entry_price, exit_price)


def _trade_record(df, entry_idx, exit_idx, direction, entry_price, exit_price) -> dict:
    sign = 1 if direction == "LONG" else -1
    pct_pnl = (exit_price - entry_price) / entry_price * sign
    return {
        "entry_time": df.index[entry_idx], "exit_time": df.index[exit_idx],
        "direction": direction, "entry_price": entry_price, "exit_price": exit_price,
        "pct_pnl": pct_pnl, "hold_bars": exit_idx - entry_idx,
    }


def simulate_fade_rsi(df: pd.DataFrame, overbought: float = 70, oversold: float = 30) -> list:
    """Mean-reversion: short overbought RSI, long oversold RSI -- the pattern
    that made up 76% of real live trades at a 31% win rate."""
    trades = []
    i = 20  # need enough history for indicators to be valid already (df is pre-filtered)
    while i < len(df) - 1:
        rsi = df["rsi_14"].iloc[i]
        if rsi >= overbought:
            trade = _run_trade(df, i, "SHORT")
            trades.append(trade)
            i = list(df.index).index(trade["exit_time"]) + 1
        elif rsi <= oversold:
            trade = _run_trade(df, i, "LONG")
            trades.append(trade)
            i = list(df.index).index(trade["exit_time"]) + 1
        else:
            i += 1
    return trades


def simulate_trend_follow(df: pd.DataFrame, rsi_extreme: float = 70, rsi_extreme_low: float = 30) -> list:
    """Momentum: trade WITH an SMA-20/50 crossover, but skip entries where
    RSI is already at a fresh extreme in that direction (avoid buying right
    at exhaustion) -- the pivot the persona was rewritten toward after 75
    real trades showed RSI-fading wasn't working."""
    trades = []
    i = 20
    prev_trend = None
    while i < len(df) - 1:
        sma20, sma50, rsi = df["sma_20"].iloc[i], df["sma_50"].iloc[i], df["rsi_14"].iloc[i]
        trend = "UP" if sma20 > sma50 else "DOWN"
        # Only enter on a FRESH crossover (trend just changed), not every bar
        # the trend happens to already be in place (that would over-trade).
        if prev_trend is not None and trend != prev_trend:
            if trend == "UP" and rsi < rsi_extreme:
                trade = _run_trade(df, i, "LONG")
                trades.append(trade)
                i = list(df.index).index(trade["exit_time"]) + 1
                prev_trend = trend
                continue
            elif trend == "DOWN" and rsi > rsi_extreme_low:
                trade = _run_trade(df, i, "SHORT")
                trades.append(trade)
                i = list(df.index).index(trade["exit_time"]) + 1
                prev_trend = trend
                continue
        prev_trend = trend
        i += 1
    return trades


def summarize(trades: list) -> dict:
    if not trades:
        return {"n": 0, "win_rate": None, "avg_win_pct": None, "avg_loss_pct": None, "net_pct": 0.0}
    wins = [t for t in trades if t["pct_pnl"] > 0]
    losses = [t for t in trades if t["pct_pnl"] <= 0]
    return {
        "n": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "avg_win_pct": (sum(t["pct_pnl"] for t in wins) / len(wins) * 100) if wins else None,
        "avg_loss_pct": (sum(t["pct_pnl"] for t in losses) / len(losses) * 100) if losses else None,
        "net_pct": sum(t["pct_pnl"] for t in trades) * 100,
    }


def main():
    print(f"Backtest params: stop={STOP_PCT*100:.0f}%, target={TARGET_PCT*100:.0f}% (2:1), "
          f"max hold={MAX_HOLD_BARS}h, data=1h bars, ~2 years\n")
    print(f"{'Instrument':14} {'Strategy':16} {'Trades':>7} {'WinRate':>8} {'AvgWin%':>8} {'AvgLoss%':>9} {'Net%':>8}")
    print("-" * 80)

    results = {}
    for key, ticker in TICKERS.items():
        raw = fetch_history(ticker)
        df = compute_indicators(raw)
        results[key] = {}

        for strat_name, strat_fn in [("fade_rsi", simulate_fade_rsi), ("trend_follow", simulate_trend_follow)]:
            trades = strat_fn(df)
            stats = summarize(trades)
            results[key][strat_name] = stats
            wr = f"{stats['win_rate']:.1f}%" if stats["win_rate"] is not None else "n/a"
            aw = f"{stats['avg_win_pct']:.2f}%" if stats["avg_win_pct"] is not None else "n/a"
            al = f"{stats['avg_loss_pct']:.2f}%" if stats["avg_loss_pct"] is not None else "n/a"
            print(f"{key:14} {strat_name:16} {stats['n']:>7} {wr:>8} {aw:>8} {al:>9} {stats['net_pct']:>7.2f}%")

    return results


if __name__ == "__main__":
    main()
