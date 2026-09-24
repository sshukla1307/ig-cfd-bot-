"""
IG CFD Trading Bot — Tick Runner

Invoked roughly every 5 minutes by GitHub Actions (a nominal target -- GitHub
doesn't guarantee precise cron timing; see cfd_trading.yml). Each tick:
  1. Kill-switch check.
  2. Connect to IG, fetch account state + our 4 tracked positions.
  3. Margin-based exit sync: every new position already gets REAL resting IG stop
     and limit orders at MARGIN_STOP_LOSS_PCT/MARGIN_PROFIT_TAKE_PCT of its margin
     (see _margin_based_stop_distance/_margin_based_limit_distance) -- IG's own
     engine executes either instantly, independent of our tick cadence. This step
     just reconciles any position whose live stop/limit doesn't already match
     those targets (predates the feature/value, or drifted) by amending them in
     place (_sync_margin_based_exits) -- never closes anything itself, EXCEPT for
     TRAILING_STOP_INSTRUMENTS positions stuck at a loss past the trailing-stop
     scheme's stale-loss timeout (see TRAILING_STOP_INSTRUMENTS), which it does
     force-close.
  4. Margin safety check: block ALL new opens account-wide if available
     margin has fallen below RULES.margin_safety_buffer_pct of balance.
  5. Per-instrument: skip any instrument whose market isn't currently
     TRADEABLE (read live from IG each tick, not a hardcoded calendar --
     commodity CFDs follow underlying futures session hours + maintenance
     windows + weekend closures, unlike 24/5 forex).
  6. Let the agent decide (or HOLD).
  7. Validate every proposed trade against the rules firewall, then execute.
  8. Log + export dashboard data.

Two independent switches must BOTH be explicitly true for any order to fire:
  IG_LIVE_TRADING_ENABLED=true   (master kill switch)
  IG_LIVE=true                   (live vs demo IG environment; defaults to demo)

*** MARGIN SIZING MATH STILL UNVERIFIED AGAINST A REAL FILL ***
Session/login and epic resolution ARE confirmed working (2026-08-20, demo
account SNDPM; Gold/Silver epics added 2026-09-23) -- see config.py for the
resolved epics. Trades 5 instruments: Brent Crude Oil, WTI Crude Oil,
Natural Gas, Spot Gold, Spot Silver. Palladium is deliberately excluded
(no rolling contract available -- see config.py).

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
from typing import Optional

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


def _cooldown_reset_marker_path(order_log_path: Path) -> Path:
    return order_log_path.parent / "cooldown_reset_at.json"


def _get_cooldown_reset_at(order_log_path: Path) -> str:
    """ISO timestamp of the last manual cooldown reset (see reset_cooldowns_now),
    or "" if one has never been requested. Any close event at/before this
    timestamp is ignored by _iter_close_events for cooldown/circuit-breaker
    purposes ONLY -- it does not touch, edit, or hide the real trade record
    anywhere else (dashboard, P&L, audit log all still show it exactly as it
    happened)."""
    path = _cooldown_reset_marker_path(order_log_path)
    if not path.exists():
        return ""
    try:
        return json.loads(path.read_text()).get("reset_at", "")
    except (json.JSONDecodeError, OSError):
        return ""


def reset_cooldowns_now(order_log_path: Path = None) -> str:
    """Manually clears every currently-active same-direction cooldown and
    consecutive-loss circuit breaker by recording 'now' as a cutoff: every
    real close logged at/before this moment stops counting toward either
    check, on every instrument/direction at once. Deliberately does NOT edit
    or delete anything in order_log.jsonl -- the real trade history stays
    exactly as it happened; only the cooldown/circuit-breaker LOOKUP ignores
    it from here on. A genuine new loss after this point still starts a
    fresh cooldown/streak normally -- this is a one-time clear, not a
    disable. Returns the reset timestamp actually written."""
    order_log_path = order_log_path or (DATA_DIR / "order_log.jsonl")
    now = datetime.now(timezone.utc).isoformat()
    order_log_path.parent.mkdir(parents=True, exist_ok=True)
    _cooldown_reset_marker_path(order_log_path).write_text(json.dumps({"reset_at": now}))
    return now


def _iter_close_events(order_log_path: Path):
    """Yields {"instrument", "direction", "logged_at", "is_loss"} for every
    real close in the audit log, in file order -- both agent-initiated CLOSE
    (action="CLOSE", status="submitted", profit read from raw.profit) AND
    broker-side fills (action="BROKER_CLOSE", status="detected", profit read
    from estimated_pnl -- see _log_broker_closes). Both feed the
    same-direction cooldown and the consecutive-loss circuit breaker; missing
    BROKER_CLOSE logging for ordinary stop/limit fills (the dominant way
    positions actually close on this account) is exactly what let a real
    losing streak (2026-09-23/24, Natural Gas re-shorted 8 times in ~13
    hours) go completely uncounted by both cooldowns -- neither a CLOSE nor
    the OLD deal-id-diff-only vanish detection ever wrote anything either
    cooldown could see. Missing/older entries (logged before
    "original_direction" was added) are simply skipped -- fails permissive,
    not unsafe, since the worst case is just not cooling down.

    Events at/before the last reset_cooldowns_now() cutoff (see
    _get_cooldown_reset_at) are skipped entirely -- a manual, one-time clear
    of every currently-active cooldown/circuit-breaker without touching the
    underlying trade record."""
    if not order_log_path.exists():
        return
    reset_at = _get_cooldown_reset_at(order_log_path)
    with open(order_log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            action = d.get("action")
            instrument = d.get("instrument")
            direction = d.get("original_direction")
            logged_at = d.get("logged_at")
            if not (instrument and direction and logged_at):
                continue
            if reset_at and logged_at <= reset_at:
                continue
            if action == "CLOSE" and d.get("status") == "submitted":
                profit = d.get("raw", {}).get("profit")
            elif action == "BROKER_CLOSE" and d.get("status") == "detected":
                profit = d.get("estimated_pnl")
            else:
                continue
            yield {
                "instrument": instrument, "direction": direction, "logged_at": logged_at,
                "is_loss": (profit is not None and profit <= 0),
            }


def _get_last_close_info(order_log_path: Path) -> dict:
    """Returns {instrument: {"direction": original_direction, "logged_at": iso_str,
    "is_loss": bool}} for the most recent close per instrument (agent-initiated
    or broker-side, see _iter_close_events), read from the append-only audit
    log (no separate state file needed). Used by the same-direction cooldown:
    a real, observed pattern of re-shorting an instrument into a strong trend
    immediately after being stopped out on the exact same thesis, repeatedly."""
    last_close = {}
    for event in _iter_close_events(order_log_path):
        last_close[event["instrument"]] = {
            "direction": event["direction"], "logged_at": event["logged_at"], "is_loss": event["is_loss"],
        }
    return last_close


def _get_same_direction_loss_streak(order_log_path: Path) -> dict:
    """Returns {instrument: {"direction", "streak", "logged_at"}} -- streak is
    how many CONSECUTIVE closes in a row (agent-initiated or broker-side, see
    _iter_close_events), in the same direction, were losses (any win resets
    to 0; a loss in a NEW direction resets to 1). Feeds the consecutive-loss
    circuit breaker (see RULES.max_consecutive_same_direction_losses): a real
    incident (2026-09-23/24) showed the single time-based
    same_direction_cooldown isn't enough on its own -- it survives a quick
    re-test, not a trend that outlasts its 60min window. Counting the streak
    directly, independent of elapsed time, catches "the agent keeps
    re-trying the identical losing bet" regardless of how long it waits
    between attempts."""
    streaks = {}
    for event in _iter_close_events(order_log_path):
        instrument, direction, logged_at, is_loss = (
            event["instrument"], event["direction"], event["logged_at"], event["is_loss"],
        )
        current = streaks.get(instrument)
        if is_loss and current and current["direction"] == direction:
            streaks[instrument] = {"direction": direction, "streak": current["streak"] + 1, "logged_at": logged_at}
        elif is_loss:
            streaks[instrument] = {"direction": direction, "streak": 1, "logged_at": logged_at}
        else:
            streaks[instrument] = {"direction": direction, "streak": 0, "logged_at": logged_at}
    return streaks


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


def _margin_headroom_ok(available: float, balance: float, rules) -> bool:
    return available >= balance * (rules.margin_safety_buffer_pct / 100)


def _estimate_unrealized_pnl(pos: dict):
    """profit = (current_price - entry_level) * size * (+1 for BUY, -1 for SELL),
    using current_bid to value a BUY (closing sells at bid) and current_offer to
    value a SELL (closing buys at offer). Cross-checked against every real IG
    dealConfirm profit figure seen in production (both directions, all 3
    instruments) and matched exactly to the cent every time -- this is not a
    rough estimate, it's the same math IG itself uses. Exposed to the agent so
    "is this a substantial gain" (house style) is answerable from real numbers
    instead of the agent having to mentally infer it from raw price levels."""
    is_long = pos["direction"] == "BUY"
    current_price = pos.get("current_bid") if is_long else pos.get("current_offer")
    entry_level = pos.get("entry_level")
    if current_price is None or entry_level is None:
        return None
    sign = 1 if is_long else -1
    return round((current_price - entry_level) * sign * pos["size"], 2)


def _check_stop_breach_backstop(broker, positions: dict) -> list:
    """Every open position always has a stop attached at open time (mandatory
    per _validate_trade) -- but that stop is a REGULAR (non-guaranteed) IG
    stop, which can suffer slippage in a fast move or price gap, filling worse
    than the stop level rather than exactly at it. Rather than passively trust
    that IG's system-side stop has already handled it by the time we look,
    this runs FIRST each tick and actively checks: has the current live price
    already moved past this position's own recorded stop level? If so, close
    it immediately at market rather than waiting -- don't let a loss run
    further just because we're taking IG's word for it between our ~5-minute
    check-ins. Also force-closes anything that is somehow missing a stop_level
    entirely (should never happen given it's mandatory at open, but a naked
    position is exactly the scenario this exists to prevent). Returns the list
    of instrument keys that were force-closed, so the caller can drop them from
    its in-memory positions dict and refresh account state before continuing."""
    closed_keys = []
    for instrument, pos in list(positions.items()):
        is_long = pos["direction"] == "BUY"
        stop_level = pos.get("stop_level")

        if stop_level is None:
            logger.error(f"[IG-CFD] {instrument} has NO stop_level recorded -- closing immediately as a safety fallback.")
            result = broker.close_position(
                deal_id=pos["deal_id"], direction=pos["direction"], epic=pos["epic"], size=pos["size"],
            )
            _log_order_event({
                "action": "STOP_BREACH_BACKSTOP", "instrument": instrument, "deal_id": pos["deal_id"],
                "reason": "No stop_level recorded on this position -- force-closed as a safety fallback.",
                **result,
            })
            if result.get("status") == "submitted":
                closed_keys.append(instrument)
            continue

        # Closing a LONG means selling at the bid; closing a SHORT means
        # buying at the offer -- use whichever price actually determines what
        # we'd realize right now, same convention as the dashboard's P&L math.
        current_price = pos.get("current_bid") if is_long else pos.get("current_offer")
        if current_price is None:
            continue  # no live price available this tick -- nothing to check against

        breached = (current_price <= stop_level) if is_long else (current_price >= stop_level)
        if not breached:
            continue

        logger.warning(
            f"[IG-CFD] {instrument} stop breached: current price {current_price} vs stop {stop_level} "
            f"({'LONG' if is_long else 'SHORT'}) -- closing immediately rather than waiting."
        )
        result = broker.close_position(
            deal_id=pos["deal_id"], direction=pos["direction"], epic=pos["epic"], size=pos["size"],
        )
        _log_order_event({
            "action": "STOP_BREACH_BACKSTOP", "instrument": instrument, "deal_id": pos["deal_id"],
            "current_price": current_price, "stop_level": stop_level,
            "reason": f"Live price breached stop level (current={current_price}, stop={stop_level}) -- closed immediately.",
            **result,
        })
        if result.get("status") == "submitted":
            closed_keys.append(instrument)

    return closed_keys




MARGIN_PROFIT_TAKE_PCT = 1.0  # Changed 2026-09-24, nudged up from a same-day 0.8% --
# both were tested (with previous constants' history below) via the same yfinance-1m-bar
# entry-anchored replay, now against the freshest 198-trade sample: 0.5%/1.0% edged out
# 0.5%/0.8% (+1.58% vs -1.12% margin summed), and a wider fine grid (stop 0.4-0.8%,
# target 0.6-1.2%) confirmed the same broad conclusion reached every time this has been
# tested: ANY meaningful widening of the STOP past ~0.5-0.6% makes things dramatically
# worse (0.8%/0.8% lost -21%), while the target can drift a little within the tight band
# without much consequence -- 1.0% was the best target found at both the 0.4% and 0.5%
# stop levels in that grid. Treat this as "the tight-target region between roughly
# 0.8-1.0% is all reasonable, and the exact best cell isn't worth chasing further" rather
# than a confident single optimum -- it moved between runs on fresh data already.
# Implemented as a REAL resting IG limit order (see _margin_based_limit_distance), not
# a bot-side poll: this account's tick cadence is 30-60 minutes, and a resting order
# lets IG execute the instant price touches it, 24/7, rather than only whenever the
# bot next happens to check.


MARGIN_STOP_LOSS_PCT = 0.5  # Unchanged 2026-09-24 alongside the MARGIN_PROFIT_TAKE_PCT
# nudge above -- every replay run this session (the original 0.4%/1.0% vs 0.5%/0.8%
# comparison, a 2.82%-stop proposal, ATR-based stops, and this latest fine grid) agrees
# the STOP specifically should stay tight; widening it is what consistently loses. See
# MARGIN_PROFIT_TAKE_PCT's comment for the full replay context. This is the INITIAL stop
# distance at open; see BREAKEVEN_TRIGGER_PCT below for the (currently disabled, see
# BREAKEVEN_RATCHET_ENABLED) ratchet mechanism that once existed to tighten it further
# once a position moved into profit.

# Per-instrument override of MARGIN_STOP_LOSS_PCT/MARGIN_PROFIT_TAKE_PCT above --
# missing entries just use the global default. NATURAL_GAS previously had a
# fixed 1.6%/1.6% override here (added 2026-09-24, after the global 0.5%/0.8%
# was found to silently clamp to IG's ~10pt minimum for NG). That override is
# now SUPERSEDED: NG moved to the trailing-stop scheme below (see
# TRAILING_STOP_INSTRUMENTS), which a fresh 200-trade replay found meaningfully
# better for NG (+10.10 points of margin vs the fixed 1.6%/1.6%, turning NG
# from -9.92% to +0.17% over the sample) -- so this dict is currently empty,
# not deleted, in case a future instrument needs a fixed (non-trailing)
# override again.
INSTRUMENT_STOP_LOSS_PCT_OVERRIDE = {}
INSTRUMENT_PROFIT_TAKE_PCT_OVERRIDE = {}


def _stop_loss_pct_for(instrument: str) -> float:
    return INSTRUMENT_STOP_LOSS_PCT_OVERRIDE.get(instrument, MARGIN_STOP_LOSS_PCT)


def _profit_take_pct_for(instrument: str) -> float:
    return INSTRUMENT_PROFIT_TAKE_PCT_OVERRIDE.get(instrument, MARGIN_PROFIT_TAKE_PCT)


# Continuous trailing-stop scheme -- 2026-09-24, replacing the fixed
# stop/target pair for all 5 instruments. Built from the user's own proposed
# strategy ("-2.85% max stop, wait up to 2h, trail stop to current-profit% -
# 0.25% at every threshold"), landed on CONTINUOUS (no arm gate, updates on
# every new favorable tick) after this session tried and real-trade-tested
# several alternatives along the way:
#
#   - Continuous, no arm, 0.20% gap (THIS, currently active): validated twice --
#     +8.48% (of margin) on an early 200-trade sample, then against the FULL
#     484-trade / 34-day account history (yfinance 1m bars for the last ~8
#     days, 5m bars further back) at the dollar level:
#         ACTUAL (real, mixed strategies over time):    -$2,640.44
#         ALL 5 continuous (this scheme):               +$1,045.74
#     The only variant tested that turned the whole period net POSITIVE.
#   - Continuous, arm-gated at 1% ("arm at 1%, 0.3% gap, 2.82% floor"):
#     REJECTED -- -25.82% vs the then-current -1.12% on 193 trades. A gap
#     between 0% and the arm level left positions fully unprotected there.
#   - Stepped (arm 1.0%, then 1.5%, 2.0%... each locking threshold-0.25%):
#     REJECTED -- -$126.49 over the full history (still a net loss, despite a
#     higher win rate than continuous). Reintroduces the same dead-zone below
#     the arm as the gated design above, just less severely.
#   - Stepped, arm lowered to 0.5% (attempt to shrink the dead zone): made it
#     WORSE, not better -- -$257.42 over the full history. Confirms the
#     problem is the existence of any gap/step discretization at all, not its
#     exact threshold: a lower arm clips winning trades earlier (locking a
#     small gain sooner) more than it rescues losers, on this account's actual
#     price action.
#   - BRENT_OIL specifically ran slightly negative in one earlier (200-trade,
#     no-arm) continuous test (-9.50pp vs its own fixed 0.5%/1.0%), but on the
#     full 484-trade history it came back positive (+$197.98 vs actual
#     -$923.19) -- the earlier result was a smaller-sample artifact, not a
#     durable Brent-specific problem.
# GOLD/SILVER still have only 1 real trade each in the entire history -- not
# enough data to draw a real conclusion for them either way; included here
# because the user asked for uniform treatment across all 5, not because
# they've been separately validated.
TRAILING_STOP_INSTRUMENTS = {"BRENT_OIL", "WTI_OIL", "NATURAL_GAS", "GOLD", "SILVER"}
TRAILING_STOP_FLOOR_PCT = 2.85  # hard worst-case stop, as % of margin -- never breached
TRAILING_STOP_GAP_PCT = 0.20  # stop trails to (peak favorable % - this), from the very first favorable tick,
# with NO arm/threshold gate -- see the module comment above for why a gate of any kind
# (gated-continuous, or stepped) underperformed this on every real-trade test run this session.
TRAILING_STOP_STALE_LOSS_MINUTES = 120  # force-close if never favorable and still negative after this long
TRAILING_STOP_CEILING_BUFFER_PCT = 20.0  # limit_level kept this far beyond the peak favorable level --
# IG's open_position/update_position both require a real limit_level (see ig_broker.py), so this can't
# actually be uncapped; instead the "limit" is recomputed every sync to always sit far past the highest
# profit reached so far, making it a formality that should realistically never fire -- the trailing stop
# above is the real exit mechanism for all 5 instruments.


BREAKEVEN_TRIGGER_PCT = 1.6  # Once a position's unrealized profit reaches this % of margin,
# its stop ratchets up (long) / down (short) to BREAKEVEN_LOCK_PCT below -- so a subsequent
# reversal exits with a small locked-in gain instead of riding all the way back down to the
# original MARGIN_STOP_LOSS_PCT loss. User's own choice, 2026-09-22, addressing exactly the
# "runs to +2%, then reverses into a full loss" scenario a fixed (non-trailing) stop/limit
# pair can't protect against on its own.

BREAKEVEN_LOCK_PCT = 1.4  # The guaranteed minimum profit (as % of margin) the stop ratchets
# to once BREAKEVEN_TRIGGER_PCT is reached -- see _ratchet_stop_target. Raised from 0.2% to
# 1.4% on 2026-09-23 (user's own choice): locks in nearly all of the gain already shown by the
# time the 1.6% trigger fires, rather than a bare sliver, while still sitting strictly below
# the 1.6% trigger level so the ratchet doesn't fire exactly at the current price.

BREAKEVEN_RATCHET_ENABLED = False  # Disabled 2026-09-23 after tr.csv showed sustained losses
# despite a 55-59% win rate -- a replay of 193 real live trades against real 1-minute price
# data (yfinance, per-trade entry-anchored scale calibration, same methodology as the
# MARGIN_PROFIT_TAKE_PCT backtest) showed the ratchet ITSELF is the problem, regardless of
# where the trigger is set: with it enabled (trigger 1.6/lock 1.4), only 2 of 193 replayed
# trades ever actually reached the 3% target -- 113 were clipped by the ratchet lock at
# ~1.4% instead. Every alternative trigger/lock pairing tested (2.2/1.8, 2.5/2.0, 2.8/2.3,
# 2.9/2.7, and trigger==lock) was ALSO worse than no ratchet at all -- this account's real
# intraday price action wobbles through almost any given profit threshold on the way to
# either the real target or the real stop, so ANY ratchet mostly just converts would-be
# winners into small premature ones without meaningfully reducing how often the stop is
# eventually hit (100-101 stop-outs in every config tested, ratcheted or not). Replaying the
# same 193 trades with the ratchet off roughly HALVED the net loss (-35% vs -68% of margin,
# summed) versus every ratcheted configuration. This reintroduces the original risk the
# ratchet was built for (a position swinging into profit then fully reversing to the stop
# with nothing banked) -- the data says that risk is real but smaller than the ratchet's own
# cost. _ratchet_stop_target still exists in full below (constants included) rather than
# being deleted, so this can be re-enabled and re-tuned if the account's price behavior
# changes and a future replay against fresh data supports it.


def _margin_based_distance(pct: float, margin_allocated: float, size: float, min_distance: float = None) -> float:
    """Shared math for both the profit and loss margin-based distances: converts a target
    % of margin into a point DISTANCE from entry (not an absolute level) -- the target $
    P&L (margin_allocated * pct/100) divided by size, using the exact same
    (current_price - entry_level) * size math _estimate_unrealized_pnl already verifies
    against real IG fills. A pure distance, independent of entry/fill price, so it can be
    passed straight into open_position's stop_distance/limit_distance -- IG computes the
    absolute level itself from the live fill price (see open_position's docstring: this
    avoids pre-computing off a snapshot price that may have moved by fill time).

    min_distance: IG's own minNormalStopOrLimitDistance for this instrument (see
    ig_broker.get_market_snapshot), clamped up to if the % computation comes out
    tighter -- added 2026-09-24 when MARGIN_STOP_LOSS_PCT dropped to 0.4%: at that
    tightness, Natural Gas's computed stop distance (~2.5pts at typical levels) falls
    well below IG's real ~10pt minimum for it, which would otherwise get every NG
    order rejected outright. Silently widening to the real minimum, rather than
    submitting a distance IG will reject, is the same fail-safe philosophy used
    throughout this file (e.g. MARGIN_LIMIT_SYNC_TOLERANCE) -- IG's own risk floor
    always wins over a requested distance that's tighter than it allows."""
    distance = round(margin_allocated * (pct / 100) / size, 4)
    if min_distance and distance < min_distance:
        return round(min_distance, 4)
    return distance


