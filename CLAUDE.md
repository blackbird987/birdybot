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
   If a path is wrong or not logged in, the bot drops it from rotation and
   logs an ERROR per dropped entry.

### Verify

- Check `data/logs/bot.log` for the startup line above
- Dashboard footer shows `· N accts` when N > 1 (`bot/discord/dashboard.py:221`)

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
