# Claude Telegram Bot

Control Claude Code CLI from your phone via Telegram. Run queries, background tasks, plan/build workflows, manage git branches — all from a mobile chat.

## Prerequisites

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (`claude` command works)
- Claude Max or Pro plan (the bot spawns CLI instances)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Your Telegram user ID (from [@userinfobot](https://t.me/userinfobot))

## Install

```bash
git clone https://github.com/YOUR_USERNAME/claude-telegram-bot.git
cd claude-telegram-bot
pip install -e .
```

## Configure

```bash
cp .env.example .env
```

Edit `.env` with your bot token and user ID:

```
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_USER_ID=12345678
```

## Run

```bash
claude-bot
# or
python -m bot
```

## Commands

**Chat** — send any text to query Claude (auto-resumes the active session)

| Command | Description |
|---------|-------------|
| `/new` | Start fresh conversation (clears chat) |
| `/bg <prompt>` | Background build task (auto-branch) |
| `/list` | Show recent instances |
| `/kill <id>` | Terminate running instance |
| `/retry <id>` | Re-run instance |
| `/log <id>` | Full output file |
| `/diff <id>` | Git diff from build task |
| `/merge <id>` | Merge build branch |
| `/discard <id>` | Delete build branch |
| `/session` | List/resume desktop CLI sessions |
| `/mode explore\|build` | Switch permission mode |
| `/verbose 0\|1\|2` | Progress detail level |
| `/context set <text>` | Pin context to all prompts |
| `/repo add\|switch\|list` | Manage repos |
| `/alias set\|list\|delete` | Command shortcuts |
| `/schedule every\|at\|list\|delete` | Recurring tasks |
| `/cost` | Spending breakdown |
| `/status` | Health dashboard |
| `/budget` | Budget info/reset |
| `/shutdown` | Stop the bot (switch PCs) |

## Workflow Buttons

Completed queries show contextual action buttons:

- **Plan** — create an implementation plan (explore mode)
- **Build It** — implement directly (build mode, auto-branch)
- **Review Plan** / **Review Code** — iterative review
- **Commit** — commit changes with descriptive message
- **Diff** / **Merge** / **Discard** — branch management

## Multiple PCs

Install the bot on multiple machines (e.g., desktop + laptop). Same bot token, same Telegram chat.

**How it works:**
- Claude CLI runs locally — the bot must run on the PC where your repos are
- Telegram only delivers messages to one poller at a time
- Starting the bot on a new PC automatically takes over; the old instance detects the conflict and shuts down gracefully
- Each PC has its own repos (`/repo add`) and local state

**To switch PCs:**
1. `/shutdown` from Telegram — remotely stops the current bot
2. Start `claude-bot` on the other PC — it takes over

Or just start the bot on the new PC — the old one auto-stops on conflict.

**Tip:** Set `PC_NAME=desktop` / `PC_NAME=laptop` in each `.env` so startup messages show which PC is active.

## Configuration

All settings via `.env` (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | required | Bot token from BotFather |
| `TELEGRAM_USER_ID` | required | Your Telegram user ID |
| `CLAUDE_BINARY` | `claude` | Path to Claude CLI |
| `MAX_CONCURRENT` | `5` | Max parallel instances |
| `DAILY_BUDGET_USD` | `20.0` | Daily spending limit |
| `QUERY_TIMEOUT_SECS` | `300` | Explore mode timeout |
| `TASK_TIMEOUT_SECS` | `600` | Build mode timeout |
| `PC_NAME` | hostname | Label for this PC (shown in notifications) |
| `LOG_LEVEL` | `INFO` | Logging level |