def _margin_based_limit_distance(margin_allocated: float, size: float, min_distance: float = None,
                                  instrument: str = None) -> float:
    """Take-profit side -- see _margin_based_distance and MARGIN_PROFIT_TAKE_PCT.
    instrument: looks up INSTRUMENT_PROFIT_TAKE_PCT_OVERRIDE (e.g. NATURAL_GAS),
    falling back to the global MARGIN_PROFIT_TAKE_PCT when None/not overridden."""
    return _margin_based_distance(_profit_take_pct_for(instrument), margin_allocated, size, min_distance)


def _margin_based_stop_distance(margin_allocated: float, size: float, min_distance: float = None,
                                 instrument: str = None) -> float:
    """Stop-loss side -- see _margin_based_distance and MARGIN_STOP_LOSS_PCT.
    instrument: looks up INSTRUMENT_STOP_LOSS_PCT_OVERRIDE (e.g. NATURAL_GAS),
    falling back to the global MARGIN_STOP_LOSS_PCT when None/not overridden."""
    return _margin_based_distance(_stop_loss_pct_for(instrument), margin_allocated, size, min_distance)


def _initial_stop_and_limit_distance(margin_allocated: float, size: float, min_distance: float = None,
                                      instrument: str = None) -> tuple:
    """Used only at OPEN time. For TRAILING_STOP_INSTRUMENTS, the initial resting
    stop is the wide TRAILING_STOP_FLOOR_PCT (the worst case the trailing scheme
    ever allows) and the initial limit is a generous, effectively-out-of-the-way
    ceiling (TRAILING_STOP_CEILING_BUFFER_PCT past zero profit) -- both get
    reconciled to the real trailing levels on the very next sync once the
    position shows a live P&L (see _trailing_stop_and_limit). Every other
    instrument keeps the plain fixed pair from _margin_based_stop_distance/
    _margin_based_limit_distance."""
    if instrument in TRAILING_STOP_INSTRUMENTS:
        stop_distance = _margin_based_distance(TRAILING_STOP_FLOOR_PCT, margin_allocated, size, min_distance)
        limit_distance = _margin_based_distance(TRAILING_STOP_CEILING_BUFFER_PCT, margin_allocated, size, min_distance)
        return stop_distance, limit_distance
    return (
        _margin_based_stop_distance(margin_allocated, size, min_distance, instrument),
        _margin_based_limit_distance(margin_allocated, size, min_distance, instrument),
    )


