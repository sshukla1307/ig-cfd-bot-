/**
 * Fires a `repository_dispatch` event at the ig-cfd-bot- GitHub Actions
 * workflow every 3 minutes. GitHub's own `schedule:` cron trigger is not
 * reliable below ~5-minute intervals (silently delayed under load), so this
 * Worker's Cron Trigger is the real source of the bot's 3-minute cadence --
 * a direct API dispatch starts almost immediately, unlike the internal
 * schedule queue.
 *
 * Requires a Worker secret named GITHUB_TOKEN: a GitHub Personal Access
 * Token (classic, "repo" scope, or fine-grained with this repo's
 * "Contents: write" + "Actions: write" permissions) -- set via:
 *   wrangler secret put GITHUB_TOKEN
 * Never hardcode the token in this file or in wrangler.toml.
 */

const REPO_OWNER = "sshukla1307";
const REPO_NAME = "ig-cfd-bot-";
const EVENT_TYPE = "cfd-tick";

export default {
  async scheduled(event, env, ctx) {
    const res = await fetch(
      `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "ig-cfd-bot-cron-worker",
        },
        body: JSON.stringify({ event_type: EVENT_TYPE }),
      }
    );

    if (!res.ok) {
      const body = await res.text();
      console.error(`repository_dispatch failed: ${res.status} ${body}`);
    } else {
      console.log(`repository_dispatch sent (${res.status})`);
    }
  },
};
