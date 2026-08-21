"""
IG CFD Trading Bot — LLM Adapter

Single provider (OpenAI, gpt-4o), forced temperature=0. Runs a tool-calling
loop until the agent calls propose_trades, same battle-tested pattern as the
Alpaca project's UnifiedLLMClient.
"""

import json
import os
import random
import time
import logging
from typing import Callable

logger = logging.getLogger(__name__)


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

    def generate(self, system_prompt: str, user_prompt: str, tools: list, max_tool_calls: int = 10) -> str:
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
        from market_data import get_technicals, get_commodity_news, get_macro
        from config import YFINANCE_TICKERS

        def execute_tool(name: str, args: dict) -> dict:
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
            return {"error": f"Unknown tool: {name}"}

        call_count = 0
        while call_count < max_tool_calls:
            @retry_with_backoff
            def _call_tool():
                return client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=formatted_tools,
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
            logger.info(f"Agent called {func_name}({args})")
            result = execute_tool(func_name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "name": func_name,
                "content": json.dumps(result, default=str),
            })

        logger.warning(f"Hit max tool calls ({max_tool_calls})")
        return ""
