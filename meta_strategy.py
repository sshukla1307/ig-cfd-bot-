"""
IG CFD Trading Bot — Meta Strategy (Day 0)

Run once to define the agent's investment playbook for its 4-instrument
universe. Injected into the prompt at every live check-in afterward.
"""

import logging
from pathlib import Path

from config import RULES, PERSONA_PROMPT, INSTRUMENTS, PLAYBOOKS_DIR
from api_adapters import OpenAIClient

logger = logging.getLogger(__name__)

PLAYBOOK_FILENAME = "cfd_aggressive.md"

DAY_0_PROMPT = f"""
You are about to begin trading a real IG CFD account autonomously, focused
exclusively on 3 instruments: Brent Crude Oil, WTI Crude Oil, and Natural Gas.
Before we start, define your "Meta-Strategy" (Investment Playbook).

This playbook will be injected into your prompt at every live check-in
(roughly every {RULES.min_tick_interval_minutes} minutes while at least one of
these markets is open). You must adhere to it.

=== SYSTEM CONSTRAINTS ===
- Execution: LIVE — orders fill immediately at the current market price via leveraged CFDs.
- Universe: Brent Crude Oil, WTI Crude Oil, Natural Gas ONLY. Nothing else.
- Position Sizing: {RULES.min_allocation_pct}-{RULES.max_allocation_pct}% of account equity (as margin) per position.
- Leverage: hard-capped at {RULES.max_leverage_multiple}x notional exposure per unit of margin, regardless of what IG's own margin terms would otherwise permit.
- Risk Management: every position requires a mandatory Stop-Loss AND Take-Profit (both required) — but YOU decide where to place them.
- Max {RULES.max_positions} concurrent positions (one per instrument).

Please define your Playbook answering the following:
1. Core Strategy: momentum, mean-reversion, catalyst-driven (inventory reports, OPEC+ decisions, geopolitical supply shocks), or macro-driven (dollar strength, rates)?
2. Per-Instrument Approach: do all 3 instruments get equal treatment, or do you weight some more heavily (e.g. WTI/Brent tend to be more liquid and catalyst-rich than natural gas)?
3. Trade Horizon: how long do you expect to typically hold a leveraged position open?
4. Risk Profile: typical Stop-Loss % and Take-Profit % given leverage risk?
5. Data Usage: how will you prioritize technicals vs. news/catalysts vs. macro context?

Output a detailed, structured markdown document. This is your constitution.
No greetings, just the markdown document.
"""


def generate_playbook(force: bool = False):
    playbook_path = PLAYBOOKS_DIR / PLAYBOOK_FILENAME

    if playbook_path.exists() and playbook_path.stat().st_size > 0 and not force:
        logger.info(f"Playbook already exists at {playbook_path}. Skipping (pass force=True to regenerate).")
        return

    logger.info("Generating Day 0 Playbook...")
    system_prompt = f"YOU ARE A LIVE IG CFD TRADING AGENT — AGGRESSIVE PERSONA.\n{PERSONA_PROMPT}\n"

    client = OpenAIClient()
    try:
        playbook = client.generate(system_prompt, DAY_0_PROMPT, tools=[], max_tool_calls=0)
        playbook_path.parent.mkdir(parents=True, exist_ok=True)
        with open(playbook_path, "w", encoding="utf-8") as f:
            f.write(playbook)
        logger.info(f"Playbook saved to {playbook_path}.")
    except Exception as e:
        logger.error(f"Failed to generate playbook: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    generate_playbook()
