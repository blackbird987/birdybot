"""Append-only session history log backed by a JSONL file.

Each completed/failed session appends one JSON line to data/history.jsonl.
Provides load_recent() for reading entries back (newest first).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from bot import config

log = logging.getLogger(__name__)

HISTORY_FILE: Path = config.DATA_DIR / "history.jsonl"


def append_entry(entry: dict) -> None:
    """Append a single history entry. Best-effort — never raises."""
    try:
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        log.warning("Failed to write history entry", exc_info=True)


def load_recent(
    repo: str | None = None,
    limit: int = 50,
    dedupe_thread: bool = False,
) -> list[dict]:
    """Load recent history entries, newest first.

    Args:
        repo: Filter by repo name (None = all repos).
        limit: Maximum entries to return.
        dedupe_thread: If True, keep only the latest entry per thread_id.
            Useful for display — collapses autopilot chains into one entry.
    """
    if not HISTORY_FILE.exists():
        return []
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        log.warning("Failed to read history file", exc_info=True)
        return []

    seen_threads: set[str] = set()
    entries: list[dict] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if repo and entry.get("repo") != repo:
            continue
        if dedupe_thread:
            tid = entry.get("thread_id", "")
            if tid and tid in seen_threads:
                continue
            if tid:
                seen_threads.add(tid)
        entries.append(entry)
        if len(entries) >= limit:
            break
    return entries
