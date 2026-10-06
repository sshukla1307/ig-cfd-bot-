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
# account (2026-08-20; GOLD/SILVER added 2026-09-23, resolved live against
# the real account's own dealing platform network calls -- markets/summary/
# <epic> -- rather than guessed). Each of these is IG's non-expiring
# "rolling" CFD (expiry "-") for its commodity, at the smaller $1-per-point
# contract size (vs. the $10 "UNC" variant) for finer position-sizing
# control.
#
# min_deal_size: verified live 2026-09-23 for ALL FIVE instruments via the
# platform's own "Minimum size" field (Brent/WTI/NG were previously
# defaulting to 0.1 unverified -- the real minimum for every one of them,
# metals included, is 0.04. Corrected here instead of left wrong.).
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
    min_deal_size: float = 0.04  # IG's minimum tradable size -- verified live per-instrument, see note above


INSTRUMENTS = {
    "BRENT_OIL": Instrument(epic="CC.D.LCO.DBI.IP", display_name="Brent Crude Oil"),
    "WTI_OIL": Instrument(epic="CC.D.CL.DBI.IP", display_name="WTI Crude Oil"),
    "NATURAL_GAS": Instrument(epic="CC.D.NG.DBI.IP", display_name="Natural Gas"),
    "GOLD": Instrument(epic="CS.D.CFDGOLD.DBI.IP", display_name="Spot Gold"),
    "SILVER": Instrument(epic="CS.D.CFDSILVER.DBI.IP", display_name="Spot Silver"),
    "PALLADIUM": Instrument(epic="", display_name="Palladium (excluded -- no rolling contract, see note above)"),
}

