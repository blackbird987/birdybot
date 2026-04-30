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

    elif action == "switch_provider":
        await interaction.response.defer(ephemeral=True)
        from bot.claude.provider import PROVIDERS
        from bot import config as _cfg
        available = [k for k, v in PROVIDERS.items() if v is not None]
        if len(available) < 2:
            await interaction.followup.send("Only one provider available.", ephemeral=True)
            return
        # Cycle to next provider
        idx = available.index(_cfg.PROVIDER) if _cfg.PROVIDER in available else 0
        next_p = available[(idx + 1) % len(available)]
        try:
            _cfg.set_provider(next_p)
        except RuntimeError as exc:
            await interaction.followup.send(f"Switch failed: {exc}", ephemeral=True)
            return
        bot._store.active_provider = next_p
        await interaction.followup.send(
            f"Switched to **{next_p}**\nBinary: `{_cfg.CLAUDE_BINARY}`",
            ephemeral=True,
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
    """Show multi-account auth panel: which CLAUDE_CONFIG_DIRs are logged in,
    which Anthropic email each maps to, and per-dir Login + global Sync.
    """
    await interaction.response.defer(ephemeral=True)

    if config.PROVIDER != "claude":
        await interaction.followup.send(
            f"Auth panel is Claude-specific. Current provider: **{config.PROVIDER}**.",
            ephemeral=True,
        )
        return

    from bot.services.auth_sync import (
        collect_account_statuses,
        host_can_show_console,
    )

    account_dirs = list(config.CLAUDE_ACCOUNTS) or [
        str(Path.home() / config.PROVIDER_DIR_NAME)
    ]
    cooldowns = getattr(bot._runner, "_account_cooldowns", {}) or {}
    statuses = await collect_account_statuses(account_dirs, cooldowns)

    can_console = host_can_show_console()
    embed = _build_auth_panel_embed(statuses, can_console)
    view = _build_auth_panel_view(statuses, can_console)

    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


def _build_auth_panel_embed(statuses: list, can_console: bool) -> discord.Embed:
    """Render account statuses + hint lines as an ephemeral embed."""
    from datetime import datetime as _dt, timezone as _tz

    title = f"Claude Auth — {config.PC_NAME}"
    embed = discord.Embed(title=title, color=discord.Color.blurple())

    if not statuses:
        embed.description = "No CLAUDE_ACCOUNTS configured."
        return embed

    now = _dt.now(_tz.utc)
    lines: list[str] = []
    for st in statuses:
        mark = "✅" if st.logged_in else "✗"
        ident = st.email or (
            "(not logged in)" if not st.logged_in else "(unknown identity)"
        )
        org = f" · _{st.org}_" if st.org else ""
        line = f"{mark} **`{st.label}`** — {ident}{org}"
        if st.cooldown_until and st.cooldown_until > now:
            mins = max(1, int((st.cooldown_until - now).total_seconds() // 60))
            line += f"  · cooldown {mins}m (UTC)"
        if st.error:
            line += f"  · error: {st.error[:60]}"
        lines.append(line)
    embed.description = "\n".join(lines)

    uuids = [s.account_uuid for s in statuses if s.account_uuid]
    if len(uuids) != len(set(uuids)):
        embed.add_field(
            name="⚠️ Duplicate accounts",
            value=(
                "Two config dirs map to the same Anthropic account "
                "— failover won’t help."
            ),
            inline=False,
        )

    hints: list[str] = []
    if all(not s.logged_in for s in statuses):
        hints.append(
            "All dirs are unauthenticated — try **Sync credentials** "
            "from another PC, or log in below."
        )
    elif all(s.cooldown_until and s.cooldown_until > now for s in statuses):
        hints.append(
            "All accounts are on cooldown. Wait or add another account "
            "to `CLAUDE_ACCOUNTS`."
        )
    if not can_console:
        hints.append(
            "This host can’t pop up a terminal — log in on the machine "
            "running the bot, or use **Sync credentials**."
        )
    if hints:
        embed.add_field(name="Hint", value="\n".join(hints), inline=False)

    return embed


def _build_auth_panel_view(statuses: list, can_console: bool) -> discord.ui.View:
    """Build per-account Login buttons + Sync/Refresh row."""
    view = discord.ui.View(timeout=300)

    for i, st in enumerate(statuses[:4]):
        label = (
            f"Log in {st.label}" if not st.logged_in
            else f"Re-login {st.label}"
        )
        view.add_item(discord.ui.Button(
            label=label[:80],
            style=(
                discord.ButtonStyle.primary if not st.logged_in
                else discord.ButtonStyle.secondary
            ),
            custom_id=f"auth:login:{i}",
            disabled=not can_console,
            row=0,
        ))

    view.add_item(discord.ui.Button(
        label="Sync credentials",
        style=discord.ButtonStyle.secondary,
        custom_id="auth:sync",
        emoji="\U0001f504",
        row=1,
    ))
    view.add_item(discord.ui.Button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        custom_id="auth:refresh",
        row=1,
    ))
    return view


async def handle_auth_button(
    bot: ClaudeBot,
    interaction: discord.Interaction,
    custom_id: str,
) -> None:
    """Dispatch auth:* button presses."""
    if not bot._is_owner(interaction.user.id):
        await interaction.response.send_message("Owner only.", ephemeral=True)
        return

    parts = custom_id.split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "login" and len(parts) >= 3:
        await interaction.response.defer(ephemeral=True)
        try:
            idx = int(parts[2])
        except ValueError:
            await interaction.followup.send("Invalid login index.", ephemeral=True)
            return

        from bot.services.auth_sync import (
            host_can_show_console, launch_login_terminal,
        )

        account_dirs = list(config.CLAUDE_ACCOUNTS) or [
            str(Path.home() / config.PROVIDER_DIR_NAME)
        ]
        if idx >= len(account_dirs):
            await interaction.followup.send(
                "Account no longer in config.", ephemeral=True,
            )
            return

        if not host_can_show_console():
            await interaction.followup.send(
                "This host can’t pop up a terminal. Log in on the bot machine "
                "directly, or use **Sync credentials** from another PC.",
                ephemeral=True,
            )
            return

        target = account_dirs[idx]
        ok = launch_login_terminal(target)
        if ok:
            await interaction.followup.send(
                f"Opened a terminal for `{Path(target).name}` on "
                f"**{config.PC_NAME}**.\n"
                f"Run `/login` inside, then tap **Refresh** here.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Failed to open a login terminal — check bot logs.",
                ephemeral=True,
            )
        return

    if action == "sync":
        await _handle_cross_pc_sync(bot, interaction)
        return

    if action == "refresh":
        await interaction.response.defer(ephemeral=True)
        from bot.services.auth_sync import (
            collect_account_statuses, host_can_show_console,
        )
        account_dirs = list(config.CLAUDE_ACCOUNTS) or [
            str(Path.home() / config.PROVIDER_DIR_NAME)
        ]
        cooldowns = getattr(bot._runner, "_account_cooldowns", {}) or {}
        statuses = await collect_account_statuses(account_dirs, cooldowns)
        can_console = host_can_show_console()
        embed = _build_auth_panel_embed(statuses, can_console)
        view = _build_auth_panel_view(statuses, can_console)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        return

    await interaction.response.send_message("Unknown auth action.", ephemeral=True)


async def _handle_cross_pc_sync(
    bot: ClaudeBot, interaction: discord.Interaction,
) -> None:
    """Cross-PC credential sync: pull if a push is waiting, otherwise push."""
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
        if hasattr(bot, "_notifier") and bot._notifier:
            await bot._notifier.broadcast(msg)
        return

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
            "Tap **Sync credentials** on the other machine to pull.",
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
