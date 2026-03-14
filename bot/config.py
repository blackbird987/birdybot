"""Env-based configuration loaded via python-dotenv."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


# --- Telegram (optional — at least one platform must be configured) ---
TELEGRAM_BOT_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID: int | None = (
    int(os.getenv("TELEGRAM_USER_ID")) if os.getenv("TELEGRAM_USER_ID") else None
)
TELEGRAM_ENABLED: bool = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID)

# --- Discord (optional) ---
DISCORD_BOT_TOKEN: str | None = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID: int | None = (
    int(os.getenv("DISCORD_GUILD_ID")) if os.getenv("DISCORD_GUILD_ID") else None
)
DISCORD_LOBBY_CHANNEL_ID: int | None = (
    int(os.getenv("DISCORD_LOBBY_CHANNEL_ID")) if os.getenv("DISCORD_LOBBY_CHANNEL_ID") else None
)
DISCORD_CATEGORY_ID: int | None = (
    int(os.getenv("DISCORD_CATEGORY_ID")) if os.getenv("DISCORD_CATEGORY_ID") else None
)
DISCORD_USER_ID: int | None = (
    int(os.getenv("DISCORD_USER_ID")) if os.getenv("DISCORD_USER_ID") else None
)
DISCORD_CATEGORY_NAME: str | None = os.getenv("DISCORD_CATEGORY_NAME")
DISCORD_ENABLED: bool = bool(DISCORD_BOT_TOKEN and DISCORD_GUILD_ID)

# Test webhook IDs (comma-separated) — allow webhook messages to bypass bot/auth guards
TEST_WEBHOOK_IDS: set[str] = set(filter(None, os.getenv("TEST_WEBHOOK_IDS", "").split(",")))

# Validate: at least one platform
if not TELEGRAM_ENABLED and not DISCORD_ENABLED:
    raise RuntimeError(
        "No platform configured. Set TELEGRAM_BOT_TOKEN + TELEGRAM_USER_ID "
        "and/or DISCORD_BOT_TOKEN + DISCORD_GUILD_ID + DISCORD_LOBBY_CHANNEL_ID."
    )

# Optional with defaults
CLAUDE_BINARY: str = os.getenv("CLAUDE_BINARY", "claude")
MAX_CONCURRENT: int = int(os.getenv("MAX_CONCURRENT", "5"))
DAILY_BUDGET_USD: float = float(os.getenv("DAILY_BUDGET_USD", "20.0"))
PC_NAME: str = os.getenv("PC_NAME", "") or __import__("platform").node()
QUERY_TIMEOUT_SECS: int = int(os.getenv("QUERY_TIMEOUT_SECS", "300"))
TASK_TIMEOUT_SECS: int = int(os.getenv("TASK_TIMEOUT_SECS", "600"))
STALL_TIMEOUT_SECS: int = int(os.getenv("STALL_TIMEOUT_SECS", "60"))
INSTANCE_RETENTION_DAYS: int = int(os.getenv("INSTANCE_RETENTION_DAYS", "7"))
DIGEST_HOUR: int = int(os.getenv("DIGEST_HOUR", "20"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# Data directory
DATA_DIR: Path = Path(os.getenv("DATA_DIR", str(_PROJECT_ROOT / "data")))
RESULTS_DIR: Path = DATA_DIR / "results"
LOGS_DIR: Path = DATA_DIR / "logs"
STATE_FILE: Path = DATA_DIR / "state.json"
LOG_FILE: Path = LOGS_DIR / "bot.log"

# Ensure data dirs exist
REBOOT_MSG_FILE: Path = DATA_DIR / "reboot_message.json"
REBOOT_REQUEST_FILE: Path = DATA_DIR / "reboot_request.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# System prompt appended via --append-system-prompt
MOBILE_HINT = (
    "The user is reading on mobile. Be concise — lead with the answer, "
    "short paragraphs, show only relevant code fragments. "
    "When resuming a conversation, briefly acknowledge what the user is asking "
    "before continuing — don't silently pick up old work without context. "
    "The user can't see your prior conversation history, so if their message "
    "is ambiguous, clarify before doing heavy work."
)

# Separate block explaining the chat-app visibility constraint
CHAT_APP_CONSTRAINT = """
--- Communication Model ---
IMPORTANT: The user is in a chat app (Discord/Telegram). They see ONLY your final text responses. They CANNOT see tool calls, file contents, diffs, command output, or intermediate steps. Your text output is their ENTIRE window into what happened.

You must narrate your work:
- If you read a file → summarize what you found
- If you edited code → show what changed (short before/after or description of the change)
- If you ran a command → report success/failure and key output
- If something errored → include the actual error message
- If you searched code → share what you found or didn't find

Bad: "I've updated the function." (user has no idea what changed)
Good: "Changed `get_user()` to accept an optional `role` param — it now filters by role when provided, defaulting to the old behavior."

Think of it like pair programming over text — your partner can't see your screen.
"""

BOT_CONTEXT = """

