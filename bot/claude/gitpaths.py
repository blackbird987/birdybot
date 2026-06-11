"""Resolve real git paths for registered repo directories.

A registered repo dir is not necessarily the git working-tree root —
projects can be registered at a subdirectory of a larger repo (e.g.
``DegenAI/AIAgent/AIAgent`` inside the ``DegenAI/AIAgent`` repo).
Git commands accept any subdirectory as cwd, but two things do NOT
follow the cwd:

- ``git status --porcelain`` paths are relative to the *toplevel*, so
  feeding them back as pathspecs from a subdirectory cwd misses every
  file ("pathspec did not match any file(s) known to git").
- ``.git`` does not live at ``<registered dir>/.git`` — writing there
  (e.g. ``info/attributes``) creates a junk directory git never reads.

Every caller that builds a pathspec or touches ``.git`` internals must
go through these helpers instead of assuming the registered dir is the
repo root.
"""

import logging
import subprocess
from pathlib import Path

from bot import config

log = logging.getLogger(__name__)

_NOWND: dict = config.NOWND


def git_toplevel(repo: str) -> str | None:
    """Absolute working-tree root for ``repo``, or None if unresolvable."""
    return _rev_parse(repo, "--show-toplevel")


def git_dir(repo: str) -> str | None:
    """Absolute ``.git`` directory for ``repo``, or None if unresolvable.

    For a linked worktree this is the per-worktree gitdir
    (``.git/worktrees/<name>``), which is where MERGE_HEAD lives —
    exactly what merge-state checks need.  For *shared* paths
    (``info/attributes``, ``worktrees/<name>`` metadata) use
    :func:`git_common_dir` instead.
    """
    return _rev_parse(repo, "--absolute-git-dir")


def git_common_dir(repo: str) -> str | None:
    """Absolute common gitdir for ``repo``, or None if unresolvable.

    The common dir holds state shared across all worktrees of a repo:
    ``info/attributes``, ``worktrees/<name>/`` metadata, refs, tags.
    Identical to :func:`git_dir` for a main repo; differs inside a
    linked worktree.
    """
    out = _rev_parse(repo, "--git-common-dir")
    if out is None:
        return None
    # Unlike --absolute-git-dir, --git-common-dir may print a path
    # relative to the subprocess cwd (e.g. ".git" at the toplevel).
    p = Path(out)
    if not p.is_absolute():
        try:
            p = (Path(repo) / p).resolve()
        except OSError:
            return None
    return str(p)


def git_dir_stat(repo: str) -> str | None:
    """Stat-only variant of :func:`git_dir` for subprocess-averse hot paths.

    Walks ``repo`` and its parents for a ``.git`` directory.  Does NOT
    resolve ``.git`` *files* (linked-worktree indirection) — callers pass
    registered main-repo dirs, where ``.git`` is always a real directory.
    """
    try:
        p = Path(repo).resolve()
    except OSError:
        return None
    for candidate in (p, *p.parents):
        gd = candidate / ".git"
        try:
            if gd.is_dir():
                return str(gd)
        except OSError:
            return None
    return None


def _rev_parse(repo: str, flag: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", repo, "rev-parse", flag],
            capture_output=True, text=True, timeout=10, **_NOWND,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.warning("git rev-parse %s failed in %s", flag, repo, exc_info=True)
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None
