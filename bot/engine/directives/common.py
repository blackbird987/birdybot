"""Parsing every engine directive shares: strict key=value args and body pairing."""

from __future__ import annotations

import logging
import re

from bot.textutil import DIRECTIVE_KV_RE, find_tilde_block, is_quoted_example, mask_tilde_bodies

log = logging.getLogger(__name__)


def parse_kv(args_str: str) -> dict[str, str] | None:
    """Parse `key=val key="quoted val"` into a dict.

    Returns None if any input bytes are left unparsed (catches malformed
    input like trailing junk). No values reach a shell: `repo` is
    whitelisted against the registry, `mode`/`effort` are enum-checked,
    `title` is a Discord thread topic, and `body` is the prompt, so the
    over-broad shell-metachar check that used to live here rejected
    legitimate titles containing `()` `{}` `!`.
    """
    out: dict[str, str] = {}
    consumed_end = 0
    for m in DIRECTIVE_KV_RE.finditer(args_str):
        # Reject if there's non-whitespace junk between matches.
        if args_str[consumed_end:m.start()].strip():
            return None
        key = m.group(1)
        val = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else m.group(4)
        )
        out[key] = val
        consumed_end = m.end()
    # Reject if there's non-whitespace tail.
    if args_str[consumed_end:].strip():
        return None
    return out


def pair_directives(
    result_text: str, directive_re: re.Pattern[str], tag: str, cap: int,
) -> tuple[list[tuple[str, str]], int, int]:
    """Pair each directive with its own adjacent ``~~~<tag>`` body.

    A directive's body must appear between it and the next directive, so a
    single body can never satisfy two directives. Directives on quoted lines
    are skipped silently.

    Returns ``(pairs, no_body, over_cap)``: ``pairs`` is the ordered list of
    ``(args_str, body)`` ready for dispatch (capped at ``cap``), ``no_body``
    counts directives that lacked their own body block, ``over_cap`` counts
    directives dropped by the cap.
    """
    # Directives are found on the masked text, so one written inside another
    # block (a /spawn inside a child's brief) is neither dispatched here nor
    # counted as the next directive that ends this one's region.
    scan = mask_tilde_bodies(result_text)
    matches = list(directive_re.finditer(scan))
    pairs: list[tuple[str, str]] = []
    no_body = 0
    over_cap = 0
    for i, m in enumerate(matches):
        if is_quoted_example(scan, m.start()):
            log.debug("BOT_CMD /%s skipped: a quoted example", tag)
            continue
        if len(pairs) >= cap:
            over_cap += 1
            continue
        region_end = (
            matches[i + 1].start() if i + 1 < len(matches) else len(result_text)
        )
        span = find_tilde_block(result_text, tag, m.end(), region_end)
        if span is None:
            no_body += 1
            continue
        pairs.append((m.group(1).strip(), result_text[span[0]:span[1]].strip()))
    return pairs, no_body, over_cap
