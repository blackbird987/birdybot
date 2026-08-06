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

Usage:
    python scripts/botctl.py start | stop | restart | status
    python scripts/botctl.py logs [n]      # last n log lines, default 50

Exit codes: 0 = success / running, 1 = failure, 2 = not running (status).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PID_FILE = REPO / "data" / "bot.pid"

IS_WINDOWS = sys.platform == "win32"


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    """Is this PID running? Mirrors bot/app.py::_is_process_alive."""
    if IS_WINDOWS:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        alive = bool(
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            and exit_code.value == 259  # STILL_ACTIVE
        )
        kernel32.CloseHandle(handle)
        return alive
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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


def _signal_posix(pid: int, sig: int) -> None:
    """Signal the bot, preferring its whole process group.

    The bot spawns CLI subprocesses that hold worktrees open, so the group is
    the right target — but only when it is a *different* group from ours.
    If the bot was launched from this very shell we'd share a group and
    killpg would take this script down with it, so fall back to the bare pid.
    """
    try:
        target_pgid = os.getpgid(pid)
    except OSError:
        target_pgid = None

    if target_pgid is not None and target_pgid != os.getpgrp():
        try:
            os.killpg(target_pgid, sig)
            return
        except OSError:
            pass
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def status() -> int:
    pid = _read_pid()
    if pid is None:
        print("Bot is NOT running (no pid file).")
        return 2
    if not _alive(pid):
        print(f"Bot is NOT running (stale pid {pid}).")
        return 2
    print(f"Bot is running (pid {pid}).")
    return 0


def start() -> int:
    pid = _read_pid()
    if pid is not None and _alive(pid):
        print(f"Bot already running (pid {pid}) — nothing to do.")
        return 0

    (REPO / "data" / "logs").mkdir(parents=True, exist_ok=True)
    stdout_log = REPO / "data" / "logs" / "stdout.log"

    # Detach so the bot outlives this script and its parent shell.
    kwargs: dict = {}
    if IS_WINDOWS:
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — no console, and Ctrl-C
        # in the launching shell doesn't propagate into the bot.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True

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
        live = _read_pid()
        if live is not None and _alive(live):
            print(f"Bot started (pid {live}). Logs: data/logs/bot.log")
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

    print(f"Stopping bot (pid {pid})...")
    if IS_WINDOWS:
        # /T so the whole tree goes: the bot spawns CLI subprocesses that
        # would otherwise be orphaned and keep holding worktrees open.
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, check=False,
        )
    else:
        # Graceful first — app.py has a SIGTERM handler that saves context.
        _signal_posix(pid, signal.SIGTERM)

    for _ in range(20):  # up to 10s
        time.sleep(0.5)
        if not _alive(pid):
            PID_FILE.unlink(missing_ok=True)
            print("Bot stopped.")
            return 0

    if not IS_WINDOWS:
        print("Graceful stop timed out — sending SIGKILL.")
        _signal_posix(pid, signal.SIGKILL)
        time.sleep(1)

    if _alive(pid):
        print(f"Failed to stop bot (pid {pid}).", file=sys.stderr)
        return 1
    PID_FILE.unlink(missing_ok=True)
    print("Bot stopped.")
    return 0


def logs(count: int = 50) -> int:
    """Print the tail of the bot log.

    Exists because `tail` is not a command on Windows, and `.claude/test.json`
    is static JSON that cannot branch per platform — the verify step needs one
    log-reading command that works on both machines.
    """
    log_file = REPO / "data" / "logs" / "bot.log"
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
