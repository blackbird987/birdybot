"""A typed message: queueing behind the channel lock, running the turn, delivering the result."""

from __future__ import annotations

import asyncio
import logging
import secrets

from bot import config
from bot.claude.types import InstanceStatus, InstanceType
from bot.engine import (
    lifecycle,
    pending as pending_mod,
    sessions as sessions_mod,
)
from bot.platform.base import RequestContext
from bot.platform.formatting import (
    collapse_bot_directives,
    format_elapsed,
    queued_button_specs,
    resolver_running_button_specs,
    running_button_specs,
    strip_verify_blocks,
)
from bot.engine.cmd_state import (
    _get_channel_lock,
    budget_warning,
    check_budget,
)
from bot.engine.directives.dispatch import _execute_bot_commands
from bot.engine.repo_cmds import _try_fast_repo_command
from bot.engine.resolver import (
    _merge_context_prefix,
    _resolver_in_flight,
)

log = logging.getLogger(__name__)


# --- Query ---

async def _enqueue_with_pending_ui(
    ctx: RequestContext, prompt: str,
    *,
    callback_action: str | None = None,
    callback_instance_id: str | None = None,
    callback_source_msg_id: str | None = None,
) -> pending_mod.PendingPrompt | None:
    """Register an interactive 'Queued' entry if the channel lock is held.

    Returns the PendingPrompt on success (caller must skip normal run
    if ``pending.handled_by_steer`` is set after acquiring the lock), or
    None if no pending UI was needed (lock was free).

    When ``callback_action`` is set, the pending represents a button-callback
    dispatch instead of a raw user prompt — Steer re-invokes the callback
    rather than prepending the steering header to text.
    """
    lock = _get_channel_lock(ctx.channel_id)
    if not lock.locked():
        return None
    active_iid = ctx.runner.active_instance_for_session(ctx.session_id)
    supports_steer = (
        ctx.runner.provider.supports_steer and active_iid is not None
    )
    pending_id = secrets.token_hex(4)
    msg_id = await ctx.messenger.send_text(
        ctx.channel_id,
        "Queued — will run after current task.",
        buttons=queued_button_specs(pending_id, supports_steer),
        silent=True,
    )
    # Without a visible embed there are no buttons to click — fall back to
    # the old silent-queue behavior rather than register a ghost entry.
    if not msg_id:
        return None
    return pending_mod.register(
        channel_id=ctx.channel_id,
        session_id=ctx.session_id,
        prompt_text=prompt if callback_action is None else "",
        message_id=str(msg_id),
        active_instance_id=active_iid,
        pending_id=pending_id,
        platform=ctx.platform,
        repo_name=ctx.repo_name,
        user_id=ctx.user_id,
        user_name=ctx.user_name,
        is_owner=ctx.is_owner,
        callback_action=callback_action,
        callback_instance_id=callback_instance_id,
        callback_source_msg_id=callback_source_msg_id,
    )


async def _finish_pending_on_acquire(
    ctx: RequestContext, pending: pending_mod.PendingPrompt | None,
) -> bool:
    """Called after acquiring the channel lock. Returns True if the caller
    should skip executing (because Steer handled it or it was cancelled).
    """
    if pending is None:
        return False
    # Steer already triggered — it kills the prior run, spawns the new one
    # itself, and marks handled_by_steer. Normal lock-holder path bails out.
    if pending.handled_by_steer:
        return True
    if pending.cancelled:
        pending_mod.clear(pending.id)
        return True
    # Normal path: delete the embed, run the prompt
    if pending.message_id:
        try:
            await ctx.messenger.delete_message(
                ctx.channel_id, pending.message_id,
            )
        except Exception:
            pass
    # Re-check after the await — Cancel/Steer can fire during delete_message
    # and flip these flags.  Honor the user's click rather than racing past it.
    if pending.handled_by_steer or pending.cancelled:
        pending_mod.clear(pending.id)
        return True
    pending_mod.clear(pending.id)
    return False


