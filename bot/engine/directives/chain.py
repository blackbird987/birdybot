"""The /chain directive: a chat session handing its plan to the build-to-ship chain."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from bot.textutil import DIRECTIVE_KV_RE, directive_re, find_tilde_block, is_quoted_example, mask_tilde_bodies
from bot.claude.types import Instance
from bot.engine import (
    prior_art,
    workflows,
)
from bot.platform.base import RequestContext
from bot.engine.cmd_state import _get_channel_lock

log = logging.getLogger(__name__)


# --- /chain directive (Tier-2 BOT_CMD — assistant-issued build→ship handoff) ---

# preset= is optional; body is a ~~~plan ... ~~~ fenced block (tilde fences,
# same rationale as /spawn). One chain per response.
_CHAIN_DIRECTIVE_RE = directive_re("chain", args_optional=True)
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
    m = DIRECTIVE_KV_RE.search(args_str)
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

    Never refuses because of chain state already on the session. The plan is
    queued per session (newest wins) and a single drain task per session runs
    it under the channel lock (mirrors app._do_cooldown_retry: the launching
    turn finalizes normally, then the chain picks up the lock). So:

    - a second /chain before the first chain starts replaces the queued plan
      instead of starting a second chain (two directives two minutes apart
      once ran the newer plan, then an empty build that paused for 12h);
    - a /chain during a running chain runs right after it;
    - a paused or leftover chain found once the lock is held is superseded,
      because every chain path holds that lock while it runs.

    Never blocks the finalize path.
    """
    parsed = _extract_chain_directive(result_text)
    if parsed is None:
        return
    preset, plan_body = parsed

    session_id = source_inst.session_id
    if not session_id:
        log.warning("BOT_CMD /chain — no session_id on source instance; ignoring")
        return

    # Resolve preset: explicit wins; else the repo's autonomy policy picks a
    # sensible default (ship repos → ship, else hold), falling back to ship.
    if preset is None:
        from bot.engine.workflows import load_workflow_policy
        policy = load_workflow_policy(source_inst.repo_path)
        preset = "ship" if policy["autonomy"] in ("merge", "ship") else "hold"

    plan = ""
    if plan_body:
        # What was already tried in the files this plan changes rides into
        # the build with it. The chat session was told to check before
        # emitting (CHAIN_CONTEXT); this is the deterministic half, for the
        # turn that did not. Fails open: no history, the plan goes alone.
        block = await asyncio.to_thread(
            prior_art.collect, source_inst.repo_path, plan_body,
        )
        plan = prior_art.attach(plan_body, block)

    # Newest wins. The override is written by the drain once it holds the
    # lock, so a chain still running can't have it swapped mid-flight or
    # wiped by its own terminal clear_chain_state.
    _pending_chains[session_id] = (preset, plan, source_inst.id, ctx.channel_id)

    if session_id in _chain_drainers:
        log.info(
            "BOT_CMD /chain: queued newer plan preset=%s session=%s "
            "(plan %d chars) behind the chain already dispatched",
            preset, session_id, len(plan_body),
        )
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                "Got the newer plan. It replaces the one queued, or runs "
                "right after the chain in progress.",
                silent=True,
            )
        except Exception:
            pass
        return

    log.info(
        "BOT_CMD /chain — launching preset=%s session=%s (plan %d chars)",
        preset, session_id, len(plan_body),
    )
    _chain_drainers.add(session_id)
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
    asyncio.create_task(_drain_chains(ctx, session_id))


# session_id -> (preset, plan_with_prior_art, source_inst_id, channel_id).
# One slot per session: a newer /chain overwrites a plan not yet started.
_pending_chains: dict[str, tuple[str, str, str, str]] = {}
# Sessions with a drain task alive (waiting on the lock or running a chain).
_chain_drainers: set[str] = set()


def _format_age(updated_at: str | None) -> str:
    """'12h ago' style age of an ISO timestamp, or '' when unknown."""
    if not updated_at:
        return ""
    try:
        then = datetime.fromisoformat(updated_at)
    except (TypeError, ValueError):
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    secs = max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    if secs < 3600:
        return f"{max(1, secs // 60)}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _supersede_note(remaining: list[str], meta: dict | None) -> str:
    """One line telling the user a leftover chain was replaced."""
    status = (meta or {}).get("status") or "leftover"
    age = _format_age((meta or {}).get("updated_at"))
    parts = [f"Replaced a {status} chain"]
    if age:
        parts.append(f"from {age}")
    if remaining:
        parts.append(f"(stopped at {remaining[0]})")
    return " ".join(parts) + " with this one."


async def _drain_chains(ctx: RequestContext, session_id: str) -> None:
    """Run this session's queued /chain plans, one at a time, under the lock.

    Holding the channel lock means no chain is live on this session (every
    chain path runs under it), so any chain state still stored is a paused
    or crashed leftover and the newer plan supersedes it.
    """
    try:
        lock = _get_channel_lock(ctx.channel_id)
        async with lock:
            while True:
                entry = _pending_chains.pop(session_id, None)
                if entry is None:
                    break
                preset, plan, source_id, _channel = entry

                remaining = ctx.store.get_autopilot_chain(session_id)
                if remaining:
                    meta = ctx.store.get_autopilot_chain_meta(session_id)
                    note = _supersede_note(list(remaining), meta)
                    log.info(
                        "BOT_CMD /chain: superseding stored chain for "
                        "session %s (meta=%s, remaining=%s)",
                        session_id, meta, remaining,
                    )
                    ctx.store.clear_chain_state(session_id)
                    try:
                        await ctx.messenger.send_text(
                            ctx.channel_id, note, silent=True,
                        )
                    except Exception:
                        pass

                if plan:
                    ctx.store.set_chain_plan_override(session_id, plan)

                try:
                    await workflows.on_conversational_chain(
                        ctx, source_id, preset,
                    )
                except Exception:
                    log.exception(
                        "Conversational chain failed for session %s", session_id,
                    )
    finally:
        _chain_drainers.discard(session_id)
