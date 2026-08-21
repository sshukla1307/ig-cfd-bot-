# CFD Bot Ticker — Cloudflare Worker

Fires the `ig-cfd-bot-` GitHub Actions workflow every 3 minutes via
`repository_dispatch`, since GitHub's own `schedule:` cron is unreliable
below ~5-minute intervals. This Worker is the real source of the bot's
3-minute cadence; `cfd_trading.yml`'s `schedule:` trigger stays only as a
free fallback in case this Worker ever goes down.

## One-time setup (do this yourself — do not hand the token to anyone else)

1. **Generate a GitHub token**: GitHub → Settings → Developer settings →
   Personal access tokens. Either a classic token with `repo` scope, or a
   fine-grained token scoped to just `ig-cfd-bot-` with `Contents: write`
   and `Actions: write` permissions (fine-grained is the smaller blast
   radius — prefer it).

2. **Install Wrangler** (Cloudflare's CLI) if you don't have it:
   ```bash
   npm install -g wrangler
   wrangler login
   ```

3. **From this directory**, set the token as a Worker secret (Wrangler
   prompts for the value — it is never written to a file):
   ```bash
   cd cloudflare-worker
   wrangler secret put GITHUB_TOKEN
   ```

4. **Deploy**:
   ```bash
   wrangler deploy
   ```

That's it — Cloudflare's Cron Triggers run server-side; nothing needs to
stay running on your machine.

## Verifying it's working

- Cloudflare dashboard → Workers & Pages → `ig-cfd-bot-ticker` → Logs, to
  see each dispatch attempt.
- GitHub → `ig-cfd-bot-` → Actions tab: new runs should appear roughly every
  3 minutes with event type `repository_dispatch`.

## Turning it off

`wrangler delete` removes the Worker (stops all dispatches immediately).
To pause without deleting, remove the `[triggers]` block from
`wrangler.toml` and redeploy, or disable the Worker's Cron Trigger from the
Cloudflare dashboard directly.
