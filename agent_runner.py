"""
IG CFD Trading Bot — Agent Runner

Prompt-building and tool schemas for the LLM decision loop. No broker
dependency of its own -- cfd_runner.py handles validation/execution.
"""

import json
import logging

from config import RULES, PERSONA_PROMPT, INSTRUMENTS, LLM_PROVIDER, OPENAI_MODEL, ANTHROPIC_MODEL
from api_adapters import OpenAIClient, AnthropicClient


def _make_llm_client():
    if LLM_PROVIDER == "anthropic":
        return AnthropicClient(model=ANTHROPIC_MODEL)
    return OpenAIClient(model=OPENAI_MODEL)

logger = logging.getLogger(__name__)


# Only instruments with a resolved epic are offered to the agent at all --
# e.g. Palladium is excluded (see config.py) rather than being proposable
# and always rejected, which would just waste tool-call budget each tick.
INSTRUMENT_KEYS = [key for key, inst in INSTRUMENTS.items() if inst.epic]

# WTI_OIL is deliberately excluded here: per explicit user request, WTI no
# longer gets independent research or an independent trading decision -- it's
# now a pure auto-mirror of BRENT_OIL (see cfd_runner.py's mirroring logic).
# No point spending tool-call budget on WTI's own technicals/seasonality/term
# structure when its direction and size are dictated entirely by Brent.
RESEARCH_INSTRUMENT_KEYS = [k for k in INSTRUMENT_KEYS if k != "WTI_OIL"]

TOOLS = [
    {
        "name": "get_technicals",
        "description": "Get RSI-14, SMA-20/50, and recent price history for an instrument. WTI_OIL is not offered here -- it's an auto-mirror of BRENT_OIL, not independently researched.",
        "parameters": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string", "enum": RESEARCH_INSTRUMENT_KEYS},
            },
            "required": ["instrument"],
        },
    },
    {
        "name": "get_commodity_news",
        "description": "Search for commodity-specific news/catalysts (inventory reports, OPEC+ decisions, geopolitical supply events).",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "e.g. 'EIA crude oil inventory report this week', 'OPEC+ production decision'"},
                "count": {"type": "integer", "minimum": 3, "maximum": 10, "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_macro",
        "description": "Get US dollar index, VIX, and 10Y Treasury yield -- macro context that moves commodity prices.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_seasonality",
        "description": "Get the calendar-based seasonal demand bias for an instrument (e.g. NG winter heating withdrawal season vs summer injection season). Deterministic, always available. WTI_OIL is not offered here -- it's an auto-mirror of BRENT_OIL.",
        "parameters": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string", "enum": RESEARCH_INSTRUMENT_KEYS},
            },
            "required": ["instrument"],
        },
    },
    {
        "name": "get_term_structure",
        "description": "Get the futures term structure (contango vs backwardation) for an instrument -- compares the near-month price to a dated contract ~3 months out. Reflects the market's own forward supply/demand expectation, a different signal from spot technicals. WTI_OIL is not offered here -- it's an auto-mirror of BRENT_OIL.",
        "parameters": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string", "enum": RESEARCH_INSTRUMENT_KEYS},
            },
            "required": ["instrument"],
        },
    },
    # get_inventory_data is intentionally NOT exposed here -- confirmed live in
    # production that FRED doesn't carry weekly EIA petroleum/gas inventory
    # data at all (it only has price series like WTI spot/futures), so every
    # call failed with "the series does not exist" while still burning a slot
    # from the tool-call budget. The function still exists in market_data.py;
    # re-enable this schema entry once it's switched to EIA's own API
    # (api.eia.gov, needs a separate EIA_API_KEY -- see market_data.py).
    {
        "name": "get_positioning_data",
        "description": "Get managed-money (speculator/hedge fund) net futures positioning from CFTC's weekly Commitment of Traders report, and how extreme it is vs the trailing year -- crowded positioning is a real contrarian signal. NOT available for BRENT_OIL (ICE-listed, outside CFTC jurisdiction).",
        "parameters": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string", "enum": ["WTI_OIL", "NATURAL_GAS"]},
            },
            "required": ["instrument"],
        },
    },
    {
        "name": "get_weather_demand",
        "description": "Get real, current national weather-driven demand for Natural Gas -- heating degree days (winter demand) and cooling degree days (summer A/C-driven power-gen demand), with the recent trend. NATURAL_GAS only. A real-time signal, not the static calendar-only proxy get_seasonality uses.",
        "parameters": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string", "enum": ["NATURAL_GAS"]},
            },
            "required": ["instrument"],
        },
    },
]