def _trailing_peaks_path() -> Path:
    return DATA_DIR / "trailing_peaks.json"


def _load_trailing_peaks() -> dict:
    path = _trailing_peaks_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_trailing_peaks(peaks: dict) -> None:
    _trailing_peaks_path().write_text(json.dumps(peaks))


def _trailing_stale_loss_due(pos: dict, peak_state: dict) -> bool:
    """True if this position has NEVER shown a favorable excursion (peak stayed
    at/below 0) and has been open at least TRAILING_STOP_STALE_LOSS_MINUTES and
    is still at a loss right now -- matches the user's own proposed "wait up to
    2 hours" rule: a trade that never even ticks into profit within that window
    is force-closed rather than left to ride all the way down to the -2.85%
    floor. A trade that DID go favorable at some point, even briefly, is left
    alone here -- the trailing stop already has a real, tighter lock in place
    for it (see _trailing_stop_and_limit)."""
    if peak_state.get(pos.get("deal_id"), 0.0) > 0:
        return False
    opened_at = pos.get("opened_at")
    if not opened_at:
        return False
    try:
        open_time = datetime.fromisoformat(opened_at)
        if open_time.tzinfo is None:
            open_time = open_time.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    minutes_open = (datetime.now(timezone.utc) - open_time).total_seconds() / 60
    if minutes_open < TRAILING_STOP_STALE_LOSS_MINUTES:
        return False
    pnl = _estimate_unrealized_pnl(pos)
    return pnl is not None and pnl <= 0


