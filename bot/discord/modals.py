"""Discord modals — QuickTaskModal for spawning sessions from control room."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from bot.engine import commands
from bot.engine.verify import MAX_ITEM_CHARS, add_item

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
    """Modal for manually adding a Verify Board item.

    Used by the `Add` button on the board view, and by the
    `Send to Verify Board` button on session result embeds (where the
    `origin_*` kwargs wire the backlink to the source thread).

    The TextInput is instantiated per-modal-instance so `default=` can
    be set dynamically without leaking between concurrent modal opens
    (class-level `TextInput` objects are templates shared across users).
    """

    def __init__(
        self,
        bot: ClaudeBot,
        repo_name: str,
        *,
        prefill: str = "",
        origin_thread_id: str | None = None,
        origin_thread_name: str | None = None,
        origin_instance_id: str | None = None,
    ) -> None:
        super().__init__(title=f"Add — {repo_name}"[:45])
        self._bot = bot
        self._repo_name = repo_name
        self._origin_thread_id = origin_thread_id
        self._origin_thread_name = origin_thread_name
        self._origin_instance_id = origin_instance_id
        default_text = (prefill or "").strip()[:MAX_ITEM_CHARS] or None
        self.text_input: discord.ui.TextInput = discord.ui.TextInput(
            label="What needs verifying?",
            placeholder="e.g. OI indicator renders on the perp chart sidebar",
            style=discord.TextStyle.short,
            required=True,
            max_length=MAX_ITEM_CHARS,
            default=default_text,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = (self.text_input.value or "").strip()
        if not text:
            await interaction.response.send_message(
                "No text provided.", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        bot = self._bot
        proj = bot._forums.forum_projects.get(self._repo_name)
        if not proj:
            await interaction.followup.send(
                f"Unknown repo: `{self._repo_name}`", ephemeral=True,
            )
            return

        # Narrow lock — only the mutation + persist. Discord followup
        # is kept outside so the lock isn't held across a network round-trip.
        lock = bot._forums.verify_lock(self._repo_name)
        async with lock:
            item = add_item(
                proj.verify_items, text,
                origin_thread_id=self._origin_thread_id,
                origin_thread_name=self._origin_thread_name,
                origin_instance_id=self._origin_instance_id,
            )
            if item is not None:
                bot._forums.save_forum_map()

        if item is None:
            await interaction.followup.send(
                "Duplicate or empty — not added.", ephemeral=True,
            )
            return
        bot._forums.schedule_verify_refresh(self._repo_name)
        await interaction.followup.send(
            f"Added to Verify Board: `{item.text[:60]}`", ephemeral=True,
        )
