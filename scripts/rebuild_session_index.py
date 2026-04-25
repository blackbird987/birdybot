"""One-shot rebuild of every Claude CLI sessions-index.json.

Run this when `--resume <id>` returns "No conversation found" for sessions
whose JSONL files exist on disk. Backs up the existing indexes first.

Usage:
    python scripts/rebuild_session_index.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Allow running as a script from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.claude import session_index  # noqa: E402


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    counts = await session_index.rebuild_all(
        state=None,                # no active-project guard from CLI run
        exempt_instance_id=None,
        backup=True,               # one-shot recovery — keep a backup
    )
    print(
        f"\nDone: {counts['projects']} projects rebuilt, "
        f"{counts['sessions']} sessions indexed, "
        f"{counts['failed']} failed, "
        f"{counts['skipped_active']} skipped (active), "
        f"{counts['no_jsonls']} empty."
    )
    return 0 if counts["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
