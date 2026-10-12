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
- attachments.py: attachment limits, ingest and the pending-upload reaper
- usage_gate.py, thread_ctx.py, startup.py: ClaudeBot mixins for the
  usage-limit gate and queue, turn context and replays, and on_ready work
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from bot import config
from bot.discord import channels
from bot.discord.access import AccessResult, load_access_config, check_user_access, has_any_access, get_most_restrictive_ceiling, get_most_restrictive_bash
from bot.discord.adapter import DiscordMessenger
from bot.discord import attachments
from bot.discord.attachments import PendingImagesMixin
from bot.discord import dashboard as dashboard_mod
from bot.discord import idle as idle_mod
from bot.discord import interactions as interactions_mod
from bot.discord import monitoring as monitoring_mod
from bot.discord import slash_commands as slash_commands_mod
from bot.discord import spawn_colors
from bot.discord.startup import StartupMixin
from bot.discord import tags as tags_mod
from bot.discord.forums import ForumManager, ThreadInfo
from bot.discord.thread_ctx import ThreadContextMixin
from bot.discord.titles import generate_title_text, read_ai_title
from bot.discord.usage_gate import UsageGateMixin
from bot.discord import memes
from bot.engine import commands
from bot.platform.base import delete_after
from bot.services.twitter import enrich_with_tweets

if TYPE_CHECKING:
    from bot.claude.runner import ClaudeRunner
    from bot.monitor.service import MonitorService
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


class _AutoDeleteMessenger:
    """Wraps a Messenger to auto-delete messages sent to a specific channel."""

    def __init__(self, inner, lobby_channel_id: str, ttl: float = 10) -> None:
        self._inner = inner
        self._lobby_id = lobby_channel_id
        self._ttl = ttl

    async def send_text(self, channel_id, text, buttons=None, silent=False):
        msg_id = await self._inner.send_text(channel_id, text, buttons, silent)
        if channel_id == self._lobby_id and msg_id:
            asyncio.create_task(delete_after(self._inner, channel_id, msg_id, self._ttl))
        return msg_id

    def __getattr__(self, name):
        return getattr(self._inner, name)