PROPOSE_TRADES_SCHEMA = {
    "name": "propose_trades",
    "description": "Submit your trading decisions for this check-in. Call this exactly once, even to propose zero trades (HOLD).",
    "parameters": {
        "type": "object",
        "required": ["trades", "notes"],
        "properties": {
            "trades": {
                "type": "array",
                "description": "Empty array means HOLD -- no action this check-in.",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "required": ["action", "reason"],
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["OPEN_LONG", "OPEN_SHORT", "CLOSE"],
                            "description": "OPEN_LONG/OPEN_SHORT open a new single-instrument position (rejected if one is already open on that instrument, and rejected outright for WTI_OIL -- see the WTI mirroring rule). CLOSE fully closes an existing position on one instrument, whichever direction it is.",
                        },
                        "instrument": {"type": "string", "enum": ["BRENT_OIL", "NATURAL_GAS", "WTI_OIL"],
                                       "description": "Required for OPEN_LONG/OPEN_SHORT/CLOSE. Only propose OPEN_LONG/OPEN_SHORT on BRENT_OIL or NATURAL_GAS -- WTI_OIL positions are opened/closed automatically as a mirror of BRENT_OIL, never propose them directly (CLOSE on WTI_OIL will just be rejected as unnecessary since the mirror handles it)."},
                        "allocation_pct": {
                            "type": "number",
                            "minimum": RULES.min_allocation_pct,
                            "maximum": RULES.max_allocation_pct,
                            "description": "For OPEN_LONG/OPEN_SHORT: % of account equity to allocate as margin for BRENT_OIL/NATURAL_GAS. When you open BRENT_OIL, the exact same allocation_pct is automatically mirrored onto WTI_OIL too.",
                        },
                        "stop_loss_pct": {
                            "type": "number",
                            "minimum": 1,
                            "maximum": 50,
                            "description": "For OPEN_LONG/OPEN_SHORT: % adverse price move from entry that triggers the stop. Mandatory.",
                        },
                        "take_profit_pct": {
                            "type": "number",
                            "minimum": 1,
                            "maximum": 200,
                            "description": "For OPEN_LONG/OPEN_SHORT: % favorable price move from entry that triggers the take-profit. Mandatory.",
                        },
                        "reason": {"type": "string", "minLength": 10},
                    },
                },
            },
            "notes": {"type": "string", "description": "Brief market outlook for this check-in."},
        },
    },
}


