"""Branch-name canonicalization helpers.

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
