"""
IG CFD Trading Bot — LLM Adapter

Two interchangeable providers (OpenAIClient, AnthropicClient), both forced
temperature=0, both sharing the same execute_tool dispatcher so tool
behavior can never drift between them. Each runs a tool-calling loop until
the agent calls propose_trades, same battle-tested pattern as the Alpaca
project's UnifiedLLMClient. agent_runner.py picks which one to use via
config.LLM_PROVIDER.
"""

import json
import os
import random
import time
import logging
from typing import Callable

logger = logging.getLogger(__name__)


def execute_tool(name: str, args: dict) -> dict:
    """Shared by both OpenAIClient and AnthropicClient -- one dispatcher, so
    adding/changing a tool can never accidentally update only one provider."""
    from market_data import (get_technicals, get_commodity_news, get_macro,
                              get_seasonality, get_term_structure, get_inventory_data,
                              get_positioning_data, get_weather_demand)
    from config import YFINANCE_TICKERS

    if name == "get_technicals":
        instrument = args.get("instrument")
        yf_ticker = YFINANCE_TICKERS.get(instrument)
        if not yf_ticker:
            return {"error": f"Unknown instrument: {instrument}"}
        return get_technicals(yf_ticker)
    if name == "get_commodity_news":
        return get_commodity_news(args.get("query", ""), count=args.get("count", 5))
    if name == "get_macro":
        return get_macro()
    if name == "get_seasonality":
        return get_seasonality(args.get("instrument"))
    if name == "get_term_structure":
        return get_term_structure(args.get("instrument"))
    if name == "get_inventory_data":
        return get_inventory_data(args.get("instrument"))
    if name == "get_positioning_data":
        return get_positioning_data(args.get("instrument"))
    if name == "get_weather_demand":
        return get_weather_demand(args.get("instrument"))
    return {"error": f"Unknown tool: {name}"}


MAX_CALLS_PER_TOOL = 3  # prevents a runaway research loop -- observed live:
# Claude called get_commodity_news 11+ times in one tick with rephrased
# variations of the same question ("is the Hormuz risk premium fading?"),
# burning the entire tool-call budget without ever reaching propose_trades.
# Applies per tool name, per generate() call (call_counts is fresh each tick).


def execute_tool_capped(name: str, args: dict, call_counts: dict) -> dict:
    """Wraps execute_tool with a per-tool-name call limit for this tick. Once
    exceeded, returns a clear nudge instead of actually calling the tool again
    -- code-level, so it holds regardless of which model's judgment about
    "have I researched enough" turns out to be unreliable."""
    call_counts[name] = call_counts.get(name, 0) + 1
    if call_counts[name] > MAX_CALLS_PER_TOOL:
        return {
            "error": (
                f"You've already called {name} {MAX_CALLS_PER_TOOL} times this check-in. "
                "Stop researching this further and make your decision (or HOLD) with the "
                "information you already have -- rephrasing the same question again won't help."
            )
        }
    return execute_tool(name, args)


def retry_with_backoff(func: Callable, max_retries: int = 5, base_delay: float = 2.0):
    def wrapper(*args, **kwargs):
        retries = 0
        while retries < max_retries:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                err_msg = str(e).lower()
                is_rate_limit = any(x in err_msg for x in ["429", "rate limit", "quota"])
                is_timeout = any(x in err_msg for x in ["timeout", "timed out", "deadline exceeded"])
                is_unavailable = any(x in err_msg for x in ["503", "unavailable", "service unavailable"])
                is_connection_error = any(x in err_msg for x in [
                    "connection error", "connection reset", "connection aborted",
                    "network is unreachable", "failed to establish a new connection",
                    "remote end closed connection",
                ])
                if is_rate_limit or is_timeout or is_unavailable or is_connection_error:
                    retries += 1
                    if retries >= max_retries:
                        logger.error(f"Max retries reached. Final error: {e}")
                        raise
                    delay = base_delay * (2 ** (retries - 1)) + random.uniform(0, 1)
                    logger.warning(f"Retry {retries}/{max_retries} due to: {e}. Sleeping {delay:.1f}s...")
                    time.sleep(delay)
                else:
                    raise
        return None
    return wrapper