def build_system_prompt(playbook: str) -> str:
    prompt = f"YOU ARE A LIVE IG CFD TRADING AGENT.\n\n"
    prompt += "=== SYSTEM RULES (enforced by code, not by you) ===\n"
    prompt += f"- Position sizing: {RULES.min_allocation_pct}-{RULES.max_allocation_pct}% of account equity (as margin) per position\n"
    prompt += f"- Max {RULES.max_positions} concurrent positions (one per instrument, {RULES.max_positions} instruments total). A Brent open uses 2 of these 3 slots (Brent + its WTI mirror).\n"
    prompt += f"- Hard leverage cap: {RULES.max_leverage_multiple}x notional exposure per unit of margin allocated, regardless of what IG's own margin factor for the instrument would otherwise permit\n"
    prompt += "- Every OPEN_LONG/OPEN_SHORT MUST include both stop_loss_pct and take_profit_pct -- both mandatory, no exceptions\n"
    prompt += (
        "- WTI_OIL IS A PURE MIRROR OF BRENT_OIL, not an independently-traded instrument anymore. "
        "You only ever decide on BRENT_OIL and NATURAL_GAS. Whenever you OPEN_LONG or OPEN_SHORT "
        "BRENT_OIL, the exact same direction and allocation_pct is automatically opened on WTI_OIL "
        "too, atomically -- if the WTI leg fails after Brent already opened, Brent is immediately "
        "closed back out rather than leaving you with an unintended naked Brent-only position. When "
        "you CLOSE BRENT_OIL, its WTI mirror is closed automatically at the same time. Never propose "
        "OPEN_LONG/OPEN_SHORT/CLOSE on WTI_OIL yourself -- it will simply be rejected. Don't research "
        "WTI's own technicals/term structure either; its direction and size are fully determined by "
        "your Brent decision, not by anything WTI-specific.\n"
    )
    prompt += f"- If available margin drops below {RULES.margin_safety_buffer_pct}% of account balance, ALL new opens are blocked account-wide until it recovers\n"
    if RULES.require_confluence:
        prompt += (
            "- Opening a position requires having called at least one of get_commodity_news/get_macro/"
            "get_seasonality/get_term_structure/get_positioning_data this check-in, in addition to "
            "technicals -- a technical signal with zero other tool calls this tick will be rejected.\n"
        )
    prompt += f"- Re-opening the same direction on an instrument is blocked for {RULES.same_direction_cooldown_minutes} min after a losing close there.\n"
    prompt += (
        f"- A discretionary CLOSE (you choosing to exit) is blocked within "
        f"{RULES.min_hold_minutes_before_discretionary_close} min of opening -- real trade data showed a "
        f"~30min median hold vs the 34-48h a backtested strategy actually needed to resolve its stop/target, "
        f"so a position needs to be given real time before you decide it isn't working. This does NOT reduce "
        f"safety: its actual stop-loss/take-profit and the stop-breach backstop still protect it the whole "
        f"time, independently of you.\n\n"
    )

    prompt += "=== YOUR PERSONA ===\n"
    prompt += f"{PERSONA_PROMPT}\n\n"

    prompt += "=== YOUR PLAYBOOK ===\n"
    prompt += "You defined this on Day 0. You MUST adhere to it.\n"
    prompt += f"{playbook}\n\n"

    prompt += "=== LIVE CHECK-IN ===\n"
    prompt += (
        "This is a LIVE check-in against a REAL IG CFD account, repeating roughly every "
        f"{RULES.min_tick_interval_minutes} minutes while at least one of your {len(INSTRUMENT_KEYS)} instruments' "
        "markets is open. Any trade you propose fills IMMEDIATELY at the live market price -- "
        "there is no paper simulation and no human reviewing your orders before they execute. "
        "CFDs are leveraged: a losing move against you compounds faster than in an unleveraged "
        "account, and a margin call can force-close a position at the worst possible moment. "
        "You decide your own cadence -- if you have no new information or edge since your last "
        "check-in, propose an empty trades list (HOLD). Do not trade just because you were asked.\n"
    )

    prompt += "\n=== INSTRUCTIONS ===\n"
    prompt += (
        "1. Use your tools to check technicals/news/macro for any instrument you're considering. "
        "You also have get_seasonality (deterministic calendar-based demand bias), get_term_structure "
        "(contango/backwardation -- the market's own forward supply/demand expectation, a genuinely "
        "different signal from spot technicals), get_positioning_data (CFTC managed-money "
        "positioning vs its trailing-year range for WTI/Natural Gas only, not Brent -- extreme crowding "
        "is a real contrarian signal), and get_weather_demand (real, current heating/cooling degree-day "
        "data for Natural Gas -- prefer this over get_seasonality's static calendar proxy when deciding "
        "on NG, it's the actual current weather driving demand, not just what month it is). "
        "Be efficient: you have a limited number of tool calls "
        "per check-in -- don't exhaustively check every tool for every instrument if you're not seriously "
        "considering a trade there. Focus deep research on the 1-2 instruments you're actually weighing, "
        "and you MUST leave room to call propose_trades before running out -- hitting the limit without "
        "concluding is treated as a system failure, not a valid HOLD. get_commodity_news specifically is "
        "capped at 3 calls per check-in, hard-enforced -- rephrasing the same question with slightly "
        "different wording won't surface new information; if 1-2 searches don't give you a clear answer, "
        "decide (or HOLD) with what you have rather than searching again.\n"
    )
    prompt += "2. Decide: open a new long/short, close an existing position, or hold, per instrument.\n"
    prompt += (
        "3. Each open position below includes unrealized_pnl_usd -- the ACTUAL real-time dollar "
        "profit/loss on that position right now (verified against real IG settlement figures, not "
        "a rough estimate). Use this real number, not a guess from the raw price levels, to judge "
        "whether a gain is substantial enough to bank or a loss is becoming severe -- see your house style.\n"
    )
    prompt += "4. You MUST end your turn by calling propose_trades exactly once.\n"
    return prompt


