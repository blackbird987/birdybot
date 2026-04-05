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
from bot.platform.formatting import MODE_COLOR, VALID_EFFORTS, VALID_MODES, effort_name, mode_name

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

# Button callback actions that trigger long-running LLM queries
_QUERY_ACTIONS: frozenset[str] = frozenset({
    "retry", "plan", "build", "review_plan", "apply_revisions",
    "review_code", "commit", "done", "autopilot", "build_and_ship",
    "continue_autopilot", "continue_ppu",
})

# --- Deploy status message management (keeps control rooms clean) ---

_deploy_status_msgs: dict[str, int] = {}  # repo_name → message_id


class DeployStatus:
    """Manages a single editable deploy status message in a control room.

    Used only for command-based deploys (not self-managed reboots).
    Uses the bot token (channel.fetch_message) instead of the interaction
    webhook token for edits, avoiding the 15-minute expiry on long deploys.
    """

    def __init__(self, channel: discord.abc.Messageable, msg: discord.Message,
                 repo_name: str, store: object | None = None):
        self._channel = channel
        self._msg = msg
        self._repo_name = repo_name
        self._store = store
        self.id = msg.id

    async def update(self, content: str) -> None:
        """Edit the status message. Cached object first, fetch fallback."""
        # Truncate to safe limit (leave room for formatting overhead)
        if len(content) > 1900:
            lines = content[:1850].rsplit("\n", 1)[0]
            if "```" in content and not lines.endswith("```"):
                lines += "\n```"
            content = lines
        try:
            await self._msg.edit(content=content)
        except (discord.NotFound, discord.HTTPException):
            try:
                self._msg = await self._channel.fetch_message(self.id)
                await self._msg.edit(content=content)
            except (discord.NotFound, discord.HTTPException):
                # Message gone — send new one as fallback
                self._msg = await self._channel.send(content)
                self.id = self._msg.id
                _deploy_status_msgs[self._repo_name] = self.id

    async def delete(self) -> None:
        """Delete the status message. Persist ID to state on failure for startup cleanup."""
        _deploy_status_msgs.pop(self._repo_name, None)
        try:
            await self._msg.delete()
        except discord.NotFound:
            pass  # Already gone
        except Exception:
            # Rate limit, network error, etc. — try fetch fallback
            try:
                msg = await self._channel.fetch_message(self.id)
                await msg.delete()
            except discord.NotFound:
                pass  # Already gone
            except Exception:
                self._persist_for_cleanup()

    def _persist_for_cleanup(self) -> None:
        """Persist message ID to state.json for startup cleanup."""
        if not self._store:
            return
        try:
            state = self._store.get_platform_state("discord")
            pending = state.get("deploy_status_msgs", {})
            pending[self._repo_name] = self.id
            state["deploy_status_msgs"] = pending
            self._store.set_platform_state("discord", state, persist=True)
            log.warning("Persisted orphaned deploy status msg %s for cleanup", self.id)
        except Exception:
            log.debug("Failed to persist deploy status msg for cleanup", exc_info=True)


async def _start_deploy_status(
    interaction: discord.Interaction, repo_name: str, initial_text: str,
    store: object | None = None,
) -> DeployStatus:
    """Send initial deploy status message, cleaning up the previous one.

    Used only for command-based deploys (self-managed reboots use the embed).
    """
    # Delete previous status message for this repo
    old_msg_id = _deploy_status_msgs.pop(repo_name, None)
    if old_msg_id:
        try:
            old_msg = await interaction.channel.fetch_message(old_msg_id)
            await old_msg.delete()
        except (discord.NotFound, discord.HTTPException):
            pass  # Already gone — dict entry already cleared by .pop()

    # Send initial message via interaction (one-time use of webhook token)
    msg = await interaction.followup.send(initial_text, wait=True)
    _deploy_status_msgs[repo_name] = msg.id
    return DeployStatus(interaction.channel, msg, repo_name, store=store)


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

    # --- Ark dashboard buttons (ark:new_repo, ark:refresh, ark:stop_all) ---
    if custom_id.startswith("ark:"):
        from bot.discord.wizard import handle_ark_button
        await handle_ark_button(bot, interaction, custom_id)
        return

    # --- Repo setup wizard buttons (wizard:add:*, wizard:create:*) ---
    if custom_id.startswith("wizard:"):
        from bot.discord.wizard import handle_wizard_button
        await handle_wizard_button(bot, interaction, custom_id)
        return

    parts = custom_id.split(":", 1)
    if len(parts) != 2:
        return

    action, instance_id = parts
    log.info("Discord button %s:%s in #%s", action, instance_id[:12], getattr(interaction.channel, "name", "?"))

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

    # --- Effort selection in new thread welcome embed ---
    if action == "effort_set" and instance_id in VALID_EFFORTS:
        await _handle_effort_set(bot, interaction, instance_id, btn_access)
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

    # --- Sync Git for a repo (bidirectional: pull + push) ---
    if action == "sync_git":
        if not btn_access.is_owner:
            await interaction.followup.send("Owner only.", ephemeral=True)
            return
        await _handle_sync_git(bot, interaction, instance_id)
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
    ctx = bot._ctx(channel_id, session_id=t_info.session_id if t_info else None,
                    thread_info=t_info, access_result=btn_access)
    ctx.user_id = str(interaction.user.id)
    ctx.user_name = interaction.user.display_name
    if t_info:
        bot._forums.attach_session_callbacks(ctx, t_info, channel_id)

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
                await bot._forums.update_pending_thread(channel_id)
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
        # Read current effort from embed (default "high" for old embeds)
        current_effort = "high"
        for field_obj in embed.fields:
            if field_obj.name == "Effort":
                current_effort = field_obj.value.lower()
                break
        view = channels.session_controls_view(target_mode, current_effort)
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.defer()
    log.info("Mode set to %s via welcome button", target_mode)


