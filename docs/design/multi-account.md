# Multi-Account Setup

The bot supports failover across multiple Claude subscriptions. When the active
account hits its 5h or weekly limit, the runner automatically rotates to the
next account in `CLAUDE_ACCOUNTS`.

**Each account needs its own config directory** — Claude Code stores OAuth
credentials per `CLAUDE_CONFIG_DIR`, so two accounts cannot share `~/.claude`.

### One-time setup on a new machine

1. Pick a directory for the second account, e.g. `~/.claude-work`
2. Authenticate it (interactive — Claude can't do this for you):
   - **Bash/zsh**: `CLAUDE_CONFIG_DIR=~/.claude-work claude`
   - **PowerShell**: `$env:CLAUDE_CONFIG_DIR="$HOME/.claude-work"; claude`
   - **cmd.exe**: `set CLAUDE_CONFIG_DIR=%USERPROFILE%\.claude-work && claude`

   Then inside the CLI: `/login` → pick the second account.
3. Add both paths to `.env`:
   ```
   CLAUDE_ACCOUNTS=/home/you/.claude,/home/you/.claude-work
   ```
4. Restart the bot. Boot log should show:
   `Claude accounts configured: 2 (...)`
   A path that doesn't exist is dropped from rotation (ERROR per entry). A
   path that exists but isn't logged in is *sidelined*, not dropped: the
   runner skips it per-run and it rejoins automatically once signed in — no
   restart, no `.env` edit.

### Verify

- Check `data/logs/bot.log` for the startup line above
- `/auth` — per-account panel (identity, cooldown, re-login)
- `/status` — `**Accounts** — N/M usable`, naming any signed-out account
- Dashboard usage label shows `· N accts`, or `· N/M accts` when one is down
- The Ark gets one notice per outage, plus one all-clear on recovery
  (`bot/discord/account_alerts.py`); "Ignore for 7d" mutes it and lets it
  return once when the week is up
- Harnesses: `python scripts/test_account_failover.py`,
  `python scripts/test_account_alerts.py`

### Notes

- Order matters — first entry is the default. Put your primary account first.
- Sessions are pinned to the account that started them (`session_account` in
  `Instance`), so `--resume` always lands on the right account.
- Invalid entries are pruned at startup — `_pick_account()` only rotates among
  validated dirs, so a typo can't cause silent runtime failover failures.

### An account that comes and goes is parked, not removed

An organization admin can switch Claude Code off for a whole account and
switch it back on later. Nothing about that is an `.env` edit: the account
stays in `CLAUDE_ACCOUNTS` the entire time and the existing sideline
machinery carries it (park, one Ark notice, roughly daily retry, rejoin on
the first successful run). It was inert until 2026-09-15 for one reason,
and that reason is the thing to keep true:

- **The wording has to be matched by name.** "Your organization has disabled
  Claude subscription access for Claude Code · Use an Anthropic API key
  instead, or ask your admin to enable access" hit the klerk account 26 times
  from 12:06 on 2026-09-14 and matched none of `is_account_unusable_error`'s
  patterns, so `_pick_account` kept picking a dead subscription. The no-turns
  fallback that exists for unmatched wording structurally cannot cover this
  one: the CLI does not abort before turn 1 the way a 401 does, it reports the
  refusal as a *completed* turn (`num_turns=1`) whose result text **is** the
  error, so both halves of "produced nothing and took no turns" are false and
  nothing is even logged as unmatched.
- **A runtime rejection spelled as a compaction failure belongs to the account
  branch.** Compaction is an ordinary API call, so the refusal surfaces
  through the summariser as "Prompt is too long · automatic compaction failed:
  Your organization has disabled ...". That reads as a context problem, and
  the context-overflow branch sits *above* the account branch, so it spent
  both recovery rungs and then abandoned a transcript that compacts perfectly
  well. q-17514 and q-17515 were amputated that way at 04:13 on 2026-09-15.
  The overflow branch now stands down on `looks_like_fatal_auth_error`.
- **`REASON_ORG_DISABLED` exists to pick the right advice, not to add text.**
  Every other sideline is answered by signing in again; this one cannot be,
  because the account is signed in fine throughout. The Ark notice drops the
  "is signed out" opener, the `CLAUDE_CONFIG_DIR` command and the login
  button, says an admin has to re-enable access, and offers **Try now**
  instead. Three surfaces carry that advice and all three branch on the
  reason: the notice, the `/auth` panel (which reads the table through
  `StateStore.sidelined_account_reasons`, so a button it offers cannot
  contradict the notice that linked to it) and the failure card
  `runner._soften_auth_dead_end` writes when there is nowhere left to fail
  over to. `RUNTIME_REJECTION_REASONS` is what keeps both runtime verdicts
  out of the reach of the on-disk probe, which can never retire either: the
  rejected credentials file parses exactly like a working one.
- **"Try now" clears an auth cooldown and nothing else.**
  `runner.retry_account_now` drops the day-long `ACCOUNT_AUTH_COOLDOWN_SECS`
  sideline so the next task tries the account; if it is still blocked it is
  re-sidelined exactly as before. It refuses a *usage* cooldown, which ends on
  a real clock, and it reads the persisted alert table as well as the
  in-memory auth/usage split, because a reboot loses that split and would
  otherwise make a restarted auth sideline look like a usage limit for the
  rest of the day. Only a `RUNTIME_REJECTION_REASONS` alert counts as that
  durable evidence, and the button is only drawn for one: the probe reasons
  open an alert without ever arming a cooldown, so reading one as an auth
  sideline would force-clear a real usage limit that happened to be sitting
  behind it. The sole-account case arms no cooldown either (parking the only
  account we have would stop everything), so there the in-memory dead mark is
  the whole sideline and dropping it is the whole retry.
- **One panel renderer.** `/auth` and its Refresh button both go through
  `wizard._render_auth_panel`. They were near-copies and had already drifted:
  Refresh left out the sideline table, so refreshing turned a server-rejected
  account back into a green tick.

Harnesses: the org-disable cases live in `scripts/test_account_failover.py`
and `scripts/test_account_alerts.py`; the compaction-wrapped one lives in
`scripts/test_context_overflow.py`, next to the recovery it must not trigger.

### A login has a hard end date, and only a fresh sign-in moves it

The CLI keeps two tokens per account. The access token lasts 8 hours and the
CLI renews it on its own from the refresh token. The refresh token carries a
`refreshTokenExpiresAt` the server sets at sign-in, about four weeks out, and
**a refresh does not move it**: klerk renewed on 2026-10-25 and its date stayed
on 2026-11-12, and a default account left idle for 9 days renewed fine with its
date unchanged too. So there is no keep-alive: pinging an account every day
spends tokens and changes nothing. The only fix is signing in again before the
date, which is what the bot now prompts for.

- **The probe reads the date and sidelines past it, by the CLI's own rule.**
  `auth_health.unusable_reason` returns `REASON_LOGIN_EXPIRED` once the
  login's end date has passed **and** the access token the CLI already holds
  (`expiresAt`) has run out too. That is the two-field check the CLI's own
  login test makes: past the end date nothing can renew, but the token in
  hand keeps working for up to 8 hours, and sidelining on the end date alone
  would bench a working account for that long. The verdict is applied on
  read, not cached with the file, since the same untouched file flips when
  the clock passes. It is a probe reason, so it is reached wherever the probe
  runs (every account pick, and boot), and a `/login` (which rewrites the
  file) retires it like any other. A missing or malformed field reads as
  "unknown", never "expired".
- **One heads-up, keyed on the date.** `account_alerts._check_login_expiry`
  runs on the notifier's minute tick and posts one warning when a login ends
  within `LOGIN_EXPIRY_WARN_DAYS` (3), with the sign-in command and a login
  button. It stores the warned date in `login_expiry_warnings`, so it fires
  once per login, and a later date on the file is the evidence of a re-login:
  one confirmation line, then the record is dropped. A shift under an hour is
  the server re-stating the date, not a renewal. An account with an open
  sideline is skipped (it has its own notice), and a login that lapsed before
  it was renewed is dropped silently: if the account was sidelined meanwhile,
  the outage notice's all-clear says it is back, and if it never was, the
  user has just signed in themselves.
- **Only `CLAUDE_ACCOUNTS` entries are checked**, the same set the outage
  drain covers. With no rotation configured the CLI's default directory is
  not watched.
- **The bot never refreshes anything to learn the date.** It only reads the
  file the CLI wrote, for the reason in `docs/design/plan-usage.md`.
- `/auth` shows each account's end date, as a relative time with a warning
  sign once it is inside the warning window.

Harness: `scripts/test_account_alerts.py` (`login-expiry-probe`,
`login-expiry-warning`).
