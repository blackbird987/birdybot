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
from bot.discord.modals import QuickTaskModal, VerifyAddModal
from bot.engine import commands
from bot.engine import sessions as sessions_mod
from bot.platform.formatting import MODE_COLOR, VALID_EFFORTS, VALID_MODES, effort_name, mode_name

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

# Button callback actions that trigger long-running LLM queries
_QUERY_ACTIONS: frozenset[str] = frozenset({
    "retry", "plan", "build", "review_plan", "apply_revisions",
    "review_code", "commit", "done", "autopilot", "autopilot_hold",
    "build_and_ship", "continue_autopilot", "continue_ppu",
    "amend", "continue_anyway",
})

# Human-readable labels for the usage-limit gate UI.  Falls back to a
# title-cased version of the action name for any action not listed.
_ACTION_LABELS: dict[str, str] = {
    "plan": "Plan",
    "build": "Build",
    "review_plan": "Review plan",
    "apply_revisions": "Apply revisions",
    "review_code": "Review code",
    "commit": "Commit",
    "done": "Done",
    "retry": "Retry",
    "amend": "Amend",
    "autopilot": "Autopilot",
    "autopilot_hold": "Autopilot (Hold)",
    "build_and_ship": "Build & ship",
    "continue_autopilot": "Continue autopilot",
    "continue_ppu": "Continue",
    "continue_anyway": "Continue anyway",
}


def action_label(action: str) -> str:
    """Human-readable label for a button action (used by usage-limit gate)."""
    return _ACTION_LABELS.get(action, action.replace("_", " ").title())

