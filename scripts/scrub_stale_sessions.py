"""Clear ThreadInfo.session_id entries whose JSONLs no longer exist.

After an account swap, Claude CLI session JSONLs become orphaned (the dead
account's files don't migrate). The bot keeps trying `--resume <dead_id>`
on every message in those threads, eats one failed run, then rebinds via
v0.85.2. This script preempts that: walks every ThreadInfo, checks if its
session JSONL still exists in ~/.claude/projects/*/, and clears any orphans.

Writes data/state.json atomically. Triggers a bot reboot so the in-memory
ForumManager picks up the scrubbed state.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "state.json"
REBOOT_PATH = ROOT / "data" / "reboot_request.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"


def collect_known_session_ids() -> set[str]:
    if not PROJECTS_DIR.is_dir():
        return set()
    ids: set[str] = set()
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.glob("*.jsonl"):
            ids.add(f.stem)
    return ids


def atomic_write_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    if not STATE_PATH.is_file():
        print(f"state file not found: {STATE_PATH}", file=sys.stderr)
        return 1

    known = collect_known_session_ids()
    print(f"Found {len(known)} session JSONLs under {PROJECTS_DIR}")

    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    forum_projects = state.get("platform_state", {}).get("discord", {}).get("forum_projects", {})
    if not forum_projects:
        print("no forum_projects in state — nothing to scrub")
        return 0

    cleared = []
    kept_alive = 0
    no_session = 0
    for repo_name, proj in forum_projects.items():
        for tid, thread in proj.get("threads", {}).items():
            sid = thread.get("session_id")
            if not sid:
                no_session += 1
                continue
            if sid in known:
                kept_alive += 1
                continue
            cleared.append((repo_name, tid, sid))
            thread["session_id"] = None

    print(f"\nScan results:")
    print(f"  alive   : {kept_alive}")
    print(f"  empty   : {no_session}")
    print(f"  cleared : {len(cleared)}")

    if not cleared:
        print("\nNothing to scrub. Bot reboot not needed.")
        return 0

    print("\nCleared:")
    for repo, tid, sid in cleared:
        print(f"  [{repo}] thread {tid}  ->  was {sid[:12]}…")

    atomic_write_json(STATE_PATH, state)
    print(f"\nWrote scrubbed state to {STATE_PATH}")

    REBOOT_PATH.write_text(
        json.dumps(
            {
                "message": f"scrubbed {len(cleared)} stale ThreadInfo.session_ids",
                "resume_prompt": (
                    "Stale-session scrub completed. Verify: "
                    "(1) tail -n 30 data/logs/bot.log and confirm no startup errors, "
                    "(2) python scripts/smoke_test.py is HEALTHY, "
                    "(3) report scrub count and any anomalies. "
                    f"Cleared {len(cleared)} stale session_ids before reboot."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {REBOOT_PATH} — bot will reboot to load scrubbed state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