class OpenAIClient:
    def __init__(self, model: str = "gpt-4o"):
        self.model = model
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            logger.warning("OPENAI_API_KEY not found. Agent will fail.")

    def _client(self):
        import httpx
        import openai
        # Explicitly force HTTP/1.1 (httpx defaults to this anyway, but the
        # persistent "Connection error." seen on GitHub Actions runners lines
        # up with a known class of HTTP/2 send-side bug -- forcing http2=False
        # here is a defensive, zero-cost guard against it ever being enabled,
        # intentionally or via a future httpx/openai default change.
        http_client = httpx.Client(http2=False)
        return openai.OpenAI(api_key=self.api_key, http_client=http_client)

    def generate(self, system_prompt: str, user_prompt: str, tools: list, max_tool_calls: int = 10,
                 tool_call_tracker: set = None) -> str:
        """tool_call_tracker: if provided, every non-propose_trades tool name
        called during this turn is added to it -- lets the caller verify e.g.
        "did the agent actually check news/macro before opening" objectively,
        rather than trusting the agent's own account of what it considered."""
        client = self._client()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        if not tools or max_tool_calls == 0:
            @retry_with_backoff
            def _call():
                return client.chat.completions.create(
                    model=self.model, messages=messages, temperature=0.0, max_tokens=4096
                )
            response = _call()
            return response.choices[0].message.content or ""

        formatted_tools = [{"type": "function", "function": t} for t in tools]

        call_count = 0
        tool_call_counts = {}
        while call_count < max_tool_calls:
            @retry_with_backoff
            def _call_tool():
                return client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=formatted_tools,
                    tool_choice="required",  # never allow a free-text final answer --
                    # a model that "concludes" by writing prose with the decision
                    # embedded in a markdown code fence (a real failure seen in
                    # production) is otherwise indistinguishable from one that
                    # crashed; forcing a tool call every turn means it must always
                    # call propose_trades to conclude, guaranteeing clean JSON.
                    temperature=0.0,
                    max_tokens=4096,
                    parallel_tool_calls=False,
                )
            response = _call_tool()
            message = response.choices[0].message
            messages.append(message)

            if not message.tool_calls:
                return message.content or ""

            tool_call = message.tool_calls[0]
            func_name = tool_call.function.name
            try:
                args = json.loads(tool_call.function.arguments)
            except Exception:
                args = {}

            if func_name == "propose_trades":
                return tool_call.function.arguments

            call_count += 1
            if tool_call_tracker is not None:
                tool_call_tracker.add(func_name)
            logger.info(f"Agent called {func_name}({args})")
            result = execute_tool_capped(func_name, args, tool_call_counts)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": func_name,
                "content": json.dumps(result, default=str),
            })

        logger.warning(f"Hit max tool calls ({max_tool_calls})")
        return ""


class AnthropicClient:
    """Same generate() interface as OpenAIClient (system prompt, user prompt,
    an OpenAI-style tools list, max_tool_calls, an optional tool_call_tracker)
    so agent_runner.py can swap providers without touching its own code.
    Shares execute_tool with OpenAIClient -- tool behavior can't drift
    between providers."""

    def __init__(self, model: str = "claude-sonnet-5"):
        self.model = model
        self.api_key = os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            logger.warning("ANTHROPIC_API_KEY not found. Agent will fail.")

    def _client(self):
        import anthropic
        return anthropic.Anthropic(api_key=self.api_key)

    @staticmethod
    def _to_anthropic_tools(tools: list) -> list:
        # OpenAI's function schemas use "parameters"; Anthropic's use
        # "input_schema" -- otherwise identical JSON Schema, so this is a
        # pure rename, not a re-derivation, keeping one source of truth
        # (agent_runner.TOOLS) for both providers.
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]

    def generate(self, system_prompt: str, user_prompt: str, tools: list, max_tool_calls: int = 10,
                 tool_call_tracker: set = None) -> str:
        client = self._client()
        messages = [{"role": "user", "content": user_prompt}]

        if not tools or max_tool_calls == 0:
            @retry_with_backoff
            def _call():
                return client.messages.create(
                    model=self.model, max_tokens=4096,
                    system=system_prompt, messages=messages,
                )
            response = _call()
            return "".join(b.text for b in response.content if b.type == "text")

        anthropic_tools = self._to_anthropic_tools(tools)

        call_count = 0
        tool_call_counts = {}
        while call_count < max_tool_calls:
            @retry_with_backoff
            def _call_tool():
                return client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    # No temperature param -- confirmed live in production that
                    # the anthropic package actually installed on the runner
                    # rejected it ("Messages.create() got an unexpected keyword
                    # argument 'temperature'") despite the latest SDK version
                    # supporting it when tested locally. Omitting it entirely
                    # (falls back to the API's own default) is the safe fix
                    # rather than chasing an exact version mismatch.
                    system=system_prompt,
                    messages=messages,
                    tools=anthropic_tools,
                    tool_choice={"type": "any"},  # never allow a free-text final answer --
                    # mirrors OpenAI's tool_choice="required": the model must always
                    # call propose_trades to conclude, guaranteeing clean JSON rather
                    # than a prose "conclusion" with the decision buried in a code fence.
                )
            response = _call_tool()

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_use_blocks:
                return "".join(b.text for b in response.content if b.type == "text")

            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            propose_trades_input = None
            for block in tool_use_blocks:
                if block.name == "propose_trades":
                    propose_trades_input = block.input
                    continue  # concluding call -- nothing to execute or respond to
                call_count += 1
                if tool_call_tracker is not None:
                    tool_call_tracker.add(block.name)
                logger.info(f"Agent called {block.name}({block.input})")
                result = execute_tool_capped(block.name, block.input, tool_call_counts)
                tool_results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })

            if propose_trades_input is not None:
                return json.dumps(propose_trades_input)

            if not tool_results:
                return ""

            messages.append({"role": "user", "content": tool_results})

        logger.warning(f"Hit max tool calls ({max_tool_calls})")
        return ""