class ClaudeBot(StartupMixin, ThreadContextMixin, UsageGateMixin, PendingImagesMixin, discord.Client):
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
        # One-shot latch so the "no owner configured" error is loud once at
        # first use rather than on every permission check.
        self._owner_unset_warned = False
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
        # Pending /ref context: {thread_id: (context_str, unix_epoch_seconds)}
        # Consumed by on_message, expires after 10 minutes. Persisted to
        # platform_state so a reboot mid-window doesn't drop the user's
        # loaded context (10-min TTL would otherwise be lost in seconds).
        self._pending_refs: dict[str, tuple[str, float]] = {}
        self._load_pending_refs()

        self._monitor_service: MonitorService | None = None
        self._monitor_started: bool = False
        self._notifier = None  # set by app.py after notifier is created
        self._voice_enabled: bool = bool(config.OPENAI_API_KEY)
        # Serializes all read/modify/write ops on config.USAGE_QUEUE_FILE.
        self._usage_queue_lock = asyncio.Lock()
        # channel_id -> window_end_utc; bypass the usage-limit gate while now < end.
        # In-memory by design — a reboot mid-window means one more prompt then quiet.
        self._usage_gate_bypass: dict[str, datetime] = {}
        # Debounce for the upload reaper — see _schedule_pending_image_sweep.
        self._last_image_sweep: float = 0.0
        self._setup_commands()

    @property
    def messenger(self) -> DiscordMessenger:
        if self._messenger is None:
            self._messenger = DiscordMessenger(bot=self)
        return self._messenger

    def _is_owner(self, user_id: int) -> bool:
        """Check if user is the bot owner.

        Fails CLOSED. This used to return True for everybody when
        DISCORD_USER_ID was unset, on the theory that an unconfigured bot has
        no one to lock out -- but the failure mode is a .env that loses the
        line, at which point every user in the guild is silently the owner.
        Nobody being owner is a visible, recoverable outage; everybody being
        owner is not.
        """
        if self._discord_user_id:
            return user_id == self._discord_user_id
        if not self._owner_unset_warned:
            self._owner_unset_warned = True
            log.error(
                "DISCORD_USER_ID is not configured — no user will be treated "
                "as owner and owner-only actions will be refused. Set it in "
                ".env and restart."
            )
        return False

    # --- Pending /ref persistence ---

    _PENDING_REFS_TTL_SECONDS = 600

    def _load_pending_refs(self) -> None:
        """Restore pending /ref context from platform_state (drops expired)."""
        try:
            state = self._store.get_platform_state("discord")
            raw = state.get("pending_refs", {}) or {}
            if not isinstance(raw, dict):
                return
            now = _time.time()
            for tid, entry in raw.items():
                # Tuples become lists in JSON; accept either for forward-compat.
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    continue
                text, ts = entry
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    continue
                if (now - ts) >= self._PENDING_REFS_TTL_SECONDS:
                    continue
                self._pending_refs[str(tid)] = (str(text), ts)
            if self._pending_refs:
                log.info("Restored %d pending /ref entries", len(self._pending_refs))
        except Exception:
            log.debug("Failed to load pending_refs", exc_info=True)

    def _save_pending_refs(self) -> None:
        """Persist pending /ref context to platform_state."""
        try:
            state = self._store.get_platform_state("discord")
            state["pending_refs"] = {
                tid: [text, ts] for tid, (text, ts) in self._pending_refs.items()
            }
            self._store.set_platform_state("discord", state, persist=True)
        except Exception:
            log.debug("Failed to save pending_refs", exc_info=True)

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
            # A grant names ONE repo, and a resolved repo that is not among
            # them is a denial -- not a fall-through onto the user's other
            # grants. Allowing it here handed a guest granted repo A a session
            # in repo B, at B's own settings, which is the whole of what a
            # per-repo grant is supposed to prevent.
            if repo_name:
                return AccessResult(
                    allowed=False, is_owner=False,
                    reason=f"No access grant for `{repo_name}`",
                )
            # Repo genuinely unresolvable (a channel the resolver does not
            # recognise). Allow, but at the tightest settings held anywhere --
            # the permissive defaults would otherwise undo a bash="none" grant.
            return AccessResult(
                allowed=True, is_owner=False,
                mode_ceiling=get_most_restrictive_ceiling(cfg, str(user_id)),
                bash_policy=get_most_restrictive_bash(cfg, str(user_id)),
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

    # --- Delegation to extracted modules ---

    def _schedule_sleep(self, channel_id: str) -> None:
        idle_mod.schedule_sleep(self, channel_id)

    def _cancel_sleep(self, channel_id: str) -> None:
        idle_mod.cancel_sleep(self, channel_id)

    async def _clear_thread_sleeping(self, channel) -> None:
        await idle_mod.clear_thread_sleeping(self, channel)

    async def _try_apply_tags_after_run(self, channel_id: str) -> None:
        await tags_mod.try_apply_tags_after_run(self, channel_id)

    async def _set_thread_active_tag(self, channel, active: bool) -> None:
        await tags_mod.set_thread_active_tag(self, channel, active)

    def _init_monitor_service(self) -> None:
        monitoring_mod.init_monitor_service(self)

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

    # --- Guest lockdown ---

    async def _apply_guest_role(self, member: "discord.Member", *, source: str) -> None:
        """Pin a named guest to the no-access role.

        The role denies View Channel on every channel, so the guest sees
        nothing until an /access grant writes a member-level allow on their own
        forum -- a member allow outranks a role deny, which is what makes
        "nothing, then exactly one thing" expressible at all.

        Deliberately keyed on an explicit username list rather than on "is not
        the owner": this runs on a live community server where ordinary people
        join, and a broad predicate here would silently blind them.
        """
        wanted = config.GUEST_AUTO_ROLE_USERS
        if not wanted:
            return
        names = {
            (member.name or "").lower(),
            (getattr(member, "global_name", None) or "").lower(),
        }
        if not (names & set(wanted)):
            return

        guild = member.guild
        role = discord.utils.get(guild.roles, name=config.GUEST_AUTO_ROLE)
        if role is None:
            log.error(
                "Guest role %r not found — %s (%s) joined UNRESTRICTED",
                config.GUEST_AUTO_ROLE, member.name, member.id,
            )
            await self._alert_owner(
                f"⚠️ **{member.name}** joined but the `{config.GUEST_AUTO_ROLE}` "
                "role does not exist — they can see the public channels. "
                "Create the role and assign it manually.",
            )
            return
        if role in member.roles:
            return
        try:
            await member.add_roles(role, reason="Named guest — no-access lockdown")
        except Exception:
            log.exception("Failed to apply guest role to %s (%s)", member.name, member.id)
            await self._alert_owner(
                f"⚠️ Could not give **{member.name}** the `{role.name}` role "
                "— assign it by hand now, they can currently see the public channels.",
            )
            return
        log.info("Applied guest role to %s (%s) via %s", member.name, member.id, source)
        await self._alert_owner(
            f"🔒 **{member.name}** (`{member.id}`) joined and was locked down with "
            f"`{role.name}` — they can see nothing. Grant their repo with "
            f"`/access grant`.",
        )

    async def _alert_owner(self, text: str) -> None:
        """Best-effort notice to The Ark. Never let it break the join path."""
        try:
            if self._lobby_channel_id:
                ch = self.get_channel(int(self._lobby_channel_id))
                if ch is not None:
                    await ch.send(text)
        except Exception:
            log.debug("Failed to post owner alert", exc_info=True)

    async def on_member_join(self, member: "discord.Member") -> None:
        if member.guild.id != self._guild_id:
            return
        try:
            await self._apply_guest_role(member, source="join")
        except Exception:
            log.exception("on_member_join failed for %s", member.id)

    async def _reconcile_guest_roles(self) -> None:
        """Catch a named guest who joined while the bot was down."""
        if not config.GUEST_AUTO_ROLE_USERS:
            return
        guild = self.get_guild(self._guild_id)
        if not guild:
            return
        for member in guild.members:
            try:
                await self._apply_guest_role(member, source="reconcile")
            except Exception:
                log.exception("Guest reconcile failed for %s", member.id)

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

        # Meme drop: handled inline and returned before any session routing,
        # so a pasted link costs no tokens and spawns no thread. Claims every
        # message in that channel, links or not - it is not a session channel.
        if await memes.handle(message):
            return

        text = message.content.strip()
        text, _image_paths, _go_on = await attachments.ingest_attachments(
            message, text, voice_enabled=self._voice_enabled,
            quiet=message.channel.id in self._forums.archive_channel_ids,
        )
        if not _go_on:
            return
        ctx = None  # set later in forum-thread / unmapped-channel branches

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
                        # Posting in a hidden repo's thread is work in that
                        # repo: same rule as starting a run in one.
                        try:
                            await self._forums.wake_repo_if_dormant(
                                proj.repo_name, notify_channel_id=channel_id)
                        except Exception:
                            log.debug("Wake-on-work check failed for %s",
                                      proj.repo_name, exc_info=True)
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

                        # Inject pending /ref context (wall-clock TTL — survives reboot)
                        ref = self._pending_refs.pop(channel_id, None)
                        if ref:
                            ref_text, ref_time = ref
                            if (_time.time() - ref_time) < self._PENDING_REFS_TTL_SECONDS:
                                text = f"{ref_text}\n\n{text}"
                                log.info("Injected /ref context into prompt in thread %s", ch_name)
                            self._save_pending_refs()

                        self._cancel_sleep(channel_id)
                        # Backgrounded, never awaited: dropping the 💤 is a
                        # thread *name* edit, and thread names carry a
                        # 2-per-10-min limit.  discord.py answers a 429 by
                        # sleeping the caller for the full Retry-After — seen
                        # at 127s on 2026-09-14 — so awaiting it parked the
                        # whole message before `on_text` ever dispatched the
                        # query: no reply, no typing, nothing.  The user
                        # re-sends, that second task skips the edit (the
                        # `_name_editing` guard is already held) and answers
                        # immediately, and then the first task wakes up and
                        # answers too.  That is the double reply.  Every other
                        # caller of this already backgrounds it; this was the
                        # one that did not.  Cosmetic work must never gate the
                        # turn.
                        asyncio.create_task(self._clear_thread_sleeping(message.channel))
                        asyncio.create_task(self._set_thread_active_tag(message.channel, True))
                        asyncio.create_task(self._refresh_dashboard())
                        ctx = self._ctx(channel_id, session_id=session_id,
                                        repo_name=repo_name, thread_info=info,
                                        access_result=msg_access,
                                        source="user_message")
                        ctx.user_id = str(message.author.id)
                        ctx.user_name = message.author.display_name
                        ctx.pending_image_paths = list(_image_paths)
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
            ctx = self._ctx(channel_id, access_result=msg_access,
                            source="user_message")
            ctx.user_id = str(message.author.id)
            ctx.user_name = message.author.display_name
            ctx.pending_image_paths = list(_image_paths)
            try:
                text = await enrich_with_tweets(text)
            except Exception:
                log.warning("Tweet enrichment failed, continuing with original text", exc_info=True)
            await commands.on_text(ctx, text)
        finally:
            # Uploads are deliberately NOT deleted here.  This frame returning
            # does not mean the run that reads the file is done with it — the
            # Steer path dispatches its run from a different task, a killed run
            # gets replaced by one resuming the same transcript, and any later
            # turn can be asked to look at the picture again.  Deleting on exit
            # wiped both of those cases (see reap_pending_images).  The reaper
            # owns the lifecycle now; nudge it so a burst can't sit over cap.
            if _image_paths:
                self._schedule_pending_image_sweep()

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
            forum_project, info = lookup
            if info._title_generated:
                return

            info._title_generated = True

            # Prefer the CLI's native, structured session title (clean — no
            # codename prefixes). Only spawn our own title-gen subprocess when
            # the jsonl has no ai-title yet (rare: very short/errored sessions).
            # read_ai_title does blocking file I/O — keep it off the event loop.
            title = (
                await asyncio.to_thread(read_ai_title, info.session_id)
                if info.session_id else None
            )
            if not title:
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
                # Preserve any spawn-color prefix (load-bearing — without this,
                # smart-title would strip the dot/square that links this thread
                # to its family).
                new_name = await spawn_colors.compose_name(
                    thread_id, new_name, forum_project, self._store,
                )
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
        """Handle button interactions (persistent views).

        Every component interaction leaves here settled: handled, or answered
        with a reason.  Letting one fall through unacknowledged costs the user
        a 3-second spinner ending in "This interaction failed", which looks
        identical to the bot being down and is just as hard to diagnose.
        """
        if interaction.type != discord.InteractionType.component:
            return
        if not self._in_scope(interaction.guild, interaction.channel):
            await interactions_mod.settle(
                interaction,
                "That control is outside this bot's workspace, so it can't act on it.",
            )
            return
        try:
            await interactions_mod.handle(self, interaction)
        except Exception:
            custom_id = (interaction.data or {}).get("custom_id", "?")
            log.exception("Button interaction failed: %s", custom_id)
            await interactions_mod.settle(
                interaction,
                "Something went wrong handling that button — the error is in the bot log.",
            )
