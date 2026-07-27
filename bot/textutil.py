"""Leaf string helpers, importable from any layer.

Deliberately depends on nothing inside `bot`. `clip` is needed by both the
runner (trimming history entries for the system prompt) and the platform
formatters (trimming rows for Discord), and those two packages sit on opposite
sides of a one-way dependency: `bot.platform` imports `bot.claude`, never the
reverse. Housing a shared helper in either one would invert that edge and
force a function-local import to dodge the resulting cycle.
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
    """
    return " ".join(str(value or "").split())