async def on_text(ctx: RequestContext, text: str) -> None:
    """Handle a plain text message — run as query."""
    if not text.strip():
        return
    # Orchestrator wave-cap reset — a genuine user-typed message (not a
    # replay, schedule, autopilot trigger, or callback-resume) means the
    # user is re-engaging this thread. Clear the cap so the next /spawn
    # starts a fresh orchestration run. Every other source leaves the
    # counter intact so the cap survives across the loop.
    if ctx.source == "user_message" and ctx.reset_spawn_wave_count is not None:
        try:
            ctx.reset_spawn_wave_count()
        except Exception:
            log.exception("reset_spawn_wave_count failed")
    # Usage-limit gate: during Anthropic throttle windows the platform offers
    # Run now / Queue for 11am PT / Cancel buttons and returns True if handled.
    if ctx.offer_usage_limit_choice is not None:
        handled = await ctx.offer_usage_limit_choice(ctx, text)
        if handled:
            return
    # Pending-merge handling: when auto-merge has failed but no resolver is in
    # flight, fall through to the normal CLI flow with a merge-context prefix
    # prepended to the user's text. The prefix replaces the previous
    # intercept's role of preventing the resumed session from reinterpreting
    # the message as a fresh task (e.g. "do another review"). If a resolver
    # IS in flight, still capture the text as deferred guidance — spawning a
    # parallel session would race the resolver on the same instance state.
    pending_merge = ctx.store.get_pending_merge_by_channel(ctx.channel_id)
    if pending_merge:
        pm_iid, pm_meta = pending_merge
        if _resolver_in_flight(ctx, pm_iid):
            accumulated = ctx.store.append_pending_merge_deferred_text(pm_iid, text)
            note = (
                "Resolver is already running — saved your message as guidance "
                "for the next Resolve attempt. Tap **Cancel** below to stop "
                "the current run and start over."
            )
            buttons = resolver_running_button_specs(pm_iid)
            preview = accumulated[:200] + "…" if accumulated and len(accumulated) > 200 else accumulated
            if preview:
                note += f"\n\nGuidance so far:\n```\n{preview}\n```"
            try:
                await ctx.messenger.send_text(
                    ctx.channel_id, note, buttons=buttons, silent=True,
                )
            except Exception:
                log.debug("Failed to re-post resolver-running buttons", exc_info=True)
            return
        text = f"{_merge_context_prefix(pm_meta)}\n{text}"
    await _run_query(ctx, text, fast_repo_commands=True)


async def _run_query(
    ctx: RequestContext, prompt: str, *, fast_repo_commands: bool = False,
) -> None:
    """Queue behind the channel's lock, then run `prompt` as a query.

    `fast_repo_commands` lets a typed message that is really a repo command
    be answered without starting a session; only typed text asks for it.
    """
    lock = _get_channel_lock(ctx.channel_id)
    pending = await _enqueue_with_pending_ui(ctx, prompt)
    async with lock:
        if await _finish_pending_on_acquire(ctx, pending):
            return
        if fast_repo_commands and await _try_fast_repo_command(ctx, prompt):
            return
        # Double-checked locking: re-read session_id after acquiring lock
        if not ctx.session_id and ctx.resolve_session_id is not None:
            fresh = ctx.resolve_session_id()
            if fresh:
                ctx.session_id = fresh
        await _execute_query(ctx, prompt)