def _trailing_stop_and_limit(pos: dict, margin: float, entry_level: float, size: float, is_long: bool,
                              min_stop_distance: float, peak_state: dict) -> tuple:
    """Core of the continuous trailing-stop scheme (see TRAILING_STOP_INSTRUMENTS):
    tracks each position's peak favorable excursion (as % of margin) across
    ticks in peak_state (mutated in place, keyed by deal_id -- persisted to
    disk by the caller), then returns:
      - stop_price: stays at the plain -TRAILING_STOP_FLOOR_PCT floor until the
        position has shown a REAL favorable excursion (peak_fav_pct > 0) at
        least once; only then does it start trailing to
        (peak_fav_pct - TRAILING_STOP_GAP_PCT) of margin, active from that
        very first favorable tick with NO arm-threshold gate -- see the module
        comment above TRAILING_STOP_INSTRUMENTS for why every gated/stepped
        alternative tested underperformed this on real trade data.
        *** peak_fav_pct must NOT be floored at 0 before this comparison --
        doing so made every position ratchet from the real -2.85% floor to a
        bare -TRAILING_STOP_GAP_PCT (e.g. -0.20%) on its very first sync, even
        with zero favorable movement, silently replacing the validated
        strategy with an untested hair-trigger one. Caught 2026-09-24 by a
        user question asking how the floor is actually implemented. ***
      - limit_price: entry adjusted by (max(peak_fav_pct, 0) + TRAILING_STOP_CEILING_BUFFER_PCT)
        of margin -- always recomputed past the current peak so it stays out
        of the way; the trailing stop is the real exit, this only exists
        because IG requires a real limit_level on every open/update call.
    _sync_margin_based_exits still applies its own one-way ratchet on top of
    the returned stop_price, so a transient dip in peak_state (shouldn't
    happen since peak_state only ever grows) can never loosen a live stop."""
    deal_id = pos.get("deal_id")
    pnl = _estimate_unrealized_pnl(pos)
    current_fav_pct = (pnl / margin * 100) if pnl is not None else 0.0
    peak_fav_pct = max(peak_state.get(deal_id, 0.0), current_fav_pct)
    peak_state[deal_id] = peak_fav_pct

    if peak_fav_pct > 0:
        lock_pct = max(-TRAILING_STOP_FLOOR_PCT, peak_fav_pct - TRAILING_STOP_GAP_PCT)
    else:
        lock_pct = -TRAILING_STOP_FLOOR_PCT
    lock_distance = margin * lock_pct / 100 / size
    if min_stop_distance and abs(lock_distance) < min_stop_distance:
        lock_distance = min_stop_distance if lock_distance >= 0 else -min_stop_distance
    stop_price = entry_level + lock_distance if is_long else entry_level - lock_distance

    ceiling_pct = peak_fav_pct + TRAILING_STOP_CEILING_BUFFER_PCT
    ceiling_distance = margin * ceiling_pct / 100 / size
    limit_price = entry_level + ceiling_distance if is_long else entry_level - ceiling_distance

    return stop_price, limit_price


def _ratchet_stop_target(pos: dict, margin: float, entry_level: float, size: float, is_long: bool,
                          min_stop_distance: float = None, instrument: str = None) -> float:
    """Computes this tick's CANDIDATE stop level: the original MARGIN_STOP_LOSS_PCT
    target, unless unrealized profit has reached BREAKEVEN_TRIGGER_PCT of margin, in
    which case the candidate becomes the tighter BREAKEVEN_LOCK_PCT profit-lock level
    instead. This is only a candidate -- _sync_margin_based_exits is what turns it into
    a one-way ratchet, by only ever moving the LIVE stop in the more-protective
    direction. That's also what makes the lock permanent with no state to track
    anywhere: once the live stop has been moved to the lock level, a later dip back
    below the trigger just produces a worse candidate (the original stop distance),
    which the caller correctly ignores rather than loosening the stop back up."""
    stop_distance = _margin_based_stop_distance(margin, size, min_stop_distance, instrument)
    plain_stop = entry_level - stop_distance if is_long else entry_level + stop_distance
    if not BREAKEVEN_RATCHET_ENABLED:
        return plain_stop

    pnl = _estimate_unrealized_pnl(pos)
    trigger_profit = margin * (BREAKEVEN_TRIGGER_PCT / 100)
    if pnl is not None and pnl >= trigger_profit:
        lock_distance = _margin_based_distance(BREAKEVEN_LOCK_PCT, margin, size)
        return entry_level + lock_distance if is_long else entry_level - lock_distance
    return plain_stop


def _margin_allocated_for_position(pos: dict, snapshot: Optional[dict], max_leverage_multiple: float) -> Optional[float]:
    """Recomputes the margin committed to an OPEN position directly from its live
    size/entry_level plus a fresh market snapshot's lot_size/margin_factor --
    the exact inverse of _compute_position_size's own math (notional =
    margin_allocated * effective_leverage, so margin_allocated = notional /
    effective_leverage), giving the identical number that was originally
    computed at open time, without needing to read it back from anywhere.

    This deliberately does NOT read the audit log (an earlier version did, via
    a since-removed _get_margin_allocated_by_deal) -- a real 2026-09-18 incident
    involved a workflow run whose audit-log commit failed to push, silently
    losing that run's OPEN_LONG/WTI_MIRROR_OPEN entries. Recomputing live means
    _sync_margin_based_exits can still correct a position's stop/limit even when
    its own opening record never made it into the log."""
    if not snapshot:
        return None
    entry_level = pos.get("entry_level")
    size = pos.get("size")
    if entry_level is None or not size:
        return None
    lot_size = snapshot.get("lot_size") or 1
    margin_factor = snapshot.get("margin_factor")
    ig_implied_leverage = 100 / margin_factor if margin_factor else max_leverage_multiple
    effective_leverage = min(ig_implied_leverage, max_leverage_multiple)
    notional = size * entry_level * lot_size
    return notional / effective_leverage


MARGIN_LIMIT_SYNC_TOLERANCE = 0.02  # points -- skip amending a position whose live
# limit_level is already within this of the target, so a tick doesn't keep firing a
# no-op update_position call every 30 minutes once a position is already correct
# (float rounding between our calc and IG's own stored level is expected). Lowered
# from 0.5 on 2026-09-24 when MARGIN_STOP_LOSS_PCT dropped to 0.4%: at that tightness,
# a real, intended stop/limit correction can itself be well under a point (e.g. a
# ratchet lock movement of a few tenths of a point) -- the old 0.5pt tolerance would
# have silently swallowed exactly that kind of legitimate small correction, not just
# genuine float noise. 0.02 still comfortably absorbs rounding (levels are rounded to
# 4 decimal places) without masking any real movement at the current tight distances.


