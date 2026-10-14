"""Bot orchestrator — starts Discord bot with shared state."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from bot import auto_update, autonomy, boot_recovery, config, sd_watchdog
from bot.claude.auth_health import (
    REASON_NO_DIR,
    relogin_command,
    unusable_reason,
)
from bot.claude import memory as claude_memory
from bot.claude.runner import ClaudeRunner
from bot.claude.types import InstanceStatus
from bot.engine import commands as engine_commands
from bot.platform.base import NotificationService
from bot.platform.formatting import redact_secrets
from bot.procutil import (
    clear_stop_request,
    detached_kwargs,
    harden_git_env,
    is_bot_process,
    is_process_alive,
    stop_was_requested,
)
from bot.scheduler import Scheduler
from bot.store.state import StateStore

log = logging.getLogger(__name__)


def setup_logging() -> None:
    """Configure rotating file handler + console."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        str(config.LOG_FILE),
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler (skip if running headless via pythonw)
    if sys.stdout is not None:
        # Force UTF-8 on Windows console to avoid cp1252 encoding errors
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        root.addHandler(ch)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)


def _acquire_pid_lock() -> bool:
    """Write PID file, refusing to start if another instance is alive.

    Liveness alone is not enough: after an unclean exit the PID file outlives
    the process, and the kernel eventually recycles that number onto something
    unrelated — at which point the bot would refuse to start forever with no
    second instance anywhere. So the PID must also still *look* like the bot.
    ``is_bot_process`` only says no on positive evidence, so an unreadable or
    ambiguous process is still treated as a live instance and we stand down.
    """
    pid_file = config.DATA_DIR / "bot.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            if old_pid != os.getpid() and is_process_alive(old_pid):
                # No `root` here on purpose: a false "not ours" would start a
                # second instance, so only the argv evidence is used. botctl
                # can afford the stricter check because its failure mode is
                # refusing to signal.
                if is_bot_process(old_pid):
                    log.error(
                        "Another bot instance is running (PID %d). Exiting.", old_pid
                    )
                    return False
                log.info(
                    "PID file holds %d, but that process is not this bot "
                    "(recycled PID) — taking over", old_pid
                )
            else:
                log.info("Stale PID file (PID %d no longer running), taking over", old_pid)
        except (ValueError, OSError):
            pass  # Corrupt PID file, overwrite it

    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    # A stop request belongs to the process it was aimed at. Anything still
    # here was left by a caller that died mid-stop, and letting it survive
    # would turn the next emergency kill into a permanent one.
    clear_stop_request(config.DATA_DIR)
    # The one line that marks where a boot begins. The health check has always
    # looked for it and it had never been written, so the check could not tell
    # this run's startup from the previous run's and judged a clean boot by
    # ten hours of unrelated runtime.
    log.info("Acquired PID lock (PID %d)", os.getpid())
    return True


def _release_pid_lock() -> None:
    """Remove PID file on clean shutdown."""
    pid_file = config.DATA_DIR / "bot.pid"
    try:
        if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
            pid_file.unlink()
    except OSError:
        pass


def _write_reboot_msg_and_relaunch(reboot_data: dict) -> None:
    """Leave the next process its reboot note, then spawn the relauncher.

    The one place both restart paths (the coalesced `/reboot` executor and the
    Windows Ctrl+Break handler) write ``REBOOT_MSG_FILE`` and start
    ``scripts/relaunch.py``. The relauncher stands down under the systemd unit,
    whose ``Restart=always`` brings the bot back instead. A failure to write
    the note is logged and does not stop the relaunch; a failure to spawn the
    relauncher raises, so the caller decides whether that aborts the reboot.
    """
    import json
    import subprocess

    try:
        config.REBOOT_MSG_FILE.write_text(json.dumps(reboot_data), encoding="utf-8")
    except Exception:
        log.warning("Failed to write reboot message file", exc_info=True)
    launcher = config._PROJECT_ROOT / "scripts" / "relaunch.py"
    subprocess.Popen(
        [sys.executable, str(launcher), str(config._PROJECT_ROOT)],
        **detached_kwargs(),
    )


