"""Leaf string helpers, importable from any layer.

Deliberately depends on nothing inside `bot`. These helpers are needed from
`bot.store` (trimming history entries for the system prompt) and `bot.engine`
(trimming rows for Discord), and the packages that would otherwise host them
sit downstream of both: `bot.platform` imports `bot.claude`, which imports
`bot.store`, so putting a shared string helper in either would close a cycle.
Housing them in `bot.store.history` instead would technically import, but
"import the text clipper from the history store" is a lie about what the
function is. A leaf module with no `bot` imports is the only honest home.
"""

from __future__ import annotations


def clip(text: str, limit: int) -> str:
    """Shorten text to at most `limit` chars, cutting on a word boundary.

    A hard slice routinely severs a word ("**What I") and leaves whoever reads
    it — model or human — a fragment to guess at. Falls back to a hard cut only
    when there is no whitespace to break on inside the limit.
    """
    if not text or len(text) <= limit:
        return text or ""
    if limit <= 1:
        # max(0, ...) so a negative limit can't slice from the END and return
        # something longer than asked for.
        return text[: max(0, limit)]
    head = text[: limit - 1]
    cut = head.rfind(" ")
    # Only honour the word boundary if it isn't throwing away most of the text.
    if cut > limit * 0.6:
        head = head[:cut]
    return head.rstrip(" ,.;:-") + "…"


def flatten(value: object) -> str:
    """Collapse any whitespace run (including newlines) to single spaces.

    Free-form markdown rendered into a single line of a list needs this, or an
    embedded newline silently splits one entry into two.

    Only ``None`` becomes empty. A falsy-but-real value (``0``, ``False``) is
    rendered, not swallowed — a shared helper that quietly drops a zero is a
    trap for the next caller even though today's callers only pass strings.
    """
    if value is None:
        return ""
    return " ".join(str(value).split())
