"""The /chain directive: a chat session handing its plan to the build-to-ship chain."""

from __future__ import annotations

import asyncio
import logging
import re

from bot.textutil import find_tilde_block, is_quoted_example, mask_tilde_bodies
from bot.claude.types import Instance
from bot.engine import (
    prior_art,
    workflows,
)
from bot.platform.base import RequestContext
from bot.engine.cmd_state import _get_channel_lock
from bot.engine.directives.spawn import _SPAWN_KV_RE

log = logging.getLogger(__name__)


# --- /chain directive (Tier-2 BOT_CMD — assistant-issued build→ship handoff) ---

# preset= is optional; body is a ~~~plan ... ~~~ fenced block (tilde fences,
# same rationale as /spawn). One chain per response.
_CHAIN_DIRECTIVE_RE = re.compile(r'\[BOT_CMD:\s*/chain(?:\s+(.+?))?\]')
_CHAIN_ALLOWED_PRESETS = frozenset(workflows._CHAIN_PRESETS)
# Plan body cap — same 32 KiB ceiling as /spawn bodies.
_CHAIN_PLAN_MAX_BYTES = 32 * 1024


def _parse_chain_preset(args_str: str) -> str | None:
    """Extract a valid preset from a /chain directive's args, or None.

    Empty/absent args → None (means "use the repo's autonomy policy default,
    falling back to ship"). An explicit but invalid preset also returns None
    with a warning so a typo degrades to the default rather than silently
    doing nothing.
    """
    args_str = (args_str or "").strip()
    if not args_str:
        return None
    m = _SPAWN_KV_RE.search(args_str)
    if m:
        key = m.group(1)
        val = m.group(2) or m.group(3) or m.group(4) or ""
        if key == "preset" and val in _CHAIN_ALLOWED_PRESETS:
            return val
    # bare "ship"/"hold"/"verify" without preset= is also accepted
    token = args_str.split()[0]
    if token in _CHAIN_ALLOWED_PRESETS:
        return token
    log.warning("BOT_CMD /chain — unrecognized preset args: %r", args_str)
    return None


def _extract_chain_directive(result_text: str) -> tuple[str | None, str] | None:
    """Find the first REAL (unquoted) /chain directive + its ~~~plan body.

    Returns ``(preset, plan_body)`` or None if there's no usable directive.
    ``preset`` may be None (→ policy default). Directives on quoted/code/heading
    lines are skipped (same guard as /spawn and /wake).
    """
    scan = mask_tilde_bodies(result_text)
    for m in _CHAIN_DIRECTIVE_RE.finditer(scan):
        if is_quoted_example(scan, m.start()):
            log.debug("BOT_CMD /chain skipped: a quoted example")
            continue
        preset = _parse_chain_preset(m.group(1) or "")
        span = find_tilde_block(result_text, "plan", m.end())
        body = result_text[span[0]:span[1]].strip() if span else ""
        if len(body.encode("utf-8", "ignore")) > _CHAIN_PLAN_MAX_BYTES:
            body = body.encode("utf-8", "ignore")[:_CHAIN_PLAN_MAX_BYTES].decode(
                "utf-8", "ignore",
            )
        return preset, body
    return None


async def _handle_chain_directive(
    ctx: RequestContext, result_text: str, source_inst: "Instance",
) -> None:
    """Launch a build→ship chain from a /chain directive.

    Guards: needs a session; refuses if a chain is already active on this
    session (the paused/running chain owns the thread). Stores the plan body
    as the session's chain-plan override so the build step injects it, then
    dispatches the chain in a detached task that acquires the channel lock
    (mirrors app._do_cooldown_retry — the launching turn finalizes normally,
    then the chain picks up the lock). Never blocks the finalize path.
    """
    parsed = _extract_chain_directive(result_text)
    if parsed is None:
        return
    preset, plan_body = parsed

    session_id = source_inst.session_id
    if not session_id:
        log.warning("BOT_CMD /chain — no session_id on source instance; ignoring")
        return

    if ctx.store.get_autopilot_chain(session_id):
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                "A chain is already running on this thread — ignoring the "
                "new /chain directive. Let it finish or resolve the pause first.",
                silent=True,
            )
        except Exception:
            pass
        return

    # Resolve preset: explicit wins; else the repo's autonomy policy picks a
    # sensible default (ship repos → ship, else hold), falling back to ship.
    if preset is None:
        from bot.engine.workflows import load_workflow_policy
        policy = load_workflow_policy(source_inst.repo_path)
        preset = "ship" if policy["autonomy"] in ("merge", "ship") else "hold"

    if plan_body:
        # What was already tried in the files this plan changes rides into
        # the build with it. The chat session was told to check before
        # emitting (CHAIN_CONTEXT); this is the deterministic half, for the
        # turn that did not. Fails open: no history, the plan goes alone.
        block = await asyncio.to_thread(
            prior_art.collect, source_inst.repo_path, plan_body,
        )
        ctx.store.set_chain_plan_override(
            session_id, prior_art.attach(plan_body, block),
        )

    log.info(
        "BOT_CMD /chain — launching preset=%s session=%s (plan %d chars)",
        preset, session_id, len(plan_body),
    )

    async def _deferred_chain() -> None:
        from bot.engine import workflows
        lock = _get_channel_lock(ctx.channel_id)
        async with lock:
            try:
                await workflows.on_conversational_chain(
                    ctx, source_inst.id, preset,
                )
            except Exception:
                log.exception(
                    "Conversational chain failed for session %s", session_id,
                )

    try:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Starting **{preset}** chain from our plan — I'll build, review, "
            f"verify"
            + (", release and merge automatically." if preset == "ship"
               else " and release." if preset == "hold"
               else "."),
            silent=True,
        )
    except Exception:
        pass
    asyncio.create_task(_deferred_chain())
