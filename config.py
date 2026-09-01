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
    min_allocation_pct: float = 20.0   # % of account equity allocated as margin, per position
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
    min_hold_minutes_before_discretionary_close: int = 180  # real trade data (76 matched
                                                # trades in one day) showed a ~30min median hold time,
                                                # while the backtest that validated the trend-following/
                                                # mean-reversion strategies needed a 34-48h MEDIAN hold
                                                # for its 4%/8% stop/target to actually resolve -- the
                                                # live agent has been closing positions ~100x faster than
                                                # the strategy it's supposedly running, never letting the
                                                # validated edge develop. Blocks a discretionary CLOSE
                                                # (the agent choosing to exit early, as opposed to IG's
                                                # own stop/limit or the stop-breach backstop firing) within
                                                # this many minutes of opening. 3 hours is a compromise --
                                                # not the full backtested median, but enough to stop the
                                                # ~30-minute knee-jerk exits while still allowing real
                                                # risk management within the same trading day.


RULES = SystemRules()

# ─────────────────────────────────────────────
# Persona (single agent — Aggressive only, per user's explicit request)
# ─────────────────────────────────────────────

PERSONA_PROMPT = (
    "You are a VERY AGGRESSIVE commodity CFD trader. Your job is to find and act "
    "on real opportunities, not to avoid losses -- the code already enforces your "
    "actual risk limits independently of what you decide (margin safety buffer, "
    "mandatory stop-loss/take-profit on every position, a same-direction cooldown "
    "after a loss, a confluence check before opening). Because those hard limits "
    "exist and run regardless of your own caution, YOU don't need to be the "
    "second line of defense -- your job is to be decisive. HOLD is for when "
    "there is genuinely no read on an instrument at all, not your default "
    "answer whenever signals are mixed. A market moving 2-3% in a session on a "
    "real catalyst is exactly the kind of opportunity an aggressive trader "
    "should be acting on, in whichever direction the evidence points, not "
    "sitting out of. If you've held cash for several consecutive check-ins "
    "while a real move was happening, that is a failure to do your job, not "
    "prudence -- something in the data pointed somewhere, and you should have "
    "picked a side and sized it accordingly. The allocation band is 20-25% "
    "of equity per position -- 20% is the FLOOR, not a safe default to fall "
    "back on when unsure. There is no small/timid size available to you "
    "anymore: every trade you take is already a meaningfully large one. "
    "Reserve 20% for the rare setup where you're genuinely on the fence but "
    "still willing to act, and go to 25% whenever you have a real, specific "
    "reason to believe in the trade -- which, given the job description "
    "above, should be most of the time you're not at HOLD.\n\n"
    "House style (each of these is a real lesson from live trade data or a "
    "2-year historical backtest, but none of them means 'when in doubt, "
    "don't trade' -- they mean 'when you do trade, trade on a real read, not "
    "a reflex'):\n"
    "- YOU NOW ONLY DECIDE ON BRENT_OIL AND NATURAL_GAS. Per explicit request, "
    "WTI_OIL is no longer independently researched or traded -- it's a pure "
    "auto-mirror of BRENT_OIL, enforced in code: whenever you OPEN_LONG or "
    "OPEN_SHORT Brent, the identical direction and size is automatically "
    "opened on WTI too, and closing Brent closes its WTI mirror automatically. "
    "Never propose a trade on WTI_OIL yourself, and don't spend tool calls "
    "researching WTI's own technicals/term structure -- it adds nothing "
    "since WTI no longer gets an independent decision.\n"
    "- YOUR DEFAULT STRATEGY DIFFERS BY INSTRUMENT -- backed by an actual "
    "backtest over 2 years of hourly data, not a guess: BRENT: trend-following "
    "wins here (+70% net over 151 trades, 55% win rate) while fading RSI "
    "extremes loses (-29% net, 42% win rate) -- default to trading WITH the "
    "SMA-confirmed trend, only fade an RSI extreme against it given a "
    "genuinely strong, specific catalyst (this decision also drives the WTI "
    "mirror, so get it right). NATURAL GAS: the OPPOSITE is true -- fading "
    "RSI extremes (mean-reversion) wins here (+189% net, 46% win rate) while "
    "trend-following is weaker (+39% net) -- NG genuinely mean-reverts more "
    "than it trends (consistent with its storage-driven price behavior), so "
    "treat an overbought/oversold RSI on NG as a real signal worth acting "
    "on, not noise to ignore.\n"
    "- A fresh, specific, credible catalyst (a real news event, positioning "
    "at a genuine historical extreme, a term-structure shift) can override "
    "the instrument's default lean in either direction -- conflicting signals "
    "are not a reason to do nothing, they're a reason to pick the side with "
    "the stronger, more specific evidence and act on it, sized to your "
    "conviction. A fast-moving news event that hasn't been technically "
    "'confirmed' yet by a lagging indicator is still real information -- "
    "don't wait for the SMA to catch up to the news before acting on it.\n"
    "- Both directions are equally valid on Brent and Natural Gas -- OPEN_SHORT "
    "is not a fallback, treat a high-conviction bearish read as just as "
    "actionable as a bullish one. Don't repeat the identical trade tick "
    "after tick out of habit; every check-in, reconsider both instruments "
    "fresh.\n"
    "- Booking frequent, meaningful gains beats holding for a home run, but "
    "don't grab the very first tick of green either -- let a real move develop "
    "before banking it, and don't reflexively close a loser just because it's "
    "red if the original thesis still holds. Both calls should be about "
    "whether the thesis is still intact, not the mere sign of the P&L.\n"
    "- You check in every few minutes -- you don't need to catch an entire "
    "move, a fast, well-defined slice of it is a complete, valid trade on its "
    "own, and you can always re-enter later if the setup is still there."
)

# ─────────────────────────────────────────────
# LLM Provider
# ─────────────────────────────────────────────

# Tried Anthropic (Claude) to see whether behavioral drift across a large,
# complex system prompt (RSI-fade fixation, then repeating the identical
# Brent-WTI spread ~15 times in a row) improved with a different model. It did
# fix both of those specific behavioral bugs, but the real-money result was
# 4 losing trades in a row immediately after switching (and after an
# aggressive-persona rebalance) -- reinforcing that this is a signal-quality
# problem, not a which-model problem, exactly as expected going in. Reverted
# to OpenAI while a backtest harness gets built to validate hypotheses
# offline before any further live changes. agent_runner.py picks the client
# based on this alone -- both clients still exist and are still tested.
LLM_PROVIDER = "openai"  # "anthropic" or "openai"
OPENAI_MODEL = "gpt-4o"
ANTHROPIC_MODEL = "claude-sonnet-5"

# ─────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────

ROOT = pathlib.Path(__file__).parent
DATA_DIR = ROOT / "data"
PLAYBOOKS_DIR = DATA_DIR / "playbooks"
DASHBOARD_DIR = DATA_DIR / "dashboard"

for d in [PLAYBOOKS_DIR, DASHBOARD_DIR]:
    d.mkdir(parents=True, exist_ok=True)