# yfinance tickers for supplementary technicals (continuous futures contracts).
# These are independent of IG's epics — used only for RSI/SMA context, not execution.
YFINANCE_TICKERS = {
    "BRENT_OIL": "BZ=F",
    "WTI_OIL": "CL=F",
    "NATURAL_GAS": "NG=F",
    "GOLD": "GC=F",
    "SILVER": "SI=F",
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
    max_positions: int = 5             # one per instrument -- 5 active (Brent, WTI, NG, Gold, Silver;
                                        # Palladium excluded, see INSTRUMENTS). Raised from 3 on 2026-09-23
                                        # when Gold/Silver were added -- the margin_safety_buffer_pct check
                                        # below already blocks new opens once ~4 positions are open at the
                                        # 20-25% allocation band regardless, so this is a soft ceiling in
                                        # practice, not a number that actually gets reached often.
    max_leverage_multiple: float = 5.0 # HARD CAP: notional exposure <= margin_allocated * this,
                                        # even if IG's own marginFactor would permit more leverage
    stop_loss_required: bool = True
    take_profit_required: bool = True
    margin_safety_buffer_pct: float = 30.0  # block ALL new opens if available/balance < this %
    min_tick_interval_minutes: int = 60  # slowed from 5 to 60 -- the validated edge (backtest.py)
                                         # was measured on 1h bars, not 5-min ticks, and every LLM
                                         # call at 5-min cadence was pure OpenAI cost with no
                                         # matching signal resolution. NOTE: GitHub Actions cron
                                         # doesn't guarantee precise firing -- see cfd_trading.yml's
                                         # comment. Treat this as the target/nominal cadence, not a
                                         # hard guarantee.
    require_confluence: bool = True  # RE-ENABLED 2026-10-02 as part of adopting Plan C ("switch
                                      # to 1st September setup and call it plan c") -- this was the
                                      # live value on 2026-09-01, before being disabled 2026-09-24
                                      # (that history: was blocking ~60% of all proposed trades, 82
                                      # of 136 rejections in the preceding 24h alone). Note today's
                                      # actual confluence CHECK (agent_runner.check_confluence) is a
                                      # real signal-agreement verification, not Sep 1's cruder
                                      # "did it call any other tool" procedural minimum -- this flag
                                      # just turns the gate back on, it doesn't downgrade the check
                                      # itself.
    same_direction_cooldown_minutes: int = 60  # RESTORED 2026-10-02 as part of Plan C -- this was
                                                # the live value on 2026-09-01, before being disabled
                                                # 2026-09-24. Originally added after a real, observed
                                                # pattern of re-shorting Brent/WTI into a strong uptrend
                                                # 3 times in ~90 minutes right after each stop-out, using
                                                # a vague "news suggests a decline" headline to satisfy
                                                # confluence each time -- blocks re-opening the SAME
                                                # direction on an instrument within this many minutes of
                                                # a LOSING close there.
    min_hold_minutes_before_discretionary_close: int = 720  # RAISED 2026-10-02 from 180 to 720
                                                # ("switch to september 16") -- 180 was the Sep-1 value,
                                                # but this account raised it to 720 (12h) on 2026-09-02
                                                # and that was still the live value on 2026-09-16 (the
                                                # code was otherwise frozen the whole 09-02 to 09-18
                                                # window). Blocks a discretionary CLOSE (the agent
                                                # choosing to exit early, as opposed to IG's own
                                                # stop/limit or the stop-breach backstop firing) within
                                                # this many minutes of opening -- the real stop/limit and
                                                # the stop-breach backstop still protect capital
                                                # independently of this rule the whole time.
    max_consecutive_same_direction_losses: int = 2  # RE-ENABLED 2026-10-06 (user's own choice,
                                                # "enable both [circuit breaker and momentum filter]") --
                                                # prompted by the 2026-10-04/06 NATURAL_GAS incident: the
                                                # agent re-shorted NG repeatedly over several days, citing
                                                # fresh-looking reasoning (RSI, contango, inventory data)
                                                # each time while real price kept rising ~4% -- exactly the
                                                # repeated-losing-thesis pattern this breaker exists to catch.
                                                # Was temporarily disabled 2026-09-30; the last time it was
                                                # disabled together with same_direction_cooldown_minutes and
                                                # require_confluence (2026-09-24), it promptly reproduced the
                                                # exact failure pattern it exists to prevent: Brent Oil got
                                                # re-opened LONG 5 times in ~12 minutes on essentially the
                                                # same thesis, losing the same ~$6.02 three times in a row.
                                                # Originally added after a real, observed overnight incident
                                                # (2026-09-23/24: the agent re-shorted Natural Gas 8 times in
                                                # ~13 hours into a persistent rally, losing 7, each re-entry
                                                # allowed because the 60-min same_direction_cooldown had
                                                # already expired by the time the next attempt came around)
                                                # -- a single fixed-length cooldown can't survive a trend
                                                # that outlasts it. This counts the streak directly instead:
                                                # once an instrument has lost this many times in a row in
                                                # the SAME direction, that direction is blocked for
                                                # consecutive_loss_cooldown_minutes below, regardless of how
                                                # much time has passed since the last one.
    consecutive_loss_cooldown_minutes: int = 60  # LOWERED 2026-10-06 from 480 (8h) to 60 (1h) per
                                                # explicit user instruction, alongside re-enabling both
                                                # breakers -- deliberately much shorter than the original
                                                # 8h design intent (see the circuit breaker's own comment:
                                                # "the whole point is surviving a trend/repeating thesis
                                                # that a short cooldown doesn't survive"), so this is a
                                                # conscious tradeoff toward faster re-entry over the
                                                # breaker's original rationale -- watch for the same
                                                # repeated-losing-thesis pattern resuming once this shorter
                                                # window expires.
    max_consecutive_losses_any_direction: int = 2  # RE-ENABLED 2026-10-06, same prompting
                                                # incident/request as max_consecutive_same_direction_losses
                                                # above. Was temporarily disabled 2026-09-30. ADDED
                                                # 2026-09-29 after finding a real gap in
                                                # max_consecutive_same_direction_losses above: that breaker
                                                # resets to 0 the instant direction flips, so BRENT_OIL could
                                                # (and did) escape it by simply switching sides -- 2026-09-28
                                                # 14:36-14:52: 2 consecutive LONG losses tripped the
                                                # same-direction breaker for LONG, but the very next tick went
                                                # SHORT with a completely fresh streak and lost again too, all
                                                # within 20 minutes citing no new evidence -- a real instance
                                                # of the narrative-whiplash pattern (flipping between the
                                                # geopolitical-bullish and technical-bearish reads described
                                                # elsewhere in this file) rather than a stuck one-direction
                                                # bias. Counts losses regardless of direction -- a loss in
                                                # EITHER direction continues the streak, only a WIN resets it
                                                # -- so flipping sides no longer resets the count to zero.
                                                # Same threshold (2) as the same-direction breaker because
                                                # that's what the actual incident took to trip; shares the
                                                # same consecutive_loss_cooldown_minutes (now 60) block
                                                # duration above, applied to BOTH directions at once.


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
    "- WTI_OIL IS A PURE AUTO-MIRROR OF BRENT_OIL AGAIN (reverted 2026-09-24, "
    "back from a brief independent-trading window 2026-09-23/24) -- you only "
    "ever decide on BRENT_OIL, NATURAL_GAS, GOLD, and SILVER. Whenever you "
    "OPEN_LONG or OPEN_SHORT Brent, the identical direction and size is "
    "automatically opened on WTI too, and closing Brent closes its WTI "
    "mirror automatically. Never propose a trade on WTI_OIL yourself, and "
    "don't spend tool calls researching WTI's own technicals/term structure/ "
    "positioning -- it adds nothing since WTI no longer gets an independent "
    "decision. (WTI's brief independence was reverted because its own CFTC "
    "positioning data -- a signal Brent structurally never gets, being "
    "ICE-listed and outside CFTC jurisdiction -- kept producing opposite-"
    "direction trades from Brent despite the two benchmarks' technicals "
    "usually agreeing and their daily returns being ~0.94 correlated "
    "historically; not a bug, just not the desired behavior here.)\n"
    "- YOUR DEFAULT STRATEGY DIFFERS BY INSTRUMENT -- backed by an actual "
    "backtest over 2 years of hourly data, not a guess: BRENT: trend-following "
    "wins here (+71% net over 151 trades, 55% win rate) while fading RSI "
    "extremes loses (-22% net, 43% win rate) -- default to trading WITH the "
    "SMA-confirmed trend, only fade an RSI extreme against it given a "
    "genuinely strong, specific catalyst. NATURAL GAS: the OPPOSITE is true "
    "-- fading RSI extremes (mean-reversion) wins here (+193% net, 46% win "
    "rate) while trend-following is weaker (+59% net) -- NG genuinely "
    "mean-reverts more than it trends (consistent with its storage-driven "
    "price behavior), so treat an overbought/oversold RSI on NG as a real "
    "signal worth acting on, not noise to ignore. BUT a real incident "
    "(2026-09-23/24) showed this default lean can fail badly: the agent "
    "shorted NG 8 times in ~13 hours fading what it read as overbought RSI, "
    "while NG was actually in a persistent, one-directional rally the whole "
    "time -- losing 7 of 8. The backtested mean-reversion edge is real on "
    "average over 2 years, but it assumes NG chops and reverts; it does NOT "
    "override a genuine, sustained trend that hasn't shown any sign of "
    "turning. Before fading an NG RSI extreme, check that price action isn't "
    "already making a series of fresh highs/lows in one direction with no "
    "reversion attempts -- if it is, that's evidence AGAINST the mean-reversion "
    "read for this specific instance, not a reason to fade harder. Same "
    "discipline as Silver (see below): a real, independent, non-technical "
    "catalyst (positioning extreme, an actual inventory print, current "
    "weather demand data, credible news) should confirm the fade, not just "
    "the RSI number alone, and after 2 consecutive losing shorts (or longs) "
    "the consecutive-loss circuit breaker will block that direction for 8 "
    "hours regardless -- treat that block as a signal to stop looking for "
    "reasons to re-enter the same way, not an obstacle to route around. "
    "UPDATE 2026-09-28: this 'check price isn't already breaking out against "
    "the fade' instruction is now ALSO enforced in code, not just advisory -- "
    "a real-trade replay of 150 NG trades found the pattern above (fading "
    "into a persistent trend) was STILL happening well after this paragraph "
    "was written, so _validate_trade now hard-rejects any NATURAL_GAS "
    "OPEN_LONG/OPEN_SHORT whose direction conflicts with the last 30 minutes "
    "of price action, regardless of how strong the RSI/mean-reversion thesis "
    "looks. If a NATURAL_GAS proposal comes back rejected with 'momentum "
    "filter' in the reason, that means this exact situation -- wait for "
    "price to actually turn before proposing that fade again, don't retry "
    "the same direction immediately.\n"
    "- GOLD AND SILVER (added 2026-09-23) -- backed by the same 2-year hourly "
    "backtest methodology: GOLD gets a real trend-following lean, nearly as "
    "strong as Brent's (+38% net over 156 trades, 51% win rate, vs fading RSI's "
    "weak +7%) -- trade WITH the SMA-confirmed trend by default, same as Brent. "
    "SILVER also leans trend-following, but MUCH more thinly than Gold or Brent "
    "(+9% net over 151 trades, 47% win rate -- barely positive, while fading RSI "
    "on Silver loses badly at -60%). A technical trend read alone is therefore a "
    "real but WEAK reason to trade Silver, and you should lean much more heavily "
    "on a genuine non-technical "
    "catalyst (positioning extreme, credible news, a term-structure shift) "
    "before sizing a Silver trade at the top of your allocation band. Gold and "
    "Silver are NOT available for get_weather_demand or get_inventory_data "
    "(no EIA coverage for metals) -- get_positioning_data (CFTC COMEX gold/"
    "silver) and get_term_structure both are.\n"
    "- A fresh, specific, credible catalyst (a real news event, positioning "
    "at a genuine historical extreme, a term-structure shift) can override "
    "the instrument's default lean in either direction -- conflicting signals "
    "are not a reason to do nothing, they're a reason to pick the side with "
    "the stronger, more specific evidence and act on it, sized to your "
    "conviction. A fast-moving news event that hasn't been technically "
    "'confirmed' yet by a lagging indicator is still real information -- "
    "don't wait for the SMA to catch up to the news before acting on it.\n"
    "- Both directions are equally valid on every instrument -- OPEN_SHORT "
    "is not a fallback, treat a high-conviction bearish read as just as "
    "actionable as a bullish one. Don't repeat the identical trade tick "
    "after tick out of habit; every check-in, reconsider each instrument "
    "fresh.\n"
    "- Booking frequent, meaningful gains beats holding for a home run, but "
    "don't grab the very first tick of green either -- let a real move develop "
    "before banking it, and don't reflexively close a loser just because it's "
    "red if the original thesis still holds. Both calls should be about "
    "whether the thesis is still intact, not the mere sign of the P&L.\n"
    "- You check in about once an hour, and a discretionary close is blocked "
    "for the first 12 hours after opening -- this strategy's edge (see the "
    "backtest) resolves over many hours, not minutes. Let the thesis actually "
    "play out toward its stop/target instead of grabbing a fast slice; you "
    "can always re-enter later if the setup is still there, but exiting a "
    "still-valid thesis early just to bank a small move is exactly the habit "
    "that has been cutting winners short."
)

