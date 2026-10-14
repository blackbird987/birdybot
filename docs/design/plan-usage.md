# Usage figures come from Anthropic, not from our own arithmetic

The usage bar on the dashboard and in every Control Room used to be a ccusage
dollar total divided by a limit the bot guessed. On 2026-10-09 it read "93%
est" for a session Anthropic had at 15%, and it had no weekly percentage at
all, which is the number that actually runs out. `bot/engine/plan_usage.py`
now reads the real figures per account, from the endpoint Claude Code's own
`/usage` screen calls (`GET https://api.anthropic.com/api/oauth/usage`, bearer
`claudeAiOauth.accessToken` from the account's `.credentials.json`, header
`anthropic-beta: oauth-2025-04-20`). ccusage stays as the fallback and as the
labelled cost section of `/usage`.

Four things that must not drift:

- **The bot never refreshes a token.** The refresh token rotates on use and
  the CLI owns it; a refresh from here would invalidate the CLI's copy and sign
  the account out. An expired access token is reported as expired, the CLI
  refreshes it on that account's next run, and the cache is keyed on the
  credentials file's (mtime, size) so the new token is picked up at once. The
  token is only ever sent to `api.anthropic.com`.
- **The endpoint is undocumented, so every key is optional.** The `limits`
  list (`session`, `weekly_all`, `weekly_scoped`) is preferred, the older
  `five_hour` / `seven_day` fields fill whatever it did not carry, and a
  response with neither is an error rather than zeros. A shape change costs
  the real bar, never the field: no figures from any account means the ccusage
  bar is drawn exactly as before.
- **It is per account, because the limits are.** Each subscription in
  `CLAUDE_ACCOUNTS` has its own session and weekly allowance, and the one that
  matters is whichever the runner is about to pick. A signed-out account is a
  one-line row and stays in rotation: an account that is only paid for some
  months is still a configured account. Do not "fix" that row by pruning it.
- **Failures are states, never exceptions, and are cached.** The bar redraws
  on every instance start and completion. A good reading lives
  `PLAN_USAGE_TTL_SECS` (60), a failure five minutes, a failed refresh serves
  the last good reading (up to 3h old) marked with its age, and concurrent
  redraws share one request through a per-account lock.

Knobs: `PLAN_USAGE_API_ENABLED`, `PLAN_USAGE_TTL_SECS` in `bot/config.py`.

Harness: `python scripts/test_plan_usage.py` (`--live [config_dir ...]` reads
the real accounts and prints each one's state and figures, never the token).
