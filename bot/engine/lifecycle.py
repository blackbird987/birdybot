"""Instance execution, progress callbacks, result delivery, finalization."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from bot import config
from bot.claude.types import Instance, InstanceStatus, RunResult
from bot.platform.base import MessageHandle, RequestContext
from bot.platform.formatting import (
    action_button_specs,
    expanded_button_specs,
    format_expanded_result_md,
    format_result_md,
    redact_secrets,
    stall_button_specs,
)

log = logging.getLogger(__name__)


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
            ctx, inst, handle, ctx.store.verbose_level,
        )
        heartbeat_task = asyncio.create_task(heartbeat())

    start_time = asyncio.get_event_loop().time()
    try:
        result = await ctx.runner.run(
            inst, on_progress=on_progress, on_stall=on_stall,
            context=ctx.store.context,
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
        icon = "✅" if not result.is_error else "❌"
        try:
            await ctx.messenger.edit_thinking(
                handle, f"{icon} {escaped} done ({elapsed_str})",
            )
        except Exception:
            pass

    finalize_run(ctx, inst, result)
    await send_result(ctx, inst, result.result_text, silent=silent)


def finalize_run(ctx: RequestContext, inst: Instance, result: RunResult) -> None:
    """Apply RunResult to Instance and persist."""
    inst.session_id = result.session_id
    inst.cost_usd = result.cost_usd
    inst.duration_ms = result.duration_ms
    inst.finished_at = datetime.now(timezone.utc).isoformat()

    if result.is_error:
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

    def _elapsed() -> str:
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed >= 60:
            return f"{elapsed / 60:.1f}m"
        return f"{elapsed:.0f}s"

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
        if verbose == 0:
            return
        now = asyncio.get_event_loop().time()
        if now - last_update[0] < 5:
            return
        last_update[0] = now
        display = detail if verbose >= 2 and detail else message
        escaped = ctx.messenger.escape(inst.display_id())
        escaped_display = ctx.messenger.escape(display)
        await _edit(f"🔄 {escaped} {escaped_display} ({_elapsed()})")

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
                await _edit(f"🔄 {escaped} processing... ({_elapsed()})")
            await asyncio.sleep(10)

    return on_progress, on_stall, heartbeat


async def send_result(
    ctx: RequestContext,
    inst: Instance,
    result_text: str,
    silent: bool = False,
) -> None:
    """Send result to channel — short inline, long as summary + file."""
    buttons = action_button_specs(inst)

    if result_text:
        result_text = redact_secrets(result_text)

    # Pass status hint so Discord can color embeds (red for failed, etc.)
    meta = {"_status": inst.status.value} if inst.status else {}

    try:
        if inst.status == InstanceStatus.FAILED or not result_text:
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
