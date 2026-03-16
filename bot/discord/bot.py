"""Discord bot with slash commands, message handler, and persistent views.

Forum-based architecture: one ForumChannel per project, one thread per session.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import tempfile
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from bot import config
from bot.discord import channels
from bot.discord import access as access_mod
from bot.discord.access import AccessResult, load_access_config, check_user_access, has_any_access, get_most_restrictive_ceiling, effective_mode as access_effective_mode
from bot.discord.adapter import DiscordMessenger
from bot.engine import commands
from bot.engine import sessions as sessions_mod
from bot.platform.base import RequestContext
from bot.platform.formatting import MODE_COLOR, MODE_DISPLAY, VALID_MODES, format_age, mode_label, mode_name

if TYPE_CHECKING:
    from bot.claude.runner import ClaudeRunner
    from bot.monitor.service import MonitorService
    from bot.store.state import StateStore

log = logging.getLogger(__name__)

_DISCORD_EPOCH_MS = 1420070400000

# Button callback actions that trigger long-running LLM queries
_QUERY_ACTIONS: frozenset[str] = frozenset({
    "retry", "plan", "build", "review_plan", "apply_revisions",
    "review_code", "commit", "done", "autopilot", "build_and_ship",
    "continue_autopilot",
})


def _snowflake_age(snowflake_id: int) -> str:
    """Human-readable age from a Discord snowflake ID."""
    created_ms = (snowflake_id >> 22) + _DISCORD_EPOCH_MS
    created = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
    return format_age(datetime.now(timezone.utc) - created)


# --- Data structures ---


@dataclass
class ThreadInfo:
    thread_id: str
    session_id: str | None = None
    origin: str = "bot"           # "bot" or "cli"
    topic: str = ""
    _synced_msg_count: int = 0
    _title_generated: bool = False
    # Per-thread settings (None = inherit global default)
    mode: str | None = None
    context: str | None = None        # None=inherit, ""=cleared, str=set
    verbose_level: int | None = None
    # User who created this thread (None = owner)
    user_id: str | None = None
    user_name: str | None = None

    def to_dict(self) -> dict:
        d = {
            "thread_id": self.thread_id,
            "session_id": self.session_id,
            "origin": self.origin,
            "topic": self.topic,
            "_synced_msg_count": self._synced_msg_count,
            "_title_generated": self._title_generated,
        }
        if self.mode is not None:
            d["mode"] = self.mode
        if self.context is not None:
            d["context"] = self.context
        if self.verbose_level is not None:
            d["verbose_level"] = self.verbose_level
        if self.user_id is not None:
            d["user_id"] = self.user_id
        if self.user_name is not None:
            d["user_name"] = self.user_name
        return d

    @classmethod
    def from_dict(cls, data: dict) -> ThreadInfo:
        return cls(
            thread_id=data["thread_id"],
            session_id=data.get("session_id"),
            origin=data.get("origin", "bot"),
            topic=data.get("topic", ""),
            _synced_msg_count=data.get("_synced_msg_count", 0),
            _title_generated=data.get("_title_generated", False),
            mode=data.get("mode"),
            context=data.get("context"),
            verbose_level=data.get("verbose_level"),
            user_id=data.get("user_id"),
            user_name=data.get("user_name"),
        )


@dataclass
class ForumProject:
    repo_name: str
    forum_channel_id: str
    threads: dict[str, ThreadInfo] = field(default_factory=dict)
    control_thread_id: str | None = None
    control_message_id: str | None = None

    def to_dict(self) -> dict:
        d = {
            "repo_name": self.repo_name,
            "forum_channel_id": self.forum_channel_id,
            "threads": {k: v.to_dict() for k, v in self.threads.items()},
        }
        if self.control_thread_id:
            d["control_thread_id"] = self.control_thread_id
        if self.control_message_id:
            d["control_message_id"] = self.control_message_id
        return d

    @classmethod
    def from_dict(cls, data: dict) -> ForumProject:
        threads = {
            k: ThreadInfo.from_dict(v)
            for k, v in data.get("threads", {}).items()
        }
        return cls(
            repo_name=data["repo_name"],
            forum_channel_id=data.get("forum_channel_id", ""),
            threads=threads,
            control_thread_id=data.get("control_thread_id"),
            control_message_id=data.get("control_message_id"),
        )


# On Windows, prevent subprocess console windows from popping up
_NOWND: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
)


async def _generate_title_text(prompt: str, summary: str = "") -> str | None:
    """Spawn a lightweight Claude CLI call to generate a 4-6 word thread title.

    Bypasses the runner semaphore — this is a standalone, cheap subprocess.
    Returns the title string, or None on failure/timeout.
    """
    from bot.claude.parser import extract_result, parse_stream_line

    title_prompt = (
        "Generate a 4-6 word title for this coding session. "
        "Maximum 6 words. No articles or filler words like 'the', 'a', 'for'. "
        "Output ONLY the title — no quotes, no explanation.\n\n"
        f"User asked: {prompt[:300]}\n"
    )
    if summary:
        title_prompt += f"\nResult: {summary[:500]}"

    cmd = [
        config.CLAUDE_BINARY, "-p", title_prompt,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "plan",
        "--max-turns", "1",
    ]

    env = os.environ.copy()
    env.pop("CLAUDE_CODE", None)
    env.pop("CLAUDECODE", None)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            **_NOWND,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=config.TITLE_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        log.debug("Title generation timed out")
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
        return None
    except Exception:
        log.warning("Title generation CLI call failed", exc_info=True)
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
        return None

    # Parse stream-json to extract result text
    events = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        parsed = parse_stream_line(line)
        if parsed:
            events.append(parsed)

    result = extract_result(events)
    if not result.result_text:
        return None

    # Take first line only (LLM might add explanation on subsequent lines)
    title = result.result_text.strip().split("\n")[0].strip()
    # Strip markdown formatting: leading # (headers), inline *_`
    title = re.sub(r'^#+\s*', '', title)
    title = re.sub(r'[*_`]', '', title)
    title = title.strip('"\'').strip()
    title = re.sub(r'[.!?:]+$', '', title).strip()
    return title if len(title) >= 3 else None


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

        # Forum-based project mapping: repo_name -> ForumProject
        self._forum_projects: dict[str, ForumProject] = {}
        self._forum_lock = asyncio.Lock()
        self._thread_lock = asyncio.Lock()
        self._name_lock = asyncio.Lock()  # Serializes thread.edit(name=...) calls
        self._dashboard_lock = asyncio.Lock()  # Serializes dashboard refreshes
        self._dashboard_pending = False  # True if a refresh is queued behind the lock
        self._idle_timers: dict[str, asyncio.TimerHandle] = {}  # channel_id -> scheduled sleep
        self._sleep_gen: dict[str, int] = {}  # generation counter per channel (stale-callback guard)
        # Pending /ref context: {thread_id: (context_str, monotonic_timestamp)}
        # Consumed by on_message, expires after 10 minutes
        self._pending_refs: dict[str, tuple[str, float]] = {}
        # In-memory set of user forum control room thread IDs (O(1) skip check)
        self._user_control_thread_ids: set[str] = set()

        self._monitor_service: MonitorService | None = None
        self._monitor_started: bool = False
        self._notifier = None  # set by app.py after notifier is created
        self._voice_enabled: bool = bool(config.OPENAI_API_KEY)
        # Pending voice transcriptions: {confirm_msg_id: {transcription, author_id, channel_id}}
        self._pending_voice: dict[str, dict] = {}
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
            # Check owner's forum projects
            lookup = self._thread_to_project(channel_id)
            if lookup:
                repo_name = lookup[0].repo_name
            else:
                # Check user forums: thread parent might be a user's personal forum
                repo_name = self._resolve_repo_from_user_forum(channel_id, str(user_id))

        grant = check_user_access(cfg, str(user_id), repo_name)
        if grant:
            return AccessResult(
                allowed=True,
                is_owner=False,
                mode_ceiling=grant.mode,
                bash_policy=grant.bash_policy,
                max_daily_queries=grant.max_daily_queries,
            )

        # No grant for this specific repo — but allow if user has ANY grant
        # (needed for repo-less commands like /help, /status, /list)
        if has_any_access(cfg, str(user_id)):
            return AccessResult(
                allowed=True,
                is_owner=False,
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
        if access_result:
            ctx.is_owner = access_result.is_owner
            if not access_result.is_owner and access_result.mode_ceiling:
                ctx.mode_ceiling = access_result.mode_ceiling
                # Enforce mode ceiling on initial mode
                current = ctx.mode or self._store.mode
                ctx.mode = access_effective_mode(
                    access_mod.RepoAccess(mode=access_result.mode_ceiling),
                    current,
                )
        return ctx

    def _persist_ctx_settings(self, ctx: RequestContext) -> None:
        """Write any ctx setting overrides back to ThreadInfo for persistence."""
        lookup = self._thread_to_project(ctx.channel_id)
        if not lookup:
            return
        _, info = lookup
        changed = False
        if ctx.mode is not None and ctx.mode != info.mode:
            info.mode = ctx.mode
            changed = True
        if ctx.context is not None and ctx.context != info.context:
            info.context = ctx.context
            changed = True
        if ctx.verbose_level is not None and ctx.verbose_level != info.verbose_level:
            info.verbose_level = ctx.verbose_level
            changed = True
        if changed:
            self._save_forum_map()

    # --- Resume after reboot ---

    async def dispatch_resume(
        self, channel_id: str, prompt: str, announce: str | None = None,
    ) -> None:
        """Dispatch a query to a forum thread after reboot, resuming the session.

        If announce is set, send it to the channel before running the prompt.
        """
        # Wait for on_ready (forum map loads there) — poll with retries
        for attempt in range(60):  # up to 60 seconds
            if self._ready_event.is_set() and self._forum_projects:
                break
            await asyncio.sleep(1)
        else:
            log.warning("dispatch_resume: timed out waiting for bot ready + forum map")
            return

        # Send reboot confirmation now that Discord is ready
        if announce:
            try:
                await self.messenger.send_text(channel_id, announce)
            except Exception:
                log.warning("dispatch_resume: failed to send announcement", exc_info=True)

        # Consume the reboot message file now that Discord is ready
        try:
            config.REBOOT_MSG_FILE.unlink(missing_ok=True)
        except Exception:
            pass

        # If no prompt, we're done (just needed the announcement)
        if not prompt:
            return

        lookup = self._thread_to_project(channel_id)
        if not lookup:
            log.warning("dispatch_resume: no thread mapping for %s (have %d projects)",
                        channel_id, len(self._forum_projects))
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
        self._persist_ctx_settings(ctx)
        asyncio.create_task(self._try_apply_tags_after_run(channel_id))
        self._schedule_sleep(channel_id)
        asyncio.create_task(self._refresh_dashboard())

    # --- Forum-Session Mapping ---

    def _load_forum_map(self) -> None:
        """Load forum→project mapping from platform_state."""
        state = self._store.get_platform_state("discord")
        raw = state.get("forum_projects", {})
        self._forum_projects = {
            k: ForumProject.from_dict(v) for k, v in raw.items()
        }
        log.info("Loaded %d forum projects", len(self._forum_projects))

    def _save_forum_map(self) -> None:
        """Persist forum→project mapping to platform_state."""
        state = self._store.get_platform_state("discord")
        state["forum_projects"] = {k: v.to_dict() for k, v in self._forum_projects.items()}
        self._store.set_platform_state("discord", state, persist=True)

    def _session_to_thread(self, session_id: str) -> tuple[str, ThreadInfo] | None:
        """Reverse lookup: find thread for a session_id. Returns (thread_id, info)."""
        for proj in self._forum_projects.values():
            for tid, info in proj.threads.items():
                if info.session_id == session_id:
                    return tid, info
        return None

    def _thread_to_project(self, thread_id: str) -> tuple[ForumProject, ThreadInfo] | None:
        """Find project + thread info for a thread_id."""
        for proj in self._forum_projects.values():
            info = proj.threads.get(thread_id)
            if info:
                return proj, info
        return None

    def _forum_by_channel_id(self, forum_id: str) -> ForumProject | None:
        """Find project by forum channel ID."""
        for proj in self._forum_projects.values():
            if proj.forum_channel_id == forum_id:
                return proj
        return None

    def _is_user_forum(self, forum_id: str) -> tuple[str, str] | None:
        """Check if a forum is a user's personal forum. Returns (user_id, user_name) or None."""
        cfg = load_access_config()
        for uid, ua in cfg.users.items():
            if ua.forum_channel_id == forum_id:
                return uid, ua.display_name
        return None

    def _user_forum_thread_to_repo(self, thread: discord.Thread) -> str | None:
        """Resolve repo name from a thread's tags in a user's personal forum."""
        repos = self._store.list_repos()
        for tag in thread.applied_tags:
            if tag.name in repos:
                return tag.name
        return None

    # --- User Forum Provisioning ---

    async def _ensure_user_forum(
        self, user_id: int, display_name: str, repo_names: list[str],
    ) -> discord.ForumChannel | None:
        """Create or get a personal forum channel for a granted user."""
        guild = self.get_guild(self._guild_id)
        if not guild or not self._category_id:
            return None
        category = guild.get_channel(self._category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            return None

        forum = await channels.ensure_user_forum(
            guild, category, guild.me, user_id,
            display_name, repo_names,
            owner_id=self._discord_user_id,
        )

        # Create welcome post if not already done
        if forum:
            cfg = load_access_config()
            ua = cfg.users.get(str(user_id))
            if ua and not ua.welcome_posted:
                try:
                    await channels.create_user_welcome_post(forum, display_name, repo_names)
                    ua.welcome_posted = True
                    access_mod.save_access_config(cfg)
                except Exception:
                    log.warning("Failed to create welcome post in forum %s for user %s, will retry next startup",
                                forum.id, user_id, exc_info=True)
            # Create control room post (awaited to populate skip-set before messages arrive).
            # Pass forum directly — forum_channel_id may not be persisted yet during /grant.
            try:
                await self._ensure_user_control_post(str(user_id), forum)
            except Exception:
                log.warning("Failed to create control room in forum %s for user %s",
                            forum.id, user_id, exc_info=True)

        return forum

    async def _sync_user_forum_tags(self, user_id: str) -> None:
        """Sync a user's forum tags to match their current access grants."""
        cfg = load_access_config()
        ua = cfg.users.get(user_id)
        if not ua or not ua.forum_channel_id:
            return
        guild = self.get_guild(self._guild_id)
        if not guild:
            return
        forum = guild.get_channel(int(ua.forum_channel_id))
        if not forum or not isinstance(forum, discord.ForumChannel):
            return
        repo_names = list(ua.repos.keys())
        await channels.sync_user_forum_tags(forum, repo_names)

    # --- Forum Provisioning ---

    async def _get_or_create_forum(self, repo_name: str) -> discord.ForumChannel | None:
        """Get or create a forum channel for a repo."""
        guild = self.get_guild(self._guild_id)
        if not guild or not self._category_id:
            return None
        category = guild.get_channel(self._category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            return None

        async with self._forum_lock:
            # Double-check after acquiring lock
            if repo_name in self._forum_projects:
                existing = self._forum_projects[repo_name]
                if existing.forum_channel_id:
                    forum = guild.get_channel(int(existing.forum_channel_id))
                    if forum and isinstance(forum, discord.ForumChannel):
                        return forum
                    # Forum was deleted externally — recreate
                    log.warning("Forum %s for repo %s was deleted, recreating", existing.forum_channel_id, repo_name)

            forum = await channels.ensure_forum(guild, category, repo_name)
            if repo_name not in self._forum_projects:
                self._forum_projects[repo_name] = ForumProject(
                    repo_name=repo_name,
                    forum_channel_id=str(forum.id),
                )
            else:
                self._forum_projects[repo_name].forum_channel_id = str(forum.id)
            self._save_forum_map()
            return forum

    async def _get_or_create_session_thread(
        self, repo_name: str, session_id: str | None, topic: str,
        origin: str = "bot",
        forum_channel_id: str | None = None,
        user_id: str | None = None,
        user_name: str | None = None,
    ) -> discord.Thread | None:
        """Find existing thread for session, or create a new one in the repo's forum.

        If forum_channel_id is provided, create the thread in that forum (personal
        user forum) instead of the repo's default forum.
        """
        # Check if session already has a thread
        if session_id:
            result = self._session_to_thread(session_id)
            if result:
                tid, info = result
                ch = self.get_channel(int(tid))
                if ch and isinstance(ch, discord.Thread):
                    return ch
                # Try fetch (archived thread)
                try:
                    ch = await self.fetch_channel(int(tid))
                    if isinstance(ch, discord.Thread):
                        return ch
                except (discord.NotFound, discord.Forbidden):
                    pass
                # Thread gone — remove stale mapping
                for proj in self._forum_projects.values():
                    proj.threads.pop(tid, None)

        # Resolve the target forum
        if forum_channel_id:
            guild = self.get_guild(self._guild_id)
            forum = guild.get_channel(int(forum_channel_id)) if guild else None
            if not forum or not isinstance(forum, discord.ForumChannel):
                log.warning("Personal forum %s not found, falling back to repo forum", forum_channel_id)
                forum = await self._get_or_create_forum(repo_name)
        else:
            forum = await self._get_or_create_forum(repo_name)
        if not forum:
            return None

        async with self._thread_lock:
            # Double-check after lock (another message may have created it)
            if session_id:
                result = self._session_to_thread(session_id)
                if result:
                    tid, _ = result
                    try:
                        ch = await self.fetch_channel(int(tid))
                        if isinstance(ch, discord.Thread):
                            return ch
                    except (discord.NotFound, discord.Forbidden):
                        pass

            thread_name = channels.build_channel_name(topic) if topic != "new-session" else "new-session"
            thread, _msg = await channels.create_forum_post(
                forum, thread_name, origin=origin,
                topic_preview=topic,
                current_mode=self._store.mode,
            )

            # Apply repo tag in personal forums
            if forum_channel_id:
                tag_map = {t.name: t for t in forum.available_tags}
                repo_tag = tag_map.get(repo_name)
                if repo_tag:
                    try:
                        await thread.edit(applied_tags=[repo_tag])
                    except Exception:
                        log.warning("Failed to apply repo tag %s to thread %s", repo_name, thread.id)

            # Store mapping
            proj = self._forum_projects.get(repo_name)
            if proj:
                proj.threads[str(thread.id)] = ThreadInfo(
                    thread_id=str(thread.id),
                    session_id=session_id,
                    origin=origin,
                    topic=topic,
                    user_id=user_id,
                    user_name=user_name,
                )
                self._save_forum_map()
            else:
                log.warning("No ForumProject for repo %s — thread %s created but unmapped", repo_name, thread.id)
            return thread

    async def _sync_single_thread(self, thread_id: str) -> None:
        """Refresh a single session thread: pull latest CLI messages."""
        lookup = self._thread_to_project(thread_id)
        if not lookup:
            return
        proj, info = lookup
        session_id = info.session_id
        repo_name = proj.repo_name
        if not session_id:
            return

        # Try to resolve the thread
        ch = self.get_channel(int(thread_id))
        if not ch:
            try:
                ch = await self.fetch_channel(int(thread_id))
            except (discord.NotFound, discord.Forbidden):
                return
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return

        # Check if there's a newer CLI session for this repo
        if repo_name and repo_name != "_default":
            repo_path = self._store.list_repos().get(repo_name, "")
            if repo_path:
                latest = await asyncio.to_thread(
                    sessions_mod.find_latest_session_for_repo, repo_path,
                )
                if latest and latest["id"] != session_id:
                    info.session_id = latest["id"]
                    info.origin = "cli"
                    info._synced_msg_count = 0
                    self._save_forum_map()
                    log.info("Single-sync updated thread %s to session %s", thread_id, latest["id"][:12])
                    session_id = latest["id"]

        await self._populate_thread_history(ch, session_id, thread_id)
        log.info("Single-sync refreshed thread %s", thread_id)

    async def _reconcile_forums(self) -> None:
        """Validate forum channels on startup. Clean stale mappings."""
        guild = self.get_guild(self._guild_id)
        if not guild or not self._category_id:
            return
        category = guild.get_channel(self._category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            return

        # Validate existing forum mappings
        valid_projects: dict[str, ForumProject] = {}
        for repo_name, proj in self._forum_projects.items():
            if not proj.forum_channel_id:
                valid_projects[repo_name] = proj
                continue
            forum = guild.get_channel(int(proj.forum_channel_id))
            if forum and isinstance(forum, discord.ForumChannel):
                # Threads are lazily validated on access — keep all mappings
                valid_projects[repo_name] = proj
            else:
                log.info("Removed stale forum mapping for repo %s (forum %s gone)", repo_name, proj.forum_channel_id)

        # Discover unmapped forums in category (skip user personal forums)
        user_forum_ids = set()
        cfg = load_access_config()
        for ua in cfg.users.values():
            if ua.forum_channel_id:
                user_forum_ids.add(ua.forum_channel_id)

        for ch in category.channels:
            if not isinstance(ch, discord.ForumChannel):
                continue
            if str(ch.id) in user_forum_ids:
                continue  # skip personal user forums
            existing = self._forum_by_channel_id(str(ch.id))
            if not existing:
                # Adopt as a project — repo name = forum name
                repo_name = ch.name
                if repo_name not in valid_projects:
                    valid_projects[repo_name] = ForumProject(
                        repo_name=repo_name,
                        forum_channel_id=str(ch.id),
                    )
                    log.info("Discovered unmapped forum %s (%s)", ch.id, ch.name)

        if valid_projects != self._forum_projects:
            self._forum_projects = valid_projects
            self._save_forum_map()

        # Clean up thread names and stale tags
        from bot.claude.types import InstanceStatus
        running_sessions = {
            i.session_id for i in self._store.list_instances()
            if i.status == InstanceStatus.RUNNING and i.session_id
        }
        for proj in valid_projects.values():
            if not proj.forum_channel_id:
                continue
            forum = guild.get_channel(int(proj.forum_channel_id))
            if not forum or not isinstance(forum, discord.ForumChannel):
                continue
            tag_map = {t.name: t for t in forum.available_tags}
            active_tag = tag_map.get("active")
            for thread in forum.threads:
                # Legacy migration: strip "repo│" prefix
                if "\u2502" in thread.name:  # │ (box-drawing vertical)
                    new_name = thread.name.split("\u2502", 1)[1].strip()
                    if new_name:
                        old_name = thread.name
                        try:
                            await thread.edit(name=new_name[:100])
                            log.info("Renamed thread %s: %s -> %s", thread.id, old_name, new_name)
                        except Exception:
                            log.debug("Failed to rename thread %s", thread.id, exc_info=True)
                # Legacy migration: strip old emoji prefixes (🔄, mode circles)
                _, topic = channels.parse_thread_name(thread.name)
                clean_name = channels.build_thread_name(topic)
                if clean_name != thread.name:
                    try:
                        await thread.edit(name=clean_name)
                        log.info("Stripped legacy emoji from thread: %s", thread.id)
                    except Exception:
                        log.debug("Failed to strip legacy emoji", exc_info=True)
                # Clear stale "active" tag (bot crashed mid-query)
                if active_tag and active_tag in thread.applied_tags:
                    # Check if this thread actually has a running session
                    info = proj.threads.get(str(thread.id))
                    if not info or info.session_id not in running_sessions:
                        new_tags = [t for t in thread.applied_tags if t != active_tag]
                        try:
                            await thread.edit(applied_tags=new_tags[:5])
                            log.info("Cleared stale active tag: %s", thread.id)
                        except Exception:
                            log.debug("Failed to clear stale active tag", exc_info=True)

        # Ensure control room posts exist for all repo forums
        for repo_name, proj in self._forum_projects.items():
            if proj.forum_channel_id and not proj.control_thread_id:
                try:
                    await self._ensure_control_post(repo_name)
                except Exception:
                    log.debug("Failed to create control room for %s", repo_name, exc_info=True)

        # Populate user control room thread ID set + create missing control rooms
        cfg = load_access_config()
        for uid, ua in cfg.users.items():
            if ua.control_thread_id:
                self._user_control_thread_ids.add(ua.control_thread_id)
            elif ua.forum_channel_id:
                try:
                    await self._ensure_user_control_post(uid)
                except Exception:
                    log.debug("Failed to create control room for user %s", uid, exc_info=True)

    async def _run_slash(
        self, interaction: discord.Interaction, coro,
        *, ephemeral: bool = False,
    ) -> None:
        """Defer, run engine command, then delete the 'thinking' response."""
        cmd_name = interaction.command.name if interaction.command else "?"
        log.info("Discord /%s in #%s by %s", cmd_name, getattr(interaction.channel, "name", "?"), interaction.user)
        await interaction.response.defer(ephemeral=ephemeral)
        channel_id = str(interaction.channel_id)
        lookup = self._thread_to_project(channel_id)
        info = lookup[1] if lookup else None
        # Compute access result for non-owner mode ceiling enforcement
        ar = self._check_access(interaction.user.id, channel_id=channel_id)
        ctx = self._ctx(channel_id, thread_info=info, access_result=ar)
        # Populate user identity
        ctx.user_id = str(interaction.user.id)
        ctx.user_name = interaction.user.display_name
        await coro(ctx)
        self._persist_ctx_settings(ctx)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

    def _setup_commands(self) -> None:
        """Register slash commands."""
        guild_obj = discord.Object(id=self._guild_id)

        @self.tree.command(name="status", description="Health dashboard", guild=guild_obj)
        async def cmd_status(interaction: discord.Interaction):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_status)

        @self.tree.command(name="cost", description="Spending breakdown", guild=guild_obj)
        async def cmd_cost(interaction: discord.Interaction):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_cost)

        @self.tree.command(name="list", description="Show instances", guild=guild_obj)
        @app_commands.describe(scope="Show all instances or just recent")
        async def cmd_list(interaction: discord.Interaction, scope: str = ""):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_list(ctx, scope))

        @self.tree.command(name="bg", description="Background task (build mode)", guild=guild_obj)
        @app_commands.describe(prompt="Task description")
        async def cmd_bg(interaction: discord.Interaction, prompt: str):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_bg(ctx, prompt))

        @self.tree.command(name="release", description="Cut a versioned release", guild=guild_obj)
        @app_commands.describe(level="patch, minor, major, or explicit version (default: patch)")
        async def cmd_release(interaction: discord.Interaction, level: str = "patch"):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_release(ctx, level))

        @self.tree.command(name="kill", description="Terminate instance", guild=guild_obj)
        @app_commands.describe(target="Instance ID or name")
        async def cmd_kill(interaction: discord.Interaction, target: str):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_kill(ctx, target))

        @self.tree.command(name="retry", description="Re-run instance", guild=guild_obj)
        @app_commands.describe(target="Instance ID or name")
        async def cmd_retry(interaction: discord.Interaction, target: str):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_retry(ctx, target))

        @self.tree.command(name="log", description="Full output", guild=guild_obj)
        @app_commands.describe(target="Instance ID or name")
        async def cmd_log(interaction: discord.Interaction, target: str):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_log(ctx, target))

        @self.tree.command(name="diff", description="Git diff", guild=guild_obj)
        @app_commands.describe(target="Instance ID or name")
        async def cmd_diff(interaction: discord.Interaction, target: str):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_diff(ctx, target))

        @self.tree.command(name="merge", description="Merge branch", guild=guild_obj)
        @app_commands.describe(target="Instance ID or name")
        async def cmd_merge(interaction: discord.Interaction, target: str):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_merge(ctx, target))

        @self.tree.command(name="discard", description="Delete branch", guild=guild_obj)
        @app_commands.describe(target="Instance ID or name")
        async def cmd_discard(interaction: discord.Interaction, target: str):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_discard(ctx, target))

        @self.tree.command(name="mode", description="View/set mode", guild=guild_obj)
        @app_commands.describe(mode="explore or build")
        async def cmd_mode(interaction: discord.Interaction, mode: str = ""):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_mode(ctx, mode))

        @self.tree.command(name="verbose", description="Progress detail level", guild=guild_obj)
        @app_commands.describe(level="0, 1, or 2")
        async def cmd_verbose(interaction: discord.Interaction, level: str = ""):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_verbose(ctx, level))

        @self.tree.command(name="context", description="Pinned context", guild=guild_obj)
        @app_commands.describe(args="set <text> | clear")
        async def cmd_context(interaction: discord.Interaction, args: str = ""):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_context(ctx, args))

        @self.tree.command(name="repo", description="Repo management", guild=guild_obj)
        @app_commands.describe(args="add|switch|list [name] [path]")
        async def cmd_repo(interaction: discord.Interaction, args: str = ""):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            # Discord enhancement: select menu for switch (no name) or bare /repo
            stripped = args.strip()
            repos = self._store.list_repos()
            if len(repos) >= 2 and stripped in ("", "switch"):
                active, _ = self._store.get_active_repo()
                select = discord.ui.Select(
                    placeholder="Switch repo...",
                    custom_id="repo_switch_select",
                    options=[
                        discord.SelectOption(
                            label=name,
                            description=path[:80],
                            value=name,
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
            await self._run_slash(interaction, lambda ctx: commands.on_repo(ctx, args))

        @self.tree.command(name="session", description="List/resume sessions", guild=guild_obj)
        @app_commands.describe(args="resume <id> | drop")
        async def cmd_session(interaction: discord.Interaction, args: str = ""):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_session(ctx, args))

        @self.tree.command(name="schedule", description="Recurring tasks", guild=guild_obj)
        @app_commands.describe(args="every|at|list|delete ...")
        async def cmd_schedule(interaction: discord.Interaction, args: str = ""):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_schedule(ctx, args))

        @self.tree.command(name="alias", description="Command shortcuts", guild=guild_obj)
        @app_commands.describe(args="set|delete|list ...")
        async def cmd_alias(interaction: discord.Interaction, args: str = ""):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_alias(ctx, args))

        @self.tree.command(name="budget", description="Budget info/reset", guild=guild_obj)
        @app_commands.describe(args="reset")
        async def cmd_budget(interaction: discord.Interaction, args: str = ""):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_budget(ctx, args))

        @self.tree.command(name="new", description="Start fresh conversation", guild=guild_obj)
        @app_commands.describe(
            repo="Repo name (default: active repo)",
            mode="Permission mode for the session",
        )
        @app_commands.choices(mode=[
            app_commands.Choice(name=name, value=key)
            for key, name in MODE_DISPLAY.items()
        ])
        async def cmd_new(interaction: discord.Interaction, repo: str = "", mode: str = ""):
            is_owner = self._is_owner(interaction.user.id)
            if not is_owner:
                access = self._check_access(interaction.user.id)
                if not access.allowed:
                    await interaction.response.send_message("Unauthorized", ephemeral=True)
                    return

            # Resolve user context for non-owners
            user_id = None if is_owner else str(interaction.user.id)
            user_name = None if is_owner else interaction.user.display_name

            # Apply mode if specified (will be set on new thread's ThreadInfo)
            mode = mode.strip().lower()
            new_thread_mode = mode if mode and mode in VALID_MODES else None

            # Determine available repos and enforce mode ceiling for non-owners
            if is_owner:
                available_repos = list(self._store.list_repos().keys())
            else:
                cfg = load_access_config()
                ua = cfg.users.get(str(interaction.user.id))
                if ua and ua.global_access:
                    available_repos = list(self._store.list_repos().keys())
                elif ua:
                    all_repos = self._store.list_repos()
                    available_repos = [r for r in ua.repos if r in all_repos]
                else:
                    available_repos = []

                # Enforce mode ceiling
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
                await self._create_new_session(
                    interaction, repo_name, mode=new_thread_mode,
                    user_id=user_id, user_name=user_name,
                )
            else:
                if len(available_repos) == 0:
                    await interaction.response.send_message(
                        "No repos available.", ephemeral=True,
                    )
                elif len(available_repos) == 1:
                    await interaction.response.defer(ephemeral=True)
                    await self._create_new_session(
                        interaction, available_repos[0], mode=new_thread_mode,
                        user_id=user_id, user_name=user_name,
                    )
                else:
                    view = discord.ui.View(timeout=60)
                    for name in available_repos:
                        btn = discord.ui.Button(
                            label=name,
                            style=discord.ButtonStyle.primary,
                            custom_id=f"new_repo:{name}",
                        )
                        view.add_item(btn)
                    await interaction.response.send_message(
                        "Pick a repo:", view=view, ephemeral=True,
                    )

        @self.tree.command(name="sync", description="Sync sessions from CLI", guild=guild_obj)
        @app_commands.describe(count="Number of sessions to sync (0 = this thread only)")
        async def cmd_sync(interaction: discord.Interaction, count: int = 0):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)

            thread_id = str(interaction.channel_id)

            # count=0: refresh this thread if in a session thread, else sync 5
            if count == 0:
                lookup = self._thread_to_project(thread_id)
                if lookup:
                    await self._sync_single_thread(thread_id)
                    await interaction.followup.send("Thread synced.", ephemeral=True)
                    return
                count = 5

            log.info("Discord /sync count=%d by %s", count, interaction.user)
            created, populated = await self._sync_cli_sessions(count)
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

        @self.tree.command(name="sync-channel", description="Refresh this thread's session history", guild=guild_obj)
        async def cmd_sync_channel(interaction: discord.Interaction):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            thread_id = str(interaction.channel_id)
            lookup = self._thread_to_project(thread_id)
            if not lookup:
                await interaction.response.send_message(
                    "This isn't a session thread.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            proj, info = lookup
            session_id = info.session_id
            if not session_id:
                await interaction.followup.send("No session mapped to this thread.", ephemeral=True)
                return
            repo_name = proj.repo_name
            # Check for newer CLI session
            repo_path = self._store.list_repos().get(repo_name, "") if repo_name and repo_name != "_default" else ""
            if repo_path:
                latest = await asyncio.to_thread(
                    sessions_mod.find_latest_session_for_repo, repo_path,
                )
                if latest and latest["id"] != session_id:
                    old_short = session_id[:12]
                    session_id = latest["id"]
                    info.session_id = session_id
                    info.origin = "cli"
                    info._synced_msg_count = 0
                    self._save_forum_map()
                    log.info("sync-channel updated thread %s session %s -> %s",
                             thread_id, old_short, session_id[:12])
            # Populate history
            ch = interaction.channel
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                await self._populate_thread_history(ch, session_id, thread_id)
            await interaction.followup.send(
                f"Synced session `{session_id[:12]}…`", ephemeral=True)

        @self.tree.command(name="help", description="Show available commands", guild=guild_obj)
        async def cmd_help(interaction: discord.Interaction):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_help)

        @self.tree.command(name="clear", description="Archive old instances", guild=guild_obj)
        async def cmd_clear(interaction: discord.Interaction):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_clear)

        @self.tree.command(name="logs", description="Bot log", guild=guild_obj)
        async def cmd_logs(interaction: discord.Interaction):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_logs)

        @self.tree.command(name="shutdown", description="Stop the bot", guild=guild_obj)
        async def cmd_shutdown(interaction: discord.Interaction):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_shutdown)

        @self.tree.command(name="reboot", description="Restart the bot (apply code changes)", guild=guild_obj)
        async def cmd_reboot(interaction: discord.Interaction):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_reboot)

        # --- /ref: reference another thread's context ---

        async def thread_autocomplete(
            interaction: discord.Interaction, current: str,
        ) -> list[app_commands.Choice[str]]:
            choices: list[tuple[int, str, str]] = []
            current_tid = str(interaction.channel_id)
            for proj in self._forum_projects.values():
                for tid, info in proj.threads.items():
                    if tid == current_tid or not info.session_id:
                        continue
                    topic = info.topic or f"Thread #{tid[-6:]}"
                    age = _snowflake_age(int(tid))
                    label = f"[{proj.repo_name}] {topic}"[:85] + f" ({age})"
                    if current.lower() in label.lower():
                        choices.append((int(tid), label[:100], tid))
            choices.sort(key=lambda x: x[0], reverse=True)  # newest first
            return [app_commands.Choice(name=c[1], value=c[2]) for c in choices[:25]]

        @self.tree.command(name="ref", description="Reference another thread's context", guild=guild_obj)
        @app_commands.describe(thread="Thread to reference", messages="Messages to include (default 6)")
        @app_commands.autocomplete(thread=thread_autocomplete)
        async def cmd_ref(interaction: discord.Interaction, thread: str, messages: int = 6):
            if not self._is_owner(interaction.user.id) and not self._check_access(interaction.user.id, channel_id=str(interaction.channel_id)).allowed:
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            messages = max(1, min(messages, 20))
            lookup = self._thread_to_project(thread)
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

            embed = self._build_ref_embed(proj, info, msgs, thread)

            # Store context for prompt injection (only in forum threads)
            channel_id = str(interaction.channel_id)
            in_forum_thread = (
                isinstance(interaction.channel, discord.Thread)
                and isinstance(getattr(interaction.channel, "parent", None), discord.ForumChannel)
            )
            if in_forum_thread:
                context = self._build_ref_context(proj, info, msgs, thread)
                self._pending_refs[channel_id] = (context, _time.monotonic())
                await interaction.followup.send(
                    embed=embed,
                    content="Context loaded \u2014 your next message will include this reference.",
                )
            else:
                await interaction.followup.send(embed=embed)

        # --- Monitor command group ---
        monitor_group = app_commands.Group(
            name="monitor", description="Live app monitoring dashboards", guild_ids=[self._guild_id],
        )

        @monitor_group.command(name="setup", description="Enable a monitor (reads config from .env)")
        @app_commands.describe(name="Monitor name (e.g. aiagent)")
        async def cmd_monitor_setup(interaction: discord.Interaction, name: str):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            result = await self._monitor_setup(name.lower())
            await interaction.followup.send(result, ephemeral=True)

        @monitor_group.command(name="refresh", description="Fetch & update all monitors now")
        async def cmd_monitor_refresh(interaction: discord.Interaction):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            if not self._monitor_service:
                await interaction.followup.send("Monitor service not initialized.", ephemeral=True)
                return
            count = await self._monitor_service.refresh_all_now()
            await interaction.followup.send(f"Refreshed {count} monitor(s).", ephemeral=True)

        @monitor_group.command(name="remove", description="Disable a monitor (keeps channel)")
        @app_commands.describe(name="Monitor name")
        async def cmd_monitor_remove(interaction: discord.Interaction, name: str):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            if not self._monitor_service:
                await interaction.followup.send("Monitor service not initialized.", ephemeral=True)
                return
            ok = await self._monitor_service.remove_monitor(name.lower())
            if ok:
                await interaction.followup.send(f"Monitor **{name}** disabled.", ephemeral=True)
            else:
                await interaction.followup.send(f"Monitor **{name}** not found.", ephemeral=True)

        @monitor_group.command(name="list", description="Show all monitors with status")
        async def cmd_monitor_list(interaction: discord.Interaction):
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            if not self._monitor_service:
                await interaction.followup.send("Monitor service not initialized.", ephemeral=True)
                return
            monitors = self._monitor_service.list_monitors()
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

        self.tree.add_command(monitor_group)

        # --- /access command group (owner-only) ---
        access_group = app_commands.Group(
            name="access", description="Manage user access to repos",
            guild_ids=[self._guild_id],
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
            if not self._is_owner(interaction.user.id):
                await interaction.response.send_message("Owner only", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)

            # Validate repo exists
            repos = self._store.list_repos()
            if repo not in repos:
                lower_map = {k.lower(): k for k in repos}
                repo = lower_map.get(repo.lower(), repo)
            if repo not in repos:
                await interaction.followup.send(f"Repo `{repo}` not found.", ephemeral=True)
                return

            cfg = load_access_config()
            uid = str(user.id)

            # Create or update user access
            if uid not in cfg.users:
                cfg.users[uid] = access_mod.UserAccess(
                    user_id=uid,
                    display_name=user.display_name,
                )
            ua = cfg.users[uid]
            ua.display_name = user.display_name
            ua.repos[repo] = access_mod.RepoAccess(mode=mode)

            # Create/update personal forum
            repo_names = list(ua.repos.keys())
            forum = await self._ensure_user_forum(user.id, user.display_name, repo_names)
            if forum:
                ua.forum_channel_id = str(forum.id)

            access_mod.save_access_config(cfg)

            await interaction.followup.send(
                f"Granted **{user.display_name}** access to `{repo}` "
                f"(mode: {mode})"
                + (f" in <#{forum.id}>" if forum else ""),
                ephemeral=True,
            )

        @access_group.command(name="revoke", description="Revoke user access")
        @app_commands.describe(user="User to revoke", repo="Repo name (omit for all)")
        async def cmd_access_revoke(
            interaction: discord.Interaction, user: discord.Member,
            repo: str = "",
        ):
            if not self._is_owner(interaction.user.id):
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

            # Sync forum tags
            if ua.forum_channel_id:
                if ua.repos:
                    await self._sync_user_forum_tags(uid)
                else:
                    # Archive the forum if no grants left
                    guild = self.get_guild(self._guild_id)
                    if guild:
                        forum = guild.get_channel(int(ua.forum_channel_id))
                        if forum and isinstance(forum, discord.ForumChannel):
                            # Can't archive forums directly, just remove permissions
                            try:
                                await forum.set_permissions(
                                    guild.get_member(user.id),
                                    overwrite=None,
                                )
                            except Exception:
                                pass
                    msg += " Forum permissions removed."

            # Clean up empty users
            if not ua.repos and not ua.global_access:
                del cfg.users[uid]

            access_mod.save_access_config(cfg)
            await interaction.followup.send(msg, ephemeral=True)

        @access_group.command(name="list", description="Show all access grants")
        async def cmd_access_list(interaction: discord.Interaction):
            if not self._is_owner(interaction.user.id):
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
                    for repo, grant in ua.repos.items():
                        lines.append(
                            f"**{ua.display_name}** — `{repo}` "
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
            if not self._is_owner(interaction.user.id):
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

        self.tree.add_command(access_group)

    async def _monitor_setup(self, name: str) -> str:
        """Set up a monitor from env config."""
        from bot.monitor.service import MonitorConfig, _load_monitor_configs

        configs = _load_monitor_configs()
        if name not in configs:
            return (
                f"No config found for **{name}**. "
                f"Set `MONITOR_{name.upper()}_URL` and `MONITOR_{name.upper()}_AUTH` in .env"
            )

        if not self._monitor_service:
            self._init_monitor_service()

        guild = self.get_guild(self._guild_id)
        if not guild or not self._category_id:
            return "Guild or category not available."
        category = guild.get_channel(self._category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            return "Category channel not found."

        cfg = configs[name]
        channel = await self._monitor_service.setup_monitor(cfg, category)

        if not self._monitor_started:
            self._monitor_service.start()
            self._monitor_started = True

        return f"Monitor **{name}** enabled \u2192 <#{channel.id}>"

    def _init_monitor_service(self) -> None:
        """Initialize the monitor service (lazy)."""
        from bot.monitor.service import MonitorService

        self._monitor_service = MonitorService(
            bot=self,
            store=self._store,
            guild_id=self._guild_id,
            category_id=self._category_id,
            notifier=self._notifier,
        )

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
                    guild,
                    self._category_name,
                    guild.me,
                    owner_id=self._discord_user_id,
                )
                self._category_id = category.id
                lobby = await channels.ensure_lobby(category)
                self._lobby_channel_id = lobby.id
                self._messenger = None
                log.info(
                    "Auto-provisioned category=%s lobby=%s",
                    category.id, lobby.id,
                )

        # Load and reconcile forum mapping
        self._load_forum_map()
        await self._reconcile_forums()

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

    def _in_scope(self, guild: discord.Guild | None, channel: discord.abc.GuildChannel | None = None) -> bool:
        """Check guild + channel is within our category."""
        if not guild or guild.id != self._guild_id:
            return False
        if channel and self._category_id:
            if isinstance(channel, discord.Thread):
                # Thread → check parent channel's category
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

    async def on_message(self, message: discord.Message) -> None:
        """Handle plain text messages in channels."""
        # Ignore own messages
        if message.author == self.user:
            return

        # Detect test webhook
        _is_test_webhook = (
            config.TEST_WEBHOOK_IDS
            and message.webhook_id
            and str(message.webhook_id) in config.TEST_WEBHOOK_IDS
        )

        # Ignore bots (except test webhook)
        if message.author.bot and not _is_test_webhook:
            return

        # Only respond inside our category
        if not self._in_scope(message.guild, message.channel):
            return

        # Auth check — skip for test webhook
        if not _is_test_webhook:
            msg_access = self._check_access(
                message.author.id, channel_id=str(message.channel.id),
            )
            if not msg_access.allowed:
                return
        else:
            msg_access = AccessResult(allowed=True, is_owner=True)

        text = message.content.strip()
        _temp_files: list[str] = []  # track temp files for cleanup

        # Handle file attachments
        IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        AUDIO_EXTS = {".ogg", ".mp3", ".wav", ".m4a", ".webm"}
        for att in message.attachments:
            if not att.filename:
                continue
            ext = Path(att.filename).suffix.lower()

            # Voice messages / audio — transcribe and confirm before running
            if ext in AUDIO_EXTS and self._voice_enabled and att.size <= 25_000_000:
                await self._handle_voice_attachment(message, att, msg_access)
                return  # voice flow handles everything via confirmation buttons

            # Text files (Discord sends large pastes as .txt)
            if ext == ".txt" and att.size <= 500_000:
                try:
                    file_bytes = await att.read()
                    file_text = file_bytes.decode("utf-8", errors="replace")
                    if text:
                        text = f"{text}\n\n{file_text}"
                    else:
                        text = file_text
                    log.info("Read .txt attachment %s (%d bytes)", att.filename, att.size)
                except Exception:
                    log.warning("Failed to read attachment %s", att.filename, exc_info=True)

            # Images — save to temp file, Claude reads from path
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
                    return  # non-owners can't use lobby
                active_repo, _ = self._store.get_active_repo()
                await self._route_lobby_message(message, text, active_repo)
                return

            # --- Forum thread: auto-resume session ---
            if isinstance(message.channel, discord.Thread):
                parent = message.channel.parent
                if parent and isinstance(parent, discord.ForumChannel):
                    # Skip control room threads (not session threads)
                    if any(p.control_thread_id == channel_id for p in self._forum_projects.values()):
                        return
                    if channel_id in self._user_control_thread_ids:
                        return
                    lookup = self._thread_to_project(channel_id)
                    if not lookup:
                        # Adopt unmapped thread in a known forum (owner forums)
                        proj = self._forum_by_channel_id(str(parent.id))
                        if proj:
                            log.info("Adopted unmapped thread %s in forum %s", channel_id, parent.name)
                            info = ThreadInfo(thread_id=channel_id, origin="bot")
                            proj.threads[channel_id] = info
                            self._save_forum_map()
                            lookup = (proj, info)

                    # Check if this is a user's personal forum
                    if not lookup:
                        user_forum_info = self._is_user_forum(str(parent.id))
                        if user_forum_info:
                            # Resolve repo from thread tags
                            repo_name = self._user_forum_thread_to_repo(message.channel)
                            if repo_name and repo_name in self._forum_projects:
                                proj = self._forum_projects[repo_name]
                                info = ThreadInfo(
                                    thread_id=channel_id, origin="bot",
                                    user_id=user_forum_info[0],
                                    user_name=user_forum_info[1],
                                )
                                proj.threads[channel_id] = info
                                self._save_forum_map()
                                lookup = (proj, info)
                                log.info("Adopted user forum thread %s repo=%s user=%s",
                                         channel_id, repo_name, user_forum_info[1])
                            elif not repo_name:
                                # No repo tag — try to auto-select if user has exactly one repo
                                uid = user_forum_info[0]
                                cfg = load_access_config()
                                ua = cfg.users.get(uid)
                                user_repos = [r for r in (ua.repos if ua else {}) if r in self._forum_projects]
                                if len(user_repos) == 1:
                                    repo_name = user_repos[0]
                                    proj = self._forum_projects[repo_name]
                                    info = ThreadInfo(
                                        thread_id=channel_id, origin="bot",
                                        user_id=user_forum_info[0],
                                        user_name=user_forum_info[1],
                                    )
                                    proj.threads[channel_id] = info
                                    self._save_forum_map()
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
                        session_id = info.session_id or None
                        repo_name = proj.repo_name if proj.repo_name != "_default" else None
                        origin = info.origin

                        # CLI → Discord transition
                        if session_id and origin == "cli":
                            log.info("Thread %s resuming CLI session %s — transitioning to bot ownership",
                                     ch_name, session_id[:12])
                            info.origin = "bot"
                            self._save_forum_map()

                        was_pending = not session_id

                        # Inject pending /ref context
                        ref = self._pending_refs.pop(channel_id, None)
                        if ref:
                            ref_text, ref_time = ref
                            if (_time.monotonic() - ref_time) < 600:  # 10 min expiry
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
                        self._attach_session_callbacks(ctx, info, channel_id)
                        try:
                            await commands.on_text(ctx, text)
                        finally:
                            self._persist_ctx_settings(ctx)
                            if was_pending:
                                await self._finalize_pending_thread(channel_id, message.channel, text)
                            elif "new-session" in message.channel.name:
                                summary = self._get_latest_summary(channel_id)
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
            # Clean up temp image files
            for tmp in _temp_files:
                Path(tmp).unlink(missing_ok=True)

    async def _handle_voice_attachment(
        self, message: discord.Message, att: discord.Attachment,
        msg_access: AccessResult,
    ) -> None:
        """Download a voice attachment, transcribe it, and show confirmation buttons."""
        try:
            file_bytes = await att.read()
            log.info("Downloaded voice attachment %s (%d bytes)", att.filename, att.size)

            from bot.services.audio import transcribe
            transcription = await transcribe(file_bytes, filename=att.filename)

            if not transcription or not transcription.strip():
                await message.channel.send("Couldn't detect any speech in that voice message.")
                return

            # Build confirmation embed with buttons
            embed = discord.Embed(
                description=transcription,
                color=discord.Color.blurple(),
            )
            embed.set_author(name="Voice Transcription")
            embed.set_footer(text="Send to run as a query, or Cancel to discard.")

            view = discord.ui.View(timeout=120)
            send_btn = discord.ui.Button(
                label="Send", style=discord.ButtonStyle.green,
                custom_id=f"voice_send:{message.author.id}",
            )
            cancel_btn = discord.ui.Button(
                label="Cancel", style=discord.ButtonStyle.secondary,
                custom_id=f"voice_cancel:{message.author.id}",
            )
            view.add_item(send_btn)
            view.add_item(cancel_btn)

            confirm_msg = await message.channel.send(embed=embed, view=view)

            # Store transcription keyed by confirmation message ID
            self._pending_voice[str(confirm_msg.id)] = {
                "transcription": transcription,
                "author_id": str(message.author.id),
                "_ts": _time.monotonic(),
            }
            # Expire stale entries older than 5 minutes
            now = _time.monotonic()
            self._pending_voice = {
                k: v for k, v in self._pending_voice.items()
                if now - v.get("_ts", now) < 300
            }
            log.info("Voice transcription pending confirmation in #%s: %s",
                      getattr(message.channel, "name", "?"), transcription[:80])

        except Exception:
            log.warning("Voice transcription failed for %s", att.filename, exc_info=True)
            try:
                await message.channel.send("Couldn't transcribe that voice message.")
            except Exception:
                pass

    async def _route_lobby_message(
        self, message: discord.Message, text: str, repo_name: str | None,
    ) -> None:
        """Route a lobby message to a forum thread."""
        repo_name = repo_name or "_default"
        # Ensure control room exists (fire-and-forget, idempotent)
        asyncio.create_task(self._ensure_control_post(repo_name))
        thread = await self._get_or_create_session_thread(repo_name, None, text)
        if thread:
            # Delete original from lobby
            try:
                await message.delete()
            except Exception:
                pass
            # Post redirect
            asyncio.create_task(self._send_redirect(thread))
            # Run query in new thread
            tid = str(thread.id)
            self._cancel_sleep(tid)
            asyncio.create_task(self._clear_thread_sleeping(thread))
            asyncio.create_task(self._set_thread_active_tag(thread, True))
            asyncio.create_task(self._refresh_dashboard())
            lookup = self._thread_to_project(tid)
            t_info = lookup[1] if lookup else None
            ctx = self._ctx(tid, repo_name=repo_name if repo_name != "_default" else None,
                            thread_info=t_info)
            if t_info:
                self._attach_session_callbacks(ctx, t_info, tid)
            try:
                await commands.on_text(ctx, text)
            finally:
                self._persist_ctx_settings(ctx)
                # Update mapping with real session_id
                await self._update_pending_thread(tid)
                # Generate smart title (fire-and-forget)
                summary = self._get_latest_summary(tid)
                asyncio.create_task(self._generate_smart_title(thread, text, summary))
                # Apply completion tags (also clears "active") + refresh dashboard
                asyncio.create_task(self._try_apply_tags_after_run(tid))
                self._schedule_sleep(tid)
                asyncio.create_task(self._refresh_dashboard())

    async def _apply_thread_tags(self, thread: discord.Thread, status: str, origin: str = "bot", mode: str | None = None) -> None:
        """Apply forum tags to a thread based on status + mode. Fire-and-forget safe."""
        try:
            if not isinstance(thread.parent, discord.ForumChannel):
                return
            forum = thread.parent
            tag_map = {t.name: t for t in forum.available_tags}
            if not tag_map:
                tag_map = await channels.ensure_forum_tags(forum)

            desired_tags = []
            if status == "completed" and "completed" in tag_map:
                desired_tags.append(tag_map["completed"])
            elif status == "failed" and "failed" in tag_map:
                desired_tags.append(tag_map["failed"])
            elif status == "running" and "active" in tag_map:
                desired_tags.append(tag_map["active"])

            if origin == "cli" and "cli" in tag_map:
                desired_tags.append(tag_map["cli"])

            if mode and mode in tag_map:
                desired_tags.append(tag_map[mode])

            if desired_tags:
                await thread.edit(applied_tags=desired_tags[:5])
        except Exception:
            log.debug("Failed to apply tags to thread %s", thread.id, exc_info=True)

    async def _try_apply_tags_after_run(self, channel_id: str) -> None:
        """Check latest instance status and apply tags to the thread.

        Always clears the 'active' tag — either by replacing with completion
        tags, or as a standalone fallback if no instance is found.
        """
        ch = self.get_channel(int(channel_id))
        if not ch or not isinstance(ch, discord.Thread):
            return
        lookup = self._thread_to_project(channel_id)
        if not lookup:
            return
        _, info = lookup
        # Find the most recent instance for this session
        for inst in self._store.list_instances()[:5]:
            if inst.session_id and inst.session_id == info.session_id:
                await self._apply_thread_tags(ch, inst.status.value, info.origin, mode=inst.mode)
                return
        # No matching instance — still clear "active" tag as fallback
        await self._set_thread_active_tag(ch, False)

    # --- CLI Session Sync ---

    async def _sync_cli_sessions(self, count: int) -> tuple[list[discord.Thread], list]:
        """Scan CLI sessions and create/populate forum threads.

        Returns (created_threads, populated_channels).
        """
        count = max(1, min(count, 15))
        raw_sessions = await asyncio.to_thread(
            sessions_mod.scan_sessions, count * 3, self._store.list_repos(),
        )
        seen_projects: set[str] = set()
        session_list = []
        for s in raw_sessions:
            proj = s["project"]
            if proj not in seen_projects:
                seen_projects.add(proj)
                session_list.append(s)
            if len(session_list) >= count:
                break

        created: list[discord.Thread] = []
        populated: list = []
        updated_threads: set[str] = set()
        for s in session_list:
            session_id = s["id"]
            repo_name = s.get("project") or "_default"

            existing = self._session_to_thread(session_id)
            if existing:
                tid, info = existing
                if tid in updated_threads:
                    continue
                updated_threads.add(tid)
                ch = self.get_channel(int(tid))
                if not ch:
                    try:
                        ch = await self.fetch_channel(int(tid))
                    except (discord.NotFound, discord.Forbidden):
                        ch = None
                if ch and isinstance(ch, (discord.TextChannel, discord.Thread)):
                    await self._populate_thread_history(ch, session_id, tid)
                    populated.append(ch)
                continue

            log.info("Sync creating thread for session %s repo=%s", session_id[:12], repo_name)
            thread = await self._get_or_create_session_thread(
                repo_name, session_id, s["topic"], origin="cli",
            )
            if thread:
                created.append(thread)
                await self._populate_thread_history(thread, session_id, str(thread.id))
                asyncio.create_task(self._generate_smart_title(thread, s["topic"]))

        return created, populated

    # --- Repo Control Room ---

    async def _get_repo_branch(self, repo_path: str) -> str | None:
        """Get the current git branch for a repo path (non-blocking)."""
        if not repo_path:
            return None
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=repo_path, capture_output=True, text=True, **_NOWND,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    async def _ensure_control_post(self, repo_name: str) -> None:
        """Ensure a control room post exists in the repo's forum.

        Called after _get_or_create_forum() returns, outside the forum lock.
        Idempotent — checks control_thread_id before creating.
        """
        proj = self._forum_projects.get(repo_name)
        if not proj or proj.control_thread_id:
            return

        forum = self.get_channel(int(proj.forum_channel_id)) if proj.forum_channel_id else None
        if not forum or not isinstance(forum, discord.ForumChannel):
            return

        repos = self._store.list_repos()
        repo_path = repos.get(repo_name, "")
        branch = await self._get_repo_branch(repo_path)

        thread, msg = await channels.create_repo_control_post(
            forum, repo_name, repo_path, branch, self._store.mode,
        )
        proj.control_thread_id = str(thread.id)
        proj.control_message_id = str(msg.id)
        self._save_forum_map()
        log.info("Created control room for repo %s (thread=%s)", repo_name, thread.id)

    async def _ensure_user_control_post(
        self, user_id: str,
        forum: discord.ForumChannel | None = None,
    ) -> None:
        """Ensure a control room post exists in a user's personal forum.

        If *forum* is passed, use it directly (avoids stale config when
        forum_channel_id hasn't been persisted yet, e.g. during /grant).
        """
        cfg = load_access_config()
        ua = cfg.users.get(user_id)
        if not ua or ua.control_thread_id:
            return

        if not forum:
            if not ua.forum_channel_id:
                return
            forum = self.get_channel(int(ua.forum_channel_id))
        if not forum or not isinstance(forum, discord.ForumChannel):
            return

        repo_names = list(ua.repos.keys())
        mode = "explore"
        if repo_names and repo_names[0] in ua.repos:
            mode = ua.repos[repo_names[0]].mode

        thread, msg = await channels.create_user_control_post(
            forum, ua.display_name, repo_names, mode,
        )
        ua.control_thread_id = str(thread.id)
        ua.control_message_id = str(msg.id)
        access_mod.save_access_config(cfg)
        self._user_control_thread_ids.add(str(thread.id))
        log.info("Created control room for user %s (thread=%s)", ua.display_name, thread.id)

    async def _refresh_control_room(self, repo_name: str) -> None:
        """Update the control room embed for a repo forum."""
        proj = self._forum_projects.get(repo_name)
        if not proj or not proj.control_thread_id or not proj.control_message_id:
            return
        thread = None
        try:
            try:
                thread = await self.fetch_channel(int(proj.control_thread_id))
            except discord.NotFound:
                thread = None
            if not thread or not isinstance(thread, discord.Thread):
                log.info("Control room thread for %s was deleted, clearing stale IDs", repo_name)
                proj.control_thread_id = None
                proj.control_message_id = None
                self._save_forum_map()
                return
            # Migrate old "Control Center" name to "Control Room"
            if thread.name == "Control Center":
                try:
                    await thread.edit(name="Control Room")
                    log.info("Renamed Control Center -> Control Room (thread=%s)", thread.id)
                except Exception:
                    pass
            msg = await thread.fetch_message(int(proj.control_message_id))

            from bot.claude.types import InstanceStatus
            instances = self._store.list_instances()
            active = sum(1 for i in instances if i.repo_name == repo_name
                         and i.status == InstanceStatus.RUNNING)
            completed = sum(1 for i in instances if i.repo_name == repo_name
                            and i.status == InstanceStatus.COMPLETED)
            failed = sum(1 for i in instances if i.repo_name == repo_name
                         and i.status == InstanceStatus.FAILED)

            repos = self._store.list_repos()
            repo_path = repos.get(repo_name, "")
            branch = await self._get_repo_branch(repo_path)

            embed = channels.build_control_embed(
                repo_name, repo_path, branch, self._store.mode,
                active, completed, failed,
            )
            view = channels.build_control_view(repo_name)
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            log.info("Control room message for %s was deleted, recreating", repo_name)
            proj.control_thread_id = None
            proj.control_message_id = None
            self._save_forum_map()
            # Delete orphan thread if it still exists
            try:
                if thread and isinstance(thread, discord.Thread):
                    await thread.delete()
            except Exception:
                pass
            # Recreate immediately
            try:
                await self._ensure_control_post(repo_name)
            except Exception:
                log.debug("Failed to recreate control room for %s", repo_name, exc_info=True)
        except Exception:
            log.debug("Failed to refresh control room for %s", repo_name, exc_info=True)

    async def _refresh_user_control_room(self, user_id: str) -> None:
        """Update the control room embed for a user's personal forum."""
        cfg = load_access_config()
        ua = cfg.users.get(user_id)
        if not ua or not ua.control_thread_id or not ua.control_message_id:
            return
        thread = None
        try:
            try:
                thread = await self.fetch_channel(int(ua.control_thread_id))
            except discord.NotFound:
                thread = None
            if not thread or not isinstance(thread, discord.Thread):
                log.info("Control room thread for user %s was deleted, clearing stale IDs", ua.display_name)
                self._user_control_thread_ids.discard(ua.control_thread_id)
                ua.control_thread_id = None
                ua.control_message_id = None
                access_mod.save_access_config(cfg)
                return
            # Migrate old "Control Center" name to "Control Room"
            if thread.name == "Control Center":
                try:
                    await thread.edit(name="Control Room")
                    log.info("Renamed Control Center -> Control Room (thread=%s)", thread.id)
                except Exception:
                    pass
            msg = await thread.fetch_message(int(ua.control_message_id))

            repo_names = list(ua.repos.keys())
            mode = "explore"
            if repo_names and repo_names[0] in ua.repos:
                mode = ua.repos[repo_names[0]].mode

            embed = channels.build_user_control_embed(ua.display_name, repo_names, mode)
            view = channels.build_user_control_view(repo_names)
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            log.info("Control room message for user %s was deleted, recreating", ua.display_name)
            self._user_control_thread_ids.discard(ua.control_thread_id)
            ua.control_thread_id = None
            ua.control_message_id = None
            access_mod.save_access_config(cfg)
            # Delete orphan thread if it still exists
            try:
                if thread and isinstance(thread, discord.Thread):
                    await thread.delete()
            except Exception:
                pass
            # Recreate immediately
            try:
                await self._ensure_user_control_post(user_id)
            except Exception:
                log.debug("Failed to recreate control room for user %s", user_id, exc_info=True)
        except Exception:
            log.debug("Failed to refresh control room for user %s", user_id, exc_info=True)

    # --- Dashboard ---

    async def _refresh_dashboard(self) -> None:
        """Update or create the pinned dashboard embed in lobby.

        Serialized: only one refresh runs at a time. If more requests arrive
        while one is running, exactly one additional refresh is queued.
        """
        if self._dashboard_lock.locked():
            # A refresh is already running — mark that another is needed
            self._dashboard_pending = True
            return

        async with self._dashboard_lock:
            await self._refresh_dashboard_impl()
            # If more requests arrived while we held the lock, run once more
            while self._dashboard_pending:
                self._dashboard_pending = False
                await self._refresh_dashboard_impl()

    async def _refresh_dashboard_impl(self) -> None:
        """Inner dashboard refresh logic (must be called under _dashboard_lock)."""
        if not self._lobby_channel_id:
            return
        lobby = self.get_channel(self._lobby_channel_id)
        if not lobby or not isinstance(lobby, discord.TextChannel):
            return

        from bot.claude.types import InstanceStatus
        from datetime import datetime, timezone
        instances = self._store.list_instances()
        running = [i for i in instances if i.status == InstanceStatus.RUNNING]
        today_cost = self._store.get_daily_cost()
        total_cost = self._store.get_total_cost()
        repos = self._store.list_repos()
        active_repo, _ = self._store.get_active_repo()

        # Build session_id -> thread_id map for cross-referencing
        session_to_thread: dict[str, str] = {}
        for proj in self._forum_projects.values():
            for tid, info in proj.threads.items():
                if info.session_id:
                    session_to_thread[info.session_id] = tid

        # Needs attention: failed + questions
        attention = self._store.needs_attention()

        # Recently completed (last 5)
        completed = [
            i for i in instances
            if i.status == InstanceStatus.COMPLETED and not i.needs_input
        ][:5]

        embed = discord.Embed(
            title="Claude Bot Dashboard",
            color=discord.Color.blurple(),
        )

        # Needs Attention section (top priority)
        if attention:
            attn_lines = []
            for inst in attention[:10]:
                icon = "❓" if inst.needs_input else "❌"
                line = f"{icon} `{inst.display_id()}` — {inst.prompt[:30]}"
                thread_id = session_to_thread.get(inst.session_id or "")
                if thread_id:
                    line += f" • <#{thread_id}>"
                attn_lines.append(line)
            embed.add_field(
                name=f"Needs Attention ({len(attention)})",
                value="\n".join(attn_lines), inline=False,
            )

        # Running — grouped by repo, no cap
        if running:
            by_repo: dict[str, list] = {}
            for inst in running:
                by_repo.setdefault(inst.repo_name or "default", []).append(inst)
            run_lines = []
            for rname, repo_insts in by_repo.items():
                if len(by_repo) > 1:
                    run_lines.append(f"**{rname}**")
                for inst in repo_insts:
                    line = f"`{inst.display_id()}` — {inst.prompt[:30]}"
                    if inst.user_name and not inst.is_owner_session:
                        line += f" [{inst.user_name}]"
                    thread_id = session_to_thread.get(inst.session_id or "")
                    if thread_id:
                        line += f" • <#{thread_id}>"
                    if inst.created_at:
                        try:
                            started = datetime.fromisoformat(inst.created_at)
                            if started.tzinfo is None:
                                started = started.replace(tzinfo=timezone.utc)
                            elapsed = datetime.now(timezone.utc) - started
                            mins = int(elapsed.total_seconds() // 60)
                            line += f" • {'<1' if mins < 1 else str(mins)}m"
                        except (ValueError, TypeError):
                            pass
                    run_lines.append(line)
            # Truncate field value to Discord 1024 limit
            field_val = "\n".join(run_lines)
            if len(field_val) > 1024:
                field_val = field_val[:1021] + "..."
            embed.add_field(name=f"Running ({len(running)})", value=field_val, inline=False)
        else:
            embed.add_field(name="Running", value="None", inline=True)

        # Recently Completed
        if completed:
            comp_lines = []
            for inst in completed:
                line = f"✅ `{inst.display_id()}` — {inst.prompt[:30]}"
                thread_id = session_to_thread.get(inst.session_id or "")
                if thread_id:
                    line += f" • <#{thread_id}>"
                comp_lines.append(line)
            embed.add_field(
                name=f"Recently Completed ({len(completed)})",
                value="\n".join(comp_lines), inline=False,
            )

        # Projects with forum links
        if self._forum_projects:
            proj_lines = []
            for name, proj in self._forum_projects.items():
                if proj.forum_channel_id and name != "_default":
                    threads = len(proj.threads)
                    marker = " *" if name == active_repo else ""
                    proj_lines.append(f"<#{proj.forum_channel_id}> ({threads} threads){marker}")
            if proj_lines:
                embed.add_field(name="Projects", value="\n".join(proj_lines), inline=False)

        # Cost + Mode
        embed.add_field(name="Today", value=f"${today_cost:.4f}", inline=True)
        embed.add_field(name="Total", value=f"${total_cost:.4f}", inline=True)
        embed.add_field(name="Mode", value=mode_label(self._store.mode), inline=True)
        embed.add_field(name="PC", value=config.PC_NAME, inline=True)

        # Get or create dashboard message
        # Read dash_msg_id from state, but don't hold the dict reference across awaits
        # to avoid overwriting concurrent _save_forum_map() mutations.
        dash_msg_id = self._store.get_platform_state("discord").get("dashboard_message_id")

        try:
            if dash_msg_id:
                try:
                    msg = await lobby.fetch_message(int(dash_msg_id))
                    await msg.edit(embed=embed)
                except (discord.NotFound, discord.HTTPException):
                    dash_msg_id = None  # Message gone, create new one

            if not dash_msg_id:
                msg = await lobby.send(embed=embed)
                try:
                    await msg.pin()
                except Exception:
                    pass
                # Re-fetch state after await to avoid clobbering concurrent mutations
                state = self._store.get_platform_state("discord")
                state["dashboard_message_id"] = str(msg.id)
                self._store.set_platform_state("discord", state, persist=True)
        except Exception:
            log.debug("Failed to update dashboard", exc_info=True)

        # Always refresh control rooms, independent of dashboard success
        for rname, proj in self._forum_projects.items():
            if proj.control_thread_id:
                asyncio.create_task(self._refresh_control_room(rname))

        # Refresh user forum control rooms
        cfg = load_access_config()
        for uid, ua in cfg.users.items():
            if ua.control_thread_id:
                asyncio.create_task(self._refresh_user_control_room(uid))

    def _get_latest_summary(self, thread_id: str) -> str:
        """Get summary from the most recent instance for this thread's session."""
        lookup = self._thread_to_project(thread_id)
        if not lookup:
            return ""
        _, info = lookup
        if not info.session_id:
            return ""
        for inst in self._store.list_instances()[:10]:
            if inst.session_id == info.session_id and inst.summary:
                return inst.summary
        return ""

    async def _generate_smart_title(
        self, thread: discord.Thread, prompt: str,
        summary: str = "",
    ) -> None:
        """Fire-and-forget: generate an LLM title and rename the thread."""
        info = None
        try:
            thread_id = str(thread.id)
            lookup = self._thread_to_project(thread_id)
            if not lookup:
                return
            _, info = lookup
            if info._title_generated:
                return

            # Claim early to prevent duplicate concurrent tasks
            info._title_generated = True

            title = await _generate_title_text(prompt, summary)
            if not title:
                log.warning("Title generation returned empty for thread %s", thread_id)
                info._title_generated = False  # Allow retry on next query
                return

            base = channels.build_title_name(title)

            async with self._name_lock:
                new_name = channels.build_thread_name(base)
                await thread.edit(name=new_name)

            # Restart idle countdown from title edit, not from query completion
            self._schedule_sleep(thread_id)

            info.topic = title
            self._save_forum_map()
            log.info("Smart title for thread %s: %s", thread_id, new_name)
        except Exception:
            log.warning("Smart title generation failed for thread %s", thread.id, exc_info=True)
            # Reset flag so next query can retry title generation
            if info is not None:
                info._title_generated = False

    # --- Thread sleep/wake (idle indicator) ---

    def _schedule_sleep(self, channel_id: str) -> None:
        """Schedule 💤 after 5 min idle. Cancel any existing timer first."""
        self._cancel_sleep(channel_id)
        gen = self._sleep_gen.get(channel_id, 0) + 1
        self._sleep_gen[channel_id] = gen
        loop = asyncio.get_running_loop()
        self._idle_timers[channel_id] = loop.call_later(
            300,  # 5 min — leaves room for wake edit within 10-min rate limit
            lambda cid=channel_id, g=gen: asyncio.create_task(self._apply_sleep(cid, g)),
        )

    def _cancel_sleep(self, channel_id: str) -> None:
        """Cancel pending sleep timer and invalidate any in-flight callbacks."""
        timer = self._idle_timers.pop(channel_id, None)
        if timer:
            timer.cancel()
        # Bump generation so stale create_task'd coroutines no-op
        self._sleep_gen[channel_id] = self._sleep_gen.get(channel_id, 0) + 1

    async def _apply_sleep(self, channel_id: str, gen: int) -> None:
        """Called by timer — set the thread to sleeping."""
        if self._sleep_gen.get(channel_id) != gen:
            return  # Stale: timer was cancelled or rescheduled
        self._idle_timers.pop(channel_id, None)
        ch = self.get_channel(int(channel_id))
        await self._set_thread_sleeping(ch)

    async def _set_thread_sleeping(
        self,
        channel: discord.abc.GuildChannel | discord.Thread | None,
    ) -> None:
        """Add 💤 prefix to thread name (idle > 5 min).

        Thread name edits have a harsh 2-per-10-min rate limit.
        We budget one edit for sleep, one for wake.
        """
        if not isinstance(channel, discord.Thread):
            return
        async with self._name_lock:
            is_sleeping, topic = channels.parse_thread_name(channel.name)
            if is_sleeping:
                return
            new_name = channels.build_sleeping_thread_name(topic)
            try:
                await channel.edit(name=new_name)
                log.debug("Thread %s now sleeping", channel.id)
            except Exception:
                log.debug("Failed to set thread sleeping", exc_info=True)

    async def _clear_thread_sleeping(
        self,
        channel: discord.abc.GuildChannel | discord.Thread | None,
    ) -> None:
        """Remove 💤 prefix from thread name (processing started)."""
        if not isinstance(channel, discord.Thread):
            return
        async with self._name_lock:
            is_sleeping, topic = channels.parse_thread_name(channel.name)
            if not is_sleeping:
                return
            new_name = channels.build_thread_name(topic)
            try:
                await channel.edit(name=new_name)
                log.debug("Thread %s woke up", channel.id)
            except Exception:
                log.debug("Failed to clear thread sleep", exc_info=True)

    async def _set_thread_active_tag(
        self,
        channel: discord.abc.GuildChannel | discord.Thread | None,
        active: bool,
    ) -> None:
        """Add or remove the 'active' forum tag on a thread.

        Tag-only edits use Discord's normal rate limit (~5/5s), not the
        harsh 2-per-10-min thread name rate limit. Fire-and-forget safe.
        """
        if not isinstance(channel, discord.Thread):
            return
        if not isinstance(channel.parent, discord.ForumChannel):
            return
        try:
            tag_map = {t.name: t for t in channel.parent.available_tags}
            if not tag_map:
                tag_map = await channels.ensure_forum_tags(channel.parent)
            active_tag = tag_map.get("active")
            if not active_tag:
                return

            original_tags = list(channel.applied_tags)
            current_tags = list(original_tags)
            if active:
                if active_tag not in current_tags:
                    current_tags.append(active_tag)
                # Also set mode tag
                lookup = self._thread_to_project(str(channel.id))
                mode = lookup[1].mode if lookup and lookup[1].mode else self._store.mode
                mode_tag = tag_map.get(mode)
                # Remove other mode tags, add current
                for m in MODE_DISPLAY:
                    mt = tag_map.get(m)
                    if mt and mt in current_tags:
                        current_tags.remove(mt)
                if mode_tag and mode_tag not in current_tags:
                    current_tags.append(mode_tag)
            else:
                if active_tag in current_tags:
                    current_tags.remove(active_tag)

            if current_tags != original_tags:
                await channel.edit(applied_tags=current_tags[:5])
                log.debug("Set thread %s active=%s", channel.id, active)
        except Exception:
            log.debug("Failed to set active tag on thread %s", channel.id, exc_info=True)

    async def _create_new_session(
        self, interaction: discord.Interaction, repo_name: str | None,
        *, redirect: bool = False, mode: str | None = None,
        user_id: str | None = None, user_name: str | None = None,
    ) -> None:
        """Create a new session thread.

        If user_id is set (non-owner), the thread is created in the user's
        personal forum instead of the repo forum.
        """
        repo_name = repo_name or "_default"

        # Resolve personal forum for non-owners
        forum_channel_id = None
        if user_id:
            cfg = load_access_config()
            ua = cfg.users.get(user_id)
            if ua and ua.forum_channel_id:
                forum_channel_id = ua.forum_channel_id

        thread = await self._get_or_create_session_thread(
            repo_name, None, "new-session",
            forum_channel_id=forum_channel_id,
            user_id=user_id, user_name=user_name,
        )
        if thread:
            # Apply per-thread mode if specified (e.g. /new mode:build)
            if mode:
                lookup = self._thread_to_project(str(thread.id))
                if lookup:
                    lookup[1].mode = mode
                    self._save_forum_map()
            # Non-owners can't see lobby, so always use ephemeral followup
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

    def _attach_session_callbacks(self, ctx, thread_info, thread_id: str) -> None:
        """Wire up session resolution callbacks on a RequestContext."""
        ctx.resolve_session_id = lambda _info=thread_info: _info.session_id or None
        ctx.on_session_resolved = lambda sid, _tid=thread_id: self._set_thread_session(_tid, sid)

    def _set_thread_session(self, thread_id: str, session_id: str) -> None:
        """Write session_id to ThreadInfo immediately (called from engine callback)."""
        lookup = self._thread_to_project(thread_id)
        if lookup:
            _, info = lookup
            if not info.session_id:
                info.session_id = session_id
                self._save_forum_map()
                log.info("Session resolved for thread %s -> %s", thread_id, session_id[:12])

    async def _finalize_pending_thread(
        self, thread_id: str, thread: discord.Thread, prompt: str,
    ) -> None:
        """After first query in a /new thread, update session mapping and rename."""
        lookup = self._thread_to_project(thread_id)
        if lookup:
            _, info = lookup
            # Happy path: on_session_resolved callback already set session_id
            if info.session_id:
                info.topic = prompt
                self._save_forum_map()
            else:
                # Fallback: search instances (in case callback didn't fire)
                for inst in self._store.list_instances()[:10]:
                    if inst.session_id and inst.origin_platform == "discord":
                        discord_msg_ids = inst.message_ids.get("discord", [])
                        if discord_msg_ids:
                            try:
                                await thread.fetch_message(int(discord_msg_ids[0]))
                            except (discord.NotFound, discord.HTTPException):
                                continue
                            info.session_id = inst.session_id
                            info.topic = prompt
                            self._save_forum_map()
                            log.info("Finalized pending thread %s -> session %s (fallback)",
                                     thread_id, inst.session_id)
                            break

        # Generate smart title if still "new-session"
        if "new-session" in thread.name:
            summary = self._get_latest_summary(thread_id)
            asyncio.create_task(self._generate_smart_title(thread, prompt, summary))

    async def _update_pending_thread(self, thread_id: str) -> None:
        """After a query completes, update a pending thread's session mapping."""
        lookup = self._thread_to_project(thread_id)
        if not lookup:
            return  # thread not tracked
        proj, info = lookup
        if info.session_id:
            return  # already mapped

        # Resolve channel once
        ch = self.get_channel(int(thread_id))
        if not ch:
            try:
                ch = await self.fetch_channel(int(thread_id))
            except (discord.NotFound, discord.Forbidden):
                return
        if not isinstance(ch, (discord.TextChannel, discord.Thread)):
            return

        for inst in self._store.list_instances()[:10]:
            if inst.session_id and inst.origin_platform == "discord":
                discord_msg_ids = inst.message_ids.get("discord", [])
                if not discord_msg_ids:
                    continue
                try:
                    await ch.fetch_message(int(discord_msg_ids[0]))
                    # Message found in this thread — map session
                    info.session_id = inst.session_id
                    info.origin = "bot"
                    self._save_forum_map()
                    log.info("Mapped thread %s -> session %s (repo: %s)",
                             thread_id, inst.session_id, proj.repo_name)
                    return
                except (discord.NotFound, discord.HTTPException):
                    continue

    def _build_ref_embed(
        self, proj: ForumProject, info: ThreadInfo,
        msgs: list[dict], target_thread_id: str,
    ) -> discord.Embed:
        """Build a purple embed showing referenced conversation context."""
        topic = info.topic or f"Thread #{target_thread_id[-6:]}"
        embed = discord.Embed(
            title=f"Referenced: {topic[:60]}",
            color=discord.Color(0x9B59B6),
        )
        embed.add_field(name="Repo", value=proj.repo_name, inline=True)
        embed.add_field(name="Thread", value=f"<#{target_thread_id}>", inline=True)

        # Adaptive truncation for embed display
        per_msg = max(150, 4000 // max(len(msgs), 1))
        lines = []
        for m in msgs:
            role = "**You**" if m["role"] == "user" else "**Claude**"
            text = m["text"][:per_msg] + ("..." if len(m["text"]) > per_msg else "")
            lines.append(f"{role}: {text}")
        embed.description = "\n\n".join(lines)[:4096]
        return embed

    def _build_ref_context(
        self, proj: ForumProject, info: ThreadInfo,
        msgs: list[dict], target_thread_id: str,
    ) -> str:
        """Build plain-text context string for prompt injection into Claude."""
        topic = info.topic or f"Thread #{target_thread_id[-6:]}"
        lines = [f"--- Referenced conversation from [{proj.repo_name}] \"{topic}\" ---"]
        for m in msgs:
            role = "You" if m["role"] == "user" else "Claude"
            text = m["text"][:2000]  # generous per-message limit for LLM
            lines.append(f"{role}: {text}")
        lines.append("--- End reference ---")
        return "\n".join(lines)

    async def _populate_thread_history(
        self, channel: discord.TextChannel | discord.Thread, session_id: str,
        thread_id: str,
        *, force: bool = False, cli_label: bool = False,
    ) -> None:
        """Send messages from a session into a thread/channel.

        Args:
            force: If True, always send last 10 (no incremental check).
            cli_label: If True, label messages as "You (CLI)" / "Claude (CLI)".
        """
        lookup = self._thread_to_project(thread_id)
        prev_count = 0
        if not force and lookup:
            prev_count = lookup[1]._synced_msg_count

        fpath = await asyncio.to_thread(sessions_mod.find_session_file, session_id)
        if not fpath:
            return

        all_messages = await asyncio.to_thread(sessions_mod.read_session_messages, fpath, 9999)
        if not all_messages:
            return

        total = len(all_messages)
        if prev_count == 0:
            to_send = all_messages[-10:]
        elif total > prev_count:
            to_send = all_messages[prev_count:]
        else:
            log.debug("No new messages for thread %s (total=%d, synced=%d)", thread_id, total, prev_count)
            return

        user_label = "**You (CLI)**" if cli_label else "**You**"
        bot_label = "**Claude (CLI)**" if cli_label else "**Claude**"

        log.info("Populating thread %s with %d messages (total=%d, prev=%d)",
                 thread_id, len(to_send), total, prev_count)
        ch_id = str(channel.id)
        for msg in to_send:
            role = user_label if msg["role"] == "user" else bot_label
            text = msg["text"]
            if len(text) > 800:
                text = text[:800] + "\u2026"
            try:
                markup = self.messenger.markdown_to_markup(f"{role}:\n{text}")
                await self.messenger.send_text(ch_id, markup, silent=True)
            except Exception:
                try:
                    await self.messenger.send_text(ch_id, f"{role}:\n{text[:800]}", silent=True)
                except Exception:
                    break

        if lookup:
            lookup[1]._synced_msg_count = total
            self._save_forum_map()

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Handle button interactions (persistent views)."""
        if interaction.type != discord.InteractionType.component:
            return
        if not self._in_scope(interaction.guild, interaction.channel):
            return

        btn_access = self._check_access(
            interaction.user.id, channel_id=str(interaction.channel_id),
        )
        if not btn_access.allowed:
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return

        custom_id = interaction.data.get("custom_id", "") if interaction.data else ""

        # --- Voice transcription confirm/cancel ---
        if custom_id.startswith("voice_send:") or custom_id.startswith("voice_cancel:"):
            confirm_msg_id = str(interaction.message.id) if interaction.message else None
            pending = self._pending_voice.get(confirm_msg_id) if confirm_msg_id else None
            if not pending:
                await interaction.response.edit_message(
                    content="This transcription has expired.", embed=None, view=None,
                )
                return

            # Only the original author can confirm
            if str(interaction.user.id) != pending["author_id"]:
                await interaction.response.send_message(
                    "Only the person who sent the voice message can do this.",
                    ephemeral=True,
                )
                return

            if custom_id.startswith("voice_cancel:"):
                self._pending_voice.pop(confirm_msg_id, None)
                await interaction.response.edit_message(
                    content="Voice message cancelled.", embed=None, view=None,
                )
                return

            # voice_send — run transcription as a normal message
            transcription = pending["transcription"]
            channel_id = str(interaction.channel_id)
            self._pending_voice.pop(confirm_msg_id, None)

            # Update embed to show it was sent
            if interaction.message and interaction.message.embeds:
                embed = interaction.message.embeds[0]
                embed.color = discord.Color.green()
                embed.set_footer(text="Sent")
                await interaction.response.edit_message(embed=embed, view=None)
            else:
                await interaction.response.edit_message(
                    content=f"Sending: {transcription[:100]}...", view=None,
                )

            # Feed transcription through the normal message handling pipeline
            channel = interaction.channel
            if isinstance(channel, discord.Thread):
                parent = channel.parent
                if parent and isinstance(parent, discord.ForumChannel):
                    lookup = self._thread_to_project(channel_id)
                    if lookup:
                        proj, info = lookup
                        session_id = info.session_id or None
                        repo_name = proj.repo_name if proj.repo_name != "_default" else None
                        self._cancel_sleep(channel_id)
                        asyncio.create_task(self._clear_thread_sleeping(channel))
                        asyncio.create_task(self._set_thread_active_tag(channel, True))
                        asyncio.create_task(self._refresh_dashboard())
                        ctx = self._ctx(channel_id, session_id=session_id,
                                        repo_name=repo_name, thread_info=info,
                                        access_result=btn_access)
                        ctx.user_id = str(interaction.user.id)
                        ctx.user_name = interaction.user.display_name
                        self._attach_session_callbacks(ctx, info, channel_id)
                        try:
                            await commands.on_text(ctx, transcription)
                        finally:
                            self._persist_ctx_settings(ctx)
                            asyncio.create_task(self._try_apply_tags_after_run(channel_id))
                            self._schedule_sleep(channel_id)
                            asyncio.create_task(self._refresh_dashboard())
                        return

            # Fallback: unmapped channel
            ctx = self._ctx(channel_id, access_result=btn_access)
            ctx.user_id = str(interaction.user.id)
            ctx.user_name = interaction.user.display_name
            await commands.on_text(ctx, transcription)
            return

        # --- Select menu: repo switch (owner only) ---
        if custom_id == "repo_switch_select":
            if not btn_access.is_owner:
                await interaction.response.send_message("Owner only", ephemeral=True)
                return
            values = interaction.data.get("values", []) if interaction.data else []
            if values:
                repo_name = values[0]
                current, _ = self._store.get_active_repo()
                if repo_name == current:
                    await interaction.response.edit_message(
                        content=f"**{repo_name}** is already active.",
                        view=None,
                    )
                elif self._store.switch_repo(repo_name):
                    _, path = self._store.get_active_repo()
                    await interaction.response.edit_message(
                        content=f"Switched to **{repo_name}**: `{path}`",
                        view=None,
                    )
                else:
                    await interaction.response.edit_message(
                        content=f"Repo '{repo_name}' not found.",
                        view=None,
                    )
            return

        parts = custom_id.split(":", 1)
        if len(parts) != 2:
            return

        action, instance_id = parts
        log.info("Discord button %s:%s in #%s", action, instance_id[:12], getattr(interaction.channel, "name", "?"))

        # --- Mode selection in new thread welcome embed ---
        if action == "mode_set" and instance_id in VALID_MODES:
            target_mode = instance_id
            # Enforce mode ceiling for non-owners
            if not btn_access.is_owner and btn_access.mode_ceiling:
                target_mode = access_effective_mode(
                    access_mod.RepoAccess(mode=btn_access.mode_ceiling),
                    target_mode,
                )
            # Write to ThreadInfo (per-thread), not global store
            thread_id = str(interaction.channel_id)
            lookup = self._thread_to_project(thread_id)
            if lookup:
                lookup[1].mode = target_mode
                self._save_forum_map()
            elif btn_access.is_owner:
                self._store.mode = target_mode  # fallback for unmapped channels (owner only)
            # Update the welcome embed to reflect selected mode
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
            return

        await interaction.response.defer()

        # --- Load CLI history into thread ---
        if action == "load_history":
            cli_session_id = instance_id
            thread_id = str(interaction.channel_id)
            lookup = self._thread_to_project(thread_id)
            ch = interaction.channel
            if lookup and isinstance(ch, (discord.TextChannel, discord.Thread)):
                proj, info = lookup
                info.session_id = cli_session_id
                info.origin = "cli"
                self._save_forum_map()
                await self._populate_thread_history(
                    ch, cli_session_id, thread_id,
                    force=True, cli_label=True,
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
            return

        # --- New session with repo picker ---
        if action == "new_repo":
            repo_name = instance_id
            user_id = None
            user_name = None
            if not btn_access.is_owner:
                user_id = str(interaction.user.id)
                user_name = interaction.user.display_name
            await self._create_new_session(
                interaction, repo_name, redirect=True,
                user_id=user_id, user_name=user_name,
            )
            # Refresh control room immediately (recovers if embed was deleted externally)
            asyncio.create_task(self._refresh_control_room(repo_name))
            if user_id:
                asyncio.create_task(self._refresh_user_control_room(user_id))
            return

        # --- Sync CLI for a repo (control room button) ---
        if action == "sync_repo":
            if not btn_access.is_owner:
                await interaction.followup.send("Owner only.", ephemeral=True)
                return
            created, populated = await self._sync_cli_sessions(3)
            parts = []
            if created:
                parts.append(f"Created {len(created)} threads")
            if populated:
                parts.append(f"Updated {len(populated)} threads")
            if not parts:
                parts.append("No sessions found")
            await interaction.followup.send(
                f"Synced `{instance_id}`: " + ", ".join(parts), ephemeral=True,
            )
            return

        # --- Session resume: create/find thread, redirect there ---
        if action == "sess_resume":
            session_id = instance_id
            from bot.engine import workflows

            topic = "session"
            repo_name = None
            session_list = await asyncio.to_thread(
                sessions_mod.scan_sessions, 10, self._store.list_repos(),
            )
            for s in session_list:
                if s["id"] == session_id:
                    topic = s["topic"]
                    repo_name = s.get("project")
                    break

            repo_name = repo_name or "_default"
            thread = await self._get_or_create_session_thread(
                repo_name, session_id, topic,
            )
            if thread:
                try:
                    await interaction.delete_original_response()
                except Exception:
                    pass
                asyncio.create_task(self._send_redirect(thread))
                lookup_info = self._thread_to_project(str(thread.id))
                ti = lookup_info[1] if lookup_info else None
                ctx = self._ctx(
                    str(thread.id), session_id=session_id,
                    repo_name=repo_name if repo_name != "_default" else None,
                    thread_info=ti,
                    access_result=btn_access,
                )
                ctx.user_id = str(interaction.user.id)
                ctx.user_name = interaction.user.display_name
                source_msg_id = str(interaction.message.id) if interaction.message else None
                await workflows.on_sess_resume(ctx, session_id, source_msg_id)
            return

        # --- New session button: create a new forum thread (like /new) ---
        if action == "new":
            thread_id = str(interaction.channel_id)
            lookup = self._thread_to_project(thread_id)
            repo_name = lookup[0].repo_name if lookup else None
            user_id = None
            user_name = None
            if not btn_access.is_owner:
                user_id = str(interaction.user.id)
                user_name = interaction.user.display_name
                # Fall back to user's granted repo, not owner's active repo
                if not repo_name:
                    cfg = load_access_config()
                    ua = cfg.users.get(user_id)
                    if ua and ua.repos:
                        granted = [r for r in ua.repos if r in self._forum_projects]
                        if granted:
                            repo_name = granted[0]
            if not repo_name:
                repo_name, _ = self._store.get_active_repo()
            await self._create_new_session(
                interaction, repo_name,
                user_id=user_id, user_name=user_name,
            )
            try:
                await interaction.delete_original_response()
            except Exception:
                pass
            return

        channel_id = str(interaction.channel_id)
        source_msg_id = str(interaction.message.id) if interaction.message else None

        is_query = action in _QUERY_ACTIONS
        if is_query:
            self._cancel_sleep(channel_id)
            asyncio.create_task(self._clear_thread_sleeping(interaction.channel))
            asyncio.create_task(self._set_thread_active_tag(interaction.channel, True))
            asyncio.create_task(self._refresh_dashboard())

        lookup = self._thread_to_project(channel_id)
        t_info = lookup[1] if lookup else None
        ctx = self._ctx(channel_id, thread_info=t_info, access_result=btn_access)
        ctx.user_id = str(interaction.user.id)
        ctx.user_name = interaction.user.display_name
        try:
            await commands.handle_callback(ctx, action, instance_id, source_msg_id)
        finally:
            self._persist_ctx_settings(ctx)
            if is_query:
                asyncio.create_task(self._try_apply_tags_after_run(channel_id))
                self._schedule_sleep(channel_id)
                asyncio.create_task(self._refresh_dashboard())
            elif action.startswith("mode_"):
                asyncio.create_task(self._refresh_dashboard())
