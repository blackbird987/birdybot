"""Delayed bot relaunch — spawned by /reboot to ensure clean restart.

Usage: python scripts/relaunch.py <project_root>

Waits for the old process to exit (via PID file), then starts a fresh bot.
"""

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.procutil import is_process_alive  # noqa: E402  (needs the path above)


def main():
    if len(sys.argv) < 2:
        print("Usage: relaunch.py <project_root>")
        sys.exit(1)

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

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        kwargs["close_fds"] = True
    subprocess.Popen([sys.executable, "-m", "bot"], cwd=cwd, **kwargs)


if __name__ == "__main__":
    main()
