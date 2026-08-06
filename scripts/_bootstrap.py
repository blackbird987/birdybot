"""Let the standalone scripts in this directory run under any interpreter.

Two jobs: put the repo root on ``sys.path``, and — if the interpreter that
started us cannot see the bot's third-party dependencies — relaunch the script
under the project's own virtualenv.

Usage: make it the *first* import in the script, above everything except the
module docstring and any ``from __future__`` line::

    <the module docstring>

    import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

    import sys
    ...

It has to come first because the relaunch has to happen before anything that
might fail on a missing dependency — including scripts that import ``bot``
lazily from inside a function, where an anchor next to the ``bot`` import would
not exist to attach to.

The scripts still carry their own ``sys.path.insert(...)`` further down. That is
now redundant with the ``sys.path`` work here, and deliberately left in place:
it keeps this change to one added line per file, and it means the scripts go on
working under the virtualenv even if this module is removed.

Why this is needed
------------------
The bot is started as ``.venv/bin/python3 -m bot``, by direct path rather than
by activating the virtualenv. That leaves ``VIRTUAL_ENV`` unset and ``.venv/bin``
absent from ``PATH``, so every process the bot spawns resolves a bare ``python``
to the *system* interpreter, which has none of the bot's dependencies. The
harness commands in ``.claude/test.json`` are written as ``python scripts/x.py``,
so a verify run failed 12 of the 15 at import with ``No module named 'dotenv'`` —
failures that look exactly like real ones but say nothing about the code. The
three survivors made it worse rather than better: a partial pass reads as a real
regression in the other twelve, not as an environment problem.

Why relaunch instead of fixing ``PATH``
---------------------------------------
The tempting fix is to have the bot prepend its virtualenv to ``PATH`` for child
processes. Don't: this bot manages several repos, so that would silently hand
*this* project's dependencies to sessions working on other projects. A wrong
interpreter that fails loudly here is better than one that succeeds quietly
somewhere it doesn't belong. Relaunching keeps the correction local to the
scripts that actually need it.

Why the command strings stay as ``python``
------------------------------------------
``.claude/test.json`` is shared by the Windows and Linux installs of the same
dual-boot machine. Hardcoding ``.venv/bin/python`` would fix one and break the
other, which needs ``.venv\\Scripts\\python.exe``. Resolving the interpreter here,
at runtime, keeps that file portable.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# Third-party packages every `bot` import chain reaches. All present => the
# running interpreter is already the right one.
#
# Two rather than one on purpose, because a single marker is a single point of
# false reassurance. `python-dotenv` is packaged by most distributions, so one
# unrelated `dnf install` could put it on the *system* interpreter and switch
# this whole module off without anyone touching it — after which a harness dies
# on the next missing package instead, with nothing left to explain why.
# `discord.py` is not distro-packaged, so the pair is only ever satisfied
# together by a real install of this project's dependencies.
_MARKERS = ("dotenv", "discord")

# Set before relaunching so a virtualenv that is itself broken cannot loop
# forever, and cleared again as soon as the dependencies are confirmed visible
# so the claim is not exported to child processes that never made the attempt.
_GUARD = "BOT_HARNESS_REEXEC"


def _worktree_parent(root: Path) -> Path | None:
    """The main checkout, when ``root`` is a git worktree — else ``None``.

    Build worktrees are created per instance and never carry their own
    ``.venv``, but chain verify steps run *inside* one, which is precisely
    where the harnesses get executed. So a worktree has to be able to borrow
    the parent checkout's interpreter, or this fixes nothing where it matters.

    A worktree's ``.git`` is a file holding
    ``gitdir: <main>/.git/worktrees/<name>``; the main checkout is the parent
    of that ``.git`` directory.
    """
    dotgit = root / ".git"
    if not dotgit.is_file():  # a normal checkout has a .git *directory*
        return None
    try:
        text = dotgit.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    # Usually absolute, but git writes a *relative* gitdir when
    # `worktree.useRelativePaths` is set, and that is relative to the worktree
    # -- not to the caller's cwd. Resolving also flattens the `..` segments,
    # without which the `.git` component below would never be seen as a name.
    gitdir = Path(text[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = root / gitdir
    gitdir = gitdir.resolve()
    for parent in gitdir.parents:
        if parent.name == ".git":
            return parent.parent
    return None


def _venv_python(root: Path) -> Path | None:
    """``root``'s virtualenv interpreter, if it exists and is runnable."""
    if os.name == "nt":
        candidate = root / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = root / ".venv" / "bin" / "python"
    # os.access is False for a path that does not exist, so this covers both.
    return candidate if os.access(candidate, os.X_OK) else None


def _same_dir(a: str | Path, b: str | Path) -> bool:
    """Do two paths name the same *directory*?

    Symlinks are resolved here, unlike in ``_is_same_interpreter`` below. That
    is safe for a directory and necessary for one: a repo reached through a
    symlinked parent gives a ``sys.prefix`` spelt differently from the one
    derived from ``__file__``, and they are still the same virtualenv.
    """
    if os.path.normcase(str(a)) == os.path.normcase(str(b)):
        return True
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(
            str(Path(b).resolve())
        )
    except OSError:
        return False


