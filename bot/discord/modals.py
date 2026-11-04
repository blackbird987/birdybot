"""Discord modals — QuickTaskModal for spawning sessions from control room."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from bot import secret_detect, vault
from bot.discord import secret_door
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
        # The door comes before prompt[:60], which would name a thread after a key.
        await secret_door.screen_modal_text(
            self._bot, interaction, self._access, prompt, self._repo_name,
            lambda clean: self._run(interaction, clean),
        )

    async def _run(self, interaction: discord.Interaction, prompt: str) -> None:
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
            await bot._after_turn(ctx, thread_id)


def _message_without_row(message: discord.Message, custom_id: str) -> discord.ui.View | None:
    """The buttons of ``message``, less the row holding ``custom_id``.

    One report can list several secrets, and acting on one leaves the others.
    """
    view = discord.ui.View.from_message(message, timeout=None)
    row = next(
        (item.row for item in view.children if getattr(item, "custom_id", None) == custom_id),
        None,
    )
    for item in list(view.children):
        if item.row == row:
            view.remove_item(item)
    return view if view.children else None


class SecretRenameModal(discord.ui.Modal, title="Rename secret"):
    """Opened by the Rename button on a stored secret's line."""

    new_name = discord.ui.TextInput(
        label="New name", placeholder="STRIPE_API_KEY", max_length=64,
    )

    def __init__(self, bot: ClaudeBot, name: str, custom_id: str):
        super().__init__()
        self._bot = bot
        self._name = name
        self._button_id = custom_id
        self.new_name.default = name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self._bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        wanted = self.new_name.value.strip()
        if secret_detect.scan(wanted):
            await interaction.response.send_message(
                "That looks like a value rather than a name, so I did not use it.",
                ephemeral=True,
            )
            return
        try:
            final = await asyncio.to_thread(vault.rename, self._name, wanted)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except vault.VaultUnavailable:
            await interaction.response.send_message(
                "The secret vault file can't be read, so nothing changed.", ephemeral=True,
            )
            return
        line = f"Renamed `{self._name}` to `{final}`."
        if interaction.message is None:
            await interaction.response.send_message(line, ephemeral=True)
            return
        await interaction.response.edit_message(
            content=f"{interaction.message.content}\n{line}"[:2000],
            view=_message_without_row(interaction.message, self._button_id),
        )


class SecretAddModal(discord.ui.Modal, title="Add secret"):
    """/secret add: the value never passes through chat or a slash option."""

    secret_name = discord.ui.TextInput(
        label="Name (optional)", placeholder="STRIPE_API_KEY", required=False, max_length=64,
    )
    value = discord.ui.TextInput(
        label="Value", style=discord.TextStyle.paragraph, max_length=4000,
    )

    def __init__(self, bot: ClaudeBot, repo: str | None):
        super().__init__()
        self._bot = bot
        self._repo = repo

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self._bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        value = self.value.value.strip()
        wanted = self.secret_name.value.strip()
        if wanted and (secret_detect.scan(wanted) or not vault.valid_name(wanted)):
            await interaction.response.send_message(
                "Names use capitals, digits and underscores, 3 to 64 characters. Nothing was stored.",
                ephemeral=True,
            )
            return
        findings = secret_detect.scan(value, attachment=True)
        if any(f.tier == secret_detect.BURN for f in findings):
            await interaction.response.send_message(
                "That looks like a seed phrase or private key, and those are never stored. "
                "Discord already has it, so treat it as exposed and move the funds.",
                ephemeral=True,
            )
            return
        name = wanted or (findings[0].suggested_name if findings else None) or secret_detect.default_name(value)
        try:
            result = await asyncio.to_thread(
                vault.add, value, name, self._repo, str(interaction.channel_id),
            )
        except vault.VaultUnavailable:
            await interaction.response.send_message(
                "The secret vault file can't be read, so nothing was stored.", ephemeral=True,
            )
            return
        await interaction.response.send_message(
            secret_door._stored_line(result, self._repo), ephemeral=True,
        )