def _sync_margin_based_exits(broker, positions: dict, snapshots: dict, max_leverage_multiple: float) -> list:
    """Ensures every open position's live stop_level AND limit_level on IG actually
    match their targets, amending them (never closing) if either doesn't:
      - limit_level: MARGIN_PROFIT_TAKE_PCT of margin, always.
      - stop_level: MARGIN_STOP_LOSS_PCT of margin initially, but RATCHETS to a
        guaranteed BREAKEVEN_LOCK_PCT profit once unrealized profit has reached
        BREAKEVEN_TRIGGER_PCT -- see _ratchet_stop_target. The ratchet is one-way:
        the live stop is only ever moved in the more-protective direction versus
        its current value, never loosened back, which is also what makes the lock
        permanent without needing to track "has this already triggered" anywhere.
    Positions opened going forward already get the initial stop/limit correct at
    open time (see the OPEN_LONG/OPEN_SHORT call sites) -- this
    function is what applies the ratchet as profit develops, and also reconciles
    a position that predates the current % values, or one whose live margin/
    snapshot can't be determined for some reason (left fully untouched: we'd
    rather leave an old stop/limit in place than guess).

    margin_allocated is recomputed live via _margin_allocated_for_position rather
    than read from the audit log -- see that function's docstring for why.

    *** INCIDENT (2026-09-18): an earlier version of this function called
    broker.update_position() with only limit_level set, leaving stop_level as its
    default None -- IG's update endpoint does NOT preserve an omitted field on an
    existing position the way the underlying trading_ig library's request-building
    code suggests; it actually DELETED the stop entirely, and the next tick's
    _check_stop_breach_backstop then correctly force-closed the affected positions
    as "no stop_level recorded", realizing a real ~$614 loss. This version doesn't
    just avoid that mistake -- it removes the whole class of it: both stop_level
    and limit_level are ALWAYS freshly computed from the current margin/size/entry
    every time, and whenever either needs to change, BOTH are sent explicitly in
    the same call. There is no more "value we're not touching" to accidentally
    omit -- there's nothing left to preserve, only two targets to (re)assert. ***

    TRAILING_STOP_INSTRUMENTS (all 5 as of 2026-09-24) instead follow the
    continuous trailing-stop scheme -- see _trailing_stop_and_limit and its
    module-level comment -- including a stale-loss force-close via
    _trailing_stale_loss_due if the position never went favorable within
    TRAILING_STOP_STALE_LOSS_MINUTES. Peak-favorable state for that scheme is
    tracked in data/trailing_peaks.json (loaded/pruned/saved once per call).

    Returns the list of instrument keys amended this tick, purely for
    logging/visibility -- callers don't need to treat this any differently."""
    synced = []
    peak_state = _load_trailing_peaks()
    live_deal_ids = {pos["deal_id"] for pos in positions.values() if pos.get("deal_id")}
    peak_state = {k: v for k, v in peak_state.items() if k in live_deal_ids}

    for instrument, pos in positions.items():
        snapshot = snapshots.get(instrument)
        margin = _margin_allocated_for_position(pos, snapshot, max_leverage_multiple)
        entry_level = pos.get("entry_level")
        current_limit = pos.get("limit_level")
        current_stop = pos.get("stop_level")
        size = pos.get("size")
        if not margin or entry_level is None or not size:
            continue

        min_stop_distance = (snapshot or {}).get("min_stop_distance")
        is_long = pos["direction"] == "BUY"
        trailing = instrument in TRAILING_STOP_INSTRUMENTS

        if trailing and _trailing_stale_loss_due(pos, peak_state):
            result = broker.close_position(
                deal_id=pos["deal_id"], direction=pos["direction"], epic=pos["epic"], size=size,
            )
            _log_order_event({
                "action": "TRAILING_STALE_LOSS_CLOSE", "instrument": instrument, "deal_id": pos["deal_id"],
                "reason": (
                    f"Never showed a favorable excursion within {TRAILING_STOP_STALE_LOSS_MINUTES} min of "
                    f"opening and remains at a loss -- force-closed per the trailing-stop strategy's "
                    f"timeout rule rather than left to ride toward the -{TRAILING_STOP_FLOOR_PCT}% floor."
                ),
                **result,
            })
            if result.get("status") == "submitted":
                synced.append(instrument)
            continue

        if trailing:
            candidate_stop, target_limit = _trailing_stop_and_limit(
                pos, margin, entry_level, size, is_long, min_stop_distance, peak_state,
            )
            target_limit = round(target_limit, 4)
        else:
            limit_distance = _margin_based_limit_distance(margin, size, min_stop_distance, instrument)
            target_limit = round(entry_level + limit_distance if is_long else entry_level - limit_distance, 4)
            candidate_stop = _ratchet_stop_target(pos, margin, entry_level, size, is_long, min_stop_distance, instrument)

        # *** BUG FOUND 2026-09-24 (real live incident, NATURAL_GAS): every candidate_stop
        # above is computed as a distance from ENTRY, and min_stop_distance only clamped
        # that entry-relative distance -- but IG's update_position validates the requested
        # stop_level against the CURRENT market price, not entry. As a trailing lock tightens
        # toward a profitable price, the entry-anchored level can end up closer to the live
        # price than IG's real minNormalStopOrLimitDistance allows, and IG rejects the amend
        # with ATTACHED_ORDER_LEVEL_ERROR -- silently, repeatedly (38 occurrences already,
        # across multiple positions), leaving the stop stuck at its last successfully-applied
        # (wider, less protective) level indefinitely while the position sits in real profit.
        # Fix: clamp candidate_stop to also respect min_stop_distance from the CURRENT price
        # (current_bid for a long -- the price it actually closes at; current_offer for a
        # short) before the one-way ratchet below, so what gets submitted is always something
        # IG can actually accept right now. ***
        if min_stop_distance:
            current_price_for_stop = pos.get("current_bid") if is_long else pos.get("current_offer")
            if current_price_for_stop is not None:
                if is_long:
                    max_allowed_stop = current_price_for_stop - min_stop_distance
                    if candidate_stop > max_allowed_stop:
                        candidate_stop = max_allowed_stop
                else:
                    min_allowed_stop = current_price_for_stop + min_stop_distance
                    if candidate_stop < min_allowed_stop:
                        candidate_stop = min_allowed_stop

        # One-way ratchet: only ever move the stop in the more-protective direction
        # (higher for a long, lower for a short) than its current live value --
        # this is what makes the breakeven-lock (and the trailing-stop lock)
        # permanent once triggered, with no separate "has this already
        # ratcheted" state to track anywhere.
        if current_stop is None:
            target_stop = round(candidate_stop, 4)
        elif is_long:
            target_stop = round(max(current_stop, candidate_stop), 4)
        else:
            target_stop = round(min(current_stop, candidate_stop), 4)

        limit_ok = current_limit is not None and abs(current_limit - target_limit) <= MARGIN_LIMIT_SYNC_TOLERANCE
        stop_ok = current_stop is not None and abs(current_stop - target_stop) <= MARGIN_LIMIT_SYNC_TOLERANCE
        if limit_ok and stop_ok:
            continue  # both already correct -- nothing to do

        result = broker.update_position(
            deal_id=pos["deal_id"], limit_level=target_limit, stop_level=target_stop,
        )
        _log_order_event({
            "action": "MARGIN_EXIT_SYNC", "instrument": instrument, "deal_id": pos["deal_id"],
            "old_limit_level": current_limit, "new_limit_level": target_limit,
            "old_stop_level": current_stop, "new_stop_level": target_stop,
            "reason": (
                f"Trailing-stop reconciliation (floor {TRAILING_STOP_FLOOR_PCT}%, gap {TRAILING_STOP_GAP_PCT}%, "
                f"continuous from the first favorable tick, no arm gate)"
                if trailing else (
                    f"Position's resting stop and/or limit didn't match the "
                    f"{_stop_loss_pct_for(instrument)}%/{_profit_take_pct_for(instrument)}%-of-margin targets "
                    f"(instrument-specific override if one exists, else the global default), or the "
                    f"breakeven ratchet (trigger {BREAKEVEN_TRIGGER_PCT}%, lock {BREAKEVEN_LOCK_PCT}%) "
                    f"applied"
                )
            ) + " -- amending both on IG directly (both always sent together, see incident note above).",
            **result,
        })
        if result.get("status") == "submitted":
            synced.append(instrument)

    if any(inst in TRAILING_STOP_INSTRUMENTS for inst in positions):
        _save_trailing_peaks(peak_state)

    return synced


