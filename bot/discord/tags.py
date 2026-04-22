"""Forum tag management — apply status/mode tags to threads."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from bot.discord import channels
from bot.platform.formatting import MODE_DISPLAY

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)


async def apply_thread_tags(
    thread: discord.Thread, status: str, origin: str = "bot",
    mode: str | None = None, *, merged: bool = False,
) -> None:
    """Apply forum tags to a thread based on status + mode. Fire-and-forget safe."""
    try:
        if not isinstance(thread.parent, discord.ForumChannel):
            return
        forum = thread.parent
        tag_map = {t.name: t for t in forum.available_tags}
        if not tag_map or (merged and "merged" not in tag_map):
            tag_map = await channels.ensure_forum_tags(forum)

        desired_tags = []
        if merged and "merged" in tag_map:
            desired_tags.append(tag_map["merged"])
        elif status == "completed" and "completed" in tag_map:
            desired_tags.append(tag_map["completed"])
        elif status == "failed" and "failed" in tag_map:
            desired_tags.append(tag_map["failed"])
        elif status == "running" and "active" in tag_map:
            desired_tags.append(tag_map["active"])

        if origin == "cli" and "cli" in tag_map:
            desired_tags.append(tag_map["cli"])

        if mode and mode in tag_map:
            desired_tags.append(tag_map[mode])

        if desired_tags:
            await thread.edit(applied_tags=desired_tags[:5])
    except Exception:
        log.debug("Failed to apply tags to thread %s", thread.id, exc_info=True)


async def try_apply_tags_after_run(bot: ClaudeBot, channel_id: str) -> None:
    """Check latest instance status and apply tags to the thread.

    Always clears the 'active' tag — either by replacing with completion
    tags, or as a standalone fallback if no instance is found.
    """
    ch = bot.get_channel(int(channel_id))
    if not ch or not isinstance(ch, discord.Thread):
        return
    lookup = bot._forums.thread_to_project(channel_id)
    if not lookup:
        return
    _, info = lookup
    # Find the most recent instance for this session
    for inst in bot._store.list_instances()[:5]:
        if inst.session_id and inst.session_id == info.session_id:
            await apply_thread_tags(ch, inst.status.value, info.origin, mode=inst.mode)
            return
    # No matching instance — still clear "active" tag as fallback
    await set_thread_active_tag(bot, ch, False)


async def set_thread_near_limit_tag(
    bot: ClaudeBot,
    channel_or_id: "discord.abc.GuildChannel | discord.Thread | str | None",
    apply: bool,
) -> None:
    """Add or remove the `near-limit` forum tag on a thread.

    Fire-and-forget safe; silently no-ops on wrong channel type, missing
    permission, or tag cap exhaustion.
    """
    if isinstance(channel_or_id, str):
        channel = bot.get_channel(int(channel_or_id))
    else:
        channel = channel_or_id
    if not isinstance(channel, discord.Thread):
        return
    if not isinstance(channel.parent, discord.ForumChannel):
        return
    try:
        tag_map = {t.name: t for t in channel.parent.available_tags}
        if "near-limit" not in tag_map:
            tag_map = await channels.ensure_forum_tags(channel.parent)
        tag = tag_map.get("near-limit")
        if not tag:
            return

        current = list(channel.applied_tags)
        if apply and tag not in current:
            current.append(tag)
        elif not apply and tag in current:
            current.remove(tag)
        else:
            return  # no change needed

        await channel.edit(applied_tags=current[:5])
        log.debug("Set near-limit=%s on thread %s", apply, channel.id)
    except Exception:
        log.debug(
            "Failed to toggle near-limit on thread %s",
            getattr(channel, "id", "?"), exc_info=True,
        )


async def set_thread_active_tag(
    bot: ClaudeBot,
    channel: discord.abc.GuildChannel | discord.Thread | None,
    active: bool,
) -> None:
    """Add or remove the 'active' forum tag on a thread.

    Tag-only edits use Discord's normal rate limit (~5/5s), not the
    harsh 2-per-10-min thread name rate limit. Fire-and-forget safe.
    """
    if not isinstance(channel, discord.Thread):
        return
    if not isinstance(channel.parent, discord.ForumChannel):
        return
    try:
        tag_map = {t.name: t for t in channel.parent.available_tags}
        if not tag_map:
            tag_map = await channels.ensure_forum_tags(channel.parent)
        active_tag = tag_map.get("active")
        if not active_tag:
            return

        original_tags = list(channel.applied_tags)
        current_tags = list(original_tags)
        if active:
            if active_tag not in current_tags:
                current_tags.append(active_tag)
            # Also set mode tag
            lookup = bot._forums.thread_to_project(str(channel.id))
            mode = lookup[1].mode if lookup and lookup[1].mode else bot._store.mode
            mode_tag = tag_map.get(mode)
            # Remove other mode tags, add current
            for m in MODE_DISPLAY:
                mt = tag_map.get(m)
                if mt and mt in current_tags:
                    current_tags.remove(mt)
            if mode_tag and mode_tag not in current_tags:
                current_tags.append(mode_tag)
        else:
            if active_tag in current_tags:
                current_tags.remove(active_tag)

        if current_tags != original_tags:
            await channel.edit(applied_tags=current_tags[:5])
            log.debug("Set thread %s active=%s", channel.id, active)
    except Exception:
        log.debug("Failed to set active tag on thread %s", channel.id, exc_info=True)