def build_user_prompt(portfolio_state: dict, now_str: str) -> str:
    prompt = f"Current time: {now_str}\n\n"
    prompt += "=== YOUR CURRENT ACCOUNT & POSITIONS ===\n"
    prompt += json.dumps(portfolio_state, indent=2) + "\n\n"
    prompt += "What are your trading decisions for this check-in? Use your tools if you need more context, then call propose_trades."
    return prompt


class AgentCallFailed(Exception):
    """Raised when the LLM call itself failed or didn't produce a usable
    decision -- distinct from the agent successfully deciding to hold. The
    caller (cfd_runner.py) still treats this as "no trades this tick" for
    safety, but logs it loudly and visibly instead of silently indistinguishable
    from a legitimate HOLD -- this exact silent-swallowing is what let a
    persistent OpenAI connection failure go unnoticed across every real tick
    this account has run."""


def get_agent_trades(playbook: str, portfolio_state: dict, now_str: str) -> tuple:
    """Returns (trades, checked_multiple_sources). checked_multiple_sources is
    True iff the agent called get_commodity_news and/or get_macro this turn --
    an objective, code-verifiable minimum for the confluence requirement (see
    RULES.require_confluence in config.py), independent of whatever the agent
    itself claims it considered."""
    client = _make_llm_client()
    sys_prompt = build_system_prompt(playbook)
    user_prompt = build_user_prompt(portfolio_state, now_str)
    tools = TOOLS + [PROPOSE_TRADES_SCHEMA]
    tools_called = set()

    try:
        # 20, not 10: with tool_choice="required" forcing a call every turn
        # and 5 research tools x 3 instruments potentially relevant, 10 was
        # observed live to run out before the agent ever reached
        # propose_trades (logged as AGENT_CALL_FAILED, no decision made at
        # all that tick -- for every instrument, not just one).
        result_json = client.generate(sys_prompt, user_prompt, tools, max_tool_calls=20,
                                       tool_call_tracker=tools_called)
    except Exception as e:
        raise AgentCallFailed(f"LLM call crashed ({LLM_PROVIDER}): {e}") from e

    if not result_json:
        raise AgentCallFailed("Agent responded without ever calling propose_trades (hit max tool calls or returned nothing)")

    checked_multiple_sources = bool(tools_called & {
        "get_commodity_news", "get_macro", "get_seasonality", "get_term_structure",
        "get_positioning_data", "get_weather_demand",
    })

    try:
        data = json.loads(result_json)
        trades = data.get("trades", [])
        logger.info(f"Agent proposed {len(trades)} trades. Notes: {data.get('notes', '')}")
        return trades, checked_multiple_sources
    except json.JSONDecodeError:
        raise AgentCallFailed(f"Agent returned invalid JSON: {result_json}")