# Verify-board sub-action → (resulting status, verb prompt, past-tense confirm).
# Keyed by the "sub" segment of verify_menu/verify_select custom_ids.
_VERIFY_ACTIONS: dict[str, tuple[str, str, str]] = {
    "done":    ("done",      "mark done", "marked done"),
    "claim":   ("claimed",   "claim",     "claimed"),
    "dismiss": ("dismissed", "dismiss",   "dismissed"),
}

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

    # --- Verify Board ---
    if custom_id.startswith("verify_menu:"):
        await _handle_verify_menu_open(bot, interaction, custom_id)
        return
    if custom_id.startswith("verify_select:"):
        await _handle_verify_select(bot, interaction, custom_id)
        return
    if custom_id.startswith("verify_add:"):
        # Must send modal as initial response — not defer
        repo_name = custom_id.split(":", 1)[1]
        await _handle_verify_add(bot, interaction, repo_name)
        return
    if custom_id.startswith("verify_history:"):
        repo_name = custom_id.split(":", 1)[1]
        await _handle_verify_history(bot, interaction, repo_name)
        return
    if custom_id.startswith("verify_board:"):
        # Send modal prefilled from session result embed
        instance_id = custom_id.split(":", 1)[1]
        await _handle_verify_board_from_embed(bot, interaction, instance_id)
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

    # --- Verify-board: Add modal (no origin) ---
    if action == "verify_add":
        modal = VerifyAddModal(bot, instance_id)
        await interaction.response.send_modal(modal)
        return

    # --- Verify-board: Send-from-session modal (origin = current thread) ---
    if action == "verify_board":
        await _open_verify_board_modal(bot, interaction, instance_id)
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

    # --- Pending-prompt interactions (Steer / Cancel on Queued embed) ---
    # Here the trailing portion of the custom_id is a pending_id, not an
    # instance_id, but we reuse the same ``parts`` split for consistency.
    if action in ("steer", "cancel_pending"):
        await _handle_pending_action(bot, interaction, action, instance_id, btn_access)
        return

    # --- Usage-limit gate interactions (Run now / Queue / Cancel) ---
    # The trailing portion is a qid assigned by _offer_usage_limit_choice.
    if action in ("usage_run", "usage_queue", "usage_cancel"):
        await _handle_usage_action(bot, interaction, action, instance_id)
        return

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

    # --- Branch from here: fork the JSONL at this message and open new thread ---
    if action == "branch":
        await _handle_branch(bot, interaction, instance_id, btn_access)
        return

    # --- Verify-board: lane action menu (open ephemeral select) ---
    if action == "verify_menu":
        await _handle_verify_menu(bot, interaction, instance_id)
        return

    # --- Verify-board: select submit (bulk status change) ---
    if action == "verify_select":
        await _handle_verify_select(bot, interaction, instance_id)
        return

    # --- Verify-board: history (ephemeral text) ---
    if action == "verify_history":
        await _handle_verify_history(bot, interaction, instance_id)
        return

    # --- Generic query button dispatch (plan, build, review, etc.) ---
    channel_id = str(interaction.channel_id)
    source_msg_id = str(interaction.message.id) if interaction.message else None

    is_query = action in _QUERY_ACTIONS

    # Build ctx early so the usage-limit gate can see user info.
    lookup = bot._forums.thread_to_project(channel_id)
    t_info = lookup[1] if lookup else None
    ctx = bot._ctx(channel_id, session_id=t_info.session_id if t_info else None,
                    thread_info=t_info, access_result=btn_access)
    ctx.user_id = str(interaction.user.id)
    ctx.user_name = interaction.user.display_name

    # Usage-limit gate: during throttle windows, offer Run/Queue/Cancel
    # before spawning a Claude session.  Skipped when an instance is
    # already running on the channel — the existing pending UI
    # (Steer/Cancel) owns the mid-run case, and the gate would hide it.
    if is_query:
        from bot.engine.commands import _get_channel_lock as _peek_channel_lock
        if not _peek_channel_lock(channel_id).locked():
            handled = await bot._offer_usage_limit_choice_for_callback(
                ctx, action, instance_id, source_msg_id,
            )
            if handled:
                return

    # Cosmetic side-effects flip the thread to "active" — do this only
    # after we've decided the query is actually going to run.
    if is_query:
        bot._cancel_sleep(channel_id)
        asyncio.create_task(bot._clear_thread_sleeping(interaction.channel))
        asyncio.create_task(bot._set_thread_active_tag(interaction.channel, True))
        asyncio.create_task(bot._refresh_dashboard())

    if t_info:
        bot._forums.attach_session_callbacks(ctx, t_info, channel_id)

    # Acquire channel lock for query actions to prevent concurrent spawns
    # (matches the serialization in _run_query for text messages).
    # Mid-run button taps register an interactive Queued entry with Steer/Cancel.
    if is_query:
        from bot.engine.commands import (
            _finish_pending_on_acquire, _get_channel_lock,
            _enqueue_with_pending_ui,
        )
        lock = _get_channel_lock(channel_id)
        # Buttons trigger callbacks rather than raw prompts — capture the
        # callback args in structured fields so Steer can re-dispatch without
        # string encoding.  ``_enqueue_with_pending_ui`` no-ops when the lock
        # is free.
        pending = await _enqueue_with_pending_ui(
            ctx, "",
            callback_action=action,
            callback_instance_id=instance_id,
            callback_source_msg_id=source_msg_id,
        )
        async with lock:
            if await _finish_pending_on_acquire(ctx, pending):
                return
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
                # Refresh forum tag immediately so the mode tag matches the
                # toggle without waiting for the next run to finish.
                asyncio.create_task(bot._try_apply_tags_after_run(channel_id))


# --- Individual handlers ---


