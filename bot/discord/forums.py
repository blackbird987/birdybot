"""Forum-based session management: ForumProject, ThreadInfo, ForumManager.

Owns all forum/thread data structures, lookups, creation, sync, and
history population. ClaudeBot delegates forum operations through
ForumManager's public interface.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from bot import config
from bot.discord import channels
from bot.discord import access as access_mod
from bot.discord.access import load_access_config
from bot.engine import sessions as sessions_mod
from bot.platform.base import RequestContext
from bot.platform.formatting import MODE_DISPLAY

if TYPE_CHECKING:
    from bot.discord.adapter import DiscordMessenger
    from bot.store.state import StateStore

log = logging.getLogger(__name__)
_NOWND: dict = config.NOWND


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
    effort: str | None = None         # None=inherit, "low"/"medium"/"high"/"max"
    # User who created this thread (None = owner)
    user_id: str | None = None
    user_name: str | None = None
    # All users who interacted with this thread (for close mentions)
    user_ids: set[str] = field(default_factory=set)

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
        if self.effort is not None:
            d["effort"] = self.effort
        if self.user_id is not None:
            d["user_id"] = self.user_id
        if self.user_name is not None:
            d["user_name"] = self.user_name
        if self.user_ids:
            d["user_ids"] = sorted(self.user_ids)
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
            effort=data.get("effort"),
            user_id=data.get("user_id"),
            user_name=data.get("user_name"),
            user_ids=set(data.get("user_ids", [])),
        )


@dataclass
class ForumProject:
    repo_name: str
    forum_channel_id: str
    threads: dict[str, ThreadInfo] = field(default_factory=dict)
    control_thread_id: str | None = None
    control_message_id: str | None = None
    archive_thread_id: str | None = None
    archive_migrated: bool = False
    # Legacy — kept only for migration from text channel to forum thread
    archive_channel_id: str | None = None
    monitor_thread_id: str | None = None
    # Verify Board — per-repo pinned thread with living verification list
    verify_board_thread_id: str | None = None
    verify_board_message_id: str | None = None
    verify_items: list[dict] = field(default_factory=list)

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
        if self.archive_thread_id:
            d["archive_thread_id"] = self.archive_thread_id
        if self.archive_migrated:
            d["archive_migrated"] = self.archive_migrated
        if self.archive_channel_id:
            d["archive_channel_id"] = self.archive_channel_id
        if self.monitor_thread_id:
            d["monitor_thread_id"] = self.monitor_thread_id
        if self.verify_board_thread_id:
            d["verify_board_thread_id"] = self.verify_board_thread_id
        if self.verify_board_message_id:
            d["verify_board_message_id"] = self.verify_board_message_id
        if self.verify_items:
            d["verify_items"] = self.verify_items
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
            archive_thread_id=data.get("archive_thread_id"),
            archive_migrated=data.get("archive_migrated", False),
            archive_channel_id=data.get("archive_channel_id"),
            monitor_thread_id=data.get("monitor_thread_id"),
            verify_board_thread_id=data.get("verify_board_thread_id"),
            verify_board_message_id=data.get("verify_board_message_id"),
            verify_items=list(data.get("verify_items", [])),
        )


class ForumManager:
    """Manages forum projects, threads, and session mappings.

    Dependencies: discord.Client (for channel ops), StateStore (for
    persistence and repo list), guild/category IDs. Does NOT hold a
    reference to ClaudeBot.
    """

    def __init__(
        self,
        client: discord.Client,
        store: StateStore,
        guild_id: int,
        category_id: int | None = None,
        discord_user_id: int | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._guild_id = guild_id
        self._category_id = category_id
        self._discord_user_id = discord_user_id

        self._forum_projects: dict[str, ForumProject] = {}
        self._forum_lock = asyncio.Lock()
        self._thread_lock = asyncio.Lock()
        # In-memory set of user forum control room thread IDs (O(1) skip check)
        self._user_control_thread_ids: set[str] = set()
        # In-memory set of archive channel IDs (O(1) skip check)
        self._archive_channel_ids: set[int] = set()
        # Cache: repo_path -> has git remote (remotes rarely change at runtime)
        self._remote_cache: dict[str, bool] = {}
        # Verify Board: per-repo debounce + lock for item mutations
        self._verify_debounce: dict[str, asyncio.Task] = {}
        self._verify_locks: dict[str, asyncio.Lock] = {}
        # Per-thread cached priming digest (raw user text — kept in-memory only,
        # never serialized to state.json; cleared on session rebind or failed run).
        self._prime_cache: dict[str, str] = {}

    @property
    def forum_projects(self) -> dict[str, ForumProject]:
        return self._forum_projects

    @property
    def user_control_thread_ids(self) -> set[str]:
        return self._user_control_thread_ids

    @property
    def archive_channel_ids(self) -> set[int]:
        return self._archive_channel_ids

    @property
    def category_id(self) -> int | None:
        return self._category_id

    @category_id.setter
    def category_id(self, value: int | None) -> None:
        self._category_id = value

    # --- Auto-Follow Helpers ---

    async def _auto_follow_thread(self, thread: discord.Thread, repo_name: str) -> None:
        """Add owner + granted users to a thread so they auto-follow.

        Batched via asyncio.gather to minimize wall-clock time.
        add_user is idempotent — no-op if already joined.
        """
        guild = self._client.get_guild(self._guild_id)
        if not guild:
            return

        targets: list[discord.Member] = []

        # Owner
        if self._discord_user_id:
            member = guild.get_member(self._discord_user_id)
            if member:
                targets.append(member)

        # Granted users with access to this repo
        cfg = load_access_config()
        for uid, ua in cfg.users.items():
            if ua.global_access or repo_name in ua.repos:
                member = guild.get_member(int(uid))
                if member and member not in targets:
                    targets.append(member)

        if not targets:
            return

        results = await asyncio.gather(
            *(thread.add_user(m) for m in targets),
            return_exceptions=True,
        )
        for m, r in zip(targets, results):
            if isinstance(r, Exception):
                log.debug("Failed to auto-follow user %s to thread %s: %s", m.id, thread.id, r)

    async def _auto_follow_user_thread(self, thread: discord.Thread, user_id: str) -> None:
        """Add specific user + owner to a personal forum thread."""
        guild = self._client.get_guild(self._guild_id)
        if not guild:
            return
        uids = {user_id}
        if self._discord_user_id:
            uids.add(str(self._discord_user_id))

        targets = []
        for uid in uids:
            try:
                member = guild.get_member(int(uid))
            except (ValueError, TypeError):
                continue
            if member:
                targets.append(member)

        if targets:
            await asyncio.gather(
                *(thread.add_user(m) for m in targets),
                return_exceptions=True,
            )

    # --- Forum-Session Mapping ---

    def load_forum_map(self) -> None:
        """Load forum->project mapping from platform_state."""
        state = self._store.get_platform_state("discord")
        raw = state.get("forum_projects", {})
        self._forum_projects = {
            k: ForumProject.from_dict(v) for k, v in raw.items()
        }
        self._archive_channel_ids = set()
        for p in self._forum_projects.values():
            if p.archive_thread_id:
                self._archive_channel_ids.add(int(p.archive_thread_id))
            if p.archive_channel_id:
                self._archive_channel_ids.add(int(p.archive_channel_id))
        log.info("Loaded %d forum projects", len(self._forum_projects))

    def save_forum_map(self) -> None:
        """Persist forum->project mapping to platform_state."""
        state = self._store.get_platform_state("discord")
        state["forum_projects"] = {k: v.to_dict() for k, v in self._forum_projects.items()}
        self._store.set_platform_state("discord", state, persist=True)

    def session_to_thread(self, session_id: str) -> tuple[str, ThreadInfo] | None:
        """Reverse lookup: find thread for a session_id. Returns (thread_id, info)."""
        for proj in self._forum_projects.values():
            for tid, info in proj.threads.items():
                if info.session_id == session_id:
                    return tid, info
        return None

    def thread_to_project(self, thread_id: str) -> tuple[ForumProject, ThreadInfo] | None:
        """Find project + thread info for a thread_id."""
        for proj in self._forum_projects.values():
            info = proj.threads.get(thread_id)
            if info:
                return proj, info
        return None

    def forum_by_channel_id(self, forum_id: str) -> ForumProject | None:
        """Find project by forum channel ID."""
        for proj in self._forum_projects.values():
            if proj.forum_channel_id == forum_id:
                return proj
        return None

    def is_user_forum(self, forum_id: str) -> tuple[str, str] | None:
        """Check if a forum is a user's personal forum. Returns (user_id, user_name) or None."""
        cfg = load_access_config()
        for uid, ua in cfg.users.items():
            if ua.forum_channel_id == forum_id:
                return uid, ua.display_name
        return None

    def user_forum_thread_to_repo(self, thread: discord.Thread) -> str | None:
        """Resolve repo name from a thread's tags in a user's personal forum."""
        repos = self._store.list_repos()
        for tag in thread.applied_tags:
            if tag.name in repos:
                return tag.name
        return None

    # --- Forum Provisioning ---

    async def ensure_user_forum(
        self, user_id: int, display_name: str, repo_names: list[str],
    ) -> discord.ForumChannel | None:
        """Create or get a personal forum channel for a granted user."""
        guild = self._client.get_guild(self._guild_id)
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
                    welcome_thread, _ = await channels.create_user_welcome_post(forum, display_name, repo_names)
                    ua.welcome_posted = True
                    access_mod.save_access_config(cfg)
                except Exception:
                    log.warning("Failed to create welcome post in forum %s for user %s, will retry next startup",
                                forum.id, user_id, exc_info=True)
                    welcome_thread = None
                if welcome_thread:
                    try:
                        await self._auto_follow_user_thread(welcome_thread, str(user_id))
                    except Exception:
                        log.warning("Failed to auto-follow welcome thread %s for user %s", welcome_thread.id, user_id)
            # Create control room post
            try:
                await self.ensure_user_control_post(str(user_id), forum)
            except Exception:
                log.warning("Failed to create control room in forum %s for user %s",
                            forum.id, user_id, exc_info=True)

        return forum

    async def sync_user_forum_tags(self, user_id: str) -> None:
        """Sync a user's forum tags to match their current access grants."""
        cfg = load_access_config()
        ua = cfg.users.get(user_id)
        if not ua or not ua.forum_channel_id:
            return
        guild = self._client.get_guild(self._guild_id)
        if not guild:
            return
        forum = guild.get_channel(int(ua.forum_channel_id))
        if not forum or not isinstance(forum, discord.ForumChannel):
            return
        repo_names = list(ua.repos.keys())
        await channels.sync_user_forum_tags(forum, repo_names)

    async def get_or_create_forum(self, repo_name: str) -> discord.ForumChannel | None:
        """Get or create a forum channel for a repo.

        Lock discipline: ``_forum_lock`` is held across the Discord create
        call so concurrent callers for the same repo can't both create
        duplicate forums. Forum creation is once-per-repo so the
        contention is sparse; safety-first beats throughput here.
        """
        guild = self._client.get_guild(self._guild_id)
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
                    log.warning("Forum %s for repo %s was deleted, recreating", existing.forum_channel_id, repo_name)

            forum = await channels.ensure_forum(guild, category, repo_name)
            if repo_name not in self._forum_projects:
                self._forum_projects[repo_name] = ForumProject(
                    repo_name=repo_name,
                    forum_channel_id=str(forum.id),
                )
            else:
                self._forum_projects[repo_name].forum_channel_id = str(forum.id)
            self.save_forum_map()
            return forum

    async def get_or_create_session_thread(
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
            result = self.session_to_thread(session_id)
            if result:
                tid, info = result
                ch = self._client.get_channel(int(tid))
                if ch and isinstance(ch, discord.Thread):
                    return ch
                try:
                    ch = await self._client.fetch_channel(int(tid))
                    if isinstance(ch, discord.Thread):
                        return ch
                except (discord.NotFound, discord.Forbidden):
                    pass
                # Thread gone — remove stale mapping
                for proj in self._forum_projects.values():
                    proj.threads.pop(tid, None)

        # Resolve the target forum
        if forum_channel_id:
            guild = self._client.get_guild(self._guild_id)
            forum = guild.get_channel(int(forum_channel_id)) if guild else None
            if not forum or not isinstance(forum, discord.ForumChannel):
                log.warning("Personal forum %s not found, falling back to repo forum", forum_channel_id)
                forum = await self.get_or_create_forum(repo_name)
        else:
            forum = await self.get_or_create_forum(repo_name)
        if not forum:
            return None

        thread = None
        # Hold _thread_lock across the Discord create call. Two messages
        # arriving concurrently for the same session_id would otherwise both
        # create real Discord threads and orphan one of them. Serializing
        # creates is cheap (per-user pace) and the safety guarantee matters
        # more than the marginal contention.
        async with self._thread_lock:
            # Double-check after lock (another message may have created it)
            if session_id:
                result = self.session_to_thread(session_id)
                if result:
                    tid, _ = result
                    try:
                        ch = await self._client.fetch_channel(int(tid))
                        if isinstance(ch, discord.Thread):
                            return ch  # existing thread — no follow needed
                    except (discord.NotFound, discord.Forbidden):
                        pass

            thread_name = channels.build_channel_name(topic) if topic != "new-session" else "new-session"
            thread, _msg = await channels.create_forum_post(
                forum, thread_name, origin=origin,
                topic_preview=topic,
                current_mode=self._store.mode,
                current_effort=self._store.effort,
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
                self.save_forum_map()
            else:
                log.warning("No ForumProject for repo %s — thread %s created but unmapped", repo_name, thread.id)

        # Auto-follow outside the lock — don't block other session creation
        if thread:
            try:
                if forum_channel_id and user_id:
                    await self._auto_follow_user_thread(thread, str(user_id))
                else:
                    await self._auto_follow_thread(thread, repo_name)
            except Exception:
                log.warning("Failed to auto-follow session thread %s", thread.id)

        return thread

    # --- Thread Sync ---

    async def sync_single_thread(self, thread_id: str, messenger: DiscordMessenger) -> None:
        """Refresh a single session thread: pull latest CLI messages."""
        lookup = self.thread_to_project(thread_id)
        if not lookup:
            return
        proj, info = lookup
        session_id = info.session_id
        repo_name = proj.repo_name
        if not session_id:
            return

        ch = self._client.get_channel(int(thread_id))
        if not ch:
            try:
                ch = await self._client.fetch_channel(int(thread_id))
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
                if latest and latest["id"] != session_id and info.origin == "cli":
                    info.session_id = latest["id"]
                    info.origin = "cli"
                    info._synced_msg_count = 0
                    self.save_forum_map()
                    log.info("Single-sync updated thread %s to session %s", thread_id, latest["id"][:12])
                    session_id = latest["id"]

        await self.populate_thread_history(ch, session_id, thread_id, messenger=messenger)
        log.info("Single-sync refreshed thread %s", thread_id)

    async def reconcile_forums(self) -> None:
        """Validate forum channels on startup. Clean stale mappings."""
        guild = self._client.get_guild(self._guild_id)
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
                continue
            existing = self.forum_by_channel_id(str(ch.id))
            if not existing:
                repo_name = ch.name
                if repo_name not in valid_projects:
                    valid_projects[repo_name] = ForumProject(
                        repo_name=repo_name,
                        forum_channel_id=str(ch.id),
                    )
                    log.info("Discovered unmapped forum %s (%s)", ch.id, ch.name)

        # Publish reconciled mapping under lock so concurrent get_or_create
        # callers don't see a half-replaced dict.
        async with self._forum_lock:
            if valid_projects != self._forum_projects:
                self._forum_projects = valid_projects
                self.save_forum_map()

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
                if "\u2502" in thread.name:
                    new_name = thread.name.split("\u2502", 1)[1].strip()
                    if new_name:
                        old_name = thread.name
                        try:
                            await thread.edit(name=new_name[:100])
                            log.info("Renamed thread %s: %s -> %s", thread.id, old_name, new_name)
                        except Exception:
                            log.debug("Failed to rename thread %s", thread.id, exc_info=True)
                # Legacy migration: strip old emoji prefixes
                _, topic = channels.parse_thread_name(thread.name)
                clean_name = channels.build_thread_name(topic)
                if clean_name != thread.name:
                    try:
                        await thread.edit(name=clean_name)
                        log.info("Stripped legacy emoji from thread: %s", thread.id)
                    except Exception:
                        log.debug("Failed to strip legacy emoji", exc_info=True)
                # Clear stale "active" tag
                if active_tag and active_tag in thread.applied_tags:
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
                    await self.ensure_control_post(repo_name)
                except Exception:
                    log.debug("Failed to create control room for %s", repo_name, exc_info=True)

        # Ensure archive threads exist for all repo forums
        for repo_name, proj in self._forum_projects.items():
            if proj.forum_channel_id and not proj.archive_thread_id:
                try:
                    await self.ensure_archive_thread(repo_name)
                except Exception:
                    log.debug("Failed to create archive thread for %s",
                              repo_name, exc_info=True)

        # Ensure Verify Board threads exist for all repo forums (one-shot migration)
        for repo_name, proj in self._forum_projects.items():
            if proj.forum_channel_id and not proj.verify_board_thread_id:
                try:
                    await self.ensure_verify_board(repo_name)
                except Exception:
                    log.debug("Failed to create verify-board for %s",
                              repo_name, exc_info=True)
            elif proj.verify_board_thread_id:
                # Re-render on startup so view buttons are re-registered
                try:
                    await self.refresh_verify_board(repo_name)
                except Exception:
                    log.debug("Failed to refresh verify-board for %s",
                              repo_name, exc_info=True)

        # Migrate old archive text channels → forum threads
        for repo_name, proj in self._forum_projects.items():
            if proj.archive_channel_id and proj.archive_thread_id and not proj.archive_migrated:
                async with self._forum_lock:
                    await self._migrate_archive_channel(repo_name, proj, guild)

        # Populate user control room thread ID set + create missing control rooms
        cfg = load_access_config()
        for uid, ua in cfg.users.items():
            if ua.control_thread_id:
                self._user_control_thread_ids.add(ua.control_thread_id)
            elif ua.forum_channel_id:
                try:
                    await self.ensure_user_control_post(uid)
                except Exception:
                    log.debug("Failed to create control room for user %s", uid, exc_info=True)

        # Ensure archive threads in personal forums
        for uid, ua in cfg.users.items():
            if ua.forum_channel_id and not ua.archive_thread_id:
                try:
                    await self.ensure_user_archive_thread(uid)
                except Exception:
                    log.debug("Failed to create archive for user %s", uid, exc_info=True)
            elif ua.archive_thread_id:
                self._archive_channel_ids.add(int(ua.archive_thread_id))

        # Auto-follow all pinned threads (batched per-repo, 0.5s inter-batch sleep)
        for repo_name, proj in self._forum_projects.items():
            tasks: list = []
            if proj.control_thread_id:
                try:
                    ch = await self._client.fetch_channel(int(proj.control_thread_id))
                    if isinstance(ch, discord.Thread):
                        tasks.append(self._auto_follow_thread(ch, repo_name))
                except Exception:
                    pass
            if proj.archive_thread_id:
                try:
                    ch = await self._client.fetch_channel(int(proj.archive_thread_id))
                    if isinstance(ch, discord.Thread):
                        tasks.append(self._auto_follow_thread(ch, repo_name))
                except Exception:
                    pass
            if proj.monitor_thread_id:
                try:
                    ch = await self._client.fetch_channel(int(proj.monitor_thread_id))
                    if isinstance(ch, discord.Thread):
                        tasks.append(self._auto_follow_thread(ch, repo_name))
                except Exception:
                    pass
            if proj.verify_board_thread_id:
                try:
                    ch = await self._client.fetch_channel(int(proj.verify_board_thread_id))
                    if isinstance(ch, discord.Thread):
                        tasks.append(self._auto_follow_thread(ch, repo_name))
                except Exception:
                    pass
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.sleep(0.5)

        # Auto-follow personal forum threads
        cfg = load_access_config()  # refresh after possible archive creation
        for uid, ua in cfg.users.items():
            tasks = []
            if ua.control_thread_id:
                try:
                    ch = await self._client.fetch_channel(int(ua.control_thread_id))
                    if isinstance(ch, discord.Thread):
                        tasks.append(self._auto_follow_user_thread(ch, uid))
                except Exception:
                    pass
            if ua.archive_thread_id:
                try:
                    ch = await self._client.fetch_channel(int(ua.archive_thread_id))
                    if isinstance(ch, discord.Thread):
                        tasks.append(self._auto_follow_user_thread(ch, uid))
                except Exception:
                    pass
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.sleep(0.5)

    # --- Archive Thread ---

    async def ensure_archive_thread(self, repo_name: str) -> discord.Thread | None:
        """Get or create the archive thread inside a repo's forum."""
        proj = self._forum_projects.get(repo_name)
        if not proj or not proj.forum_channel_id:
            return None

        # Check existing — clear stale ID if thread was deleted
        if proj.archive_thread_id:
            try:
                ch = await self._client.fetch_channel(int(proj.archive_thread_id))
                if isinstance(ch, discord.Thread):
                    if ch.archived:
                        await ch.edit(archived=False)
                    return ch
            except discord.NotFound:
                log.info("Archive thread %s for %s was deleted, recreating",
                         proj.archive_thread_id, repo_name)
                self._archive_channel_ids.discard(int(proj.archive_thread_id))
                proj.archive_thread_id = None

        forum = self._client.get_channel(int(proj.forum_channel_id))
        if not forum or not isinstance(forum, discord.ForumChannel):
            return None

        thread, _ = await channels.create_archive_post(forum, repo_name)
        proj.archive_thread_id = str(thread.id)
        self._archive_channel_ids.add(thread.id)
        self.save_forum_map()
        await self._auto_follow_thread(thread, repo_name)
        return thread

    async def ensure_user_archive_thread(self, user_id: str) -> discord.Thread | None:
        """Get or create an archive thread in a user's personal forum."""
        cfg = load_access_config()
        ua = cfg.users.get(user_id)
        if not ua or not ua.forum_channel_id:
            return None

        # Check existing
        if ua.archive_thread_id:
            try:
                ch = await self._client.fetch_channel(int(ua.archive_thread_id))
                if isinstance(ch, discord.Thread):
                    if ch.archived:
                        await ch.edit(archived=False)
                    return ch
            except discord.NotFound:
                log.info("User archive thread %s for %s was deleted, recreating",
                         ua.archive_thread_id, ua.display_name)
                self._archive_channel_ids.discard(int(ua.archive_thread_id))
                ua.archive_thread_id = None
                access_mod.save_access_config(cfg)

        forum = self._client.get_channel(int(ua.forum_channel_id))
        if not forum or not isinstance(forum, discord.ForumChannel):
            return None

        thread, _ = await channels.create_archive_post(forum, ua.display_name)
        ua.archive_thread_id = str(thread.id)
        self._archive_channel_ids.add(thread.id)
        access_mod.save_access_config(cfg)
        await self._auto_follow_user_thread(thread, user_id)
        return thread

    # --- Verify Board ---

    def verify_lock(self, repo_name: str) -> asyncio.Lock:
        """Per-repo lock for verify_items mutations. Created on first use."""
        lock = self._verify_locks.get(repo_name)
        if lock is None:
            lock = asyncio.Lock()
            self._verify_locks[repo_name] = lock
        return lock

    async def ensure_verify_board(self, repo_name: str) -> discord.Thread | None:
        """Get or create the Verify Board pinned thread in a repo forum."""
        proj = self._forum_projects.get(repo_name)
        if not proj or not proj.forum_channel_id:
            return None

        if proj.verify_board_thread_id:
            try:
                ch = await self._client.fetch_channel(int(proj.verify_board_thread_id))
                if isinstance(ch, discord.Thread):
                    if ch.archived:
                        try:
                            await ch.edit(archived=False)
                        except Exception:
                            log.debug("Could not unarchive verify-board", exc_info=True)
                    return ch
            except discord.NotFound:
                log.info(
                    "Verify-board thread %s for %s was deleted, recreating",
                    proj.verify_board_thread_id, repo_name,
                )
                proj.verify_board_thread_id = None
                proj.verify_board_message_id = None

        forum = self._client.get_channel(int(proj.forum_channel_id))
        if not forum or not isinstance(forum, discord.ForumChannel):
            return None

        # Pass current items so the initial create renders them in one
        # API call (avoids a redundant edit for the crash-recovery case
        # where items exist in state before the board was provisioned).
        thread, msg = await channels.create_verify_board_post(
            forum, repo_name, proj.verify_items,
        )
        proj.verify_board_thread_id = str(thread.id)
        proj.verify_board_message_id = str(msg.id)
        self.save_forum_map()
        try:
            await self._auto_follow_thread(thread, repo_name)
        except Exception:
            log.debug("Failed to auto-follow verify-board thread", exc_info=True)
        return thread

    def schedule_verify_refresh(self, repo_name: str, *, delay: float = 0.5) -> None:
        """Debounced refresh — coalesces rapid mutations into one edit.

        Cancels any in-flight debounce for this repo and schedules a
        fresh `refresh_verify_board()` call after `delay` seconds.
        """
        existing = self._verify_debounce.get(repo_name)
        if existing and not existing.done():
            existing.cancel()

        async def _run() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            try:
                await self.refresh_verify_board(repo_name)
            except Exception:
                log.debug(
                    "Debounced verify-board refresh failed for %s",
                    repo_name, exc_info=True,
                )

        self._verify_debounce[repo_name] = asyncio.create_task(_run())

    async def refresh_verify_board(self, repo_name: str) -> None:
        """Re-render the Verify Board embed + view in place.

        Lazily creates the board thread if missing — items can
        accumulate before `reconcile_forums` runs (fresh repo added +
        session completes in the startup gap), and we don't want them
        to silently disappear from the UI.

        On NotFound (thread/message deleted) we clear the stored IDs
        and recreate the thread so the board stays reachable.
        """
        proj = self._forum_projects.get(repo_name)
        if not proj or not proj.forum_channel_id:
            return
        from bot.discord.verify_board import build_board_embed, build_board_view
        from bot.engine.verify import prune_old

        # Prune expired items before render (cheap, mutates in place)
        pruned = prune_old(proj.verify_items)
        if pruned:
            self.save_forum_map()

        # Lazy create if the board thread isn't provisioned yet.
        # `ensure_verify_board` now renders current items on create, so
        # once it returns we're done — no follow-up edit needed.
        # Mutations that happened during ensure's `await` will schedule
        # their own refresh and arrive via the normal edit path next.
        if not proj.verify_board_thread_id:
            try:
                await self.ensure_verify_board(repo_name)
            except Exception:
                log.debug(
                    "Lazy verify-board creation failed for %s",
                    repo_name, exc_info=True,
                )
            return

        thread = None
        try:
            try:
                thread = await self._client.fetch_channel(int(proj.verify_board_thread_id))
            except discord.NotFound:
                thread = None
            if not thread or not isinstance(thread, discord.Thread):
                log.info(
                    "Verify-board thread for %s was deleted, clearing stale IDs",
                    repo_name,
                )
                proj.verify_board_thread_id = None
                proj.verify_board_message_id = None
                self.save_forum_map()
                try:
                    await self.ensure_verify_board(repo_name)
                except Exception:
                    log.debug(
                        "Failed to recreate verify-board for %s",
                        repo_name, exc_info=True,
                    )
                return

            if thread.archived:
                try:
                    await thread.edit(archived=False)
                except Exception:
                    log.debug("Could not unarchive verify-board", exc_info=True)

            embed = build_board_embed(repo_name, proj.verify_items)
            view = build_board_view(repo_name, proj.verify_items)

            if not proj.verify_board_message_id:
                # No message yet — send and pin
                msg = await thread.send(embed=embed, view=view)
                proj.verify_board_message_id = str(msg.id)
                self.save_forum_map()
                return

            try:
                msg = await thread.fetch_message(int(proj.verify_board_message_id))
            except discord.NotFound:
                msg = await thread.send(embed=embed, view=view)
                proj.verify_board_message_id = str(msg.id)
                self.save_forum_map()
                return

            await msg.edit(embed=embed, view=view)
        except Exception:
            log.debug(
                "Failed to refresh verify-board for %s", repo_name, exc_info=True,
            )

    async def _migrate_archive_channel(
        self, repo_name: str, proj: ForumProject, guild: discord.Guild,
    ) -> None:
        """Migrate old archive text channel messages into the new forum thread.

        Called under _forum_lock. Copies all messages, deletes old channel on
        success, keeps it on failure.
        """
        old_ch = guild.get_channel(int(proj.archive_channel_id))
        if not old_ch or not isinstance(old_ch, discord.TextChannel):
            # Channel already gone — just mark migrated
            self._archive_channel_ids.discard(int(proj.archive_channel_id))
            proj.archive_channel_id = None
            proj.archive_migrated = True
            self.save_forum_map()
            log.info("Archive channel for %s already deleted, marking migrated",
                     repo_name)
            return

        try:
            archive_thread = await self._client.fetch_channel(int(proj.archive_thread_id))
        except Exception:
            log.warning("Cannot fetch archive thread for %s, skipping migration", repo_name)
            proj.archive_migrated = True
            self.save_forum_map()
            return

        try:
            messages = [m async for m in old_ch.history(limit=None, oldest_first=True)]
            for m in messages:
                if m.content:
                    await archive_thread.send(m.content)
                    await asyncio.sleep(0.5)
            await old_ch.delete(reason="Migrated to forum archive thread")
            self._archive_channel_ids.discard(int(proj.archive_channel_id))
            proj.archive_channel_id = None
            log.info("Migrated %d archive messages for %s, deleted old channel",
                     len(messages), repo_name)
        except Exception:
            log.warning("Archive migration failed for %s, keeping old channel",
                        repo_name, exc_info=True)

        proj.archive_migrated = True
        self.save_forum_map()

    async def post_archive_entry(self, channel_id: str) -> None:
        """Post a session summary + link to the archive thread.

        Uses a waterfall to find the best summary:
        1. CHANGELOG entries from DONE/COMMIT instance result file
        2. BUILD instance summary (first paragraph of build output)
        3. Most recent non-DONE instance summary
        4. Thread topic fallback
        """
        proj_info = self.thread_to_project(channel_id)
        if not proj_info:
            return
        proj, info = proj_info
        if not info.session_id:
            return

        from bot.claude.types import InstanceOrigin
        from bot.platform.formatting import parse_finalize_output

        candidates = [
            i for i in self._store.list_instances()
            if i.session_id == info.session_id and i.summary
        ]

        summary = None
        inst = None  # best instance for metadata (status, mode, duration, cost)
        finalize_info = None

        if candidates:
            candidates.sort(key=lambda i: i.finished_at or "", reverse=True)

            # Pass 1: CHANGELOG entries from DONE/COMMIT result file
            done_origins = {InstanceOrigin.DONE, InstanceOrigin.COMMIT}
            for c in candidates:
                if c.origin in done_origins and c.result_file:
                    try:
                        full_text = Path(c.result_file).read_text(encoding="utf-8")
                        fi = parse_finalize_output(full_text)
                        if fi and fi.changelog_entries:
                            summary = "\n".join(f"- {e}" for e in fi.changelog_entries)
                            finalize_info = fi
                            inst = c
                            break
                    except OSError:
                        pass

            # Pass 2: BUILD instance summary
            if not summary:
                for c in candidates:
                    if c.origin == InstanceOrigin.BUILD:
                        summary = c.summary
                        inst = c
                        break

            # Pass 3: Most recent instance, preferring non-DONE/COMMIT
            if not summary:
                for c in candidates:
                    if c.origin not in done_origins:
                        summary = c.summary
                        inst = c
                        break
                if not summary:
                    inst = candidates[0]
                    summary = inst.summary

        if not summary:
            summary = info.topic or "No summary"

        archive_thread = await self.ensure_archive_thread(proj.repo_name)

        if inst:
            status_emoji = "\u2705" if inst.status.value == "completed" else "\u274c"
            mode = inst.mode or "explore"
            if len(summary) > 200:
                summary = summary[:197] + "..."

            extras: list[str] = []
            if inst.duration_ms:
                secs = inst.duration_ms / 1000
                extras.append(f"{secs / 60:.0f}m" if secs >= 60 else f"{secs:.0f}s")
            if inst.cost_usd:
                extras.append(f"${inst.cost_usd:.2f}")
            if finalize_info and finalize_info.commit_hash:
                extras.append(f"`{finalize_info.commit_hash}`")
            if finalize_info and finalize_info.version:
                extras.append(finalize_info.version)
            elif inst.branch:
                extras.append(f"`{inst.branch}`")
            extra_str = f" \u2014 {', '.join(extras)}" if extras else ""

            msg = (
                f"**Session {inst.status.value} {status_emoji}** \u00b7 "
                f"{mode.title()}{extra_str}\n"
                f"{summary}\n"
                f"\U0001f517 <#{channel_id}>"
            )
        else:
            msg = f"**Session closed**\n{summary}\n\U0001f517 <#{channel_id}>"

        # Collect archive targets: repo archive + personal forum archive if applicable
        targets: list[discord.Thread] = []
        if archive_thread:
            targets.append(archive_thread)

        # Check if thread is in a personal forum — also post to that user's archive
        thread_ch = self._client.get_channel(int(channel_id))
        if thread_ch and isinstance(thread_ch, discord.Thread) and thread_ch.parent:
            user_info = self.is_user_forum(str(thread_ch.parent_id))
            if user_info:
                try:
                    personal_archive = await self.ensure_user_archive_thread(user_info[0])
                    if personal_archive and personal_archive not in targets:
                        targets.append(personal_archive)
                except Exception:
                    log.debug("Failed to get personal archive for user %s", user_info[0], exc_info=True)

        for target in targets:
            try:
                await target.send(msg)
            except discord.HTTPException:
                try:
                    await target.edit(archived=False)
                    await target.send(msg)
                except Exception:
                    log.debug("Failed to post archive entry to thread %s for session %s",
                              target.id, channel_id, exc_info=True)

    async def sync_cli_sessions(self, count: int) -> tuple[list[discord.Thread], list]:
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

            existing = self.session_to_thread(session_id)
            if existing:
                tid, info = existing
                if tid in updated_threads:
                    continue
                updated_threads.add(tid)
                ch = self._client.get_channel(int(tid))
                if not ch:
                    try:
                        ch = await self._client.fetch_channel(int(tid))
                    except (discord.NotFound, discord.Forbidden):
                        ch = None
                if ch and isinstance(ch, (discord.TextChannel, discord.Thread)):
                    await self.populate_thread_history(ch, session_id, tid)
                    populated.append(ch)
                continue

            log.info("Sync creating thread for session %s repo=%s", session_id[:12], repo_name)
            thread = await self.get_or_create_session_thread(
                repo_name, session_id, s["topic"], origin="cli",
            )
            if thread:
                created.append(thread)
                await self.populate_thread_history(thread, session_id, str(thread.id))

        return created, populated

    # --- Control Room ---

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

    async def _has_git_remote(self, repo_path: str) -> bool:
        """Check if the repo has any git remotes configured (cached)."""
        if not repo_path:
            return False
        if repo_path in self._remote_cache:
            return self._remote_cache[repo_path]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "remote"],
                cwd=repo_path, capture_output=True, text=True, **_NOWND,
            )
            has = result.returncode == 0 and bool(result.stdout.strip())
        except Exception:
            has = False
        self._remote_cache[repo_path] = has
        return has

    async def ensure_control_post(self, repo_name: str) -> None:
        """Ensure a control room post exists in the repo's forum.

        Called after get_or_create_forum() returns, outside the forum lock.
        Idempotent — checks control_thread_id before creating.
        """
        proj = self._forum_projects.get(repo_name)
        if not proj or proj.control_thread_id:
            return

        forum = self._client.get_channel(int(proj.forum_channel_id)) if proj.forum_channel_id else None
        if not forum or not isinstance(forum, discord.ForumChannel):
            return

        repos = self._store.list_repos()
        repo_path = repos.get(repo_name, "")
        branch = await self._get_repo_branch(repo_path)
        has_remote = await self._has_git_remote(repo_path)

        thread, msg = await channels.create_repo_control_post(
            forum, repo_name, repo_path, branch, has_remote=has_remote,
        )
        proj.control_thread_id = str(thread.id)
        proj.control_message_id = str(msg.id)
        self.save_forum_map()
        await self._auto_follow_thread(thread, repo_name)
        log.info("Created control room for repo %s (thread=%s)", repo_name, thread.id)

    async def ensure_user_control_post(
        self, user_id: str,
        forum: discord.ForumChannel | None = None,
    ) -> None:
        """Ensure a control room post exists in a user's personal forum."""
        cfg = load_access_config()
        ua = cfg.users.get(user_id)
        if not ua or ua.control_thread_id:
            return

        if not forum:
            if not ua.forum_channel_id:
                return
            forum = self._client.get_channel(int(ua.forum_channel_id))
        if not forum or not isinstance(forum, discord.ForumChannel):
            return

        repo_names = list(ua.repos.keys())

        thread, msg = await channels.create_user_control_post(
            forum, ua.display_name, repo_names,
        )
        ua.control_thread_id = str(thread.id)
        ua.control_message_id = str(msg.id)
        access_mod.save_access_config(cfg)
        self._user_control_thread_ids.add(str(thread.id))
        await self._auto_follow_user_thread(thread, user_id)
        log.info("Created control room for user %s (thread=%s)", ua.display_name, thread.id)

    async def cleanup_all_control_rooms(self) -> None:
        """Delete orphaned messages from all control room threads on startup.

        Runs as a fire-and-forget task — must not block bot readiness.
        Deletes any message that isn't the pinned control embed, plus any
        persisted deploy status message IDs from failed deletions.
        """
        # First, clean up any persisted deploy status msg IDs
        state = self._store.get_platform_state("discord")
        pending_msgs = state.pop("deploy_status_msgs", {})
        if pending_msgs:
            self._store.set_platform_state("discord", state, persist=True)
            for repo_name, msg_id in pending_msgs.items():
                await self._delete_message_safe(repo_name, msg_id)

        # Then purge non-embed messages from each control room thread
        # Snapshot to avoid RuntimeError if dict is mutated during async iteration
        for repo_name, proj in list(self._forum_projects.items()):
            if not proj.control_thread_id or not proj.control_message_id:
                continue
            try:
                await self._cleanup_control_room(
                    proj.control_thread_id, proj.control_message_id,
                )
            except Exception:
                log.debug("Control room cleanup failed for %s", repo_name, exc_info=True)

    async def _cleanup_control_room(self, thread_id: str, embed_msg_id: str) -> None:
        """Delete all messages in a control room thread except the embed."""
        try:
            thread = self._client.get_channel(int(thread_id))
            if not thread:
                thread = await self._client.fetch_channel(int(thread_id))
        except (discord.NotFound, discord.HTTPException):
            return
        if not isinstance(thread, discord.Thread):
            return

        embed_id = int(embed_msg_id)
        deleted = 0
        async for message in thread.history(limit=50):
            if message.id == embed_id:
                continue
            try:
                await message.delete()
                deleted += 1
            except (discord.NotFound, discord.HTTPException):
                pass  # Already gone or too old (>14 days)
            except Exception:
                log.debug("Failed to delete msg %s in control room", message.id, exc_info=True)
        if deleted:
            log.info("Cleaned up %d orphaned messages from control room %s", deleted, thread_id)

    async def _delete_message_safe(self, repo_name: str, msg_id: int) -> None:
        """Best-effort delete a single message by ID from a repo's control room."""
        proj = self._forum_projects.get(repo_name)
        if not proj or not proj.control_thread_id:
            return
        try:
            thread = self._client.get_channel(int(proj.control_thread_id))
            if not thread:
                thread = await self._client.fetch_channel(int(proj.control_thread_id))
            msg = await thread.fetch_message(msg_id)
            await msg.delete()
            log.info("Deleted persisted deploy status msg %s for %s", msg_id, repo_name)
        except (discord.NotFound, discord.HTTPException):
            pass  # Already gone
        except Exception:
            log.debug("Failed to delete persisted msg %s", msg_id, exc_info=True)

    async def refresh_control_room(self, repo_name: str, *, usage_bar: str | None = None,
                                   drain_status: str | None = None) -> None:
        """Update the control room embed for a repo forum."""
        proj = self._forum_projects.get(repo_name)
        if not proj or not proj.control_thread_id or not proj.control_message_id:
            return
        thread = None
        try:
            try:
                thread = await self._client.fetch_channel(int(proj.control_thread_id))
            except discord.NotFound:
                thread = None
            if not thread or not isinstance(thread, discord.Thread):
                log.info("Control room thread for %s was deleted, clearing stale IDs", repo_name)
                proj.control_thread_id = None
                proj.control_message_id = None
                self.save_forum_map()
                return
            # Migrate old names to current Control Room name
            if thread.name in ("Control Center", "Control Room"):
                try:
                    await thread.edit(name=channels.CONTROL_ROOM_NAME)
                    log.info("Renamed %s -> %s (thread=%s)", thread.name, channels.CONTROL_ROOM_NAME, thread.id)
                except Exception:
                    pass
            msg = await thread.fetch_message(int(proj.control_message_id))

            from bot.claude.types import InstanceStatus

            repo_instances = self._store.list_by_repo(repo_name)
            running = [i for i in repo_instances if i.status == InstanceStatus.RUNNING]
            attention = [i for i in self._store.needs_attention() if i.repo_name == repo_name]
            completed = [i for i in repo_instances
                         if i.status == InstanceStatus.COMPLETED and not i.needs_input][:5]

            # Build session->thread map for this repo
            session_to_thread: dict[str, str] = {
                ti.session_id: ti.thread_id
                for ti in proj.threads.values()
                if ti.session_id
            }

            repos = self._store.list_repos()
            repo_path = repos.get(repo_name, "")
            branch = await self._get_repo_branch(repo_path)
            has_remote = await self._has_git_remote(repo_path)
            today_cost = self._store.get_repo_daily_cost(repo_name)

            ds = self._store.get_deploy_state(repo_name)
            # Build instance_id -> thread_id map for deploy state session links.
            # pending_sessions stores instance IDs (e.g. "t-523"), but threads
            # are keyed by session_id (UUID). Bridge via instance lookup.
            deploy_thread_ids: dict[str, str] = {}
            if ds and ds.pending_sessions:
                for inst_id in ds.pending_sessions:
                    inst = self._store.get_instance(inst_id)
                    if inst and inst.session_id and inst.session_id in session_to_thread:
                        deploy_thread_ids[inst_id] = session_to_thread[inst.session_id]
            # Use threaded usage_bar if provided (from dashboard cascade),
            # otherwise fetch independently (standalone refresh).
            if usage_bar is None:
                from bot.engine.usage import get_usage_bar_async
                try:
                    usage_bar = await get_usage_bar_async()
                except Exception:
                    pass
            dc = self._store.get_deploy_config(repo_name)
            embed = channels.build_control_embed(
                repo_name, repo_path, branch,
                running_instances=running,
                attention_instances=attention,
                completed_instances=completed,
                session_to_thread=session_to_thread,
                today_cost=today_cost,
                deploy_state=ds,
                deploy_thread_ids=deploy_thread_ids,
                usage_bar=usage_bar,
                drain_status=drain_status,
            )
            view = channels.build_control_view(
                repo_name,
                active_count=len(running),
                deploy_state=ds,
                deploy_config=dc,
                has_remote=has_remote,
            )
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            log.info("Control room message for %s was deleted, recreating", repo_name)
            proj.control_thread_id = None
            proj.control_message_id = None
            self.save_forum_map()
            try:
                if thread and isinstance(thread, discord.Thread):
                    await thread.delete()
            except Exception:
                pass
            try:
                await self.ensure_control_post(repo_name)
            except Exception:
                log.debug("Failed to recreate control room for %s", repo_name, exc_info=True)
        except Exception:
            log.debug("Failed to refresh control room for %s", repo_name, exc_info=True)

    async def refresh_user_control_room(self, user_id: str) -> None:
        """Update the control room embed for a user's personal forum."""
        cfg = load_access_config()
        ua = cfg.users.get(user_id)
        if not ua or not ua.control_thread_id or not ua.control_message_id:
            return
        thread = None
        try:
            try:
                thread = await self._client.fetch_channel(int(ua.control_thread_id))
            except discord.NotFound:
                thread = None
            if not thread or not isinstance(thread, discord.Thread):
                log.info("Control room thread for user %s was deleted, clearing stale IDs", ua.display_name)
                self._user_control_thread_ids.discard(ua.control_thread_id)
                ua.control_thread_id = None
                ua.control_message_id = None
                access_mod.save_access_config(cfg)
                return
            # Migrate old names to current Control Room name
            if thread.name in ("Control Center", "Control Room"):
                try:
                    await thread.edit(name=channels.CONTROL_ROOM_NAME)
                    log.info("Renamed %s -> %s (thread=%s)", thread.name, channels.CONTROL_ROOM_NAME, thread.id)
                except Exception:
                    pass
            msg = await thread.fetch_message(int(ua.control_message_id))

            repo_names = list(ua.repos.keys())

            embed = channels.build_user_control_embed(ua.display_name, repo_names)
            view = channels.build_user_control_view(repo_names)
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            log.info("Control room message for user %s was deleted, recreating", ua.display_name)
            self._user_control_thread_ids.discard(ua.control_thread_id)
            ua.control_thread_id = None
            ua.control_message_id = None
            access_mod.save_access_config(cfg)
            try:
                if thread and isinstance(thread, discord.Thread):
                    await thread.delete()
            except Exception:
                pass
            try:
                await self.ensure_user_control_post(user_id)
            except Exception:
                log.debug("Failed to recreate control room for user %s", user_id, exc_info=True)
        except Exception:
            log.debug("Failed to refresh control room for user %s", user_id, exc_info=True)

    # --- Verify Board ---

    def _verify_lock(self, repo_name: str) -> asyncio.Lock:
        """Get or create a per-repo lock for verify-board mutations."""
        lock = self._verify_locks.get(repo_name)
        if lock is None:
            lock = asyncio.Lock()
            self._verify_locks[repo_name] = lock
        return lock

    async def ensure_verify_board(self, repo_name: str) -> discord.Thread | None:
        """Ensure the pinned verify-board thread exists for a repo's forum.

        Serialized via the per-repo verify lock so concurrent reconcile +
        first-tap can't create duplicate boards.
        """
        proj = self._forum_projects.get(repo_name)
        if not proj or not proj.forum_channel_id:
            return None

        async with self._verify_lock(repo_name):
            # Re-check after acquiring lock
            if proj.verify_board_thread_id:
                try:
                    ch = await self._client.fetch_channel(int(proj.verify_board_thread_id))
                    if isinstance(ch, discord.Thread):
                        if ch.archived:
                            try:
                                await ch.edit(archived=False)
                            except Exception:
                                pass
                        return ch
                except discord.NotFound:
                    log.info("Verify-board thread %s for %s was deleted, recreating",
                             proj.verify_board_thread_id, repo_name)
                    proj.verify_board_thread_id = None
                    proj.verify_board_message_id = None

            forum = self._client.get_channel(int(proj.forum_channel_id))
            if not forum or not isinstance(forum, discord.ForumChannel):
                return None

            thread, msg = await channels.create_verify_board_post(
                forum, proj, guild_id=self._guild_id,
            )
            proj.verify_board_thread_id = str(thread.id)
            proj.verify_board_message_id = str(msg.id)
            self.save_forum_map()

        try:
            await self._auto_follow_thread(thread, repo_name)
        except Exception:
            log.debug("Failed to auto-follow verify-board thread %s", thread.id)
        return thread

    async def refresh_verify_board(self, repo_name: str) -> None:
        """Re-render the verify-board embed for a repo (out-of-lock Discord edit)."""
        from bot.discord import verify_board as vb_mod

        proj = self._forum_projects.get(repo_name)
        if not proj or not proj.verify_board_thread_id or not proj.verify_board_message_id:
            return
        try:
            thread = await self._client.fetch_channel(int(proj.verify_board_thread_id))
        except discord.NotFound:
            log.info("Verify-board thread for %s was deleted, clearing IDs", repo_name)
            proj.verify_board_thread_id = None
            proj.verify_board_message_id = None
            self.save_forum_map()
            return
        if not isinstance(thread, discord.Thread):
            return

        embed = vb_mod.build_board_embed(proj, self._guild_id)
        view = vb_mod.build_board_view(repo_name, proj)
        try:
            msg = await thread.fetch_message(int(proj.verify_board_message_id))
            await msg.edit(embed=embed, view=view)
        except discord.NotFound:
            log.info("Verify-board message for %s was deleted, recreating thread", repo_name)
            proj.verify_board_thread_id = None
            proj.verify_board_message_id = None
            self.save_forum_map()
            try:
                await self.ensure_verify_board(repo_name)
            except Exception:
                log.debug("Failed to recreate verify-board for %s", repo_name, exc_info=True)
        except Exception:
            log.debug("Failed to refresh verify-board for %s", repo_name, exc_info=True)

    async def _mutate_verify(self, repo_name: str, mutator):
        """Run `mutator(proj.verify_items)` under the repo lock, then save + refresh.

        The mutator runs in-memory only — no Discord awaits inside the lock.
        Pruning happens under the same lock so it can't clobber concurrent
        appends. After release, `refresh_verify_board` is fired as a background
        task so the caller doesn't block on a Discord edit.
        Returns whatever the mutator returns.
        """
        from bot.engine import verify as verify_mod

        proj = self._forum_projects.get(repo_name)
        if not proj:
            return None
        async with self._verify_lock(repo_name):
            result = mutator(proj.verify_items)
            try:
                verify_mod.prune_old(proj.verify_items)
            except Exception:
                log.debug("verify prune failed for %s", repo_name, exc_info=True)
            self.save_forum_map()
        asyncio.create_task(self.refresh_verify_board(repo_name))
        return result

    # --- Session Callbacks ---

    def set_thread_session(self, thread_id: str, session_id: str) -> None:
        """Write session_id to ThreadInfo immediately (called from engine callback).

        Always overwrites: the engine fires this once per successful run with the
        current canonical session_id, so the latest value wins. This is critical
        for account failover — the old session lives on the exhausted account and
        must be replaced with the new account's session_id, otherwise every
        subsequent resume hits "No conversation found" (dementia bug).
        """
        lookup = self.thread_to_project(thread_id)
        if not lookup:
            return
        _, info = lookup
        if info.session_id == session_id:
            return
        if info.session_id:
            log.info(
                "Thread %s session rebind %s -> %s",
                thread_id, info.session_id[:12], session_id[:12],
            )
            # Cached priming digest is stale once we swap to a new session.
            self._prime_cache.pop(thread_id, None)
        else:
            log.info("Session resolved for thread %s -> %s", thread_id, session_id[:12])
        info.session_id = session_id
        self.save_forum_map()

    def attach_session_callbacks(self, ctx: RequestContext, thread_info: ThreadInfo, thread_id: str) -> None:
        """Wire up session resolution callbacks on a RequestContext."""
        ctx.resolve_session_id = lambda _info=thread_info: _info.session_id or None
        ctx.on_session_resolved = lambda sid, _tid=thread_id: self.set_thread_session(_tid, sid)
        ctx.maybe_prime_briefing = lambda _tid=thread_id: self.build_prime_briefing(_tid)
        ctx.invalidate_prime = lambda _tid=thread_id: self.clear_prime_briefing(_tid)

    def clear_prime_briefing(self, thread_id: str) -> None:
        """Invalidate the cached briefing for a thread (e.g. after a failed run)."""
        self._prime_cache.pop(thread_id, None)

    async def build_prime_briefing(self, thread_id: str) -> str | None:
        """Read recent Discord history, return a brief context summary or None.

        Quoted messages are wrapped in nonced fence delimiters; the nonce is
        scrubbed from quoted content first so user-supplied text cannot
        reproduce the closing marker. Cache lives in-memory only.
        """
        cached = self._prime_cache.get(thread_id)
        if cached:
            return cached

        lookup = self.thread_to_project(thread_id)
        if not lookup:
            return None
        _, info = lookup

        # Skip autopilot threads (non-interactive).
        if info.session_id and self._store.get_autopilot_chain(info.session_id):
            return None

        ch = self._client.get_channel(int(thread_id))
        if not ch:
            try:
                ch = await self._client.fetch_channel(int(thread_id))
            except (discord.NotFound, discord.Forbidden):
                return None
        if not isinstance(ch, discord.Thread):
            return None

        # Bot/webhook role-assignment parity with on_message (bot.py:1040-1047):
        # TEST_WEBHOOK_IDS messages are user-shaped, even though m.author.bot is True.
        test_webhook_ids = set(config.TEST_WEBHOOK_IDS or ())

        def _is_user_msg(m: discord.Message) -> bool:
            if m.webhook_id and str(m.webhook_id) in test_webhook_ids:
                return True
            if self._client.user and m.author.id == self._client.user.id:
                return False
            return not m.author.bot

        HARD_BYTE_BUDGET = 64 * 1024
        msgs: list[tuple[str, str]] = []
        total_bytes = 0
        try:
            async for m in ch.history(limit=50, oldest_first=False):
                text = (m.content or "").strip()
                if not text:
                    continue
                # Strip BOT_CMD-shaped lines so quoted content cannot replay
                # through the Tier-2 [BOT_CMD: ...] dispatcher.
                text = "\n".join(
                    ln for ln in text.splitlines()
                    if not ln.lstrip().startswith("[BOT_CMD:")
                ).strip()
                if not text:
                    continue
                role = "user" if _is_user_msg(m) else "bot"
                size = len(text.encode("utf-8"))
                if total_bytes + size > HARD_BYTE_BUDGET:
                    break
                total_bytes += size
                msgs.append((role, text))
        except (discord.HTTPException, discord.Forbidden):
            log.warning("Failed to read thread history for priming %s", thread_id, exc_info=True)
            return None

        msgs.reverse()  # chronological

        # Drop the most recent user message (the one being processed now) and
        # any bot messages that came after it — notably the "Reconstructing
        # context…" status sent moments before this read, but also any idle
        # probes or autopilot pings that may have landed in between.
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i][0] == "user":
                msgs = msgs[:i]
                break

        # Tightened caps to stay inside ~500-token brief budget:
        # 3 user × 350 chars + 1 bot × 200 chars + 200 topic + header ≈ 450 tokens
        last_user = [t for r, t in msgs if r == "user"][-3:]
        last_bot = [t for r, t in msgs if r == "bot"][-1:]
        if not last_user and not last_bot:
            return None  # degenerate

        # Per-briefing nonce — quoted text cannot reproduce the closing fence
        # because we strip any occurrence of the nonce from quoted content
        # before wrapping. The nonce also appears in the prompt header so
        # the session knows which fences delimit untrusted quoted data.
        nonce = secrets.token_hex(8)
        fence_open = f"<<<PRIOR-{nonce}"
        fence_close = f"PRIOR-{nonce}>>>"

        def _scrub(t: str) -> str:
            return t.replace(nonce, "").replace("PRIOR-", "PRIOR_")

        parts: list[str] = [f"NONCE: {nonce}"]
        if info.topic:
            parts.append(
                f"{fence_open} kind=thread_topic\n{_scrub(info.topic[:200])}\n{fence_close}"
            )
        for t in last_user:
            parts.append(
                f"{fence_open} kind=prior_user_msg\n{_scrub(t[:350])}\n{fence_close}"
            )
        for t in last_bot:
            snippet = t[:200].replace("\n", " ")
            parts.append(
                f"{fence_open} kind=prior_bot_msg\n{_scrub(snippet)}…\n{fence_close}"
            )

        summary = "\n".join(parts)
        self._prime_cache[thread_id] = summary
        log.info(
            "Built prime briefing for thread %s (%d chars, %d user / %d bot)",
            thread_id, len(summary), len(last_user), len(last_bot),
        )
        return summary

    def persist_ctx_settings(self, ctx: RequestContext) -> None:
        """Write any ctx setting overrides back to ThreadInfo for persistence."""
        lookup = self.thread_to_project(ctx.channel_id)
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
        if ctx.effort is not None and ctx.effort != info.effort:
            info.effort = ctx.effort
            changed = True
        if changed:
            self.save_forum_map()

    # --- Thread History ---

    async def populate_thread_history(
        self, channel: discord.TextChannel | discord.Thread, session_id: str,
        thread_id: str,
        *, force: bool = False, cli_label: bool = False,
        messenger: DiscordMessenger | None = None,
    ) -> None:
        """Send messages from a session into a thread/channel."""
        lookup = self.thread_to_project(thread_id)
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

        # Use messenger if provided for proper formatting
        if messenger:
            ch_id = str(channel.id)
            for msg in to_send:
                role = user_label if msg["role"] == "user" else bot_label
                text = msg["text"]
                if len(text) > 800:
                    text = text[:800] + "\u2026"
                try:
                    markup = messenger.markdown_to_markup(f"{role}:\n{text}")
                    await messenger.send_text(ch_id, markup, silent=True)
                except Exception:
                    try:
                        await messenger.send_text(ch_id, f"{role}:\n{text[:800]}", silent=True)
                    except Exception:
                        break
        else:
            # Fallback: send raw via channel.send
            for msg in to_send:
                role = user_label if msg["role"] == "user" else bot_label
                text = msg["text"]
                if len(text) > 800:
                    text = text[:800] + "\u2026"
                try:
                    await channel.send(f"{role}:\n{text[:1900]}")
                except Exception:
                    break

        if lookup:
            lookup[1]._synced_msg_count = total
            self.save_forum_map()

    # --- Pending Thread Finalization ---

    async def finalize_pending_thread(
        self, thread_id: str, thread: discord.Thread, prompt: str,
    ) -> None:
        """After first query in a /new thread, update session mapping and rename."""
        lookup = self.thread_to_project(thread_id)
        if lookup:
            _, info = lookup
            if info.session_id:
                info.topic = prompt
                self.save_forum_map()
            else:
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
                            self.save_forum_map()
                            log.info("Finalized pending thread %s -> session %s (fallback)",
                                     thread_id, inst.session_id)
                            break

    async def update_pending_thread(self, thread_id: str) -> None:
        """After a query completes, update a pending thread's session mapping."""
        lookup = self.thread_to_project(thread_id)
        if not lookup:
            return
        proj, info = lookup
        if info.session_id:
            return

        ch = self._client.get_channel(int(thread_id))
        if not ch:
            try:
                ch = await self._client.fetch_channel(int(thread_id))
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
                    info.session_id = inst.session_id
                    info.origin = "bot"
                    self.save_forum_map()
                    log.info("Mapped thread %s -> session %s (repo: %s)",
                             thread_id, inst.session_id, proj.repo_name)
                    return
                except (discord.NotFound, discord.HTTPException):
                    continue

    def get_latest_summary(self, thread_id: str) -> str:
        """Get summary from the most recent instance for this thread's session."""
        lookup = self.thread_to_project(thread_id)
        if not lookup:
            return ""
        _, info = lookup
        if not info.session_id:
            return ""
        for inst in self._store.list_instances()[:10]:
            if inst.session_id == info.session_id and inst.summary:
                return inst.summary
        return ""

    # --- Ref Embed ---

    def build_ref_embed(
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

        per_msg = max(150, 4000 // max(len(msgs), 1))
        lines = []
        for m in msgs:
            role = "**You**" if m["role"] == "user" else "**Claude**"
            text = m["text"][:per_msg] + ("..." if len(m["text"]) > per_msg else "")
            lines.append(f"{role}: {text}")
        embed.description = "\n\n".join(lines)[:4096]
        return embed

    @staticmethod
    def build_ref_context(
        proj: ForumProject, info: ThreadInfo,
        msgs: list[dict], target_thread_id: str,
    ) -> str:
        """Build plain-text context string for prompt injection into Claude."""
        topic = info.topic or f"Thread #{target_thread_id[-6:]}"
        lines = [f"--- Referenced conversation from [{proj.repo_name}] \"{topic}\" ---"]
        for m in msgs:
            role = "You" if m["role"] == "user" else "Claude"
            text = m["text"][:2000]
            lines.append(f"{role}: {text}")
        lines.append("--- End reference ---")
        return "\n".join(lines)