async def run() -> None:
    """Main async entry point."""
    setup_logging()
    log.info("Starting Claude Bot...")

    # Before anything can shell out to git. See harden_git_env() -- without
    # this a missing credential is a 30s timeout with no cause in the log.
    harden_git_env()

    # How many accounts exist but can't authenticate right now.  A count, not a
    # table: surfacing these in The Ark is reconcile_account_health()'s job
    # below, which reads disk after the runner is up and so also catches
    # accounts that went out (or came back) while the bot was down.
    degraded = 0

    if config.CLAUDE_ACCOUNTS:
        configured_count = len(config.CLAUDE_ACCOUNTS)
        log.info(
            "Claude accounts configured: %d (%s)",
            configured_count,
            ", ".join(config.CLAUDE_ACCOUNTS),
        )
        # Structurally-invalid entries (missing dir) are pruned for good --
        # nothing can rehabilitate them without an .env edit + restart anyway.
        # Logged-out entries are NOT pruned: the runner skips them per-run and
        # rejoins them the moment credentials reappear, so a `/login` heals
        # rotation without a reboot.
        valid: list[str] = []
        for acct in config.CLAUDE_ACCOUNTS:
            reason = unusable_reason(acct)
            if reason == REASON_NO_DIR:
                log.error(
                    "CLAUDE_ACCOUNTS entry dropped: %s (%s). "
                    "See docs/design/multi-account.md.",
                    acct,
                    reason,
                )
                continue
            valid.append(acct)
            if reason:
                degraded += 1
                # WARNING, not ERROR: a sidelined account is a *handled*
                # condition -- rotation carries on without it and it rejoins on
                # its own after a login. Logging it at ERROR meant a signed-out
                # backup account made every startup read as a failed startup,
                # which is also how the aggregate line below already treats it.
                log.warning(
                    "CLAUDE_ACCOUNTS entry sidelined: %s (%s). It stays in "
                    "rotation config and rejoins automatically after login -- "
                    "run: %s",
                    acct,
                    reason,
                    relogin_command(acct),
                )
        # Through the setter, not a bare assignment: dropping an entry can move
        # the primary account, and the projects root and CLAUDE_CONFIG_DIR pin
        # are both derived from it.
        config.set_accounts(valid)
        usable = len(valid) - degraded
        if usable != configured_count:
            log.warning(
                "Multi-account failover degraded: %d of %d accounts usable",
                usable,
                configured_count,
            )

    if not config.CLAUDE_ACCOUNTS:
        log.warning(
            "CLAUDE_ACCOUNTS unset or all invalid -- running single-account "
            "from default ~/.claude. Failover is DISABLED. "
            "See docs/design/multi-account.md."
        )

    if not _acquire_pid_lock():
        return

    start_time = time.time()
    stop_event = asyncio.Event()

    # systemd's watchdog, armed as early as possible: its timer runs from
    # service start, so a slow but healthy startup still has to ping inside
    # WatchdogSec, and a hang anywhere in startup is caught too. Inert unless
    # the unit sets WatchdogSec=. The health check binds
    # `discord_bot` late, which is why it is declared here and not where
    # Discord starts. See docs/design/sd-watchdog.md.
    discord_bot = None
    sd_watchdog_task = asyncio.create_task(sd_watchdog.watchdog_loop(
        sd_watchdog.DownGate(
            lambda: _discord_is_down(discord_bot),
            config.SD_WATCHDOG_DOWN_GRACE_SECS,
        ),
    ))

    # Initialize shared store
    store = StateStore(
        state_file=config.STATE_FILE,
        results_dir=config.RESULTS_DIR,
        retention_days=config.INSTANCE_RETENTION_DAYS,
        max_retained=config.INSTANCE_MAX_RETAINED,
    )

    boot_recovery.audit_repo_portability(store)

    orphans = store.mark_orphans()
    # Stays None on a clean restart and on every non-Linux host — read much
    # later, when the orphan messages go out, so it must always be bound.
    oom_reason: str | None = None
    if orphans:
        log.warning("Marked %d orphaned instances as failed", len(orphans))
        # Ask *why* we restarted, but only when sessions actually died with us —
        # on a clean restart there is nobody to tell, and the journal read isn't
        # free. Every interruption used to collapse to "interrupted by bot
        # restart", which is how the machine running out of memory twice in one
        # night (2026-08-17, 01:09 and 02:34) presented as a bot bug and took a
        # full forensic session to pin down. One question at startup replaces
        # that whole investigation.
        oom_reason = claude_memory.previous_run_was_oom_killed()
        if oom_reason:
            log.error(
                "Previous run ended in an OOM kill — %s. %d session(s) died "
                "with it.", oom_reason, len(orphans),
            )
            claude_memory.write_oom_marker(config.DATA_DIR, oom_reason)

    archive_count = store.archive_old()
    if archive_count:
        log.info("Archived %d old instances", archive_count)

    # Sweep stale title-gen jsonls so /session pickers don't list "[Temp]…"
    # entries from prior runs that crashed before per-call cleanup ran.
    try:
        from bot.discord.titles import cleanup_stale_temp_jsonls
        removed = cleanup_stale_temp_jsonls()
        if removed:
            log.info("Cleaned %d stale title-gen jsonl(s)", removed)
    except Exception:
        log.warning("Stale title-gen cleanup failed at startup", exc_info=True)

    # Restore provider from state (overrides env var if explicitly switched at runtime)
    if store.active_provider and store.active_provider != config.PROVIDER:
        try:
            config.set_provider(store.active_provider)
            log.info("Restored provider from state: %s", store.active_provider)
        except RuntimeError as exc:
            log.warning("Could not restore provider '%s': %s", store.active_provider, exc)

    # Initialize shared runner
    runner = ClaudeRunner(store=store)

    # Bring the account alert table in line with what's actually on disk right
    # now: announce accounts that went out while we were down, and retire
    # sidelines on accounts that were signed back in.  Needs the runner (it
    # owns those rules), so it can't move up next to the store.
    try:
        runner.reconcile_account_health()
    except Exception:
        log.warning("Account health reconcile failed at startup", exc_info=True)

    try:
        cli_version = await runner.check_cli()
        log.info("Claude CLI version: %s", cli_version)
    except RuntimeError as e:
        log.error("Claude CLI self-test failed: %s", e)
        cli_version = f"FAILED: {e}"

    # Initialize engine module state
    engine_commands.init(start_time, cli_version, shutdown_fn=lambda: stop_event.set())

    # Capture deploy state baselines for all repos, then register the deploy
    # config each repo implies.
    from bot.engine.deploy import auto_register_deploy_configs, capture_boot_baselines
    capture_boot_baselines(store, str(config._PROJECT_ROOT))
    auto_register_deploy_configs(store)

    # Emergency signal handler for Windows Ctrl+Break (SIGBREAK): save state
    # and relaunch, so a bot broken into from a console comes back online.
    #
    # It is deliberately NOT registered for SIGTERM. The graceful handler
    # further down has always owned SIGTERM (it is registered later and
    # replaced this one from the day this was added), and on Linux the
    # restart after a SIGTERM is the systemd unit's job (`Restart=always`).
    def _emergency_reboot_handler(signum, frame):
        # A caller that means "stop" leaves a sentinel; consult it before
        # anything else, since this runs as a signal handler.
        if stop_was_requested(config.DATA_DIR):
            log.warning("Caught signal %s, deliberate stop, not relaunching", signum)
            store.save()
            _release_pid_lock()
            os._exit(0)

        log.warning("Caught signal %s, emergency reboot", signum)
        # No channel and no resume prompt: the note only makes the next
        # process announce that it is back. (This used to record a running
        # instance's first *message* id as the channel to resume, which
        # nothing could resume into.)
        try:
            _write_reboot_msg_and_relaunch({"message": "recovered from an interrupt signal"})
        except Exception:
            log.exception("Emergency relaunch could not be spawned")
        store.save()
        _release_pid_lock()
        os._exit(1)

    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _emergency_reboot_handler)

    # Notification service
    notifier = NotificationService()

    # Reboot coalescing: register idle callback that executes pending reboots
    # after all tasks finish. Multiple autopilots requesting reboots produce
    # a single reboot instead of racing.
    async def _execute_pending_reboots() -> None:
        """Called by runner when idle + reboots pending. Runs at most once."""
        reboots = runner.pending_reboots()
        if not reboots:
            runner._reboot_executing = False
            return

        try:
            last = reboots[-1]
            reason = last.get("message", "reboot requested")

            # Reboot notification removed — control room embed already shows
            # drain/reboot state, and ephemeral ack is sent by the button handler.
            # Sending messages here created orphaned clutter in control rooms
            # since _deploy_status_msgs is lost on restart.

            # Merge resume prompts from all requesters
            resume_parts = [r["resume_prompt"] for r in reboots if r.get("resume_prompt")]
            merged_prompt = "\n---\n".join(resume_parts) if resume_parts else None

            reboot_data = {
                "channel_id": last.get("channel_id", ""),
                "platform": last.get("platform", ""),
                "message": reason,
            }
            if merged_prompt:
                reboot_data["resume_prompt"] = merged_prompt

            log.info("Executing coalesced reboot (%d requests merged)", len(reboots))
            _write_reboot_msg_and_relaunch(reboot_data)
            runner.clear_reboots()
            stop_event.set()
        except Exception:
            log.exception("Reboot executor failed — resetting for retry")
            runner.clear_reboots()
            runner.purge_drain_queue()  # discard stale queued messages
            runner._reboot_executing = False

    runner.set_on_idle_reboot(_execute_pending_reboots)

    # Schedule result callback
    async def on_schedule_result(instance, result, changed):
        if instance.status == InstanceStatus.FAILED:
            escaped = redact_secrets(instance.error or 'Unknown error')
            await notifier.broadcast(
                f"**Scheduled task failed**\n{instance.display_id()}: {escaped}",
                ttl=15,
            )
        elif changed:
            escaped = redact_secrets(instance.summary or 'No summary')
            await notifier.broadcast(
                f"{instance.display_id()} (scheduled)\n{escaped}",
                silent=True, ttl=15,
            )

    # Self-wake callback — resumes a thread's session for a thread-bound wake.
    # `discord_bot` is assigned later in this function; the closure binds it
    # late (only ever called at runtime on the 30s tick, well after startup).
    async def on_self_wake(channel_id: str, prompt: str) -> str:
        if discord_bot is None:
            return "drop"          # Discord disabled — nothing will ever resume it
        # Not ready yet (bot still connecting / forum map not loaded). A persisted
        # overdue wake can come due on the first tick before this is true — re-arm
        # rather than drop it, since thread_to_project returns None for ALL
        # channels until the map loads (indistinguishable from "thread gone").
        if not discord_bot._ready_event.is_set() or not discord_bot._forums.forum_projects:
            return "busy"
        lookup = discord_bot._forums.thread_to_project(channel_id)
        if lookup is None:
            log.info("Self-wake: thread %s gone, dropping", channel_id)
            return "drop"          # dead/merged/closed thread — don't resurface
        if not lookup[1].session_id:
            # Thread is alive but has no session bound. Used to be lumped in
            # with "gone" and dropped — which is how three overnight runs
            # finished their work and then vanished in silence (2026-08-27).
            # A wake body is written to stand on its own ("read the log tail,
            # report the numbers"), so dispatching cold into the right thread
            # beats saying nothing: _replay_to_thread starts a fresh session
            # there. The bind fixes above make this a rare last resort.
            log.warning(
                "Self-wake: thread %s is alive but sessionless — dispatching "
                "cold rather than dropping", channel_id,
            )
        if runner.active_instance_for_channel(channel_id):
            return "busy"          # mid-turn — scheduler re-arms instead of colliding
        # A fired wake runs unattended. Append the end-of-turn protocol so this
        # turn knows to finish with [TURN_COMPLETE] or schedule a fresh wake —
        # otherwise check_wake_request auto-nudges it. (The nudge prompt already
        # says this; the extra copy is harmless reinforcement.)
        wake_prompt = prompt + config.UNATTENDED_TURN_PROTOCOL
        ok = await discord_bot._replay_to_thread(channel_id, wake_prompt, source="wake")
        return "done" if ok else "drop"

    def _watch_messenger():
        # Late-bound like on_self_wake above: discord_bot is assigned further
        # down this function, and this is only ever called on the 30s tick.
        if discord_bot is None or not discord_bot._ready_event.is_set():
            return None
        return discord_bot.messenger

    scheduler = Scheduler(
        store, runner, on_result=on_schedule_result, on_wake=on_self_wake,
        messenger_getter=_watch_messenger,
    )
    scheduler.recalculate_next_runs()

    # Declared nudges are reconciled after recalculate_next_runs (which skips
    # labelled rows) so the config, not the generic missed-run recovery, owns
    # their clock. Reconciling every boot is what makes editing config/nudges.json
    # the only step needed to change what the bot sends unprompted.
    from bot.engine import nudges as _nudges
    try:
        _nudge_summary = _nudges.reconcile(store, config.NUDGES_FILE)
        if any(_nudge_summary.get(k) for k in
               ("created", "updated", "removed", "error")):
            log.info("Nudges reconciled: %s", _nudge_summary)
    except Exception:
        # Never let a nudge config fault stop the bot from booting; reconcile()
        # already swallows config errors, so reaching here means a real bug.
        log.exception("Nudge reconciliation failed")

    # Background tasks
    async def auto_save_loop():
        tick = 0
        while True:
            await asyncio.sleep(60)
            tick += 1
            try:
                # Prune accumulated terminal instances every ~30 min so
                # state.json stays small and saves stay cheap.
                if tick % 30 == 0:
                    pruned = store.archive_old()
                    if pruned:
                        log.info("Auto-pruned %d old instances", pruned)
                # Refresh the .bak backup here (not on every hot-path save).
                store.save_if_dirty(backup=True)
            except Exception:
                log.exception("Auto-save failed")

    # Signal handling
    def signal_handler(sig, frame):
        log.info("Received signal %s, shutting down...", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start platform tasks
    platform_tasks = []

    # --- Discord ---
    if config.DISCORD_ENABLED:
        try:
            discord_bot = await _start_discord(store, runner, notifier, stop_event)
            platform_tasks.append(("discord", discord_bot))
            log.info("Discord platform started")
        except Exception:
            log.exception("Failed to start Discord platform")

    if not platform_tasks:
        log.error("No platforms started successfully!")
        return

    # Cooldown retry loop — auto-retries instances that hit usage limits
    _cooldown_retrying: set[str] = set()

    async def cooldown_loop():
        from datetime import datetime as dt, timezone as tz_mod
        from bot.engine import lifecycle
        while True:
            await asyncio.sleep(60)
            try:
                now = dt.now(tz_mod.utc)
                all_instances = store.list_instances(all_=True)

                # Build a fast lookup: session_id -> [completed instances by created_at desc]
                # to avoid O(n²) search for each cooldown instance
                completed_by_session: dict[str, list] = {}
                for s in all_instances:
                    if s.status == InstanceStatus.COMPLETED and s.session_id:
                        if s.session_id not in completed_by_session:
                            completed_by_session[s.session_id] = []
                        completed_by_session[s.session_id].append(s)
                # Sort by created_at descending so most recent is first
                for sids in completed_by_session.values():
                    sids.sort(key=lambda x: x.created_at or "", reverse=True)

                # Asked once per pass, not per instance: it purges expired
                # cooldowns and re-probes credentials, and eighteen parked
                # instances asking the same question eighteen times is the
                # same answer at eighteen times the cost.
                accounts_free = runner.has_spawnable_account()

                for inst in all_instances:
                    if not inst.cooldown_retry_at or not inst.cooldown_channel_id:
                        continue
                    if inst.id in _cooldown_retrying:
                        continue
                    if lifecycle.cooldown_retry_is_due(
                        inst.cooldown_retry_at, now, accounts_free,
                    ):
                        if not lifecycle.cooldown_retry_is_due(
                            inst.cooldown_retry_at, now, accounts_free=False,
                        ):
                            log.info(
                                "Pulling cooldown retry for %s forward from %s "
                                "(an account is available again)",
                                inst.id, inst.cooldown_retry_at,
                            )
                        # Skip if session already has completed work after this instance
                        # (e.g. user switched accounts and finished the task manually)
                        # O(1) lookup via pre-built dict instead of O(n) scan per instance
                        completed_after = (
                            inst.session_id
                            and inst.session_id in completed_by_session
                            and completed_by_session[inst.session_id]
                            and (completed_by_session[inst.session_id][0].created_at or "") > (inst.created_at or "")
                        )
                        if completed_after:
                            log.info("Skipping stale cooldown retry for %s — session %s already completed",
                                     inst.id, inst.session_id)
                            inst.cooldown_retry_at = None
                            inst.cooldown_channel_id = None
                            store.update_instance(inst)
                            continue

                        _cooldown_retrying.add(inst.id)
                        asyncio.create_task(
                            _do_cooldown_retry(store, runner, inst, discord_bot, _cooldown_retrying)
                        )
            except Exception:
                log.exception("Cooldown loop error")

    # Start background tasks (store refs to prevent GC)
    _bg_tasks = [
        sd_watchdog_task,
        asyncio.create_task(auto_save_loop()),
        asyncio.create_task(cooldown_loop()),
        asyncio.create_task(autonomy.autonomy_loop(store, runner, discord_bot)),
    ]
    if config.AUTO_UPDATE:
        _bg_tasks.append(asyncio.create_task(
            auto_update.auto_update_loop(stop_event, runner, notifier),
        ))
    if discord_bot:
        from bot.discord.account_alerts import run_account_alert_notifier
        _bg_tasks.append(asyncio.create_task(
            run_account_alert_notifier(discord_bot, stop_event),
        ))
        from bot.discord.resource_alerts import run_resource_check
        _bg_tasks.append(asyncio.create_task(
            run_resource_check(discord_bot),
        ))
    if config.LOG_TRIAGE_ENABLED and discord_bot:
        from bot.discord.log_triage import run_triage_service
        _bg_tasks.append(asyncio.create_task(
            run_triage_service(discord_bot, stop_event),
        ))
    scheduler.start()

    boot_recovery.log_orphan_git(store, runner)

    log.info(
        "Bot ready — PC: %s, CLI: %s, platforms: %s",
        config.PC_NAME, cli_version,
        ", ".join(name for name, _ in platform_tasks),
    )

    # Announce the restart and resume the thread that asked for it, if a
    # reboot note was left behind.
    await boot_recovery.announce_reboot(notifier, discord_bot)

    # Replay messages that were queued during reboot drain
    drain_queue = runner.read_drain_queue()
    # Only callback entries substitute for chain work — text messages in a
    # thread don't resume the autopilot chain, so exclude them from dedup
    drain_callback_channel_ids: set[str] = {
        e.get("channel_id") for e in drain_queue
        if e.get("type") == "callback" and e.get("channel_id")
    } if drain_queue else set()
    if drain_queue and discord_bot:
        asyncio.create_task(discord_bot.dispatch_drain_queue(drain_queue))
        log.info("Dispatched %d drain-queued messages for replay", len(drain_queue))

    # Fleet ship: drain post-deploy verify prompts left pending by a
    # self-managed deploy (which reboots this process mid-pipeline) or a
    # crash between deploy and verify. Entries persist in platform_state.
    if discord_bot:
        _fleet_bot = discord_bot

        async def _drain_fleet_verify() -> None:
            if not await _fleet_bot._wait_for_ready("fleet_verify_drain"):
                return
            from bot.discord import fleet
            n = await fleet.drain_pending_verify(_fleet_bot)
            if n:
                log.info("Fleet ship: dispatched %d post-deploy verify prompts", n)
        asyncio.create_task(_drain_fleet_verify())

    # Collect session_ids that will be auto-resumed (chain resume or drain
    # callback) so orphan cleanup can skip those threads
    auto_resuming_sessions: set[str] = set()
    all_chains = store.get_all_autopilot_chains()
    if all_chains:
        auto_resuming_sessions.update(all_chains.keys())

    # Resume autopilot chains that were interrupted by the restart
    if discord_bot:
        asyncio.create_task(
            boot_recovery.resume_interrupted_chains(store, discord_bot, drain_callback_channel_ids)
        )

    # Update thinking messages for orphaned instances (interrupted by restart).
    # Skips instances that will be auto-resumed to avoid confusing
    # "interrupted" → immediate restart sequence.
    await boot_recovery.cleanup_orphan_messages(
        notifier, orphans, auto_resuming_sessions, drain_callback_channel_ids,
        interrupt_reason=oom_reason,
    )

    await boot_recovery.post_oom_notice(discord_bot, orphans, oom_reason)

    # Restore interactive pending-prompt entries (Steer/Queue embeds) from
    # before the reboot.  Instances they reference are dead — we edit the
    # embed to "Lost on restart" so the user knows to resend.
    if discord_bot:
        asyncio.create_task(boot_recovery.restore_pending_prompts(discord_bot))

    # Wait for shutdown
    await stop_event.wait()

    # Graceful shutdown
    log.info("Shutting down...")
    scheduler.stop()

    # Drain active queries before tearing down platforms.
    # After kill_all(), the run_instance coroutines process the killed-process
    # result through the normal finalize + send_result flow (platforms still up).
    if runner.is_busy:
        log.info(
            "Waiting for %d active tasks to finish (30s timeout)...",
            runner.active_task_count,
        )
        drained = await runner.wait_until_idle(timeout=30)
        if not drained:
            log.warning(
                "Drain timed out; killing %d remaining processes",
                runner.active_count,
            )
            await runner.kill_all()
            # Give run_instance coroutines time to finalize + deliver results
            drained = await runner.wait_until_idle(timeout=10)
            if not drained:
                runner.force_clear_tasks()

    store.save()

    if discord_bot:
        try:
            await discord_bot.close()
        except Exception:
            log.exception("Error shutting down Discord")

    _release_pid_lock()
    log.info("Shutdown complete")


async def _do_cooldown_retry(store, runner, inst, discord_bot, retrying_set):
    """Auto-retry an instance after usage-limit cooldown expires.

    Holds the per-channel lock for the entire retry + chain-resume so a
    concurrent text/button on the same thread can't double-spawn against the
    same session (root cause of the t-3501 duplicate-build incident).
    """
    from bot.engine.commands import _get_channel_lock

    try:
        channel_id = inst.cooldown_channel_id
        if not channel_id or not discord_bot:
            log.warning("No channel/bot for cooldown retry %s — clearing", inst.id)
            inst.cooldown_retry_at = None
            inst.cooldown_channel_id = None
            store.update_instance(inst)
            return

        lock = _get_channel_lock(str(channel_id))
        async with lock:
            await _do_cooldown_retry_locked(
                store, runner, inst, discord_bot, channel_id,
            )

    except Exception:
        log.exception("Cooldown retry failed for %s", inst.id)
        # Clear cooldown to prevent infinite retry attempts
        inst.cooldown_retry_at = None
        inst.cooldown_channel_id = None
        store.update_instance(inst)
    finally:
        retrying_set.discard(inst.id)


async def _do_cooldown_retry_locked(store, runner, inst, discord_bot, channel_id):
    """Inner cooldown retry body, run while holding the channel lock."""
    from bot.engine import lifecycle
    from bot.platform.formatting import running_button_specs

    # Build RequestContext from discord bot (like scheduler does)
    lookup = discord_bot._forums.thread_to_project(channel_id)
    t_info = lookup[1] if lookup else None
    repo_name = lookup[0].repo_name if lookup else inst.repo_name
    ctx = discord_bot._ctx(channel_id, thread_info=t_info, repo_name=repo_name)
    # Wire the session callbacks so the session this retry produces gets
    # registered onto the thread.  Without this the retry ran through
    # lifecycle.run_instance, which never binds — so even a *successful* retry
    # left the thread sessionless and unable to be resumed by a self-wake.
    # Same omission that was fixed for post-reboot replays (_replay_to_thread).
    if t_info is not None:
        discord_bot._forums.attach_session_callbacks(ctx, t_info, channel_id)
    # Mark this as an unattended turn: nobody typed it, so lifecycle's
    # end-of-turn protocol applies (finish with [TURN_COMPLETE] or schedule a
    # wake, else get auto-nudged rather than silently stranding the thread).
    ctx.source = "cooldown"

    # Wake the thread (cancel sleep, set active tag)
    discord_bot._cancel_sleep(channel_id)
    try:
        ch = discord_bot.get_channel(int(channel_id))
        if ch:
            asyncio.create_task(discord_bot._clear_thread_sleeping(ch))
            asyncio.create_task(discord_bot._set_thread_active_tag(ch, True))
    except Exception:
        pass

    # Create new instance from original
    new_inst = store.clone_instance_for_rerun(
        inst,
        origin_platform=inst.origin_platform,
        effort=inst.effort,
        model=inst.model,
    )
    new_inst.cooldown_retries = inst.cooldown_retries  # Carry count forward
    # If the prior turn was running in a worktree (build / build-derived),
    # the cooldown can interrupt mid-edit. The resumed agent often thinks
    # the task is already done — leaving uncommitted edits stranded. Nudge
    # it to check git state first so the autopilot's no-commits guard never
    # has to fire.
    if inst.worktree_path:
        new_inst.prompt = (
            (inst.prompt or "") +
            "\n\n[Cooldown retry note] Your prior turn was interrupted "
            "by a usage-limit cooldown. Before doing anything else, run "
            "`git status` in the worktree. If there are uncommitted edits "
            "from the previous turn, finish the work and commit (with "
            "`git log -1 --oneline` to confirm) before saying you're done."
        )
    else:
        # Non-worktree retry: this turn runs unattended, so append the
        # end-of-turn protocol (finish with [TURN_COMPLETE] or schedule a
        # wake). Worktree retries are excluded — they can't self-wake and get
        # the commit-focused note above instead; they're also excluded from the
        # nudge path by the instance.branch gate in check_wake_request.
        new_inst.prompt = (inst.prompt or "") + config.UNATTENDED_TURN_PROTOCOL
    store.update_instance(new_inst)

    # Clear cooldown on the original instance
    inst.cooldown_retry_at = None
    inst.cooldown_channel_id = None
    store.update_instance(inst)

    escaped = ctx.messenger.escape(new_inst.display_id())
    handle = await ctx.messenger.send_thinking(
        channel_id, f"⏳ {escaped} auto-retrying after cooldown...",
        buttons=running_button_specs(new_inst.id),
    )
    if handle.get("message_id"):
        new_inst.message_ids.setdefault(ctx.platform, []).append(handle.get("message_id"))
        store.update_instance(new_inst)

    log.info("Cooldown retry: %s → %s in channel %s", inst.id, new_inst.id, channel_id)
    try:
        await lifecycle.run_instance(ctx, new_inst, handle=handle)

        # Backstop: run_instance never binds, so if the interrupted turn was
        # cut off before it produced a session_id at all, this retry's session
        # is the only one the thread will ever see.  finalize_run has already
        # written the authoritative id onto the instance by now.  Fill-only and
        # worktree-excluded — the rule lives in backfill_thread_session, shared
        # with /retry, the Retry button and continue-on-pay-per-use.
        await lifecycle.backfill_thread_session(ctx, new_inst)

        # Resume autopilot chain if this retry was mid-chain
        if (new_inst.status == InstanceStatus.COMPLETED
                and not new_inst.needs_input
                and not new_inst.cooldown_retry_at
                and new_inst.session_id):
            chain = store.get_autopilot_chain(new_inst.session_id)
            if chain and len(chain) > 1:
                last_msgs = new_inst.message_ids.get("discord", [])
                last_msg = last_msgs[-1] if last_msgs else None
                log.info("Cooldown retry resuming autopilot chain: %s → step %s",
                         new_inst.id, chain[1])
                try:
                    await ctx.messenger.send_text(
                        channel_id,
                        "⏳ Autopilot resuming after cooldown...",
                        silent=True,
                    )
                    from bot.engine import workflows
                    await workflows.resume_autopilot_chain(
                        ctx, new_inst.id, last_msg, new_inst.session_id,
                    )
                except Exception:
                    log.exception("Autopilot chain resume failed after cooldown retry %s",
                                  new_inst.id)
    finally:
        await discord_bot._after_turn(ctx, channel_id)


def _discord_is_down(bot) -> bool:
    """True when this Discord client will not come back without a restart.

    Closed, or its start task ended: login failed, or connect() raised past
    its own reconnect loop. In both cases `_run_discord` has already logged
    and returned, and nothing in the process will ever try again. A client
    that is merely reconnecting is neither, and None (not started yet, or
    Discord disabled) is not down either.
    """
    if bot is None:
        return False
    task = getattr(bot, "_start_task", None)
    return bot.is_closed() or (task is not None and task.done())


async def _start_discord(store, runner, notifier, stop_event):
    """Start the Discord platform."""
    from bot.discord.bot import ClaudeBot

    bot = ClaudeBot(
        store=store,
        runner=runner,
        guild_id=config.DISCORD_GUILD_ID,
        lobby_channel_id=config.DISCORD_LOBBY_CHANNEL_ID,
        category_id=config.DISCORD_CATEGORY_ID,
        category_name=config.DISCORD_CATEGORY_NAME,
        discord_user_id=config.DISCORD_USER_ID,
    )

    # Start in background task (discord.py's start() blocks)
    async def _run_discord():
        try:
            await bot.start(config.DISCORD_BOT_TOKEN)
        except Exception:
            log.exception("Discord bot crashed")

    # Kept on the bot, not fire-and-forget: asyncio holds only a weak reference
    # to a task, and _discord_is_down reads this one to tell a client that
    # gave up from one that is reconnecting.
    bot._start_task = asyncio.create_task(_run_discord())

    # Wait for on_ready to finish (auto-provisions category/lobby)
    try:
        await asyncio.wait_for(bot._ready_event.wait(), timeout=30)
    except asyncio.TimeoutError:
        log.warning("Discord bot timed out waiting for ready")

    # Register with notifier (lobby_channel_id may have been set in on_ready)
    if bot._lobby_channel_id:
        notifier.register(bot.messenger, str(bot._lobby_channel_id))

    # Give bot access to notifier for monitor alerts
    bot._notifier = notifier

    # Startup auth sync — pull credentials from Discord if local auth is broken
    try:
        from bot.services.auth_sync import startup_auth_check
        await startup_auth_check(bot)
    except Exception:
        log.exception("Startup auth check failed (non-fatal)")

    return bot