def _is_same_interpreter(interpreter: Path, root: Path) -> bool:
    """Are we already running ``interpreter``?

    The executable comparison deliberately does NOT resolve symlinks. A
    virtualenv's ``python`` is usually a *symlink to the system binary* — here
    ``.venv/bin/python`` and ``/usr/bin/python`` both resolve to
    ``/usr/bin/python3.14``. What makes an interpreter "the venv one" is the
    path it is invoked by, not the binary it ends at: that path is what
    ``sys.prefix`` is derived from, and therefore what decides whether the
    venv's packages are importable. Resolving symlinks before comparing erases
    exactly the distinction that matters, and this function would then report a
    match and skip the one relaunch that was needed.

    ``sys.prefix`` is the direct answer to the same question and is checked too,
    for the case where the venv was entered by some other path spelling.
    """
    if os.path.normcase(str(interpreter)) == os.path.normcase(
        os.path.abspath(sys.executable)
    ):
        return True
    return _same_dir(sys.prefix, root / ".venv")


def _missing_deps() -> list[str]:
    """Which of ``_MARKERS`` this interpreter cannot see."""
    missing = []
    for name in _MARKERS:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            # A parent package that is itself unimportable, or a name the
            # finder rejects — either way it is not usable from here.
            found = False
        if not found:
            missing.append(name)
    return missing


def _relaunch(interpreter: Path) -> None:
    """Restart this script under ``interpreter``. Does not return.

    On POSIX that is ``os.execv``: same pid, same cwd, same file descriptors,
    so whoever is waiting on us goes on waiting on the process that does the
    work.

    Windows has no such call. Its C runtime's ``execv`` *spawns* a new process
    and terminates this one, so the parent's wait returns immediately — with a
    success code — while the real script is only just starting, writing to
    handles nobody is reading any more. For a check harness that is the worst
    possible outcome: it would report a pass without having run. And this is
    not hypothetical, because ``.claude/test.json`` is shared with the Windows
    install of the same dual-boot machine, where the bot is launched the same
    way and so hits the same missing interpreter. Windows therefore waits on a
    child and forwards its exit code instead.
    """
    argv = [str(interpreter), *sys.argv]
    # Nothing should have been written yet — this module is imported first —
    # but flush before handing over rather than rely on that staying true.
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "nt":
        import subprocess

        # OSError propagates to the caller, same as execv's, so a launch that
        # fails falls through to the next candidate either way.
        sys.exit(subprocess.run(argv).returncode)
    os.execv(str(interpreter), argv)


def _bootstrap() -> None:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    missing = _missing_deps()
    if not missing:
        # Drop the guard once we know it is not needed. It only ever means
        # "this process already relaunched and it did not help", and leaving it
        # set would export that claim to every child — barring a script this
        # one spawns by a bare `python` from correcting itself, on the strength
        # of an attempt it never made.
        os.environ.pop(_GUARD, None)
        return

    if os.environ.get(_GUARD):
        # We already relaunched once and the dependencies are still missing.
        # Stop here and let the natural ImportError name what is absent.
        return

    # Re-exec replays ``sys.argv``, which only reconstructs the original command
    # when argv[0] is a script path. Under ``python -c`` it is "-c" and under a
    # REPL or piped stdin it is "" or "-", and replaying those would produce a
    # baffling "Argument expected for the -c option" instead of an import error.
    if not sys.argv or not os.path.isfile(sys.argv[0]):
        return

    roots = [root]
    parent = _worktree_parent(root)
    if parent is not None and parent != root:
        roots.append(parent)

    already_in: Path | None = None
    unlaunchable: list[str] = []
    for candidate_root in roots:
        interpreter = _venv_python(candidate_root)
        if interpreter is None:
            continue
        if _is_same_interpreter(interpreter, candidate_root):
            already_in = candidate_root / ".venv"  # relaunching would achieve nothing
            continue
        os.environ[_GUARD] = "1"
        try:
            _relaunch(interpreter)  # argv preserved; does not return
        except OSError as exc:
            # Executable by the permission bits but not actually runnable -- a
            # noexec mount, a stale venv pointing at a deleted base interpreter.
            # Try the next candidate rather than dying on a traceback about
            # exec, which says nothing about the missing dependency.
            del os.environ[_GUARD]
            unlaunchable.append(f"{interpreter} ({exc.strerror or exc})")

    # Three different dead ends arrive here and they call for three different
    # sentences. Reporting "no virtualenv was found" to someone who is standing
    # in one -- running .venv/bin/python against a venv missing a package -- is
    # the kind of wrong hint that costs an hour.
    if already_in is not None:
        reason = f"the virtualenv it is already running from ({already_in}) lacks it"
    elif unlaunchable:
        reason = "no virtualenv could be started (" + "; ".join(unlaunchable) + ")"
    else:
        looked_in = ", ".join(str(r / ".venv") for r in roots)
        reason = f"no virtualenv was found (looked in: {looked_in})"
    sys.stderr.write(
        f"[_bootstrap] {sys.executable} cannot import "
        f"{', '.join(repr(name) for name in missing)} and {reason}. "
        f"Falling through — the import error below is the real one.\n"
    )


_bootstrap()
