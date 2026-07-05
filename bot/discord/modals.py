"""Discord modals — QuickTaskModal for spawning sessions from control room."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from bot.engine import commands

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot
    from bot.discord.access import AccessResult

log = logging.getLogger(__name__)


class QuickTaskModal(discord.ui.Modal):
    """Modal for Quick Task — collects a prompt and spawns a session."""

    prompt_input = discord.ui.TextInput(
        label="Prompt",
        placeholder="What should Claude do?",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
    )

    def __init__(self, bot: ClaudeBot, repo_name: str, access: AccessResult):
        super().__init__(title=f"Quick Task — {repo_name}"[:45])
        self._bot = bot
        self._repo_name = repo_name
        self._access = access

    async def on_submit(self, interaction: discord.Interaction) -> None:
        prompt = self.prompt_input.value
        if not prompt or not prompt.strip():
            await interaction.response.send_message("No prompt provided.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        bot = self._bot

        thread = await bot._forums.get_or_create_session_thread(
            self._repo_name, None, prompt[:60],
        )
        if not thread:
            await interaction.followup.send("Could not create thread.", ephemeral=True)
            return

        thread_id = str(thread.id)
        lookup = bot._forums.thread_to_project(thread_id)
        t_info = lookup[1] if lookup else None
        ctx = bot._ctx(thread_id, repo_name=self._repo_name, thread_info=t_info,
                       access_result=self._access)
        ctx.user_id = str(interaction.user.id)
        ctx.user_name = interaction.user.display_name
        if t_info:
            bot._forums.attach_session_callbacks(ctx, t_info, thread_id)

        # Auto-follow creator to the thread
        try:
            await thread.add_user(interaction.user)
        except Exception:
            pass

        await interaction.followup.send(
            f"Quick task started: <#{thread.id}>", ephemeral=True,
        )

        asyncio.create_task(bot._send_redirect(thread))
        asyncio.create_task(bot._set_thread_active_tag(thread, True))
        try:
            await commands.on_text(ctx, prompt)
        finally:
            bot._forums.persist_ctx_settings(ctx)
            asyncio.create_task(bot._try_apply_tags_after_run(thread_id))
            bot._schedule_sleep(thread_id)
            asyncio.create_task(bot._refresh_dashboard())
