#!/usr/bin/env python3
"""Cross-platform start/stop/status for the bot.

Replaces the Windows-only ``start_bot.bat`` / ``stop_bot.bat`` pair as the
entry point named in ``.claude/test.json``. That file is static JSON with no
way to branch per platform, so its ``start``/``stop`` commands pointed at
batch files that simply do not exist on Linux — meaning the verify step could
never recover a bot it found down.

The bot writes and clears ``data/bot.pid`` itself (see ``bot/app.py``), and
that logic is already cross-platform, so this script only has to launch a
detached process and later signal it.

Two things it must get right, because the caller is usually an automated
verify step rather than a person: it always acts on the *installed* bot even
when run from a build worktree (see ``procutil.install_root``), and it never
signals a PID that no longer looks like the bot (see ``procutil.is_bot_process``).

Usage:
    python scripts/botctl.py start | stop | restart | status
    python scripts/botctl.py logs [n]      # last n log lines, default 50

Exit codes: 0 = success / running, 1 = failure, 2 = not running (status).
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

# Where this copy of the script lives — used to import the bot package, so we
# always run the liveness/identity logic that shipped alongside us.
SCRIPT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_ROOT))
from bot.procutil import (  # noqa: E402  (needs the path above)
    clear_stop_request,
    detached_kwargs,
    install_root,
    is_bot_process,
    is_process_alive,
    request_stop,
)

# Everything we act on — pid file, log, launch cwd, venv — belongs to the
# installed bot, not to whatever worktree this script was invoked from.
REPO = install_root(SCRIPT_ROOT)
DATA_DIR = REPO / "data"
PID_FILE = DATA_DIR / "bot.pid"

IS_WINDOWS = sys.platform == "win32"


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    """Cheap liveness — safe to call in a poll loop."""
    return is_process_alive(pid)


def _live_bot_pid() -> int | None:
    """The PID of a running bot, or None. Rejects a recycled PID.

    An unclean exit leaves the PID file behind and the kernel eventually
    reuses that number. Trusting it blindly means `start` silently does
    nothing (the one job it has when verify finds the bot down) and `stop`
    SIGKILLs a stranger's process *group*.
    """
    pid = _read_pid()
    if pid is None or not _alive(pid):
        return None
    if not is_bot_process(pid, REPO):
        return None
    return pid


def _python() -> str:
    """Interpreter to launch the bot with.

    Prefer the repo venv so this works when invoked by a system python; fall
    back to whatever is running us. On Windows prefer pythonw so no console
    window pops up for a background service.
    """
    names = (
        ["Scripts/pythonw.exe", "Scripts/python.exe"]
        if IS_WINDOWS else ["bin/python3", "bin/python"]
    )
    for rel in names:
        cand = REPO / ".venv" / rel
        if cand.exists():
            return str(cand)
    return sys.executable or ("pythonw" if IS_WINDOWS else "python3")


def _signal_posix(pid: int, sig: int) -> str | None:
    """Signal the bot, preferring its whole process group.

    The bot spawns CLI subprocesses that hold worktrees open, so the group is
    the right target — but only when it is a *different* group from ours.
    If the bot was launched from this very shell we'd share a group and
    killpg would take this script down with it, so fall back to the bare pid.

    Returns the error text if nothing could be signalled, else None. Swallowing
    it meant a permission failure looked identical to a bot that ignores
    SIGTERM, and the two need opposite responses.
    """
    try:
        target_pgid = os.getpgid(pid)
    except OSError:
        target_pgid = None

    if target_pgid is not None and target_pgid != os.getpgrp():
        try:
            os.killpg(target_pgid, sig)
            return None
        except OSError as e:
            last = str(e)
    else:
        last = None
    try:
        os.kill(pid, sig)
    except OSError as e:
        return last or str(e)
    return None


def status() -> int:
    pid = _read_pid()
    if pid is None:
        print("Bot is NOT running (no pid file).")
        return 2
    if not _alive(pid):
        print(f"Bot is NOT running (stale pid {pid}).")
        return 2
    if not is_bot_process(pid, REPO):
        print(f"Bot is NOT running (pid {pid} was recycled onto another process).")
        return 2
    print(f"Bot is running (pid {pid}).")
    return 0


def start() -> int:
    pid = _live_bot_pid()
    if pid is not None:
        print(f"Bot already running (pid {pid}) — nothing to do.")
        return 0

    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    stdout_log = DATA_DIR / "logs" / "stdout.log"

    # A sentinel from an earlier stop must not outlive it — the bot clears it
    # on startup too, but not before it has read its own signal handlers in.
    clear_stop_request(DATA_DIR)

    # Detach so the bot outlives this script and its parent shell.
    kwargs = detached_kwargs()

    with open(stdout_log, "ab") as out:
        proc = subprocess.Popen(
            [_python(), "-m", "bot"],
            cwd=str(REPO),
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **kwargs,
        )

    # The bot writes its own pid file once it has the singleton lock. Wait for
    # that rather than trusting our child pid, so "started" means "actually
    # came up" and not "process existed for a moment".
    for _ in range(40):  # up to 20s
        time.sleep(0.5)
        if proc.poll() is not None:
            print(
                f"Bot exited immediately (rc={proc.returncode}). "
                f"See {stdout_log} and data/logs/bot.log",
                file=sys.stderr,
            )
            return 1
        live = _live_bot_pid()
        if live is not None:
            print(f"Bot started (pid {live}). Logs: {DATA_DIR / 'logs' / 'bot.log'}")
            return 0

    print(
        "Bot process launched but never claimed the pid file — it may be "
        "stuck or another instance holds the lock. Check data/logs/bot.log",
        file=sys.stderr,
    )
    return 1


def stop() -> int:
    pid = _read_pid()
    if pid is None:
        print("No pid file — bot does not appear to be running.")
        return 0
    if not _alive(pid):
        print(f"Stale pid {pid} — clearing.")
        PID_FILE.unlink(missing_ok=True)
        return 0
    if not is_bot_process(pid, REPO):
        # Never signal on this: the group kill below would take out whatever
        # unrelated thing inherited the number.
        print(f"Pid {pid} is not this bot (recycled) — clearing the file only.")
        PID_FILE.unlink(missing_ok=True)
        return 0

    print(f"Stopping bot (pid {pid})...")
    # Tell the bot this is a real shutdown. Its SIGTERM handler otherwise
    # relaunches itself, so without this every stop on Linux would be answered
    # by a fresh bot a few seconds later.
    request_stop(DATA_DIR)
    err: str | None = None
    if IS_WINDOWS:
        # /T so the whole tree goes: the bot spawns CLI subprocesses that
        # would otherwise be orphaned and keep holding worktrees open.
        done = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, text=True, check=False,
        )
        if done.returncode != 0:
            err = (done.stderr or done.stdout or "").strip() or None
    else:
        # Graceful first — app.py has a SIGTERM handler that saves context.
        err = _signal_posix(pid, signal.SIGTERM)

    for _ in range(20):  # up to 10s
        time.sleep(0.5)
        if not _alive(pid):
            return _stopped()

    if not IS_WINDOWS:
        print("Graceful stop timed out — sending SIGKILL.")
        err = _signal_posix(pid, signal.SIGKILL) or err
        time.sleep(1)

    if _alive(pid):
        # Leave the sentinel: it expires on its own, and clearing it here
        # would re-arm the relaunch under a bot we have not managed to stop.
        detail = f": {err}" if err else ""
        print(f"Failed to stop bot (pid {pid}){detail}.", file=sys.stderr)
        return 1
    return _stopped()


def _stopped() -> int:
    """Tidy up after the bot is confirmed gone."""
    PID_FILE.unlink(missing_ok=True)
    # A SIGKILLed bot never ran its handler, so the sentinel can outlive it.
    clear_stop_request(DATA_DIR)
    print("Bot stopped.")
    return 0


def logs(count: int = 50) -> int:
    """Print the tail of the bot log.

    Exists because `tail` is not a command on Windows, and `.claude/test.json`
    is static JSON that cannot branch per platform — the verify step needs one
    log-reading command that works on both machines.
    """
    log_file = DATA_DIR / "logs" / "bot.log"
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=max(1, count))
    except OSError as e:
        print(f"Cannot read {log_file}: {e}", file=sys.stderr)
        return 1
    sys.stdout.write("".join(tail))
    if tail and not tail[-1].endswith("\n"):
        sys.stdout.write("\n")
    return 0


USAGE = "Usage: botctl.py start|stop|restart|status|logs [n]"


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        return start()
    if cmd == "stop":
        return stop()
    if cmd == "restart":
        rc = stop()
        if rc != 0:
            return rc
        time.sleep(1)
        return start()
    if cmd == "status":
        return status()
    if cmd == "logs":
        try:
            count = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        except ValueError:
            print(f"logs: line count must be a number\n{USAGE}", file=sys.stderr)
            return 1
        return logs(count)
    print(f"Unknown command: {cmd}\n{USAGE}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