# ─────────────────────────────────────────────
# LLM Provider
# ─────────────────────────────────────────────

# Tried Anthropic (Claude) once before (see git history) to see whether
# behavioral drift across a large, complex system prompt (RSI-fade fixation,
# then repeating the identical Brent-WTI spread ~15 times in a row) improved
# with a different model. It did fix both of those specific behavioral bugs,
# but the real-money result was 4 losing trades in a row immediately after
# switching (and after an aggressive-persona rebalance) -- reinforcing that
# this was a signal-quality problem, not a which-model problem, exactly as
# expected going in. Reverted to OpenAI at the time.
#
# SWITCHED BACK TO "anthropic" on 2026-09-29, per explicit user instruction
# ("change model from OPEN AI to claude"). Watch closely: the prior attempt's
# 4-losing-trades outcome is a real precedent, but a lot has changed since
# then that didn't exist during that attempt -- per-instrument floor/arm
# tuning (see cfd_runner.PLANS), the recent-trade-history injection into the
# prompt, the any-direction consecutive-loss circuit breaker, and (new
# 2026-09-29) an adversarial second-opinion critic on every proposed trade
# (agent_runner.get_second_opinion) -- so this isn't a repeat of the earlier
# bare swap.
#
# SUPERSEDED 2026-10-01 as the sole decision-maker: per explicit user
# instruction ("For WTI, CRUDE, SPOT GOLD use openAI to make decision. For NG
# and Silver use Claude"), the primary decision is now made PER INSTRUMENT via
# INSTRUMENT_LLM_PROVIDER below, not by this single global value. LLM_PROVIDER
# itself is kept only as the fallback default for an instrument that somehow
# isn't in that mapping (agent_runner._make_llm_client / get_second_opinion's
# fallback) -- every one of this account's 5 real instruments is explicitly
# mapped below, so this fallback shouldn't actually be exercised in practice.
LLM_PROVIDER = "anthropic"  # "anthropic" or "openai" -- fallback default only, see note above
OPENAI_MODEL = "gpt-4o"
OPENAI_RESEARCH_MODEL = "gpt-4o-mini"  # used ONLY for the intermediate tool-selection
# turns (get_technicals/get_commodity_news/etc.) within a tick's research loop -- those
# are mechanical function-picking steps, not the trade judgment itself. The actual
# propose_trades decision is always re-asked of OPENAI_MODEL once research concludes
# (see OpenAIClient.generate), so trade quality shouldn't be affected -- only the cost
# of the research turns, which was most of a tick's token spend at 5-6 calls/tick.
ANTHROPIC_MODEL = "claude-sonnet-5"