def _validate_trade(trade: dict, account: dict, positions: dict, rules,
                     running_available: float = None, checked_multiple_sources: bool = True,
                     confluence_reason: str = "", last_close_info: dict = None,
                     loss_streak_info: dict = None) -> tuple:
    """running_available: the margin-safety check's source of truth for
    'available margin right now'. Pass this explicitly (rather than reading
    account['available'] directly) so the caller can track it as a running
    total across MULTIPLE trades processed in the same tick -- otherwise every
    proposed open in a tick gets checked against the same stale pre-tick
    snapshot, and several individually-fine-looking opens could collectively
    breach the safety buffer. Defaults to account['available'] if omitted.

    checked_multiple_sources: result of agent_runner.check_confluence for
    THIS specific trade's instrument+direction -- True iff a real independent
    signal (seasonality/term-structure/positioning/weather/inventory) called
    this tick for this instrument actually agrees with the proposed
    direction, not just "was some other tool called". confluence_reason
    carries check_confluence's specific explanation for the rejection
    message. Only gates OPENs -- closing a position never needs new research.

    last_close_info: {instrument: {direction, logged_at, is_loss}} from
    _get_last_close_info -- blocks re-opening the SAME direction on an
    instrument within RULES.same_direction_cooldown_minutes of a LOSING close
    there (a real, observed pattern: re-shorting into a strong uptrend
    immediately after each stop-out, 3 times in ~90 minutes).

    loss_streak_info: {instrument: {direction, streak, logged_at}} from
    _get_same_direction_loss_streak -- once an instrument has lost
    RULES.max_consecutive_same_direction_losses times in a row in the same
    direction, blocks that direction for RULES.consecutive_loss_cooldown_minutes
    regardless of how much time has passed (unlike same_direction_cooldown_minutes
    above, which only looks at the single most recent close)."""
    if running_available is None:
        running_available = account["available"]

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
        if rules.require_confluence and not checked_multiple_sources:
            return False, confluence_reason or (
                "Confluence requirement not met: no independent signal agreeing with this "
                "direction was checked this tick -- opening on a technical signal alone is "
                "blocked (see RULES.require_confluence)"
            )
        if last_close_info and instrument in last_close_info:
            lc = last_close_info[instrument]
            proposed_direction = "BUY" if action == "OPEN_LONG" else "SELL"
            if lc["is_loss"] and proposed_direction == lc["direction"]:
                minutes_since = (datetime.now(timezone.utc) - datetime.fromisoformat(lc["logged_at"])).total_seconds() / 60
                if minutes_since < rules.same_direction_cooldown_minutes:
                    return False, (
                        f"Same-direction cooldown: {instrument} was closed at a loss "
                        f"{minutes_since:.0f} min ago in this same direction ({proposed_direction}) -- "
                        f"blocked for {rules.same_direction_cooldown_minutes} min to avoid immediately "
                        f"re-entering a thesis that just failed"
                    )
        if loss_streak_info and instrument in loss_streak_info:
            streak_info = loss_streak_info[instrument]
            proposed_direction = "BUY" if action == "OPEN_LONG" else "SELL"
            if streak_info["direction"] == proposed_direction and streak_info["streak"] >= rules.max_consecutive_same_direction_losses:
                minutes_since = (datetime.now(timezone.utc) - datetime.fromisoformat(streak_info["logged_at"])).total_seconds() / 60
                if minutes_since < rules.consecutive_loss_cooldown_minutes:
                    return False, (
                        f"Consecutive-loss circuit breaker: {instrument} has lost "
                        f"{streak_info['streak']} times in a row going {proposed_direction} (most recently "
                        f"{minutes_since:.0f} min ago) -- blocked for {rules.consecutive_loss_cooldown_minutes} "
                        f"min regardless of the shorter same-direction cooldown, to avoid repeating a thesis "
                        f"that keeps failing against what may be a persistent trend"
                    )
        if not _margin_headroom_ok(running_available, account["balance"], rules):
            return False, (
                f"Margin safety buffer breached: available ${running_available:.2f} is below "
                f"{rules.margin_safety_buffer_pct}% of balance ${account['balance']:.2f} -- "
                f"all new opens blocked account-wide until margin recovers"
            )
    else:  # CLOSE
        if not existing:
            return False, f"No open position on {instrument} to close"
        opened_at = existing.get("opened_at")
        if opened_at:
            try:
                open_time = datetime.fromisoformat(opened_at)
                if open_time.tzinfo is None:
                    open_time = open_time.replace(tzinfo=timezone.utc)
                minutes_held = (datetime.now(timezone.utc) - open_time).total_seconds() / 60
                if minutes_held < rules.min_hold_minutes_before_discretionary_close:
                    return False, (
                        f"Minimum hold time not met: {instrument} has only been open for "
                        f"{minutes_held:.0f} min (minimum {rules.min_hold_minutes_before_discretionary_close} "
                        f"min before a discretionary close) -- its real stop-loss/take-profit, and the "
                        f"stop-breach backstop, still protect it independently of this rule"
                    )
            except (ValueError, TypeError):
                pass  # can't parse the timestamp -- fail permissive, allow the close

    return True, "OK"


def _validate_spread_trade(trade: dict, account: dict, positions: dict, rules,
                            running_available: float = None, checked_multiple_sources: bool = True,
                            last_close_info: dict = None) -> tuple:
    """Validates OPEN_SPREAD (long one of Brent/WTI, short the other) --
    reuses the exact same rules as a single-instrument open (confluence,
    per-leg same-direction cooldown, allocation bounds, mandatory stop/limit,
    margin safety) but checks BOTH legs, since a spread is really just two
    ordinary IG positions we treat as a linked pair at our own bookkeeping
    layer -- IG itself has no concept of "spread" here."""
    if running_available is None:
        running_available = account["available"]

    long_instrument = (trade.get("long_instrument") or "").upper()
    short_instrument = (trade.get("short_instrument") or "").upper()

    valid_pair = {"BRENT_OIL", "WTI_OIL"}
    if {long_instrument, short_instrument} != valid_pair:
        return False, (
            f"OPEN_SPREAD requires long_instrument and short_instrument to be BRENT_OIL and WTI_OIL "
            f"(in either order) -- got long={long_instrument!r} short={short_instrument!r}"
        )

    if positions.get(long_instrument):
        return False, f"{long_instrument} already has an open position -- both spread legs must be free"
    if positions.get(short_instrument):
        return False, f"{short_instrument} already has an open position -- both spread legs must be free"

    if len(positions) + 2 > rules.max_positions:
        return False, f"Max positions ({rules.max_positions}) would be exceeded by opening both spread legs"

    allocation_pct = trade.get("allocation_pct", 0)
    if allocation_pct < rules.min_allocation_pct or allocation_pct > rules.max_allocation_pct:
        return False, f"Allocation {allocation_pct}% outside [{rules.min_allocation_pct}, {rules.max_allocation_pct}]%"
    if not trade.get("stop_loss_pct"):
        return False, "stop_loss_pct is mandatory"
    if not trade.get("take_profit_pct"):
        return False, "take_profit_pct is mandatory"
    if rules.require_confluence and not checked_multiple_sources:
        return False, (
            "Confluence requirement not met: no news/macro/seasonality/term-structure/inventory data "
            "was checked this tick -- opening on a technical signal alone is blocked"
        )

    for instrument, direction in ((long_instrument, "BUY"), (short_instrument, "SELL")):
        if last_close_info and instrument in last_close_info:
            lc = last_close_info[instrument]
            if lc["is_loss"] and direction == lc["direction"]:
                minutes_since = (datetime.now(timezone.utc) - datetime.fromisoformat(lc["logged_at"])).total_seconds() / 60
                if minutes_since < rules.same_direction_cooldown_minutes:
                    return False, (
                        f"Same-direction cooldown: {instrument} was closed at a loss "
                        f"{minutes_since:.0f} min ago in this same direction ({direction}) -- "
                        f"blocked for {rules.same_direction_cooldown_minutes} min"
                    )

    if not _margin_headroom_ok(running_available, account["balance"], rules):
        return False, (
            f"Margin safety buffer breached: available ${running_available:.2f} is below "
            f"{rules.margin_safety_buffer_pct}% of balance ${account['balance']:.2f} -- "
            f"all new opens blocked account-wide until margin recovers"
        )

    return True, "OK"


def _last_known_positions_by_deal_id() -> dict:
    """Returns {deal_id: position_dict} as of the most recent equity_history.jsonl
    snapshot (written at the end of every real run_cfd_tick pass) -- the cheapest
    available 'last known state' to diff a fresh get_positions() call against in
    run_watch_check, with no need for its own separate state file. Each
    position_dict includes "instrument", "direction" and the last-known
    "unrealized_pnl_usd" -- everything needed to log a reasonable win/loss
    classification for a position that vanishes (see _log_broker_closes)."""
    path = DATA_DIR / "equity_history.jsonl"
    if not path.exists():
        return {}
    last_line = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                last_line = line
    if not last_line:
        return {}
    try:
        snapshot = json.loads(last_line)
    except json.JSONDecodeError:
        return {}
    return {p["deal_id"]: p for p in snapshot.get("positions", []) if p.get("deal_id")}


def _log_broker_closes(vanished_deal_ids: set, last_known_positions: dict) -> None:
    """Logs a BROKER_CLOSE event for each position that vanished between
    snapshots -- almost always IG's own resting stop or take-profit firing,
    which (unlike an agent-initiated CLOSE) previously produced NO audit-log
    entry at all. A real incident (2026-09-23/24: the agent re-shorted
    Natural Gas 8 times in ~13 hours into a persistent rally) traced back to
    exactly this gap -- same_direction_cooldown_minutes and the consecutive-
    loss circuit breaker both only ever looked at "CLOSE" actions, so a
    string of ordinary stop-outs (the dominant way positions actually close
    on this account) was invisible to both cooldowns the whole time.
    win/loss is estimated from the LAST KNOWN unrealized_pnl_usd (as of the
    prior ~2-minute watch snapshot) since we don't have the exact realized
    fill after the fact without an extra IG activity-history call -- close
    enough to classify the SIGN correctly, which is all the cooldowns need."""
    for deal_id in vanished_deal_ids:
        pos = last_known_positions.get(deal_id)
        if not pos:
            continue
        estimated_pnl = pos.get("unrealized_pnl_usd")
        _log_order_event({
            "action": "BROKER_CLOSE", "instrument": pos.get("instrument"), "deal_id": deal_id,
            "original_direction": pos.get("direction"),
            "estimated_pnl": estimated_pnl,
            "status": "detected",
            "reason": (
                "Position vanished between watch snapshots -- its own resting stop or "
                "take-profit almost certainly fired. estimated_pnl is the last known "
                "unrealized_pnl_usd (from the prior watch cycle), not an exact realized "
                "fill, but reliable enough to classify win/loss for the cooldown rules."
            ),
        })


