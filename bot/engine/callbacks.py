"""Button callbacks: one dispatcher from a callback id to the handler that owns it."""

from __future__ import annotations

import logging
from pathlib import Path

from bot import config
from bot.claude.types import Instance
from bot.engine import (
    lifecycle,
    workflows,
)
from bot.platform.base import RequestContext
from bot.platform.formatting import (
    action_button_specs,
    expanded_button_specs,
    format_expanded_result_chunks,
    format_result_md,
    merge_failed_button_specs,
    mode_label,
)
from bot.engine.branch_actions import (
    _finish_branch_action,
    _finish_resolved_branch,
    branch_action_slot,
    discard_progress_text,
    merge_progress_text,
    perform_discard,
    show_branch_progress,
)
from bot.engine.cmd_state import _clear_chain_on_manual_finalize
from bot.engine.instance_cmds import (
    _start_retry,
    on_new,
    on_share,
    perform_kill,
)
from bot.engine.resolver import (
    _on_resolve_cancel,
    _on_resolve_merge,
    _resolver_in_flight,
)

log = logging.getLogger(__name__)


# --- Callback dispatch ---

async def _clear_expand_overflow(ctx: RequestContext, inst: Instance) -> None:
    """Delete the follow-up messages a previous Expand posted.

    Expand spills a long result across extra messages; Collapse (or a second
    Expand) has to take them back down, or the thread accumulates orphaned
    halves of an answer nobody asked for twice.
    """
    if not inst.expand_msg_ids:
        return
    for msg_id in list(inst.expand_msg_ids):
        try:
            await ctx.messenger.delete_message(ctx.channel_id, msg_id)
        except Exception:
            log.debug("Could not delete expand overflow message %s", msg_id)
    inst.expand_msg_ids.clear()
    ctx.store.update_instance(inst)


async def _on_kill(ctx: RequestContext, inst: Instance, source_msg_id: str | None) -> None:
    await perform_kill(ctx, inst, source_msg_id)


async def _on_retry(ctx: RequestContext, inst: Instance, source_msg_id: str | None) -> None:
    started = await _start_retry(ctx, inst, source_msg_id)
    if not started:
        return
    new_inst, handle = started
    await lifecycle.run_instance(ctx, new_inst, handle=handle)
    await lifecycle.backfill_thread_session(ctx, new_inst)


async def _on_log(ctx: RequestContext, inst: Instance, source_msg_id: str | None) -> None:
    if inst.result_file and Path(inst.result_file).exists():
        await ctx.messenger.send_file(
            ctx.channel_id, inst.result_file, f"{inst.id}.md",
            caption=f"Full output for {inst.display_id()}",
        )
        return
    text = inst.error or inst.summary or "No output recorded."
    escaped = ctx.messenger.escape(text)
    if source_msg_id:
        await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, escaped)
    else:
        await ctx.messenger.send_text(ctx.channel_id, escaped)


async def _on_diff(ctx: RequestContext, inst: Instance, source_msg_id: str | None) -> None:
    if inst.diff_file and Path(inst.diff_file).exists():
        await ctx.messenger.send_file(
            ctx.channel_id, inst.diff_file, f"{inst.id}.diff",
            caption=f"Diff for {inst.display_id()}",
        )
    else:
        await ctx.messenger.send_text(ctx.channel_id, "No diff available.")


async def _on_merge(ctx: RequestContext, inst: Instance, source_msg_id: str | None) -> None:
    # Resolver in flight: reject, the user must Cancel the resolver first.
    if _resolver_in_flight(ctx, inst.id):
        await ctx.messenger.send_text(
            ctx.channel_id,
            "Cannot retry merge while the resolver is running. Tap **Cancel** first.",
        )
        return
    with branch_action_slot(inst.id) as claimed:
        if not claimed:
            # The first tap's progress line is already on the card.
            log.info("Merge tap on %s ignored: one is already running", inst.id)
            return
        _clear_chain_on_manual_finalize(ctx.store, inst.session_id, "Merge button")
        # Early guard: branch already cleared by a prior merge/discard
        if not inst.branch:
            msg = await ctx.runner.merge_branch(inst)  # returns "Already merged (...)"
            await _finish_resolved_branch(ctx, inst, msg, source_msg_id)
            return
        await show_branch_progress(ctx, source_msg_id, merge_progress_text(inst))
        # The "merged" tag lands inside merge_from_user, before the close.
        escaped, msg = await workflows.merge_from_user(ctx, inst)
        await _finish_branch_action(
            ctx, inst, escaped, source_msg_id,
            keep_open=workflows.merge_blocks_ship(msg),
            kept_buttons=merge_failed_button_specs(inst.id),
        )