# ADDED 2026-10-01 per explicit user instruction ("For WTI, CRUDE, SPOT GOLD use
# openAI to make decision. For NG and Silver use Claude"). This is now what
# actually decides the primary trade-proposal provider for each instrument,
# not LLM_PROVIDER above. agent_runner.get_agent_trades is called ONCE PER
# PROVIDER GROUP per tick (cfd_runner.run_cfd_tick groups INSTRUMENT_LLM_PROVIDER
# by value), each call scoped to only its own group's instruments -- see
# agent_runner.build_system_prompt's "YOUR SCOPE THIS CHECK-IN" section and
# _tools_for_instruments/_propose_trades_schema_for_instruments.
#
# WTI_OIL maps to the SAME provider as BRENT_OIL (openai) even though WTI is
# never given its own decision -- it's a pure mirror of whatever BRENT_OIL
# decides (see agent_runner's WTI-mirror comments), so there's no separate
# "WTI decision" for a provider to make; this entry exists mainly so the
# mapping is total over every instrument in INSTRUMENTS, and so it's obvious
# at a glance which provider effectively drives WTI's position too.
#
# Historical note: Anthropic was tried once before as the SOLE primary
# decision-maker (see LLM_PROVIDER's comment above) and produced 4 losing
# trades in a row immediately after switching. This split is a narrower bet
# than that -- Claude only drives NATURAL_GAS and SILVER now, not the whole
# account -- but it's still worth watching those two instruments specifically
# for a similar pattern.
INSTRUMENT_LLM_PROVIDER = {
    # REVERTED 2026-10-02 to single-OpenAI for all 5 instruments, as part of adopting Plan C
    # ("switch to 1st September setup and call it plan c") -- the NATURAL_GAS/SILVER -> Claude
    # split (2026-10-01) didn't exist on Sep 1; OpenAI was the sole provider for everything.
    # This mapping is global, not plan-scoped like PLANS -- switching ACTIVE_PLAN back to "A" or
    # "B" later will NOT automatically restore the split; it would need to be set back manually
    # to {"BRENT_OIL": "openai", "WTI_OIL": "openai", "GOLD": "openai", "NATURAL_GAS":
    # "anthropic", "SILVER": "anthropic"}.
    "BRENT_OIL": "openai",
    "WTI_OIL": "openai",     # mirrors BRENT_OIL's decision automatically, never asked independently
    "GOLD": "openai",
    "NATURAL_GAS": "openai",
    "SILVER": "openai",
}

PAUSED_INSTRUMENTS = set()  # CLEARED 2026-10-02 as part of adopting Plan C (user's own choice,
# "lift the pause") -- was {"BRENT_OIL", "WTI_OIL", "GOLD"} from an earlier same-day request to
# stop trading those three and skip OpenAI calls entirely. Re-add instruments here (same
# mechanics as before: excluded from cfd_runner.run_cfd_tick's provider-group loop, and the
# provider group for an instrument-less provider is skipped entirely) to pause again.

# ─────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────

ROOT = pathlib.Path(__file__).parent
DATA_DIR = ROOT / "data"
PLAYBOOKS_DIR = DATA_DIR / "playbooks"
DASHBOARD_DIR = DATA_DIR / "dashboard"

for d in [PLAYBOOKS_DIR, DASHBOARD_DIR]:
    d.mkdir(parents=True, exist_ok=True)
