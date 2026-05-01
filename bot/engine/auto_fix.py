"""Generic auto-fix primitive — spawns diagnosis + autopilot chain for failures.

Used by: deploy failures, test failures, monitoring critical alerts.
Each trigger creates a forum thread, runs Claude to diagnose, then chains
into autopilot to fix. Callers provide trigger-specific prompts and callbacks.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Awaitable

if TYPE_CHECKING:
    from bot.platform.base import RequestContext

log = logging.getLogger(__name__)


@dataclass
class AutoFixState:
    """Per-repo, per-trigger auto-fix tracking."""

    attempt: int = 0
    thread_id: str | None = None

    def to_dict(self) -> dict:
        return {"attempt": self.attempt, "thread_id": self.thread_id}

    @classmethod
    def from_dict(cls, d: dict) -> AutoFixState:
        return cls(attempt=d.get("attempt", 0), thread_id=d.get("thread_id"))


async def spawn_fix_session(
    bot: Any,
    repo_name: str,
    trigger: str,
    error_summary: str,
    error_output: str,
    fix_prompt: str,
    *,
    max_retries: int = 1,
    max_cost_usd: float = 1.0,
    on_success: Callable[[], Awaitable[Any]] | None = None,
    on_started: Callable[[str], None] | None = None,
) -> None:
    """Spawn a fix session in a new forum thread.

    1. Creates thread  ``{trigger}-fix-{attempt}``
    2. Runs diagnosis query (plan mode)
    3. Chains into autopilot with cost budget
    4. On success calls ``on_success`` callback (e.g. redeploy)
    5. On failure/exhaustion notifies owner
    """
    from bot.claude.types import InstanceStatus
    from bot.engine import commands as engine_commands
    from bot.engine import workflows

    store = bot._store
    state = store.get_auto_fix_state(repo_name, trigger)

    if state.attempt >= max_retries:
        log.info("Auto-fix exhausted for %s/%s (%d/%d)",
                 repo_name, trigger, state.attempt, max_retries)
        return

    state.attempt += 1
    store.set_auto_fix_state(repo_name, trigger, state)

    # Create fix thread
    thread = await bot._forums.get_or_create_session_thread(
        repo_name, None, f"{trigger}-fix-{state.attempt}",
    )
    if not thread:
        log.warning("Could not create auto-fix thread for %s/%s", repo_name, trigger)
        return

    channel_id = str(thread.id)
    state.thread_id = channel_id
    store.set_auto_fix_state(repo_name, trigger, state)
    asyncio.create_task(bot._forums.refresh_control_room(repo_name))

    # Follow the owner
    try:
        if bot._discord_user_id:
            owner = bot.get_user(bot._discord_user_id)
            if owner:
                await thread.add_user(owner)
    except Exception:
        log.debug("Failed to add owner to auto-fix thread", exc_info=True)

    # Build context
    lookup = bot._forums.thread_to_project(channel_id)
    t_info = lookup[1] if lookup else None
    ctx = bot._ctx(channel_id, repo_name=repo_name, thread_info=t_info)
    ctx.is_owner = True
    if t_info:
        bot._forums.attach_session_callbacks(ctx, t_info, channel_id)

    # Let caller sync state (e.g. DeployState for control room display)
    if on_started:
        on_started(channel_id)

    # Notify owner
    owner_id_str = str(bot._discord_user_id) if bot._discord_user_id else ""
    mention = ctx.messenger.format_mention(owner_id_str) if owner_id_str else ""
    notify_text = (
        f"{mention} {trigger.replace('_', ' ').title()} failed — auto-fix session started."
        if mention else
        f"{trigger.replace('_', ' ').title()} failed — auto-fix session started."
    )
    try:
        await ctx.messenger.send_text(channel_id, notify_text, silent=False)
    except Exception:
        log.debug("Failed to send auto-fix notification", exc_info=True)

    # Run initial query (diagnosis)
    try:
        await engine_commands.on_text(ctx, fix_prompt)
    except Exception:
        log.exception("Auto-fix on_text failed for %s/%s", repo_name, trigger)
        try:
            await ctx.messenger.send_text(
                channel_id, "❌ Auto-fix could not start — query failed.",
            )
        except Exception:
            pass
        _clear_thread(store, repo_name, trigger, bot)
        return

    # Find the session to chain from
    if t_info:
        lookup = bot._forums.thread_to_project(channel_id)
        if lookup:
            t_info = lookup[1]
    session_id = t_info.session_id if t_info else None
    if not session_id:
        log.warning("No session_id after auto-fix query for %s/%s", repo_name, trigger)
        _clear_thread(store, repo_name, trigger, bot)
        return

    # Find latest instance for this session
    all_instances = store.list_instances()
    session_instances = [i for i in all_instances if i.session_id == session_id]
    if not session_instances:
        log.warning("No instances found for auto-fix session %s", session_id)
        _clear_thread(store, repo_name, trigger, bot)
        return

    source_inst = session_instances[-1]
    source_msg_id = None
    platform_msgs = source_inst.message_ids.get("discord", [])
    if platform_msgs:
        source_msg_id = platform_msgs[-1]

    # Chain into autopilot with cost budget.
    # Acquire the per-channel lock for the chain — on_text above released its
    # lock when its query finished, so without re-acquiring here a concurrent
    # text/button on this thread could double-spawn.  Matches the contract
    # documented on workflows.resume_autopilot_chain.
    from bot.engine.commands import _get_channel_lock
    chain_lock = _get_channel_lock(str(channel_id))
    try:
        async with chain_lock:
            result = await workflows.on_autopilot(
                ctx, source_inst.id, source_msg_id,
                start_from="review_loop",
                cost_budget_usd=max_cost_usd,
            )
    except Exception:
        log.exception("Auto-fix autopilot chain failed for %s/%s", repo_name, trigger)
        try:
            await ctx.messenger.send_text(
                channel_id, "❌ Auto-fix chain failed unexpectedly.",
            )
        except Exception:
            pass
        _clear_thread(store, repo_name, trigger, bot)
        return

    # Check if chain completed successfully (merged)
    chain_succeeded = (
        result
        and result.status == InstanceStatus.COMPLETED
        and result.branch is None
    )

    if not chain_succeeded:
        try:
            await ctx.messenger.send_text(
                channel_id,
                "⚠️ Auto-fix chain did not complete — manual intervention needed.",
            )
        except Exception:
            pass
        _clear_thread(store, repo_name, trigger, bot)
        return

    # Success — call on_success callback
    if on_success:
        try:
            await on_success()
        except Exception:
            log.exception("Auto-fix on_success callback failed for %s/%s", repo_name, trigger)
            try:
                await ctx.messenger.send_text(
                    channel_id, "⚠️ Fix merged but post-fix action failed.",
                )
            except Exception:
                pass
    else:
        try:
            await ctx.messenger.send_text(
                channel_id,
                "✅ Fix merged. Review the changes when you're ready.",
                silent=True,
            )
        except Exception:
            pass

    _clear_thread(store, repo_name, trigger, bot)


def _clear_thread(store: Any, repo_name: str, trigger: str, bot: Any) -> None:
    """Clear auto-fix thread tracking and refresh control room."""
    state = store.get_auto_fix_state(repo_name, trigger)
    state.thread_id = None
    store.set_auto_fix_state(repo_name, trigger, state)
    asyncio.create_task(bot._forums.refresh_control_room(repo_name))
