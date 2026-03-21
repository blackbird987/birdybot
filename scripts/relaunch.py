"""Delayed bot relaunch — spawned by /reboot to ensure clean restart.

Usage: python scripts/relaunch.py <project_root>

Waits for the old process to exit (via PID file), then starts a fresh bot.
"""

import subprocess
import sys
import time
from pathlib import Path


def _is_process_alive(pid: int) -> bool:
    """Check if a process is still running (cross-platform)."""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        STILL_ACTIVE = 259
        exit_code = ctypes.c_ulong()
        alive = bool(
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            and exit_code.value == STILL_ACTIVE
        )
        kernel32.CloseHandle(handle)
        return alive
    else:
        import os
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


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
            if not _is_process_alive(old_pid):
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
