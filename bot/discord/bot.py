"""Discord bot with slash commands, message handler, and persistent views."""

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from bot import config
from bot.discord import channels
from bot.discord.adapter import DiscordMessenger
from bot.engine import commands
from bot.engine import sessions as sessions_mod
from bot.platform.base import ButtonSpec, RequestContext

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
        # channel_id -> {"session_id": str, "repo_name": str}
        self._channel_sessions: dict[str, dict] = {}
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

    # --- Channel-Session Mapping ---

    def _load_channel_map(self) -> None:
        """Load channel→session mapping from platform_state."""
        state = self._store.get_platform_state("discord")
        raw = state.get("channel_sessions", {})
        # Migrate old format (str session_id) to new format (dict)
        migrated = {}
        for ch_id, val in raw.items():
            if isinstance(val, str):
                migrated[ch_id] = {"session_id": val, "repo_name": ""}
            elif isinstance(val, dict):
                migrated[ch_id] = val
        self._channel_sessions = migrated
        log.info("Loaded %d channel-session mappings", len(self._channel_sessions))

    def _save_channel_map(self) -> None:
        """Persist channel→session mapping to platform_state (in-memory only, auto-save writes to disk)."""
        state = self._store.get_platform_state("discord")
        state["channel_sessions"] = self._channel_sessions
        self._store.set_platform_state("discord", state, persist=False)

    def _session_to_channel(self, session_id: str) -> str | None:
        """Reverse lookup: find channel for a session_id."""
        for ch_id, info in self._channel_sessions.items():
            if info.get("session_id") == session_id:
                return ch_id
        return None

    def _get_active_channel_ids(self) -> set[str]:
        """Return channel IDs that have a running/queued instance."""
        from bot.claude.types import InstanceStatus
        active = set()
        for inst in self._store.list_instances():
            if inst.status in (InstanceStatus.RUNNING, InstanceStatus.QUEUED):
                # Match by session_id → channel mapping
                if inst.session_id:
                    ch_id = self._session_to_channel(inst.session_id)
                    if ch_id:
                        active.add(ch_id)
                # Also match by discord message_ids (channel_id is the key)
                for ch_id in inst.message_ids.get("discord", []):
                    if ch_id in self._channel_sessions:
                        active.add(ch_id)
        return active

    async def _sync_single_channel(self, ch_id: str) -> None:
        """Refresh a single session channel: pull latest CLI messages."""
        ch_info = self._channel_sessions.get(ch_id)
        if not ch_info:
            return
        session_id = ch_info.get("session_id")
        repo_name = ch_info.get("repo_name")
        if not session_id:
            return

        guild = self.get_guild(self._guild_id)
        if not guild:
            return
        ch = guild.get_channel(int(ch_id))
        if not ch or not isinstance(ch, discord.TextChannel):
            return

        # Check if there's a newer CLI session for this repo
        if repo_name:
            repo_path = self._store.list_repos().get(repo_name, "")
            if repo_path:
                latest = await asyncio.to_thread(
                    sessions_mod.find_latest_session_for_repo, repo_path,
                )
                if latest and latest["id"] != session_id:
                    # Update to the newer session
                    ch_info["session_id"] = latest["id"]
                    ch_info["origin"] = "cli"
                    ch_info["_synced_msg_count"] = 0  # reset for fresh populate
                    self._save_channel_map()
                    asyncio.create_task(self._safe_edit(ch, topic=f"session:{latest['id']}"))
                    log.info("Single-sync updated #%s to session %s", ch.name, latest["id"][:12])
                    session_id = latest["id"]

        await self._populate_channel_history(ch, session_id)
        log.info("Single-sync refreshed #%s", ch.name)

    async def _get_or_create_session_channel(
        self, session_id: str | None, topic: str,
        repo_name: str | None = None,
    ) -> discord.TextChannel | None:
        """Find existing channel for session, or create a new one.

        Returns None if category is not available.
        """
        guild = self.get_guild(self._guild_id)
        if not guild or not self._category_id:
            return None
        category = guild.get_channel(self._category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            return None

        # Check if this session already has a channel
        if session_id:
            existing_ch_id = self._session_to_channel(session_id)
            if existing_ch_id:
                ch = guild.get_channel(int(existing_ch_id))
                if ch and isinstance(ch, discord.TextChannel):
                    return ch
                # Channel was deleted externally — remove stale mapping
                del self._channel_sessions[existing_ch_id]

        # Enforce channel limit before creating
        await self._enforce_channel_limit(category)

        channel = await channels.create_session_channel(
            guild, category, topic, session_id=session_id, repo_name=repo_name,
        )
        if session_id:
            self._channel_sessions[str(channel.id)] = {
                "session_id": session_id,
                "repo_name": repo_name or "",
                "origin": "bot",
            }
        self._save_channel_map()
        await self._pin_lobby_top()
        return channel

    async def _pin_lobby_top(self) -> None:
        """Ensure lobby channel stays at position 0 in the category."""
        if not self._lobby_channel_id:
            return
        guild = self.get_guild(self._guild_id)
        if not guild:
            return
        lobby = guild.get_channel(self._lobby_channel_id)
        if lobby and getattr(lobby, "position", 0) != 0:
            try:
                await lobby.edit(position=0)
            except Exception:
                pass

    async def _enforce_channel_limit(
        self, category: discord.CategoryChannel,
    ) -> None:
        """Archive oldest session channels if over the limit."""
        session_channels = [
            ch for ch in category.text_channels
            if ch.id != self._lobby_channel_id
            and not (ch.topic and ch.topic.startswith("monitor:"))
        ]
        if len(session_channels) < channels.MAX_SESSION_CHANNELS:
            return

        # Sort by position (higher = older/lower in list)
        session_channels.sort(key=lambda c: c.position, reverse=True)
        to_remove = len(session_channels) - channels.MAX_SESSION_CHANNELS + 1

        for ch in session_channels[:to_remove]:
            self._channel_sessions.pop(str(ch.id), None)
            await channels.archive_session_channel(ch)

        self._save_channel_map()

    async def _reconcile_channels(self) -> None:
        """Sync mapping with reality on startup."""
        guild = self.get_guild(self._guild_id)
        if not guild or not self._category_id:
            return
        category = guild.get_channel(self._category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            return

        # Remove mappings for channels that no longer exist
        valid = {}
        for ch_id, info in self._channel_sessions.items():
            ch = guild.get_channel(int(ch_id))
            if ch and isinstance(ch, discord.TextChannel) and ch.category_id == self._category_id:
                valid[ch_id] = info
            else:
                log.info("Removed stale channel mapping %s -> %s", ch_id, info.get("session_id", "?"))

        # Discover unmapped session channels by topic
        for ch in category.text_channels:
            ch_id = str(ch.id)
            if ch.id == self._lobby_channel_id or ch_id in valid:
                continue
            if ch.topic and ch.topic.startswith("session:"):
                sid = ch.topic.split(":", 1)[1]
                if sid and sid != "pending":
                    # Try to recover repo_name from channel name prefix (e.g. "aiagent│topic")
                    repo_name = ""
                    if "│" in ch.name:
                        repo_name = ch.name.split("│", 1)[0]
                    valid[ch_id] = {"session_id": sid, "repo_name": repo_name}
                    log.info("Recovered channel mapping %s -> %s (repo: %s) from topic", ch_id, sid, repo_name or "?")

        if valid != self._channel_sessions:
            self._channel_sessions = valid
            self._save_channel_map()

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
        @app_commands.describe(repo="Repo name (default: active repo)")
        async def cmd_new(interaction: discord.Interaction, repo: str = ""):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return

            repo = repo.strip()
            if repo:
                repos = self._store.list_repos()
                lower_map = {k.lower(): k for k in repos}
                repo_name = repos.get(repo) and repo or lower_map.get(repo.lower())
                if not repo_name or repo_name not in repos:
                    await interaction.response.send_message(
                        f"Repo '{repo}' not found.", ephemeral=True,
                    )
                    return
                await interaction.response.defer(ephemeral=True)
                await self._create_new_session(interaction, repo_name)
            else:
                # Show repo picker buttons
                repos = self._store.list_repos()
                if len(repos) <= 1:
                    # Only one repo — just create directly
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

        @self.tree.command(name="sync", description="Sync sessions (in session channel: refresh this one)", guild=guild_obj)
        @app_commands.describe(count="Number of sessions to sync (default 5, 0 = this channel only)")
        async def cmd_sync(interaction: discord.Interaction, count: int = 5):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)

            # In a session channel with default count → just refresh this channel
            ch_id_str = str(interaction.channel_id)
            if ch_id_str in self._channel_sessions:
                if count == 5:  # default — user didn't specify a count
                    await self._sync_single_channel(ch_id_str)
                    await interaction.followup.send("Channel synced.", ephemeral=True)
                    return
                elif count == 0:
                    await self._sync_single_channel(ch_id_str)
                    await interaction.followup.send("Channel synced.", ephemeral=True)
                    return
            elif count == 0:
                await interaction.followup.send(
                    "Not in a session channel. Use `/sync <count>` from the lobby.", ephemeral=True,
                )
                return

            log.info("Discord /sync count=%d by %s", count, interaction.user)
            count = max(1, min(count, channels.MAX_SESSION_CHANNELS))
            session_list = await asyncio.to_thread(sessions_mod.scan_sessions, count, self._store.list_repos())
            created = []
            populated = []
            updated_channels: set[str] = set()  # avoid editing same channel multiple times
            for s in session_list:
                session_id = s["id"]
                repo_name = s.get("project")
                # Match by exact session_id only — each session gets its own channel
                existing_ch_id = self._session_to_channel(session_id)
                if existing_ch_id:
                    if existing_ch_id in updated_channels:
                        continue
                    updated_channels.add(existing_ch_id)
                    # Channel exists — update session reference, populate history
                    # Mark as CLI-originated so on_message starts a fresh bot session
                    guild = self.get_guild(self._guild_id)
                    if guild:
                        ch = guild.get_channel(int(existing_ch_id))
                        if ch and isinstance(ch, discord.TextChannel):
                            ch_info = self._channel_sessions.get(existing_ch_id, {})
                            old_sid = ch_info.get("session_id")
                            origin = ch_info.get("origin")
                            if old_sid != session_id:
                                self._channel_sessions[existing_ch_id]["session_id"] = session_id
                                self._channel_sessions[existing_ch_id]["origin"] = "cli"
                                self._save_channel_map()
                                # Fire-and-forget topic edit — don't block sync on rate limits
                                asyncio.create_task(self._safe_edit(ch, topic=f"session:{session_id}"))
                                log.info("Sync updated #%s session %s -> %s (cli)", ch.name, (old_sid or "?")[:12], session_id[:12])
                                await self._populate_channel_history(ch, session_id)
                            elif origin != "bot":
                                # Same session, CLI origin — check for new messages
                                await self._populate_channel_history(ch, session_id)
                            else:
                                log.debug("Skipping #%s — already active on Discord", ch.name)
                            # Rename stuck "new-session" channels using session topic
                            if "new-session" in ch.name and s.get("topic"):
                                await self._rename_channel_from_prompt(ch, s["topic"])
                            populated.append(ch)
                    continue
                log.info("Sync creating new channel for session %s repo=%s", session_id[:12], repo_name)
                ch = await self._get_or_create_session_channel(
                    session_id, s["topic"], repo_name=repo_name,
                )
                if ch:
                    # Mark as CLI-originated — first user message will start fresh bot session
                    ch_id = str(ch.id)
                    if ch_id in self._channel_sessions:
                        self._channel_sessions[ch_id]["origin"] = "cli"
                    else:
                        self._channel_sessions[ch_id] = {
                            "session_id": session_id,
                            "repo_name": repo_name or "",
                            "origin": "cli",
                        }
                    self._save_channel_map()
                    created.append(ch)
                    await self._populate_channel_history(ch, session_id)
            # Remove channels not in the synced set (skip channels with active instances)
            synced_session_ids = {s["id"] for s in session_list}
            active_channels = self._get_active_channel_ids()
            removed = []
            for ch_id, info in list(self._channel_sessions.items()):
                sid = info.get("session_id") if isinstance(info, dict) else info
                if sid and sid not in synced_session_ids:
                    if ch_id in active_channels:
                        log.info("Skipping removal of #%s — has active instance", ch_id)
                        continue
                    guild = self.get_guild(self._guild_id)
                    if guild:
                        ch = guild.get_channel(int(ch_id))
                        if ch and isinstance(ch, discord.TextChannel):
                            removed.append(ch.name)
                            await channels.archive_session_channel(ch)
                    del self._channel_sessions[ch_id]
            if removed:
                self._save_channel_map()

            parts = []
            if created:
                links = ", ".join(f"<#{ch.id}>" for ch in created)
                parts.append(f"Created {len(created)}: {links}")
            if populated:
                links = ", ".join(f"<#{ch.id}>" for ch in populated)
                parts.append(f"Updated {len(populated)}: {links}")
            if removed:
                parts.append(f"Removed {len(removed)}: {', '.join(removed)}")
            if not parts:
                parts.append("No sessions found")
            await self._pin_lobby_top()
            await interaction.followup.send("\n".join(parts), ephemeral=True)

        @self.tree.command(name="sync-channel", description="Refresh this channel's session history", guild=guild_obj)
        async def cmd_sync_channel(interaction: discord.Interaction):
            if not self._auth(interaction.user.id):
                await interaction.response.send_message("Unauthorized", ephemeral=True)
                return
            ch_id = str(interaction.channel_id)
            ch_info = self._channel_sessions.get(ch_id)
            if not ch_info:
                await interaction.response.send_message(
                    "This isn't a session channel.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            session_id = ch_info.get("session_id")
            if not session_id:
                await interaction.followup.send("No session mapped to this channel.", ephemeral=True)
                return
            repo_name = ch_info.get("repo_name", "")
            # Check for a newer CLI session on the same repo
            repo_path = self._store.list_repos().get(repo_name, "") if repo_name else ""
            if repo_path:
                latest = await asyncio.to_thread(
                    sessions_mod.find_latest_session_for_repo, repo_path,
                )
                if latest and latest["id"] != session_id:
                    old_short = session_id[:12]
                    session_id = latest["id"]
                    ch_info["session_id"] = session_id
                    ch_info["origin"] = "cli"
                    ch_info["_synced_msg_count"] = 0  # reset for full re-sync
                    self._save_channel_map()
                    ch = interaction.channel
                    if isinstance(ch, discord.TextChannel):
                        asyncio.create_task(self._safe_edit(ch, topic=f"session:{session_id}"))
                    log.info("sync-channel updated #%s session %s -> %s",
                             interaction.channel, old_short, session_id[:12])
            # Populate history
            ch = interaction.channel
            if isinstance(ch, discord.TextChannel):
                await self._populate_channel_history(ch, session_id)
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
                status = "\U0001f7e2" if m["enabled"] else "\u26ab"
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

        # Start background loop if not already running
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
                # Reset messenger so it picks up new IDs
                self._messenger = None
                log.info(
                    "Auto-provisioned category=%s lobby=%s",
                    category.id, lobby.id,
                )

        # Load and reconcile channel-session mapping
        self._load_channel_map()
        await self._reconcile_channels()

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

        # Note: reboot announcement is handled by app.py broadcast (not here,
        # since on_ready also fires on reconnects, not just first boot).

    def _in_scope(self, guild: discord.Guild | None, channel: discord.abc.GuildChannel | None = None) -> bool:
        """Check guild + channel is within our category."""
        if not guild or guild.id != self._guild_id:
            return False
        if channel and self._category_id:
            # Must be inside our category (lobby or session channels)
            parent = getattr(channel, "category_id", None)
            if parent != self._category_id:
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
        # Ignore bots
        if message.author.bot:
            return
        # Only respond inside our category
        if not self._in_scope(message.guild, message.channel):
            return
        # Auth check
        if not self._auth(message.author.id):
            return

        text = message.content.strip()
        if not text:
            return

        channel_id = str(message.channel.id)
        ch_name = getattr(message.channel, "name", channel_id)
        log.info("Discord msg in #%s: %s", ch_name, text[:80])

        # --- Lobby: create a new session channel and redirect ---
        if message.channel.id == self._lobby_channel_id:
            active_repo, _ = self._store.get_active_repo()
            ch = await self._get_or_create_session_channel(None, text, repo_name=active_repo)
            if ch:
                # Delete original message from lobby to keep it clean
                try:
                    await message.delete()
                except Exception:
                    pass
                # Post redirect in lobby (auto-delete after 5s)
                asyncio.create_task(self._send_redirect(ch))
                # Run query in new channel (no session_id yet — pending)
                ctx = self._ctx(str(ch.id))
                await commands.on_text(ctx, text)
                # After run, the instance has a session_id — update mapping
                await self._update_pending_channel(str(ch.id))
            return

        # --- Session channel: auto-resume ---
        ch_info = self._channel_sessions.get(channel_id)
        if ch_info:
            session_id = ch_info.get("session_id") or None  # "" (pending) -> None
            repo_name = ch_info.get("repo_name") or None
            origin = ch_info.get("origin", "bot")

            # CLI → Discord transition: resume the CLI session seamlessly,
            # then mark as "bot" so we don't auto-sync to newer CLI sessions later.
            if session_id and origin == "cli":
                log.info("Channel #%s resuming CLI session %s — transitioning to bot ownership",
                         ch_name, session_id[:12])
                ch_info["origin"] = "bot"
                self._save_channel_map()

            was_pending = not session_id

            # CLI activity notification (debounced, one-shot per CLI session)
            # Only alert if the newer session isn't already mapped to another channel
            now_ts = asyncio.get_event_loop().time()
            last_check = ch_info.get("_last_sync_check", 0)
            if repo_name and (now_ts - last_check > 60):
                ch_info["_last_sync_check"] = now_ts
                repo_path = self._store.list_repos().get(repo_name, "")
                if repo_path:
                    try:
                        latest = await asyncio.to_thread(
                            sessions_mod.find_latest_session_for_repo, repo_path,
                        )
                        notified_id = ch_info.get("_cli_notified_id")
                        # Check if this session is already tracked in another channel
                        already_mapped = (
                            latest and self._session_to_channel(latest["id"]) is not None
                        )
                        if (latest and not already_mapped
                                and latest["id"] != (ch_info.get("session_id") or "")
                                and latest["id"] != notified_id
                                and _time.time() - latest["mtime"] < 3600):
                            ch_info["_cli_notified_id"] = latest["id"]
                            buttons = [[ButtonSpec(
                                "Load CLI history",
                                f"load_history:{latest['id']}",
                            )]]
                            await self.messenger.send_text(
                                channel_id,
                                "ℹ️ New CLI activity detected on this repo.",
                                buttons=buttons,
                                silent=True,
                            )
                    except Exception:
                        log.debug("CLI activity check failed for channel %s", channel_id, exc_info=True)

            ctx = self._ctx(channel_id, session_id=session_id, repo_name=repo_name)
            await commands.on_text(ctx, text)
            # If session was pending, update mapping with real session_id + rename
            if was_pending:
                await self._finalize_pending_channel(channel_id, message.channel, text)
            elif (isinstance(message.channel, discord.TextChannel)
                  and "new-session" in message.channel.name):
                # Retry rename if still stuck (e.g. previous rename was rate-limited)
                await self._rename_channel_from_prompt(message.channel, text, position=1)
            elif getattr(message.channel, "position", 0) > 2:
                # Move channel toward top for most-recent ordering (skip if already near top)
                try:
                    await message.channel.edit(position=1)
                except Exception:
                    pass
            return

        # --- Other channel (unmapped): no session ---
        ctx = self._ctx(channel_id)
        await commands.on_text(ctx, text)

    async def _safe_edit(self, channel: discord.TextChannel, **kwargs) -> None:
        """Edit a channel, logging failures instead of blocking on rate limits."""
        try:
            await channel.edit(**kwargs)
        except Exception:
            log.warning("Failed to edit channel %s", channel.id, exc_info=True)

    async def _rename_channel_from_prompt(
        self, channel: discord.TextChannel, prompt: str, **extra_edits,
    ) -> None:
        """Rename a 'new-session' channel based on the first prompt.

        Extra kwargs (e.g. topic=, position=) are included in the same edit call.
        """
        ch_info = self._channel_sessions.get(str(channel.id), {})
        repo_name = ch_info.get("repo_name") or ""
        new_name = channels.build_channel_name(prompt, repo_name or None)
        try:
            await channel.edit(name=new_name, **extra_edits)
            log.info("Renamed channel %s -> %s", channel.id, new_name)
        except Exception:
            log.warning("Failed to rename channel %s -> %s", channel.id, new_name, exc_info=True)

    async def _create_new_session(
        self, interaction: discord.Interaction, repo_name: str | None,
        *, redirect: bool = False,
    ) -> None:
        """Create a new session channel.

        Args:
            redirect: If True, post a redirect link in lobby instead of followup.
        """
        ch = await self._get_or_create_session_channel(None, "new-session", repo_name=repo_name)
        if ch:
            self._channel_sessions[str(ch.id)] = {
                "session_id": "",
                "repo_name": repo_name or "",
                "origin": "bot",
            }
            self._save_channel_map()
            if redirect:
                asyncio.create_task(self._send_redirect(ch))
            else:
                await interaction.followup.send(
                    f"Fresh session created: <#{ch.id}>", ephemeral=True,
                )
        else:
            if redirect:
                asyncio.create_task(self._send_temp_lobby_msg("Could not create channel."))
            else:
                await interaction.followup.send(
                    "Could not create channel.", ephemeral=True,
                )

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

    async def _send_redirect(self, channel: discord.TextChannel) -> None:
        """Post a redirect link in lobby, auto-delete after 5s."""
        await self._send_temp_lobby_msg(f"→ <#{channel.id}>")

    async def _finalize_pending_channel(
        self, channel_id: str, channel: discord.abc.GuildChannel, prompt: str,
    ) -> None:
        """After first query in a /new channel, update session mapping and rename."""
        session_id = None
        # Find the instance that just ran in this channel (check only recent 10)
        for inst in self._store.list_instances()[:10]:
            if inst.session_id and inst.origin_platform == "discord":
                discord_msg_ids = inst.message_ids.get("discord", [])
                if discord_msg_ids:
                    try:
                        if isinstance(channel, discord.TextChannel):
                            await channel.fetch_message(int(discord_msg_ids[0]))
                    except (discord.NotFound, discord.HTTPException):
                        continue
                    session_id = inst.session_id
                    self._channel_sessions[channel_id] = {
                        "session_id": session_id,
                        "repo_name": inst.repo_name or "",
                        "origin": "bot",
                    }
                    self._save_channel_map()
                    log.info("Finalized pending channel %s -> session %s", channel_id, session_id)
                    break

        # Single edit call: rename + topic + position (avoids rate-limit from multiple edits)
        edits: dict = {"position": 1}
        if session_id:
            edits["topic"] = f"session:{session_id}"
        if isinstance(channel, discord.TextChannel) and "new-session" in channel.name:
            repo_name = self._channel_sessions.get(channel_id, {}).get("repo_name")
            edits["name"] = channels.build_channel_name(prompt, repo_name or None)
        if edits:
            try:
                await channel.edit(**edits)
                if "name" in edits:
                    log.info("Renamed channel %s -> %s", channel.id, edits["name"])
            except Exception:
                log.warning("Failed to edit channel %s", channel.id, exc_info=True)

    async def _update_pending_channel(self, channel_id: str) -> None:
        """After a query completes, update a pending channel's session mapping."""
        if channel_id in self._channel_sessions:
            return  # already mapped

        # Find the most recent instance with a session_id (check only recent 10)
        for inst in self._store.list_instances()[:10]:
            if inst.session_id and inst.origin_platform == "discord":
                # Check if any of this instance's discord message_ids were sent to our channel
                discord_msg_ids = inst.message_ids.get("discord", [])
                if not discord_msg_ids:
                    continue
                # Verify by checking the channel — try to fetch one message
                guild = self.get_guild(self._guild_id)
                if not guild:
                    return
                ch = guild.get_channel(int(channel_id))
                if not ch or not isinstance(ch, discord.TextChannel):
                    return
                try:
                    msg = await ch.fetch_message(int(discord_msg_ids[0]))
                    if msg:
                        self._channel_sessions[channel_id] = {
                            "session_id": inst.session_id,
                            "repo_name": inst.repo_name or "",
                            "origin": "bot",
                        }
                        self._save_channel_map()
                        try:
                            await ch.edit(topic=f"session:{inst.session_id}")
                        except Exception:
                            pass
                        log.info("Mapped channel %s -> session %s (repo: %s)", channel_id, inst.session_id, inst.repo_name)
                        return
                except (discord.NotFound, discord.HTTPException):
                    continue

    async def _populate_channel_history(
        self, channel: discord.TextChannel, session_id: str,
        *, force: bool = False, cli_label: bool = False,
    ) -> None:
        """Send messages from a session into a channel.

        Args:
            force: If True, always send last 10 (no incremental check).
            cli_label: If True, label messages as "You (CLI)" / "Claude (CLI)".
        """
        ch_id = str(channel.id)
        ch_info = self._channel_sessions.get(ch_id, {})
        prev_count = 0 if force else ch_info.get("_synced_msg_count", 0)

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
            log.debug("No new messages for #%s (total=%d, synced=%d)", channel.name, total, prev_count)
            return

        user_label = "**You (CLI)**" if cli_label else "**You**"
        bot_label = "**Claude (CLI)**" if cli_label else "**Claude**"

        log.info("Populating #%s with %d messages (total=%d, prev=%d)", channel.name, len(to_send), total, prev_count)
        for msg in to_send:
            role = user_label if msg["role"] == "user" else bot_label
            text = msg["text"]
            if len(text) > 800:
                text = text[:800] + "…"
            try:
                markup = self.messenger.markdown_to_markup(f"{role}:\n{text}")
                await self.messenger.send_text(ch_id, markup, silent=True)
            except Exception:
                try:
                    await self.messenger.send_text(ch_id, f"{role}:\n{text[:800]}", silent=True)
                except Exception:
                    break

        if ch_id in self._channel_sessions:
            self._channel_sessions[ch_id]["_synced_msg_count"] = total
            self._save_channel_map()

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Handle button interactions (persistent views)."""
        # Only handle component interactions (buttons)
        if interaction.type != discord.InteractionType.component:
            return
        # Only respond inside our category
        if not self._in_scope(interaction.guild, interaction.channel):
            return

        if not self._auth(interaction.user.id):
            await interaction.response.send_message("Unauthorized", ephemeral=True)
            return

        custom_id = interaction.data.get("custom_id", "") if interaction.data else ""
        parts = custom_id.split(":", 1)
        if len(parts) != 2:
            return

        action, instance_id = parts
        log.info("Discord button %s:%s in #%s", action, instance_id[:12], getattr(interaction.channel, "name", "?"))
        await interaction.response.defer()

        # --- Load CLI history into channel ---
        if action == "load_history":
            cli_session_id = instance_id
            ch_id = str(interaction.channel_id)
            ch_info = self._channel_sessions.get(ch_id)
            if ch_info and isinstance(interaction.channel, discord.TextChannel):
                # Update channel to use the CLI session
                ch_info["session_id"] = cli_session_id
                ch_info["origin"] = "cli"
                self._save_channel_map()
                # Populate history (force — skip the "already has messages" check)
                await self._populate_channel_history(
                    interaction.channel, cli_session_id,
                    force=True, cli_label=True,
                )
                asyncio.create_task(self._safe_edit(
                    interaction.channel, topic=f"session:{cli_session_id}",
                ))
                log.info("Loaded CLI history %s into #%s", cli_session_id[:12],
                         interaction.channel.name)
            # Delete the notification message
            try:
                await interaction.message.delete()
            except Exception:
                pass
            # Clean up deferred response
            try:
                await interaction.delete_original_response()
            except Exception:
                pass
            return

        # --- New session with repo picker ---
        if action == "new_repo":
            repo_name = instance_id  # instance_id is actually the repo name here
            await self._create_new_session(interaction, repo_name, redirect=True)
            # Clean up deferred response
            try:
                await interaction.delete_original_response()
            except Exception:
                pass
            return

        # --- Session resume: create/find channel, redirect there ---
        if action == "sess_resume":
            session_id = instance_id
            from bot.engine import workflows

            # Scan session to get topic and project for channel name + repo
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

            ch = await self._get_or_create_session_channel(
                session_id, topic, repo_name=repo_name,
            )
            if ch:
                # Delete the "thinking" deferred response
                try:
                    await interaction.delete_original_response()
                except Exception:
                    pass
                # Post redirect in lobby
                asyncio.create_task(self._send_redirect(ch))
                # Send context messages in the new channel
                ctx = self._ctx(str(ch.id), session_id=session_id, repo_name=repo_name)
                source_msg_id = str(interaction.message.id) if interaction.message else None
                await workflows.on_sess_resume(ctx, session_id, source_msg_id)
            return

        channel_id = str(interaction.channel_id)
        source_msg_id = str(interaction.message.id) if interaction.message else None

        ctx = self._ctx(channel_id)
        await commands.handle_callback(ctx, action, instance_id, source_msg_id)
