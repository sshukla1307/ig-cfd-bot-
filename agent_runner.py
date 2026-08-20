"""
IG CFD Trading Bot — Agent Runner

Prompt-building and tool schemas for the LLM decision loop. No broker
dependency of its own -- cfd_runner.py handles validation/execution.
"""

import json
import logging

from config import RULES, PERSONA_PROMPT, INSTRUMENTS
from api_adapters import OpenAIClient

logger = logging.getLogger(__name__)


# Only instruments with a resolved epic are offered to the agent at all --
# e.g. Palladium is excluded (see config.py) rather than being proposable
# and always rejected, which would just waste tool-call budget each tick.
INSTRUMENT_KEYS = [key for key, inst in INSTRUMENTS.items() if inst.epic]

TOOLS = [
    {
        "name": "get_technicals",
        "description": "Get RSI-14, SMA-20/50, and recent price history for one of the 4 tracked instruments.",
        "parameters": {
            "type": "object",
            "properties": {
                "instrument": {"type": "string", "enum": INSTRUMENT_KEYS},
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
                    "required": ["action", "instrument", "reason"],
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["OPEN_LONG", "OPEN_SHORT", "CLOSE"],
                            "description": "OPEN_LONG/OPEN_SHORT open a new position (rejected if one is already open on this instrument). CLOSE fully closes an existing position, whichever direction it is.",
                        },
                        "instrument": {"type": "string", "enum": INSTRUMENT_KEYS},
                        "allocation_pct": {
                            "type": "number",
                            "minimum": RULES.min_allocation_pct,
                            "maximum": RULES.max_allocation_pct,
                            "description": "For OPEN_LONG/OPEN_SHORT only: % of account equity to allocate as margin for this position.",
                        },
                        "stop_loss_pct": {
                            "type": "number",
                            "minimum": 1,
                            "maximum": 50,
                            "description": "For OPEN_LONG/OPEN_SHORT only: % adverse price move from entry that triggers the stop. Mandatory.",
                        },
                        "take_profit_pct": {
                            "type": "number",
                            "minimum": 1,
                            "maximum": 200,
                            "description": "For OPEN_LONG/OPEN_SHORT only: % favorable price move from entry that triggers the take-profit. Mandatory.",
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
    prompt += f"- Max {RULES.max_positions} concurrent positions (one per instrument, {RULES.max_positions} instruments total)\n"
    prompt += f"- Hard leverage cap: {RULES.max_leverage_multiple}x notional exposure per unit of margin allocated, regardless of what IG's own margin factor for the instrument would otherwise permit\n"
    prompt += "- Every OPEN_LONG/OPEN_SHORT MUST include both stop_loss_pct and take_profit_pct -- both mandatory, no exceptions\n"
    prompt += f"- If available margin drops below {RULES.margin_safety_buffer_pct}% of account balance, ALL new opens are blocked account-wide until it recovers\n\n"

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
    prompt += "1. Use your tools to check technicals/news/macro for any instrument you're considering.\n"
    prompt += "2. Decide: open a new long/short, close an existing position, or hold, per instrument.\n"
    prompt += "3. You MUST end your turn by calling propose_trades exactly once.\n"
    return prompt


def build_user_prompt(portfolio_state: dict, now_str: str) -> str:
    prompt = f"Current time: {now_str}\n\n"
    prompt += "=== YOUR CURRENT ACCOUNT & POSITIONS ===\n"
    prompt += json.dumps(portfolio_state, indent=2) + "\n\n"
    prompt += "What are your trading decisions for this check-in? Use your tools if you need more context, then call propose_trades."
    return prompt


def get_agent_trades(playbook: str, portfolio_state: dict, now_str: str) -> list:
    client = OpenAIClient()
    sys_prompt = build_system_prompt(playbook)
    user_prompt = build_user_prompt(portfolio_state, now_str)
    tools = TOOLS + [PROPOSE_TRADES_SCHEMA]

    try:
        result_json = client.generate(sys_prompt, user_prompt, tools, max_tool_calls=10)
    except Exception as e:
        logger.error(f"Agent crashed: {e}")
        return []

    if not result_json:
        logger.error("Agent failed to propose trades.")
        return []

    try:
        data = json.loads(result_json)
        trades = data.get("trades", [])
        logger.info(f"Agent proposed {len(trades)} trades. Notes: {data.get('notes', '')}")
        return trades
    except json.JSONDecodeError:
        logger.error(f"Agent returned invalid JSON: {result_json}")
        return []
