"""DiscordMessenger — implements the Messenger protocol for Discord."""

from __future__ import annotations

import asyncio
import logging

import discord

from bot.discord import channels, formatter as discord_fmt
from bot.platform.base import ButtonSpec, MessageHandle
from bot.platform.formatting import FinalizeInfo

log = logging.getLogger(__name__)

# Button style map: action prefix -> Discord button style
_STYLE_MAP = {
    "merge:": discord.ButtonStyle.success,     # Green
    "build:": discord.ButtonStyle.success,
    "commit:": discord.ButtonStyle.success,
    "done:": discord.ButtonStyle.success,
    "kill:": discord.ButtonStyle.danger,       # Red
    "discard:": discord.ButtonStyle.danger,
    "cancel_pending:": discord.ButtonStyle.danger,  # Red — matches Cancel intent
    "steer:": discord.ButtonStyle.primary,     # Blue — matches other action buttons
    "retry:": discord.ButtonStyle.primary,     # Blue
    "plan:": discord.ButtonStyle.primary,        # Blue
    "review_plan:": discord.ButtonStyle.primary,
    "apply_revisions:": discord.ButtonStyle.primary,
    "review_code:": discord.ButtonStyle.primary,
    "mode_build:": discord.ButtonStyle.success,    # Green
    "mode_plan:": discord.ButtonStyle.primary,     # Blue
    "mode_explore:": discord.ButtonStyle.primary,  # Blue
    "autopilot:": discord.ButtonStyle.success,     # Green
    "autopilot_hold:": discord.ButtonStyle.primary,  # Blue — test before merge
    "build_and_ship:": discord.ButtonStyle.success, # Green
    "continue_autopilot:": discord.ButtonStyle.success,  # Green
    "continue_ppu:": discord.ButtonStyle.danger,           # Red — signals cost
}