async def _handle_pending_action(
    bot: ClaudeBot,
    interaction: discord.Interaction,
    action: str,
    pending_id: str,
    btn_access: AccessResult,
) -> None:
    """Dispatch Steer / Cancel taps on a Queued embed.

    Steer: kill the currently-running instance, wait for finalize, then
    re-spawn the pending prompt (or re-invoke the pending callback) with
    a steering header prepended so Claude knows the prior turn was
    interrupted.  Idempotent: second taps no-op via handled_by_steer.

    Cancel: mark as cancelled and delete the embed.  The lock-holder's
    post-lock path sees ``cancelled`` and skips execution.
    """
    from bot.engine import commands as commands_mod
    from bot.engine import pending as pending_mod
    from bot.engine.commands import _get_channel_lock, _execute_query

    pending = pending_mod.get(pending_id)
    if not pending:
        # Already processed (e.g., prior run finished and lock-holder dequeued
        # it before the tap arrived). Best-effort clean-up of the embed.
        try:
            msg = interaction.message
            if msg:
                await msg.delete()
        except Exception:
            pass
        await interaction.followup.send(
            "This pending prompt is no longer active.", ephemeral=True,
        )
        return

    if action == "cancel_pending":
        pending.cancelled = True
        try:
            if pending.message_id:
                await bot.messenger.delete_message(
                    pending.channel_id, pending.message_id,
                )
        except Exception:
            pass
        pending_mod.clear(pending_id)
        return

    # --- Steer ---
    if pending.handled_by_steer:
        return  # second tap
    if not bot._runner.provider.supports_steer:
        await interaction.followup.send(
            "Steer isn't supported by the current provider.", ephemeral=True,
        )
        return

    pending.handled_by_steer = True
    # Update the embed to show progress
    try:
        if pending.message_id:
            await bot.messenger.edit_text(
                pending.channel_id, pending.message_id,
                "⚡ Steering current run...", None,
            )
    except Exception:
        pass

    # Resolve the live active instance at tap-time rather than trusting the
    # snapshot taken when pending was enqueued.  With queue depth > 1, the
    # original lock-holder may have already finished and a newer run (from an
    # earlier-queued pending) could be holding the lock now — we want to kill
    # whoever is running right now, not the stale reference.
    live_active = (
        bot._runner.active_instance_for_session(pending.session_id)
        or pending.active_instance_id
    )
    if live_active:
        try:
            await bot._runner.kill_and_wait(live_active)
        except Exception:
            log.exception("kill_and_wait failed during Steer")

    # Delete the "Steering..." message so the real run can render clean progress
    try:
        if pending.message_id:
            await bot.messenger.delete_message(
                pending.channel_id, pending.message_id,
            )
    except Exception:
        pass

    channel_id = pending.channel_id
    lookup = bot._forums.thread_to_project(channel_id)
    t_info = lookup[1] if lookup else None
    ctx = bot._ctx(channel_id,
                   session_id=t_info.session_id if t_info else pending.session_id,
                   thread_info=t_info, access_result=btn_access)
    ctx.user_id = pending.user_id or str(interaction.user.id)
    ctx.user_name = pending.user_name or interaction.user.display_name
    if t_info:
        bot._forums.attach_session_callbacks(ctx, t_info, channel_id)

    # Dispatch: re-run the pending payload. Two flavors:
    #   1. Raw user prompt → prepend STEER_HEADER and run as a query
    #   2. Structured callback → re-invoke the original button handler
    #      (can't easily steer a callback — just rerun it)
    lock = _get_channel_lock(channel_id)
    async with lock:
        try:
            pending_mod.clear(pending_id)
            # Last-chance cancel check: user may have tapped Cancel during
            # kill_and_wait.  Once Steer fires we've already killed the prior
            # run, but we can still honor the cancel by not dispatching.
            if pending.cancelled:
                return
            if pending.callback_action and pending.callback_instance_id:
                await commands_mod.handle_callback(
                    ctx,
                    pending.callback_action,
                    pending.callback_instance_id,
                    pending.callback_source_msg_id,
                )
            else:
                steered = f"{pending_mod.STEER_HEADER}\n\n{pending.prompt_text}"
                # Double-checked locking mirror from commands._run_query
                if not ctx.session_id and ctx.resolve_session_id is not None:
                    fresh = ctx.resolve_session_id()
                    if fresh:
                        ctx.session_id = fresh
                await _execute_query(ctx, steered)
        finally:
            # Always run cleanup, including after cancel-early-return: the
            # prior task was killed so the thread is idle and needs its sleep
            # timer (re)scheduled and tags refreshed.
            bot._forums.persist_ctx_settings(ctx)
            await bot._forums.update_pending_thread(channel_id)
            asyncio.create_task(bot._try_apply_tags_after_run(channel_id))
            bot._schedule_sleep(channel_id)
            asyncio.create_task(bot._refresh_dashboard())


