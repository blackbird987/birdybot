"""Forum-based session management: ForumProject, ThreadInfo, ForumManager.

Owns all forum/thread data structures, lookups, creation, sync, and
history population. ClaudeBot delegates forum operations through
ForumManager's public interface.
"""

from __future__ import annotations

import asyncio
import logging
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
    archive_channel_id: str | None = None

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
        if self.archive_channel_id:
            d["archive_channel_id"] = self.archive_channel_id
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
            archive_channel_id=data.get("archive_channel_id"),
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

    # --- Forum-Session Mapping ---

    def load_forum_map(self) -> None:
        """Load forum->project mapping from platform_state."""
        state = self._store.get_platform_state("discord")
        raw = state.get("forum_projects", {})
        self._forum_projects = {
            k: ForumProject.from_dict(v) for k, v in raw.items()
        }
        self._archive_channel_ids = {
            int(p.archive_channel_id)
            for p in self._forum_projects.values()
            if p.archive_channel_id
        }
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
                    await channels.create_user_welcome_post(forum, display_name, repo_names)
                    ua.welcome_posted = True
                    access_mod.save_access_config(cfg)
                except Exception:
                    log.warning("Failed to create welcome post in forum %s for user %s, will retry next startup",
                                forum.id, user_id, exc_info=True)
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
        """Get or create a forum channel for a repo."""
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

        async with self._thread_lock:
            # Double-check after lock (another message may have created it)
            if session_id:
                result = self.session_to_thread(session_id)
                if result:
                    tid, _ = result
                    try:
                        ch = await self._client.fetch_channel(int(tid))
                        if isinstance(ch, discord.Thread):
                            return ch
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

        # Ensure archive channels exist for all repo forums
        for repo_name, proj in self._forum_projects.items():
            if proj.forum_channel_id and not proj.archive_channel_id:
                try:
                    await self.ensure_archive_channel(repo_name)
                except Exception:
                    log.debug("Failed to create archive channel for %s",
                              repo_name, exc_info=True)

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

    # --- Archive Channel ---

    async def ensure_archive_channel(self, repo_name: str) -> discord.TextChannel | None:
        """Get or create the archive text channel for a repo."""
        guild = self._client.get_guild(self._guild_id)
        if not guild or not self._category_id:
            return None
        category = guild.get_channel(self._category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            return None

        proj = self._forum_projects.get(repo_name)
        if not proj:
            return None

        # Check existing — clear stale ID if channel was deleted
        if proj.archive_channel_id:
            ch = guild.get_channel(int(proj.archive_channel_id))
            if ch and isinstance(ch, discord.TextChannel):
                return ch
            log.info("Archive channel %s for %s was deleted, recreating",
                     proj.archive_channel_id, repo_name)
            self._archive_channel_ids.discard(int(proj.archive_channel_id))
            proj.archive_channel_id = None

        ch = await channels.ensure_archive_channel(guild, category, repo_name)
        proj.archive_channel_id = str(ch.id)
        self._archive_channel_ids.add(ch.id)
        self.save_forum_map()
        return ch

    async def post_archive_entry(self, channel_id: str) -> None:
        """Post a session summary + link to the archive channel.

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

        archive_ch = await self.ensure_archive_channel(proj.repo_name)
        if not archive_ch:
            return

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

        try:
            await archive_ch.send(msg)
        except Exception:
            log.debug("Failed to post archive entry for thread %s",
                      channel_id, exc_info=True)

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
        log.info("Created control room for user %s (thread=%s)", ua.display_name, thread.id)

    async def refresh_control_room(self, repo_name: str, *, usage_bar: str | None = None) -> None:
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
            attention = [i for i in repo_instances
                         if i.status == InstanceStatus.FAILED or i.needs_input]
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

    # --- Session Callbacks ---

    def set_thread_session(self, thread_id: str, session_id: str) -> None:
        """Write session_id to ThreadInfo immediately (called from engine callback)."""
        lookup = self.thread_to_project(thread_id)
        if lookup:
            _, info = lookup
            if not info.session_id:
                info.session_id = session_id
                self.save_forum_map()
                log.info("Session resolved for thread %s -> %s", thread_id, session_id[:12])

    def attach_session_callbacks(self, ctx: RequestContext, thread_info: ThreadInfo, thread_id: str) -> None:
        """Wire up session resolution callbacks on a RequestContext."""
        ctx.resolve_session_id = lambda _info=thread_info: _info.session_id or None
        ctx.on_session_resolved = lambda sid, _tid=thread_id: self.set_thread_session(_tid, sid)

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
