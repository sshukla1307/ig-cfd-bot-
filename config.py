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
# *** REQUIRED MANUAL STEP BEFORE FIRST RUN ***
# IG identifies every tradable instrument by an account-specific "epic" string
# (e.g. "CC.D.LCO.UNC.IP" for a Brent Crude future — NOT verified, just an
# example of the shape). These are NOT guessed here on purpose: getting an
# epic wrong either fails loudly (safe) or, worse, silently trades the wrong
# instrument. Resolve the real epics yourself once you have API access:
#
#   from trading_ig import IGService
#   ig = IGService(username, password, api_key, acc_type="demo")
#   ig.create_session()
#   print(ig.search_markets("Brent Crude"))
#   print(ig.search_markets("WTI Crude Oil"))
#   print(ig.search_markets("Natural Gas"))
#   print(ig.search_markets("Palladium"))
#
# Each result includes an "epic" field — copy the correct one (check
# instrumentType/expiry match what you intend to trade) into INSTRUMENTS below.

@dataclass(frozen=True)
class Instrument:
    epic: str  # "" until resolved — see instructions above
    display_name: str
    min_deal_size: float = 0.1  # IG's minimum tradable size for this instrument; verify per-instrument


INSTRUMENTS = {
    "BRENT_OIL": Instrument(epic="", display_name="Brent Crude Oil"),
    "WTI_OIL": Instrument(epic="", display_name="WTI Crude Oil"),
    "NATURAL_GAS": Instrument(epic="", display_name="Natural Gas"),
    "PALLADIUM": Instrument(epic="", display_name="Palladium"),
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
    max_positions: int = 4             # one per instrument — matches the 4-instrument universe
    max_leverage_multiple: float = 5.0 # HARD CAP: notional exposure <= margin_allocated * this,
                                        # even if IG's own marginFactor would permit more leverage
    stop_loss_required: bool = True
    take_profit_required: bool = True
    margin_safety_buffer_pct: float = 30.0  # block ALL new opens if available/balance < this %
    min_tick_interval_minutes: int = 5


RULES = SystemRules()

# ─────────────────────────────────────────────
# Persona (single agent — Aggressive only, per user's explicit request)
# ─────────────────────────────────────────────

PERSONA_PROMPT = (
    "You are an AGGRESSIVE commodity CFD trader. Maximize risk-adjusted returns "
    "on a small, focused universe of four instruments: Brent Crude Oil, WTI Crude "
    "Oil, Natural Gas, and Palladium. Concentrate on your highest-conviction ideas. "
    "Take advantage of momentum, inventory-report catalysts (EIA/API data), OPEC+ "
    "decisions, and geopolitical supply shocks. Every position is leveraged — size "
    "and stop-loss discipline matter more here than in an unleveraged account."
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