def run_watch_check():
    """Cheap, frequent check (see gh-cron-pinger's 2-min "watch" Cron Trigger,
    distinct from the normal 30-min "tick" one). Two things happen every
    cycle, both far cheaper than a full tick (no OpenAI calls, no trade
    validation/execution):
      1. _sync_margin_based_exits runs on whatever's currently open -- this is
         what makes the breakeven-lock ratchet (see BREAKEVEN_TRIGGER_PCT)
         actually responsive to a fast intra-tick price move, rather than only
         ever being checked once every 30 minutes. Observed median trade
         duration on this account is ~15 min, comfortably short enough that a
         position could round-trip from +2% back to a full loss entirely
         between two scheduled ticks with nothing in between to catch it.
      2. Did any position open as of the last recorded snapshot silently close
         since then -- most likely its resting IG take-profit/stop firing? If
         so, escalate immediately into a full run_cfd_tick() pass rather than
         waiting up to 30 min for the next one. The full tick's own exit-sync
         makes step 1 redundant in that case, so it's skipped here to avoid a
         wasted extra IG call.

    Writes NOTHING to disk when nothing needs to change -- the calling
    workflow's git-auto-commit-action step only commits if there's an actual
    diff under data/, so a quiet watch cycle (no ratchet fired, nothing
    closed) still produces zero commit noise despite running 15x more often
    than the full tick. Uses the same two kill-switches as run_cfd_tick (a
    disabled account makes no IG calls here either)."""
    enabled = os.getenv("IG_LIVE_TRADING_ENABLED", "").lower() == "true"
    if not enabled:
        logger.info("[IG-CFD] [watch] IG_LIVE_TRADING_ENABLED is not 'true'. Doing nothing.")
        return

    last_known_positions = _last_known_positions_by_deal_id()
    last_known_deal_ids = set(last_known_positions.keys())
    if not last_known_deal_ids:
        logger.info("[IG-CFD] [watch] No open positions as of the last snapshot (or no snapshot yet) -- nothing to check.")
        return

    from config import RULES, INSTRUMENTS
    from ig_broker import IGBroker

    live = os.getenv("IG_LIVE", "").lower() == "true"
    username = os.getenv("IG_USERNAME")
    password = os.getenv("IG_PASSWORD")
    api_key = os.getenv("IG_API_KEY")
    if not (username and password and api_key):
        logger.error("[IG-CFD] [watch] IG_USERNAME / IG_PASSWORD / IG_API_KEY not fully set. Aborting.")
        return

    try:
        broker = IGBroker(username, password, api_key, live=live)
    except Exception as e:
        logger.error(f"[IG-CFD] [watch] Could not create IG session: {e}")
        return

    epic_to_key = {inst.epic: key for key, inst in INSTRUMENTS.items() if inst.epic}
    if not epic_to_key:
        return
    positions = broker.get_positions(epic_to_key)
    current_deal_ids = {pos["deal_id"] for pos in positions.values()}

    vanished = last_known_deal_ids - current_deal_ids
    if vanished:
        logger.warning(
            f"[IG-CFD] [watch] {len(vanished)} position(s) closed since the last snapshot "
            f"(deal_ids: {sorted(vanished)}) -- likely the resting take-profit or stop firing. "
            f"Escalating to a full tick immediately instead of waiting for the next scheduled run."
        )
        _log_broker_closes(vanished, last_known_positions)
        run_cfd_tick()
        return

    if not positions:
        logger.info("[IG-CFD] [watch] No change since the last snapshot -- nothing to do.")
        return

    # Nothing closed -- still run the cheap margin-based exit sync so the
    # breakeven-lock ratchet (and any other stop/limit correction) is checked
    # on this same 2-min cadence, not just once every 30 min.
    snapshots = {}
    for instrument in positions:
        inst = INSTRUMENTS.get(instrument)
        if inst and inst.epic:
            snapshots[instrument] = broker.get_market_snapshot(inst.epic)
    synced = _sync_margin_based_exits(broker, positions, snapshots, RULES.max_leverage_multiple)
    if synced:
        logger.warning(f"[IG-CFD] [watch] Margin-based exit sync updated: {synced}")
    else:
        logger.info("[IG-CFD] [watch] No change since the last snapshot -- nothing to do.")


