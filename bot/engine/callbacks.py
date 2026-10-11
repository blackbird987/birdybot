"""Button callbacks: one dispatcher from a callback id to the handler that owns it."""

from __future__ import annotations

import logging
from pathlib import Path

from bot import config
from bot.claude.types import Instance, merge_msg_is_failure
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
    merge_failed_banner,
    mode_label,
    running_button_specs,
)
from bot.engine.branch_actions import (
    _finish_branch_action,
    _finish_resolved_branch,
    perform_discard,
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


async def handle_callback(
    ctx: RequestContext,
    action: str,
    instance_id: str,
    source_msg_id: str | None = None,
) -> None:
    """Dispatch a button callback action."""
    if action == "kill":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        await perform_kill(ctx, inst, source_msg_id)

    elif action == "retry":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        started = await _start_retry(ctx, inst, source_msg_id)
        if not started:
            return
        new_inst, handle = started

        await lifecycle.run_instance(ctx, new_inst, handle=handle)
        await lifecycle.backfill_thread_session(ctx, new_inst)

    elif action == "log":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        if inst.result_file and Path(inst.result_file).exists():
            await ctx.messenger.send_file(
                ctx.channel_id, inst.result_file, f"{inst.id}.md",
                caption=f"Full output for {inst.display_id()}",
            )
        else:
            text = inst.error or inst.summary or "No output recorded."
            escaped = ctx.messenger.escape(text)
            if source_msg_id:
                await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, escaped)
            else:
                await ctx.messenger.send_text(ctx.channel_id, escaped)

    elif action == "diff":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        if inst.diff_file and Path(inst.diff_file).exists():
            await ctx.messenger.send_file(
                ctx.channel_id, inst.diff_file, f"{inst.id}.diff",
                caption=f"Diff for {inst.display_id()}",
            )
        else:
            await ctx.messenger.send_text(ctx.channel_id, "No diff available.")

    elif action == "share":
        await on_share(ctx, instance_id)

    elif action == "merge":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        # Resolver in flight: reject — user must Cancel resolver first.
        if _resolver_in_flight(ctx, inst.id):
            await ctx.messenger.send_text(
                ctx.channel_id,
                "Cannot retry merge while the resolver is running. Tap **Cancel** first.",
            )
            return
        _clear_chain_on_manual_finalize(ctx.store, inst.session_id, "Merge button")
        # Early guard: branch already cleared by a prior merge/discard
        if not inst.branch:
            msg = await ctx.runner.merge_branch(inst)  # returns "Already merged (...)"
            await _finish_resolved_branch(ctx, inst, msg, source_msg_id)
            return
        branch_name = inst.branch  # Save before merge clears it
        msg = await ctx.runner.merge_branch(inst)
        ctx.store.update_instance(inst)
        banner_suffix = ""
        # Clear stale branch refs on all sibling instances
        if branch_name and not merge_msg_is_failure(msg):
            ctx.store.clear_pending_merge(inst.id)
            workflows.clear_stale_branches(ctx.store, branch_name)
            from bot.engine.deploy import apply_post_merge_deploy
            await apply_post_merge_deploy(ctx.messenger, ctx.store, inst)
            # Apply "merged" tag before close (tag must land before archive)
            if ctx.on_merged:
                await ctx.on_merged()
        elif merge_msg_is_failure(msg):
            # Refresh the pending_merge failure_kind so the banner the user
            # sees next time matches this attempt's symptom, not the original
            # failure that opened the pending_merge entry.
            failure_kind = ctx.runner._last_merge_failure_kind.get(inst.id)
            ctx.store.set_pending_merge_failure_kind(inst.id, failure_kind)
            banner_suffix = f"\n\n{merge_failed_banner(failure_kind)}"
        # Escape the runner output but append the banner unescaped — its
        # **bold** + `code` markdown is intentional. Same escape model as the
        # /merge slash and resolve_merge follow-ups above.
        escaped = f"{ctx.messenger.escape(msg)}{banner_suffix}"
        await _finish_branch_action(ctx, inst, escaped, source_msg_id)

    elif action == "discard":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        # Early guard: branch already cleared by a prior merge/discard
        if not inst.branch:
            _clear_chain_on_manual_finalize(ctx.store, inst.session_id, "Discard button")
            outcome = await ctx.runner.discard_branch(inst)  # returns "Already discarded (...)"
            await _finish_resolved_branch(ctx, inst, outcome.message, source_msg_id)
            return
        outcome = await perform_discard(ctx, inst, "Discard button")
        escaped = ctx.messenger.escape(outcome.message)
        await _finish_branch_action(ctx, inst, escaped, source_msg_id)

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

    elif action == "expand":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
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

    elif action == "collapse":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        await _clear_expand_overflow(ctx, inst)
        collapsed = format_result_md(inst, preview=inst.read_result_text())
        buttons = action_button_specs(inst, show_expand=True)
        markup = ctx.messenger.markdown_to_markup(collapsed)
        if source_msg_id:
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, markup, buttons)
        else:
            await ctx.messenger.send_text(ctx.channel_id, markup, buttons)

    elif action == "plan":
        await workflows.on_plan(ctx, instance_id, source_msg_id)
    elif action == "build":
        await workflows.on_build(ctx, instance_id, source_msg_id)
    elif action == "review_plan":
        await workflows.on_review_plan(ctx, instance_id, source_msg_id)
    elif action == "apply_revisions":
        await workflows.on_apply_revisions(ctx, instance_id, source_msg_id)
    elif action == "review_code":
        await workflows.on_review_code(ctx, instance_id, source_msg_id)
    elif action == "tldr":
        await workflows.on_tldr(ctx, instance_id, source_msg_id)
    elif action == "commit":
        await workflows.on_commit(ctx, instance_id, source_msg_id)
    elif action == "done":
        inst = ctx.store.get_instance(instance_id)
        if inst:
            _clear_chain_on_manual_finalize(ctx.store, inst.session_id, "Done button")
        await workflows.on_done(ctx, instance_id, source_msg_id)
    elif action == "autopilot":
        await workflows.on_autopilot(ctx, instance_id, source_msg_id)
    elif action == "autopilot_hold":
        await workflows.on_autopilot_hold(ctx, instance_id, source_msg_id)
    elif action == "build_and_ship":
        await workflows.on_build_and_ship(ctx, instance_id, source_msg_id)
    elif action == "continue_autopilot":
        inst = ctx.store.get_instance(instance_id)
        if inst:
            # Resume the stored chain verbatim — do NOT re-slice from a
            # default step list, which would re-inject "merge" into Hold chains.
            resumed = await workflows.resume_autopilot_chain(
                ctx, instance_id, source_msg_id, inst.session_id,
            )
            if resumed is None:
                await ctx.messenger.send_text(
                    ctx.channel_id, "No paused chain to continue.",
                )
        else:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")

    elif action == "amend":
        # Verify-release Amend: re-run the chain `done` step with the
        # verifier's rationale prepended, then re-run verify_release.
        await workflows.on_amend_done(ctx, instance_id, source_msg_id)

    elif action == "continue_anyway":
        # Verify-release Continue: clear needs_input on the verify_release
        # instance and resume the chain at the next step (release / merge).
        await workflows.on_continue_anyway(ctx, instance_id, source_msg_id)
    elif action == "sess_resume":
        await workflows.on_sess_resume(ctx, instance_id, source_msg_id)

    elif action == "continue_ppu":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        if not config.API_FALLBACK_ENABLED:
            await ctx.messenger.send_text(ctx.channel_id, "API fallback not configured.")
            return
        # Budget gate — refuse if daily cap exhausted
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
        # Create new instance with api_fallback flag
        new_inst = ctx.store.clone_instance_for_rerun(
            inst,
            name_suffix="ppu",
            origin_platform=ctx.platform,
            effort=ctx.effective_effort,
            model=None,
        )
        new_inst.api_fallback = True
        new_inst.cooldown_retries = 0
        ctx.store.update_instance(new_inst)
        if source_msg_id:
            try:
                await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, None)
            except Exception:
                pass
        escaped = ctx.messenger.escape(new_inst.display_id())
        handle = await ctx.messenger.send_thinking(
            ctx.channel_id,
            f"{escaped} continuing with {config.API_FALLBACK_MODEL} (pay-per-use)...",
            buttons=running_button_specs(new_inst.id),
        )
        if handle.get("message_id"):
            new_inst.message_ids.setdefault(ctx.platform, []).append(handle.get("message_id"))
            ctx.store.update_instance(new_inst)
        await lifecycle.run_instance(ctx, new_inst, handle=handle)
        # Pay-per-use is the manual twin of the cooldown auto-retry — it fires
        # from the same usage-limit card, so it needs the same backstop.
        await lifecycle.backfill_thread_session(ctx, new_inst)

    elif action in ("mode_explore", "mode_plan", "mode_build"):
        target = action.split("_", 1)[1]  # "explore", "plan", or "build"
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

        if actual == current:
            if actual != target and ctx.mode_ceiling:
                # Capped — always explain why, even on repeat taps
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"Mode: {mode_label(actual)} (capped — your ceiling is {mode_label(ctx.mode_ceiling)})",
                    silent=True,
                )
            return  # same effective mode, skip duplicate message

        msg = f"Mode: {mode_label(actual)}"
        if actual != target and ctx.mode_ceiling:
            msg += f" (capped — your ceiling is {mode_label(ctx.mode_ceiling)})"
        await ctx.messenger.send_text(ctx.channel_id, msg, silent=True)

    else:
        # The platform layer acknowledged the tap before dispatching here, so
        # falling off the end of this chain produces a button that visibly
        # does nothing.  Name the action instead — it is almost always a
        # button left over from an older build of the bot.
        log.warning("Unhandled button action %r (instance=%s)", action, instance_id)
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"That button (`{action}`) isn't wired up in this version of the bot.",
            silent=True,
        )
