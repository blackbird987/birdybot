"""Discord bot with slash commands, message handler, and persistent views.

Forum-based architecture: one ForumChannel per project, one thread per session.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from bot import config
from bot.discord import channels
from bot.discord.adapter import DiscordMessenger
from bot.engine import commands
from bot.engine import sessions as sessions_mod
from bot.platform.base import RequestContext
from bot.platform.formatting import MODE_DISPLAY, VALID_MODES, mode_label, mode_name

if TYPE_CHECKING:
    from bot.claude.runner import ClaudeRunner
    from bot.monitor.service import MonitorService
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


# --- Data structures ---


@dataclass
class ThreadInfo:
    thread_id: str
    session_id: str | None = None
    origin: str = "bot"           # "bot" or "cli"
    topic: str = ""
    _synced_msg_count: int = 0

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "session_id": self.session_id,
            "origin": self.origin,
            "topic": self.topic,
            "_synced_msg_count": self._synced_msg_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ThreadInfo:
        return cls(
            thread_id=data["thread_id"],
            session_id=data.get("session_id"),
            origin=data.get("origin", "bot"),
            topic=data.get("topic", ""),
            _synced_msg_count=data.get("_synced_msg_count", 0),
        )


@dataclass
class ForumProject:
    repo_name: str
    forum_channel_id: str
    threads: dict[str, ThreadInfo] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "repo_name": self.repo_name,
            "forum_channel_id": self.forum_channel_id,
            "threads": {k: v.to_dict() for k, v in self.threads.items()},
        }

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
        )


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
        self._dashboard_last_refresh: float = 0.0  # monotonic timestamp for debounce

        self._monitor_service: MonitorService | None = None
        self._monitor_started: bool = False
        self._notifier = None  # set by app.py after notifier is created
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

    def _auth(self, user_id: int) -> bool:
        if self._discord_user_id:
            return user_id == self._discord_user_id
        return True

    def _ctx(
        self, channel_id: str,
        session_id: str | None = None,
        repo_name: str | None = None,
    ) -> RequestContext:
        return RequestContext(
            messenger=self.messenger,
            channel_id=channel_id,
            platform="discord",
            store=self._store,
            runner=self._runner,
            session_id=session_id,
            repo_name=repo_name,
        )

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
        ctx = self._ctx(channel_id, session_id=session_id, repo_name=repo_name)
        await commands.on_text(ctx, prompt)
        asyncio.create_task(self._try_apply_tags_after_run(channel_id))
        asyncio.create_task(self._refresh_dashboard())

    # --- Forum-Session Mapping ---

    def _load_forum_map(self) -> None:
        """Load forum→project mapping from platform_state, with migration from old format."""
        state = self._store.get_platform_state("discord")

        # Migration: convert old channel_sessions to forum_projects
        old_cs = state.get("channel_sessions")
        if old_cs and "forum_projects" not in state:
            log.info("Migrating %d channel_sessions to forum_projects format", len(old_cs))
            migrated: dict[str, ForumProject] = {}
            for ch_id, val in old_cs.items():
                if isinstance(val, str):
                    val = {"session_id": val, "repo_name": ""}
                repo = val.get("repo_name", "")
                if not repo:
                    repo = "_default"
                if repo not in migrated:
                    migrated[repo] = ForumProject(
                        repo_name=repo,
                        forum_channel_id="",  # created on first use
                    )
                thread_info = ThreadInfo(
                    thread_id=ch_id,  # old channel ID as placeholder
                    session_id=val.get("session_id"),
                    origin=val.get("origin", "cli"),
                    topic=val.get("topic", ""),
                    _synced_msg_count=val.get("_synced_msg_count", 0),
                )
                migrated[repo].threads[ch_id] = thread_info
            self._forum_projects = migrated
            # Remove old key
            state.pop("channel_sessions", None)
            state["forum_projects"] = {k: v.to_dict() for k, v in migrated.items()}
            self._store.set_platform_state("discord", state, persist=True)
            log.info("Migration complete: %d projects", len(migrated))
            return

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
    ) -> discord.Thread | None:
        """Find existing thread for session, or create a new one in the repo's forum."""
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

            # Store mapping
            proj = self._forum_projects[repo_name]
            proj.threads[str(thread.id)] = ThreadInfo(
                thread_id=str(thread.id),
                session_id=session_id,
                origin=origin,
                topic=topic,
            )
            self._save_forum_map()
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
        """Validate forum channels on startup. Clean stale mappings, archive old text channels."""
        guild = self.get_guild(self._guild_id)
        if not guild or not self._category_id:
            return
        category = guild.get_channel(self._category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            return

        # Phase 6.2: Auto-archive old text channels (migration cleanup)
        for ch in category.text_channels:
            if ch.id == self._lobby_channel_id:
                continue
            if ch.topic and ch.topic.startswith("monitor:"):
                continue
            log.info("Archiving old text channel %s (%s)", ch.id, ch.name)
            await channels.archive_session_channel(ch)

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

        # Discover unmapped forums in category
        for ch in category.channels:
            if not isinstance(ch, discord.ForumChannel):
                continue
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

        # Migrate thread names: strip legacy "repo│" prefix
        for proj in valid_projects.values():
            if not proj.forum_channel_id:
                continue
            forum = guild.get_channel(int(proj.forum_channel_id))
            if not forum or not isinstance(forum, discord.ForumChannel):
                continue
            for thread in forum.threads:
                if "\u2502" in thread.name:  # │ (box-drawing vertical)
                    new_name = thread.name.split("\u2502", 1)[1].strip()
                    if new_name:
                        old_name = thread.name
                        try:
                            await thread.edit(name=new_name[:100])
                            log.info("Renamed thread %s: %s -> %s", thread.id, old_name, new_name)
                        except Exception:
                            log.debug("Failed to rename thread %s", thread.id, exc_info=True)

    async def _run_slash(
        self, interaction: discord.Interaction, coro,
        *, ephemeral: bool = False,
    ) -> None:
        """Defer, run engine command, then delete the 'thinking' response."""
        cmd_name = interaction.command.name if interaction.command else "?"
        log.info("Discord /%s in #%s by %s", cmd_name, getattr(interaction.channel, "name", "?"), interaction.user)
        await interaction.response.defer(ephemeral=ephemeral)
        ctx = self._ctx(str(interaction.channel_id))
        await coro(ctx)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

    def _setup_commands(self) -> None:
        """Register slash commands."""
        guild_obj = discord.Object(id=self._guild_id)

        @self.tree.command(name="status", description="Health dashboard", guild=guild_obj)
        async def cmd_status(interaction: discord.Interaction):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_status)

        @self.tree.command(name="cost", description="Spending breakdown", guild=guild_obj)
        async def cmd_cost(interaction: discord.Interaction):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_cost)

        @self.tree.command(name="list", description="Show instances", guild=guild_obj)
        @app_commands.describe(scope="Show all instances or just recent")
        async def cmd_list(interaction: discord.Interaction, scope: str = ""):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_list(ctx, scope))

        @self.tree.command(name="bg", description="Background task (build mode)", guild=guild_obj)
        @app_commands.describe(prompt="Task description")
        async def cmd_bg(interaction: discord.Interaction, prompt: str):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_bg(ctx, prompt))

        @self.tree.command(name="release", description="Cut a versioned release", guild=guild_obj)
        @app_commands.describe(level="patch, minor, major, or explicit version (default: patch)")
        async def cmd_release(interaction: discord.Interaction, level: str = "patch"):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_release(ctx, level))

        @self.tree.command(name="kill", description="Terminate instance", guild=guild_obj)
        @app_commands.describe(target="Instance ID or name")
        async def cmd_kill(interaction: discord.Interaction, target: str):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_kill(ctx, target))

        @self.tree.command(name="retry", description="Re-run instance", guild=guild_obj)
        @app_commands.describe(target="Instance ID or name")
        async def cmd_retry(interaction: discord.Interaction, target: str):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_retry(ctx, target))

        @self.tree.command(name="log", description="Full output", guild=guild_obj)
        @app_commands.describe(target="Instance ID or name")
        async def cmd_log(interaction: discord.Interaction, target: str):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_log(ctx, target))

        @self.tree.command(name="diff", description="Git diff", guild=guild_obj)
        @app_commands.describe(target="Instance ID or name")
        async def cmd_diff(interaction: discord.Interaction, target: str):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_diff(ctx, target))

        @self.tree.command(name="merge", description="Merge branch", guild=guild_obj)
        @app_commands.describe(target="Instance ID or name")
        async def cmd_merge(interaction: discord.Interaction, target: str):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_merge(ctx, target))

        @self.tree.command(name="discard", description="Delete branch", guild=guild_obj)
        @app_commands.describe(target="Instance ID or name")
        async def cmd_discard(interaction: discord.Interaction, target: str):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_discard(ctx, target))

        @self.tree.command(name="mode", description="View/set mode", guild=guild_obj)
        @app_commands.describe(mode="explore or build")
        async def cmd_mode(interaction: discord.Interaction, mode: str = ""):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_mode(ctx, mode))

        @self.tree.command(name="verbose", description="Progress detail level", guild=guild_obj)
        @app_commands.describe(level="0, 1, or 2")
        async def cmd_verbose(interaction: discord.Interaction, level: str = ""):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_verbose(ctx, level))

        @self.tree.command(name="context", description="Pinned context", guild=guild_obj)
        @app_commands.describe(args="set <text> | clear")
        async def cmd_context(interaction: discord.Interaction, args: str = ""):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_context(ctx, args))

        @self.tree.command(name="repo", description="Repo management", guild=guild_obj)
        @app_commands.describe(args="add|switch|list [name] [path]")
        async def cmd_repo(interaction: discord.Interaction, args: str = ""):
            if not self._auth(interaction.user.id):
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
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_session(ctx, args))

        @self.tree.command(name="schedule", description="Recurring tasks", guild=guild_obj)
        @app_commands.describe(args="every|at|list|delete ...")
        async def cmd_schedule(interaction: discord.Interaction, args: str = ""):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_schedule(ctx, args))

        @self.tree.command(name="alias", description="Command shortcuts", guild=guild_obj)
        @app_commands.describe(args="set|delete|list ...")
        async def cmd_alias(interaction: discord.Interaction, args: str = ""):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, lambda ctx: commands.on_alias(ctx, args))

        @self.tree.command(name="budget", description="Budget info/reset", guild=guild_obj)
        @app_commands.describe(args="reset")
        async def cmd_budget(interaction: discord.Interaction, args: str = ""):
            if not self._auth(interaction.user.id):
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
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return

            # Apply mode if specified
            mode = mode.strip().lower()
            if mode and mode in VALID_MODES:
                self._store.mode = mode

            repo = repo.strip()
            if repo:
                repos = self._store.list_repos()
                lower_map = {k.lower(): k for k in repos}
                repo_name = repo if repo in repos else lower_map.get(repo.lower())
                if not repo_name:
                    await interaction.response.send_message(
                        f"Repo '{repo}' not found.", ephemeral=True,
                    )
                    return
                await interaction.response.defer(ephemeral=True)
                await self._create_new_session(interaction, repo_name)
            else:
                repos = self._store.list_repos()
                if len(repos) <= 1:
                    await interaction.response.defer(ephemeral=True)
                    repo_name, _ = self._store.get_active_repo()
                    await self._create_new_session(interaction, repo_name)
                else:
                    view = discord.ui.View(timeout=60)
                    for name in repos:
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
            if not self._auth(interaction.user.id):
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
            count = max(1, min(count, 15))
            raw_sessions = await asyncio.to_thread(sessions_mod.scan_sessions, count * 3, self._store.list_repos())
            seen_projects: set[str] = set()
            session_list = []
            for s in raw_sessions:
                proj = s["project"]
                if proj not in seen_projects:
                    seen_projects.add(proj)
                    session_list.append(s)
                if len(session_list) >= count:
                    break

            created = []
            populated = []
            updated_threads: set[str] = set()
            for s in session_list:
                session_id = s["id"]
                repo_name = s.get("project") or "_default"

                # Check if session already has a thread
                existing = self._session_to_thread(session_id)
                if existing:
                    tid, info = existing
                    if tid in updated_threads:
                        continue
                    updated_threads.add(tid)
                    # Populate history
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

                # Create new thread
                log.info("Sync creating thread for session %s repo=%s", session_id[:12], repo_name)
                thread = await self._get_or_create_session_thread(
                    repo_name, session_id, s["topic"], origin="cli",
                )
                if thread:
                    created.append(thread)
                    await self._populate_thread_history(thread, session_id, str(thread.id))

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
            if not self._auth(interaction.user.id):
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
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_help)

        @self.tree.command(name="clear", description="Archive old instances", guild=guild_obj)
        async def cmd_clear(interaction: discord.Interaction):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_clear)

        @self.tree.command(name="logs", description="Bot log", guild=guild_obj)
        async def cmd_logs(interaction: discord.Interaction):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_logs)

        @self.tree.command(name="shutdown", description="Stop the bot", guild=guild_obj)
        async def cmd_shutdown(interaction: discord.Interaction):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_shutdown)

        @self.tree.command(name="reboot", description="Restart the bot (apply code changes)", guild=guild_obj)
        async def cmd_reboot(interaction: discord.Interaction):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await self._run_slash(interaction, commands.on_reboot)

        # --- Monitor command group ---
        monitor_group = app_commands.Group(
            name="monitor", description="Live app monitoring dashboards", guild_ids=[self._guild_id],
        )

        @monitor_group.command(name="setup", description="Enable a monitor (reads config from .env)")
        @app_commands.describe(name="Monitor name (e.g. aiagent)")
        async def cmd_monitor_setup(interaction: discord.Interaction, name: str):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            result = await self._monitor_setup(name.lower())
            await interaction.followup.send(result, ephemeral=True)

        @monitor_group.command(name="refresh", description="Fetch & update all monitors now")
        async def cmd_monitor_refresh(interaction: discord.Interaction):
            if not self._auth(interaction.user.id):
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
            if not self._auth(interaction.user.id):
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
            if not self._auth(interaction.user.id):
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
        if not _is_test_webhook and not self._auth(message.author.id):
            return

        text = message.content.strip()
        _temp_files: list[str] = []  # track temp files for cleanup

        # Handle file attachments
        IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        for att in message.attachments:
            if not att.filename:
                continue
            ext = Path(att.filename).suffix.lower()

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
            # --- Lobby: route to forum thread ---
            if message.channel.id == self._lobby_channel_id:
                active_repo, _ = self._store.get_active_repo()
                await self._route_lobby_message(message, text, active_repo)
                return

            # --- Forum thread: auto-resume session ---
            if isinstance(message.channel, discord.Thread):
                parent = message.channel.parent
                if parent and isinstance(parent, discord.ForumChannel):
                    lookup = self._thread_to_project(channel_id)
                    if not lookup:
                        # Adopt unmapped thread in a known forum
                        proj = self._forum_by_channel_id(str(parent.id))
                        if proj:
                            log.info("Adopted unmapped thread %s in forum %s", channel_id, parent.name)
                            info = ThreadInfo(thread_id=channel_id, origin="bot")
                            proj.threads[channel_id] = info
                            self._save_forum_map()
                            lookup = (proj, info)

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

                        ctx = self._ctx(channel_id, session_id=session_id, repo_name=repo_name)
                        await commands.on_text(ctx, text)

                        if was_pending:
                            await self._finalize_pending_thread(channel_id, message.channel, text)
                        elif "new-session" in message.channel.name:
                            await self._rename_thread_from_prompt(message.channel, text)
                        # Apply tags + refresh dashboard (fire-and-forget)
                        asyncio.create_task(self._try_apply_tags_after_run(channel_id))
                        asyncio.create_task(self._refresh_dashboard())
                        return

            # --- Other channel (unmapped): no session ---
            ctx = self._ctx(channel_id)
            await commands.on_text(ctx, text)
        finally:
            # Clean up temp image files
            for tmp in _temp_files:
                Path(tmp).unlink(missing_ok=True)

    async def _route_lobby_message(
        self, message: discord.Message, text: str, repo_name: str | None,
    ) -> None:
        """Route a lobby message to a forum thread."""
        repo_name = repo_name or "_default"
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
            ctx = self._ctx(str(thread.id), repo_name=repo_name if repo_name != "_default" else None)
            await commands.on_text(ctx, text)
            # Update mapping with real session_id
            await self._update_pending_thread(str(thread.id))
            # Apply tags + refresh dashboard
            asyncio.create_task(self._try_apply_tags_after_run(str(thread.id)))
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
        """Check latest instance status and apply tags to the thread."""
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
                break

    # --- Dashboard ---

    async def _refresh_dashboard(self) -> None:
        """Update or create the pinned dashboard embed in lobby. Debounced to 5s."""
        now = asyncio.get_event_loop().time()
        if now - self._dashboard_last_refresh < 5:
            return
        self._dashboard_last_refresh = now

        if not self._lobby_channel_id:
            return
        lobby = self.get_channel(self._lobby_channel_id)
        if not lobby or not isinstance(lobby, discord.TextChannel):
            return

        from bot.claude.types import InstanceStatus
        instances = self._store.list_instances()
        running = [i for i in instances if i.status == InstanceStatus.RUNNING]
        today_cost = self._store.get_daily_cost()
        total_cost = self._store.get_total_cost()
        repos = self._store.list_repos()
        active_repo, _ = self._store.get_active_repo()

        embed = discord.Embed(
            title="Claude Bot Dashboard",
            color=discord.Color.blurple(),
        )
        # Active instances
        if running:
            run_lines = []
            for inst in running[:5]:
                run_lines.append(f"`{inst.display_id()}` — {inst.prompt[:40]}")
            embed.add_field(name=f"Running ({len(running)})", value="\n".join(run_lines), inline=False)
        else:
            embed.add_field(name="Running", value="None", inline=True)

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
        state = self._store.get_platform_state("discord")
        dash_msg_id = state.get("dashboard_message_id")

        try:
            if dash_msg_id:
                try:
                    msg = await lobby.fetch_message(int(dash_msg_id))
                    await msg.edit(embed=embed)
                    return
                except (discord.NotFound, discord.HTTPException):
                    pass  # Message gone, create new one

            msg = await lobby.send(embed=embed)
            try:
                await msg.pin()
            except Exception:
                pass
            state["dashboard_message_id"] = str(msg.id)
            self._store.set_platform_state("discord", state, persist=True)
        except Exception:
            log.debug("Failed to update dashboard", exc_info=True)

    async def _rename_thread_from_prompt(
        self, thread: discord.Thread, prompt: str,
    ) -> None:
        """Rename a 'new-session' thread based on the first prompt."""
        new_name = channels.build_channel_name(prompt)[:100]
        try:
            await thread.edit(name=new_name)
            log.info("Renamed thread %s -> %s", thread.id, new_name)
        except Exception:
            log.warning("Failed to rename thread %s -> %s", thread.id, new_name, exc_info=True)

    async def _create_new_session(
        self, interaction: discord.Interaction, repo_name: str | None,
        *, redirect: bool = False,
    ) -> None:
        """Create a new session thread."""
        repo_name = repo_name or "_default"
        thread = await self._get_or_create_session_thread(repo_name, None, "new-session")
        if thread:
            if redirect:
                asyncio.create_task(self._send_redirect(thread))
            else:
                await interaction.followup.send(
                    f"Fresh session created: <#{thread.id}>", ephemeral=True,
                )
        else:
            msg = "Could not create thread."
            if redirect:
                asyncio.create_task(self._send_temp_lobby_msg(msg))
            else:
                await interaction.followup.send(msg, ephemeral=True)

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

    async def _finalize_pending_thread(
        self, thread_id: str, thread: discord.Thread, prompt: str,
    ) -> None:
        """After first query in a /new thread, update session mapping and rename."""
        session_id = None
        for inst in self._store.list_instances()[:10]:
            if inst.session_id and inst.origin_platform == "discord":
                discord_msg_ids = inst.message_ids.get("discord", [])
                if discord_msg_ids:
                    try:
                        await thread.fetch_message(int(discord_msg_ids[0]))
                    except (discord.NotFound, discord.HTTPException):
                        continue
                    session_id = inst.session_id
                    # Update thread info
                    lookup = self._thread_to_project(thread_id)
                    if lookup:
                        _, info = lookup
                        info.session_id = session_id
                        info.topic = prompt
                        self._save_forum_map()
                    log.info("Finalized pending thread %s -> session %s", thread_id, session_id)
                    break

        # Rename if still "new-session"
        if "new-session" in thread.name:
            await self._rename_thread_from_prompt(thread, prompt)

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

        if not self._auth(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return

        custom_id = interaction.data.get("custom_id", "") if interaction.data else ""

        # --- Select menu: repo switch ---
        if custom_id == "repo_switch_select":
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
            self._store.mode = target_mode
            # Update the welcome embed to reflect selected mode
            if interaction.message and interaction.message.embeds:
                embed = interaction.message.embeds[0]
                for i, field in enumerate(embed.fields):
                    if field.name == "Mode":
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
            await self._create_new_session(interaction, repo_name, redirect=True)
            try:
                await interaction.delete_original_response()
            except Exception:
                pass
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
                ctx = self._ctx(
                    str(thread.id), session_id=session_id,
                    repo_name=repo_name if repo_name != "_default" else None,
                )
                source_msg_id = str(interaction.message.id) if interaction.message else None
                await workflows.on_sess_resume(ctx, session_id, source_msg_id)
            return

        # --- New session button: create a new forum thread (like /new) ---
        if action == "new":
            thread_id = str(interaction.channel_id)
            lookup = self._thread_to_project(thread_id)
            repo_name = lookup[0].repo_name if lookup else None
            if not repo_name:
                repo_name, _ = self._store.get_active_repo()
            await self._create_new_session(interaction, repo_name)
            try:
                await interaction.delete_original_response()
            except Exception:
                pass
            return

        channel_id = str(interaction.channel_id)
        source_msg_id = str(interaction.message.id) if interaction.message else None

        ctx = self._ctx(channel_id)
        await commands.handle_callback(ctx, action, instance_id, source_msg_id)

        # Refresh dashboard after mode switch (Discord-specific)
        if action.startswith("mode_"):
            asyncio.create_task(self._refresh_dashboard())