async def _on_discard(ctx: RequestContext, inst: Instance, source_msg_id: str | None) -> None:
    with branch_action_slot(inst.id) as claimed:
        if not claimed:
            log.info("Discard tap on %s ignored: a merge or discard is running", inst.id)
            return
        # Early guard: branch already cleared by a prior merge/discard
        if not inst.branch:
            _clear_chain_on_manual_finalize(ctx.store, inst.session_id, "Discard button")
            outcome = await ctx.runner.discard_branch(inst)  # returns "Already discarded (...)"
            await _finish_resolved_branch(ctx, inst, outcome.message, source_msg_id)
            return
        await show_branch_progress(ctx, source_msg_id, discard_progress_text(inst))
        outcome = await perform_discard(ctx, inst, "Discard button")
        escaped = ctx.messenger.escape(outcome.message)
        await _finish_branch_action(ctx, inst, escaped, source_msg_id)


async def _on_expand(ctx: RequestContext, inst: Instance, source_msg_id: str | None) -> None:
    if not inst.result_file or not Path(inst.result_file).exists():
        await ctx.messenger.send_text(ctx.channel_id, "Result file not available.")
        return
    result_text = Path(inst.result_file).read_text(encoding="utf-8")
    # Expand posts the WHOLE result: chunk 0 replaces the card, the rest
    # follow as plain messages. Collapse deletes the follow-ups again.
    chunks = format_expanded_result_chunks(inst, result_text)
    buttons = expanded_button_specs(inst)
    await _clear_expand_overflow(ctx, inst)
    head = ctx.messenger.markdown_to_markup(chunks[0])
    if source_msg_id:
        await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, head, buttons)
    else:
        await ctx.messenger.send_text(ctx.channel_id, head, buttons)
    for extra in chunks[1:]:
        msg_id = await ctx.messenger.send_text(
            ctx.channel_id, ctx.messenger.markdown_to_markup(extra), silent=True,
        )
        if msg_id:
            inst.expand_msg_ids.append(str(msg_id))
    if chunks[1:]:
        ctx.store.update_instance(inst)


async def _on_collapse(ctx: RequestContext, inst: Instance, source_msg_id: str | None) -> None:
    await _clear_expand_overflow(ctx, inst)
    collapsed = format_result_md(inst, preview=inst.read_result_text())
    buttons = action_button_specs(inst, show_expand=True)
    markup = ctx.messenger.markdown_to_markup(collapsed)
    if source_msg_id:
        await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, markup, buttons)
    else:
        await ctx.messenger.send_text(ctx.channel_id, markup, buttons)


async def _on_continue_autopilot(
    ctx: RequestContext, inst: Instance, source_msg_id: str | None,
) -> None:
    # Resume the stored chain verbatim. Do NOT re-slice from a default step
    # list, which would re-inject "merge" into Hold chains.
    resumed = await workflows.resume_autopilot_chain(
        ctx, inst.id, source_msg_id, inst.session_id,
    )
    if resumed is None:
        await ctx.messenger.send_text(ctx.channel_id, "No paused chain to continue.")


async def _on_continue_ppu(
    ctx: RequestContext, inst: Instance, source_msg_id: str | None,
) -> None:
    if not config.API_FALLBACK_ENABLED:
        await ctx.messenger.send_text(ctx.channel_id, "API fallback not configured.")
        return
    # Budget gate: refuse if the daily cap is exhausted
    daily_spend = ctx.store.get_fallback_spend_today()
    if daily_spend >= config.API_FALLBACK_DAILY_MAX_USD:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"API fallback budget exhausted "
            f"(${daily_spend:.2f}/${config.API_FALLBACK_DAILY_MAX_USD:.2f} today). "
            "Waiting for free auto-retry instead.",
        )
        return
    # Cancel pending cooldown auto-retry
    inst.cooldown_retry_at = None
    inst.cooldown_channel_id = None
    ctx.store.update_instance(inst)
    started = await _start_retry(ctx, inst, source_msg_id, pay_per_use=True)
    if not started:
        return
    new_inst, handle = started
    await lifecycle.run_instance(ctx, new_inst, handle=handle)
    # Pay-per-use is the manual twin of the cooldown auto-retry: it fires
    # from the same usage-limit card, so it needs the same backstop.
    await lifecycle.backfill_thread_session(ctx, new_inst)


