# Live CFD Trading Bot — IG Account

An autonomous LLM agent (Aggressive persona) trading leveraged CFDs on a real IG account, focused exclusively on 3 instruments: **Brent Crude Oil, WTI Crude Oil, and Natural Gas**. Currently running on Claude Sonnet 5 (`config.LLM_PROVIDER`, switched from GPT-4o to see whether behavioral drift across the large system prompt improves — both `OpenAIClient` and `AnthropicClient` exist in `api_adapters.py` and share the exact same tool dispatcher, so switching back is a one-line config change). Checks in roughly every 5 minutes whenever at least one of those markets is open. No paper simulation — every trade executes with real, leveraged capital, subject to a hard-coded rules firewall and two independent kill switches.

**Trading style**: both directions are treated as equally valid — the agent is explicitly told OPEN_SHORT is not a fallback, just as actionable as OPEN_LONG on a high-conviction bearish read. The house style favors fast, frequent, smaller profit-taking over holding out for a large swing — the agent is encouraged to proactively CLOSE a position once satisfied with a gain rather than only waiting on the passive take-profit level.

**Palladium was deliberately dropped from the universe**: this IG account has no rolling/perpetual Palladium CFD, only dated futures-tracking contracts (e.g. Sep-26, Dec-26), and this bot has no expiry-rollover logic. Trading a dated contract unattended risks holding a position into expiry with nothing to catch it. Revisit if rollover support gets built, or if a rolling contract becomes available.

## ⚠️ Status: live and trading real money — win rate is the open question, not plumbing

Confirmed working since 2026-08-20: IG session/login, epic resolution, real order submission/closing, margin sizing against real fills, the full agent decision loop. This is no longer a "is it wired up correctly" question — the bot has executed dozens of real trades.

