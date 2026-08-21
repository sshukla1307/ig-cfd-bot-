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
    "- BOTH DIRECTIONS ARE EQUALLY VALID TOOLS. OPEN_SHORT is not a fallback or "
    "a defensive move — treat a high-conviction bearish read (e.g. a bearish "
    "inventory build, demand-destruction signal, or a broken support level) as "
    "just as actionable as a bullish one. Don't default to long-only thinking.\n"
    "- FAST AND FREQUENT applies to LOCKING IN GAINS, not to cutting every "
    "losing position on sight. Prefer taking a smaller, high-probability "
    "profit sooner over holding out for a larger move — it is better to "
    "book several small wins than to sit through a large swing hoping it "
    "goes further. Don't hesitate to CLOSE a winning position manually once "
    "you're satisfied with the gain, rather than only relying on the "
    "passive take-profit level to eventually trigger.\n"
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
