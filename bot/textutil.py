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


# --- Tilde-fenced directive bodies -----------------------------------------
#
# Every bot directive carries its payload in a tilde block (``~~~spawn``,
# ``~~~wake``, ``~~~plan``, ...), and a body can legitimately contain another
# directive with its own block: a parent's /spawn brief that tells the child
# how to arm a /wake is the ordinary case. Two things went wrong with that
# while each parser used a flat ``~~~tag\n(.*?)\n~~~`` regex and scanned the
# whole text for directives:
#
# * the body stopped at the FIRST ``~~~`` line, which was the nested block's
#   closer, so the child received half its brief;
# * the nested directive was read as the PARENT's, so the parent armed the
#   wake it had only written down for the child.
#
# Both are the same missing fact, which block a line is inside, so one
# depth-aware scanner answers it for every parser. An opener is a line that is
# ``~~~`` plus a tag; a closer is a line that is exactly ``~~~``. A block left
# open runs to the end of the text, the way an unclosed Markdown fence does.
_TILDE_OPEN_RE = re.compile(r"[ \t]*~~~([A-Za-z][\w-]*)[ \t]*\r?")
_TILDE_CLOSE_RE = re.compile(r"[ \t]*~~~[ \t]*\r?")


def _tilde_lines(text: str, start: int):
    """Yield ``(line_start, line_end, kind, tag)`` for each line from ``start``.

    ``start`` is snapped back to the start of its line. ``kind`` is "open",
    "close" or "" for an ordinary line; ``line_end`` is the index of the
    newline (or ``len(text)``).
    """
    pos = text.rfind("\n", 0, start) + 1 if start > 0 else 0
    n = len(text)
    while pos <= n:
        nl = text.find("\n", pos)
        end = n if nl == -1 else nl
        line = text[pos:end]
        if "~~~" in line:
            mo = _TILDE_OPEN_RE.fullmatch(line)
            if mo:
                yield pos, end, "open", mo.group(1)
            elif _TILDE_CLOSE_RE.fullmatch(line):
                yield pos, end, "close", ""
            else:
                yield pos, end, "", ""
        else:
            yield pos, end, "", ""
        if nl == -1:
            return
        pos = nl + 1


def find_tilde_block(
    text: str, tag: str, start: int = 0, end: int | None = None,
) -> tuple[int, int, int] | None:
    """Locate the first top-level ``~~~<tag>`` block opening in ``[start, end)``.

    Returns ``(body_start, body_end, block_end)``: ``text[body_start:body_end]``
    is the body, and ``block_end`` is the index just past the closing ``~~~``.
    ``None`` when there is no such opener, or the block is never closed (a
    truncated block has no trustworthy end, so it has no body either).

    Nesting is honoured both ways: a ``~~~<tag>`` inside some other block
    between ``start`` and the target is not the target, and a block nested
    inside the target does not end it.
    """
    if not text or "~~~" not in text:
        return None
    limit = len(text) if end is None else end
    depth = 0
    body_start = -1
    for line_start, line_end, kind, found in _tilde_lines(text, start):
        if body_start < 0:
            if line_start >= limit:
                return None
            if kind == "open":
                if depth == 0 and found == tag and line_start >= start:
                    body_start = min(line_end + 1, len(text))
                    depth = 1
                else:
                    depth += 1
            elif kind == "close" and depth > 0:
                depth -= 1
            continue
        if kind == "open":
            depth += 1
        elif kind == "close":
            depth -= 1
            if depth == 0:
                # The body excludes the newline in front of the closer, as
                # the regexes this replaced did.
                body_end = max(body_start, line_start - 1)
                return body_start, body_end, line_end
    return None


def mask_tilde_bodies(text: str) -> str:
    """Blank the inside of every top-level tilde block, keeping every offset.

    Directive scanners run on the result, so a directive quoted inside
    another directive's body (a /wake written into a /spawn brief for the
    child) is invisible to them, while the opener and closer lines survive
    and positions still index the original text. Body characters become
    spaces and newlines are kept, so line-based guards see the same lines.
    An unclosed block is masked to the end of the text.
    """
    if not text or "~~~" not in text:
        return text or ""
    depth = 0
    body_start = 0
    spans: list[tuple[int, int]] = []
    for line_start, line_end, kind, _tag in _tilde_lines(text, 0):
        if kind == "open":
            if depth == 0:
                body_start = min(line_end + 1, len(text))
            depth += 1
        elif kind == "close" and depth > 0:
            depth -= 1
            if depth == 0:
                spans.append((body_start, line_start))
    if depth > 0:
        spans.append((body_start, len(text)))
    if not spans:
        return text
    chars = list(text)
    for a, b in spans:
        for i in range(a, b):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)
