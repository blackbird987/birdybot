"""Spawn logic from button callbacks — plan, build, review, commit."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from bot import config
from bot.claude.types import (
    CODE_CHANGE_TOOLS, Instance, InstanceOrigin, InstanceStatus, InstanceType,
)
from bot.engine import lifecycle, sessions as sessions_mod
from bot.platform.base import RequestContext
from bot.platform.formatting import running_button_specs

log = logging.getLogger(__name__)


@dataclass
class SpawnConfig:
    instance_type: InstanceType
    prompt: str
    mode: str
    origin: InstanceOrigin
    status_text: str = "Processing..."
    resume_session: bool = False
    copy_branch: bool = False
    auto_branch: bool = False
    silent: bool = False


async def spawn_from(
    ctx: RequestContext,
    source_id: str,
    cfg: SpawnConfig,
    strip_source_buttons: bool = True,
    source_msg_id: str | None = None,
) -> Instance | None:
    """Common pattern for spawning a new instance from a button press.

    Returns the new Instance, or None if guards fail.
    """
    source = ctx.store.get_instance(source_id)
    if not source:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return None

    # Budget guard
    if ctx.store.get_daily_cost() >= config.DAILY_BUDGET_USD:
        await ctx.messenger.send_text(ctx.channel_id, "Daily budget exceeded.")
        return None

    # Repo guard
    if not source.repo_path or not Path(source.repo_path).is_dir():
        await ctx.messenger.send_text(ctx.channel_id, "Repo path no longer valid.")
        return None

    # Session guard
    if cfg.resume_session and not source.session_id:
        await ctx.messenger.send_text(ctx.channel_id, "No session to resume.")
        return None

    new_inst = ctx.store.create_instance(
        instance_type=cfg.instance_type,
        prompt=cfg.prompt,
        mode=cfg.mode,
    )
    new_inst.origin = cfg.origin
    new_inst.origin_platform = ctx.platform
    new_inst.parent_id = source.id
    new_inst.repo_name = source.repo_name
    new_inst.repo_path = source.repo_path

    if cfg.resume_session:
        new_inst.session_id = source.session_id
    if cfg.copy_branch:
        new_inst.branch = source.branch
        new_inst.original_branch = source.original_branch
    elif cfg.auto_branch:
        new_inst.branch = f"claude-bot/{new_inst.id}"

    ctx.store.update_instance(new_inst)

    # Strip buttons from source message
    if strip_source_buttons and source_msg_id:
        try:
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, None)
        except Exception:
            pass

    # Send thinking message
    escaped = ctx.messenger.escape(new_inst.display_id())
    escaped_status = ctx.messenger.escape(cfg.status_text.lower())
    handle = await ctx.messenger.send_thinking(
        ctx.channel_id, f"⏳ {escaped} {escaped_status}",
        buttons=running_button_specs(new_inst.id),
    )
    # Track the thinking message
    if handle.get("message_id"):
        new_inst.message_ids.setdefault(ctx.platform, []).append(
            handle.get("message_id")
        )
        ctx.store.update_instance(new_inst)

    await lifecycle.run_instance(ctx, new_inst, handle=handle, silent=cfg.silent)
    return new_inst


async def on_plan(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> None:
    source = ctx.store.get_instance(source_id)
    if not source:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return
    prompt = config.PLAN_PROMPT_PREFIX + source.prompt
    await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.QUERY, prompt=prompt,
        mode="explore", origin=InstanceOrigin.PLAN,
        status_text="Planning...", resume_session=True,
    ), source_msg_id=source_msg_id)


async def on_build(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> None:
    source = ctx.store.get_instance(source_id)
    if not source:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return
    is_plan = source.plan_active
    await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.TASK,
        prompt=config.BUILD_FROM_PLAN_PROMPT if is_plan else config.BUILD_FROM_QUERY_PROMPT,
        mode="build", origin=InstanceOrigin.BUILD,
        status_text="Building...", resume_session=True,
        auto_branch=True, silent=True,
    ), source_msg_id=source_msg_id)


async def on_review_plan(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> None:
    await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.QUERY, prompt=config.PLAN_REVIEW_PROMPT,
        mode="explore", origin=InstanceOrigin.REVIEW_PLAN,
        status_text="Reviewing plan...", resume_session=True,
    ), source_msg_id=source_msg_id)


async def on_apply_revisions(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> None:
    await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.QUERY, prompt=config.APPLY_REVISIONS_PROMPT,
        mode="explore", origin=InstanceOrigin.APPLY_REVISIONS,
        status_text="Applying revisions...", resume_session=True,
    ), source_msg_id=source_msg_id)


async def on_review_code(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> None:
    """Review code, auto-looping until no issues remain (max 5 rounds)."""
    MAX_ROUNDS = 5
    current_source = source_id
    current_msg = source_msg_id
    for round_num in range(MAX_ROUNDS):
        status = "Reviewing code..." if round_num == 0 else f"Re-reviewing (round {round_num + 1})..."
        result = await spawn_from(ctx, current_source, SpawnConfig(
            instance_type=InstanceType.TASK, prompt=config.CODE_REVIEW_PROMPT,
            mode="build", origin=InstanceOrigin.REVIEW_CODE,
            status_text=status, resume_session=True,
            copy_branch=True, silent=True,
        ), source_msg_id=current_msg)

        if not result:
            break

        # Clean review (no code changes) — done
        if not (CODE_CHANGE_TOOLS & set(result.tools_used or [])):
            break

        # Changes were made — strip buttons from this result, review again
        current_source = result.id
        result_msgs = result.message_ids.get(ctx.platform, [])
        current_msg = result_msgs[-1] if result_msgs else None


async def on_commit(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> None:
    await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.TASK, prompt=config.COMMIT_PROMPT,
        mode="build", origin=InstanceOrigin.COMMIT,
        status_text="Committing...", resume_session=True,
        copy_branch=True, silent=True,
    ), source_msg_id=source_msg_id)


async def on_done(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> None:
    """Commit all changes, update changelog, then close the conversation."""
    result = await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.TASK, prompt=config.DONE_PROMPT,
        mode="build", origin=InstanceOrigin.DONE,
        status_text="Wrapping up...", resume_session=True,
        copy_branch=True, silent=True,
    ), source_msg_id=source_msg_id)

    if not result or result.status != InstanceStatus.COMPLETED:
        return

    # Close the thread/conversation after successful commit
    try:
        await ctx.messenger.close_conversation(ctx.channel_id)
    except Exception:
        log.debug("Failed to close conversation for channel %s", ctx.channel_id, exc_info=True)


async def on_sess_resume(
    ctx: RequestContext,
    session_id: str,
    source_msg_id: str | None = None,
) -> None:
    """Resume a desktop CLI session."""
    ctx.store.active_session_id = session_id

    # Strip buttons from session list message
    if source_msg_id:
        try:
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, None)
        except Exception:
            pass

    # Load recent messages and show last 2 user+assistant pairs
    fpath = await asyncio.to_thread(sessions_mod.find_session_file, session_id)
    if fpath:
        all_msgs = await asyncio.to_thread(sessions_mod.read_session_messages, fpath, 999)

        pairs = []
        i = len(all_msgs) - 1
        while i >= 0 and len(pairs) < 2:
            if all_msgs[i]["role"] == "assistant":
                assistant_msg = all_msgs[i]
                j = i - 1
                while j >= 0 and all_msgs[j]["role"] != "user":
                    j -= 1
                if j >= 0:
                    pairs.append((all_msgs[j], assistant_msg))
                else:
                    pairs.append((None, assistant_msg))
                i = j - 1
            else:
                i -= 1

        pairs.reverse()

        for user_msg, asst_msg in pairs:
            if user_msg:
                text = user_msg["text"]
                if len(text) > 400:
                    text = text[:400] + "…"
                escaped = ctx.messenger.escape(text)
                markup = ctx.messenger.markdown_to_markup(f"**You:**\n{text}")
                try:
                    await ctx.messenger.send_text(ctx.channel_id, markup, silent=True)
                except Exception:
                    await ctx.messenger.send_text(ctx.channel_id, f"You:\n{text[:400]}", silent=True)

            text = asst_msg["text"]
            if len(text) > 400:
                text = text[:400] + "…"
            markup = ctx.messenger.markdown_to_markup(f"**Claude:**\n{text}")
            try:
                await ctx.messenger.send_text(ctx.channel_id, markup, silent=True)
            except Exception:
                await ctx.messenger.send_text(ctx.channel_id, f"Claude:\n{text[:400]}", silent=True)

    await ctx.messenger.send_text(
        ctx.channel_id, "✅ Session resumed. Send a message to continue.", silent=True,
    )