async def _handle_effort_set(
    bot: ClaudeBot, interaction: discord.Interaction,
    target_effort: str, btn_access: AccessResult,
) -> None:
    thread_id = str(interaction.channel_id)
    lookup = bot._forums.thread_to_project(thread_id)
    if lookup:
        lookup[1].effort = target_effort
        bot._forums.save_forum_map()
    if interaction.message and interaction.message.embeds:
        embed = interaction.message.embeds[0]
        # Update or add Effort field
        found = False
        for i, field_obj in enumerate(embed.fields):
            if field_obj.name == "Effort":
                embed.set_field_at(i, name="Effort", value=effort_name(target_effort), inline=True)
                found = True
                break
        if not found:
            embed.add_field(name="Effort", value=effort_name(target_effort), inline=True)
        # Read current mode from embed to rebuild view
        current_mode = "explore"
        for field_obj in embed.fields:
            if field_obj.name == "Mode":
                for m in ("explore", "plan", "build"):
                    if mode_name(m) == field_obj.value:
                        current_mode = m
                        break
                break
        view = channels.session_controls_view(current_mode, target_effort)
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.defer()
    log.info("Effort set to %s via welcome button", target_effort)

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
        ephemeral=True,
    )
    asyncio.create_task(bot._forums.refresh_control_room(repo_name))


async def execute_deploy(
    bot: ClaudeBot,
    repo_name: str,
    deploy_config: dict,
    *,
    status_callback=None,
) -> tuple[bool, str, str]:
    """Run a command-based deploy for a repo.

    Decoupled from discord.Interaction so it can be called from both
    button handlers and programmatic triggers (auto-fix redeploy).

    Returns (success, output, error_summary).
    """
    command = deploy_config["command"]
    repo_path = bot._store.list_repos().get(repo_name)
    if not repo_path:
        return False, "", f"Repo `{repo_name}` not found"

    raw_cwd = deploy_config.get("cwd")
    if raw_cwd:
        cwd_path = Path(raw_cwd)
        if not cwd_path.is_absolute():
            cwd_path = Path(repo_path) / cwd_path
        cwd = str(cwd_path.resolve())
    else:
        cwd = repo_path

    async def _status(msg: str) -> None:
        if status_callback:
            await status_callback(msg)

    # Push to origin before deploying (safety net)
    await _status("\U0001f680 Pushing to origin...")
    try:
        push_proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "push", "origin", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        push_out, _ = await asyncio.wait_for(
            push_proc.communicate(), timeout=30,
        )
        if push_proc.returncode != 0:
            push_output = push_out.decode(errors="replace")[:1500]
            await _status(
                f"⚠️ Push failed (exit {push_proc.returncode}), deploying anyway...\n```\n{push_output}\n```"
            )
    except asyncio.TimeoutError:
        try:
            push_proc.kill()
        except ProcessLookupError:
            pass
        await _status("⚠️ Push timed out (30s). Proceeding with deploy...")
    except Exception as e:
        await _status(f"⚠️ Push error: {e}. Proceeding with deploy...")

    await _status(f"\U0001f680 Running: `{command}`...")

    ds = bot._store.get_deploy_state(repo_name)
    proc = None
    try:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        raw_timeout = deploy_config.get("timeout", 600)
        deploy_timeout = max(10, min(int(raw_timeout), 3600)) if isinstance(raw_timeout, (int, float)) else 600
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=deploy_timeout)
        output = stdout.decode(errors="replace")[:1500]

        if proc.returncode == 0:
            if ds:
                ds.last_deploy_error = None
                ds.boot_version = ds.current_version
                ds.boot_ref = ds.current_ref
                ds.pending_sessions.clear()
                ds.pending_changes.clear()
                ds.auto_fix_attempt = 0
                ds.auto_fix_thread_id = None
                bot._store.set_deploy_state(repo_name, ds)
            return True, output, ""
        else:
            err_summary = ""
            if output.strip():
                for line in reversed(output.strip().splitlines()):
                    line = line.strip()
                    if line and not line.startswith("---"):
                        err_summary = line[:200]
                        break
            err_summary = err_summary or f"Exit code {proc.returncode}"
            if ds:
                ds.last_deploy_error = err_summary
                bot._store.set_deploy_state(repo_name, ds)
            return False, output, err_summary
    except asyncio.TimeoutError:
        if proc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        err = f"Timed out ({deploy_timeout}s)"
        if ds:
            ds.last_deploy_error = err
            bot._store.set_deploy_state(repo_name, ds)
        return False, "", err
    except Exception as e:
        err = str(e)[:200]
        if ds:
            ds.last_deploy_error = err
            bot._store.set_deploy_state(repo_name, ds)
        return False, "", err


