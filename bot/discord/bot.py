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
import logging
import os
import tempfile
import time as _time
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

if TYPE_CHECKING:
    from bot.claude.runner import ClaudeRunner
    from bot.monitor.service import MonitorService
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


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

    async def dispatch_resume(
        self, channel_id: str, prompt: str, announce: str | None = None,
    ) -> None:
        """Dispatch a query to a forum thread after reboot, resuming the session."""
        for attempt in range(60):
            if self._ready_event.is_set() and self._forums.forum_projects:
                break
            await asyncio.sleep(1)
        else:
            log.warning("dispatch_resume: timed out waiting for bot ready + forum map")
            return

        if announce:
            try:
                await self.messenger.send_text(channel_id, announce)
            except Exception:
                log.warning("dispatch_resume: failed to send announcement", exc_info=True)

        try:
            config.REBOOT_MSG_FILE.unlink(missing_ok=True)
        except Exception:
            pass

        if not prompt:
            return

        lookup = self._forums.thread_to_project(channel_id)
        if not lookup:
            log.warning("dispatch_resume: no thread mapping for %s (have %d projects)",
                        channel_id, len(self._forums.forum_projects))
            return
        proj, info = lookup
        session_id = info.session_id or None
        repo_name = proj.repo_name if proj.repo_name != "_default" else None
        log.info("Resuming post-reboot in thread %s session=%s: %s",
                 channel_id, session_id and session_id[:12], prompt[:80])
        self._cancel_sleep(channel_id)
        ctx = self._ctx(channel_id, session_id=session_id, repo_name=repo_name,
                        thread_info=info)
        await commands.on_text(ctx, prompt)
        self._forums.persist_ctx_settings(ctx)
        asyncio.create_task(self._try_apply_tags_after_run(channel_id))
        self._schedule_sleep(channel_id)
        asyncio.create_task(self._refresh_dashboard())

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
        await coro(ctx)
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

    async def on_ready(self) -> None:
        log.info("Discord bot ready as %s", self.user)

        if not self._voice_enabled and not getattr(self, "_voice_warning_logged", False):
            log.warning("OPENAI_API_KEY not configured — voice messages will be ignored")
            self._voice_warning_logged = True

        # Auto-provision category + lobby if not configured
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

        # Load and reconcile forum mapping
        self._forums.load_forum_map()
        await self._forums.reconcile_forums()

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

        # Refresh dashboard on startup
        asyncio.create_task(self._refresh_dashboard())

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
            # --- Lobby: route to forum thread (owner only) ---
            if message.channel.id == self._lobby_channel_id:
                if not msg_access.is_owner:
                    return
                active_repo, _ = self._store.get_active_repo()
                await self._route_lobby_message(message, text, active_repo)
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
                        asyncio.create_task(self._clear_thread_sleeping(message.channel))
                        asyncio.create_task(self._set_thread_active_tag(message.channel, True))
                        asyncio.create_task(self._refresh_dashboard())
                        ctx = self._ctx(channel_id, session_id=session_id,
                                        repo_name=repo_name, thread_info=info,
                                        access_result=msg_access)
                        ctx.user_id = str(message.author.id)
                        ctx.user_name = message.author.display_name
                        self._forums.attach_session_callbacks(ctx, info, channel_id)
                        try:
                            await commands.on_text(ctx, text)
                        finally:
                            self._forums.persist_ctx_settings(ctx)
                            if was_pending:
                                await self._forums.finalize_pending_thread(channel_id, message.channel, text)
                            if not info._title_generated:
                                summary = self._forums.get_latest_summary(channel_id)
                                asyncio.create_task(self._generate_smart_title(
                                    message.channel, text, summary))
                            asyncio.create_task(self._try_apply_tags_after_run(channel_id))
                            self._schedule_sleep(channel_id)
                            asyncio.create_task(self._refresh_dashboard())
                        return

            # --- Other channel (unmapped): no session ---
            ctx = self._ctx(channel_id, access_result=msg_access)
            ctx.user_id = str(message.author.id)
            ctx.user_name = message.author.display_name
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
            asyncio.create_task(self._clear_thread_sleeping(thread))
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
