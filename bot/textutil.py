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

import re


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


# --- Durations -------------------------------------------------------------
#
# ONE duration grammar, shared by everything that lets a session name a span of
# time: `/wake delay=`, `/watch timeout=`, and the collapsed directive chip that
# renders both back to the user. It lived in `bot.engine.watches` while /watch
# was its only caller; it moved here when /wake grew unit suffixes, because the
# chip renderer lives in `bot.platform` — upstream of `bot.engine` — and a leaf
# module is the only place all three can reach without closing a cycle.
#
# Anchored on purpose: "3days" is a typo, not three days, and must fall back
# rather than silently parse as its "3d" prefix.
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$", re.IGNORECASE)
_DURATION_MULT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(raw: str | None, default: int) -> int:
    """``"6h"`` / ``"90m"`` / ``"2d"`` / ``"3600"`` -> seconds. Garbage -> ``default``.

    Sessions write durations the way humans do, and a typo'd unit must not
    silently drop the request that carried it — every caller falls back to its
    own default instead. A bare number is seconds, so the older numeric-only
    spellings of both directives still parse identically.
    """
    if raw is None:
        return default
    m = _DURATION_RE.match(str(raw))
    if not m:
        return default
    try:
        return int(float(m.group(1)) * _DURATION_MULT[m.group(2).lower()])
    except (ValueError, KeyError, OverflowError):
        return default
