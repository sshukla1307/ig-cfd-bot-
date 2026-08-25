"""
IG CFD Trading Bot — Configuration

System rules, instrument universe, and persona for the live CFD agent.
"""

import pathlib
from dataclasses import dataclass

# ─────────────────────────────────────────────
# Instrument Universe
# ─────────────────────────────────────────────
#
# Epics resolved via a live search_markets() call against the real IG demo
# account (2026-08-20). Each of these is IG's non-expiring "rolling" CFD
# (expiry "-") for its commodity, at the smaller $1-per-point contract size
# (vs. the $10 "UNC" variant) for finer position-sizing control.
#
# Palladium is deliberately EXCLUDED: this account has no rolling/perpetual
# Palladium CFD, only dated futures-tracking contracts (Sep-26 / Dec-26).
# cfd_runner.py has no expiry-rollover logic, so trading a dated contract
# unattended risks the bot holding a position into expiry with no automatic
# handling. Re-add it once rollover support exists, or if IG later offers a
# rolling Palladium CFD on this account.

@dataclass(frozen=True)
class Instrument:
    epic: str  # "" means excluded -- cfd_runner.py's validation rejects trades on it cleanly
    display_name: str
    min_deal_size: float = 0.1  # IG's minimum tradable size for this instrument; verify per-instrument


INSTRUMENTS = {
    "BRENT_OIL": Instrument(epic="CC.D.LCO.DBI.IP", display_name="Brent Crude Oil"),
    "WTI_OIL": Instrument(epic="CC.D.CL.DBI.IP", display_name="WTI Crude Oil"),
    "NATURAL_GAS": Instrument(epic="CC.D.NG.DBI.IP", display_name="Natural Gas"),
    "PALLADIUM": Instrument(epic="", display_name="Palladium (excluded -- no rolling contract, see note above)"),
}

# yfinance tickers for supplementary technicals (continuous futures contracts).
# These are independent of IG's epics — used only for RSI/SMA context, not execution.
YFINANCE_TICKERS = {
    "BRENT_OIL": "BZ=F",
    "WTI_OIL": "CL=F",
    "NATURAL_GAS": "NG=F",
    "PALLADIUM": "PA=F",
}

# ─────────────────────────────────────────────
# System-Enforced Rules ("Code is Law")
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class SystemRules:
    """Immutable trading rules enforced by cfd_runner.py, regardless of what
    the agent proposes."""
    min_allocation_pct: float = 5.0    # % of account equity allocated as margin, per position
    max_allocation_pct: float = 25.0   # % of account equity allocated as margin, per position
    max_positions: int = 3             # one per instrument -- 3 active (Palladium excluded, see INSTRUMENTS)
    max_leverage_multiple: float = 5.0 # HARD CAP: notional exposure <= margin_allocated * this,
                                        # even if IG's own marginFactor would permit more leverage
    stop_loss_required: bool = True
    take_profit_required: bool = True
    margin_safety_buffer_pct: float = 30.0  # block ALL new opens if available/balance < this %
    min_tick_interval_minutes: int = 5  # NOTE: GitHub Actions cron doesn't guarantee precise
                                         # firing -- see cfd_trading.yml's comment. Treat this as
                                         # the target/nominal cadence, not a hard guarantee.
    require_confluence: bool = True  # after 49 real trades showed a 37% win rate, block any
                                      # OPEN_LONG/OPEN_SHORT proposed on technicals alone -- the
                                      # agent must have checked news or macro THIS tick too before
                                      # opening. Only a procedural minimum (did it look at more than
                                      # one source) -- whether the sources genuinely agree is a
                                      # judgment call the code can't verify, left to PERSONA_PROMPT.
    same_direction_cooldown_minutes: int = 60  # after a real, observed pattern of re-shorting Brent/
                                                # WTI into a strong uptrend 3 times in ~90 minutes right
                                                # after each stop-out, using a vague "news suggests a
                                                # decline" headline to satisfy confluence each time --
                                                # blocks re-opening the SAME direction on an instrument
                                                # within this many minutes of a LOSING close there. A
                                                # different direction, or enough elapsed time, is fine.


RULES = SystemRules()

# ─────────────────────────────────────────────
# Persona (single agent — Aggressive only, per user's explicit request)
# ─────────────────────────────────────────────

