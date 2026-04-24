"""Discord modals — QuickTaskModal for spawning sessions from control room."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from bot.engine import commands
from bot.engine import verify as verify_mod

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


class VerifyAddModal(discord.ui.Modal):
    """Modal for adding an item to a repo's verify-board.

    Optionally pre-filled with a session-derived prompt and an origin
    (thread + instance) so the new item carries a backlink.
    """

    def __init__(
        self,
        bot: ClaudeBot,
        repo_name: str,
        *,
        prefill: str = "",
        origin_thread_id: int | None = None,
        origin_thread_name: str | None = None,
        origin_instance_id: str | None = None,
    ):
        super().__init__(title=f"Add to verify-board — {repo_name}"[:45])
        self._bot = bot
        self._repo_name = repo_name
        self._origin_thread_id = origin_thread_id
        self._origin_thread_name = origin_thread_name
        self._origin_instance_id = origin_instance_id
        # Construct per-instance to avoid sharing state across modals
        self.text_input = discord.ui.TextInput(
            label="What needs verifying?",
            placeholder="e.g. Confirm new chart renders on mobile",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=verify_mod.MAX_TEXT_LEN,
            default=prefill[: verify_mod.MAX_TEXT_LEN] if prefill else None,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = (self.text_input.value or "").strip()
        if not text:
            await interaction.response.send_message(
                "Nothing to add.", ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Ensure the board exists before mutating — abort if it can't be created
        try:
            board = await self._bot._forums.ensure_verify_board(self._repo_name)
        except Exception:
            log.debug("ensure_verify_board failed before add", exc_info=True)
            board = None
        if not board:
            await interaction.followup.send(
                f"Could not open verify-board for `{self._repo_name}`.",
                ephemeral=True,
            )
            return

        def _do(items: list[dict]) -> dict:
            return verify_mod.add_item(
                items, text,
                origin_thread_id=self._origin_thread_id,
                origin_thread_name=self._origin_thread_name,
                origin_instance_id=self._origin_instance_id,
            )

        item = await self._bot._forums._mutate_verify(self._repo_name, _do)
        if not item:
            await interaction.followup.send(
                f"No verify-board for repo `{self._repo_name}`.", ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Added to verify-board: {item['text']}", ephemeral=True,
        )
