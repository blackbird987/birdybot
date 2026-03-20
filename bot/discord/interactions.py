"""Button, select menu, and modal interaction dispatch for Discord."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from bot.discord import access as access_mod
from bot.discord import channels
from bot.discord.access import AccessResult, load_access_config, effective_mode as access_effective_mode
from bot.discord.modals import QuickTaskModal
from bot.engine import commands
from bot.engine import sessions as sessions_mod
from bot.platform.formatting import MODE_COLOR, VALID_MODES, mode_name

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

# Button callback actions that trigger long-running LLM queries
_QUERY_ACTIONS: frozenset[str] = frozenset({
    "retry", "plan", "build", "review_plan", "apply_revisions",
    "review_code", "commit", "done", "autopilot", "build_and_ship",
    "continue_autopilot",
})


async def handle(bot: ClaudeBot, interaction: discord.Interaction) -> None:
    """Handle button/select/modal interactions (persistent views)."""
    btn_access = bot._check_access(
        interaction.user.id, channel_id=str(interaction.channel_id),
    )
    if not btn_access.allowed:
        await interaction.response.send_message("Unauthorized", ephemeral=True)
        return

    custom_id = interaction.data.get("custom_id", "") if interaction.data else ""

    # --- Select menu: repo switch (owner only) ---
    if custom_id == "repo_switch_select":
        await _handle_repo_switch(bot, interaction, btn_access)
        return

    parts = custom_id.split(":", 1)
    if len(parts) != 2:
        return

    action, instance_id = parts
    log.info("Discord button %s:%s in #%s", action, instance_id[:12], getattr(interaction.channel, "name", "?"))

    # --- Control room mode toggle (repo-scoped default) ---
    if action == "control_mode":
        await _handle_control_mode(bot, interaction, instance_id, btn_access)
        return

    # --- User control room mode toggle ---
    if action == "user_control_mode":
        await _handle_user_control_mode(bot, interaction, instance_id, btn_access)
        return

    # --- Quick Task modal (must send modal as initial response, NOT defer) ---
    if action == "quick_task":
        if not btn_access.is_owner:
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return
        modal = QuickTaskModal(bot, instance_id, btn_access)
        await interaction.response.send_modal(modal)
        return

    # --- Mode selection in new thread welcome embed ---
    if action == "mode_set" and instance_id in VALID_MODES:
        await _handle_mode_set(bot, interaction, instance_id, btn_access)
        return

    await interaction.response.defer()

    # --- Load CLI history into thread ---
    if action == "load_history":
        await _handle_load_history(bot, interaction, instance_id)
        return

    # --- New session with repo picker ---
    if action == "new_repo":
        await _handle_new_repo(bot, interaction, instance_id, btn_access)
        return

    # --- Sync CLI for a repo (control room button) ---
    if action == "sync_repo":
        if not btn_access.is_owner:
            await interaction.followup.send("Owner only.", ephemeral=True)
            return
        created, populated = await bot._forums.sync_cli_sessions(3)
        parts_msg = []
        if created:
            parts_msg.append(f"Created {len(created)} threads")
        if populated:
            parts_msg.append(f"Updated {len(populated)} threads")
        if not parts_msg:
            parts_msg.append("No sessions found")
        await interaction.followup.send(
            f"Synced `{instance_id}`: " + ", ".join(parts_msg), ephemeral=True,
        )
        return

    # --- Resume latest session for a repo (control room button) ---
    if action == "resume_latest":
        await _handle_resume_latest(bot, interaction, instance_id)
        return

    # --- Reboot/Deploy from control room ---
    if action == "reboot_repo":
        if not btn_access.is_owner:
            await interaction.followup.send("Owner only.", ephemeral=True)
            return
        await _handle_reboot_repo(bot, interaction, instance_id)
        return

    # --- Approve file-sourced deploy config ---
    if action == "approve_deploy":
        if not btn_access.is_owner:
            await interaction.followup.send("Owner only.", ephemeral=True)
            return
        await _handle_approve_deploy(bot, interaction, instance_id)
        return

    # --- Refresh control room (repo or user) ---
    if action == "refresh_control":
        await bot._forums.refresh_control_room(instance_id)
        await interaction.followup.send("Refreshed.", ephemeral=True)
        return

    if action == "refresh_user_control":
        user_id = str(interaction.user.id)
        await bot._forums.refresh_user_control_room(user_id)
        await interaction.followup.send("Refreshed.", ephemeral=True)
        return

    # --- Cancel cooldown auto-retry (lightweight, no channel lock needed) ---
    if action == "cancel_cooldown":
        inst = bot._store.get_instance(instance_id)
        if inst and inst.cooldown_retry_at:
            inst.cooldown_retry_at = None
            inst.cooldown_channel_id = None
            bot._store.update_instance(inst)
            # Edit the cooldown message in-place, removing the button
            try:
                await interaction.message.edit(
                    content="Auto-retry cancelled.", view=None,
                )
            except Exception:
                await interaction.followup.send("Auto-retry cancelled.")
        else:
            await interaction.followup.send("No pending auto-retry.", ephemeral=True)
        return

    # --- Stop all running instances for a repo (owner-only) ---
    if action == "stop_all":
        await _handle_stop_all(bot, interaction, instance_id, btn_access)
        return

    # --- Session resume: create/find thread, redirect there ---
    if action == "sess_resume":
        await _handle_sess_resume(bot, interaction, instance_id, btn_access)
        return

    # --- New session button: create a new forum thread (like /new) ---
    if action == "new":
        await _handle_new_session(bot, interaction, btn_access)
        return

    # --- Generic query button dispatch (plan, build, review, etc.) ---
    channel_id = str(interaction.channel_id)
    source_msg_id = str(interaction.message.id) if interaction.message else None

    is_query = action in _QUERY_ACTIONS
    if is_query:
        bot._cancel_sleep(channel_id)
        asyncio.create_task(bot._clear_thread_sleeping(interaction.channel))
        asyncio.create_task(bot._set_thread_active_tag(interaction.channel, True))
        asyncio.create_task(bot._refresh_dashboard())

    lookup = bot._forums.thread_to_project(channel_id)
    t_info = lookup[1] if lookup else None
    ctx = bot._ctx(channel_id, thread_info=t_info, access_result=btn_access)
    ctx.user_id = str(interaction.user.id)
    ctx.user_name = interaction.user.display_name

    # Acquire channel lock for query actions to prevent concurrent spawns
    # (matches the serialization in _run_query for text messages)
    if is_query:
        from bot.engine.commands import _get_channel_lock
        lock = _get_channel_lock(channel_id)
        queued_msg_id = None
        if lock.locked():
            queued_msg_id = await ctx.messenger.send_text(
                ctx.channel_id,
                "📋 Queued — waiting for current query to finish.",
                silent=True,
            )
        async with lock:
            if queued_msg_id:
                try:
                    await ctx.messenger.delete_message(ctx.channel_id, queued_msg_id)
                except Exception:
                    pass
            try:
                await commands.handle_callback(ctx, action, instance_id, source_msg_id)
            finally:
                bot._forums.persist_ctx_settings(ctx)
                asyncio.create_task(bot._try_apply_tags_after_run(channel_id))
                bot._schedule_sleep(channel_id)
                asyncio.create_task(bot._refresh_dashboard())
    else:
        try:
            await commands.handle_callback(ctx, action, instance_id, source_msg_id)
        finally:
            bot._forums.persist_ctx_settings(ctx)
            if action.startswith("mode_"):
                asyncio.create_task(bot._refresh_dashboard())


# --- Individual handlers ---


async def _handle_repo_switch(
    bot: ClaudeBot, interaction: discord.Interaction, btn_access: AccessResult,
) -> None:
    if not btn_access.is_owner:
        await interaction.response.send_message("Owner only", ephemeral=True)
        return
    values = interaction.data.get("values", []) if interaction.data else []
    if values:
        repo_name = values[0]
        current, _ = bot._store.get_active_repo()
        if repo_name == current:
            await interaction.response.edit_message(
                content=f"**{repo_name}** is already active.", view=None,
            )
        elif bot._store.switch_repo(repo_name):
            _, path = bot._store.get_active_repo()
            await interaction.response.edit_message(
                content=f"Switched to **{repo_name}**: `{path}`", view=None,
            )
        else:
            await interaction.response.edit_message(
                content=f"Repo '{repo_name}' not found.", view=None,
            )


async def _handle_control_mode(
    bot: ClaudeBot, interaction: discord.Interaction,
    instance_id: str, btn_access: AccessResult,
) -> None:
    cr_parts = instance_id.split(":", 1)
    if len(cr_parts) != 2 or cr_parts[1] not in VALID_MODES:
        return
    cr_repo, target_mode = cr_parts
    if not btn_access.is_owner and btn_access.mode_ceiling:
        target_mode = access_effective_mode(
            access_mod.RepoAccess(mode=btn_access.mode_ceiling), target_mode,
        )
    if btn_access.is_owner:
        bot._store.mode = target_mode
    else:
        cfg = load_access_config()
        ua = cfg.users.get(str(interaction.user.id))
        if ua and cr_repo in ua.repos:
            ua.repos[cr_repo].mode = target_mode
            access_mod.save_access_config(cfg)
    if interaction.message and interaction.message.embeds:
        embed = interaction.message.embeds[0]
        for i, field_obj in enumerate(embed.fields):
            if field_obj.name == "Mode":
                embed.set_field_at(i, name="Mode", value=mode_name(target_mode), inline=True)
                break
        from bot.claude.types import InstanceStatus
        instances = bot._store.list_instances()
        active = sum(1 for inst in instances if inst.repo_name == cr_repo
                     and inst.status == InstanceStatus.RUNNING)
        ds = bot._store.get_deploy_state(cr_repo)
        view = channels.build_control_view(cr_repo, current_mode=target_mode, active_count=active, deploy_state=ds)
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.defer()
    log.info("Control room mode set to %s for repo %s", target_mode, cr_repo)


async def _handle_user_control_mode(
    bot: ClaudeBot, interaction: discord.Interaction,
    instance_id: str, btn_access: AccessResult,
) -> None:
    cr_parts = instance_id.split(":", 1)
    if len(cr_parts) != 2 or cr_parts[1] not in VALID_MODES:
        return
    cr_repo, target_mode = cr_parts
    if not btn_access.is_owner and btn_access.mode_ceiling:
        target_mode = access_effective_mode(
            access_mod.RepoAccess(mode=btn_access.mode_ceiling), target_mode,
        )
    target_user_id = str(interaction.user.id)
    if btn_access.is_owner:
        uf = bot._resolve_user_forum_context(interaction)
        if uf:
            target_user_id = uf[0]
    cfg = load_access_config()
    ua = cfg.users.get(target_user_id)
    if ua and cr_repo in ua.repos:
        ua.repos[cr_repo].mode = target_mode
        access_mod.save_access_config(cfg)
    elif btn_access.is_owner:
        bot._store.mode = target_mode
    if interaction.message and interaction.message.embeds:
        embed = interaction.message.embeds[0]
        for i, field_obj in enumerate(embed.fields):
            if field_obj.name == "Mode":
                embed.set_field_at(i, name="Mode", value=mode_name(target_mode), inline=True)
                break
        repo_names = list(ua.repos.keys()) if ua else [cr_repo]
        view = channels.build_user_control_view(repo_names, current_mode=target_mode)
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.defer()
    log.info("User control room mode set to %s for %s", target_mode, cr_repo)


async def _handle_mode_set(
    bot: ClaudeBot, interaction: discord.Interaction,
    target_mode: str, btn_access: AccessResult,
) -> None:
    if not btn_access.is_owner and btn_access.mode_ceiling:
        target_mode = access_effective_mode(
            access_mod.RepoAccess(mode=btn_access.mode_ceiling), target_mode,
        )
    thread_id = str(interaction.channel_id)
    lookup = bot._forums.thread_to_project(thread_id)
    if lookup:
        lookup[1].mode = target_mode
        bot._forums.save_forum_map()
    elif btn_access.is_owner:
        bot._store.mode = target_mode
    if interaction.message and interaction.message.embeds:
        embed = interaction.message.embeds[0]
        embed.color = discord.Color(MODE_COLOR.get(target_mode, 0x5865F2))
        for i, field_obj in enumerate(embed.fields):
            if field_obj.name == "Mode":
                embed.set_field_at(i, name="Mode", value=mode_name(target_mode), inline=True)
                break
        view = channels.mode_select_view(target_mode)
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.defer()
    log.info("Mode set to %s via welcome button", target_mode)


async def _handle_load_history(
    bot: ClaudeBot, interaction: discord.Interaction, cli_session_id: str,
) -> None:
    thread_id = str(interaction.channel_id)
    lookup = bot._forums.thread_to_project(thread_id)
    ch = interaction.channel
    if lookup and isinstance(ch, (discord.TextChannel, discord.Thread)):
        proj, info = lookup
        info.session_id = cli_session_id
        info.origin = "cli"
        bot._forums.save_forum_map()
        await bot._forums.populate_thread_history(
            ch, cli_session_id, thread_id,
            force=True, cli_label=True, messenger=bot.messenger,
        )
        log.info("Loaded CLI history %s into thread %s", cli_session_id[:12],
                 getattr(ch, "name", thread_id))
    try:
        await interaction.message.delete()
    except Exception:
        pass
    try:
        await interaction.delete_original_response()
    except Exception:
        pass


async def _handle_new_repo(
    bot: ClaudeBot, interaction: discord.Interaction,
    repo_name: str, btn_access: AccessResult,
) -> None:
    user_id = None
    user_name = None
    if not btn_access.is_owner:
        user_id = str(interaction.user.id)
        user_name = interaction.user.display_name
    else:
        uf = bot._resolve_user_forum_context(interaction)
        if uf:
            user_id, user_name = uf[0], uf[1]
    await bot._create_new_session(
        interaction, repo_name, redirect=True,
        user_id=user_id, user_name=user_name,
    )
    asyncio.create_task(bot._forums.refresh_control_room(repo_name))
    if user_id:
        asyncio.create_task(bot._forums.refresh_user_control_room(user_id))


async def _handle_resume_latest(
    bot: ClaudeBot, interaction: discord.Interaction, repo_name: str,
) -> None:
    proj = bot._forums.forum_projects.get(repo_name)
    if not proj or not proj.threads:
        await interaction.followup.send(
            "No sessions yet — start one with **New Session**.", ephemeral=True,
        )
        return
    latest_tid = max(
        (tid for tid, info in proj.threads.items()
         if info.session_id and tid != (proj.control_thread_id or "")),
        key=lambda t: int(t),
        default=None,
    )
    if not latest_tid:
        await interaction.followup.send(
            "No sessions yet — start one with **New Session**.", ephemeral=True,
        )
        return
    info = proj.threads[latest_tid]
    topic = info.topic or "session"
    await interaction.followup.send(
        f"Resuming: <#{latest_tid}> — {topic[:60]}", ephemeral=True,
    )
    try:
        thread = await bot.fetch_channel(int(latest_tid))
        if isinstance(thread, discord.Thread) and thread.archived:
            await thread.edit(archived=False)
    except Exception:
        pass


async def _handle_stop_all(
    bot: ClaudeBot, interaction: discord.Interaction,
    repo_name: str, btn_access: AccessResult,
) -> None:
    if not btn_access.is_owner:
        await interaction.followup.send("Owner only.", ephemeral=True)
        return
    from bot.claude.types import InstanceStatus
    instances = bot._store.list_instances()
    running = [i for i in instances if i.repo_name == repo_name
               and i.status == InstanceStatus.RUNNING]
    if not running:
        await interaction.followup.send("No running instances.", ephemeral=True)
        return
    count = len(running)
    await interaction.followup.send(
        f"Stopping {count} instance{'s' if count != 1 else ''}...", ephemeral=True,
    )
    killed = 0
    for inst in running:
        try:
            if await bot._runner.kill(inst.id):
                inst.status = InstanceStatus.KILLED
                inst.finished_at = datetime.now(timezone.utc).isoformat()
                bot._store.update_instance(inst)
                killed += 1
        except Exception:
            log.debug("Failed to kill %s during stop_all", inst.id, exc_info=True)
    asyncio.create_task(bot._forums.refresh_control_room(repo_name))
    asyncio.create_task(bot._refresh_dashboard())
    log.info("Stop all: killed %d/%d instances for %s", killed, count, repo_name)


async def _handle_sess_resume(
    bot: ClaudeBot, interaction: discord.Interaction,
    session_id: str, btn_access: AccessResult,
) -> None:
    from bot.engine import workflows

    topic = "session"
    repo_name = None
    session_list = await asyncio.to_thread(
        sessions_mod.scan_sessions, 10, bot._store.list_repos(),
    )
    for s in session_list:
        if s["id"] == session_id:
            topic = s["topic"]
            repo_name = s.get("project")
            break

    repo_name = repo_name or "_default"
    thread = await bot._forums.get_or_create_session_thread(
        repo_name, session_id, topic,
    )
    if thread:
        try:
            await interaction.delete_original_response()
        except Exception:
            pass
        asyncio.create_task(bot._send_redirect(thread))
        lookup_info = bot._forums.thread_to_project(str(thread.id))
        ti = lookup_info[1] if lookup_info else None
        ctx = bot._ctx(
            str(thread.id), session_id=session_id,
            repo_name=repo_name if repo_name != "_default" else None,
            thread_info=ti,
            access_result=btn_access,
        )
        ctx.user_id = str(interaction.user.id)
        ctx.user_name = interaction.user.display_name
        source_msg_id = str(interaction.message.id) if interaction.message else None
        await workflows.on_sess_resume(ctx, session_id, source_msg_id)


async def _handle_new_session(
    bot: ClaudeBot, interaction: discord.Interaction, btn_access: AccessResult,
) -> None:
    thread_id = str(interaction.channel_id)
    lookup = bot._forums.thread_to_project(thread_id)
    repo_name = lookup[0].repo_name if lookup else None
    user_id = None
    user_name = None
    if not btn_access.is_owner:
        user_id = str(interaction.user.id)
        user_name = interaction.user.display_name
        if not repo_name:
            cfg = load_access_config()
            ua = cfg.users.get(user_id)
            if ua and ua.repos:
                granted = [r for r in ua.repos if r in bot._forums.forum_projects]
                if granted:
                    repo_name = granted[0]
    else:
        uf = bot._resolve_user_forum_context(interaction)
        if uf:
            user_id, user_name = uf[0], uf[1]
            if not repo_name:
                repo_name = uf[2]
    if not repo_name:
        repo_name, _ = bot._store.get_active_repo()
    await bot._create_new_session(
        interaction, repo_name,
        user_id=user_id, user_name=user_name,
    )
    try:
        await interaction.delete_original_response()
    except Exception:
        pass


async def _handle_approve_deploy(
    bot: ClaudeBot, interaction: discord.Interaction, repo_name: str,
) -> None:
    """Approve a file-sourced deploy config so the Reboot button becomes active."""
    config = bot._store.get_deploy_config(repo_name)
    if not config:
        await interaction.followup.send("No deploy config found.", ephemeral=True)
        return
    config["approved"] = True
    bot._store.set_deploy_config(repo_name, config)
    await interaction.followup.send(
        f"Deploy approved for **{repo_name}**: `{config.get('command', 'self')}`",
    )
    asyncio.create_task(bot._forums.refresh_control_room(repo_name))


async def _handle_reboot_repo(
    bot: ClaudeBot, interaction: discord.Interaction, repo_name: str,
) -> None:
    """Execute a reboot/deploy for a repo based on its deploy config."""
    config = bot._store.get_deploy_config(repo_name)
    if not config or not config.get("approved"):
        await interaction.followup.send(
            "Deploy not configured or not approved.", ephemeral=True,
        )
        return

    ds = bot._store.get_deploy_state(repo_name)
    method = config.get("method", "command")

    if method == "self":
        # Guard against duplicate button clicks while already draining
        if bot._runner.is_draining:
            await interaction.followup.send(
                "Reboot already in progress.", ephemeral=True,
            )
            return

        msg = f"Reboot requested from control room ({repo_name})"
        if ds and ds.pending_changes:
            msg += f" ({len(ds.pending_changes)} pending changes)"

        # Drain active tasks first (same as /reboot slash command)
        if bot._runner.is_busy:
            ids = ", ".join(bot._runner.active_ids) or "(between steps)"
            await interaction.followup.send(
                f"⏳ Waiting for active work to finish: {ids}",
            )
            idle = await bot._runner.wait_until_idle(timeout=300)
            if not idle:
                remaining = ", ".join(bot._runner.active_ids)
                await interaction.followup.send(
                    f"⚠️ Timed out. Force-rebooting with "
                    f"{bot._runner.active_count} still running: {remaining}",
                )

        bot._runner.request_reboot({
            "message": msg,
            "channel_id": str(interaction.channel_id),
            "platform": "discord",
        })
        await interaction.followup.send("\U0001f504 Rebooting...")
        # NOTE: Do NOT reset deploy state here — capture_boot_baselines()
        # handles it on the next startup when it detects self_managed=True.

    elif method == "command":
        command = config["command"]
        # Resolve cwd: absolute as-is, relative against repo root, default to repo path
        repo_path = bot._store.list_repos().get(repo_name)
        if not repo_path:
            await interaction.followup.send(
                f"Repo `{repo_name}` not found — deploy config may be stale.",
                ephemeral=True,
            )
            return
        raw_cwd = config.get("cwd")
        if raw_cwd:
            cwd_path = Path(raw_cwd)
            if not cwd_path.is_absolute():
                cwd_path = Path(repo_path) / cwd_path
            cwd = str(cwd_path.resolve())
        else:
            cwd = repo_path

        await interaction.followup.send(f"Running: `{command}`...")

        proc = None
        try:
            proc = await asyncio.create_subprocess_shell(
                command, cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            output = stdout.decode(errors="replace")[:1500]

            if proc.returncode == 0:
                # Reset deploy state after successful command-based deploy
                if ds:
                    ds.boot_version = ds.current_version
                    ds.boot_ref = ds.current_ref
                    ds.pending_sessions.clear()
                    ds.pending_changes.clear()
                    bot._store.set_deploy_state(repo_name, ds)
                await interaction.followup.send(
                    f"\u2705 Deploy successful.\n```\n{output}\n```"
                    if output.strip() else "\u2705 Deploy successful.",
                )
            else:
                await interaction.followup.send(
                    f"\u274c Deploy failed (exit {proc.returncode}).\n```\n{output}\n```"
                    if output.strip()
                    else f"\u274c Deploy failed (exit {proc.returncode}).",
                )
        except asyncio.TimeoutError:
            if proc:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await interaction.followup.send("\u274c Deploy timed out (60s).")
        except Exception as e:
            await interaction.followup.send(f"\u274c Deploy error: {e}")

    asyncio.create_task(bot._forums.refresh_control_room(repo_name))
    asyncio.create_task(bot._refresh_dashboard())
