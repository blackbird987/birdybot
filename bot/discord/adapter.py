"""DiscordMessenger — implements the Messenger protocol for Discord."""

from __future__ import annotations

import logging
from pathlib import Path

import discord

from bot.discord import channels, formatter as discord_fmt
from bot.platform.base import ButtonSpec, MessageHandle

log = logging.getLogger(__name__)


def _buttons_to_view(
    buttons: list[list[ButtonSpec]] | None,
) -> discord.ui.View | None:
    """Convert ButtonSpec rows to a discord.py View. Discord max is 5 rows."""
    if not buttons:
        return None
    view = discord.ui.View(timeout=None)
    for row_idx, row in enumerate(buttons[:5]):  # Discord limit: 5 rows
        for btn_spec in row:
            button = discord.ui.Button(
                label=btn_spec.label,
                custom_id=btn_spec.callback_data,
                style=discord.ButtonStyle.secondary,
                row=row_idx,
            )
            view.add_item(button)
    return view


class DiscordMessenger:
    """Implements Messenger protocol for Discord."""

    def __init__(
        self,
        bot: discord.Client,
        guild_id: int,
        lobby_channel_id: int,
        category_id: int | None = None,
    ) -> None:
        self._bot = bot
        self._guild_id = guild_id
        self._lobby_channel_id = lobby_channel_id
        self._category_id = category_id

    @property
    def platform_name(self) -> str:
        return "discord"

    def _get_guild(self) -> discord.Guild | None:
        return self._bot.get_guild(self._guild_id)

    def _get_channel(self, channel_id: str) -> discord.abc.Messageable | None:
        return self._bot.get_channel(int(channel_id))

    async def create_conversation(
        self, instance_id: str, summary: str, is_task: bool,
    ) -> str:
        """Create thread (query) or channel (task)."""
        guild = self._get_guild()
        if not guild:
            return str(self._lobby_channel_id)

        if is_task:
            # Create full channel for tasks
            category = None
            if self._category_id:
                category = guild.get_channel(self._category_id)
            name = f"t-{instance_id}-{summary[:60]}"
            ch = await channels.create_task_channel(guild, name, category)
            return str(ch.id)
        else:
            # Create thread for queries
            lobby = self._get_channel(str(self._lobby_channel_id))
            if isinstance(lobby, discord.TextChannel):
                name = f"q-{instance_id}-{summary[:60]}"
                thread = await channels.create_thread(lobby, name)
                return str(thread.id)
            return str(self._lobby_channel_id)

    async def send_thinking(
        self, channel_id: str, text: str,
        buttons: list[list[ButtonSpec]] | None = None,
    ) -> MessageHandle:
        """Send a thinking message."""
        channel = self._get_channel(channel_id)
        if not channel:
            return MessageHandle(platform="discord", _data={})

        view = _buttons_to_view(buttons)
        msg = await channel.send(content=text, view=view)
        return MessageHandle(
            platform="discord",
            _data={"channel_id": channel_id, "message_id": str(msg.id)},
        )

    async def edit_thinking(
        self, handle: MessageHandle, text: str,
        buttons: list[list[ButtonSpec]] | None = None,
    ) -> None:
        """Edit a thinking message."""
        channel_id = handle.get("channel_id")
        message_id = handle.get("message_id")
        if not channel_id or not message_id:
            return

        channel = self._get_channel(channel_id)
        if not channel:
            return

        try:
            msg = await channel.fetch_message(int(message_id))
            view = _buttons_to_view(buttons)
            await msg.edit(content=text, view=view)
        except Exception:
            log.debug("Failed to edit thinking message %s", message_id, exc_info=True)

    async def send_text(
        self, channel_id: str, text: str,
        buttons: list[list[ButtonSpec]] | None = None,
        silent: bool = False,
    ) -> str:
        """Send a text message."""
        channel = self._get_channel(channel_id)
        if not channel:
            return ""

        view = _buttons_to_view(buttons)
        msg = await channel.send(content=text, view=view, silent=silent)
        return str(msg.id)

    async def send_result(
        self, channel_id: str, text: str,
        metadata: dict | None = None,
        buttons: list[list[ButtonSpec]] | None = None,
        silent: bool = False,
    ) -> str:
        """Send a result as an embed."""
        channel = self._get_channel(channel_id)
        if not channel:
            return ""

        # Determine embed color from metadata status hint
        color = discord.Color.green()
        if metadata:
            status = metadata.pop("_status", None)
            if status == "failed":
                color = discord.Color.red()
            elif status == "killed":
                color = discord.Color.orange()
        embed = discord.Embed(
            description=text[:4096],
            color=color,
        )
        if metadata:
            footer_parts = []
            for k, v in metadata.items():
                footer_parts.append(f"{k}: {v}")
            if footer_parts:
                embed.set_footer(text=" | ".join(footer_parts))

        view = _buttons_to_view(buttons)
        msg = await channel.send(embed=embed, view=view, silent=silent)
        return str(msg.id)

    async def edit_text(
        self, channel_id: str, msg_id: str | None, text: str | None,
        buttons: list[list[ButtonSpec]] | None = None,
    ) -> None:
        """Edit a message."""
        if not msg_id:
            return
        channel = self._get_channel(channel_id)
        if not channel:
            return

        try:
            msg = await channel.fetch_message(int(msg_id))
            view = _buttons_to_view(buttons)
            if text is None:
                await msg.edit(view=view)
            elif msg.embeds:
                # Original was an embed — update embed description (4096 limit)
                embed = msg.embeds[0].copy()
                embed.description = text[:4096]
                await msg.edit(embed=embed, view=view)
            else:
                await msg.edit(content=text[:2000], view=view)
        except Exception:
            log.debug("Failed to edit message %s", msg_id, exc_info=True)

    async def delete_message(self, channel_id: str, msg_id: str) -> None:
        channel = self._get_channel(channel_id)
        if not channel:
            return
        try:
            msg = await channel.fetch_message(int(msg_id))
            await msg.delete()
        except Exception:
            log.debug("Failed to delete message %s", msg_id, exc_info=True)

    async def send_file(
        self, channel_id: str, file_path: str, filename: str,
        caption: str | None = None,
    ) -> str:
        channel = self._get_channel(channel_id)
        if not channel:
            return ""

        file = discord.File(file_path, filename=filename)
        msg = await channel.send(content=caption, file=file)
        return str(msg.id)

    def markdown_to_markup(self, md: str) -> str:
        """Discord uses markdown natively — pass through."""
        return md

    def escape(self, text: str) -> str:
        return discord_fmt.escape_discord(text)

    def chunk_message(self, text: str) -> list[str]:
        # Discord regular messages: 2000 char limit
        return discord_fmt.chunk_message(text, limit=2000)