async def _on_mode(
    ctx: RequestContext, target: str, instance_id: str, source_msg_id: str | None,
) -> None:
    current = ctx.effective_mode
    ctx.update_mode(target)
    actual = ctx.effective_mode  # may be capped by mode_ceiling

    # Update source message buttons to reflect new mode
    inst = ctx.store.get_instance(instance_id)
    if inst and source_msg_id:
        inst.mode = actual
        ctx.store.update_instance(inst)
        # Third place that used to guess "is there more behind Expand?"
        # from the result file's size. The delivery path records it.
        show_expand = bool(inst.result_collapsed and inst.result_file)
        buttons = action_button_specs(inst, show_expand=show_expand)
        await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, None, buttons)

    capped = actual != target and ctx.mode_ceiling
    note = (f" (capped \u2014 your ceiling is {mode_label(ctx.mode_ceiling)})"
            if capped else "")
    if actual == current and not capped:
        return  # same effective mode, skip duplicate message
    # Capped: always explain why, even on repeat taps.
    await ctx.messenger.send_text(
        ctx.channel_id, f"Mode: {mode_label(actual)}{note}", silent=True,
    )


# Buttons that act on one recorded instance, which is looked up first: a
# missing one gets "Instance not found." and the handler never runs.
_INSTANCE_ACTIONS = {
    "kill": _on_kill,
    "retry": _on_retry,
    "log": _on_log,
    "diff": _on_diff,
    "merge": _on_merge,
    "discard": _on_discard,
    "expand": _on_expand,
    "collapse": _on_collapse,
    "continue_autopilot": _on_continue_autopilot,
    "continue_ppu": _on_continue_ppu,
}

# Workflow step buttons: action -> the ``workflows`` facade function it calls,
# by name. It is read off the facade at tap time, so a harness patching the
# facade (on_autopilot is one of the two names allowed) is still honoured.
# ``amend`` re-runs the chain's done step with the verifier's rationale, and
# ``continue_anyway`` clears needs_input on a verify_release and resumes.
_WORKFLOW_ACTIONS = {
    "plan": "on_plan",
    "build": "on_build",
    "review_plan": "on_review_plan",
    "apply_revisions": "on_apply_revisions",
    "review_code": "on_review_code",
    "tldr": "on_tldr",
    "commit": "on_commit",
    "autopilot": "on_autopilot",
    "autopilot_hold": "on_autopilot_hold",
    "build_and_ship": "on_build_and_ship",
    "amend": "on_amend_done",
    "continue_anyway": "on_continue_anyway",
    "sess_resume": "on_sess_resume",
}

_MODE_ACTIONS = {"mode_explore": "explore", "mode_plan": "plan", "mode_build": "build"}


async def handle_callback(
    ctx: RequestContext,
    action: str,
    instance_id: str,
    source_msg_id: str | None = None,
) -> None:
    """Dispatch a button callback action."""
    if action in _INSTANCE_ACTIONS:
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        await _INSTANCE_ACTIONS[action](ctx, inst, source_msg_id)

    elif action in _WORKFLOW_ACTIONS:
        step = getattr(workflows, _WORKFLOW_ACTIONS[action])
        await step(ctx, instance_id, source_msg_id)

    elif action == "done":
        inst = ctx.store.get_instance(instance_id)
        if inst:
            _clear_chain_on_manual_finalize(ctx.store, inst.session_id, "Done button")
        await workflows.on_done(ctx, instance_id, source_msg_id)

    elif action in _MODE_ACTIONS:
        await _on_mode(ctx, _MODE_ACTIONS[action], instance_id, source_msg_id)

    elif action == "share":
        await on_share(ctx, instance_id)

    elif action == "resolve_merge":
        await _on_resolve_merge(ctx, instance_id, source_msg_id)

    elif action == "resolve_cancel":
        await _on_resolve_cancel(ctx, instance_id, source_msg_id)

    elif action == "wait":
        if source_msg_id:
            await ctx.messenger.edit_text(
                ctx.channel_id, source_msg_id, "Waiting... process is still running.",
            )
        else:
            await ctx.messenger.send_text(ctx.channel_id, "Waiting... process is still running.")

    elif action == "new":
        await on_new(ctx)

    else:
        # The platform layer acknowledged the tap before dispatching here, so
        # falling off the end of this chain produces a button that visibly
        # does nothing. Name the action instead: it is almost always a
        # button left over from an older build of the bot.
        log.warning("Unhandled button action %r (instance=%s)", action, instance_id)
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"That button (`{action}`) isn't wired up in this version of the bot.",
            silent=True,
        )