async def _spawn_deploy_fix(
    bot: ClaudeBot,
    repo_name: str,
    deploy_config: dict,
    error_output: str,
    error_summary: str,
) -> None:
    """Auto-spawn a fix session after deploy failure.

    Creates a forum thread, runs the fix prompt, then chains into autopilot.
    If auto_fix_redeploy is enabled, re-runs the deploy after a successful merge.
    """
    from bot.claude.types import InstanceStatus
    from bot.engine import commands as engine_commands
    from bot.engine import workflows

    ds = bot._store.get_deploy_state(repo_name)
    max_retries = deploy_config.get("auto_fix_retries", 1)

    if not ds:
        log.warning("No deploy state for %s — skipping auto-fix", repo_name)
        return

    if ds.auto_fix_attempt >= max_retries:
        log.info("Auto-fix exhausted for %s (%d/%d)", repo_name, ds.auto_fix_attempt, max_retries)
        return

    ds.auto_fix_attempt += 1
    bot._store.set_deploy_state(repo_name, ds)

    # Create fix thread
    thread = await bot._forums.get_or_create_session_thread(
        repo_name, None, f"deploy-fix-{ds.auto_fix_attempt}",
    )
    if not thread:
        log.warning("Could not create auto-fix thread for %s", repo_name)
        return

    channel_id = str(thread.id)
    ds.auto_fix_thread_id = channel_id
    bot._store.set_deploy_state(repo_name, ds)
    asyncio.create_task(bot._forums.refresh_control_room(repo_name))

    # Follow the owner
    try:
        if bot._discord_user_id:
            owner = bot.get_user(bot._discord_user_id)
            if owner:
                await thread.add_user(owner)
    except Exception:
        log.debug("Failed to add owner to auto-fix thread", exc_info=True)

    # Build context — owner-level, plan mode for initial query
    lookup = bot._forums.thread_to_project(channel_id)
    t_info = lookup[1] if lookup else None
    ctx = bot._ctx(channel_id, repo_name=repo_name, thread_info=t_info)
    ctx.is_owner = True
    if t_info:
        bot._forums.attach_session_callbacks(ctx, t_info, channel_id)

    # Notify owner in the thread
    command = deploy_config.get("command", "?")
    owner_id_str = str(bot._discord_user_id) if bot._discord_user_id else ""
    mention = ctx.messenger.format_mention(owner_id_str) if owner_id_str else ""
    notify_text = f"{mention} Deploy failed — auto-fix session started." if mention else "Deploy failed — auto-fix session started."
    try:
        await ctx.messenger.send_text(channel_id, notify_text, silent=False)
    except Exception:
        log.debug("Failed to send auto-fix notification", exc_info=True)

    # Build the fix prompt
    prompt = (
        f"The deploy command `{command}` failed.\n\n"
        f"**Error:** {error_summary}\n\n"
    )
    if error_output.strip():
        prompt += f"**Full output:**\n```\n{error_output}\n```\n\n"
    prompt += (
        "Diagnose the issue and fix it. The deploy command runs in the repo root. "
        "Focus on what would cause this specific error."
    )

    # Run initial query (produces plan instance)
    try:
        await engine_commands.on_text(ctx, prompt)
    except Exception:
        log.exception("Auto-fix on_text failed for %s", repo_name)
        try:
            await ctx.messenger.send_text(
                channel_id, "❌ Auto-fix could not start — query failed.",
            )
        except Exception:
            pass
        ds = bot._store.get_deploy_state(repo_name)
        if ds:
            ds.auto_fix_thread_id = None
            bot._store.set_deploy_state(repo_name, ds)
        asyncio.create_task(bot._forums.refresh_control_room(repo_name))
        return

    # Find the instance that was just created
    if t_info:
        # Re-read in case callback resolved it
        lookup = bot._forums.thread_to_project(channel_id)
        if lookup:
            t_info = lookup[1]
    session_id = t_info.session_id if t_info else None
    if not session_id:
        log.warning("No session_id after auto-fix query for %s", repo_name)
        ds = bot._store.get_deploy_state(repo_name)
        if ds:
            ds.auto_fix_thread_id = None
            bot._store.set_deploy_state(repo_name, ds)
        asyncio.create_task(bot._forums.refresh_control_room(repo_name))
        return

    # Find the latest instance for this session to chain from
    all_instances = bot._store.list_instances()
    session_instances = [i for i in all_instances if i.session_id == session_id]
    if not session_instances:
        log.warning("No instances found for auto-fix session %s", session_id)
        ds = bot._store.get_deploy_state(repo_name)
        if ds:
            ds.auto_fix_thread_id = None
            bot._store.set_deploy_state(repo_name, ds)
        asyncio.create_task(bot._forums.refresh_control_room(repo_name))
        return

    source_inst = session_instances[-1]
    source_msg_id = None
    platform_msgs = source_inst.message_ids.get("discord", [])
    if platform_msgs:
        source_msg_id = platform_msgs[-1]

    # Chain into autopilot: review_loop → build → review_code → done → merge
    try:
        result = await workflows.on_autopilot(
            ctx, source_inst.id, source_msg_id, start_from="review_loop",
        )
    except Exception:
        log.exception("Auto-fix autopilot chain failed for %s", repo_name)
        try:
            await ctx.messenger.send_text(
                channel_id, "❌ Auto-fix chain failed unexpectedly.",
            )
        except Exception:
            pass
        ds = bot._store.get_deploy_state(repo_name)
        if ds:
            ds.auto_fix_thread_id = None
            bot._store.set_deploy_state(repo_name, ds)
        asyncio.create_task(bot._forums.refresh_control_room(repo_name))
        return

    # Check if chain completed successfully (merge happened)
    ds = bot._store.get_deploy_state(repo_name)
    if not ds:
        return

    # branch is cleared by merge_branch; status check prevents false positives
    # from early chain exits (e.g. review instances that never created a branch)
    chain_succeeded = (
        result
        and result.status == InstanceStatus.COMPLETED
        and result.branch is None
    )

    if not chain_succeeded:
        # Chain didn't complete (failed step, needs input, etc.)
        try:
            await ctx.messenger.send_text(
                channel_id,
                "⚠️ Auto-fix chain did not complete — manual intervention needed.",
            )
        except Exception:
            pass
        ds.auto_fix_thread_id = None
        bot._store.set_deploy_state(repo_name, ds)
        asyncio.create_task(bot._forums.refresh_control_room(repo_name))
        return

    # Chain succeeded — optionally redeploy
    if deploy_config.get("auto_fix_redeploy"):
        async def _redeploy_status(msg: str) -> None:
            try:
                await ctx.messenger.send_text(channel_id, msg, silent=True)
            except Exception:
                pass

        success, output, err = await execute_deploy(
            bot, repo_name, deploy_config, status_callback=_redeploy_status,
        )
        if success:
            try:
                await ctx.messenger.send_text(
                    channel_id,
                    f"✅ Redeploy successful.\n```\n{output}\n```" if output.strip()
                    else "✅ Redeploy successful.",
                    silent=True,
                )
            except Exception:
                pass
        else:
            try:
                await ctx.messenger.send_text(
                    channel_id,
                    f"❌ Redeploy also failed: {err}",
                )
            except Exception:
                pass
    else:
        try:
            await ctx.messenger.send_text(
                channel_id,
                "✅ Fix merged. Deploy button is ready — tap it when you're ready to redeploy.",
                silent=True,
            )
        except Exception:
            pass

    # Clean up thread tracking
    ds = bot._store.get_deploy_state(repo_name)
    if ds:
        ds.auto_fix_thread_id = None
        bot._store.set_deploy_state(repo_name, ds)
    asyncio.create_task(bot._forums.refresh_control_room(repo_name))


