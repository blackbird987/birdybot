"""Instance execution, progress callbacks, result delivery, finalization."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timezone

from bot import config
from bot.claude.types import (
    CODE_CHANGE_TOOLS, PLAN_ORIGINS, Instance, InstanceOrigin, InstanceStatus,
    RunResult,
)
from bot.platform.base import MessageHandle, RequestContext
from bot.platform.formatting import (
    action_button_specs,
    format_duration,
    format_tokens,
    format_result_md,
    mode_name,
    parse_finalize_output,
    redact_secrets,
    running_button_specs,
    stall_button_specs,
    strip_summary_block,
)

log = logging.getLogger(__name__)

# Labels that don't auto-derive well from InstanceOrigin.value
_ORIGIN_LABEL_OVERRIDES: dict[InstanceOrigin, str] = {
    InstanceOrigin.DIRECT: "",
    InstanceOrigin.DONE: "wrap-up ",
}


def _origin_label(origin: InstanceOrigin) -> str:
    """Human-readable prefix for completion messages, e.g. 'review-code '."""
    if origin in _ORIGIN_LABEL_OVERRIDES:
        return _ORIGIN_LABEL_OVERRIDES[origin]
    return origin.value.replace("_", "-") + " "


def get_sibling_summary(store, inst: Instance) -> str | None:
    """Scan running instances in same repo, return summary for system prompt."""
    if not inst.repo_name:
        return None
    siblings = [
        i for i in store.list_instances()
        if i.repo_name == inst.repo_name
        and i.id != inst.id
        and i.status == InstanceStatus.RUNNING
    ]
    if not siblings:
        return None
    lines = [f"[{s.display_id()}] {s.prompt[:60]}" for s in siblings[:8]]
    return "Other active sessions in this repo:\n" + "\n".join(lines)


async def run_instance(
    ctx: RequestContext,
    inst: Instance,
    handle: MessageHandle | None = None,
    silent: bool = False,
) -> None:
    """Run an instance with optional live progress via handle."""
    inst.status = InstanceStatus.RUNNING
    ctx.store.update_instance(inst)

    on_progress = None
    on_stall = None
    heartbeat_task = None
    if handle:
        on_progress, on_stall, heartbeat = make_progress_callbacks(
            ctx, inst, handle, ctx.effective_verbose,
        )
        heartbeat_task = asyncio.create_task(heartbeat())

    sibling_ctx = get_sibling_summary(ctx.store, inst)

    start_time = asyncio.get_event_loop().time()
    result = None
    finalized = False
    ctx.runner.begin_task(inst.id)
    try:
        try:
            result = await ctx.runner.run(
                inst, on_progress=on_progress, on_stall=on_stall,
                context=ctx.effective_context,
                sibling_context=sibling_ctx,
            )
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()

        # Update thinking message to show completion
        if handle:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= 60:
                elapsed_str = f"{elapsed / 60:.1f}m"
            else:
                elapsed_str = f"{elapsed:.0f}s"
            escaped = ctx.messenger.escape(inst.display_id())
            icon = "❓" if result.needs_input else ("✅" if not result.is_error else "❌")
            origin_label = _origin_label(inst.origin)
            status = "asking a question" if result.needs_input else ("done" if not result.is_error else "failed")
            try:
                await ctx.messenger.edit_thinking(
                    handle, f"{icon} {escaped} {origin_label}{status} ({elapsed_str})",
                )
            except Exception:
                pass

        finalize_run(ctx, inst, result)
        finalized = True
        await send_result(ctx, inst, result.result_text, silent=silent)

        # Check reboot request BEFORE end_task so it's queued when end_task
        # checks for pending reboots on idle.  Safe because check_reboot_request
        # just queues (no waiting) — the actual reboot fires from end_task.
        await check_reboot_request(ctx)

    except asyncio.CancelledError:
        # Shutdown cancelled this task. The 30s drain in app.py keeps the
        # event loop alive long enough for us to attempt delivery.
        delivered = False
        if result is not None:
            # Result was computed — try to finalize + deliver it.
            # Guard against double-finalize if cancellation hit after
            # finalize_run() but before send_result().
            try:
                if not finalized:
                    finalize_run(ctx, inst, result)
                await send_result(ctx, inst, result.result_text, silent=silent)
                delivered = True
            except (asyncio.CancelledError, Exception):
                pass
        else:
            # Killed mid-execution — mark as failed
            inst.status = InstanceStatus.FAILED
            inst.error = "Bot restarted — instance interrupted"
            inst.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.store.update_instance(inst)
        # Only overwrite thinking message if result wasn't delivered —
        # otherwise it already shows the correct completion status.
        if not delivered and handle:
            try:
                await ctx.messenger.edit_thinking(
                    handle, "⚠️ Interrupted by bot restart",
                )
            except (asyncio.CancelledError, Exception):
                pass
        raise

    finally:
        ctx.runner.end_task(inst.id)

def _repo_has_changes(repo_path: str) -> bool:
    """Check if a repo has uncommitted changes (staged or unstaged)."""
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path, capture_output=True, timeout=5, text=True,
        )
        return bool(r.stdout.strip())
    except Exception:
        log.warning("Failed to check repo changes in %s", repo_path, exc_info=True)
        return False


def finalize_run(ctx: RequestContext, inst: Instance, result: RunResult) -> None:
    """Apply RunResult to Instance and persist."""
    inst.session_id = result.session_id
    inst.cost_usd = result.cost_usd
    inst.duration_ms = result.duration_ms
    inst.tools_used = result.tools_used
    inst.num_turns = result.num_turns
    inst.input_tokens = result.input_tokens
    inst.output_tokens = result.output_tokens
    inst.needs_input = result.needs_input
    inst.finished_at = datetime.now(timezone.utc).isoformat()

    # Detect session context flags (plan/code) from this instance or siblings
    tools = set(result.tools_used)
    plan_tools = {"EnterPlanMode"}

    if (plan_tools & tools) or inst.origin in PLAN_ORIGINS or inst.mode == "plan":
        inst.plan_active = True
        log.debug("%s plan_active=True (tools=%s, origin=%s, mode=%s)",
                  inst.id, plan_tools & tools, inst.origin.value, inst.mode)
    if CODE_CHANGE_TOOLS & tools:
        inst.code_active = True
    elif "Agent" in tools and inst.repo_path:
        # Subagents (Agent tool) can make edits that don't appear in parent's
        # tools_used. Check git for actual uncommitted changes in the repo.
        inst.code_active = _repo_has_changes(inst.repo_path)

    # Inherit flags from session siblings if not already set
    if inst.session_id and not (inst.plan_active and inst.code_active):
        for sibling in ctx.store.list_instances():
            if sibling.session_id == inst.session_id:
                if sibling.plan_active:
                    inst.plan_active = True
                if sibling.code_active:
                    inst.code_active = True
                if inst.plan_active and inst.code_active:
                    break

    if result.is_error and not result.needs_input:
        inst.status = InstanceStatus.FAILED
        inst.error = result.error_message or result.result_text
    else:
        inst.status = InstanceStatus.COMPLETED

    ctx.store.update_instance(inst)

    if result.cost_usd:
        ctx.store.add_cost(result.cost_usd)


def make_progress_callbacks(
    ctx: RequestContext,
    inst: Instance,
    handle: MessageHandle,
    verbose: int = 1,
):
    """Create on_progress, on_stall, and heartbeat closures."""
    last_update = [0.0]
    start_time = asyncio.get_event_loop().time()
    last_text = [None]
    is_stalled = [False]
    last_activity = ["processing..."]  # tracks last known tool activity
    mode_tag = f"[{inst.mode}] " if inst.mode and inst.mode != "explore" else ""

    def _elapsed() -> str:
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed >= 60:
            return f"{elapsed / 60:.1f}m"
        return f"{elapsed:.0f}s"

    stop_buttons = running_button_specs(inst.id)

    async def _edit(text: str, buttons=None):
        if text == last_text[0] and not buttons:
            return
        last_text[0] = text
        try:
            await ctx.messenger.edit_thinking(handle, text, buttons)
        except Exception:
            pass

    async def on_progress(message: str, detail: str = ""):
        is_stalled[0] = False
        # Always track latest activity (even if throttled)
        display = detail if verbose >= 2 and detail else message
        last_activity[0] = display
        if verbose == 0:
            return
        now = asyncio.get_event_loop().time()
        if now - last_update[0] < 5:
            return
        last_update[0] = now
        escaped = ctx.messenger.escape(inst.display_id())
        escaped_display = ctx.messenger.escape(display)
        await _edit(
            f"🔄 {mode_tag}{escaped} {escaped_display} ({_elapsed()})",
            buttons=stop_buttons,
        )

    async def on_stall(instance_id: str):
        is_stalled[0] = True
        escaped = ctx.messenger.escape(inst.display_id())
        await _edit(
            f"⚠️ {escaped} stalled (no output for {config.STALL_TIMEOUT_SECS}s) ({_elapsed()})",
            buttons=stall_button_specs(instance_id),
        )

    async def heartbeat():
        await asyncio.sleep(3)
        while True:
            if not is_stalled[0]:
                escaped = ctx.messenger.escape(inst.display_id())
                activity = ctx.messenger.escape(last_activity[0])
                await _edit(
                    f"🔄 {mode_tag}{escaped} {activity} ({_elapsed()})",
                    buttons=stop_buttons,
                )
            await asyncio.sleep(10)

    return on_progress, on_stall, heartbeat


async def send_result(
    ctx: RequestContext,
    inst: Instance,
    result_text: str,
    silent: bool = False,
) -> None:
    """Send result to channel — short inline, long as summary + file."""
    has_chain = bool(ctx.store.get_autopilot_chain(inst.session_id))
    buttons = action_button_specs(inst, has_autopilot_chain=has_chain)

    if result_text:
        result_text = redact_secrets(result_text)

    # Pass structured metadata for Discord embeds (Telegram ignores unknown fields)
    meta = {"_status": inst.status.value, "_mode": inst.mode} if inst.status else {}
    dur = format_duration(inst.duration_ms) if inst.duration_ms else None
    if dur:
        meta["Duration"] = dur
    if inst.num_turns:
        meta["Turns"] = str(inst.num_turns)
    total_tokens = inst.input_tokens + inst.output_tokens
    if total_tokens:
        meta["Tokens"] = format_tokens(total_tokens)
    if inst.cost_usd:
        meta["Cost"] = f"${inst.cost_usd:.4f}"
    if inst.branch:
        meta["Branch"] = inst.branch
    meta["Mode"] = mode_name(inst.mode)

    # Parse structured finalize output for commit/done/release origins
    is_finalize = inst.origin in (
        InstanceOrigin.COMMIT, InstanceOrigin.DONE, InstanceOrigin.RELEASE,
    )
    finalize_info = None
    if is_finalize and result_text and inst.status == InstanceStatus.COMPLETED:
        finalize_info = parse_finalize_output(result_text)
        if finalize_info:
            # Strip the raw summary block from result_text
            result_text = strip_summary_block(result_text)
            meta["_finalize"] = finalize_info

    try:
        if inst.status == InstanceStatus.FAILED or not result_text or finalize_info:
            # Failed/empty results use summary embed; finalize results use rich embed
            formatted = format_result_md(inst)
            markup = ctx.messenger.markdown_to_markup(formatted)
            msg_id = await ctx.messenger.send_result(
                ctx.channel_id, markup, metadata=meta,
                buttons=buttons, silent=silent,
            )
            inst.message_ids.setdefault(ctx.platform, []).append(msg_id)

        elif len(result_text) < 2000:
            markup = ctx.messenger.markdown_to_markup(result_text)
            chunks = ctx.messenger.chunk_message(markup)
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                msg_id = await ctx.messenger.send_text(
                    ctx.channel_id, chunk,
                    buttons if is_last else None, silent,
                )
                inst.message_ids.setdefault(ctx.platform, []).append(msg_id)

        else:
            expand_buttons = action_button_specs(inst, show_expand=True)
            formatted = format_result_md(inst)
            markup = ctx.messenger.markdown_to_markup(formatted)
            msg_id = await ctx.messenger.send_result(
                ctx.channel_id, markup, metadata=meta,
                buttons=expand_buttons, silent=silent,
            )
            inst.message_ids.setdefault(ctx.platform, []).append(msg_id)

    except Exception:
        log.exception("Failed to send result for %s", inst.id)
        try:
            error_text = inst.error or inst.summary or "Result delivery failed"
            msg_id = await ctx.messenger.send_text(
                ctx.channel_id,
                f"{inst.display_id()}: {error_text[:500]}",
                silent=silent,
            )
            inst.message_ids.setdefault(ctx.platform, []).append(msg_id)
        except Exception:
            log.exception("Last-resort notification also failed for %s", inst.id)

    ctx.store.update_instance(inst)


async def check_reboot_request(ctx: RequestContext) -> None:
    """Check if a Claude Code instance wrote a reboot request file.

    If found, queue the reboot on the runner. The actual reboot executes
    after all active tasks finish (coalesced — multiple autopilots requesting
    reboots produce a single reboot).
    """
    try:
        raw = config.REBOOT_REQUEST_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except Exception:
        log.warning("Failed to read reboot request file", exc_info=True)
        return
    try:
        data = json.loads(raw)
    except Exception:
        log.warning("Malformed reboot request file, removing", exc_info=True)
        config.REBOOT_REQUEST_FILE.unlink(missing_ok=True)
        return

    # Delete file immediately to prevent re-reads by concurrent instances
    try:
        config.REBOOT_REQUEST_FILE.unlink()
    except OSError:
        pass

    # Attach channel context so the reboot executor knows where to notify
    data["channel_id"] = ctx.channel_id
    data["platform"] = ctx.platform

    ctx.runner.request_reboot(data)
    log.info(
        "Queued reboot request (reason: %s, total pending: %d)",
        data.get("message", "reboot requested"),
        len(ctx.runner.pending_reboots()),
    )
