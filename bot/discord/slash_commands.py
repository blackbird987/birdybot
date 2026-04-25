"""Slash command registration for Discord bot."""

from __future__ import annotations

import asyncio
import json as _json
import logging
import time as _time
from pathlib import Path as _Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from bot.discord import access as access_mod
from bot.discord import channels
from bot.discord.access import (
    AccessResult, load_access_config, check_user_access,
    effective_mode as access_effective_mode,
)
from bot.claude.types import InstanceStatus
from bot.discord.monitoring import monitor_setup
from bot.engine import commands
from bot.engine import sessions as sessions_mod
from bot.platform.formatting import MODE_DISPLAY, VALID_MODES, format_age, mode_name
from bot.store import history as history_mod

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

_DISCORD_EPOCH_MS = 1420070400000


def _snowflake_age(snowflake_id: int) -> str:
    """Human-readable age from a Discord snowflake ID."""
    created_ms = (snowflake_id >> 22) + _DISCORD_EPOCH_MS
    from datetime import datetime, timezone
    created = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
    return format_age(datetime.now(timezone.utc) - created)


def setup(bot: ClaudeBot) -> None:
    """Register all slash commands on the bot's command tree."""
    guild_obj = discord.Object(id=bot._guild_id)

    @bot.tree.command(name="status", description="Health dashboard", guild=guild_obj)
    async def cmd_status(interaction: discord.Interaction):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, commands.on_status)

    @bot.tree.command(name="cost", description="Spending breakdown", guild=guild_obj)
    async def cmd_cost(interaction: discord.Interaction):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, commands.on_cost)

    @bot.tree.command(name="usage", description="Token usage & rate limit estimates", guild=guild_obj)
    @app_commands.describe(force="Force refresh (bypass cache)")
    async def cmd_usage(interaction: discord.Interaction, force: bool = False):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        # Dedicated ephemeral flow — bypasses _run_slash so the response
        # appears as a private interaction reply, never blocks on subprocess.
        await interaction.response.defer(ephemeral=True)
        try:
            channel_id = str(interaction.channel_id)
            lookup = bot._forums.thread_to_project(channel_id)
            info = lookup[1] if lookup else None
            ar = bot._check_access(interaction.user.id, channel_id=channel_id)
            ctx = bot._ctx(channel_id, thread_info=info, access_result=ar)
            ctx.user_id = str(interaction.user.id)
            ctx.user_name = interaction.user.display_name
            text = await commands.on_usage(ctx, force=force)
            if len(text) > 2000:
                text = text[:1997] + "..."
            await interaction.followup.send(text, ephemeral=True)
        except Exception:
            log.exception("/usage failed")
            try:
                await interaction.followup.send(
                    "Usage data unavailable \u2014 check logs.", ephemeral=True,
                )
            except Exception:
                pass

    @bot.tree.command(name="list", description="Show instances", guild=guild_obj)
    @app_commands.describe(scope="Show all instances or just recent")
    async def cmd_list(interaction: discord.Interaction, scope: str = ""):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_list(ctx, scope))

    @bot.tree.command(name="bg", description="Background task (build mode)", guild=guild_obj)
    @app_commands.describe(prompt="Task description")
    async def cmd_bg(interaction: discord.Interaction, prompt: str):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_bg(ctx, prompt))

    @bot.tree.command(name="release", description="Cut a versioned release", guild=guild_obj)
    @app_commands.describe(level="patch, minor, major, or explicit version (default: patch)")
    async def cmd_release(interaction: discord.Interaction, level: str = "patch"):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_release(ctx, level))

    @bot.tree.command(name="kill", description="Terminate instance", guild=guild_obj)
    @app_commands.describe(target="Instance ID or name")
    async def cmd_kill(interaction: discord.Interaction, target: str):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_kill(ctx, target))

    @bot.tree.command(name="retry", description="Re-run instance", guild=guild_obj)
    @app_commands.describe(target="Instance ID or name")
    async def cmd_retry(interaction: discord.Interaction, target: str):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_retry(ctx, target))

    @bot.tree.command(name="log", description="Full output", guild=guild_obj)
    @app_commands.describe(target="Instance ID or name")
    async def cmd_log(interaction: discord.Interaction, target: str):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_log(ctx, target))

    @bot.tree.command(name="export", description="Export session as HTML transcript", guild=guild_obj)
    @app_commands.describe(target="Instance ID or name")
    async def cmd_export(interaction: discord.Interaction, target: str):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_share(ctx, target))

    @bot.tree.command(name="diff", description="Git diff", guild=guild_obj)
    @app_commands.describe(target="Instance ID or name")
    async def cmd_diff(interaction: discord.Interaction, target: str):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_diff(ctx, target))

    @bot.tree.command(name="merge", description="Merge branch", guild=guild_obj)
    @app_commands.describe(target="Instance ID or name")
    async def cmd_merge(interaction: discord.Interaction, target: str):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_merge(ctx, target))

    @bot.tree.command(name="discard", description="Delete branch", guild=guild_obj)
    @app_commands.describe(target="Instance ID or name")
    async def cmd_discard(interaction: discord.Interaction, target: str):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_discard(ctx, target))

    @bot.tree.command(name="branches", description="List orphaned branches", guild=guild_obj)
    async def cmd_branches(interaction: discord.Interaction):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, commands.on_branches)

    @bot.tree.command(name="mode", description="View/set mode", guild=guild_obj)
    @app_commands.describe(mode="explore or build")
    async def cmd_mode(interaction: discord.Interaction, mode: str = ""):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_mode(ctx, mode))

    @bot.tree.command(name="verbose", description="Progress detail level", guild=guild_obj)
    @app_commands.describe(level="0, 1, or 2")
    async def cmd_verbose(interaction: discord.Interaction, level: str = ""):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_verbose(ctx, level))

    @bot.tree.command(name="effort", description="Reasoning effort level", guild=guild_obj)
    @app_commands.describe(level="low, medium, high, or max")
    async def cmd_effort(interaction: discord.Interaction, level: str = ""):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_effort(ctx, level))

    @bot.tree.command(name="provider", description="View or switch CLI provider", guild=guild_obj)
    @app_commands.describe(name="Provider: claude, cursor")
    async def cmd_provider(interaction: discord.Interaction, name: str = ""):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Owner only", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_provider(ctx, name))

    @bot.tree.command(name="context", description="Pinned context", guild=guild_obj)
    @app_commands.describe(args="set <text> | clear")
    async def cmd_context(interaction: discord.Interaction, args: str = ""):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_context(ctx, args))

    @bot.tree.command(name="repo", description="Repo management", guild=guild_obj)
    @app_commands.describe(args="add|switch|list [name] [path]")
    async def cmd_repo(interaction: discord.Interaction, args: str = ""):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        stripped = args.strip()
        repos = bot._store.list_repos()
        if len(repos) >= 2 and stripped in ("", "switch"):
            active, _ = bot._store.get_active_repo()
            select = discord.ui.Select(
                placeholder="Switch repo...",
                custom_id="repo_switch_select",
                options=[
                    discord.SelectOption(
                        label=name, description=path[:80], value=name,
                        default=(name == active),
                    )
                    for name, path in repos.items()
                ],
            )
            view = discord.ui.View(timeout=60)
            view.add_item(select)
            lines = []
            for name, path in repos.items():
                marker = " \\*" if name == active else ""
                lines.append(f"`{name}`{marker} → `{path}`")
            await interaction.response.send_message(
                "\n".join(lines), view=view, ephemeral=True,
            )
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_repo(ctx, args))

    @bot.tree.command(name="session", description="List/resume sessions", guild=guild_obj)
    @app_commands.describe(args="resume <id> | drop")
    async def cmd_session(interaction: discord.Interaction, args: str = ""):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_session(ctx, args))

    @bot.tree.command(name="schedule", description="Recurring tasks", guild=guild_obj)
    @app_commands.describe(args="every|at|list|delete ...")
    async def cmd_schedule(interaction: discord.Interaction, args: str = ""):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_schedule(ctx, args))

    @bot.tree.command(name="alias", description="Command shortcuts", guild=guild_obj)
    @app_commands.describe(args="set|delete|list ...")
    async def cmd_alias(interaction: discord.Interaction, args: str = ""):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_alias(ctx, args))

    @bot.tree.command(name="budget", description="Budget info/reset", guild=guild_obj)
    @app_commands.describe(args="reset")
    async def cmd_budget(interaction: discord.Interaction, args: str = ""):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_budget(ctx, args))

    @bot.tree.command(name="report", description="Session quality report", guild=guild_obj)
    @app_commands.describe(days="Number of days to cover (default: 1)")
    async def cmd_report(interaction: discord.Interaction, days: int = 1):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        from bot.engine.report import full_report
        try:
            text = full_report(days=days)
        except Exception as exc:
            log.warning("Report generation failed", exc_info=True)
            text = f"Report generation failed: {exc}"
        await interaction.followup.send(embed=discord.Embed(
            title=f"Session Report ({days}d)",
            description=text[:4096],
            color=0x5865F2,
        ), ephemeral=True)

    @bot.tree.command(name="new", description="Start fresh conversation", guild=guild_obj)
    @app_commands.describe(
        repo="Repo name (default: active repo)",
        mode="Permission mode for the session",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name=name, value=key)
        for key, name in MODE_DISPLAY.items()
    ])
    async def cmd_new(interaction: discord.Interaction, repo: str = "", mode: str = ""):
        is_owner = bot._is_owner(interaction.user.id)
        if not is_owner:
            access = bot._check_access(interaction.user.id)
            if not access.allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return

        user_id = None if is_owner else str(interaction.user.id)
        user_name = None if is_owner else interaction.user.display_name
        mode = mode.strip().lower()
        new_thread_mode = mode if mode and mode in VALID_MODES else None

        if is_owner:
            available_repos = list(bot._store.list_repos().keys())
        else:
            cfg = load_access_config()
            ua = cfg.users.get(str(interaction.user.id))
            if ua and ua.global_access:
                available_repos = list(bot._store.list_repos().keys())
            elif ua:
                all_repos = bot._store.list_repos()
                available_repos = [r for r in ua.repos if r in all_repos]
            else:
                available_repos = []
            if new_thread_mode:
                grant = check_user_access(cfg, str(interaction.user.id), repo.strip() or None)
                if grant:
                    new_thread_mode = access_effective_mode(grant, new_thread_mode)

        repo = repo.strip()
        if repo:
            lower_map = {k.lower(): k for k in available_repos}
            repo_name = repo if repo in available_repos else lower_map.get(repo.lower())
            if not repo_name:
                await interaction.response.send_message(
                    f"Repo '{repo}' not found.", ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True)
            await bot._create_new_session(
                interaction, repo_name, mode=new_thread_mode,
                user_id=user_id, user_name=user_name,
            )
        else:
            if len(available_repos) == 0:
                await interaction.response.send_message("No repos available.", ephemeral=True)
            elif len(available_repos) == 1:
                await interaction.response.defer(ephemeral=True)
                await bot._create_new_session(
                    interaction, available_repos[0], mode=new_thread_mode,
                    user_id=user_id, user_name=user_name,
                )
            else:
                view = discord.ui.View(timeout=60)
                for name in available_repos:
                    btn = discord.ui.Button(
                        label=name, style=discord.ButtonStyle.primary,
                        custom_id=f"new_repo:{name}",
                    )
                    view.add_item(btn)
                await interaction.response.send_message(
                    "Pick a repo:", view=view, ephemeral=True,
                )

    @bot.tree.command(name="history", description="Recent completed sessions", guild=guild_obj)
    @app_commands.describe(count="Number of entries (default 10, max 25)")
    async def cmd_history(interaction: discord.Interaction, count: int = 10):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await interaction.response.defer()
        count = max(1, min(count, 25))

        # Resolve repo context from the current thread/forum
        repo_name: str | None = None
        channel_id = str(interaction.channel_id)
        lookup = bot._forums.thread_to_project(channel_id)
        if lookup:
            repo_name = lookup[0].repo_name
        elif isinstance(interaction.channel, discord.Thread):
            parent = interaction.channel.parent
            if parent and isinstance(parent, discord.ForumChannel):
                proj = bot._forums.forum_by_channel_id(str(parent.id))
                if proj:
                    repo_name = proj.repo_name

        entries = history_mod.load_recent(repo=repo_name, limit=count, dedupe_thread=True)
        if not entries:
            label = f" for **{repo_name}**" if repo_name else ""
            await interaction.followup.send(f"No session history{label} yet.")
            return

        guild_id = bot._guild_id
        lines: list[str] = []
        for e in entries:
            status = e.get("status", "?")
            icon = "\u2705" if status == "completed" else "\u274c"
            eid = e.get("id", "?")
            topic = e.get("topic", "")[:50]
            thread_id = e.get("thread_id", "")

            # Relative age from finished timestamp
            age = ""
            finished = e.get("finished")
            if finished:
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(finished)
                    age = format_age(datetime.now(timezone.utc) - dt)
                except Exception:
                    pass

            # Thread link if available
            if thread_id:
                link = f"[{eid}](https://discord.com/channels/{guild_id}/{thread_id})"
            else:
                link = f"`{eid}`"

            line = f"{icon} {link} {topic}"
            if age:
                line += f"  *{age}*"
            lines.append(line)

        title = f"Recent Sessions"
        if repo_name:
            title += f" — {repo_name}"
        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="sync", description="Sync sessions from CLI", guild=guild_obj)
    @app_commands.describe(count="Number of sessions to sync (0 = this thread only)")
    async def cmd_sync(interaction: discord.Interaction, count: int = 0):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        thread_id = str(interaction.channel_id)
        if count == 0:
            lookup = bot._forums.thread_to_project(thread_id)
            if lookup:
                await bot._forums.sync_single_thread(thread_id, bot.messenger)
                await interaction.followup.send("Thread synced.", ephemeral=True)
                return
            count = 5
        log.info("Discord /sync count=%d by %s", count, interaction.user)
        created, populated = await bot._forums.sync_cli_sessions(count)
        parts = []
        if created:
            links = ", ".join(f"<#{t.id}>" for t in created)
            parts.append(f"Created {len(created)}: {links}")
        if populated:
            links = ", ".join(f"<#{ch.id}>" for ch in populated)
            parts.append(f"Updated {len(populated)}: {links}")
        if not parts:
            parts.append("No sessions found")
        await interaction.followup.send("\n".join(parts), ephemeral=True)

    @bot.tree.command(name="sync-channel", description="Refresh this thread's session history", guild=guild_obj)
    async def cmd_sync_channel(interaction: discord.Interaction):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        thread_id = str(interaction.channel_id)
        lookup = bot._forums.thread_to_project(thread_id)
        if not lookup:
            await interaction.response.send_message("This isn't a session thread.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        proj, info = lookup
        session_id = info.session_id
        if not session_id:
            await interaction.followup.send("No session mapped to this thread.", ephemeral=True)
            return
        repo_name = proj.repo_name
        repo_path = bot._store.list_repos().get(repo_name, "") if repo_name and repo_name != "_default" else ""
        if repo_path:
            latest = await asyncio.to_thread(sessions_mod.find_latest_session_for_repo, repo_path)
            if latest and latest["id"] != session_id:
                old_short = session_id[:12]
                session_id = latest["id"]
                info.session_id = session_id
                info.origin = "cli"
                info._synced_msg_count = 0
                bot._forums.save_forum_map()
                log.info("sync-channel updated thread %s session %s -> %s",
                         thread_id, old_short, session_id[:12])
        ch = interaction.channel
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            await bot._forums.populate_thread_history(ch, session_id, thread_id, messenger=bot.messenger)
        await interaction.followup.send(f"Synced session `{session_id[:12]}…`", ephemeral=True)

    @bot.tree.command(name="done", description="Wrap up — commit, changelog, release", guild=guild_obj)
    async def cmd_done(interaction: discord.Interaction):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return

        channel_id = str(interaction.channel_id)
        lookup = bot._forums.thread_to_project(channel_id)
        if not lookup:
            await interaction.response.send_message("Not in a session thread.", ephemeral=True)
            return

        _, info = lookup
        if not info.session_id:
            await interaction.response.send_message("No session found for this thread.", ephemeral=True)
            return

        # Find the most recent instance for this session
        source_inst = None
        for inst in bot._store.list_instances():
            if inst.session_id == info.session_id:
                source_inst = inst
                break

        if not source_inst:
            await interaction.response.send_message("No recent instance found.", ephemeral=True)
            return

        # Guard: refuse if the latest instance is still active
        if source_inst.status in (InstanceStatus.RUNNING, InstanceStatus.QUEUED):
            await interaction.response.send_message(
                "Instance is still running — `/kill` it first, then `/done`.",
                ephemeral=True,
            )
            return

        # Log when wrapping up from a non-success state
        if source_inst.status in (InstanceStatus.FAILED, InstanceStatus.KILLED):
            log.info("/done wrapping up from %s source instance %s",
                     source_inst.status.value, source_inst.display_id())

        await interaction.response.defer()

        # Mark thread as active (matches button handler in interactions.py)
        bot._cancel_sleep(channel_id)
        asyncio.create_task(bot._clear_thread_sleeping(interaction.channel))
        asyncio.create_task(bot._set_thread_active_tag(interaction.channel, True))
        asyncio.create_task(bot._refresh_dashboard())

        ar = bot._check_access(interaction.user.id, channel_id=channel_id)
        ctx = bot._ctx(channel_id, session_id=info.session_id, thread_info=info, access_result=ar)
        ctx.user_id = str(interaction.user.id)
        ctx.user_name = interaction.user.display_name
        bot._forums.attach_session_callbacks(ctx, info, channel_id)

        # Cancel any pending cooldown auto-retry for this instance
        if source_inst.cooldown_retry_at:
            source_inst.cooldown_retry_at = None
            source_inst.cooldown_channel_id = None
            bot._store.update_instance(source_inst)

        # Acquire channel lock to prevent concurrent spawns
        from bot.engine.commands import _get_channel_lock
        lock = _get_channel_lock(channel_id)
        async with lock:
            try:
                from bot.engine import workflows
                await workflows.on_done(ctx, source_inst.id)
            except Exception:
                log.exception("/done failed for instance %s", source_inst.display_id())
                try:
                    await ctx.messenger.send_text(
                        ctx.channel_id, "Done failed — try `/done` again or wrap up manually.",
                    )
                except Exception:
                    pass
            finally:
                bot._forums.persist_ctx_settings(ctx)
                await bot._forums.update_pending_thread(channel_id)
                asyncio.create_task(bot._try_apply_tags_after_run(channel_id))
                bot._schedule_sleep(channel_id)
                asyncio.create_task(bot._refresh_dashboard())

        try:
            await interaction.delete_original_response()
        except Exception:
            pass

    @bot.tree.command(name="help", description="Show available commands", guild=guild_obj)
    async def cmd_help(interaction: discord.Interaction):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, commands.on_help)

    @bot.tree.command(name="clear", description="Archive old instances", guild=guild_obj)
    async def cmd_clear(interaction: discord.Interaction):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, commands.on_clear)

    @bot.tree.command(name="logs", description="Bot log", guild=guild_obj)
    async def cmd_logs(interaction: discord.Interaction):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, commands.on_logs)

    @bot.tree.command(name="shutdown", description="Stop the bot", guild=guild_obj)
    async def cmd_shutdown(interaction: discord.Interaction):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, commands.on_shutdown)

    @bot.tree.command(name="reboot", description="Restart the bot (apply code changes)", guild=guild_obj)
    async def cmd_reboot(interaction: discord.Interaction):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, commands.on_reboot)

    # --- /ref: reference another thread's context ---

    async def thread_autocomplete(
        interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        choices: list[tuple[int, str, str]] = []
        current_tid = str(interaction.channel_id)
        for proj in bot._forums.forum_projects.values():
            for tid, info in proj.threads.items():
                if tid == current_tid or not info.session_id:
                    continue
                topic = info.topic or f"Thread #{tid[-6:]}"
                age = _snowflake_age(int(tid))
                label = f"[{proj.repo_name}] {topic}"[:85] + f" ({age})"
                if current.lower() in label.lower():
                    choices.append((int(tid), label[:100], tid))
        choices.sort(key=lambda x: x[0], reverse=True)
        return [app_commands.Choice(name=c[1], value=c[2]) for c in choices[:25]]

    @bot.tree.command(name="ref", description="Reference another thread's context", guild=guild_obj)
    @app_commands.describe(thread="Thread to reference", messages="Messages to include (default 6)")
    @app_commands.autocomplete(thread=thread_autocomplete)
    async def cmd_ref(interaction: discord.Interaction, thread: str, messages: int = 6):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        messages = max(1, min(messages, 20))
        lookup = bot._forums.thread_to_project(thread)
        if not lookup:
            await interaction.response.send_message("Thread not found.", ephemeral=True)
            return
        proj, info = lookup
        if not info.session_id:
            await interaction.response.send_message("No session in that thread.", ephemeral=True)
            return
        await interaction.response.defer()

        fpath = await asyncio.to_thread(sessions_mod.find_session_file, info.session_id)
        if not fpath:
            await interaction.followup.send("Session file not found.", ephemeral=True)
            return
        msgs = await asyncio.to_thread(sessions_mod.read_session_messages, fpath, messages)
        if not msgs:
            await interaction.followup.send("No messages in that session.", ephemeral=True)
            return

        embed = bot._forums.build_ref_embed(proj, info, msgs, thread)
        channel_id = str(interaction.channel_id)
        in_forum_thread = (
            isinstance(interaction.channel, discord.Thread)
            and isinstance(getattr(interaction.channel, "parent", None), discord.ForumChannel)
        )
        if in_forum_thread:
            context = bot._forums.build_ref_context(proj, info, msgs, thread)
            bot._pending_refs[channel_id] = (context, _time.time())
            bot._save_pending_refs()
            await interaction.followup.send(
                embed=embed,
                content="Context loaded \u2014 your next message will include this reference.",
            )
        else:
            await interaction.followup.send(embed=embed)

    @bot.tree.command(name="deferred", description="View/clear deferred review items", guild=guild_obj)
    @app_commands.describe(args="[repo_name] | clear [repo_name]")
    async def cmd_deferred(interaction: discord.Interaction, args: str = ""):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await bot._run_slash(interaction, lambda ctx: commands.on_deferred(ctx, args))

    # --- Monitor command group ---
    monitor_group = app_commands.Group(
        name="monitor", description="Live app monitoring dashboards", guild_ids=[bot._guild_id],
    )

    @monitor_group.command(name="setup", description="Enable a monitor (reads config from .env)")
    @app_commands.describe(
        name="Monitor name (e.g. aiagent)",
        repo="Repo to place monitor in (creates thread in repo forum)",
    )
    async def cmd_monitor_setup(interaction: discord.Interaction, name: str, repo: str | None = None):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await monitor_setup(bot, name.lower(), repo_name=repo)
        await interaction.followup.send(result, ephemeral=True)

    @monitor_group.command(name="refresh", description="Fetch & update all monitors now")
    async def cmd_monitor_refresh(interaction: discord.Interaction):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if not bot._monitor_service:
            await interaction.followup.send("Monitor service not initialized.", ephemeral=True)
            return
        count = await bot._monitor_service.refresh_all_now()
        await interaction.followup.send(f"Refreshed {count} monitor(s).", ephemeral=True)

    @monitor_group.command(name="remove", description="Disable a monitor (keeps channel)")
    @app_commands.describe(name="Monitor name")
    async def cmd_monitor_remove(interaction: discord.Interaction, name: str):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if not bot._monitor_service:
            await interaction.followup.send("Monitor service not initialized.", ephemeral=True)
            return
        ok = await bot._monitor_service.remove_monitor(name.lower())
        if ok:
            await interaction.followup.send(f"Monitor **{name}** disabled.", ephemeral=True)
        else:
            await interaction.followup.send(f"Monitor **{name}** not found.", ephemeral=True)

    @monitor_group.command(name="list", description="Show all monitors with status")
    async def cmd_monitor_list(interaction: discord.Interaction):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if not bot._monitor_service:
            await interaction.followup.send("Monitor service not initialized.", ephemeral=True)
            return
        monitors = bot._monitor_service.list_monitors()
        if not monitors:
            await interaction.followup.send("No monitors configured.", ephemeral=True)
            return
        lines = []
        for m in monitors:
            status = "on" if m["enabled"] else "off"
            attn = m.get("last_attention_level", "?")
            ch = f"<#{m['channel_id']}>" if m.get("channel_id") else "no channel"
            last = m.get("last_fetch_at", "never")
            if last and last != "never":
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(last)
                    last = dt.strftime("%b %d, %H:%M UTC")
                except Exception:
                    pass
            fails = m.get("consecutive_failures", 0)
            fail_str = f" ({fails} failures)" if fails else ""
            lines.append(f"{status} **{m['name']}** — {attn} — {ch} — {last}{fail_str}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    bot.tree.add_command(monitor_group)

    # --- /access command group (owner-only) ---
    access_group = app_commands.Group(
        name="access", description="Manage user access to repos",
        guild_ids=[bot._guild_id],
    )

    @access_group.command(name="grant", description="Grant user access to a repo")
    @app_commands.describe(user="User to grant", repo="Repo name", mode="Mode ceiling (default: explore)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Explore (read-only)", value="explore"),
        app_commands.Choice(name="Plan (read-only + plan)", value="plan"),
        app_commands.Choice(name="Build (full access)", value="build"),
    ])
    async def cmd_access_grant(
        interaction: discord.Interaction, user: discord.Member,
        repo: str, mode: str = "explore",
    ):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Owner only", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        repos = bot._store.list_repos()
        if repo not in repos:
            lower_map = {k.lower(): k for k in repos}
            repo = lower_map.get(repo.lower(), repo)
        if repo not in repos:
            await interaction.followup.send(f"Repo `{repo}` not found.", ephemeral=True)
            return
        cfg = load_access_config()
        uid = str(user.id)
        if uid not in cfg.users:
            cfg.users[uid] = access_mod.UserAccess(user_id=uid, display_name=user.display_name)
        ua = cfg.users[uid]
        ua.display_name = user.display_name
        ua.repos[repo] = access_mod.RepoAccess(mode=mode)
        repo_names = list(ua.repos.keys())
        forum = await bot._forums.ensure_user_forum(user.id, user.display_name, repo_names)
        if forum:
            ua.forum_channel_id = str(forum.id)
        access_mod.save_access_config(cfg)

        # Auto-follow: add user to repo's control room + archive threads
        proj = bot._forums.forum_projects.get(repo)
        if proj:
            for tid in (proj.control_thread_id, proj.archive_thread_id):
                if tid:
                    try:
                        ch = await bot.fetch_channel(int(tid))
                        if isinstance(ch, discord.Thread):
                            await ch.add_user(user)
                    except Exception:
                        log.debug("Auto-follow failed for user %s thread %s", user.id, tid)

        await interaction.followup.send(
            f"Granted **{user.display_name}** access to `{repo}` "
            f"(mode: {mode})"
            + (f" in <#{forum.id}>" if forum else ""),
            ephemeral=True,
        )

    @access_group.command(name="revoke", description="Revoke user access")
    @app_commands.describe(user="User to revoke", repo="Repo name (omit for all)")
    async def cmd_access_revoke(
        interaction: discord.Interaction, user: discord.Member, repo: str = "",
    ):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Owner only", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        cfg = load_access_config()
        uid = str(user.id)
        ua = cfg.users.get(uid)
        if not ua:
            await interaction.followup.send(
                f"**{user.display_name}** has no access grants.", ephemeral=True,
            )
            return
        if repo:
            ua.repos.pop(repo, None)
            msg = f"Revoked **{user.display_name}**'s access to `{repo}`."
        else:
            ua.repos.clear()
            msg = f"Revoked all access for **{user.display_name}**."
        if ua.forum_channel_id:
            if ua.repos:
                await bot._forums.sync_user_forum_tags(uid)
            else:
                guild = bot.get_guild(bot._guild_id)
                if guild:
                    forum = guild.get_channel(int(ua.forum_channel_id))
                    if forum and isinstance(forum, discord.ForumChannel):
                        try:
                            await forum.set_permissions(guild.get_member(user.id), overwrite=None)
                        except Exception:
                            pass
                msg += " Forum permissions removed."
        if not ua.repos and not ua.global_access:
            del cfg.users[uid]
        access_mod.save_access_config(cfg)
        await interaction.followup.send(msg, ephemeral=True)

    @access_group.command(name="list", description="Show all access grants")
    async def cmd_access_list(interaction: discord.Interaction):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Owner only", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        cfg = load_access_config()
        if not cfg.users:
            await interaction.followup.send("No access grants.", ephemeral=True)
            return
        lines = []
        for uid, ua in cfg.users.items():
            forum_link = f" <#{ua.forum_channel_id}>" if ua.forum_channel_id else ""
            if ua.global_access:
                lines.append(f"**{ua.display_name}** — all repos{forum_link}")
            else:
                for r, grant in ua.repos.items():
                    lines.append(
                        f"**{ua.display_name}** — `{r}` "
                        f"({grant.mode}, bash={grant.bash_policy}, "
                        f"limit={grant.max_daily_queries}/day){forum_link}"
                    )
        await interaction.followup.send("\n".join(lines) or "No grants.", ephemeral=True)

    @access_group.command(name="set", description="Change access settings")
    @app_commands.describe(
        user="User to modify", repo="Repo name",
        key="Setting to change", value="New value",
    )
    @app_commands.choices(key=[
        app_commands.Choice(name="mode", value="mode"),
        app_commands.Choice(name="bash", value="bash"),
        app_commands.Choice(name="daily_limit", value="daily_limit"),
    ])
    async def cmd_access_set(
        interaction: discord.Interaction, user: discord.Member,
        repo: str, key: str, value: str,
    ):
        if not bot._is_owner(interaction.user.id):
            await interaction.response.send_message("Owner only", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        cfg = load_access_config()
        uid = str(user.id)
        ua = cfg.users.get(uid)
        if not ua or repo not in ua.repos:
            await interaction.followup.send(
                f"**{user.display_name}** has no grant for `{repo}`.", ephemeral=True,
            )
            return
        grant = ua.repos[repo]
        if key == "mode":
            if value not in ("explore", "plan", "build"):
                await interaction.followup.send("Mode must be explore, plan, or build.", ephemeral=True)
                return
            grant.mode = value
        elif key == "bash":
            if value not in ("allowlist", "full", "none"):
                await interaction.followup.send("Bash must be allowlist, full, or none.", ephemeral=True)
                return
            grant.bash_policy = value
        elif key == "daily_limit":
            try:
                grant.max_daily_queries = int(value)
            except ValueError:
                await interaction.followup.send("daily_limit must be a number.", ephemeral=True)
                return
        access_mod.save_access_config(cfg)
        await interaction.followup.send(
            f"Updated **{user.display_name}** `{repo}`: {key}={value}", ephemeral=True,
        )

    bot.tree.add_command(access_group)

    # --- Diagnostics toggle ---
    @bot.tree.command(
        name="diagnostics",
        description="Toggle diagnostic scaffolding for this repo",
        guild=guild_obj,
    )
    @app_commands.describe(toggle="on, off, or status")
    @app_commands.choices(toggle=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
        app_commands.Choice(name="status", value="status"),
    ])
    async def cmd_diagnostics(interaction: discord.Interaction, toggle: str = "status"):
        if not bot._is_owner(interaction.user.id) and not bot._check_access(
            interaction.user.id, channel_id=str(interaction.channel_id)
        ).allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        # Resolve repo from current thread/forum
        repo_name: str | None = None
        channel_id = str(interaction.channel_id)
        lookup = bot._forums.thread_to_project(channel_id)
        if lookup:
            repo_name = lookup[0].repo_name
        elif isinstance(interaction.channel, discord.Thread):
            parent = interaction.channel.parent
            if parent and isinstance(parent, discord.ForumChannel):
                proj = bot._forums.forum_by_channel_id(str(parent.id))
                if proj:
                    repo_name = proj.repo_name

        if not repo_name:
            await interaction.followup.send("No repo linked to this channel.", ephemeral=True)
            return

        repo_path = bot._store.list_repos().get(repo_name, "")
        if not repo_path:
            await interaction.followup.send(f"Repo **{repo_name}** not found.", ephemeral=True)
            return

        test_json = _Path(repo_path) / ".claude" / "test.json"

        # Load existing config
        cfg: dict = {}
        if test_json.exists():
            try:
                raw = _json.loads(test_json.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    cfg = raw
            except Exception:
                pass

        if toggle == "status":
            enabled = cfg.get("diagnostics", True)
            status = "**on**" if enabled else "**off**"
            await interaction.followup.send(
                f"Diagnostics: {status} for **{repo_name}**", ephemeral=True,
            )
            return

        # Update
        cfg["diagnostics"] = (toggle == "on")
        test_json.parent.mkdir(parents=True, exist_ok=True)
        test_json.write_text(_json.dumps(cfg, indent=2), encoding="utf-8")

        msg = (
            "build steps will scaffold diagnostic endpoints"
            if toggle == "on"
            else "build steps skip endpoint scaffolding"
        )
        await interaction.followup.send(
            f"Diagnostics **{toggle}** for **{repo_name}** — {msg}", ephemeral=True,
        )
