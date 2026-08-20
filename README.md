# Live CFD Trading Bot — IG Account

An autonomous GPT-4o agent (Aggressive persona) trading leveraged CFDs on a real IG account, focused exclusively on 3 instruments: **Brent Crude Oil, WTI Crude Oil, and Natural Gas**. Checks in roughly every 3 minutes whenever at least one of those markets is open. No paper simulation — every trade executes with real, leveraged capital, subject to a hard-coded rules firewall and two independent kill switches.

**Trading style**: both directions are treated as equally valid — the agent is explicitly told OPEN_SHORT is not a fallback, just as actionable as OPEN_LONG on a high-conviction bearish read. The house style favors fast, frequent, smaller profit-taking over holding out for a large swing — the agent is encouraged to proactively CLOSE a position once satisfied with a gain rather than only waiting on the passive take-profit level.

**Palladium was deliberately dropped from the universe**: this IG account has no rolling/perpetual Palladium CFD, only dated futures-tracking contracts (e.g. Sep-26, Dec-26), and this bot has no expiry-rollover logic. Trading a dated contract unattended risks holding a position into expiry with nothing to catch it. Revisit if rollover support gets built, or if a rolling contract becomes available.

## ⚠️ Status: connection verified, margin math still unverified against a real fill

Confirmed working (2026-08-20, against IG demo account `SNDPM`):
- IG session/login via `trading_ig`.
- The 3 instrument epics in `config.py`, resolved via a live `search_markets()` call.

**Not yet confirmed**: the margin/leverage sizing math in `cfd_runner._compute_position_size` uses IG's own `marginFactor`/`lotSize` fields (the documented mechanism), but has never been checked against a real fill. Before `IG_LIVE_TRADING_ENABLED` is ever set to `"true"` against `IG_LIVE=true`:

1. **Fund the demo account.** As of the last check, `SNDPM`'s balance and available margin were both $0.00 — reset/top up its virtual balance in IG's platform before any test trade can actually fill.
2. **Open one small test position on demo** and confirm the actual margin used (via `get_account_state()`'s `deposit` field) is in the ballpark `_compute_position_size` predicts for the `allocation_pct` you used — do not trust the formula blindly.
3. Only then consider `IG_LIVE=true`.

## How it works

Every ~3 minutes (Sun–Fri, `.github/workflows/cfd_trading.yml` — nominal target; GitHub Actions cron doesn't guarantee sub-5-minute precision) runs `python -m cfd_runner`, which:

1. Connects to IG, fetches account state and the bot's 3 tracked positions.
2. Checks each instrument's **live market status** directly from IG (not a hardcoded calendar) — commodity CFDs follow underlying futures session hours plus daily maintenance windows and weekend closures, unlike 24/5 forex. Any instrument not `TRADEABLE` right now is skipped.
3. Blocks **all** new position opens account-wide if available margin has fallen below the safety buffer (default 30% of balance) — tracked as a *running* total across every trade processed in the same tick, not just a single stale pre-tick snapshot, so several individually-fine-looking opens in one tick can't collectively breach it.
4. Lets the agent decide (or hold) per instrument, using technicals (yfinance continuous futures), commodity-specific news (Brave), and macro context (FRED dollar index/VIX/10Y yield).
5. Validates and executes any accepted trade immediately, then re-exports the dashboard.

## "Code is Law" — the rules firewall

`cfd_runner.py` enforces these regardless of what the agent wants:

- **Sizing**: 5–25% of account equity allocated as margin per position.
- **Hard leverage cap**: 5x notional exposure per unit of margin allocated — enforced independent of whatever leverage IG's own margin factor for the instrument would otherwise permit.
- **Absolute notional safety ceiling**: no single position's notional exposure exceeds a fixed dollar ceiling, regardless of the leverage math above — a backstop in case that formula is ever wrong.
- **Mandatory stop-loss AND take-profit** on every opening trade.
- **Margin safety buffer**: new opens blocked account-wide if available margin drops below 30% of balance — checked both against current state AND against what each specific trade's own margin requirement would leave behind, tracked across every trade in a tick, not just a single stale snapshot.
- **Max 3 concurrent positions** — one per instrument, matching the 3-instrument universe.
- **Universe**: exactly 3 instruments (Brent Crude, WTI Crude, Natural Gas). Nothing else is tradable, by design — no watchlist scanning.

## Two independent safety switches

Both must be explicitly `"true"` — set only inside `cfd_trading.yml`, never anywhere else:

- `IG_LIVE_TRADING_ENABLED` — master kill switch. Off by default.
- `IG_LIVE` — live vs. demo IG environment. Defaults to demo.

## Project structure

- `cfd_runner.py` — the tick orchestrator: market-status gating, margin safety check, validation, execution.
- `ig_broker.py` — `trading_ig` wrapper (account state, positions, market snapshots, order submission).
- `agent_runner.py` — prompt-building and tool schemas for the LLM decision loop.
- `api_adapters.py` — OpenAI client with the tool-calling loop.
- `market_data.py` — technicals (yfinance), news (Brave), macro (FRED) for the 3 instruments.
- `meta_strategy.py` — one-time Day-0 playbook generation.
- `dashboard_exporter.py` — audit trail → dashboard JSON.

## Setup

**Secrets** (GitHub repo → Settings → Secrets and variables → Actions): `IG_USERNAME`, `IG_PASSWORD`, `IG_API_KEY`, `OPENAI_API_KEY`, `BRAVE_API_KEY`, `FRED_API_KEY`.

**One-time**: generate the Day-0 playbook (epics are already resolved in `config.py`):
```bash
python meta_strategy.py
```

**Manual trigger**: Actions tab → "LIVE CFD Trading Loop (Real Capital)" → Run workflow → type the confirmation phrase.

## Dashboard

`index.html` reads `data/dashboard/*.json` and auto-refreshes every ~3 minutes. If hosted via GitHub Pages, the same caveat as any public trading bot applies: Pages on the free plan requires a public repo, and a public repo means the full trade/equity history is visible to anyone via the committed JSON/JSONL files, not just the dashboard view of them.

## License
MIT
