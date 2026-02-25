"""Env-based configuration loaded via python-dotenv."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


# Required
TELEGRAM_BOT_TOKEN: str = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID: int = int(_require("TELEGRAM_USER_ID"))

# Optional with defaults
CLAUDE_BINARY: str = os.getenv("CLAUDE_BINARY", "claude")
MAX_CONCURRENT: int = int(os.getenv("MAX_CONCURRENT", "5"))
DAILY_BUDGET_USD: float = float(os.getenv("DAILY_BUDGET_USD", "20.0"))
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
DATA_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# System prompt appended via --append-system-prompt
MOBILE_HINT = (
    "The user is reading on mobile. Be concise — lead with the answer, "
    "short paragraphs, show only relevant code fragments."
)

BOT_CONTEXT = """

--- Telegram Bot Context ---
You are running inside a Telegram bot that manages Claude Code instances. The user is chatting from their phone. You can do normal Claude Code work (read files, search code, run commands, etc.) but the bot also has these capabilities the user can invoke directly in Telegram:

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
"""

# Claude Code session/plan data lives here
CLAUDE_PROJECTS_DIR: Path = Path.home() / ".claude" / "projects"

# Explore mode allowed tools
EXPLORE_TOOLS = "Read,Glob,Grep,WebSearch,WebFetch,Task,Bash(git *)"

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