async def _handle_usage_action(
    bot: ClaudeBot,
    interaction: discord.Interaction,
    action: str,
    qid: str,
) -> None:
    """Dispatch Run / Queue / Cancel clicks from the usage-limit gate."""
    msg = interaction.message

    # "Already resolved" paths never overwrite the message content — the
    # winning path (auto-fire, another click) has already rendered the
    # correct final state.  We only edit the message when THIS click wins.

    if action == "usage_cancel":
        removed = await bot._usage_queue_remove(qid)
        if removed:
            from bot.discord.bot import _unlink_image_paths
            _unlink_image_paths(removed.get("image_paths", []), site="cancel")
            if msg:
                try:
                    await msg.edit(content="Cancelled.", view=None)
                except Exception:
                    pass
        else:
            await interaction.followup.send(
                "This prompt was already resolved.", ephemeral=True,
            )
        return

    if action == "usage_queue":
        updated = await bot._usage_queue_update(qid, status="queued")
        if not updated:
            await interaction.followup.send(
                "This prompt was already resolved.", ephemeral=True,
            )
            return
        from bot.discord.usage_notifier import next_window_end_utc
        unlock_ts = int(next_window_end_utc().timestamp())
        # Show which button was queued so users skimming a thread can tell
        # stacked entries apart.  Text prompts use the generic "Queued".
        if updated.get("type") == "callback":
            queued_label = f"{action_label(updated.get('action', ''))} queued"
        else:
            queued_label = "Queued"
        if msg:
            try:
                await msg.edit(
                    content=f"⏸ {queued_label} — will run at <t:{unlock_ts}:t>.",
                    view=None,
                )
            except Exception:
                pass
        return

    # action == "usage_run"
    removed = await bot._usage_queue_remove(qid)
    if not removed:
        await interaction.followup.send(
            "This prompt was already resolved.", ephemeral=True,
        )
        return

    if msg:
        try:
            await msg.edit(content="Running now…", view=None)
        except Exception:
            pass

    channel_id = removed["channel_id"]
    # Quiet the gate for this thread for the rest of the throttle window —
    # one Run Now click consents to bypass everything until the window ends.
    from bot.discord.usage_notifier import next_window_end_utc
    bot._usage_gate_bypass[channel_id] = next_window_end_utc()

    # Dispatch by entry type.  Callback entries replay the original button
    # action via _replay_callback (no images involved).  Text entries replay
    # the prompt and own the image-file lifecycle.
    if removed.get("type", "text") == "callback":
        asyncio.create_task(bot._replay_callback(removed))
    else:
        prompt = removed["prompt"]
        repo_name = removed.get("repo_name")
        image_paths = removed.get("image_paths", [])

        async def _run_and_cleanup() -> None:
            from bot.discord.bot import _strip_missing_image_refs, _unlink_image_paths
            cleaned = _strip_missing_image_refs(prompt, image_paths)
            try:
                await bot._replay_to_thread(channel_id, cleaned, repo_name=repo_name)
            finally:
                _unlink_image_paths(image_paths, site="run_now")

        # Fire-and-forget so the interaction handler returns promptly; the
        # lock inside _replay_to_thread serializes any concurrent spawn.
        asyncio.create_task(_run_and_cleanup())


# --- Verify Board handlers ---


# action_kind → (target_status, present-tense verb, past-tense verb).
# Single source of truth for the three bulk-action variants.
_VERIFY_ACTIONS: dict[str, tuple[str, str, str]] = {
    "done":    ("done",      "mark done", "marked done"),
    "claim":   ("claimed",   "claim",     "claimed"),
    "dismiss": ("dismissed", "dismiss",   "dismissed"),
}


async def _handle_verify_menu_open(
    bot: ClaudeBot, interaction: discord.Interaction, custom_id: str,
) -> None:
    """`verify_menu:{action}:{repo}` → ephemeral select-menu popup for bulk action."""
    from bot.discord.verify_board import build_select_options

    try:
        _, action_kind, repo_name = custom_id.split(":", 2)
    except ValueError:
        await interaction.response.send_message("Bad verify menu id.", ephemeral=True)
        return
    if action_kind not in _VERIFY_ACTIONS:
        await interaction.response.send_message("Unknown action.", ephemeral=True)
        return
    proj = bot._forums.forum_projects.get(repo_name)
    if not proj:
        await interaction.response.send_message(
            f"Unknown repo: `{repo_name}`", ephemeral=True,
        )
        return
    options = build_select_options(proj.verify_items)
    if not options:
        await interaction.response.send_message(
            "Nothing to act on — the board is empty.", ephemeral=True,
        )
        return

    _, verb_present, _ = _VERIFY_ACTIONS[action_kind]
    view = discord.ui.View(timeout=180)
    view.add_item(discord.ui.Select(
        placeholder=f"Pick items to {verb_present}",
        min_values=1,
        max_values=len(options),
        options=options,
        custom_id=f"verify_select:{action_kind}:{repo_name}",
    ))
    await interaction.response.send_message(
        f"Pick items to **{verb_present}**:", view=view, ephemeral=True,
    )


