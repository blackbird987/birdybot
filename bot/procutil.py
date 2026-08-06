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
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


def detached_kwargs() -> dict:
    """``Popen`` kwargs for a child that must outlive us.

    The two Windows flags are spelled as literals on purpose.
    ``subprocess.DETACHED_PROCESS`` and friends **do not exist** on POSIX
    Python — reading the attribute raises ``AttributeError``. Both of the
    bot's restart paths did exactly that, inside an ``except`` that swallowed
    it, so on Linux the emergency relaunch and ``/reboot`` were silent no-ops
    while the code read as if they worked.

    On POSIX ``start_new_session`` is the equivalent that matters: it puts the
    child in its own session, so a process-*group* kill aimed at the dying bot
    does not also take out the relauncher that is meant to bring it back.
    """
    if IS_WINDOWS:
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        return {
            "creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            "close_fds": True,
        }
    return {"start_new_session": True, "close_fds": True}


# --------------------------------------------------------------------------
# Git must never stop and ask a human
# --------------------------------------------------------------------------


def harden_git_env() -> None:
    """Make git fail loudly on missing credentials instead of hanging.

    Every git call the bot makes is a subprocess with no terminal attached.
    When such a git needs a username or password it does *not* give up -- it
    looks for a graphical helper, and on a KDE desktop it finds
    ``SSH_ASKPASS=/usr/bin/ksshaskpass`` and pops a dialog on a screen no
    agent is watching. Git then blocks on that dialog forever, the bot's 30s
    push timeout fires, and the log records a meaningless
    ``Push to origin timed out (30s)``.

    That is how a plain missing-credential turned into a silent, repeating,
    hour-after-hour sync failure on this machine. The credential itself lived
    in Windows' credential manager and did not survive the move to Linux; the
    fix for *that* is SSH remotes. This function fixes the second, worse half
    -- that the failure was invisible.

    Set once at startup so all ~84 git call sites, plus git run by the Claude
    CLI children, inherit it. Afterwards a credential problem surfaces in
    under a second as ``could not read Username ... terminal prompts
    disabled``, naming the actual cause.
    """
    # No terminal prompt...
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    # ...and no GUI one either. Empty-but-set is deliberate: git reads
    # GIT_ASKPASS first and only consults SSH_ASKPASS when GIT_ASKPASS is
    # *unset*, so this both disables the helper and shadows the desktop's.
    os.environ["GIT_ASKPASS"] = ""
    # Same for ssh itself, which has its own askpass path for key passphrases
    # (OpenSSH >= 8.4). Harmless on older ssh, which ignores it.
    os.environ["SSH_ASKPASS_REQUIRE"] = "never"


# --------------------------------------------------------------------------
# "Stay down" handshake
# --------------------------------------------------------------------------
# The bot relaunches itself when signalled, so that an agent killing it does
# not leave it dead. On Windows that only ever fired for an unusual signal;
# on Linux SIGTERM is *the* ordinary way to stop a process, so `botctl stop`,
# `start.sh` and `systemctl stop` would all be met by a bot that immediately
# comes back. A caller that means it writes this file first.
STOP_SENTINEL = "stop_requested"

# Long enough for the slowest stop (SIGTERM, 10s grace, SIGKILL), short enough
# that a sentinel left behind by a crashed caller cannot disable the emergency
# relaunch for any length of time. Startup clears it too.
_STOP_MAX_AGE_S = 120.0


def request_stop(data_dir: Path | str) -> None:
    """Declare the next shutdown deliberate — the bot must not relaunch."""
    try:
        target = Path(data_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / STOP_SENTINEL).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass  # worst case the bot relaunches; the caller can try again


def clear_stop_request(data_dir: Path | str) -> None:
    """Drop any sentinel — called on startup and after a stop completes."""
    try:
        (Path(data_dir) / STOP_SENTINEL).unlink()
    except OSError:
        pass


def stop_was_requested(data_dir: Path | str) -> bool:
    """Consume the sentinel. True only for a fresh, deliberate stop.

    Consuming rather than merely reading it means one request answers one
    signal; a second kill of a bot that came back is treated as an emergency
    again, which is the safer default.
    """
    path = Path(data_dir) / STOP_SENTINEL
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    try:
        path.unlink()
    except OSError:
        pass
    return age <= _STOP_MAX_AGE_S


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

    Matching the argument that actually follows ``-m`` is both wider and
    narrower than looking for the two tokens anywhere: it accepts the
    ``bot.__main__`` spelling, and it stops ``python -m pytest bot`` from
    passing for the bot itself.
    """
    if "-m" in args:
        idx = args.index("-m")
        if idx + 1 < len(args) and args[idx + 1] in ("bot", "bot.__main__"):
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
            cwd = os.readlink(str(proc / "cwd"))
        except OSError:
            return True  # different user or hardened /proc — argv was enough
        try:
            # samefile, not string equality: this repo lives under a mount
            # point that is reachable by more than one path, and a bot whose
            # directory has been replaced reads back as "<path> (deleted)",
            # which no amount of normalising turns into a match. Both cases
            # raise here and fall through to the conservative answer.
            return os.path.samefile(cwd, str(root))
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