def run_cfd_tick():
    enabled = os.getenv("IG_LIVE_TRADING_ENABLED", "").lower() == "true"
    if not enabled:
        logger.info("[IG-CFD] IG_LIVE_TRADING_ENABLED is not 'true'. Doing nothing.")
        return

    from config import RULES, INSTRUMENTS, PLAYBOOKS_DIR
    from ig_broker import IGBroker
    from agent_runner import get_agent_trades, AgentCallFailed, check_confluence
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

    # Rule: don't wait for a loss to deepen. Runs before the agent's turn so it
    # always sees already-resolved, accurate position state -- same timing
    # pattern as the Alpaca bot's profit-lock backstop.
    closed_keys = _check_stop_breach_backstop(broker, positions)
    if closed_keys:
        for key in closed_keys:
            positions.pop(key, None)
        account = broker.get_account_state()  # margin/balance changed by the closes above
        logger.warning(
            f"[IG-CFD] Refreshed account after stop-breach backstop / orphan cleanup closes ({closed_keys}): "
            f"balance={account['balance']:.2f} available={account['available']:.2f}"
        )

    # Per-instrument market status, read fresh every tick -- this is what
    # actually gates trading hours instead of a hardcoded calendar. Also
    # supplies lot_size/margin_factor for the margin-based limit sync below.
    snapshots = {}
    for key, inst in INSTRUMENTS.items():
        if not inst.epic:
            continue
        snap = broker.get_market_snapshot(inst.epic)
        snapshots[key] = snap
        status = snap.get("market_status") if snap else "UNKNOWN"
        logger.info(f"[IG-CFD] {key} ({inst.display_name}): market_status={status}")

    # Margin-based take-profit AND stop-loss are now REAL resting IG orders, set at
    # open time (see MARGIN_PROFIT_TAKE_PCT/MARGIN_STOP_LOSS_PCT) -- IG's own engine
    # executes either instantly the moment price touches it, not on our tick cadence.
    # This just reconciles any position whose live stop/limit doesn't already match
    # those targets (e.g. it predates the current % values) by amending them in
    # place -- never closes anything itself.
    _sync_margin_based_exits(broker, positions, snapshots, RULES.max_leverage_multiple)

    tradeable_instruments = {k: s for k, s in snapshots.items() if broker.is_tradeable(s)}
    if not tradeable_instruments:
        logger.info("[IG-CFD] No tracked instrument's market is currently TRADEABLE. Skipping this tick.")
        return

    positions_with_pnl = {key: {**pos, "unrealized_pnl_usd": _estimate_unrealized_pnl(pos)}
                           for key, pos in positions.items()}

    portfolio_state = {
        "account": account,
        "positions": positions_with_pnl,
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
    tool_call_log = []
    try:
        trades, tool_call_log = get_agent_trades(playbook, portfolio_state, now_str)
    except AgentCallFailed as e:
        # Fails SAFE (no trades this tick, same as a real HOLD would), but
        # logged loudly and distinctly in the audit trail/dashboard -- an
        # agent-call failure must never look identical to a legitimate HOLD,
        # or a persistent outage (e.g. a broken OpenAI connection) can go
        # unnoticed indefinitely, which is exactly what happened before this.
        logger.error(f"[IG-CFD] {e}")
        _log_order_event({"action": "AGENT_CALL_FAILED", "status": "ERROR", "reason": str(e)})
        trades = []

    if not trades:
        logger.info("[IG-CFD] Agent proposed no trades this tick (HOLD).")
    else:
        # Tracks margin consumption across MULTIPLE trades within this single
        # tick, so the safety buffer reflects reality even if the agent opens
        # several positions at once (more likely now that both directions are
        # actively encouraged) -- see _validate_trade's docstring.
        running_available = account["available"]
        last_close_info = _get_last_close_info(DATA_DIR / "order_log.jsonl")
        loss_streak_info = _get_same_direction_loss_streak(DATA_DIR / "order_log.jsonl")

        for trade in trades:
            action = trade.get("action", "").upper()

            # OPEN_SPREAD is unreachable now -- removed from
            # PROPOSE_TRADES_SCHEMA's action enum back when WTI was a pure
            # auto-mirror of Brent (a spread needs WTI to move OPPOSITE
            # Brent, which conflicted with always mirroring it). WTI trades
            # independently again now (2026-09-23), but the schema entry was
            # never restored -- left in place, like get_inventory_data, in
            # case spread trading is ever explicitly reintroduced.
            if action == "OPEN_SPREAD":
                long_instrument = (trade.get("long_instrument") or "").upper()
                short_instrument = (trade.get("short_instrument") or "").upper()

                ok, reason = _validate_spread_trade(trade, account, positions, RULES, running_available=running_available,
                                                     checked_multiple_sources=True,  # unreachable path, see comment above
                                                     last_close_info=last_close_info)
                if not ok:
                    _log_order_event({"action": action, "long_instrument": long_instrument, "short_instrument": short_instrument,
                                       "status": "REJECTED", "reason": reason})
                    continue

                long_snapshot = snapshots.get(long_instrument)
                short_snapshot = snapshots.get(short_instrument)
                if not broker.is_tradeable(long_snapshot) or not broker.is_tradeable(short_snapshot):
                    _log_order_event({"action": action, "long_instrument": long_instrument, "short_instrument": short_instrument,
                                       "status": "REJECTED", "reason": "One or both legs' market no longer tradeable"})
                    continue

                half_alloc = trade["allocation_pct"] / 2
                long_inst_cfg = INSTRUMENTS[long_instrument]
                short_inst_cfg = INSTRUMENTS[short_instrument]

                long_sizing, long_err = _compute_position_size(
                    account["balance"], half_alloc, long_snapshot, RULES.max_leverage_multiple, long_inst_cfg.min_deal_size,
                )
                short_sizing, short_err = _compute_position_size(
                    account["balance"], half_alloc, short_snapshot, RULES.max_leverage_multiple, short_inst_cfg.min_deal_size,
                )
                if long_err or short_err:
                    _log_order_event({"action": action, "long_instrument": long_instrument, "short_instrument": short_instrument,
                                       "status": "REJECTED", "reason": f"Sizing failed: long={long_err}, short={short_err}"})
                    continue

                combined_margin = long_sizing["margin_allocated"] + short_sizing["margin_allocated"]
                projected_available = running_available - combined_margin
                if not _margin_headroom_ok(projected_available, account["balance"], RULES):
                    _log_order_event({
                        "action": action, "long_instrument": long_instrument, "short_instrument": short_instrument,
                        "status": "REJECTED",
                        "reason": (
                            f"Combined spread margin (${combined_margin:.2f}) would leave available at "
                            f"${projected_available:.2f}, below the {RULES.margin_safety_buffer_pct}% safety buffer"
                        ),
                    })
                    continue

                # Open the LONG leg first.
                long_stop_distance = _margin_based_stop_distance(long_sizing["margin_allocated"], long_sizing["size"])
                long_limit_distance = _margin_based_limit_distance(long_sizing["margin_allocated"], long_sizing["size"])
                long_result = broker.open_position(
                    epic=long_inst_cfg.epic, direction="BUY", size=long_sizing["size"],
                    stop_distance=long_stop_distance, limit_distance=long_limit_distance,
                    currency_code=account["currency"], expiry=long_snapshot.get("expiry", "-"),
                )
                _log_order_event({
                    "action": "OPEN_SPREAD_LEG", "instrument": long_instrument, "direction": "BUY",
                    "size": long_sizing["size"], "margin_allocated": long_sizing["margin_allocated"],
                    "notional": long_sizing["notional"], "stop_distance": long_stop_distance, "limit_distance": long_limit_distance,
                    "reason": trade.get("reason", ""), **long_result,
                })
                if long_result["status"] != "submitted":
                    # Long leg itself failed -- nothing to roll back yet.
                    continue

                # Now the SHORT leg.
                short_stop_distance = _margin_based_stop_distance(short_sizing["margin_allocated"], short_sizing["size"])
                short_limit_distance = _margin_based_limit_distance(short_sizing["margin_allocated"], short_sizing["size"])
                short_result = broker.open_position(
                    epic=short_inst_cfg.epic, direction="SELL", size=short_sizing["size"],
                    stop_distance=short_stop_distance, limit_distance=short_limit_distance,
                    currency_code=account["currency"], expiry=short_snapshot.get("expiry", "-"),
                )
                _log_order_event({
                    "action": "OPEN_SPREAD_LEG", "instrument": short_instrument, "direction": "SELL",
                    "size": short_sizing["size"], "margin_allocated": short_sizing["margin_allocated"],
                    "notional": short_sizing["notional"], "stop_distance": short_stop_distance, "limit_distance": short_limit_distance,
                    "reason": trade.get("reason", ""), **short_result,
                })

                if short_result["status"] == "submitted":
                    running_available -= combined_margin
                else:
                    # SHORT leg failed after LONG already succeeded -- roll back
                    # the long leg immediately rather than leaving an unintended
                    # naked directional position. This is the entire point of
                    # building spreads carefully rather than as two independent
                    # opens that happen to land in the same tick.
                    rollback_result = broker.close_position(
                        deal_id=long_result["deal_id"], direction="BUY", epic=long_inst_cfg.epic,
                        size=long_sizing["size"], expiry=long_snapshot.get("expiry", "-"),
                    )
                    _log_order_event({
                        "action": "SPREAD_ROLLBACK", "instrument": long_instrument,
                        "reason": (
                            f"Short leg ({short_instrument}) failed after long leg ({long_instrument}) succeeded -- "
                            f"closing the long leg immediately to avoid an unintended naked directional position."
                        ),
                        **rollback_result,
                    })
                continue

            instrument = trade.get("instrument", "").upper()

            if action in ("OPEN_LONG", "OPEN_SHORT"):
                confluence_ok, confluence_reason = check_confluence(tool_call_log, instrument, action)
            else:
                confluence_ok, confluence_reason = True, ""

            ok, reason = _validate_trade(trade, account, positions, RULES, running_available=running_available,
                                          checked_multiple_sources=confluence_ok, confluence_reason=confluence_reason,
                                          last_close_info=last_close_info, loss_streak_info=loss_streak_info)
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

                # Precise post-trade check: does THIS trade's specific margin
                # requirement, on top of whatever's already been committed
                # this tick, still leave enough headroom? (The check inside
                # _validate_trade above only caught the case where we were
                # ALREADY below buffer before this trade -- this catches the
                # case where this trade would be what tips us under it.)
                projected_available = running_available - sizing["margin_allocated"]
                if not _margin_headroom_ok(projected_available, account["balance"], RULES):
                    _log_order_event({
                        "action": action, "instrument": instrument, "status": "REJECTED",
                        "reason": (
                            f"This trade's margin (${sizing['margin_allocated']:.2f}) would leave available "
                            f"at ${projected_available:.2f}, below the {RULES.margin_safety_buffer_pct}% safety "
                            f"buffer of balance ${account['balance']:.2f} -- rejected to avoid risking a "
                            f"forced closure on this or other open positions"
                        ),
                    })
                    continue

                direction = "BUY" if action == "OPEN_LONG" else "SELL"
                min_stop_distance = (snapshot or {}).get("min_stop_distance")
                stop_distance, limit_distance = _initial_stop_and_limit_distance(
                    sizing["margin_allocated"], sizing["size"], min_stop_distance, instrument,
                )

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
                    # Persisted so a future pass over order_log.jsonl can actually correlate
                    # confluence agreement against real outcomes -- checked_multiple_sources
                    # was never logged before this, only used transiently to gate the open.
                    "confluence_detail": confluence_reason,
                    "reason": trade.get("reason", ""), **result,
                })
                if result["status"] == "submitted":
                    running_available -= sizing["margin_allocated"]

            else:  # CLOSE
                pos = positions[instrument]
                result = broker.close_position(
                    deal_id=pos["deal_id"], direction=pos["direction"], epic=pos["epic"],
                    size=pos["size"], expiry=snapshot.get("expiry", "-") if snapshot else "-",
                )
                _log_order_event({
                    "action": "CLOSE", "instrument": instrument, "deal_id": pos["deal_id"],
                    # original_direction (not the closing/reversed direction inside **result) is
                    # what the same-direction cooldown check needs to detect "re-opening the exact
                    # thesis that just lost" -- see _get_last_close_info.
                    "original_direction": pos["direction"],
                    "reason": trade.get("reason", ""), **result,
                })

    # Refresh + snapshot final state for the dashboard.
    account = broker.get_account_state()
    positions = broker.get_positions(epic_to_key)
    for pos in positions.values():
        pos["unrealized_pnl_usd"] = _estimate_unrealized_pnl(pos)
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
    # CFD_EVENT_TYPE is set by the workflow from github.event.action, which is
    # only populated for a repository_dispatch trigger -- a "schedule" or
    # "workflow_dispatch" run leaves it unset, and anything other than exactly
    # "watch" runs the normal full tick (fails toward "just run everything",
    # today's existing behavior, rather than ever silently skipping a real tick).
    if os.getenv("CFD_EVENT_TYPE") == "watch":
        run_watch_check()
    else:
        run_cfd_tick()