**What's actually being worked on now is signal quality.** Early trades (49 closed) showed a ~37% win rate despite a properly-configured 2:1 reward:risk (which needs ~60% to break even) — RSI/SMA-crossover signals alone, re-evaluated every 5 minutes on some of the most liquid futures markets in the world, aren't producing durable edge. Several rounds of fixes have shipped in response: a confluence requirement (blocks opening on a technical signal alone), a same-direction cooldown (blocks immediately re-opening a just-disproven thesis), and new signal sources — seasonality, term structure, CFTC speculator positioning, and real NOAA weather demand data — intended to give the agent something more differentiated than spot RSI. (A planned real-EIA-inventory-data source turned out not to exist on FRED and is currently disabled pending a switch to EIA's own API.) Whether any of this actually moves the win rate is still being observed against live trades, not backtested in advance.

## How it works

Every ~5 minutes (Sun–Fri, `.github/workflows/cfd_trading.yml` — nominal target; GitHub Actions cron doesn't guarantee precise timing) runs `python -m cfd_runner`, which:

1. Connects to IG, fetches account state and the bot's 3 tracked positions.
2. **Stop-breach backstop, runs before anything else**: for every open position, compares the CURRENT live price against that position's own recorded stop level (not IG's system-side stop, which is non-guaranteed and can slip in a fast move or gap). If price has already moved past the stop — or a stop is somehow missing entirely — force-closes it at market immediately, rather than waiting out the ~5-minute gap until the next check-in on the assumption IG's stop already fired.
3. Checks each instrument's **live market status** directly from IG (not a hardcoded calendar) — commodity CFDs follow underlying futures session hours plus daily maintenance windows and weekend closures, unlike 24/5 forex. Any instrument not `TRADEABLE` right now is skipped.
4. Blocks **all** new position opens account-wide if available margin has fallen below the safety buffer (default 30% of balance) — tracked as a *running* total across every trade processed in the same tick, not just a single stale pre-tick snapshot, so several individually-fine-looking opens in one tick can't collectively breach it. Also checked precisely per-trade: if that specific trade's own margin requirement would tip the running total under the buffer, it's rejected even if the tick started out healthy.
5. Lets the agent decide (or hold) per instrument, using technicals (yfinance continuous futures), commodity-specific news (Brave), macro context (FRED dollar index/VIX/10Y yield), calendar-based seasonality, futures term structure (contango/backwardation), CFTC speculator positioning data (WTI/Natural Gas only), and real NOAA weather demand data (Natural Gas only).
6. Validates and executes any accepted trade immediately (including atomic two-leg Brent-WTI spread trades), then re-exports the dashboard.

## "Code is Law" — the rules firewall

`cfd_runner.py` enforces these regardless of what the agent wants:

- **Stop-breach backstop**: every tick, before the agent gets a turn, live price is checked against each open position's own stop level — if breached (or missing), force-closed immediately rather than trusting IG's non-guaranteed system stop to have already handled it.
- **Sizing**: 5–25% of account equity allocated as margin per position.
- **Hard leverage cap**: 5x notional exposure per unit of margin allocated — enforced independent of whatever leverage IG's own margin factor for the instrument would otherwise permit.
- **Absolute notional safety ceiling**: no single position's notional exposure exceeds a fixed dollar ceiling, regardless of the leverage math above — a backstop in case that formula is ever wrong.
- **Mandatory stop-loss AND take-profit** on every opening trade (each leg, for spreads too).
- **Margin safety buffer**: new opens blocked account-wide if available margin drops below 30% of balance — checked both against current state AND against what each specific trade's own margin requirement would leave behind, tracked across every trade in a tick, not just a single stale snapshot.
- **Confluence requirement**: opening a position on a technical signal alone is blocked — the agent must have also checked news, macro, seasonality, term structure, or positioning data this tick (verified objectively via which tools it actually called, not just trusted).
- **Same-direction cooldown**: re-opening the same direction on an instrument is blocked for 60 minutes after a losing close there, to stop immediately re-entering a thesis that just failed.
- **Spread rollback safety**: `OPEN_SPREAD` (long one of Brent/WTI, short the other) opens atomically — if the second leg fails after the first succeeds, the first is immediately closed back out rather than left as an unintended naked position.
- **Max 3 concurrent positions** — one per instrument, matching the 3-instrument universe (a spread uses 2 of the 3 slots).
- **Universe**: exactly 3 instruments (Brent Crude, WTI Crude, Natural Gas). Nothing else is tradable, by design — no watchlist scanning.

## On tick cadence

GitHub's own `schedule:` cron is not perfectly reliable (can be delayed under load, confirmed in practice at sub-5-minute intervals), so treat 5 minutes as a nominal target, not a hard guarantee — actual spacing may occasionally drift wider. A true, precise 3-minute cadence would require an external pinger (e.g. a Cloudflare Worker Cron Trigger hitting the GitHub API directly) instead of relying on GitHub's internal scheduler, but that path was dropped in favor of keeping the setup simple — 5 minutes on GitHub's own scheduler is an acceptable tradeoff.

## Two independent safety switches

Both must be explicitly `"true"` — set only inside `cfd_trading.yml`, never anywhere else:

- `IG_LIVE_TRADING_ENABLED` — master kill switch. Off by default.
- `IG_LIVE` — live vs. demo IG environment. Defaults to demo.

## Project structure

- `cfd_runner.py` — the tick orchestrator: market-status gating, margin safety check, validation, execution.
- `ig_broker.py` — `trading_ig` wrapper (account state, positions, market snapshots, order submission).
- `agent_runner.py` — prompt-building and tool schemas for the LLM decision loop.
- `api_adapters.py` — OpenAI and Anthropic clients, both with the same tool-calling loop interface (`config.LLM_PROVIDER` picks which one runs).
- `market_data.py` — technicals (yfinance), news (Brave), macro (FRED), seasonality (deterministic calendar-based), term structure (yfinance dated contracts), CFTC speculator positioning data, and NOAA weather demand data (Natural Gas) for the 3 instruments.
- `meta_strategy.py` — one-time Day-0 playbook generation.
- `dashboard_exporter.py` — audit trail → dashboard JSON.

## Setup

**Secrets** (GitHub repo → Settings → Secrets and variables → Actions): `IG_USERNAME`, `IG_PASSWORD`, `IG_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `BRAVE_API_KEY`, `FRED_API_KEY`. Both LLM keys are wired in regardless of which one `LLM_PROVIDER` is actively using, so switching providers never needs a workflow change.

**One-time**: generate the Day-0 playbook (epics are already resolved in `config.py`):
```bash
python meta_strategy.py
```

**Manual trigger**: Actions tab → "LIVE CFD Trading Loop (Real Capital)" → Run workflow → type the confirmation phrase.

## Dashboard

`index.html` reads `data/dashboard/*.json` and auto-refreshes every ~5 minutes. If hosted via GitHub Pages, the same caveat as any public trading bot applies: Pages on the free plan requires a public repo, and a public repo means the full trade/equity history is visible to anyone via the committed JSON/JSONL files, not just the dashboard view of them.

## License
MIT
