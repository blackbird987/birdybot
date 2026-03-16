"""Thread sleep/wake management — idle indicator via 💤 prefix."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from bot.discord import channels

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)


def schedule_sleep(bot: ClaudeBot, channel_id: str) -> None:
    """Schedule 💤 after 5 min idle. Cancel any existing timer first."""
    cancel_sleep(bot, channel_id)
    gen = bot._sleep_gen.get(channel_id, 0) + 1
    bot._sleep_gen[channel_id] = gen
    loop = asyncio.get_running_loop()
    bot._idle_timers[channel_id] = loop.call_later(
        300,  # 5 min — leaves room for wake edit within 10-min rate limit
        lambda cid=channel_id, g=gen: asyncio.create_task(_apply_sleep(bot, cid, g)),
    )


def cancel_sleep(bot: ClaudeBot, channel_id: str) -> None:
    """Cancel pending sleep timer and invalidate any in-flight callbacks."""
    timer = bot._idle_timers.pop(channel_id, None)
    if timer:
        timer.cancel()
    # Bump generation so stale create_task'd coroutines no-op
    bot._sleep_gen[channel_id] = bot._sleep_gen.get(channel_id, 0) + 1


async def _apply_sleep(bot: ClaudeBot, channel_id: str, gen: int) -> None:
    """Called by timer — set the thread to sleeping."""
    if bot._sleep_gen.get(channel_id) != gen:
        return  # Stale: timer was cancelled or rescheduled
    bot._idle_timers.pop(channel_id, None)
    ch = bot.get_channel(int(channel_id))
    await set_thread_sleeping(bot, ch)


async def set_thread_sleeping(
    bot: ClaudeBot,
    channel: discord.abc.GuildChannel | discord.Thread | None,
) -> None:
    """Add 💤 prefix to thread name (idle > 5 min).

    Thread name edits have a harsh 2-per-10-min rate limit.
    We budget one edit for sleep, one for wake.
    """
    if not isinstance(channel, discord.Thread):
        return
    is_sleeping, topic = channels.parse_thread_name(channel.name)
    if is_sleeping:
        return
    tid = str(channel.id)
    if tid in bot._name_editing:
        return
    bot._name_editing.add(tid)
    try:
        new_name = channels.build_sleeping_thread_name(topic)
        try:
            await channel.edit(name=new_name)
            log.debug("Thread %s now sleeping", channel.id)
        except Exception:
            log.debug("Failed to set thread sleeping", exc_info=True)
    finally:
        bot._name_editing.discard(tid)


async def clear_thread_sleeping(
    bot: ClaudeBot,
    channel: discord.abc.GuildChannel | discord.Thread | None,
) -> None:
    """Remove 💤 prefix from thread name (processing started)."""
    if not isinstance(channel, discord.Thread):
        return
    is_sleeping, topic = channels.parse_thread_name(channel.name)
    if not is_sleeping:
        return
    tid = str(channel.id)
    if tid in bot._name_editing:
        return
    bot._name_editing.add(tid)
    try:
        new_name = channels.build_thread_name(topic)
        try:
            await channel.edit(name=new_name)
            log.debug("Thread %s woke up", channel.id)
        except Exception:
            log.debug("Failed to clear thread sleep", exc_info=True)
    finally:
        bot._name_editing.discard(tid)
