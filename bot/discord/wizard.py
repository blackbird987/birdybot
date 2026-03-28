"""Stateless repo setup wizard using ephemeral messages + encoded custom_ids.

All wizard state is encoded in button custom_ids — no server-side dict needed.
Subdirectories use sorted indices (not names) to stay within Discord's 100-char
custom_id limit.  The listing is deterministic (sorted), so indices are stable
within the wizard's ~2-minute interaction window.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from bot import config
from bot.engine import commands

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_workspace_roots(bot: ClaudeBot) -> list[str]:
    """Get workspace roots from config or infer from registered repos."""
    configured = getattr(config, "WORKSPACE_ROOTS", "")
    if configured:
        return [r.strip() for r in configured.split(",") if r.strip()]
    repos = bot._store.list_repos()
    parents = sorted({str(Path(p).parent) for p in repos.values()})
    return parents


def _build_root_picker(mode: str, repo_name: str, roots: list[str]) -> discord.ui.View:
    """Buttons for workspace roots (up to 5)."""
    view = discord.ui.View(timeout=120)
    for i, root in enumerate(roots[:5]):
        label = Path(root).name or root
        view.add_item(discord.ui.Button(
            label=label[:80],
            custom_id=f"wizard:{mode}:browse:{repo_name}:{i}",
            style=discord.ButtonStyle.primary,
        ))
    return view


def _sorted_subdirs(root: str, limit: int = 20) -> list[str]:
    """Return sorted subdirectory names under *root* (up to *limit*)."""
    try:
        return sorted([d.name for d in Path(root).iterdir() if d.is_dir()])[:limit]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Modal — single text field for repo name
# ---------------------------------------------------------------------------

class RepoNameModal(discord.ui.Modal, title="Repo Name"):
    """Collects just the repo name, then shows workspace-root picker."""

    name = discord.ui.TextInput(
        label="Name",
        placeholder="my-project",
        max_length=50,
    )

    def __init__(self, bot: ClaudeBot, mode: str):
        super().__init__()
        self._bot = bot
        self._mode = mode

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Re-check owner (modal callbacks don't carry access context)
        if not self._bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return

        repo_name = self.name.value.strip().replace(" ", "-")
        if not repo_name:
            await interaction.response.send_message("Name cannot be empty.", ephemeral=True)
            return
        if not re.match(r'^[a-zA-Z0-9_-]+$', repo_name):
            await interaction.response.send_message(
                "Repo name must be alphanumeric (hyphens/underscores ok).",
                ephemeral=True,
            )
            return

        roots = get_workspace_roots(self._bot)

        if not roots:
            await interaction.response.send_message(
                f"No known directories.\n"
                f"Type `/repo {self._mode} {repo_name} <path>` to proceed manually.",
                ephemeral=True,
            )
            return

        verb = "is" if self._mode == "add" else "should I create"
        view = _build_root_picker(self._mode, repo_name, roots)
        await interaction.response.send_message(
            f"Where {verb} **{repo_name}**?",
            view=view, ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Top-level Ark button handler
# ---------------------------------------------------------------------------

async def handle_ark_button(
    bot: ClaudeBot,
    interaction: discord.Interaction,
    custom_id: str,
) -> None:
    """Dispatch ark:* button presses."""
    if not bot._is_owner(interaction.user.id):
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return

    action = custom_id.split(":")[1]

    if action == "new_repo":
        view = discord.ui.View(timeout=60)
        view.add_item(discord.ui.Button(
            label="Add Existing Repo",
            style=discord.ButtonStyle.primary,
            custom_id="wizard:add:name_step",
            emoji="\U0001f4c2",
        ))
        view.add_item(discord.ui.Button(
            label="Create New Repo",
            style=discord.ButtonStyle.green,
            custom_id="wizard:create:name_step",
            emoji="\u2728",
        ))
        await interaction.response.send_message(
            "**New Repo Setup**\nAdd an existing directory or create a fresh one?",
            view=view, ephemeral=True,
        )

    elif action == "refresh":
        await interaction.response.defer(ephemeral=True)
        from bot.discord.dashboard import refresh_dashboard
        await refresh_dashboard(
            bot, bot._store, bot._forums,
            bot._lobby_channel_id,
            bot._dashboard_lock, bot._dashboard_pending_flag,
        )
        await interaction.followup.send("Refreshed.", ephemeral=True)

    elif action == "stop_all":
        await interaction.response.defer(ephemeral=True)
        from bot.claude.types import InstanceStatus
        instances = bot._store.list_instances()
        running = [i for i in instances if i.status == InstanceStatus.RUNNING]
        if not running:
            await interaction.followup.send("No running instances.", ephemeral=True)
            return
        killed = 0
        for inst in running:
            try:
                if await bot._runner.kill(inst.id):
                    inst.status = InstanceStatus.KILLED
                    from datetime import datetime, timezone
                    inst.finished_at = datetime.now(timezone.utc).isoformat()
                    bot._store.update_instance(inst)
                    killed += 1
            except Exception:
                log.debug("Failed to kill %s during ark stop_all", inst.id, exc_info=True)
        await interaction.followup.send(
            f"Stopped {killed}/{len(running)} instances.", ephemeral=True,
        )
        from bot.discord.dashboard import refresh_dashboard
        asyncio.create_task(refresh_dashboard(
            bot, bot._store, bot._forums,
            bot._lobby_channel_id,
            bot._dashboard_lock, bot._dashboard_pending_flag,
        ))

    elif action == "claude_login":
        await _handle_claude_login(bot, interaction)

    else:
        await interaction.response.send_message("Unknown action.", ephemeral=True)


async def _handle_claude_login(
    bot: ClaudeBot, interaction: discord.Interaction,
) -> None:
    """Smart auth sync: pull if credentials are waiting, push otherwise."""
    await interaction.response.defer(ephemeral=True)

    from bot.services.auth_sync import (
        credentials_look_valid,
        pull_credentials,
        push_credentials,
        verify_cli,
    )

    lobby_id = bot._lobby_channel_id
    if not lobby_id:
        await interaction.followup.send("No lobby channel configured.", ephemeral=True)
        return

    # Step 1: Try to pull (scan for AUTH_SYNC messages from other PCs)
    source = await pull_credentials(bot, lobby_id)
    if source:
        ok = verify_cli()
        if ok:
            msg = (
                f"\U0001f511 Auth restored on **{config.PC_NAME}** "
                f"from {source} — CLI verified!"
            )
        else:
            msg = (
                f"\u26a0\ufe0f Credentials written on **{config.PC_NAME}** "
                f"from {source}, but CLI verify failed — tokens may be revoked."
            )
        await interaction.followup.send(msg, ephemeral=True)
        # Also broadcast to lobby so it's visible
        if hasattr(bot, "_notifier") and bot._notifier:
            await bot._notifier.broadcast(msg)
        return

    # Step 2: Nothing to pull — try to push this machine's credentials
    if not credentials_look_valid():
        await interaction.followup.send(
            f"\u274c **{config.PC_NAME}**: No valid local credentials and "
            "no AUTH_SYNC messages found.\n"
            "Push from a working machine first.",
            ephemeral=True,
        )
        return

    result = await push_credentials(bot, lobby_id)
    if result:
        await interaction.followup.send(
            f"\U0001f4e4 Credentials pushed from **{config.PC_NAME}**.\n"
            "Tap **Claude Login** on the other machine to pull.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            "Failed to push credentials — check logs.", ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Wizard step handler
# ---------------------------------------------------------------------------

async def handle_wizard_button(
    bot: ClaudeBot,
    interaction: discord.Interaction,
    custom_id: str,
) -> None:
    """Dispatch wizard:* button presses. All steps are owner-gated."""
    if not bot._is_owner(interaction.user.id):
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return

    parts = custom_id.split(":")
    if len(parts) < 3:
        await interaction.response.send_message("Invalid wizard state.", ephemeral=True)
        return
    mode = parts[1]   # "add" or "create"
    step = parts[2]   # "name_step", "browse", "pick", "gh", "confirm"

    if step == "name_step":
        modal = RepoNameModal(bot, mode)
        await interaction.response.send_modal(modal)
        return

    # All remaining steps need defer
    await interaction.response.defer(ephemeral=True)

    if step == "browse":
        # wizard:{mode}:browse:{name}:{root_idx}
        repo_name = parts[3]
        root_idx = int(parts[4])
        roots = get_workspace_roots(bot)
        if root_idx >= len(roots):
            await interaction.followup.send("Workspace root no longer available.", ephemeral=True)
            return
        root = roots[root_idx]

        if mode == "create":
            # Create mode: repo goes at root/repo_name — skip subdirectory browse,
            # go straight to GitHub question
            view = discord.ui.View(timeout=120)
            for gh_opt, label in [
                ("private", "Yes (private)"),
                ("public", "Yes (public)"),
                ("no", "No, local only"),
            ]:
                view.add_item(discord.ui.Button(
                    label=label,
                    custom_id=f"wizard:create:confirm:{repo_name}:{root_idx}:0:{gh_opt}",
                    style=discord.ButtonStyle.primary,
                ))
            full_path = str(Path(root) / repo_name)
            await interaction.followup.send(
                f"Will create at `{full_path}`\nPush to GitHub?",
                view=view, ephemeral=True,
            )
            return

        # Add mode: list subdirectories for the user to pick from
        subdirs = _sorted_subdirs(root)
        if not subdirs:
            await interaction.followup.send(
                f"No subdirectories found in `{Path(root).name}/`.\n"
                f"Type `/repo add {repo_name} <full-path>` to proceed manually.",
                ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=120)
        for si, subdir in enumerate(subdirs):
            cid = f"wizard:add:pick:{repo_name}:{root_idx}:{si}"
            view.add_item(discord.ui.Button(
                label=subdir[:80],
                custom_id=cid,
                style=discord.ButtonStyle.secondary,
            ))
        await interaction.followup.send(
            f"Select folder in `{Path(root).name}/`:",
            view=view, ephemeral=True,
        )

    elif step == "pick":
        # wizard:add:pick:{name}:{root_idx}:{subdir_idx}  (add mode only)
        repo_name = parts[3]
        root_idx, subdir_idx = int(parts[4]), int(parts[5])
        roots = get_workspace_roots(bot)
        if root_idx >= len(roots):
            await interaction.followup.send("Workspace root no longer available.", ephemeral=True)
            return
        root = roots[root_idx]
        subdirs = _sorted_subdirs(root)
        if subdir_idx >= len(subdirs):
            await interaction.followup.send("Directory listing changed. Please try again.", ephemeral=True)
            return
        subdir = subdirs[subdir_idx]
        full_path = str(Path(root) / subdir)

        ctx = bot._ctx(str(interaction.channel_id))
        await commands.on_repo(ctx, f'add {repo_name} "{full_path}"')
        if repo_name in bot._store.list_repos():
            await interaction.followup.send(
                f"\u2705 Added **{repo_name}** \u2192 `{full_path}`",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Failed to add **{repo_name}**. Check The Ark for details.",
                ephemeral=True,
            )

    elif step == "confirm" and mode == "create":
        # wizard:create:confirm:{name}:{root_idx}:{subdir_idx}:{gh_opt}
        repo_name = parts[3]
        root_idx = int(parts[4])
        # subdir_idx not used for create — repo created with repo_name as folder
        gh_opt = parts[6]
        roots = get_workspace_roots(bot)
        if root_idx >= len(roots):
            await interaction.followup.send("Workspace root no longer available.", ephemeral=True)
            return
        root = roots[root_idx]
        full_path = str(Path(root) / repo_name)

        cmd = f'create {repo_name} "{full_path}"'
        if gh_opt in ("private", "public"):
            cmd += " --github"
        if gh_opt == "public":
            cmd += " --public"

        ctx = bot._ctx(str(interaction.channel_id))
        await commands.on_repo(ctx, cmd)
        if repo_name in bot._store.list_repos():
            await interaction.followup.send(
                f"\u2705 Created **{repo_name}** at `{full_path}`",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Failed to create **{repo_name}**. Check The Ark for details.",
                ephemeral=True,
            )

    else:
        await interaction.followup.send("Unknown wizard step.", ephemeral=True)
