"""Git reads the chain needs: which instance owns a branch, what a build changed, the Unreleased block."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from bot.procutil import run_capture
from bot.claude.gitpaths import git_dir_stat, ref_sha
from bot.claude.types import (
    Instance, InstanceOrigin,
    InstanceStatus,
)

log = logging.getLogger(__name__)


async def _git_head(path: str | None) -> str | None:
    """Return the current git HEAD SHA at *path*, or None if unavailable."""
    if not path or not Path(path).is_dir():
        return None
    return await asyncio.to_thread(ref_sha, path)


def _is_worktree_live(repo_path: str, worktree_path: str) -> bool:
    """Cheap validation that a worktree is registered with git, not just
    a leftover directory.

    git worktree creates ``<repo>/.git/worktrees/<basename>/`` metadata when
    a worktree is registered, and removes that dir when the worktree is
    unregistered (``git worktree remove``).  Checking both the worktree dir
    and that metadata catches the case where the dir survived but git no
    longer recognises it (manual rm, branch force-deleted, ``worktree
    remove`` followed by mkdir, etc.) — preventing the chain-detector from
    handing a dead worktree path to a fresh build spawn.

    Pure stat — no subprocess.
    """
    if not repo_path or not worktree_path:
        return False
    try:
        if not Path(worktree_path).is_dir():
            return False
        basename = Path(worktree_path).name
        gd = git_dir_stat(repo_path)
        if gd is None:
            return False
        return (Path(gd) / "worktrees" / basename).is_dir()
    except OSError:
        return False


def _find_session_branch_instance(
    store, session_id: str | None,
    *, predicate=None,
) -> Instance | None:
    """Most recent instance in a session with a live branch + worktree.

    Filters newest-first to instances sharing ``session_id`` whose
    ``branch``/``original_branch``/``worktree_path`` are all set and whose
    worktree is still registered with git (`_is_worktree_live`).  An
    optional predicate narrows further (e.g. require completion, exclude
    running instances).  Shared between the merge-resume path and the
    chain-detector so they can't drift out of sync.
    """
    if not session_id:
        return None
    for inst in store.list_instances(all_=True):  # newest-first
        if inst.session_id != session_id:
            continue
        if not (inst.branch and inst.original_branch and inst.worktree_path):
            continue
        if not _is_worktree_live(inst.repo_path, inst.worktree_path):
            continue
        if predicate is not None and not predicate(inst):
            continue
        return inst
    return None


def _find_mergeable_instance(store, session_id: str | None) -> Instance | None:
    """Find a completed done instance with a branch, for merge-step resume."""
    return (
        _find_session_branch_instance(
            store, session_id,
            predicate=lambda i: (
                i.origin == InstanceOrigin.DONE
                and i.status == InstanceStatus.COMPLETED
            ),
        )
        or _find_session_branch_instance(store, session_id)
    )


def _find_recoverable_session_predecessor(
    store, session_id: str | None, repo_path: str | None,
) -> Instance | None:
    """Most recent session-mate instance whose branch+worktree dir survive
    but whose git worktree metadata is gone — a candidate for inline
    re-register before a Build click decides to fresh-master.

    Mirrors ``_find_session_branch_instance`` but inverts the live-worktree
    filter: we want the case where ``_is_worktree_live`` returns False
    specifically because of missing ``.git/worktrees/<name>/`` metadata,
    not because the worktree dir itself is gone (that's truly orphaned).
    Skip instances already flagged ``manual_recovery_needed`` — they were
    parked on a previous pass and the user hasn't intervened yet.
    """
    if not session_id or not repo_path:
        return None
    for inst in store.list_instances(all_=True):  # newest-first
        if inst.session_id != session_id:
            continue
        if inst.repo_path != repo_path:
            continue
        if inst.manual_recovery_needed:
            continue
        if not (inst.branch and inst.original_branch and inst.worktree_path):
            continue
        if _is_worktree_live(inst.repo_path, inst.worktree_path):
            continue
        # Working dir present but git metadata missing → recoverable
        wt_dir = Path(inst.worktree_path)
        if not wt_dir.is_dir():
            continue
        _gd = git_dir_stat(inst.repo_path)
        meta_dir = (
            Path(_gd) / "worktrees" / wt_dir.name if _gd
            else Path(inst.repo_path) / ".git" / "worktrees" / wt_dir.name
        )
        if meta_dir.is_dir():
            continue
        return inst
    return None


def _find_prior_build_for_chain(
    store, session_id: str | None, repo_path: str | None,
    runner=None,
) -> Instance | None:
    """Most recent COMPLETED build in this session+repo to chain a new
    build on top of.

    Returns None if there is no prior unmerged work, or if the only
    candidate is still running (status != COMPLETED, or the runner still
    has it as an active subprocess).  Callers fall back to a fresh
    branch-from-master spawn in that case.

    The status + active-subprocess gate is critical: ``inst.branch`` is
    set the moment a build instance is created, before its subprocess
    starts.  Without this filter, two rapid Build clicks would both
    chain the second click onto the still-running first build and
    write the same worktree directory simultaneously.
    """
    if not repo_path:
        return None
    active_ids = (
        set(runner.active_ids) if runner is not None else set()
    )

    def is_chainable(inst: Instance) -> bool:
        if inst.repo_path != repo_path:
            return False
        if inst.status != InstanceStatus.COMPLETED:
            return False
        if inst.id in active_ids:
            return False
        return True

    return _find_session_branch_instance(
        store, session_id, predicate=is_chainable,
    )


_DIFF_PAYLOAD_CAP = 20 * 1024  # 20KB before truncation

# Anchored regex: ## [Unreleased] header, capture until next ## header or EOF.
_UNRELEASED_RE = re.compile(
    r'^##\s*\[Unreleased\]\s*$\n(.*?)(?=^##\s|\Z)',
    re.DOTALL | re.MULTILINE,
)


# Sync twin of _git_head; a module attribute so the chain harness can patch it.
_git_head_sha = ref_sha


def _git_log_messages(repo_path: str, entry_sha: str) -> str:
    """Concatenated commit messages from entry_sha..HEAD (newest first)."""
    try:
        r = run_capture(["git", "log", f"{entry_sha}..HEAD", "--format=%B%x00"],
            cwd=repo_path, timeout=10, encoding='utf-8', errors='replace',
        )
        if r.returncode != 0:
            return ""
        # Split on NUL, strip empties
        parts = [p.strip() for p in (r.stdout or "").split("\x00") if p.strip()]
        return "\n\n---\n\n".join(parts)
    except Exception:
        log.warning("git log failed in %s", repo_path, exc_info=True)
        return ""


def _git_diff_stat(repo_path: str, entry_sha: str) -> str:
    try:
        r = run_capture(["git", "diff", "--stat", f"{entry_sha}..HEAD"],
            cwd=repo_path, timeout=10, encoding='utf-8', errors='replace',
        )
        return (r.stdout or "") if r.returncode == 0 else ""
    except Exception:
        return ""


def _git_diff_payload(repo_path: str, entry_sha: str) -> tuple[str, bool, list[str]]:
    """Return (diff_text, truncated, file_list).

    Truncation is at _DIFF_PAYLOAD_CAP bytes; file_list is the full
    `git diff --name-only` so a downstream pass can re-fetch any file
    that fell outside the window.
    """
    try:
        r = run_capture(["git", "diff", f"{entry_sha}..HEAD"],
            cwd=repo_path, timeout=15, encoding='utf-8', errors='replace',
        )
        diff = (r.stdout or "") if r.returncode == 0 else ""
    except Exception:
        diff = ""

    truncated = False
    if len(diff) > _DIFF_PAYLOAD_CAP:
        diff = diff[:_DIFF_PAYLOAD_CAP] + "\n[TRUNCATED]"
        truncated = True

    files: list[str] = []
    try:
        rf = run_capture(["git", "diff", "--name-only", f"{entry_sha}..HEAD"],
            cwd=repo_path, timeout=10, encoding='utf-8', errors='replace',
        )
        if rf.returncode == 0:
            files = [ln.strip() for ln in (rf.stdout or "").splitlines() if ln.strip()]
    except Exception:
        pass

    return diff, truncated, files


def _read_unreleased_block(repo_path: str) -> str | None:
    """Return the raw text of the ## [Unreleased] block, or None if missing/malformed."""
    cl = Path(repo_path) / "CHANGELOG.md"
    if not cl.exists():
        return None
    try:
        text = cl.read_text(encoding="utf-8")
    except Exception:
        return None
    # Normalize \r\n / \r so the multiline regex anchors work consistently
    # regardless of how the file was committed.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    m = _UNRELEASED_RE.search(text)
    if not m:
        return None
    body = m.group(1).strip()
    if not body:
        return None
    return body


def _resolve_chain_repo_path(*candidates: Instance | None) -> str | None:
    """Pick the right cwd for git operations during a chain step.

    Walks candidates in order and prefers each one's worktree (where the
    chain is committing) over its bare repo path. Returns the first
    existing directory.
    """
    for cand in candidates:
        if not cand:
            continue
        if cand.worktree_path and Path(cand.worktree_path).is_dir():
            return cand.worktree_path
        if cand.repo_path and Path(cand.repo_path).is_dir():
            return cand.repo_path
    return None
