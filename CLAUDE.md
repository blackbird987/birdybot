# Claude Code Bot

Multi-platform (Telegram + Discord) bot for managing Claude Code instances remotely.

## Quick Start

```bash
python -m bot          # start the bot
```

## Log Monitoring

When debugging or testing, always tail the log file proactively:

```bash
tail -f data/logs/bot.log        # real-time monitoring (run in background)
tail -n 50 data/logs/bot.log     # recent entries
```

Don't wait for the user to paste errors — check the logs yourself.

## Key Paths

- **Entry point**: `bot/__main__.py` -> `bot/app.py:run()`
- **Config**: `bot/config.py` (reads `.env`)
- **Log file**: `data/logs/bot.log`
- **State**: `data/state.json`
- **Engine** (platform-agnostic): `bot/engine/commands.py`, `lifecycle.py`, `workflows.py`, `sessions.py`
- **Platform layer**: `bot/platform/base.py` (Messenger protocol), `bot/platform/formatting.py`
- **Telegram**: `bot/telegram/adapter.py`, `bridge.py`, `formatter.py`
- **Discord**: `bot/discord/bot.py`, `adapter.py`, `channels.py`, `formatter.py`

## Discord Limits

- Max 5 button rows per View (truncate, don't crash)
- 2000 char regular message limit, 4096 for embed descriptions
- Slash commands are guild-synced (instant registration)
- 3-second interaction timeout — always `defer()` first
- `intents.members = True` needed for permission overwrites on category creation

## Testing

No test suite yet. Test by running the bot and interacting via Telegram/Discord.
