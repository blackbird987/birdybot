"""Monitor service lifecycle — lazy init and setup."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)


async def monitor_setup(bot: ClaudeBot, name: str, repo_name: str | None = None) -> str:
    """Set up a monitor from env config.

    If repo_name is provided, creates the monitor as a pinned thread inside
    the repo's forum channel. Otherwise falls back to a text channel in the
    bot's category (legacy).
    """
    from bot.monitor.service import _load_monitor_configs

    configs = _load_monitor_configs()
    if name not in configs:
        return (
            f"No config found for **{name}**. "
            f"Set `MONITOR_{name.upper()}_URL` and `MONITOR_{name.upper()}_AUTH` in .env"
        )

    if not bot._monitor_service:
        init_monitor_service(bot)

    guild = bot.get_guild(bot._guild_id)
    if not guild or not bot._category_id:
        return "Guild or category not available."
    category = guild.get_channel(bot._category_id)
    if not category or not isinstance(category, discord.CategoryChannel):
        return "Category channel not found."

    cfg = configs[name]

    # Use repo_name from command, config, or env var (priority order)
    effective_repo = repo_name or cfg.repo_name
    forum = None
    if effective_repo:
        proj = bot._forums.forum_projects.get(effective_repo)
        if proj and proj.forum_channel_id:
            forum = guild.get_channel(int(proj.forum_channel_id))
            if not isinstance(forum, discord.ForumChannel):
                forum = None
        if not forum:
            return f"Repo **{effective_repo}** not found or has no forum channel."

    channel = await bot._monitor_service.setup_monitor(
        cfg, category, forum=forum, repo_name=effective_repo,
    )

    # Store monitor thread ID in ForumProject if repo-specific
    if effective_repo and isinstance(channel, discord.Thread):
        proj = bot._forums.forum_projects.get(effective_repo)
        if proj:
            proj.monitor_thread_id = str(channel.id)
            bot._forums.save_forum_map()

    if not bot._monitor_started:
        bot._monitor_service.start()
        bot._monitor_started = True

    return f"Monitor **{name}** enabled → <#{channel.id}>"


def init_monitor_service(bot: ClaudeBot) -> None:
    """Initialize the monitor service (lazy)."""
    from bot.monitor.service import MonitorService

    bot._monitor_service = MonitorService(
        bot=bot,
        store=bot._store,
        guild_id=bot._guild_id,
        category_id=bot._category_id,
        notifier=bot._notifier,
    )
