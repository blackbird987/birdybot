"""Startup work run once the gateway is ready, as a ClaudeBot mixin.

on_ready provisioning and reconcile, the stale worktree cleanup, and the
recovery notices posted for runs a restart interrupted. Moved out of bot.py
unchanged.
"""

from __future__ import annotations

import asyncio
import logging

from bot.discord import channels

log = logging.getLogger(__name__)


class StartupMixin:
    """ClaudeBot methods; see the module docstring."""

    async def on_ready(self) -> None:
        log.info("Discord bot ready as %s", self.user)

        await self._reconcile_guest_roles()

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

        # Granted users' forums predate some permissions (message history
        # among them); top them up. Idempotent, so reconnects cost nothing.
        asyncio.create_task(self._forums.reconcile_user_forum_permissions())

        # Clean up orphaned messages in control rooms (one-time, non-blocking)
        if not getattr(self, '_control_rooms_cleaned', False):
            self._control_rooms_cleaned = True
            asyncio.create_task(self._forums.cleanup_all_control_rooms())

        # A forum has one pin slot — make sure the Control Room holds it
        # (one-time, non-blocking; no-ops on forums that are already right)
        if not getattr(self, '_forum_pins_reconciled', False):
            self._forum_pins_reconciled = True
            asyncio.create_task(self._forums.reconcile_forum_pins())

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

        # Upload reaper.  Separate from the drain above because the drain
        # sweeps once and then never again — and the reaper is the only thing
        # deleting uploads now, so "once at boot" would let a long-lived
        # process accumulate them indefinitely.
        existing_reaper = getattr(self, '_image_sweep_task', None)
        if not existing_reaper or existing_reaper.done():
            self._image_sweep_task = asyncio.create_task(
                self._pending_image_sweep_loop()
            )

        # Clean up stale worktrees/branches from interrupted autopilot chains
        asyncio.create_task(self._startup_worktree_cleanup())

    async def _startup_worktree_cleanup(self) -> None:
        """Recover partial worktrees, merge pending results, clean orphans.

        Order is load-bearing: ``recover_partial_worktrees`` must run BEFORE
        ``cleanup_stale_worktrees`` so an instance whose metadata was lost in
        an earlier buggy cleanup pass gets re-registered before the next
        cleanup pass would otherwise scoop it up as an orphan again. Without
        the recovery-first ordering, a single bad startup could permanently
        cement state.json into a broken state across reboots.
        """
        try:
            repos = self._store.list_repos()
            if not repos:
                return
            try:
                events = await self._runner.recover_partial_worktrees(
                    self._store, repos,
                )
            except Exception:
                events = []
                log.warning("Worktree recovery scan failed", exc_info=True)
            for ev in events:
                log.info(
                    "Worktree recovery: %s %s/%s status=%s detail=%s",
                    ev.instance_id, ev.repo_name, ev.branch,
                    ev.status, ev.detail,
                )
            if events:
                await self._surface_recovery_events(events)
            messages = await self._runner.cleanup_stale_worktrees(
                self._store, repos,
            )
            for msg in messages:
                log.info("Startup cleanup: %s", msg)
            if messages:
                log.info("Startup cleanup complete: %d actions", len(messages))
            try:
                release_events = await self._runner.scan_orphan_release_commits(
                    self._store, repos,
                )
            except Exception:
                release_events = []
                log.warning("Orphan release-commit scan failed", exc_info=True)
            for ev in release_events:
                log.info(
                    "Release recovery: %s %s/%s commit=%s bumped=%s detail=%s",
                    ev.instance_id, ev.repo_name, ev.branch,
                    ev.commit_sha, ev.bumped_version, ev.detail,
                )
            if release_events:
                await self._surface_release_recovery_events(release_events)
        except Exception:
            log.warning("Startup worktree cleanup failed", exc_info=True)

    async def _surface_recovery_events(self, events) -> None:
        """Post a one-line notice into each recovery event's thread.

        Silent recovery is what kept the original t-3700 bug invisible —
        every surfaced event here is a state change the user (managing
        from phone) needs to see. ``skipped`` events with a "branch no
        longer exists" detail are noise (recovered upstream by orphan
        cleanup) and intentionally suppressed.
        """
        for ev in events:
            if ev.status == "skipped" and "no longer exists" in (ev.detail or ""):
                continue
            inst = self._store.get_instance(ev.instance_id)
            if not inst or not inst.session_id:
                continue
            try:
                lookup = self._forums.session_to_thread(inst.session_id)
            except Exception:
                lookup = None
            if not lookup:
                continue
            thread_id, _ = lookup
            if ev.status == "recovered":
                text = (
                    f"Recovered worktree for `{ev.branch}` "
                    f"after metadata loss (content matched branch tip)."
                )
            elif ev.status == "manual_recovery_needed":
                text = (
                    f"Worktree for `{ev.branch}` lost git metadata "
                    f"AND has uncommitted drift ({ev.detail}). "
                    f"Inspect manually before the next Build."
                )
            else:  # "skipped"
                text = (
                    f"Skipped worktree recovery for `{ev.branch}` "
                    f"({ev.detail})."
                )
            try:
                await self.messenger.send_text(thread_id, text, silent=True)
            except Exception:
                log.debug(
                    "Failed to post recovery notice to thread %s", thread_id,
                    exc_info=True,
                )

    async def _surface_release_recovery_events(self, events) -> None:
        """Post a notice when a release session crashed mid-tag.

        Each event represents a non-terminal RELEASE/DONE instance that
        committed a vX.Y.Z bump but has no matching git tag — the runner
        has already marked it FAILED. Surface so the user can decide
        whether to retry the release or clean up the bumped commit.
        """
        for ev in events:
            inst = self._store.get_instance(ev.instance_id)
            if not inst or not inst.session_id:
                continue
            try:
                lookup = self._forums.session_to_thread(inst.session_id)
            except Exception:
                lookup = None
            if not lookup:
                continue
            thread_id, _ = lookup
            text = (
                f"Release crashed mid-tag: commit `{ev.commit_sha[:8]}` "
                f"bumped to `{ev.bumped_version}` on `{ev.branch}` but no "
                f"matching tag was created. Marked as failed — re-run "
                f"/release if you want to retry, or revert the bump."
            )
            try:
                await self.messenger.send_text(thread_id, text, silent=True)
            except Exception:
                log.debug(
                    "Failed to post release-recovery notice to thread %s",
                    thread_id, exc_info=True,
                )
