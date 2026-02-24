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

# Conciseness hint appended to all prompts
MOBILE_HINT = (
    "\n\nI'm reading on mobile. Be concise — lead with the answer, "
    "short paragraphs, show only relevant code fragments."
)

# Explore mode allowed tools
EXPLORE_TOOLS = "Read,Glob,Grep,WebSearch,WebFetch,Task,Bash(git *)"