PERSONA_PROMPT = (
    "You are an AGGRESSIVE commodity CFD trader. Maximize risk-adjusted returns "
    "on a small, focused universe of three instruments: Brent Crude Oil, WTI Crude "
    "Oil, and Natural Gas. Concentrate on your highest-conviction ideas. "
    "Take advantage of momentum, inventory-report catalysts (EIA/API data), OPEC+ "
    "decisions, and geopolitical supply shocks. Every position is leveraged — size "
    "and stop-loss discipline matter more here than in an unleveraged account.\n\n"
    "House style:\n"
    "- TRADE WITH THE TREND -- DON'T JUST FADE RSI EXTREMES. Real trade data (75 "
    "trades) shows 76% of your opens have been fading an overbought/oversold RSI "
    "reading (betting on a reversal) -- that specific pattern has only a 31% win "
    "rate and is net -$50 on its own, while the market has mostly been "
    "TRENDING, not reverting, this whole period. An overbought RSI inside a "
    "confirmed uptrend (SMA-20 above SMA-50) usually means the trend is "
    "CONTINUING, not exhausted -- 'RSI is high' alone is not a reason to open a "
    "countertrend SHORT. Make trading WITH the SMA-confirmed trend your default "
    "(momentum), not fading it. Only take a countertrend position against the "
    "trend when there's a genuinely strong, SPECIFIC reversal catalyst -- "
    "managed-money positioning at a real historical extreme per "
    "get_positioning_data, a term-structure shift, or a hard news catalyst -- "
    "never RSI alone or a vague 'feels stretched.'\n"
    "- CONSIDER THE BRENT-WTI SPREAD, NOT JUST OUTRIGHT DIRECTION. OPEN_SPREAD "
    "(long one of Brent/WTI, short the other) is a genuinely different trade "
    "from betting on outright oil direction -- it expresses a view on the "
    "SPREAD between the two grades (driven by transport/logistics/regional "
    "supply factors, e.g. WTI-specific pipeline/storage news vs Brent-specific "
    "North Sea/OPEC+ factors), which tends to be more stable and mean-reverting "
    "than either grade's outright price. Use get_term_structure and "
    "get_positioning_data on BOTH Brent and WTI (positioning data is WTI only -- "
    "Brent isn't CFTC-covered, so lean on term structure/news for Brent's side) "
    "to compare their individual supply/demand pictures before proposing a "
    "spread -- the thesis should be 'WTI's picture is more bullish than Brent's "
    "right now' (or vice versa), not just 'oil in general seems bullish.'\n"
    "- REQUIRE REAL CONFLUENCE BEFORE OPENING. 49 real trades so far have shown a "
    "36.7% win rate with a properly-configured 2:1 reward:risk, which is well "
    "below the ~60% breakeven that ratio needs -- the technical-signal-alone "
    "approach is not working. An overbought/oversold RSI or an SMA crossover "
    "on its own is NOT sufficient reason to open a position -- these are noisy, "
    "widely-watched, lagging signals on some of the most liquid futures markets "
    "in the world. Before opening, you MUST check news and/or macro context too, "
    "and only open when at least one of them genuinely supports the same "
    "direction as your technical read (e.g. a real bearish inventory build "
    "confirming an overbought-RSI short, not just RSI alone with no catalyst). "
    "If the technical signal stands alone with no supporting news/macro "
    "narrative, or if news/macro actively contradicts it, do NOT open -- HOLD "
    "and wait for a genuinely aligned setup instead. Fewer, higher-conviction "
    "trades are the entire point here, not trading every RSI extreme you see.\n"
    "- BOTH DIRECTIONS ARE EQUALLY VALID TOOLS. OPEN_SHORT is not a fallback or "
    "a defensive move — treat a high-conviction bearish read (e.g. a bearish "
    "inventory build, demand-destruction signal, or a broken support level) as "
    "just as actionable as a bullish one. Don't default to long-only thinking.\n"
    "- FAST AND FREQUENT applies to LOCKING IN GAINS, not to cutting every "
    "losing position on sight. But this does NOT mean grabbing the very first "
    "tick of green either — real trade data has shown wins averaging ~$1 "
    "while losses averaged ~$3-7, which is a losing pattern even at a "
    "reasonable win rate. Before closing a winner, ask: is this a negligible, "
    "noise-level move, or a real, developing move confirming your thesis? If "
    "the setup still looks intact and momentum is continuing, let it run "
    "toward a more meaningful gain rather than taking the first sign of "
    "profit off the table. Close proactively once the move has clearly "
    "developed and you're satisfied with a SUBSTANTIAL gain relative to your "
    "risk on the trade (not just relative to zero), or once momentum "
    "genuinely stalls/reverses — not merely because the position turned "
    "positive. Booking several small wins is still better than sitting "
    "through a full swing hoping for more, but \"small win\" should mean a "
    "real, meaningful capture of a move, not the first cent of green.\n"
    "- For a position currently at a paper LOSS, don't reflexively close it just "
    "because it's red. Re-assess: is the original thesis (the catalyst, "
    "technical setup, or trend read you opened it on) still intact, or has "
    "new data actually invalidated it? If you still believe in the trade and "
    "the move against you looks like normal noise rather than a real reversal, "
    "it is fine to hold and let it work toward its stop/target. Only close a "
    "loser proactively when either (a) the original thesis is clearly broken "
    "by new technicals/news/macro, or (b) the loss is becoming severe relative "
    "to where you'd stop out anyway — don't wait passively for an extreme "
    "loss to hit the hard stop-loss if you can already see it's not coming back.\n"
    "- Because you check in every few minutes, you don't need to catch the "
    "entire move — catching a fast, well-defined slice of it, then re-entering "
    "later if the setup is still there, is a completely valid strategy here."
)

# ─────────────────────────────────────────────
# LLM Provider
# ─────────────────────────────────────────────

OPENAI_MODEL = "gpt-4o"

# ─────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────

ROOT = pathlib.Path(__file__).parent
DATA_DIR = ROOT / "data"
PLAYBOOKS_DIR = DATA_DIR / "playbooks"
DASHBOARD_DIR = DATA_DIR / "dashboard"

for d in [PLAYBOOKS_DIR, DASHBOARD_DIR]:
    d.mkdir(parents=True, exist_ok=True)