async def _handle_verify_select(
    bot: ClaudeBot, interaction: discord.Interaction, custom_id: str,
) -> None:
    """Select-menu submit for bulk status change."""
    from bot.engine.verify import set_status

    try:
        _, action_kind, repo_name = custom_id.split(":", 2)
    except ValueError:
        await interaction.response.send_message("Bad select id.", ephemeral=True)
        return
    action = _VERIFY_ACTIONS.get(action_kind)
    if not action:
        await interaction.response.send_message("Unknown action.", ephemeral=True)
        return
    target_status, _, verb_past = action

    values = interaction.data.get("values", []) if interaction.data else []
    if not values:
        await interaction.response.send_message("No items selected.", ephemeral=True)
        return

    proj = bot._forums.forum_projects.get(repo_name)
    if not proj:
        await interaction.response.send_message(
            f"Unknown repo: `{repo_name}`", ephemeral=True,
        )
        return

    user_id = str(interaction.user.id)
    updated = 0
    lock = bot._forums.verify_lock(repo_name)
    async with lock:
        for vid in values:
            if set_status(proj.verify_items, vid, target_status, user_id):
                updated += 1
        if updated:
            bot._forums.save_forum_map()

    if updated:
        bot._forums.schedule_verify_refresh(repo_name)

    await interaction.response.edit_message(
        content=f"{updated} item(s) {verb_past}.", view=None,
    )


async def _handle_verify_add(
    bot: ClaudeBot, interaction: discord.Interaction, repo_name: str,
) -> None:
    """`verify_add:{repo}` → open blank modal."""
    if repo_name not in bot._forums.forum_projects:
        await interaction.response.send_message(
            f"Unknown repo: `{repo_name}`", ephemeral=True,
        )
        return
    modal = VerifyAddModal(bot, repo_name)
    await interaction.response.send_modal(modal)


async def _handle_verify_history(
    bot: ClaudeBot, interaction: discord.Interaction, repo_name: str,
) -> None:
    """`verify_history:{repo}` → ephemeral 30-day history embed."""
    from bot.discord.verify_board import build_history_embed

    proj = bot._forums.forum_projects.get(repo_name)
    if not proj:
        await interaction.response.send_message(
            f"Unknown repo: `{repo_name}`", ephemeral=True,
        )
        return
    embed = build_history_embed(repo_name, proj.verify_items)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def _handle_verify_board_from_embed(
    bot: ClaudeBot, interaction: discord.Interaction, instance_id: str,
) -> None:
    """`verify_board:{instance_id}` → modal with origin backlink prefilled.

    Prefills from `inst.summary` (the one-liner set at finalize), since
    `inst.prompt` on workflow-button sessions is often a static template
    string (e.g. "Implement the plan…") which isn't useful as a verify item.
    Blank prefill is fine — user types what they want checked.
    """
    inst = bot._store.get_instance(instance_id)
    if not inst:
        await interaction.response.send_message(
            "Instance not found.", ephemeral=True,
        )
        return
    if inst.repo_name not in bot._forums.forum_projects:
        await interaction.response.send_message(
            f"No forum for repo `{inst.repo_name}`.", ephemeral=True,
        )
        return
    thread_id = str(interaction.channel_id) if interaction.channel_id else None
    prefill = ""
    if inst.summary:
        # First line of the summary, trimmed for the short-form input.
        prefill = inst.summary.strip().splitlines()[0][:80]
    modal = VerifyAddModal(
        bot, inst.repo_name,
        prefill=prefill,
        origin_thread_id=thread_id,
        origin_thread_name=inst.id,       # already in "t-2842" / "q-5707" form
        origin_instance_id=inst.id,
    )
    await interaction.response.send_modal(modal)


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
    asyncio.create_task(bot._try_apply_tags_after_run(thread_id))
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