async def _handle_reboot_repo(
    bot: ClaudeBot, interaction: discord.Interaction, repo_name: str,
) -> None:
    """Execute a reboot/deploy for a repo based on its deploy config.

    Self-managed reboots show drain status in the control room embed.
    Command-based deploys use a temporary status message (deleted after).
    """
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

        # Ephemeral ack — only the user who tapped Reboot sees this
        await interaction.followup.send("Reboot initiated.", ephemeral=True)

        # Drain active tasks — show status in the control room embed
        if bot._runner.is_busy:
            ids = ", ".join(bot._runner.active_ids) or "(between steps)"
            drain_text = f"Waiting for active work: {ids}"
            await bot._forums.refresh_control_room(
                repo_name, drain_status=drain_text,
            )
            idle = await bot._runner.wait_until_idle(timeout=300)
            if not idle:
                remaining = ", ".join(bot._runner.active_ids)
                drain_text = (
                    f"⚠️ Timed out — force-rebooting with "
                    f"{bot._runner.active_count} still running: {remaining}"
                )
                await bot._forums.refresh_control_room(
                    repo_name, drain_status=drain_text,
                )

        # Embed edit confirmed above — safe to request reboot now
        bot._runner.request_reboot({
            "message": msg,
            "channel_id": str(interaction.channel_id),
            "platform": "discord",
        })
        # NOTE: Do NOT reset deploy state here — capture_boot_baselines()
        # handles it on the next startup when it detects self_managed=True.

    elif method == "command":
        repo_path = bot._store.list_repos().get(repo_name)
        if not repo_path:
            await interaction.followup.send(
                f"Repo `{repo_name}` not found — deploy config may be stale.",
                ephemeral=True,
            )
            return

        # Single status message for the deploy cycle (deleted after completion)
        status = await _start_deploy_status(
            interaction, repo_name, "\U0001f680 Pushing to origin...",
            store=bot._store,
        )

        success, output, err_summary = await execute_deploy(
            bot, repo_name, config,
            status_callback=status.update,
        )

        if success:
            await status.update(
                f"\u2705 Deploy successful.\n```\n{output}\n```"
                if output.strip() else "\u2705 Deploy successful.",
            )
        else:
            if output.strip():
                await status.update(
                    f"\u274c Deploy failed.\n```\n{output}\n```",
                )
            else:
                await status.update(f"\u274c Deploy failed: {err_summary}")

            # Auto-fix: spawn a fix session if enabled
            if config.get("auto_fix"):
                asyncio.create_task(_spawn_deploy_fix(
                    bot, repo_name, config, output, err_summary,
                ))

        await status.delete()

    asyncio.create_task(bot._forums.refresh_control_room(repo_name))
    asyncio.create_task(bot._refresh_dashboard())


