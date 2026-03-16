"""Monitor service lifecycle — lazy init and setup."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)


async def monitor_setup(bot: ClaudeBot, name: str) -> str:
    """Set up a monitor from env config."""
    from bot.monitor.service import MonitorConfig, _load_monitor_configs

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
    channel = await bot._monitor_service.setup_monitor(cfg, category)

    if not bot._monitor_started:
        bot._monitor_service.start()
        bot._monitor_started = True

    return f"Monitor **{name}** enabled \u2192 <#{channel.id}>"


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
