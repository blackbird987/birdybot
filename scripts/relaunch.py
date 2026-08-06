"""Delayed bot relaunch — brings the bot back after it exits.

Spawned by both restart paths in ``bot/app.py``: the coalesced `/reboot`
executor, and the signal handler that catches a bot being killed.

Usage: python scripts/relaunch.py <project_root>

Waits for the old process to exit (via PID file), then starts a fresh bot —
unless systemd is already going to do that for us.
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.procutil import (  # noqa: E402  (needs the path above)
    detached_kwargs,
    is_process_alive,
)


# The unit shipped in scripts/. Renaming it on install costs only the check
# below, which then falls back to relaunching ourselves.
SERVICE_UNIT = "claude-bot.service"


def _under_service_unit() -> bool:
    """Is a systemd service already responsible for restarting the bot?

    If one is, starting a replacement here puts two candidates in a race for
    the PID lock. The loser exits immediately — and when the loser is the
    supervised one, `Restart=always` turns that into a restart loop until
    systemd gives up on the unit, leaving a bot that runs but is no longer
    watched by anything.

    Read from the cgroup, not from INVOCATION_ID: a desktop terminal is itself
    a systemd *scope* and exports that variable, so a bot started by hand from
    Konsole would inherit it and look supervised when nothing is watching.
    The cgroup path names the unit that actually owns this process.
    """
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return False  # not Linux, or no /proc — nobody else is restarting us
    return SERVICE_UNIT in cgroup


def main():
    if len(sys.argv) < 2:
        print("Usage: relaunch.py <project_root>")
        sys.exit(1)

    if _under_service_unit():
        print("Running under systemd — leaving the restart to the unit.")
        return

    cwd = sys.argv[1]
    pid_file = Path(cwd) / "data" / "bot.pid"

    # Wait up to 15 seconds for the old process to exit
    for _ in range(30):
        time.sleep(0.5)
        if not pid_file.exists():
            break
        try:
            old_pid = int(pid_file.read_text().strip())
            if not is_process_alive(old_pid):
                break
        except (ValueError, OSError):
            break

    subprocess.Popen([sys.executable, "-m", "bot"], cwd=cwd, **detached_kwargs())


if __name__ == "__main__":
    main()
