"""How a turn in a thread is set up and replayed, as a ClaudeBot mixin.

``_ctx`` builds the RequestContext every entry point runs a turn with (rate
and spawn callbacks, spawn colours, wave bookkeeping). The replay methods
re-run a turn into an existing thread after a reboot, a fired wake or a
queued prompt. Moved out of bot.py unchanged.
"""

from __future__ import annotations

import asyncio
import logging

import discord

from bot import config
from bot.discord import channels
from bot.discord.access import AccessResult
from bot.discord import idle as idle_mod
from bot.discord import spawn_colors
from bot.discord.forums import ThreadInfo
from bot.engine import commands
from bot.platform.base import RequestContext, SpawnArgs, SpawnResult
from bot.platform.modes import cap_mode

log = logging.getLogger(__name__)


class ThreadContextMixin:
    """ClaudeBot methods; see the module docstring."""

    def _ctx(
        self, channel_id: str,
        session_id: str | None = None,
        repo_name: str | None = None,
        thread_info: ThreadInfo | None = None,
        access_result: AccessResult | None = None,
        source: str = "system",
    ) -> RequestContext:
        ctx = RequestContext(
            messenger=self.messenger,
            channel_id=channel_id,
            platform="discord",
            store=self._store,
            runner=self._runner,
            session_id=session_id,
            repo_name=repo_name,
            source=source,
        )
        if thread_info:
            ctx.mode = thread_info.mode
            ctx.context = thread_info.context
            ctx.verbose_level = thread_info.verbose_level
            ctx.effort = thread_info.effort
            ctx.model = thread_info.model
            ctx.spawn_depth_inherit = thread_info.spawn_depth
        if access_result:
            ctx.is_owner = access_result.is_owner
            if not access_result.is_owner and access_result.mode_ceiling:
                ctx.mode_ceiling = access_result.mode_ceiling
                current = ctx.mode or self._store.mode
                ctx.mode = cap_mode(current, access_result.mode_ceiling)
            if not access_result.is_owner and access_result.bash_policy:
                ctx.bash_policy = access_result.bash_policy
            if not access_result.is_owner and access_result.max_daily_queries > 0:
                ctx.max_daily_queries = access_result.max_daily_queries

                def _make_rate_callbacks(uid, max_q):
                    def _check():
                        from bot.discord.access import load_access_config, check_rate_limit
                        return check_rate_limit(load_access_config(), uid, max_q)

                    def _increment():
                        from bot.discord.access import load_access_config, increment_query_count
                        increment_query_count(load_access_config(), uid)
                    return _check, _increment
                ctx.check_rate_limit, ctx.increment_query_count = _make_rate_callbacks(
                    ctx.user_id or "", access_result.max_daily_queries,
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

        # [BOT_CMD: /spawn] — engine calls this when a directive in the
        # assistant's final response asks the bot to hand off a generated
        # prompt to a fresh thread. We create the thread, build a child
        # ctx for it, stamp spawn_depth, and dispatch through on_text.
        async def _spawn_session(args: SpawnArgs) -> SpawnResult:
            parent_id = ctx.channel_id
            parent_lookup = _bot._forums.thread_to_project(parent_id)
            parent_forum_project = parent_lookup[0] if parent_lookup else None

            # Spawn-color: assign a slot for this family (reusing the parent's
            # existing slot, the root's stamped slot for root-revival, or the
            # lowest unused). Rename the parent once to prepend the colored
            # square, and pass an explicit child name override carrying the dot.
            assigned_slot: int | None = None
            root_id: str | None = None
            child_name_override: str | None = None
            if parent_forum_project is not None:
                result = await spawn_colors.assign_slot(
                    parent_id,
                    parent_forum_project,
                    _bot._store,
                    _bot._forums,
                )
                if result is not None:
                    assigned_slot, root_id = result
                    sanitized = (
                        channels.build_channel_name(args.title)
                        if args.title and args.title != "new-session"
                        else "new-session"
                    )
                    child_name_override = spawn_colors.compose_for_slot(
                        assigned_slot, sanitized, is_root=False,
                    )

            # Rename the family root to prepend the square emoji. A /spawn
            # event implies activity, so we treat the rename as a wake: any
            # existing 💤 prefix is dropped, and only the color square remains.
            # Reuses the _name_editing guard to not race _generate_smart_title.
            if assigned_slot is not None and root_id is not None:
                square = spawn_colors.prefix_for_root(assigned_slot)
                root_channel = _bot.get_channel(int(root_id))
                if isinstance(root_channel, discord.Thread):
                    _is_sleeping, bare_topic = channels.parse_thread_name(root_channel.name)
                    new_root_name = f"{square} {bare_topic}"[:100]
                    if root_channel.name != new_root_name and root_id not in _bot._name_editing:
                        _bot._name_editing.add(root_id)
                        try:
                            await root_channel.edit(name=new_root_name)
                            # Activity implies awake — cancel any pending sleep
                            # timer so it doesn't immediately reapply 💤.
                            idle_mod.cancel_sleep(_bot, root_id)
                        except Exception:
                            log.warning(
                                "Failed to prepend spawn-color square to root thread %s",
                                root_id, exc_info=True,
                            )
                        finally:
                            _bot._name_editing.discard(root_id)

            thread = await _bot._forums.get_or_create_session_thread(
                args.repo,
                session_id=None,
                topic=args.title,
                origin="spawn",
                user_id=ctx.user_id or None,
                user_name=ctx.user_name or None,
                name_override=child_name_override,
            )
            if thread is None:
                raise RuntimeError("get_or_create_session_thread returned None")
            new_channel_id = str(thread.id)
            lookup = _bot._forums.thread_to_project(new_channel_id)
            new_info = lookup[1] if lookup else None
            if new_info is not None:
                new_info.spawn_depth = args.parent_depth + 1
                new_info.mode = args.mode
                if args.effort:
                    new_info.effort = args.effort
                # Orchestrator loop: record parent linkage on the child so
                # _maybe_callback_parent (in engine.lifecycle) can ping back
                # when this child finalizes.
                new_info.parent_thread_id = ctx.channel_id
                _bot._store.save()

            # Register the new child in its family. Done after parent_thread_id
            # is stamped so find_root sees the link.
            if assigned_slot is not None and root_id is not None and lookup is not None:
                child_forum_project = lookup[0]
                try:
                    await spawn_colors.register_member(
                        root_id, new_channel_id,
                        child_forum_project, _bot._store, _bot._forums,
                    )
                except Exception:
                    log.warning(
                        "Failed to register spawn-color member %s for root %s",
                        new_channel_id, root_id, exc_info=True,
                    )
            # Wave cap accounting: bump the parent's counter so the next
            # /spawn from the same orchestration run sees an incremented
            # value. Counter resets when the user types a fresh message
            # (see commands.on_text reset block). Reuses parent_lookup
            # from the spawn-color block above — same thread, same ref.
            if parent_lookup is not None:
                _, parent_info = parent_lookup
                parent_info.spawn_wave_count += 1
                _bot._store.save()
            new_ctx = _bot._ctx(
                new_channel_id,
                session_id=None,
                repo_name=args.repo,
                thread_info=new_info,
                source="spawn_dispatch",
            )
            # Inherit identity + access from the parent ctx so non-owner spawns
            # carry the same gating into the child thread.
            new_ctx.user_id = ctx.user_id
            new_ctx.user_name = ctx.user_name
            new_ctx.is_owner = ctx.is_owner
            new_ctx.mode_ceiling = ctx.mode_ceiling
            new_ctx.bash_policy = ctx.bash_policy
            new_ctx.max_daily_queries = ctx.max_daily_queries
            new_ctx.check_rate_limit = ctx.check_rate_limit
            new_ctx.increment_query_count = ctx.increment_query_count
            if new_info is not None:
                _bot._forums.attach_session_callbacks(new_ctx, new_info, new_channel_id)
            # Skip the "Reconstructing context…" priming pass on this dispatch:
            # a spawned thread has no prior conversation history that could
            # actually inform the run, and the generated prompt is meant to be
            # self-contained. Leaving it on adds a transient noise message in
            # the brand-new thread for no benefit.
            new_ctx.maybe_prime_briefing = None

            async def _dispatch():
                try:
                    await commands.on_text(new_ctx, args.prompt)
                    if new_info is not None:
                        _bot._forums.persist_ctx_settings(new_ctx)
                    asyncio.create_task(_bot._try_apply_tags_after_run(new_channel_id))
                except Exception:
                    log.exception("spawn dispatch failed in thread %s", new_channel_id)

            asyncio.create_task(_dispatch())
            mention = f"<#{new_channel_id}>"
            url = None
            if thread.guild is not None:
                url = f"https://discord.com/channels/{thread.guild.id}/{new_channel_id}"
            return SpawnResult(
                thread_id=new_channel_id,
                thread_mention=mention,
                thread_url=url,
            )

        ctx.spawn_session = _spawn_session

        # Orchestrator seam: when a spawned child finalizes (COMPLETED/FAILED),
        # post a callback into the recorded parent thread with a Resume button.
        # Closure captures (_bot, channel_id) — the engine just calls it.
        async def _notify_parent(status: str, summary: str) -> None:
            from bot.discord.orchestrator import post_parent_callback
            await post_parent_callback(_bot, channel_id, status, summary)

        ctx.notify_parent_on_finalize = _notify_parent

        # [BOT_CMD: /reply] — deliver a parent's answer into one of its own
        # spawned children. The engine has already checked that the target is
        # a thread THIS session spawned, so this closure only has to make the
        # thread reachable and dispatch.
        async def _reply_to_child(child_thread_id: str, prompt: str) -> bool:
            lookup = _bot._forums.thread_to_project(child_thread_id)
            if lookup is None:
                log.warning("/reply — no thread mapping for child %s", child_thread_id)
                return False
            child_ch = _bot.get_channel(int(child_thread_id))
            if isinstance(child_ch, discord.Thread) and child_ch.archived:
                # A child that finished and got archived can still be answered;
                # posting into it is what reopens it.
                try:
                    await child_ch.edit(archived=False)
                except discord.HTTPException:
                    log.debug("Could not unarchive child thread %s", child_thread_id)

            async def _dispatch() -> None:
                try:
                    # source stays non-"user_message" so answering a child does
                    # not reset that child's own spawn-wave counter.
                    await _bot._replay_to_thread(
                        child_thread_id, prompt, source="parent_reply",
                    )
                except Exception:
                    log.exception("/reply dispatch failed for child %s", child_thread_id)

            # Detached: the parent's turn is finalizing right now and must not
            # block on the child's whole run.
            asyncio.create_task(_dispatch())
            return True

        ctx.reply_to_child = _reply_to_child

        # Orchestrator join: the /spawn dispatch loop finished, so this
        # thread's wave roster is final. Re-check it in case every child
        # already reported while the loop was still creating threads.
        async def _wave_sealed() -> None:
            from bot.discord.orchestrator import evaluate_wave_now
            await evaluate_wave_now(_bot, channel_id)

        ctx.on_spawn_wave_sealed = _wave_sealed

        # Per-thread counters on ThreadInfo: the spawn-wave cap, the self-wake
        # runaway cap and the unattended-nudge cap. Each accessor looks the
        # thread up on demand, so it reflects the latest state.json and never
        # a stale snapshot, and saves through so a count survives a reboot.
        def _counter(field: str):
            def _info():
                lookup = _bot._forums.thread_to_project(channel_id)
                return lookup[1] if lookup else None

            def read() -> int:
                info = _info()
                return getattr(info, field) if info else 0

            def bump() -> int:
                info = _info()
                if info is None:
                    return 0
                setattr(info, field, getattr(info, field) + 1)
                _bot._store.save()
                return getattr(info, field)

            def reset() -> None:
                info = _info()
                if info is not None and getattr(info, field) != 0:
                    setattr(info, field, 0)
                    _bot._store.save()

            return read, bump, reset

        ctx.read_spawn_wave_count, _, ctx.reset_spawn_wave_count = _counter("spawn_wave_count")
        _, ctx.bump_wake_count, ctx.reset_wake_count = _counter("wake_count")
        _, ctx.bump_nudge_count, ctx.reset_nudge_count = _counter("nudge_count")
        return ctx

    # --- Resume after reboot ---

    async def _replay_to_thread(
        self, channel_id: str, prompt: str, repo_name: str | None = None,
        *, source: str = "replay",
    ) -> bool:
        """Look up a thread, build context, and dispatch a prompt through on_text.

        Returns True on success, False if the thread wasn't found.

        *source* tags the RequestContext so the engine can distinguish
        callback-resume replays from normal replays for the orchestrator
        wave-cap reset. Defaults to "replay" (non-resetting).
        """
        lookup = self._forums.thread_to_project(channel_id)
        if not lookup:
            log.warning("replay_to_thread: no thread mapping for %s", channel_id)
            return False
        proj, info = lookup
        # Every unattended resume lands here: a fired self-wake, a tripped
        # /watch, a `--here` schedule, an orchestrator wave join, a
        # post-reboot replay. None of them go through
        # get_or_create_session_thread (the thread already exists), so
        # without this a hidden repo would keep working and keep posting
        # into a forum parked out of the sidebar.
        try:
            await self._forums.wake_repo_if_dormant(
                proj.repo_name, notify_channel_id=channel_id)
        except Exception:
            log.debug("Wake-on-work check failed for %s",
                      proj.repo_name, exc_info=True)
        session_id = info.session_id or None
        resolved_repo = repo_name or (
            proj.repo_name if proj.repo_name != "_default" else None
        )
        self._cancel_sleep(channel_id)
        ctx = self._ctx(channel_id, session_id=session_id, repo_name=resolved_repo,
                        thread_info=info, source=source)
        # Wire on_session_resolved so the session_id this replay produces
        # gets registered to info.session_id. Without it, the replay's
        # session lives only on disk (query.py only fires the
        # callback when it's set) — the next user message then finds
        # info.session_id still empty, starts cold, and loses the
        # plan/context this replay just produced. Parity with every
        # other entry point (user-message, lobby, callback, modal,
        # slash, interactions, spawn, app-resume) which all attach.
        self._forums.attach_session_callbacks(ctx, info, channel_id)
        # Replay bypasses the usage-limit gate: the window-end promoter already
        # classified these as queued, and "Run now" clicks have already been
        # consented to.  Re-prompting would loop.
        ctx.offer_usage_limit_choice = None
        await commands.on_text(ctx, prompt)
        await self._after_turn(ctx, channel_id, dashboard=False)
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
                        thread_info=info, source="replay")
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

        # source_msg_id was added in 0.87 for gated-button replay; pre-0.87
        # entries (drain-queue) won't have it — .get() keeps that path
        # backward-compatible (handle_callback tolerates None).
        source_msg_id = entry.get("source_msg_id")

        log.info("Replaying callback %s:%s in thread %s",
                 button_action, instance_id[:12], channel_id)

        # Acquire channel lock — same as normal button path in interactions.py
        from bot.engine.commands import _get_channel_lock
        lock = _get_channel_lock(channel_id)
        async with lock:
            try:
                await commands.handle_callback(
                    ctx, button_action, instance_id, source_msg_id=source_msg_id,
                )
            except Exception:
                log.exception("Failed to replay callback %s in thread %s",
                              button_action, channel_id)
            finally:
                await self._after_turn(ctx, channel_id, dashboard=False)
