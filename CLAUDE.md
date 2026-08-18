# Claude Code Bot

Discord bot for managing Claude Code instances remotely.

## Quick Start

```bash
python -m bot          # start the bot
```

## Key Paths

- **Entry point**: `bot/__main__.py` -> `bot/app.py:run()`
- **Config**: `bot/config.py` (reads `.env`)
- **Log file**: `data/logs/bot.log`
- **State**: `data/state.json`
- **Engine** (platform-agnostic): `bot/engine/commands.py`, `lifecycle.py`, `workflows.py`, `sessions.py`, `eval.py`, `report.py`
- **Platform layer**: `bot/platform/base.py` (Messenger protocol), `bot/platform/formatting.py`
- **Discord**: `bot/discord/bot.py` (orchestrator), `slash_commands.py`, `interactions.py`, `adapter.py`, `channels.py`, `forums.py`, `idle.py`, `tags.py`, `modals.py`, `monitoring.py`, `formatter.py`

## Discord Limits

- Max 5 button rows per View (truncate, don't crash)
- 2000 char regular message limit, 4096 for embed descriptions
- Slash commands are guild-synced (instant registration)
- 3-second interaction timeout — always `defer()` first
- `intents.members = True` needed for permission overwrites on category creation

## Discord Architecture (v0.3.0)

Forum-based: one ForumChannel per project/repo, one thread per session.
- Bot auto-provisions private category + The Ark (top-level dashboard channel) on startup
- Messages in The Ark → informational reply only (no session routing)
- Messages in forum thread → session auto-resumed
- Dashboard embed pinned in The Ark (auto-updates on instance start/complete)
- Per-repo control rooms live as pinned threads inside each repo's forum
- Forum tags: active, completed, failed, cli, build

Key data structures in `bot/discord/forums.py`:
- `ForumProject`: repo_name + forum_channel_id + threads dict
- `ThreadInfo`: thread_id + session_id + origin + topic
- Persisted in `data/state.json` under `platform_state.discord.forum_projects`

## Build Isolation (Git Worktrees)

Build tasks use git worktrees for parallel isolation:
- Each build creates a worktree at `{repo}/.worktrees/{instance-id}/`
- Main repo always stays on master — no `git checkout` in the shared directory
- Parallel builds on the same repo work without conflicts
- Session files are copied between main repo and worktree project directories so `--resume` works
- Per-repo asyncio lock serializes git admin operations (worktree add/remove, merge, branch delete)
- After Done/Commit → Merge/Discard buttons appear in the thread
- Autopilot auto-merges after a successful chain completes
- `/branches` scans for orphaned branches and worktree directories

## Spawn-Wave Join (`bot/discord/orchestrator.py`)

When a session fans work out with `/spawn`, the bot joins the whole wave back
to the parent instead of making the user do it.

- Child state is **derived**, never stored: `ThreadInfo.session_id` -> newest
  `Instance` for that session -> status + `needs_input`. A child that parked on
  a question is `blocked`, not `completed` (finalize marks both COMPLETED).
- The wave roster is `Instance.spawn_dispatched_thread_ids`, sealed with
  `spawn_wave_sealed` when the dispatch loop ends. **A wave is not joinable
  before it is sealed** — otherwise a fast-failing first child closes the wave
  while its siblings are still being created.
- A child's callback resolves the wave whose roster **contains that child**, not
  the newest wave — a parent can have two waves open at once (wave 1 resumes it,
  it dispatches wave 2).
- On close, the parent's resume prompt carries each child's **full report file
  path** (`Instance.result_file`), not an excerpt. The human-facing post gets
  the excerpts.
- Full wave -> parent auto-resumes (`ORCH_AUTO_RESUME`, default on), bounded by
  the existing 12-wave cap since `callback_resume` doesn't reset it. Partial or
  timed-out release -> manual "Resume parent" button.
- Sweep in `autonomy_loop` (every ~5 min): partial-releases a wave past
  `ORCH_WAVE_TIMEOUT_MIN` (default 45; `0` = wait forever), closes a
  fully-settled wave early (a killed child never calls back), and silently
  *retires* any wave older than `_WAVE_ABANDON_HOURS` (12) — which is what
  absorbs waves recorded before this feature existed.
- Blocked-child wake-ups are budgeted at `_MAX_BLOCKED_RESUMES` (4) per wave
  (`Instance.spawn_blocked_resumes`) — parent answers, child asks again, repeat.
- `[BOT_CMD: /reply thread=<id>]` + `~~~reply` body lets a parent answer its own
  blocked child. Target must be in this session's own dispatched ids.
- Harness: `python scripts/test_orchestrator_join.py`

## Computational Sensors (`.claude/sensors.json`)

Chains run a deterministic sensor step (build → **sensors** → review_code → …)
that executes fast checks in the build worktree and feeds raw tool output back
to the build session for self-fixing (`bot/engine/sensors.py`).

- Auto-detection per stack: `dotnet build` (C#), ruff critical-errors-only
  (Python, only if installed — syntax errors/undefined names, not style),
  `npx tsc --noEmit` (tsconfig present). No stack/tools → step skips silently.
- Per-repo override in the main repo (not the worktree), replaces auto-detect:
  ```json
  {
    "sensors": [
      {"name": "ruff", "command": "ruff check .", "blocking": true, "timeout_s": 180}
    ],
    "policy": "block",
    "max_fix_rounds": 2
  }
  ```
- `policy`: `block` (default — persistent failures halt the chain via
  needs_input, like verify-fail) or `warn` (post failures, advance anyway).
- Sits alongside the other per-repo config files: `.claude/test.json`
  (verify policy + diagnostics) and `.claude/workflow.json` (merge autonomy).

## Multi-Account Setup

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

## Versioning

See `~/.claude/CLAUDE.md` for universal versioning conventions.
Version source: `pyproject.toml`

## Testing

### Discord integration test tool

```bash
python scripts/discord_test.py <command>
```

**Setup (one-time):**
1. Create Ark webhook: `python scripts/discord_test.py setup-webhook <ark_channel_id>`
   → Add URL to `TEST_LOBBY_WEBHOOK_URL` in `.env`
2. Create forum webhook: `python scripts/discord_test.py setup-webhook <forum_channel_id>`
   → Add URL to `TEST_WEBHOOK_URL` in `.env`
3. Add both webhook IDs to `TEST_WEBHOOK_IDS` (comma-separated) in `.env`
4. Restart bot

**Commands:**
- `list-channels` — show all channels in bot category (verify forums exist)
- `list-threads <forum_id>` — show active/archived threads + tags
- `channel-info <id>` — channel type, parent, tags, archive status
- `send <channel_or_thread_id> <msg>` — send via webhook (auto-picks Ark vs forum webhook)
- `read <channel_or_thread_id> [limit]` — read messages with embeds/buttons
- `wait-response <channel_id> [timeout]` — poll for bot response after sending
- `run-suite` — automated test sequence (forum creation, thread resume, archived resume, dedup, tags)

**Quick verification after changes:**
```bash
python scripts/discord_test.py list-channels          # forums exist?
python scripts/discord_test.py list-threads <forum_id> # threads created?
python scripts/discord_test.py read <thread_id> 5      # bot responding?
```

### Read Discord messages (lightweight)

```bash
python scripts/discord_read.py [channel_id] [limit]   # default: The Ark, 10
```

### Manual verification

- `/sync 3` → threads created per project with history
- `/new` → fresh thread in project forum
- `/repo` → select menu dropdown (with 2+ repos)
- Workflow buttons (Plan/Build/Review/Commit) work inside forum threads
- Send message in The Ark → informational reply (no routing)
- Send message in archived thread → auto-unarchives + resumes session

### Log monitoring

Always tail logs when debugging or testing:
```bash
tail -f data/logs/bot.log        # real-time (run in background)
tail -n 50 data/logs/bot.log     # recent entries
```
