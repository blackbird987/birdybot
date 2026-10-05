"""Branch-name helpers shared by the runner and the workflow engine.

Centralizes the parsing of ``git branch --list`` decorations and any other
sources of branch-name strings in the codebase. Existed because the
``+`` prefix that git emits for branches checked out in linked worktrees
slipped past the orphan-cleanup membership check, causing every active
build branch to be misclassified as an orphan on every restart.
"""

from __future__ import annotations


def canonical_branch(s: str | None) -> str | None:
    """Strip whitespace and ``git branch --list`` decorations from a name.

    Recognised decorations: ``* `` (current HEAD), ``+ `` (checked out in a
    linked worktree), and any leading whitespace. Rejects empty strings,
    detached-HEAD placeholders (``(HEAD detached at ...)``), and names
    containing internal whitespace — real git branch names never contain
    spaces, so an internal space means a parser bug or hostile input.

    Returns ``None`` for any input that fails validation; callers treat
    ``None`` as "not a usable branch name".
    """
    if not s:
        return None
    cleaned = s.strip()
    if not cleaned:
        return None
    # Strip a leading decoration character (`*` or `+`) plus optional space.
    if cleaned[0] in "*+":
        cleaned = cleaned[1:].lstrip()
    if not cleaned:
        return None
    if cleaned.startswith("("):
        # Detached-HEAD placeholders like "(HEAD detached at abc1234)"
        return None
    if any(ch.isspace() for ch in cleaned):
        return None
    return cleaned


def clear_stale_branches(store, branch_name: str) -> int:
    """Clear branch/worktree_path on ALL instances sharing a branch name.

    Also nulls the branch field in history.jsonl so resumed sessions don't
    see stale branch refs in their system prompt.

    Returns the number of instances updated.

    Lives here rather than in either caller because the runner's startup
    auto-merge and the workflow engine both need it, and bot.claude cannot
    import bot.engine.  It was a byte-identical copy in each until 2026-09-09.
    """
    count = 0
    for inst in store.list_instances(all_=True):
        if inst.branch == branch_name:
            inst.branch = None
            inst.worktree_path = None
            store.update_instance(inst)
            count += 1
    try:
        from bot.store import history as history_mod
        history_mod.clear_branch(branch_name)
    except Exception:
        pass
    return count