async def _handle_branch(
    bot: ClaudeBot, interaction: discord.Interaction,
    instance_id: str, btn_access: AccessResult,
) -> None:
    """Fork the session JSONL at this message and open a new thread there.

    The fork lives in the destination project dir keyed by the repo's CURRENT
    path (looked up via ``store.list_repos()``), so ``--resume`` works even if
    the instance's frozen ``repo_path`` has since been moved or renamed.
    """
    from bot import config
    from bot.engine import workflows
    from bot.engine.session_fork import (
        encode_project_path, fork_session, get_last_assistant_uuid,
    )

    inst = bot._store.get_instance(instance_id)
    if not inst:
        await interaction.followup.send("Instance not found.", ephemeral=True)
        return
    if not inst.session_id:
        await interaction.followup.send(
            "Can't branch — this instance has no session.", ephemeral=True,
        )
        return

    # Resolve the CURRENT repo path (instance.repo_path is frozen at creation
    # and may be stale if the user moved/re-registered the repo).
    repo_name = inst.repo_name or ""
    current_repo_path = bot._store.list_repos().get(repo_name) if repo_name else None
    repo_path = current_repo_path or inst.repo_path
    if not repo_path:
        await interaction.followup.send(
            "Can't branch — no repo path for this session.", ephemeral=True,
        )
        return

    dest_dir = config.CLAUDE_PROJECTS_DIR / encode_project_path(repo_path)
    src_path = dest_dir / f"{inst.session_id}.jsonl"
    if not src_path.exists():
        # Fall back to a global search (handles encoding mismatches).
        found = await asyncio.to_thread(sessions_mod.find_session_file, inst.session_id)
        if not found:
            await interaction.followup.send(
                "Can't branch — source session file not found.", ephemeral=True,
            )
            return
        src_path = found

    source_msg_id = str(interaction.message.id) if interaction.message else None
    target_uuid = inst.jsonl_uuid_by_msg_id.get(source_msg_id) if source_msg_id else None
    if not target_uuid:
        # Fallback: anchor at the most recent assistant uuid in the source.
        target_uuid = await asyncio.to_thread(get_last_assistant_uuid, src_path)
        if not target_uuid:
            await interaction.followup.send(
                "Can't branch — no fork point recorded.", ephemeral=True,
            )
            return

    fork_result = await asyncio.to_thread(fork_session, src_path, target_uuid, dest_dir)
    if not fork_result:
        await interaction.followup.send(
            "Branch failed — see bot logs for details.", ephemeral=True,
        )
        return
    new_session_id, _new_path = fork_result
    log.info(
        "Branched session %s -> %s at uuid %s",
        inst.session_id[:8], new_session_id[:8], target_uuid[:8],
    )

    # Build a topic for the new thread (60-char cap; emoji prefix stays well
    # under Discord's 100-char channel-name limit).
    base = (inst.summary or inst.prompt or "branched session").strip()
    first_line = base.splitlines()[0] if base else "branched"
    topic = f"\U0001f33f {first_line[:60]}"

    forum_repo_name = repo_name or "_default"
    thread = await bot._forums.get_or_create_session_thread(
        forum_repo_name, new_session_id, topic, origin="branch",
    )
    if not thread:
        await interaction.followup.send(
            "Branch failed — couldn't create thread.", ephemeral=True,
        )
        return

    asyncio.create_task(bot._send_redirect(thread))
    lookup_info = bot._forums.thread_to_project(str(thread.id))
    ti = lookup_info[1] if lookup_info else None
    ctx = bot._ctx(
        str(thread.id), session_id=new_session_id,
        repo_name=repo_name if repo_name else None,
        thread_info=ti,
        access_result=btn_access,
    )
    ctx.user_id = str(interaction.user.id)
    ctx.user_name = interaction.user.display_name

    try:
        await interaction.followup.send(
            f"\U0001f33f Branched into <#{thread.id}>.", ephemeral=True,
        )
    except Exception:
        pass

    await workflows.on_sess_resume(ctx, new_session_id, source_msg_id=None)


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
                # Also reset generic auto-fix state for deploy trigger
                from bot.engine.auto_fix import AutoFixState
                bot._store.set_auto_fix_state(repo_name, "deploy", AutoFixState())
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

    Delegates to the generic auto-fix primitive. If auto_fix_redeploy is
    enabled, re-runs the deploy after a successful merge.
    Syncs AutoFixState back to DeployState for control room display.
    """
    from bot.engine.auto_fix import spawn_fix_session

    command = deploy_config.get("command", "?")
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

    # Build redeploy callback if enabled
    on_success = None
    if deploy_config.get("auto_fix_redeploy"):
        async def _redeploy() -> None:
            await execute_deploy(bot, repo_name, deploy_config)
        on_success = _redeploy

    # Sync AutoFixState → DeployState for control room display
    def _sync_deploy_state(channel_id: str) -> None:
        ds = bot._store.get_deploy_state(repo_name)
        if ds:
            afs = bot._store.get_auto_fix_state(repo_name, "deploy")
            ds.auto_fix_attempt = afs.attempt
            ds.auto_fix_thread_id = afs.thread_id
            bot._store.set_deploy_state(repo_name, ds)

    await spawn_fix_session(
        bot, repo_name,
        trigger="deploy",
        error_summary=error_summary,
        error_output=error_output,
        fix_prompt=prompt,
        max_retries=deploy_config.get("auto_fix_retries", 1),
        max_cost_usd=2.0,
        on_success=on_success,
        on_started=_sync_deploy_state,
    )

    # Final sync after chain completes (thread_id now None)
    _sync_deploy_state("")


async def _post_deploy_healthcheck(
    bot: ClaudeBot,
    repo_name: str,
    deploy_config: dict,
    healthcheck: dict,
) -> None:
    """Wait, then run health check commands. Trigger auto-fix if unhealthy."""
    from bot.engine.auto_fix import spawn_fix_session

    delay = healthcheck.get("delay_secs", 30)
    commands = healthcheck.get("commands", [])
    if not commands:
        return

    await asyncio.sleep(delay)

    repo_path = bot._store.list_repos().get(repo_name, "")

    for cmd in commands:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=repo_path or None,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                error_output = stdout.decode(errors="replace")[:1500]
                log.warning("Health check failed for %s: %s (exit %d)",
                            repo_name, cmd, proc.returncode)

                if deploy_config.get("auto_fix"):
                    prompt = (
                        f"Post-deploy health check `{cmd}` failed (exit {proc.returncode}).\n\n"
                    )
                    if error_output.strip():
                        prompt += f"**Output:**\n```\n{error_output}\n```\n\n"
                    prompt += (
                        "Diagnose why this health check fails after a successful deploy. "
                        "Focus on recent changes that could cause this."
                    )

                    async def _redeploy() -> None:
                        await execute_deploy(bot, repo_name, deploy_config)

                    await spawn_fix_session(
                        bot, repo_name,
                        trigger="healthcheck",
                        error_summary=f"Health check failed: {cmd}",
                        error_output=error_output,
                        fix_prompt=prompt,
                        max_retries=1,
                        max_cost_usd=1.5,
                        on_success=_redeploy if deploy_config.get("auto_fix_redeploy") else None,
                    )
                return
        except asyncio.TimeoutError:
            log.warning("Health check timed out for %s: %s", repo_name, cmd)
            return
        except Exception:
            log.exception("Health check error for %s: %s", repo_name, cmd)
            return

    log.info("All health checks passed for %s", repo_name)


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
            # Post-deploy health gate
            healthcheck = config.get("healthcheck")
            if healthcheck:
                asyncio.create_task(_post_deploy_healthcheck(
                    bot, repo_name, config, healthcheck,
                ))
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


# --- Verify Board handlers ---


def _split_verify_payload(payload: str) -> tuple[str, str] | None:
    """Split "<sub>:<repo>" payload from a verify_menu/verify_select custom_id."""
    sub, _, repo = payload.partition(":")
    if not sub or not repo:
        return None
    return sub, repo


async def _handle_verify_menu(
    bot: ClaudeBot, interaction: discord.Interaction, payload: str,
) -> None:
    """Open an ephemeral select menu listing items for a lane action."""
    from bot.discord import verify_board as vb_mod
    from bot.engine import verify as verify_mod

    parsed = _split_verify_payload(payload)
    if not parsed:
        await interaction.followup.send("Bad menu payload.", ephemeral=True)
        return
    sub, repo_name = parsed
    action = _VERIFY_ACTIONS.get(sub)
    if not action:
        await interaction.followup.send("Bad action.", ephemeral=True)
        return
    _new_status, label, _past = action

    proj = bot._forums.forum_projects.get(repo_name)
    if not proj:
        await interaction.followup.send(
            f"No verify-board for `{repo_name}`.", ephemeral=True,
        )
        return

    # For "done" and "dismiss" we offer pending+claimed; for "claim" only pending
    if sub == "claim":
        items = verify_mod.get_by_lane(proj.verify_items, "needs_check")
    else:
        items = (
            verify_mod.get_by_lane(proj.verify_items, "needs_check")
            + verify_mod.get_by_lane(proj.verify_items, "claimed")
        )

    if not items:
        await interaction.followup.send(
            f"No items to {label}.", ephemeral=True,
        )
        return

    view = vb_mod.build_lane_select_view(repo_name, sub, label, items)
    await interaction.followup.send(
        f"Select item(s) to {label}:", view=view, ephemeral=True,
    )


async def _handle_verify_select(
    bot: ClaudeBot, interaction: discord.Interaction, payload: str,
) -> None:
    """Apply a bulk status change from the select-menu submission."""
    from bot.engine import verify as verify_mod

    parsed = _split_verify_payload(payload)
    if not parsed:
        await interaction.followup.send("Bad select payload.", ephemeral=True)
        return
    sub, repo_name = parsed
    action = _VERIFY_ACTIONS.get(sub)
    if not action:
        await interaction.followup.send("Bad action.", ephemeral=True)
        return
    new_status, _verb, past_label = action

    values = interaction.data.get("values", []) if interaction.data else []
    item_ids = [v for v in values if v]
    if not item_ids:
        await interaction.followup.send("Nothing selected.", ephemeral=True)
        return

    user_id = interaction.user.id

    def _do(items: list[dict]) -> int:
        return verify_mod.bulk_set_status(items, item_ids, new_status, user_id=user_id)

    updated = await bot._forums._mutate_verify(repo_name, _do)
    if not updated:
        await interaction.followup.send(
            "Items already changed by someone else.", ephemeral=True,
        )
        return

    plural = "s" if updated != 1 else ""
    # Replace the ephemeral select message with a confirmation
    try:
        await interaction.edit_original_response(
            content=f"{updated} item{plural} {past_label}.", view=None,
        )
    except Exception:
        await interaction.followup.send(
            f"{updated} item{plural} {past_label}.", ephemeral=True,
        )


async def _open_verify_board_modal(
    bot: ClaudeBot, interaction: discord.Interaction, instance_id: str,
) -> None:
    """Open VerifyAddModal pre-filled from a session-result button.

    custom_id: "verify_board:{instance_id}". The instance carries the repo
    name and origin metadata. The user can edit the prefilled text before
    submitting.
    """
    inst = bot._store.get_instance(instance_id)
    if not inst or not inst.repo_name:
        await interaction.response.send_message(
            "No repo for this session.", ephemeral=True,
        )
        return

    # Use summary if available, else first line of prompt
    base = (inst.summary or inst.prompt or "").strip()
    first_line = base.splitlines()[0] if base else ""
    prefill = first_line[:120]

    origin_thread_id: int | None = None
    origin_thread_name: str | None = None
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        origin_thread_id = channel.id
        origin_thread_name = channel.name

    modal = VerifyAddModal(
        bot, inst.repo_name,
        prefill=prefill,
        origin_thread_id=origin_thread_id,
        origin_thread_name=origin_thread_name,
        origin_instance_id=instance_id,
    )
    await interaction.response.send_modal(modal)