def _button_style(callback_data: str) -> discord.ButtonStyle:
    """Determine button style from callback data prefix."""
    for prefix, style in _STYLE_MAP.items():
        if callback_data.startswith(prefix):
            return style
    return discord.ButtonStyle.secondary  # Gray default


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
                style=_button_style(btn_spec.callback_data),
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

    def find_channel_for_session(self, session_id: str) -> str | None:
        """Find the Discord channel/thread ID for a session. Returns None if not found."""
        result = self._bot._session_to_thread(session_id)
        return result[0] if result else None

    @property
    def platform_name(self) -> str:
        return "discord"

    def _get_guild(self) -> discord.Guild | None:
        return self._bot.get_guild(self._guild_id)

    async def _resolve_channel(self, channel_id: str) -> discord.abc.Messageable | None:
        """Resolve channel/thread ID, fetching from API if not cached (archived threads)."""
        ch = self._bot.get_channel(int(channel_id))
        if ch is not None:
            return ch
        try:
            ch = await self._bot.fetch_channel(int(channel_id))
            return ch
        except discord.NotFound:
            log.warning("Channel %s not found", channel_id)
            return None
        except discord.Forbidden:
            log.warning("No access to channel %s", channel_id)
            return None

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
            lobby = await self._resolve_channel(str(self._lobby_channel_id))
            if isinstance(lobby, discord.TextChannel):
                name = f"q-{instance_id}-{summary[:60]}"
                thread = await channels.create_thread(lobby, name)
                return str(thread.id)
            return str(self._lobby_channel_id)

    async def send_thinking(
        self, channel_id: str, text: str,
        buttons: list[list[ButtonSpec]] | None = None,
    ) -> MessageHandle:
        """Send a thinking/progress message as a blue-sidebar embed."""
        channel = await self._resolve_channel(channel_id)
        if not channel:
            return MessageHandle(platform="discord", _data={})

        embed = discord.Embed(
            description=text[:4096],
            color=discord.Color.blurple(),
        )
        view = _buttons_to_view(buttons)
        msg = await channel.send(embed=embed, view=view)
        return MessageHandle(
            platform="discord",
            _data={"channel_id": channel_id, "message_id": str(msg.id)},
        )

    async def edit_thinking(
        self, handle: MessageHandle, text: str,
        buttons: list[list[ButtonSpec]] | None = None,
        *, footer: str | None = None, severity: str | None = None,
    ) -> None:
        """Edit a thinking/progress embed.

        *severity* controls color: None=blurple, "warn"=gold, "crit"=red.
        *footer* is rendered in the embed footer slot when provided.
        """
        channel_id = handle.get("channel_id")
        message_id = handle.get("message_id")
        if not channel_id or not message_id:
            return

        channel = await self._resolve_channel(channel_id)
        if not channel:
            return

        if severity == "crit":
            color = discord.Color.red()
        elif severity == "warn":
            color = discord.Color.gold()
        else:
            color = discord.Color.blurple()

        try:
            msg = await channel.fetch_message(int(message_id))
            embed = discord.Embed(
                description=text[:4096],
                color=color,
            )
            if footer:
                embed.set_footer(text=footer[:2048])
            view = _buttons_to_view(buttons)
            await msg.edit(embed=embed, view=view)
        except Exception:
            log.debug("Failed to edit thinking message %s", message_id, exc_info=True)

    async def send_text(
        self, channel_id: str, text: str,
        buttons: list[list[ButtonSpec]] | None = None,
        silent: bool = False,
    ) -> str:
        """Send a text message."""
        channel = await self._resolve_channel(channel_id)
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
        mention_user_id: str | None = None,
    ) -> str:
        """Send a result as a structured embed with inline fields."""
        channel = await self._resolve_channel(channel_id)
        if not channel:
            return ""

        # Determine embed color from status + mode
        color = discord.Color.green()
        if metadata:
            status = metadata.get("_status")
            mode = metadata.get("_mode")
            if status == "failed":
                color = discord.Color.red()
            elif status == "killed":
                color = discord.Color.orange()
            elif mode == "plan":
                color = discord.Color.blurple()
            elif mode == "build":
                color = discord.Color.green()
            else:
                color = discord.Color.blue()  # explore

        # Deferred revisions embed (from autopilot plan review loop)
        is_deferred = metadata.get("_deferred") if metadata else False
        if is_deferred:
            color = discord.Color.light_grey()

        # Check for structured finalize info (commit/done/release)
        finalize = metadata.get("_finalize") if metadata else None
        if finalize:
            embed = self._build_finalize_embed(finalize, color, metadata)
        else:
            embed = discord.Embed(
                description=text[:4096],
                color=color,
            )
            # Add metadata as inline fields (Duration, Cost, Repo, Branch)
            if metadata:
                for k, v in metadata.items():
                    if not k.startswith("_") and v:
                        embed.add_field(name=k, value=str(v), inline=True)

        view = _buttons_to_view(buttons)
        # Include @mention as content alongside the embed to ping the user.
        # Force non-silent when mentioning so the notification actually fires.
        mention_content = self.format_mention(mention_user_id) if mention_user_id else None
        effective_silent = False if mention_content else silent
        msg = await channel.send(
            content=mention_content, embed=embed, view=view,
            silent=effective_silent,
        )
        return str(msg.id)

    @staticmethod
    def _build_finalize_embed(
        finalize: FinalizeInfo,
        color: discord.Color,
        metadata: dict | None,
    ) -> discord.Embed:
        """Build a rich embed for commit/done/release results."""
        info = finalize

        # Title: version release or commit — clean, no emojis
        if info.version:
            title = f"Released {info.version}"
            color = discord.Color.gold()
        else:
            title = "Changes Committed"

        embed = discord.Embed(title=title, color=color)

        # Commit field (Discord field limit: 1024 chars)
        if info.commit_hash:
            commit_val = f"`{info.commit_hash}`"
            if info.commit_message:
                commit_val += f" {info.commit_message}"
            if len(commit_val) > 1024:
                commit_val = commit_val[:1021] + "..."
            embed.add_field(
                name="Commit",
                value=commit_val,
                inline=False,
            )

        # Changelog field
        if info.changelog_entries:
            entries = "\n".join(f"\u2022 {e}" for e in info.changelog_entries)
            if len(entries) > 1000:
                entries = entries[:997] + "..."
            embed.add_field(
                name="Changelog",
                value=entries,
                inline=False,
            )

        # Stats bar — compact single-line summary of session metrics
        if metadata:
            stats_parts = []
            for key in ("Duration", "Turns", "Tokens", "Cost"):
                val = metadata.get(key)
                if val:
                    stats_parts.append(f"**{key}** {val}")
            if stats_parts:
                embed.add_field(
                    name="Stats",
                    value=" \u2502 ".join(stats_parts),  # │ separator
                    inline=False,
                )
            # Deferred revisions from autopilot plan review
            deferred = metadata.get("_deferred_revisions")
            if deferred:
                items = "\n".join(f"\u2022 {d}" for d in deferred[:10])
                if len(items) > 1000:
                    items = items[:997] + "..."
                embed.add_field(
                    name="Deferred Revisions",
                    value=items,
                    inline=False,
                )
            # Remaining metadata (Branch, Mode, etc.) as inline fields
            skip = {"Duration", "Turns", "Tokens", "Cost"}
            for k, v in metadata.items():
                if not k.startswith("_") and k not in skip and v:
                    embed.add_field(name=k, value=str(v), inline=True)

        return embed

    async def edit_text(
        self, channel_id: str, msg_id: str | None, text: str | None,
        buttons: list[list[ButtonSpec]] | None = None,
    ) -> None:
        """Edit a message."""
        if not msg_id:
            return
        channel = await self._resolve_channel(channel_id)
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
        channel = await self._resolve_channel(channel_id)
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
        channel = await self._resolve_channel(channel_id)
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

    def format_mention(self, user_id: str) -> str | None:
        return f"<@{user_id}>"

    def chunk_message(self, text: str) -> list[str]:
        # Discord regular messages: 2000 char limit
        return discord_fmt.chunk_message(text, limit=2000)

    async def on_repo_added(self, repo_name: str) -> None:
        """Create forum channel + control post for newly added repo."""
        forums = getattr(self._bot, "_forums", None)
        if forums:
            try:
                await forums.get_or_create_forum(repo_name)
                asyncio.create_task(forums.ensure_control_post(repo_name))
            except Exception:
                log.warning("Failed to auto-create forum for %s", repo_name)
            refresh = getattr(self._bot, "_refresh_dashboard", None)
            if refresh:
                asyncio.create_task(refresh())

    async def on_deploy_state_changed(self, repo_name: str) -> None:
        """Refresh the control room embed for this repo."""
        forums = getattr(self._bot, "_forums", None)
        if forums:
            await forums.refresh_control_room(repo_name)

    async def close_conversation(self, channel_id: str, *, skip_mention: bool = False) -> None:
        """Archive a Discord forum thread, optionally mentioning participants.

        If *skip_mention* is True, the result embed already pinged the user
        so we skip the redundant mention (avoids double-ping).
        Does not lock — users can reopen by posting (Discord auto-unarchives).
        """
        ch = await self._resolve_channel(channel_id)
        if not ch or not isinstance(ch, discord.Thread):
            return
        if ch.archived:
            return
        try:
            # Mention users who interacted with this thread (unless already pinged)
            if not skip_mention:
                thread_info = self._bot._forums.thread_to_project(channel_id)
                if thread_info:
                    _, info = thread_info
                    # Filter out the bot's own ID
                    bot_id = str(self._bot.user.id) if self._bot.user else None
                    user_ids = {uid for uid in info.user_ids if uid != bot_id}
                    if user_ids:
                        mentions = " ".join(f"<@{uid}>" for uid in user_ids)
                        await ch.send(f"Thread archived. {mentions}")
                        # Wait for Discord to fanout the notification before archiving
                        await asyncio.sleep(1.5)

            # Post to archive channel (isolated — never blocks archiving)
            try:
                await self._bot._forums.post_archive_entry(channel_id)
            except Exception:
                log.debug("Archive post failed for %s", channel_id, exc_info=True)

            await ch.edit(archived=True)
            log.info("Closed (archived) thread %s", channel_id)
        except Exception:
            log.debug("Failed to close thread %s", channel_id, exc_info=True)
