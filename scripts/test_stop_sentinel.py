#!/usr/bin/env python3
"""Does a signalled bot come back, or stay down?

The bot answers a signal by relaunching itself, so that an agent killing it
does not leave it offline. On Windows that only ever fired for an unusual
signal. On Linux SIGTERM is how *everything* stops a process — `botctl stop`,
`start.sh`, `systemctl stop`, shutdown — so the same behaviour makes the bot
unstoppable, and the only reason it did not was a second bug that made the
relaunch silently fail on Linux. Fixing that one exposes this one.

The handshake: a caller that means it writes `data/stop_requested` first, and
the signal handler consumes it and stays down.

This exercises that against a real process and real signals, using a stand-in
daemon rather than the bot itself — starting a second bot would fight the
running one for the PID lock and the Discord token.

    python scripts/test_stop_sentinel.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.procutil import (  # noqa: E402  (needs the path above)
    clear_stop_request,
    detached_kwargs,
    request_stop,
    stop_was_requested,
)

# Mirrors the shape of _emergency_reboot_handler in bot/app.py: consult the
# sentinel, and either stay down or "relaunch". Writing a marker instead of
# spawning a bot is the only difference.
DAEMON = '''
import os, signal, sys, time
from pathlib import Path
sys.path.insert(0, %(root)r)
from bot.procutil import stop_was_requested

data = Path(%(data)r)

def handler(signum, frame):
    if stop_was_requested(data):
        (data / "outcome").write_text("stayed-down", encoding="utf-8")
    else:
        (data / "outcome").write_text("relaunched", encoding="utf-8")
    os._exit(0)

signal.signal(signal.SIGTERM, handler)
(data / "ready").write_text(str(os.getpid()), encoding="utf-8")
time.sleep(30)
'''

failures: list[str] = []


def _run_case(name: str, arm, expected: str) -> None:
    """Start the stand-in, let `arm` prepare state, SIGTERM it, read the outcome."""
    data = Path(tempfile.mkdtemp(prefix="stopsentinel-"))
    root = str(Path(__file__).resolve().parent.parent)
    proc = subprocess.Popen(
        [sys.executable, "-c", DAEMON % {"root": root, "data": str(data)}],
        **detached_kwargs(),
    )
    try:
        for _ in range(100):  # up to 10s for the handler to be installed
            if (data / "ready").exists():
                break
            time.sleep(0.1)
        else:
            failures.append(f"{name}: stand-in never became ready")
            return

        arm(data)
        os.kill(proc.pid, signal.SIGTERM)

        for _ in range(100):
            if (data / "outcome").exists():
                break
            time.sleep(0.1)
        else:
            failures.append(f"{name}: no outcome written — handler never ran")
            return

        got = (data / "outcome").read_text(encoding="utf-8").strip()
        if got != expected:
            failures.append(f"{name}: expected {expected!r}, got {got!r}")
        else:
            print(f"  [PASS] {name} -> {got}")
    finally:
        try:
            proc.kill()
        except OSError:
            pass


def check_helpers() -> None:
    """The sentinel itself: one request answers exactly one signal, and expires."""
    data = Path(tempfile.mkdtemp(prefix="stopsentinel-"))

    if stop_was_requested(data):
        failures.append("helpers: reported a stop with no sentinel present")

    request_stop(data)
    if not stop_was_requested(data):
        failures.append("helpers: a fresh request was not seen")
    if stop_was_requested(data):
        failures.append("helpers: request survived being read — one stop, one signal")

    # A caller that died mid-stop must not disable the relaunch indefinitely.
    request_stop(data)
    stale = time.time() - 300
    os.utime(data / "stop_requested", (stale, stale))
    if stop_was_requested(data):
        failures.append("helpers: a 5-minute-old request was still honoured")
    if (data / "stop_requested").exists():
        failures.append("helpers: expired request was not cleaned up")

    request_stop(data)
    clear_stop_request(data)
    if (data / "stop_requested").exists():
        failures.append("helpers: clear_stop_request left the file behind")

    clear_stop_request(data)  # must not raise when there is nothing to clear
    print("  [PASS] sentinel helpers (write / consume / expire / clear)")


def check_detached_kwargs() -> None:
    """The kwargs must never name a constant this platform does not have."""
    kw = detached_kwargs()
    if sys.platform == "win32":
        if "creationflags" not in kw:
            failures.append("detached_kwargs: no creationflags on Windows")
    else:
        if "creationflags" in kw:
            failures.append(
                "detached_kwargs: creationflags on POSIX — Popen rejects it"
            )
        if not kw.get("start_new_session"):
            failures.append(
                "detached_kwargs: no start_new_session — a process-group kill "
                "aimed at the bot would also take out its relauncher"
            )
    print(f"  [PASS] detached_kwargs -> {kw}")


def main() -> int:
    print("=" * 60)
    print("Stop sentinel — does a signalled bot stay down when told to?")
    print("=" * 60)

    check_detached_kwargs()
    check_helpers()

    if sys.platform == "win32":
        # taskkill /F runs no handler, and there is no SIGTERM to send.
        print("  [SKIP] signal cases — Windows stops the bot with taskkill /F")
    else:
        _run_case("killed with no warning -> relaunches", lambda d: None, "relaunched")
        _run_case("stopped deliberately -> stays down", request_stop, "stayed-down")

        def stale(d: Path) -> None:
            request_stop(d)
            old = time.time() - 300
            os.utime(d / "stop_requested", (old, old))

        _run_case("stale stop request -> relaunches", stale, "relaunched")

    print("-" * 60)
    if failures:
        print(f"FAILED — {len(failures)} problem(s):")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