async def _handle_sync_git(
    bot: ClaudeBot, interaction: discord.Interaction, repo_name: str,
) -> None:
    """Bidirectional git sync: pull from remote (ff-only), then push local changes + tags."""
    import subprocess
    from bot.config import NOWND

    repos = bot._store.list_repos()
    repo_path = repos.get(repo_name)
    if not repo_path:
        await interaction.followup.send(
            f"Repo `{repo_name}` not found.", ephemeral=True,
        )
        return

    ds = bot._store.get_deploy_state(repo_name)
    is_self = ds.self_managed if ds else False

    try:
        # Step 1a: Fetch branches (must succeed)
        fetch = await asyncio.to_thread(
            subprocess.run,
            ["git", "fetch", "origin"],
            cwd=repo_path, capture_output=True, text=True, timeout=30, **NOWND,
        )
        if fetch.returncode != 0:
            err = (fetch.stderr or fetch.stdout or "unknown error")[:200]
            await interaction.followup.send(
                f"`{repo_name}`: Fetch failed \u2014 `{err}`", ephemeral=True,
            )
            return

        # Step 1b: Fetch tags with --force (non-fatal if it fails)
        tag_fetch = await asyncio.to_thread(
            subprocess.run,
            ["git", "fetch", "origin", "--tags", "--force"],
            cwd=repo_path, capture_output=True, text=True, timeout=15, **NOWND,
        )
        if tag_fetch.returncode != 0:
            log.warning("Tag fetch failed for %s: %s", repo_name,
                        (tag_fetch.stderr or "")[:200])

        # Step 2: Check ahead/behind counts
        ahead_result = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-list", "--count", "@{upstream}..HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=10, **NOWND,
        )
        behind_result = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-list", "--count", "HEAD..@{upstream}"],
            cwd=repo_path, capture_output=True, text=True, timeout=10, **NOWND,
        )

        if ahead_result.returncode != 0 or behind_result.returncode != 0:
            await interaction.followup.send(
                f"`{repo_name}`: No upstream branch configured.", ephemeral=True,
            )
            return

        ahead = int(ahead_result.stdout.strip())
        behind = int(behind_result.stdout.strip())
        parts = []

        # Step 3: Pull if behind (fast-forward only)
        if behind > 0:
            # Check for dirty worktree before attempting pull
            status = await asyncio.to_thread(
                subprocess.run,
                ["git", "status", "--porcelain"],
                cwd=repo_path, capture_output=True, text=True, timeout=10, **NOWND,
            )
            if status.returncode == 0 and status.stdout.strip():
                await interaction.followup.send(
                    f"`{repo_name}`: Working tree has uncommitted changes \u2014 commit or stash first.",
                    ephemeral=True,
                )
                return

            pull = await asyncio.to_thread(
                subprocess.run,
                ["git", "pull", "--ff-only"],
                cwd=repo_path, capture_output=True, text=True, timeout=30, **NOWND,
            )
            if pull.returncode != 0:
                err = (pull.stderr or pull.stdout or "unknown error")[:200]
                await interaction.followup.send(
                    f"`{repo_name}`: Pull failed (histories diverged?) \u2014 `{err}`",
                    ephemeral=True,
                )
                return
            parts.append(f"pulled {behind} commit{'s' if behind != 1 else ''}")

            # Update deploy state — HEAD moved forward
            from bot.engine.deploy import (
                get_head_ref, detect_version, get_unreleased_changes,
            )
            if ds:
                ds.current_ref = get_head_ref(repo_path)
                ds.current_version = detect_version(repo_path)
                changes = get_unreleased_changes(repo_path)
                if changes:
                    ds.pending_changes = changes
                bot._store.set_deploy_state(repo_name, ds)

            # Self-managed repo pulled new code — queue reboot
            if is_self:
                parts.append("reboot queued")
                bot._runner.request_reboot({
                    "message": f"Sync Git: pulled {behind} commit(s)",
                })

        # Step 4: Push if ahead
        if ahead > 0:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "push"],
                cwd=repo_path, capture_output=True, text=True, timeout=30, **NOWND,
            )
            if result.returncode == 0:
                parts.append(f"pushed {ahead} commit{'s' if ahead != 1 else ''}")
            else:
                err = (result.stderr or result.stdout or "unknown error")[:200]
                await interaction.followup.send(
                    f"`{repo_name}`: Push failed \u2014 `{err}`", ephemeral=True,
                )
                return

        # Step 5: Push tags (best-effort)
        tag_result = await asyncio.to_thread(
            subprocess.run,
            ["git", "push", "--tags"],
            cwd=repo_path, capture_output=True, text=True, timeout=30, **NOWND,
        )
        if tag_result.returncode == 0:
            tag_lines = [
                line for line in (tag_result.stderr or "").splitlines()
                if "new tag" in line.lower()
            ]
            if tag_lines:
                parts.append(f"{len(tag_lines)} tag{'s' if len(tag_lines) != 1 else ''}")
        else:
            err = (tag_result.stderr or tag_result.stdout or "")[:200]
            if err:
                parts.append(f"tags failed: `{err}`")

        # Step 6: Report
        if not parts:
            await interaction.followup.send(
                f"`{repo_name}`: Already in sync with remote.", ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"`{repo_name}`: {', '.join(parts)}.", ephemeral=True,
            )

        # Refresh control room to reflect any version/ref changes
        asyncio.create_task(bot._forums.refresh_control_room(repo_name))

    except Exception as exc:
        await interaction.followup.send(
            f"`{repo_name}`: Git sync failed \u2014 {exc}", ephemeral=True,
        )
