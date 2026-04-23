"""Discord bot with slash commands, message handler, and persistent views.

Forum-based architecture: one ForumChannel per project, one thread per session.
Delegates forum/thread management to ForumManager (bot.discord.forums).

Extracted modules:
- slash_commands.py — slash command registration (~720 lines)
- interactions.py — button/select/modal dispatch (~500 lines)
- idle.py — thread sleep/wake timer management
- tags.py — forum tag management
- monitoring.py — monitor service lifecycle
- modals.py — QuickTaskModal
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time as _time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from bot import config
from bot.discord import channels
from bot.discord import access as access_mod
from bot.discord.access import AccessResult, load_access_config, check_user_access, has_any_access, get_most_restrictive_ceiling, effective_mode as access_effective_mode
from bot.discord.adapter import DiscordMessenger
from bot.discord import dashboard as dashboard_mod
from bot.discord import idle as idle_mod
from bot.discord import interactions as interactions_mod
from bot.discord import monitoring as monitoring_mod
from bot.discord import slash_commands as slash_commands_mod
from bot.discord import tags as tags_mod
from bot.discord.forums import ForumManager, ThreadInfo
from bot.discord.titles import generate_title_text
from bot.engine import commands
from bot.platform.base import RequestContext
from bot.services.twitter import enrich_with_tweets

if TYPE_CHECKING:
    from bot.claude.runner import ClaudeRunner
    from bot.monitor.service import MonitorService
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


def _parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string, returning None on any failure."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class _AutoDeleteMessenger:
    """Wraps a Messenger to auto-delete messages sent to a specific channel."""

    def __init__(self, inner, lobby_channel_id: str, ttl: float = 10) -> None:
        self._inner = inner
        self._lobby_id = lobby_channel_id
        self._ttl = ttl

    async def send_text(self, channel_id, text, buttons=None, silent=False):
        msg_id = await self._inner.send_text(channel_id, text, buttons, silent)
        if channel_id == self._lobby_id and msg_id:
            asyncio.create_task(self._delete_after(channel_id, msg_id))
        return msg_id

    async def _delete_after(self, channel_id: str, msg_id: str) -> None:
        try:
            await asyncio.sleep(self._ttl)
            await self._inner.delete_message(channel_id, msg_id)
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._inner, name)


class ClaudeBot(discord.Client):
    """Discord bot for Claude Code instance management."""

    def __init__(
        self,
        store: StateStore,
        runner: ClaudeRunner,
        guild_id: int,
        lobby_channel_id: int | None = None,
        category_id: int | None = None,
        category_name: str | None = None,
        discord_user_id: int | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True  # needed to resolve owner member for permissions

        super().__init__(intents=intents)

        self._store = store
        self._runner = runner
        self._guild_id = guild_id
        self._lobby_channel_id = lobby_channel_id
        self._category_id = category_id
        self._category_name = category_name
        self._discord_user_id = discord_user_id
        self._ready_event = asyncio.Event()

        self.tree = app_commands.CommandTree(self)
        self._messenger: DiscordMessenger | None = None

        # Forum manager — owns all forum/thread data and operations
        self._forums = ForumManager(
            client=self,
            store=store,
            guild_id=guild_id,
            category_id=category_id,
            discord_user_id=discord_user_id,
        )

        self._name_editing: set[str] = set()  # thread IDs with a name edit in-flight
        self._dashboard_lock = asyncio.Lock()  # Serializes dashboard refreshes
        self._dashboard_pending_flag = [False]  # Mutable flag for dashboard_mod
        self._idle_timers: dict[str, asyncio.TimerHandle] = {}  # channel_id -> scheduled sleep
        self._sleep_gen: dict[str, int] = {}  # generation counter per channel (stale-callback guard)
        # Pending /ref context: {thread_id: (context_str, monotonic_timestamp)}
        # Consumed by on_message, expires after 10 minutes
        self._pending_refs: dict[str, tuple[str, float]] = {}

        self._monitor_service: MonitorService | None = None
        self._monitor_started: bool = False
        self._notifier = None  # set by app.py after notifier is created
        self._voice_enabled: bool = bool(config.OPENAI_API_KEY)
        # Serializes all read/modify/write ops on config.USAGE_QUEUE_FILE.
        self._usage_queue_lock = asyncio.Lock()
        self._setup_commands()

    @property
    def messenger(self) -> DiscordMessenger:
        if self._messenger is None:
            self._messenger = DiscordMessenger(
                bot=self,
                guild_id=self._guild_id,
                lobby_channel_id=self._lobby_channel_id,
                category_id=self._category_id,
            )
        return self._messenger

    def _is_owner(self, user_id: int) -> bool:
        """Check if user is the bot owner."""
        if self._discord_user_id:
            return user_id == self._discord_user_id
        return True

    def _check_access(
        self, user_id: int, *,
        repo_name: str | None = None,
        channel_id: str | None = None,
    ) -> AccessResult:
        """Check if a user has access. Owner always passes."""
        if self._is_owner(user_id):
            return AccessResult(allowed=True, is_owner=True)

        cfg = load_access_config()

        # Resolve repo from channel if not explicit
        if not repo_name and channel_id:
            lookup = self._forums.thread_to_project(channel_id)
            if lookup:
                repo_name = lookup[0].repo_name
            else:
                repo_name = self._resolve_repo_from_user_forum(channel_id, str(user_id))

        grant = check_user_access(cfg, str(user_id), repo_name)
        if grant:
            return AccessResult(
                allowed=True, is_owner=False,
                mode_ceiling=grant.mode,
                bash_policy=grant.bash_policy,
                max_daily_queries=grant.max_daily_queries,
            )

        if has_any_access(cfg, str(user_id)):
            return AccessResult(
                allowed=True, is_owner=False,
                mode_ceiling=get_most_restrictive_ceiling(cfg, str(user_id)),
            )

        return AccessResult(allowed=False, is_owner=False, reason="No access grant")

    def _resolve_repo_from_user_forum(self, channel_id: str, user_id: str) -> str | None:
        """Resolve repo name from a thread in a user's personal forum via tags."""
        try:
            ch = self.get_channel(int(channel_id))
            if not isinstance(ch, discord.Thread):
                return None
            repos = self._store.list_repos()
            for tag in ch.applied_tags:
                if tag.name in repos:
                    return tag.name
        except Exception:
            pass
        return None

    def _ctx(
        self, channel_id: str,
        session_id: str | None = None,
        repo_name: str | None = None,
        thread_info: ThreadInfo | None = None,
        access_result: AccessResult | None = None,
    ) -> RequestContext:
        ctx = RequestContext(
            messenger=self.messenger,
            channel_id=channel_id,
            platform="discord",
            store=self._store,
            runner=self._runner,
            session_id=session_id,
            repo_name=repo_name,
        )
        if thread_info:
            ctx.mode = thread_info.mode
            ctx.context = thread_info.context
            ctx.verbose_level = thread_info.verbose_level
            ctx.effort = thread_info.effort
        if access_result:
            ctx.is_owner = access_result.is_owner
            if not access_result.is_owner and access_result.mode_ceiling:
                ctx.mode_ceiling = access_result.mode_ceiling
                current = ctx.mode or self._store.mode
                ctx.mode = access_effective_mode(
                    access_mod.RepoAccess(mode=access_result.mode_ceiling),
                    current,
                )
            if not access_result.is_owner and access_result.bash_policy:
                ctx.bash_policy = access_result.bash_policy
            if not access_result.is_owner and access_result.max_daily_queries > 0:
                ctx.max_daily_queries = access_result.max_daily_queries
                def _make_rate_callbacks(uid, rn, max_q):
                    def _check():
                        from bot.discord.access import load_access_config, check_rate_limit
                        return check_rate_limit(load_access_config(), uid, max_q)
                    def _increment():
                        from bot.discord.access import load_access_config, increment_query_count
                        increment_query_count(load_access_config(), uid)
                    return _check, _increment
                ctx.check_rate_limit, ctx.increment_query_count = _make_rate_callbacks(
                    ctx.user_id or "", ctx.repo_name, access_result.max_daily_queries,
                )
        # Wire up merged-tag callback (covers all ctx creation paths)
        _cid = channel_id
        _bot = self

        async def _apply_merged_tag():
            ch = _bot.get_channel(int(_cid))
            if isinstance(ch, discord.Thread):
                from bot.discord.tags import apply_thread_tags
                await apply_thread_tags(ch, "completed", merged=True)

        ctx.on_merged = _apply_merged_tag
        ctx.offer_usage_limit_choice = self._offer_usage_limit_choice
        return ctx

    # --- Delegation to extracted modules ---

    def _schedule_sleep(self, channel_id: str) -> None:
        idle_mod.schedule_sleep(self, channel_id)

    def _cancel_sleep(self, channel_id: str) -> None:
        idle_mod.cancel_sleep(self, channel_id)

    async def _set_thread_sleeping(self, channel) -> None:
        await idle_mod.set_thread_sleeping(self, channel)

    async def _clear_thread_sleeping(self, channel) -> None:
        await idle_mod.clear_thread_sleeping(self, channel)

    async def _apply_thread_tags(self, thread, status, origin="bot", mode=None) -> None:
        await tags_mod.apply_thread_tags(thread, status, origin, mode)

    async def _try_apply_tags_after_run(self, channel_id: str) -> None:
        await tags_mod.try_apply_tags_after_run(self, channel_id)

    async def _set_thread_active_tag(self, channel, active: bool) -> None:
        await tags_mod.set_thread_active_tag(self, channel, active)

    async def _monitor_setup(self, name: str) -> str:
        return await monitoring_mod.monitor_setup(self, name)

    def _init_monitor_service(self) -> None:
        monitoring_mod.init_monitor_service(self)

    # --- Resume after reboot ---

    async def _replay_to_thread(
        self, channel_id: str, prompt: str, repo_name: str | None = None,
    ) -> bool:
        """Look up a thread, build context, and dispatch a prompt through on_text.

        Returns True on success, False if the thread wasn't found.
        """
        lookup = self._forums.thread_to_project(channel_id)
        if not lookup:
            log.warning("replay_to_thread: no thread mapping for %s", channel_id)
            return False
        proj, info = lookup
        session_id = info.session_id or None
        resolved_repo = repo_name or (
            proj.repo_name if proj.repo_name != "_default" else None
        )
        self._cancel_sleep(channel_id)
        ctx = self._ctx(channel_id, session_id=session_id, repo_name=resolved_repo,
                        thread_info=info)
        # Replay bypasses the usage-limit gate: the window-end promoter already
        # classified these as queued, and "Run now" clicks have already been
        # consented to.  Re-prompting would loop.
        ctx.offer_usage_limit_choice = None
        await commands.on_text(ctx, prompt)
        self._forums.persist_ctx_settings(ctx)
        asyncio.create_task(self._try_apply_tags_after_run(channel_id))
        self._schedule_sleep(channel_id)
        return True

    async def _wait_for_ready(self, label: str) -> bool:
        """Wait up to 60s for bot + forum map. Returns False on timeout."""
        for _ in range(60):
            if self._ready_event.is_set() and self._forums.forum_projects:
                return True
            await asyncio.sleep(1)
        log.warning("%s: timed out waiting for bot ready + forum map", label)
        return False

    async def dispatch_resume(
        self, channel_id: str, prompt: str, announce: str | None = None,
    ) -> None:
        """Dispatch a query to a forum thread after reboot, resuming the session."""
        if not await self._wait_for_ready("dispatch_resume"):
            return

        if announce:
            asyncio.create_task(self._send_temp_lobby_msg(announce, delay=10))

        try:
            config.REBOOT_MSG_FILE.unlink(missing_ok=True)
        except Exception:
            pass

        if not prompt:
            return

        log.info("Resuming post-reboot in thread %s: %s", channel_id, prompt[:80])
        if await self._replay_to_thread(channel_id, prompt):
            asyncio.create_task(self._refresh_dashboard())

    async def dispatch_drain_queue(self, queue: list[dict]) -> None:
        """Replay messages that were queued during a reboot drain."""
        if not queue:
            return
        if not await self._wait_for_ready("dispatch_drain_queue"):
            return

        log.info("Replaying %d drain-queued messages", len(queue))
        for entry in queue:
            channel_id = entry.get("channel_id")
            if not channel_id:
                continue

            entry_type = entry.get("type", "text")
            if entry_type == "callback":
                await self._replay_callback(entry)
            else:
                prompt = entry.get("prompt")
                if not prompt:
                    continue
                try:
                    log.info("Replaying queued message in thread %s: %s",
                             channel_id, prompt[:60])
                    await self._replay_to_thread(
                        channel_id, prompt, repo_name=entry.get("repo_name"),
                    )
                except Exception:
                    log.exception("Failed to replay queued message in thread %s", channel_id)
        asyncio.create_task(self._refresh_dashboard())

    async def _replay_callback(self, entry: dict) -> None:
        """Replay a button callback action that was interrupted by reboot."""
        channel_id = entry.get("channel_id")
        action = entry.get("action")
        instance_id = entry.get("instance_id")
        if not channel_id or not action or not instance_id:
            log.warning("Incomplete callback replay entry: %s", entry)
            return

        lookup = self._forums.thread_to_project(channel_id)
        if not lookup:
            log.warning("replay_callback: no thread mapping for %s", channel_id)
            return

        proj, info = lookup
        ctx = self._ctx(channel_id, session_id=info.session_id,
                        thread_info=info)
        ctx.user_id = entry.get("user_id", "")
        ctx.user_name = entry.get("user_name", "")
        self._forums.attach_session_callbacks(ctx, info, channel_id)

        # Map origin values back to button action names
        _ORIGIN_TO_ACTION = {
            "plan": "plan",
            "build": "build",
            "review_plan": "review_plan",
            "apply_revisions": "apply_revisions",
            "review_code": "review_code",
            "commit": "commit",
            "done": "done",
            "verify": "verify",
        }
        button_action = _ORIGIN_TO_ACTION.get(action, action)

        log.info("Replaying callback %s:%s in thread %s",
                 button_action, instance_id[:12], channel_id)

        # Acquire channel lock — same as normal button path in interactions.py
        from bot.engine.commands import _get_channel_lock
        lock = _get_channel_lock(channel_id)
        async with lock:
            try:
                await commands.handle_callback(
                    ctx, button_action, instance_id, source_msg_id=None,
                )
            except Exception:
                log.exception("Failed to replay callback %s in thread %s",
                              button_action, channel_id)
            finally:
                self._forums.persist_ctx_settings(ctx)
                asyncio.create_task(self._try_apply_tags_after_run(channel_id))
                self._schedule_sleep(channel_id)

    # --- Usage-limit gate: Run now / Queue for 11am PT / Cancel ---

    async def _offer_usage_limit_choice(
        self, ctx: RequestContext, text: str,
    ) -> bool:
        """Platform hook invoked from on_text during throttle windows.

        Returns True when the gate handled the message (prompt presented to
        user).  Returns False to let on_text run the prompt normally — used
        both when the window isn't active and as a safety fallback if the
        Discord send fails after the entry was persisted.
        """
        from bot.discord.usage_notifier import (
            is_usage_limit_active, next_window_end_utc,
        )
        if not is_usage_limit_active():
            return False
        # Only gate forum-thread messages.  Non-forum channels have no
        # replay path (_replay_to_thread returns False) and queued entries
        # would be orphaned.
        if not self._forums.thread_to_project(ctx.channel_id):
            return False

        qid = uuid.uuid4().hex[:8]
        end_utc = next_window_end_utc()
        entry = {
            "qid": qid,
            "status": "awaiting_choice",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_at": end_utc.isoformat(),
            "channel_id": ctx.channel_id,
            "message_id": None,
            "prompt": text,
            "repo_name": ctx.repo_name,
            "user_id": ctx.user_id,
            "user_name": ctx.user_name,
        }
        # Persist before send so a reboot between render and click cannot
        # silently drop the user's prompt.
        await self._usage_queue_append(entry)

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Run now", style=discord.ButtonStyle.primary,
            custom_id=f"usage_run:{qid}",
        ))
        view.add_item(discord.ui.Button(
            label="Queue for 11am PT", style=discord.ButtonStyle.secondary,
            custom_id=f"usage_queue:{qid}",
        ))
        view.add_item(discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.danger,
            custom_id=f"usage_cancel:{qid}",
        ))

        unlock_ts = int(end_utc.timestamp())
        content = (
            f"⚠️ Usage limits active until <t:{unlock_ts}:t>. "
            f"Run now (will be throttled) or queue for when the window ends?"
        )
        try:
            channel = self.get_channel(int(ctx.channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(ctx.channel_id))
            msg = await channel.send(content=content, view=view)
        except Exception:
            log.exception(
                "usage gate: failed to send choice prompt for %s, falling through",
                qid,
            )
            await self._usage_queue_remove(qid)
            return False

        await self._usage_queue_update(qid, message_id=str(msg.id))
        return True

    # --- Persistent queue helpers (atomic, lock-serialized) ---

    def _read_usage_queue(self) -> list[dict]:
        try:
            data = json.loads(
                config.USAGE_QUEUE_FILE.read_text(encoding="utf-8"),
            )
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _write_usage_queue_atomic(self, entries: list[dict]) -> None:
        tmp = config.USAGE_QUEUE_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, config.USAGE_QUEUE_FILE)

    async def _usage_queue_append(self, entry: dict) -> None:
        async with self._usage_queue_lock:
            entries = self._read_usage_queue()
            entries.append(entry)
            self._write_usage_queue_atomic(entries)

    async def _usage_queue_update(self, qid: str, **fields) -> dict | None:
        async with self._usage_queue_lock:
            entries = self._read_usage_queue()
            updated = None
            for e in entries:
                if e.get("qid") == qid:
                    e.update(fields)
                    updated = e
                    break
            if updated is not None:
                self._write_usage_queue_atomic(entries)
            return updated

    async def _usage_queue_remove(self, qid: str) -> dict | None:
        async with self._usage_queue_lock:
            entries = self._read_usage_queue()
            removed = None
            kept: list[dict] = []
            for e in entries:
                if removed is None and e.get("qid") == qid:
                    removed = e
                    continue
                kept.append(e)
            if removed is not None:
                self._write_usage_queue_atomic(kept)
            return removed

    async def _usage_queue_promote_expired(self, now_utc: datetime) -> None:
        """Flip awaiting_choice -> queued for any entries whose run_at has passed.

        This is the "ignoring the prompt = queuing" semantics. Runs atomically
        so the periodic loop never races with live user clicks on the same qid.
        """
        async with self._usage_queue_lock:
            entries = self._read_usage_queue()
            changed = False
            for e in entries:
                if e.get("status") != "awaiting_choice":
                    continue
                run_at = _parse_iso(e.get("run_at"))
                if run_at and run_at <= now_utc:
                    e["status"] = "queued"
                    changed = True
            if changed:
                self._write_usage_queue_atomic(entries)

    async def _usage_queue_pop_due(self, now_utc: datetime) -> list[dict]:
        """Remove and return all status==queued entries whose run_at has passed."""
        async with self._usage_queue_lock:
            entries = self._read_usage_queue()
            due: list[dict] = []
            kept: list[dict] = []
            for e in entries:
                if e.get("status") == "queued":
                    run_at = _parse_iso(e.get("run_at"))
                    if run_at and run_at <= now_utc:
                        due.append(e)
                        continue
                kept.append(e)
            if due:
                self._write_usage_queue_atomic(kept)
            return due

    async def _fire_due_entries(self, now_utc: datetime) -> None:
        """Shared body: promote expired awaiting_choice, then pop + replay queued."""
        await self._usage_queue_promote_expired(now_utc)
        due = await self._usage_queue_pop_due(now_utc)
        if not due:
            return
        log.info("Usage queue: firing %d due entries", len(due))
        for entry in due:
            # Clear the stale gate message (best-effort) so its buttons don't
            # linger as "already resolved" traps once the prompt is running.
            await self._retire_gate_message(entry)
            try:
                await self._replay_to_thread(
                    entry["channel_id"], entry["prompt"],
                    repo_name=entry.get("repo_name"),
                )
            except Exception:
                log.exception(
                    "usage_queue replay failed for %s", entry.get("qid"),
                )

    async def _retire_gate_message(self, entry: dict) -> None:
        """Edit the gate-prompt message to reflect that it's been fired."""
        msg_id = entry.get("message_id")
        ch_id = entry.get("channel_id")
        if not msg_id or not ch_id:
            return
        try:
            channel = self.get_channel(int(ch_id))
            if channel is None:
                channel = await self.fetch_channel(int(ch_id))
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(content="▶ Window ended — running now.", view=None)
        except Exception:
            pass  # message deleted / no access — purely cosmetic

    async def _usage_queue_startup_drain(self) -> None:
        """Fire any entries overdue at boot — runs once before the periodic loop."""
        if not await self._wait_for_ready("usage_queue_startup_drain"):
            return
        await self._fire_due_entries(datetime.now(timezone.utc))

    async def _usage_queue_replay_loop(self) -> None:
        """Periodic tick (60s). Runs AFTER startup drain completes."""
        while True:
            await asyncio.sleep(60)
            try:
                await self._fire_due_entries(datetime.now(timezone.utc))
            except Exception:
                log.exception("usage_queue_replay_loop iteration failed")

    def _resolve_user_forum_context(
        self, interaction: discord.Interaction,
    ) -> tuple[str, str, str | None] | None:
        """If interaction is inside a user's personal forum, return (user_id, user_name, repo_name)."""
        parent = getattr(interaction.channel, "parent", None)
        if not parent:
            return None
        uf = self._forums.is_user_forum(str(parent.id))
        if not uf:
            return None
        user_id, user_name = uf
        repo_name = None
        cfg = load_access_config()
        ua = cfg.users.get(user_id)
        if ua and ua.repos:
            granted = [r for r in ua.repos if r in self._forums.forum_projects]
            if granted:
                repo_name = granted[0]
        return user_id, user_name, repo_name

    # --- Slash commands (delegated) ---

    async def _run_slash(
        self, interaction: discord.Interaction, coro,
        *, ephemeral: bool = False,
    ) -> None:
        """Defer, run engine command, then delete the 'thinking' response."""
        cmd_name = interaction.command.name if interaction.command else "?"
        log.info("Discord /%s in #%s by %s", cmd_name, getattr(interaction.channel, "name", "?"), interaction.user)
        await interaction.response.defer(ephemeral=ephemeral)
        channel_id = str(interaction.channel_id)
        lookup = self._forums.thread_to_project(channel_id)
        info = lookup[1] if lookup else None
        ar = self._check_access(interaction.user.id, channel_id=channel_id)
        ctx = self._ctx(channel_id, thread_info=info, access_result=ar)
        ctx.user_id = str(interaction.user.id)
        ctx.user_name = interaction.user.display_name
        # Auto-delete responses sent to the lobby (keep it clean)
        in_lobby = interaction.channel_id == self._lobby_channel_id
        if in_lobby:
            ctx.messenger = _AutoDeleteMessenger(ctx.messenger, channel_id)
        try:
            await coro(ctx)
        except Exception:
            log.exception("Slash command failed: /%s", cmd_name)
            try:
                await interaction.followup.send(
                    f"/{cmd_name} failed \u2014 check logs.", ephemeral=True,
                )
            except Exception:
                pass
            try:
                await interaction.delete_original_response()
            except Exception:
                pass
            return
        self._forums.persist_ctx_settings(ctx)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

    def _setup_commands(self) -> None:
        """Register slash commands (delegated to slash_commands module)."""
        slash_commands_mod.setup(self)

    # --- Bot lifecycle ---

    async def setup_hook(self) -> None:
        """Called when the bot is ready. Sync commands to guild."""
        guild = discord.Object(id=self._guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Synced slash commands to guild %s", self._guild_id)

        # Note: ArkView is NOT registered via add_view() — this bot uses
        # centralized on_interaction dispatch (interactions.handle) instead of
        # per-view callbacks.  Registering would intercept custom_ids and
        # raise NotImplementedError (no callback on plain Button items).

    async def on_ready(self) -> None:
        log.info("Discord bot ready as %s", self.user)

        if not self._voice_enabled and not getattr(self, "_voice_warning_logged", False):
            log.warning("OPENAI_API_KEY not configured — voice messages will be ignored")
            self._voice_warning_logged = True

        # Auto-provision category + The Ark if not configured
        if self._category_name and not self._lobby_channel_id:
            guild = self.get_guild(self._guild_id)
            if guild and guild.me:
                category = await channels.ensure_category(
                    guild, self._category_name, guild.me,
                    owner_id=self._discord_user_id,
                )
                self._category_id = category.id
                self._forums.category_id = category.id
                lobby = await channels.ensure_lobby(category)
                self._lobby_channel_id = lobby.id
                self._messenger = None
                log.info("Auto-provisioned category=%s lobby=%s", category.id, lobby.id)

                # Ping owner in new Ark so it appears in their sidebar
                if not [m async for m in lobby.history(limit=1)]:
                    owner = guild.get_member(self._discord_user_id) if self._discord_user_id else None
                    if owner:
                        await lobby.send(
                            f"{owner.mention} The Ark is ready. "
                            f"Add a repo with `/repo add` to get started.",
                            delete_after=60,
                        )

        # Load and reconcile forum mapping
        self._forums.load_forum_map()
        await self._forums.reconcile_forums()

        # Clean up orphaned messages in control rooms (one-time, non-blocking)
        if not getattr(self, '_control_rooms_cleaned', False):
            self._control_rooms_cleaned = True
            asyncio.create_task(self._forums.cleanup_all_control_rooms())

        self._ready_event.set()

        # Start monitor service if there are enabled monitors
        if not self._monitor_started:
            state = self._store.get_platform_state("discord")
            monitors = state.get("monitors", {})
            has_enabled = any(m.get("enabled") for m in monitors.values())
            if has_enabled:
                self._init_monitor_service()
                await self._monitor_service.recover_on_startup()
                self._monitor_service.start()
                self._monitor_started = True
                log.info("Monitor service started with %d enabled monitors",
                         sum(1 for m in monitors.values() if m.get("enabled")))

        # Warm up ccusage cache, then refresh dashboard (sequential so
        # the first dashboard render always has cached usage data)
        asyncio.create_task(self._warmup_then_refresh_lobby())

        # Periodic dashboard refresh (keeps usage data current)
        # Guard: on_ready fires on every reconnect — don't create duplicates
        existing = getattr(self, '_periodic_refresh_task', None)
        if not existing or existing.done():
            from bot.discord.dashboard import start_periodic_refresh
            self._periodic_refresh_task = asyncio.create_task(
                start_periodic_refresh(
                    self, self._store, self._forums,
                    self._lobby_channel_id, self._dashboard_lock,
                    self._dashboard_pending_flag,
                )
            )

        # Start usage limit notifier (DMs owner at 5am/11am PT on weekdays)
        if self._discord_user_id:
            existing_notifier = getattr(self, '_usage_notifier_task', None)
            if not existing_notifier or existing_notifier.done():
                from bot.discord import usage_notifier as _usage_notifier
                self._usage_notifier_task = asyncio.create_task(
                    _usage_notifier.usage_limit_notifier_loop(self, self._discord_user_id)
                )
        elif not getattr(self, '_notifier_warning_logged', False):
            log.warning("Usage limit notifier disabled — DISCORD_USER_ID not set in .env")
            self._notifier_warning_logged = True

        # Usage-queue startup drain + periodic replay loop.  Startup drain
        # runs first and awaits completion so the periodic loop cannot race
        # with it on the same entries.
        existing_replay = getattr(self, '_usage_queue_task', None)
        if not existing_replay or existing_replay.done():
            async def _usage_queue_main() -> None:
                try:
                    await self._usage_queue_startup_drain()
                except Exception:
                    log.exception("usage_queue_startup_drain failed")
                await self._usage_queue_replay_loop()
            self._usage_queue_task = asyncio.create_task(_usage_queue_main())

        # Clean up stale worktrees/branches from interrupted autopilot chains
        asyncio.create_task(self._startup_worktree_cleanup())

    async def _startup_worktree_cleanup(self) -> None:
        """Merge pending autopilot results and clean up orphaned worktrees."""
        try:
            repos = self._store.list_repos()
            if not repos:
                return
            messages = await self._runner.cleanup_stale_worktrees(
                self._store, repos,
            )
            for msg in messages:
                log.info("Startup cleanup: %s", msg)
            if messages:
                log.info("Startup cleanup complete: %d actions", len(messages))
        except Exception:
            log.warning("Startup worktree cleanup failed", exc_info=True)

    def _in_scope(self, guild: discord.Guild | None, channel: discord.abc.GuildChannel | None = None) -> bool:
        """Check guild + channel is within our category."""
        if not guild or guild.id != self._guild_id:
            return False
        if channel and self._category_id:
            if isinstance(channel, discord.Thread):
                parent = channel.parent or guild.get_channel(channel.parent_id)
                cat_id = getattr(parent, "category_id", None) if parent else None
            else:
                cat_id = getattr(channel, "category_id", None)
            if cat_id != self._category_id:
                return False
        return True

    async def close(self) -> None:
        if hasattr(self, '_periodic_refresh_task') and self._periodic_refresh_task:
            self._periodic_refresh_task.cancel()
        if self._monitor_service:
            self._monitor_service.stop()
        await super().close()

    # --- Message handling ---

    async def on_message(self, message: discord.Message) -> None:
        """Handle plain text messages in channels."""
        if message.author == self.user:
            return

        _is_test_webhook = (
            config.TEST_WEBHOOK_IDS
            and message.webhook_id
            and str(message.webhook_id) in config.TEST_WEBHOOK_IDS
        )

        if message.author.bot and not _is_test_webhook:
            return

        if not self._in_scope(message.guild, message.channel):
            return

        if not _is_test_webhook:
            msg_access = self._check_access(
                message.author.id, channel_id=str(message.channel.id),
            )
            if not msg_access.allowed:
                return
        else:
            msg_access = AccessResult(allowed=True, is_owner=True)

        text = message.content.strip()
        _temp_files: list[str] = []

        # Handle file attachments
        IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        AUDIO_EXTS = {".ogg", ".mp3", ".wav", ".m4a", ".webm"}
        if message.attachments:
            log.info(
                "Message has %d attachment(s): %s",
                len(message.attachments),
                [(a.filename, a.size, a.content_type) for a in message.attachments],
            )
        elif not text:
            log.info(
                "Empty message (flags=%s, type=%s, snapshots=%s, embeds=%d)",
                message.flags.value,
                message.type,
                bool(getattr(message, "message_snapshots", None)),
                len(message.embeds),
            )
        for att in message.attachments:
            if not att.filename:
                continue
            ext = Path(att.filename).suffix.lower()

            if ext in AUDIO_EXTS and self._voice_enabled and att.size <= 25_000_000:
                try:
                    file_bytes = await att.read()
                    from bot.services.audio import transcribe
                    transcription = await transcribe(file_bytes, filename=att.filename)
                    cleaned = transcription.strip() if transcription else ""
                    if cleaned:
                        text = f"[Voice message] {cleaned}"
                        log.info("Transcribed voice %s: %s", att.filename, cleaned[:80])
                        # Echo is non-critical — don't lose the transcription if send fails
                        try:
                            echo = cleaned[:1900] + "…" if len(cleaned) > 1900 else cleaned
                            await message.channel.send(f'🎙️ *"{echo}"*')
                        except Exception:
                            log.warning("Failed to send voice echo", exc_info=True)
                    else:
                        try:
                            await message.channel.send("Couldn't detect any speech in that voice message.")
                        except Exception:
                            pass
                        return
                except Exception:
                    log.warning("Voice transcription failed for %s", att.filename, exc_info=True)
                    try:
                        await message.channel.send("Couldn't transcribe that voice message.")
                    except Exception:
                        pass
                    return
                break  # voice consumed, skip remaining attachments

            if ext == ".txt" and att.size <= 500_000:
                try:
                    file_bytes = await att.read()
                    file_text = file_bytes.decode("utf-8", errors="replace")
                    text = f"{text}\n\n{file_text}" if text else file_text
                    log.info("Read .txt attachment %s (%d bytes)", att.filename, att.size)
                except Exception:
                    log.warning("Failed to read attachment %s", att.filename, exc_info=True)

            elif ext in IMAGE_EXTS and att.size <= 10_000_000:
                try:
                    fd, tmp_path = tempfile.mkstemp(suffix=ext)
                    os.close(fd)
                    file_bytes = await att.read()
                    Path(tmp_path).write_bytes(file_bytes)
                    _temp_files.append(tmp_path)
                    img_prompt = f"[Image: {att.filename} saved at {tmp_path}]"
                    if text:
                        text = f"{text}\n\n{img_prompt}"
                    else:
                        text = f"Analyze this screenshot at {tmp_path}. Describe what you see."
                    log.info("Saved image attachment %s (%d bytes) to %s", att.filename, att.size, tmp_path)
                except Exception:
                    log.warning("Failed to save image %s", att.filename, exc_info=True)

        if not text:
            return

        channel_id = str(message.channel.id)
        ch_name = getattr(message.channel, "name", channel_id)
        log.info("Discord msg in #%s: %s", ch_name, text[:80])

        try:
            # --- Archive channels: read-only, never respond ---
            if message.channel.id in self._forums.archive_channel_ids:
                return

            # --- The Ark: informational only, no session routing ---
            if message.channel.id == self._lobby_channel_id:
                if not msg_access.is_owner:
                    return
                if not self._store.list_repos():
                    await message.channel.send(
                        "Add or create a repo to begin \u2014 use `/repo add` or `/repo create`.",
                        delete_after=15,
                    )
                else:
                    await message.channel.send(
                        "Please send prompts inside the repo forum channels, not here.",
                        delete_after=15,
                    )
                try:
                    await message.delete()
                except Exception:
                    pass
                return

            # --- Forum thread: auto-resume session ---
            if isinstance(message.channel, discord.Thread):
                parent = message.channel.parent
                if parent and isinstance(parent, discord.ForumChannel):
                    # Skip control room threads
                    if any(p.control_thread_id == channel_id for p in self._forums.forum_projects.values()):
                        return
                    if channel_id in self._forums.user_control_thread_ids:
                        return
                    lookup = self._forums.thread_to_project(channel_id)
                    if not lookup:
                        # Adopt unmapped thread in a known forum (owner forums)
                        proj = self._forums.forum_by_channel_id(str(parent.id))
                        if proj:
                            log.info("Adopted unmapped thread %s in forum %s", channel_id, parent.name)
                            info = ThreadInfo(thread_id=channel_id, origin="bot")
                            proj.threads[channel_id] = info
                            self._forums.save_forum_map()
                            lookup = (proj, info)

                    # Check if this is a user's personal forum
                    if not lookup:
                        user_forum_info = self._forums.is_user_forum(str(parent.id))
                        if user_forum_info:
                            repo_name = self._forums.user_forum_thread_to_repo(message.channel)
                            if repo_name and repo_name in self._forums.forum_projects:
                                proj = self._forums.forum_projects[repo_name]
                                info = ThreadInfo(
                                    thread_id=channel_id, origin="bot",
                                    user_id=user_forum_info[0],
                                    user_name=user_forum_info[1],
                                )
                                proj.threads[channel_id] = info
                                self._forums.save_forum_map()
                                lookup = (proj, info)
                                log.info("Adopted user forum thread %s repo=%s user=%s",
                                         channel_id, repo_name, user_forum_info[1])
                            elif not repo_name:
                                uid = user_forum_info[0]
                                cfg = load_access_config()
                                ua = cfg.users.get(uid)
                                user_repos = [r for r in (ua.repos if ua else {}) if r in self._forums.forum_projects]
                                if len(user_repos) == 1:
                                    repo_name = user_repos[0]
                                    proj = self._forums.forum_projects[repo_name]
                                    info = ThreadInfo(
                                        thread_id=channel_id, origin="bot",
                                        user_id=user_forum_info[0],
                                        user_name=user_forum_info[1],
                                    )
                                    proj.threads[channel_id] = info
                                    self._forums.save_forum_map()
                                    lookup = (proj, info)
                                    log.info("Auto-selected single repo %s for user %s",
                                             repo_name, user_forum_info[1])
                                else:
                                    await self.messenger.send_text(
                                        channel_id,
                                        "Please select a repo tag on this thread so I know "
                                        "which project to work in.",
                                    )
                                    return

                    if lookup:
                        proj, info = lookup
                        # Track interacting user for close mentions
                        info.user_ids.add(str(message.author.id))
                        session_id = info.session_id or None
                        repo_name = proj.repo_name if proj.repo_name != "_default" else None
                        origin = info.origin

                        if session_id and origin == "cli":
                            log.info("Thread %s resuming CLI session %s — transitioning to bot ownership",
                                     ch_name, session_id[:12])
                            info.origin = "bot"
                            self._forums.save_forum_map()

                        was_pending = not session_id

                        # Inject pending /ref context
                        ref = self._pending_refs.pop(channel_id, None)
                        if ref:
                            ref_text, ref_time = ref
                            if (_time.monotonic() - ref_time) < 600:
                                text = f"{ref_text}\n\n{text}"
                                log.info("Injected /ref context into prompt in thread %s", ch_name)

                        self._cancel_sleep(channel_id)
                        await self._clear_thread_sleeping(message.channel)
                        asyncio.create_task(self._set_thread_active_tag(message.channel, True))
                        asyncio.create_task(self._refresh_dashboard())
                        ctx = self._ctx(channel_id, session_id=session_id,
                                        repo_name=repo_name, thread_info=info,
                                        access_result=msg_access)
                        ctx.user_id = str(message.author.id)
                        ctx.user_name = message.author.display_name
                        self._forums.attach_session_callbacks(ctx, info, channel_id)
                        user_text = text  # preserve before tweet enrichment for title/topic
                        try:
                            try:
                                text = await enrich_with_tweets(text)
                            except Exception:
                                log.warning("Tweet enrichment failed, continuing with original text", exc_info=True)
                            await commands.on_text(ctx, text)
                        finally:
                            self._forums.persist_ctx_settings(ctx)
                            if was_pending:
                                await self._forums.finalize_pending_thread(channel_id, message.channel, user_text)
                            if not info._title_generated:
                                summary = self._forums.get_latest_summary(channel_id)
                                asyncio.create_task(self._generate_smart_title(
                                    message.channel, user_text, summary))
                            asyncio.create_task(self._try_apply_tags_after_run(channel_id))
                            self._schedule_sleep(channel_id)
                            asyncio.create_task(self._refresh_dashboard())
                        return

            # --- Other channel (unmapped): no session ---
            ctx = self._ctx(channel_id, access_result=msg_access)
            ctx.user_id = str(message.author.id)
            ctx.user_name = message.author.display_name
            try:
                text = await enrich_with_tweets(text)
            except Exception:
                log.warning("Tweet enrichment failed, continuing with original text", exc_info=True)
            await commands.on_text(ctx, text)
        finally:
            for tmp in _temp_files:
                Path(tmp).unlink(missing_ok=True)

    async def _route_lobby_message(
        self, message: discord.Message, text: str, repo_name: str | None,
    ) -> None:
        """Route a lobby message to a forum thread."""
        repo_name = repo_name or "_default"
        asyncio.create_task(self._forums.ensure_control_post(repo_name))
        thread = await self._forums.get_or_create_session_thread(
            repo_name, None, text,
            user_id=str(message.author.id),
            user_name=message.author.display_name,
        )
        if thread:
            try:
                await thread.add_user(message.author)
            except Exception:
                log.warning("Failed to auto-follow user %s in thread %s",
                            message.author.id, thread.id)
            try:
                await message.delete()
            except Exception:
                pass
            asyncio.create_task(self._send_redirect(thread))
            tid = str(thread.id)
            self._cancel_sleep(tid)
            await self._clear_thread_sleeping(thread)
            asyncio.create_task(self._set_thread_active_tag(thread, True))
            asyncio.create_task(self._refresh_dashboard())
            lookup = self._forums.thread_to_project(tid)
            t_info = lookup[1] if lookup else None
            ctx = self._ctx(tid, repo_name=repo_name if repo_name != "_default" else None,
                            thread_info=t_info)
            if t_info:
                self._forums.attach_session_callbacks(ctx, t_info, tid)
            try:
                await commands.on_text(ctx, text)
            finally:
                self._forums.persist_ctx_settings(ctx)
                await self._forums.update_pending_thread(tid)
                summary = self._forums.get_latest_summary(tid)
                asyncio.create_task(self._generate_smart_title(thread, text, summary))
                asyncio.create_task(self._try_apply_tags_after_run(tid))
                self._schedule_sleep(tid)
                asyncio.create_task(self._refresh_dashboard())

    # --- Dashboard (delegated to dashboard_mod) ---

    async def _refresh_dashboard(self) -> None:
        """Update or create the pinned dashboard embed in lobby."""
        await dashboard_mod.refresh_dashboard(
            self, self._store, self._forums,
            self._lobby_channel_id, self._dashboard_lock,
            self._dashboard_pending_flag,
        )

    async def _warmup_then_refresh_lobby(self) -> None:
        """Warm ccusage cache, then refresh dashboard + clean lobby.

        Sequential ordering guarantees the first dashboard render has
        cached usage data instead of racing the warmup task.
        """
        from bot.engine.usage import warmup as _usage_warmup
        await _usage_warmup()
        await self._refresh_dashboard()
        await self._cleanup_lobby()

    async def _generate_smart_title(
        self, thread: discord.Thread, prompt: str, summary: str = "",
    ) -> None:
        """Fire-and-forget: generate an LLM title and rename the thread."""
        info = None
        try:
            thread_id = str(thread.id)
            lookup = self._forums.thread_to_project(thread_id)
            if not lookup:
                return
            _, info = lookup
            if info._title_generated:
                return

            info._title_generated = True

            title = await generate_title_text(prompt, summary)
            if not title:
                log.warning("Title generation returned empty for thread %s", thread_id)
                info._title_generated = False
                return

            base = channels.build_title_name(title)

            if thread_id in self._name_editing:
                info._title_generated = False
                return
            self._name_editing.add(thread_id)
            try:
                new_name = channels.build_thread_name(base)
                await thread.edit(name=new_name)
            finally:
                self._name_editing.discard(thread_id)

            info.topic = title
            self._forums.save_forum_map()
            log.info("Smart title for thread %s: %s", thread_id, new_name)
        except Exception:
            log.warning("Smart title generation failed for thread %s", thread.id, exc_info=True)
            if info is not None:
                info._title_generated = False

    # --- Session creation helpers ---

    async def _create_new_session(
        self, interaction: discord.Interaction, repo_name: str | None,
        *, redirect: bool = False, mode: str | None = None,
        user_id: str | None = None, user_name: str | None = None,
    ) -> None:
        """Create a new session thread."""
        repo_name = repo_name or "_default"

        forum_channel_id = None
        if user_id:
            cfg = load_access_config()
            ua = cfg.users.get(user_id)
            if ua and ua.forum_channel_id:
                forum_channel_id = ua.forum_channel_id

        thread = await self._forums.get_or_create_session_thread(
            repo_name, None, "new-session",
            forum_channel_id=forum_channel_id,
            user_id=user_id, user_name=user_name,
        )
        if thread:
            try:
                await thread.add_user(interaction.user)
            except Exception:
                log.warning("Failed to auto-follow user %s in thread %s",
                            interaction.user.id, thread.id)
            if mode:
                lookup = self._forums.thread_to_project(str(thread.id))
                if lookup:
                    lookup[1].mode = mode
                    self._forums.save_forum_map()
            if user_id or not redirect:
                await interaction.followup.send(
                    f"Fresh session created: <#{thread.id}>", ephemeral=True,
                )
            else:
                asyncio.create_task(self._send_redirect(thread))
        else:
            msg = "Could not create thread."
            if user_id or not redirect:
                await interaction.followup.send(msg, ephemeral=True)
            else:
                asyncio.create_task(self._send_temp_lobby_msg(msg))

    async def _send_temp_lobby_msg(self, text: str, delay: float = 5) -> None:
        """Send a temporary message in lobby that auto-deletes."""
        try:
            lobby = self.get_channel(self._lobby_channel_id)
            if lobby and isinstance(lobby, discord.TextChannel):
                msg = await lobby.send(text)
                await asyncio.sleep(delay)
                await msg.delete()
        except Exception:
            pass

    async def _send_redirect(self, thread: discord.Thread) -> None:
        """Post a redirect link in lobby, auto-delete after 5s."""
        await self._send_temp_lobby_msg(f"\u2192 <#{thread.id}>")

    async def _cleanup_lobby(self) -> None:
        """Delete all non-pinned messages from lobby on startup."""
        lobby = self.get_channel(self._lobby_channel_id)
        if not lobby or not isinstance(lobby, discord.TextChannel):
            return
        pinned_ids = {m.id for m in await lobby.pins()}
        deleted = 0

        # Pass 1: bulk-delete recent messages (<14 days) — fast
        try:
            purged = await lobby.purge(
                limit=100, check=lambda m: m.id not in pinned_ids,
            )
            deleted += len(purged)
        except Exception:
            log.warning("Lobby purge failed", exc_info=True)

        # Pass 2: individually delete older messages (>14 days)
        batch = 0
        try:
            async for msg in lobby.history(limit=100):
                if msg.id not in pinned_ids:
                    try:
                        await msg.delete()
                        deleted += 1
                        batch += 1
                        if batch >= 5:
                            await asyncio.sleep(1)
                            batch = 0
                    except Exception:
                        pass
        except Exception:
            log.warning("Lobby old-message cleanup failed", exc_info=True)

        if deleted:
            log.info("Lobby cleanup: deleted %d messages", deleted)

    # --- Interaction dispatch (delegated) ---

    async def on_error(self, event_method, *args, **kwargs) -> None:
        """Route event handler exceptions to the log file (not just stderr)."""
        log.exception("Unhandled exception in %s", event_method)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Handle button interactions (persistent views)."""
        if interaction.type != discord.InteractionType.component:
            return
        if not self._in_scope(interaction.guild, interaction.channel):
            return
        await interactions_mod.handle(self, interaction)