async def _execute_query(ctx: RequestContext, prompt: str) -> None:
    # Block spawns during reboot drain. Active-session overlap is no longer
    # rejected here — the per-channel lock + Queued embed handle it visibly.
    spawn_err = ctx.runner.check_spawn_allowed()
    if spawn_err:
        if ctx.runner.is_draining:
            ctx.runner.queue_for_replay({
                "channel_id": ctx.channel_id,
                "platform": ctx.platform,
                "prompt": prompt,
                "repo_name": ctx.repo_name,
                "user_id": ctx.user_id,
                "user_name": ctx.user_name,
                "is_owner": ctx.is_owner,
            })
            await ctx.messenger.send_text(
                ctx.channel_id,
                "Reboot in progress — your message will be replayed after restart.",
            )
        else:
            await ctx.messenger.send_text(ctx.channel_id, spawn_err)
        return

    if not check_budget(ctx):
        await ctx.messenger.send_text(
            ctx.channel_id, "Daily budget exceeded. Use /budget reset to override.",
        )
        return

    # Rate limit for non-owner users (callbacks populated by platform layer)
    if not ctx.is_owner and ctx.user_id and ctx.check_rate_limit:
        if not ctx.check_rate_limit():
            limit = ctx.max_daily_queries or "?"
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"Daily query limit reached ({limit}/day). "
                "Ask the bot owner to increase your limit.",
            )
            return
        if ctx.increment_query_count:
            ctx.increment_query_count()

    # Log user attribution
    user_label = f"{ctx.user_name} ({ctx.user_id})" if ctx.user_id else "owner"
    log.info("Query by %s (owner=%s) repo=%s: %s", user_label, ctx.is_owner, ctx.repo_name, prompt[:80])

    # Per-channel repo (Discord) takes priority over global active repo
    if ctx.repo_name:
        repos = ctx.store.list_repos()
        # Case-insensitive lookup (session project "AIAgent" vs repo "aiagent")
        repo_path = repos.get(ctx.repo_name)
        repo_name = ctx.repo_name
        if not repo_path:
            lower_map = {k.lower(): (k, v) for k, v in repos.items()}
            match = lower_map.get(ctx.repo_name.lower())
            if match:
                repo_name, repo_path = match
        if not repo_path:
            available = ", ".join(sorted(repos.keys())) if repos else "none"
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"Repo '{ctx.repo_name}' not found (available: {available}).\n"
                f"To add it: `/repo add {ctx.repo_name} <path>`",
            )
            return
    else:
        # Forum-thread channels always have a deterministic repo binding —
        # falling back to the global active repo silently runs the query
        # against whichever repo the user last switched to (see thread
        # 1498267960257675334 incident, where a steered query ran in
        # Flowchart-read-only because the steer path didn't pass repo_name).
        # resolve_session_id is only wired in attach_session_callbacks, so
        # its presence is a faithful "this is a forum thread" signal without
        # needing a new RequestContext field.
        if ctx.resolve_session_id is not None:
            await ctx.messenger.send_text(
                ctx.channel_id,
                "Missing repo context for this thread — refusing to fall "
                "back to the global active repo. This is a bug; please "
                f"report thread id `{ctx.channel_id}`.",
            )
            log.error(
                "Engine refused get_active_repo() fallback for forum thread "
                "channel=%s (resolve_session_id is set, indicating thread origin)",
                ctx.channel_id,
            )
            return
        repo_name, repo_path = ctx.store.get_active_repo()
    if not repo_path:
        await ctx.messenger.send_text(
            ctx.channel_id, "No repo set. Use /repo add <name> <path> first.",
        )
        return

    # The thread's own session is the only one resumed. Threads are isolated:
    # a thread with no session starts fresh, never on a global one.
    resume_session = ctx.session_id or None

    # Context priming. Two trigger paths:
    #   "cold"   — no resumable session at all (fresh thread, post-account-swap,
    #              post-scrub).  Lightweight ~500-token brief.
    #   "resume" — --resume target's JSONL contains a `compact_boundary` AND
    #              has not yet been re-primed past it.  Larger ~12K-token brief
    #              because Claude Code's compaction summarised away the actual
    #              exchange and the model needs verbatim history to recover.
    # Healthy resumes (no compaction) and already-rehydrated resumes pay
    # zero extra tokens — important for prompt-cache hit rate on long threads.
    prime_briefing: str | None = None
    prime_mode: str | None = None
    if ctx.maybe_prime_briefing is not None:
        if not resume_session:
            prime_mode = "cold"
        else:
            try:
                compacted, rehydrated = await asyncio.to_thread(
                    sessions_mod.session_resume_state, resume_session,
                )
            except Exception:
                log.exception("session_resume_state failed for %s", resume_session)
                compacted, rehydrated = (False, False)
            if compacted and not rehydrated:
                prime_mode = "resume"

    if prime_mode is not None:
        status_text = (
            "Reconstructing context…"
            if prime_mode == "cold"
            else "Refreshing context after long gap…"
        )
        prime_msg_id = None
        try:
            prime_msg_id = await ctx.messenger.send_text(
                ctx.channel_id, status_text,
            )
        except Exception:
            pass
        try:
            prime_briefing = await ctx.maybe_prime_briefing(prime_mode)
        except Exception:
            log.exception("Priming failed for channel %s", ctx.channel_id)
        finally:
            if prime_msg_id:
                try:
                    await ctx.messenger.delete_message(ctx.channel_id, prime_msg_id)
                except Exception:
                    pass

    if prime_briefing:
        # Shared with the runner's context-overflow recovery, which primes a
        # fresh session the same way — see config.prime_preamble. Only the
        # situation sentence differs between the two modes here.
        preamble = config.prime_preamble(
            config.PRIME_SITUATION_COMPACTED
            if prime_mode == "resume"
            else config.PRIME_SITUATION_LOST
        )
        prompt = f"{preamble}\n\n{prime_briefing}\n\n---\n\n{prompt}"
        log.info(
            "Primed %s session in channel %s (+%d chars context)",
            prime_mode, ctx.channel_id, len(prime_briefing),
        )

    inst = ctx.store.create_instance(
        instance_type=InstanceType.QUERY,
        prompt=prompt,
        mode=ctx.effective_mode,
    )
    inst.origin_platform = ctx.platform
    inst.effort = ctx.effective_effort
    # Per-thread /model pin. None (the normal case) leaves the instance
    # unrouted so it falls through to DEFAULT_SESSION_MODEL at command-build
    # time -- unchanged behaviour for every thread that never set one.
    inst.model = ctx.effective_model
    inst.repo_name = repo_name or ""
    inst.repo_path = repo_path or ""
    # User identity and access control
    inst.user_id = ctx.user_id or ""
    inst.user_name = ctx.user_name or ""
    inst.is_owner_session = ctx.is_owner
    if not ctx.is_owner and ctx.bash_policy:
        inst.bash_policy = ctx.bash_policy
        inst.bash_policy_baseline = ctx.bash_policy
    # Stamp spawn_depth on this Instance so the recursion cap in /spawn
    # fires when the spawned session itself tries to spawn. One-shot per
    # request: cleared after consumption. Subsequent prompts in the same
    # spawned thread re-seed via _ctx reading ThreadInfo.spawn_depth.
    if ctx.spawn_depth_inherit > 0:
        inst.spawn_depth = ctx.spawn_depth_inherit
        ctx.spawn_depth_inherit = 0
    if resume_session:
        inst.session_id = resume_session
    inst.status = InstanceStatus.RUNNING
    ctx.store.update_instance(inst, critical=True)

    if resume_session:
        label = "resuming..."
        # Show session context hint so user knows what they're continuing
        if ctx.session_id:
            try:
                fpath = await asyncio.to_thread(sessions_mod.find_session_file, resume_session)
                if fpath:
                    msgs = await asyncio.to_thread(sessions_mod.read_session_messages, fpath, 2)
                    # Find last assistant message for context
                    last_topic = ""
                    for m in reversed(msgs):
                        if m["role"] == "assistant":
                            last_topic = m["text"][:80].replace("\n", " ").strip()
                            break
                    if last_topic:
                        label = f"resuming... (last: {last_topic})"
            except Exception:
                pass
    else:
        label = "processing..."
    escaped = ctx.messenger.escape(inst.display_id())
    handle = await ctx.messenger.send_thinking(
        ctx.channel_id, f"⏳ {escaped} {label}",
        buttons=running_button_specs(inst.id),
    )
    if handle.get("message_id"):
        inst.message_ids.setdefault(ctx.platform, []).append(handle.get("message_id"))
        ctx.store.update_instance(inst)

    (
        on_progress, on_stall, heartbeat, on_recovery, on_context_reset,
    ) = lifecycle.make_progress_callbacks(
        ctx, inst, handle, ctx.effective_verbose,
    )

    heartbeat_task = asyncio.create_task(heartbeat())
    start_time = asyncio.get_event_loop().time()
    ctx.runner.begin_task(inst.id, session_id=inst.session_id, channel_id=ctx.channel_id)
    result = None
    finalized = False
    try:
        try:
            result = await ctx.runner.run(
                inst, on_progress=on_progress, on_stall=on_stall,
                context=ctx.effective_context,
                on_recovery=on_recovery,
                on_context_reset=on_context_reset,
            )
        finally:
            heartbeat_task.cancel()

        # Update thinking message to show completion
        elapsed_str = format_elapsed(asyncio.get_event_loop().time() - start_time)
        icon = "✅" if not result.is_error else "❌"
        try:
            await ctx.messenger.edit_thinking(
                handle, f"{icon} {escaped} done ({elapsed_str})",
            )
        except Exception:
            pass

        lifecycle.finalize_run(ctx, inst, result)
        finalized = True

        # Write session_id back immediately (before lock release), and BEFORE
        # the cooldown early-return below.  A usage-limit failure is not the end
        # of a conversation, it's a pause: _do_cooldown_retry_locked resumes
        # this exact session_id.  Bailing out first meant the thread never
        # learned it — so the retry's work was unreachable and the /watch it
        # armed fired into "thread gone/sessionless, dropping" hours later
        # (2026-08-27, three ev-nova children).
        #
        # Every other error still skips the bind — see should_bind_session.
        #
        # Non-fatal by design: this now sits ABOVE the cooldown scheduling and
        # the result delivery, so a failed state write must not be able to cost
        # the turn its retry or its answer.  It logs at ERROR, and the
        # sessionless-wake fallback in app.on_self_wake is the safety net.
        if lifecycle.should_bind_session(result):
            try:
                await lifecycle.bind_thread_session(ctx, inst, result.session_id)
            except Exception:
                log.exception("Failed to bind session for %s", inst.id)

        # Usage limit: schedule auto-retry instead of showing normal failure
        if await lifecycle.schedule_cooldown_retry(ctx, inst, result):
            return  # Timer loop picks this up — finally: end_task still fires

        # Recovery exhausted: layer-3 fired in the runner (resume failed on every
        # account + index rebuild, fresh session was created).  The thread's prior
        # turns are still on disk under the dead session_id but unreachable from
        # --resume.  Surface this to the user so the dementia is visible — without
        # this notice, the rebind is silent and the user only notices when the
        # bot answers as if it has no memory.
        # Skip when the t-3541 Layer 4 on_recovery callback already posted a
        # richer mid-run warning for this same event — avoids two near-identical
        # alerts for one recovery.
        if result.session_recovery_exhausted and not result.recovery_warning_posted:
            try:
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    "Lost prior conversation context — claude.exe couldn't "
                    "resume the previous session even after recovery attempts. "
                    "Continuing with a fresh session. Re-paste anything important "
                    "from earlier in this thread if you need me to remember it.",
                    silent=True,
                )
            except Exception:
                log.exception("Failed to send session-recovery-exhausted notice")

        # If we primed this turn, drop the cache regardless of outcome.
        # Success path: the new session is now resumable, future messages
        # don't need priming so the cached digest is dead weight. Failure
        # path: history may have grown by retry time, force a rebuild.
        # (Idempotent — a second pop after set_thread_session's rebind pop
        # is a no-op.)
        if prime_briefing and ctx.invalidate_prime is not None:
            try:
                ctx.invalidate_prime()
            except Exception:
                log.debug("invalidate_prime callback raised", exc_info=True)

        # [BOT_CMD: /image] — pictures go up first so the result embed (and its
        # buttons) stays the last thing in the thread.
        if result.result_text and not result.is_error:
            from bot.engine.images import deliver_images
            await deliver_images(ctx, result.result_text, inst)

        # Displayed copy only — directives collapse to one subtext line each
        # (parity with the background path in lifecycle.run_instance, which has
        # always done this). The dispatchers below keep reading RAW text.
        display_text = collapse_bot_directives(strip_verify_blocks(result.result_text))
        await lifecycle.send_result(ctx, inst, display_text, result=result)

        # A /spawn child's own turns run through here, not run_instance, so
        # this is where most children land. The BLOCKED notice is skipped for
        # a turn a human typed: they are already in the child's thread, and a
        # question asked back to them is not the child stuck on its brief.
        await lifecycle.notify_parent(
            ctx, inst, result, display_text,
            skip_blocked=ctx.source == "user_message",
        )

        # Tier 2: scan Claude's response for [BOT_CMD: /repo ...] directives
        if result.result_text and not result.is_error:
            await _execute_bot_commands(ctx, result.result_text, source_inst=inst)

        await budget_warning(ctx)

        # Check reboot request BEFORE end_task so it's queued when end_task
        # checks for pending reboots on idle.  Safe because check_reboot_request
        # just queues (no waiting) — the actual reboot fires from end_task.
        await lifecycle.check_reboot_request(ctx)
        # Self-wake: schedule a thread-bound resume if this turn emitted a
        # [BOT_CMD: /wake] or /watch directive; else reset the runaway counter.
        # The raw result text is passed so the directive can be parsed, and so
        # a promise to report back with nothing armed can be nudged once.
        await lifecycle.check_wake_request(ctx, inst, final_text=result.result_text)
    except asyncio.CancelledError:
        # Shutdown cancelled the turn. A finished run is recorded with its real
        # outcome; one still running is marked interrupted, as run_instance does.
        if not finalized:
            try:
                if result is not None:
                    lifecycle.finalize_run(ctx, inst, result)
                else:
                    lifecycle.mark_interrupted(ctx, inst)
            except Exception:
                log.exception("could not record the interrupted run %s", inst.id)
            if result is None:
                await lifecycle.edit_interrupted_card(ctx, handle)
        raise
    except Exception as e:
        # Without this a runner exception left the chat run RUNNING forever,
        # the t-8954 shape run_instance was already fixed for.
        if not finalized:
            await lifecycle.fail_unfinalized(ctx, inst, handle, e, started=result is not None)
        log.error("chat turn %s raised", inst.id, exc_info=True)
        raise
    finally:
        ctx.runner.end_task(inst.id)
