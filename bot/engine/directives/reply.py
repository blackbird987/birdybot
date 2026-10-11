"""The /reply directive: a parent answering a child it spawned."""

from __future__ import annotations

import logging
import re

from bot.textutil import find_tilde_block, is_quoted_example, mask_tilde_bodies
from bot.platform.base import RequestContext
from bot.engine.directives.spawn import _parse_spawn_kv

log = logging.getLogger(__name__)


# --- /reply directive (Tier-2 BOT_CMD — parent answers its own child) -------
#
# A spawned child that stops to ask a question used to sit there until the
# human noticed. The parent wrote that child's brief, so it is the natural
# authority to answer — this directive gives it a way to, without going
# through the user at all.
#
# The blast radius is deliberately tiny: the target must be a thread THIS
# session spawned. That roster (`Instance.spawn_dispatched_thread_ids`) is
# written by the /spawn dispatcher itself, so a session cannot talk its way
# into an arbitrary thread by naming an id.

_REPLY_DIRECTIVE_RE = re.compile(r'\[BOT_CMD:\s*/reply\s+(.+?)\]')
_REPLY_ALLOWED_KEYS = {"thread"}
_REPLY_BODY_MAX_BYTES = 32 * 1024
# Bounds one confused turn without blocking a parent answering a whole wave of
# blocked children at once.
_MAX_REPLIES_PER_RESPONSE = 5


def _own_child_thread_ids(ctx: RequestContext) -> set[str]:
    """Thread ids of every child this session has spawned, across all waves.

    Spans waves on purpose: a child blocked during wave 1 may only get its
    answer while the parent is mid-wave-2, and refusing that would leave the
    stuck child exactly as stuck as before.
    """
    if not ctx.session_id:
        return set()
    owned: set[str] = set()
    try:
        for inst in ctx.store.list_instances(all_=True):
            if inst.session_id == ctx.session_id:
                owned.update(inst.spawn_dispatched_thread_ids)
    except Exception:
        log.exception("failed to collect own child thread ids")
    return owned


def _pair_reply_directives(
    result_text: str,
) -> tuple[list[tuple[str, str]], int, int]:
    """Pair each /reply directive with its own adjacent ~~~reply body.

    Same contract as _pair_spawn_directives: a body must sit between its
    directive and the next one, so two directives can never share a body,
    and directives on quoted/code lines are skipped.
    """
    # Directives are found on the masked text, so one written inside another
    # block (a /spawn inside a child's brief) is neither dispatched here nor
    # counted as the next directive that ends this one's region.
    scan = mask_tilde_bodies(result_text)
    matches = list(_REPLY_DIRECTIVE_RE.finditer(scan))
    pairs: list[tuple[str, str]] = []
    no_body = 0
    over_cap = 0
    for i, m in enumerate(matches):
        if is_quoted_example(scan, m.start()):
            log.debug("BOT_CMD /reply skipped: a quoted example")
            continue
        if len(pairs) >= _MAX_REPLIES_PER_RESPONSE:
            over_cap += 1
            continue
        region_end = (
            matches[i + 1].start() if i + 1 < len(matches) else len(result_text)
        )
        span = find_tilde_block(result_text, "reply", m.end(), region_end)
        if span is None:
            no_body += 1
            continue
        pairs.append((m.group(1).strip(), result_text[span[0]:span[1]].strip()))
    return pairs, no_body, over_cap


async def _handle_reply_directives(ctx: RequestContext, result_text: str) -> None:
    """Dispatch every [BOT_CMD: /reply thread=<id>] directive in a response."""
    if not result_text or "/reply" not in result_text:
        return
    pairs, no_body, over_cap = _pair_reply_directives(result_text)
    if not pairs and not no_body and not over_cap:
        return
    if ctx.reply_to_child is None:
        log.warning("BOT_CMD /reply — platform has no reply_to_child callback; ignoring")
        return

    async def _notice(text: str) -> None:
        try:
            await ctx.messenger.send_text(ctx.channel_id, text, silent=True)
        except Exception:
            pass

    if no_body:
        log.warning("BOT_CMD /reply — %d directive(s) had no adjacent body", no_body)
        await _notice(
            f"{no_body} /reply directive(s) ignored — each directive must be "
            "immediately followed by its own `~~~reply ... ~~~` body block.",
        )
    if over_cap:
        log.warning("BOT_CMD /reply — %d directive(s) beyond cap", over_cap)
        await _notice(
            f"Only the first {_MAX_REPLIES_PER_RESPONSE} /reply directives per "
            f"response — {over_cap} ignored.",
        )
    if not pairs:
        return

    owned = _own_child_thread_ids(ctx)
    for args_str, body in pairs:
        kv = _parse_spawn_kv(args_str)
        if kv is None or set(kv) - _REPLY_ALLOWED_KEYS or "thread" not in kv:
            await _notice(
                "/reply directive ignored — expected exactly "
                "`[BOT_CMD: /reply thread=<child_thread_id>]`.",
            )
            continue
        target = kv["thread"].strip().strip("<#>")
        if not target.isdigit():
            await _notice(
                f"/reply directive ignored — `{kv['thread']}` is not a thread id.",
            )
            continue
        if target not in owned:
            log.warning("BOT_CMD /reply blocked — %s is not a child of this session", target)
            await _notice(
                f"/reply refused — <#{target}> is not a session this thread "
                "spawned. You can only answer your own children.",
            )
            continue
        if not body:
            await _notice("/reply directive ignored — the `~~~reply` body was empty.")
            continue
        if len(body.encode("utf-8")) > _REPLY_BODY_MAX_BYTES:
            await _notice("/reply refused — reply body exceeds the 32 KiB limit.")
            continue
        try:
            delivered = await ctx.reply_to_child(target, body)
        except Exception:
            log.exception("BOT_CMD /reply — platform callback raised")
            await _notice(f"/reply failed — could not deliver into <#{target}>.")
            continue
        if delivered:
            await _notice(f"Answered child session <#{target}>.")
        else:
            await _notice(
                f"/reply failed — <#{target}> could not be resumed "
                "(archived, deleted, or already running).",
            )
