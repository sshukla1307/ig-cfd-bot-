# Live CFD Trading Bot — IG Account

An autonomous GPT-4o agent (Aggressive persona) trading leveraged CFDs on a real IG account, focused exclusively on 4 instruments: **Brent Crude Oil, WTI Crude Oil, Natural Gas, and Palladium**. Checks in roughly every 5 minutes whenever at least one of those markets is open. No paper simulation — every trade executes with real, leveraged capital, subject to a hard-coded rules firewall and two independent kill switches.

## ⚠️ Status: NOT YET VERIFIED AGAINST A REAL IG ACCOUNT

This is built against:
- The `trading_ig` community library's actual source code (method signatures, DataFrame column names — inspected directly, not assumed from memory).
- IG's publicly documented REST endpoint shapes.

It has **not** been run against a live or demo IG account yet. Before `IG_LIVE_TRADING_ENABLED` is ever set to `"true"` against `IG_LIVE=true`, you must:

1. **Resolve the 4 instrument epics.** `config.py`'s `INSTRUMENTS` dict has empty `epic` placeholders on purpose — guessing epic strings risks silently trading the wrong contract. Resolve them yourself:
   ```python
   from trading_ig import IGService
   ig = IGService(username, password, api_key, acc_type="demo")
   ig.create_session()
   print(ig.search_markets("Brent Crude"))
   print(ig.search_markets("WTI Crude Oil"))
   print(ig.search_markets("Natural Gas"))
   print(ig.search_markets("Palladium"))
   ```
   Copy the correct `epic` field from each result into `config.py`.
2. **Test against IG's demo environment first** (`IG_LIVE=false`). Manually open one small test position and confirm the actual margin used (via `get_account_state()`'s `deposit` field) is in the ballpark the sizing math in `cfd_runner._compute_position_size` predicts — that formula uses IG's own `marginFactor`/`lotSize` fields, which is the documented mechanism, but has never been checked against a real fill.
3. Only then consider `IG_LIVE=true`.

## How it works

Every 5 minutes (Sun–Fri, `.github/workflows/cfd_trading.yml`) runs `python -m cfd_runner`, which:

1. Connects to IG, fetches account state and the bot's 4 tracked positions.
2. Checks each instrument's **live market status** directly from IG (not a hardcoded calendar) — commodity CFDs follow underlying futures session hours plus daily maintenance windows and weekend closures, unlike 24/5 forex. Any instrument not `TRADEABLE` right now is skipped.
3. Blocks **all** new position opens account-wide if available margin has fallen below the safety buffer (default 30% of balance).
4. Lets the agent decide (or hold) per instrument, using technicals (yfinance continuous futures), commodity-specific news (Brave), and macro context (FRED dollar index/VIX/10Y yield).
5. Validates and executes any accepted trade immediately, then re-exports the dashboard.

## "Code is Law" — the rules firewall

`cfd_runner.py` enforces these regardless of what the agent wants:

- **Sizing**: 5–25% of account equity allocated as margin per position.
- **Hard leverage cap**: 5x notional exposure per unit of margin allocated — enforced independent of whatever leverage IG's own margin factor for the instrument would otherwise permit.
- **Absolute notional safety ceiling**: no single position's notional exposure exceeds a fixed dollar ceiling, regardless of the leverage math above — a backstop in case that formula is ever wrong.
- **Mandatory stop-loss AND take-profit** on every opening trade.
- **Margin safety buffer**: new opens blocked account-wide if available margin drops below 30% of balance.
- **Max 4 concurrent positions** — one per instrument, matching the 4-instrument universe.
- **Universe**: exactly 4 instruments. Nothing else is tradable, by design — no watchlist scanning.

## Two independent safety switches

Both must be explicitly `"true"` — set only inside `cfd_trading.yml`, never anywhere else:

- `IG_LIVE_TRADING_ENABLED` — master kill switch. Off by default.
- `IG_LIVE` — live vs. demo IG environment. Defaults to demo.

## Project structure

- `cfd_runner.py` — the tick orchestrator: market-status gating, margin safety check, validation, execution.
- `ig_broker.py` — `trading_ig` wrapper (account state, positions, market snapshots, order submission).
- `agent_runner.py` — prompt-building and tool schemas for the LLM decision loop.
- `api_adapters.py` — OpenAI client with the tool-calling loop.
- `market_data.py` — technicals (yfinance), news (Brave), macro (FRED) for the 4 instruments.
- `meta_strategy.py` — one-time Day-0 playbook generation.
- `dashboard_exporter.py` — audit trail → dashboard JSON.

## Setup

**Secrets** (GitHub repo → Settings → Secrets and variables → Actions): `IG_USERNAME`, `IG_PASSWORD`, `IG_API_KEY`, `OPENAI_API_KEY`, `BRAVE_API_KEY`, `FRED_API_KEY`.

**One-time**: resolve the 4 epics (see above), then generate the Day-0 playbook:
```bash
python meta_strategy.py
```

**Manual trigger**: Actions tab → "LIVE CFD Trading Loop (Real Capital)" → Run workflow → type the confirmation phrase.

## Dashboard

`index.html` reads `data/dashboard/*.json` and auto-refreshes every 5 minutes. If hosted via GitHub Pages, the same caveat as any public trading bot applies: Pages on the free plan requires a public repo, and a public repo means the full trade/equity history is visible to anyone via the committed JSON/JSONL files, not just the dashboard view of them.

## License
MIT
