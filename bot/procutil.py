"""Locating the running bot: where it is installed, and whether it is alive.

Stdlib only, and deliberately free of any bot import — the control scripts
that need it (``botctl.py``, ``smoke_test.py``, ``relaunch.py``) load it
before, and sometimes without, the virtualenv.

Two callers ask the same question, "is the PID in ``data/bot.pid`` still the
bot?", and get very different consequences from a wrong yes. ``bot/app.py``
merely refuses to start. ``scripts/botctl.py`` *signals* it — so on a PID the
kernel has recycled onto something else, a wrong yes means SIGTERM and then
SIGKILL to an unrelated process **group**, which on a desktop can be the
user's whole terminal session.

Hence the identity half, which is one-sided on purpose: it answers "no" only
on positive evidence that the process is something else, and "yes" wherever it
cannot see (no ``/proc``, permission denied, ``tasklist`` missing). Guessing
"no" would be the worse error in the other direction — it would let a second
bot start against the same Discord token and a second ``state.json``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


def install_root(script_root: Path) -> Path:
    """The bot's real installation directory, even when called from a worktree.

    Builds run in ``.worktrees/<id>/``, which gets its own ``data/`` tree —
    empty ``logs/``, no ``bot.pid``, no ``.env`` (it is gitignored). Anything
    that derives those paths from its own location therefore reads a blank
    installation and concludes the bot is down: the health check reports
    UNHEALTHY on a perfectly healthy bot, and following ``.claude/test.json``'s
    "only start it if the log says it is down" rule then launches a SECOND
    instance against the same Discord token and a fresh ``state.json`` — the
    exact collision the singleton note in that file forbids.

    ``git rev-parse --git-common-dir`` resolves to the main checkout's ``.git``
    from anywhere inside the repo, worktree or not, so its parent is the real
    installation. Without git we are almost certainly not in a worktree
    either, so falling back to the caller's own location is safe.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(script_root), capture_output=True, text=True,
            check=True, timeout=15,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return script_root
    if not out:
        return script_root
    root = Path(out).parent
    return root if (root / "bot" / "__main__.py").exists() else script_root


def is_process_alive(pid: int) -> bool:
    """Is a process with this PID running? Says nothing about *which* process.

    Only ESRCH means dead. All three earlier copies of this caught bare
    ``OSError``, which also swallows EPERM — a live process owned by another
    user, which the kernel refuses to let us signal. Reading that as "dead"
    is the dangerous direction: the singleton lock would take over a PID file
    still held by a running process. Proven on this host, where the old form
    reported PID 1 as not running.
    """
    if IS_WINDOWS:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        # An exited-but-not-reaped process still opens; STILL_ACTIVE separates them.
        STILL_ACTIVE = 259
        exit_code = ctypes.c_ulong()
        alive = bool(
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            and exit_code.value == STILL_ACTIVE
        )
        kernel32.CloseHandle(handle)
        return alive
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return True  # unexpected — err towards "still there"
    return True


def _looks_like_bot_argv(args: list[str]) -> bool:
    """Both supported launch forms: ``python -m bot`` and ``python bot/__main__.py``.

    ``start.sh``, ``scripts/claude-bot.service``, ``start.bat`` and
    ``botctl.py start`` all use the first; the second is what you get running
    it by hand from an editor.
    """
    if "bot" in args and "-m" in args:
        return True
    joined = " ".join(args).replace("\\", "/")
    return "bot/__main__.py" in joined


def is_bot_process(pid: int, root: Path | str | None = None) -> bool:
    """Could this PID be our bot? False only on positive evidence otherwise.

    ``root``, when given, is the installation directory the bot should be
    running in; a live process whose working directory is a *different* repo
    is someone else's bot, not ours.
    """
    if IS_WINDOWS:
        return _is_bot_process_windows(pid)

    proc = Path("/proc") / str(pid)
    try:
        raw = (proc / "cmdline").read_bytes()
    except OSError:
        return True  # no /proc, or not readable — can't tell, so don't guess
    args = [a for a in raw.decode("utf-8", "replace").split("\0") if a]
    if not args:
        return True  # kernel thread or vanished mid-read

    if not _looks_like_bot_argv(args):
        return False

    if root is not None:
        try:
            cwd = Path(os.readlink(str(proc / "cwd"))).resolve()
        except OSError:
            return True  # different user or hardened /proc — argv was enough
        try:
            return cwd == Path(root).resolve()
        except OSError:
            return True
    return True


def _is_bot_process_windows(pid: int) -> bool:
    """Image name only — there is no cheap dependency-free command line on Windows.

    Enough for the case that matters: a recycled PID landing on some ordinary
    program, which is then not signalled.
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, check=False, timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue  # "INFO: No tasks are running..." — filter matched nothing
        image = line.split('","')[0].lstrip('"').lower()
        return image.startswith("python") or image.startswith("py.exe")
    return True
