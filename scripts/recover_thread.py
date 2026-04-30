"""One-shot recovery for thread 1498267960257675334.

Halts the running bot, rebinds the thread back to its GOOD aiagent session
(`a8780890-f00e-42c3-ad91-9427041c16a9`), and prints a restart instruction.

Run once from the repo root:

    python scripts/recover_thread.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PID_FILE = REPO / "data" / "bot.pid"
STATE_FILE = REPO / "data" / "state.json"

THREAD_ID = "1498267960257675334"
GOOD_SESSION = "a8780890-f00e-42c3-ad91-9427041c16a9"
REPO_NAME = "aiagent"


def stop_bot() -> None:
    if not PID_FILE.exists():
        print(f"[stop] No PID file at {PID_FILE} — assuming bot is not running.")
        return
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError) as exc:
        print(f"[stop] Cannot read PID file: {exc}")
        return

    print(f"[stop] Halting bot (PID {pid})...")
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                check=False, capture_output=True,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print("[stop] Process was already gone.")
    except OSError as exc:
        print(f"[stop] Failed to signal {pid}: {exc}")
        return

    deadline = time.time() + 15
    while time.time() < deadline:
        if not PID_FILE.exists():
            print("[stop] Bot exited (PID file removed).")
            return
        time.sleep(0.5)

    # Force-cleanup PID file if process is gone but file lingered
    try:
        PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    print("[stop] PID file removed (timeout fallback).")


def rebind_thread() -> bool:
    raw = STATE_FILE.read_text(encoding="utf-8")
    data = json.loads(raw)

    forums = (
        data.get("platform_state", {})
        .get("discord", {})
        .get("forum_projects", {})
    )
    proj = forums.get(REPO_NAME)
    if not proj:
        print(f"[rebind] ERROR: no forum project named {REPO_NAME!r} in state.")
        return False

    threads = proj.get("threads", {})
    t = threads.get(THREAD_ID)
    if not t:
        print(f"[rebind] ERROR: thread {THREAD_ID} not found under {REPO_NAME}.")
        return False

    current = t.get("session_id")
    print(f"[rebind] Current session_id on thread: {current}")
    if current == GOOD_SESSION:
        print("[rebind] Already bound to GOOD session — no change needed.")
        return True

    t["session_id"] = GOOD_SESSION
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[rebind] Rewrote thread {THREAD_ID} session_id -> {GOOD_SESSION}")
    return True


def main() -> int:
    if not STATE_FILE.exists():
        print(f"ERROR: {STATE_FILE} does not exist.")
        return 1

    stop_bot()
    ok = rebind_thread()

    print()
    if ok:
        print("Recovery complete. Start the bot again with:")
        print("    python -m bot")
        print()
        print("Then verify with:")
        print(f"    python scripts/discord_test.py read {THREAD_ID} 5")
        return 0
    print("Recovery FAILED — state.json was not modified.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