--- Bot Context ---
You are running inside a bot that manages Claude Code instances. The user is chatting from their phone. You can do normal Claude Code work (read files, search code, run commands, etc.) but the bot also has these capabilities the user can invoke directly:

Scheduling:
- /schedule every <interval> <prompt> — recurring task (e.g. "every 6h", "every 30m", "every 1d")
- /schedule at <HH:MM> <prompt> — one-shot at a specific UTC time
- /schedule at +<duration> <prompt> — one-shot after a delay (e.g. "+2m", "+1h")
- /schedule list — show active schedules
- /schedule delete <id> — remove a schedule

Instance management:
- /bg <description> — run a background task (build mode, auto-branch)
- /list — show recent instances
- /kill <id|name> — terminate a running instance
- /retry <id|name> — re-run a failed instance
- /log <id|name> — view full output
- /diff <id|name> — view git changes from build tasks
- /merge <id|name> — merge build task branch
- /discard <id|name> — delete build task branch

Settings:
- /session — list recent desktop CLI sessions; /session resume <id> to continue one
- /mode explore|build — switch permission mode
- /verbose 0|1|2 — progress detail level (silent/normal/detailed)
- /context set <text> — pin context to all prompts
- /repo switch|add|list — manage repos
- /alias set|list|delete — saved command shortcuts
- /new — start a fresh conversation
- /cost — spending breakdown
- /status — health dashboard

If the user asks to do something the bot handles (like scheduling, switching repos, etc.), guide them to the right command rather than saying you can't do it.

Rebooting the bot:
- NEVER kill the bot process directly (taskkill, kill, etc.) — this interrupts all active queries and leaves stale messages.
- You can reboot the bot yourself when needed (e.g. to apply code changes you just made). Write a JSON file to data/reboot_request.json:
  {"message": "why you're rebooting", "resume_prompt": "what you want to do when you wake back up"}
  The bot picks this up after your response completes, waits for other queries to finish, reboots, and then sends resume_prompt back to this thread — resuming your session so you continue seamlessly.
- Use this naturally as part of your workflow. For example, if you edit bot code and need to apply it:
  1. Make the code changes
  2. Tell the user what you did and that you're rebooting to apply them
  3. Write the reboot file with a resume_prompt that has full context: what you changed, what to verify, what to do next
  4. The bot restarts, you wake up with that context, and you continue — check logs, verify the fix, report back
- The resume_prompt should read like your own notes-to-self. Include enough context to pick up exactly where you left off.
- IMPORTANT: You ARE the bot process. If you run taskkill/kill, you kill YOURSELF mid-response and the user sees "interrupted by bot restart" with no result. Always use the reboot file instead.
"""

# Claude Code session/plan data lives here
CLAUDE_PROJECTS_DIR: Path = Path.home() / ".claude" / "projects"

# Explore mode allowed tools
EXPLORE_TOOLS = "Read,Glob,Grep,WebSearch,WebFetch,Task,Skill,Bash(git *)"

# --- Canned prompts for contextual action buttons ---

PLAN_PROMPT_PREFIX = (
    "Create a detailed implementation plan for the following task. "
    "Explore the codebase, understand existing patterns and architecture, "
    "and design your approach. Do NOT implement anything yet — just plan.\n\n"
    "Task: "
)

BUILD_FROM_PLAN_PROMPT = (
    "Now implement the plan above. You have full build permissions."
)

BUILD_FROM_QUERY_PROMPT = (
    "Now implement the above. You have full build permissions."
)

PLAN_REVIEW_PROMPT = (
    'Carefully review this entire plan and come up with your best revisions '
    'in terms of better architecture, new features, changed features, etc. '
    'to make it better, more robust/reliable, more performant, more '
    'compelling/useful, etc. Also review for DRY, modularity, scalability '
    'and maintainability.\n\n'
    'For each proposed change, give your detailed analysis and '
    'rationale/justification for why it would make the project better '
    'along with git-diff style changes relative to the original plan.'
)

CODE_REVIEW_PROMPT = (
    'Now carefully read over all of the new code you just wrote and other '
    'existing code you just modified with "fresh eyes" looking super '
    'carefully for any obvious bugs, errors, problems, issues, confusion, '
    'etc. Carefully fix anything you uncover. Use ultrathink. '
    'Review if this is DRY, scalable, maintainable and modular.'
)

COMMIT_PROMPT = (
    'Review all uncommitted changes on this branch. '
    'Commit them with a clear, descriptive commit message. '
    'Also update CHANGELOG.md with a summary of what was changed and why. '
    'If CHANGELOG.md does not exist, create it.'
)

DONE_PROMPT = (
    'Wrap up this session. '
    'Review all uncommitted changes and commit them with a clear, descriptive message. '
    'Update CHANGELOG.md under the most recent version section with a summary of '
    'what was changed and why in this session. '
    'If CHANGELOG.md does not exist, create it. '
    'Make sure nothing is left uncommitted — this session is being closed.'
)
