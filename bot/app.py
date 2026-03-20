"""Bot orchestrator — starts Discord bot with shared state."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot import config
from bot.claude.runner import ClaudeRunner
from bot.claude.types import InstanceStatus
from bot.engine import commands as engine_commands
from bot.platform.base import NotificationService
from bot.platform.formatting import redact_secrets
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


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running (cross-platform)."""
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        # Check if process has actually exited
        STILL_ACTIVE = 259
        exit_code = ctypes.c_ulong()
        alive = bool(
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            and exit_code.value == STILL_ACTIVE
        )
        kernel32.CloseHandle(handle)
        return alive
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _acquire_pid_lock() -> bool:
    """Write PID file, refusing to start if another instance is alive."""
    pid_file = config.DATA_DIR / "bot.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            if old_pid != os.getpid() and _is_process_alive(old_pid):
                log.error("Another bot instance is running (PID %d). Exiting.", old_pid)
                return False
            else:
                log.info("Stale PID file (PID %d no longer running), taking over", old_pid)
        except (ValueError, OSError):
            pass  # Corrupt PID file, overwrite it

    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _release_pid_lock() -> None:
    """Remove PID file on clean shutdown."""
    pid_file = config.DATA_DIR / "bot.pid"
    try:
        if pid_file.exists() and pid_file.read_text().strip() == str(os.getpid()):
            pid_file.unlink()
    except OSError:
        pass


def _detect_update_branch() -> str:
    """Detect the remote default branch for auto-update.

    Priority: AUTO_UPDATE_BRANCH env > git symbolic-ref > fallback 'master'.
    """
    if config.AUTO_UPDATE_BRANCH:
        return config.AUTO_UPDATE_BRANCH
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=str(config._PROJECT_ROOT),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            # "refs/remotes/origin/main" -> "main"
            ref = result.stdout.strip()
            return ref.rsplit("/", 1)[-1]
    except Exception:
        pass
    return "master"


async def auto_update_loop(
    stop_event: asyncio.Event,
    runner: ClaudeRunner,
    notifier: NotificationService,
) -> None:
    """Periodically check for upstream changes, pull, and reboot."""
    branch = _detect_update_branch()
    interval = config.AUTO_UPDATE_INTERVAL_SECS
    log.info("Auto-update started (branch=%s, interval=%ds)", branch, interval)

    failure_notified = False  # dedup: only notify once per ongoing failure
    first_check = True

    while not stop_event.is_set():
        if not first_check:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # normal: interval elapsed
        first_check = False

        try:
            # Hold repo lock for the entire fetch-compare-pull sequence
            # to prevent racing with worktree operations on the bot repo
            repo_lock = runner._get_repo_lock(str(config._PROJECT_ROOT))
            async with repo_lock:
                # 1. Fetch
                fetch = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "fetch", "origin"],
                    cwd=str(config._PROJECT_ROOT),
                    capture_output=True, text=True, timeout=30,
                )
                if fetch.returncode != 0:
                    err = fetch.stderr.strip() or "unknown error"
                    if not failure_notified:
                        log.warning("Auto-update: git fetch failed — %s", err)
                        await notifier.broadcast(
                            f"⚠️ Auto-update: git fetch failed — {err}",
                        )
                        failure_notified = True
                    continue

                # 2. Compare HEAD vs remote
                local_head = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(config._PROJECT_ROOT),
                    capture_output=True, text=True, timeout=10,
                )
                remote_head = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "rev-parse", f"origin/{branch}"],
                    cwd=str(config._PROJECT_ROOT),
                    capture_output=True, text=True, timeout=10,
                )
                if local_head.returncode != 0 or remote_head.returncode != 0:
                    if not failure_notified:
                        log.warning("Auto-update: git rev-parse failed (branch=%s)", branch)
                        await notifier.broadcast(
                            f"⚠️ Auto-update: can't resolve branch `{branch}` — check AUTO_UPDATE_BRANCH",
                        )
                        failure_notified = True
                    continue
                local_sha = local_head.stdout.strip()
                remote_sha = remote_head.stdout.strip()

                if local_sha == remote_sha:
                    if failure_notified:
                        failure_notified = False  # reset on success
                    continue

                # 3. Get commit log for notification
                log_result = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "log", "--oneline", f"{local_sha}..{remote_sha}"],
                    cwd=str(config._PROJECT_ROOT),
                    capture_output=True, text=True, timeout=10,
                )
                commits = log_result.stdout.strip().splitlines()
                n_commits = len(commits)
                latest_msg = commits[0] if commits else "unknown"

                log.info("Auto-update: %d new commit(s) on origin/%s", n_commits, branch)

                # 4. Pull (ff-only)
                pull = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "pull", "--ff-only", "origin", branch],
                    cwd=str(config._PROJECT_ROOT),
                    capture_output=True, text=True, timeout=30,
                )
                if pull.returncode != 0:
                    err = pull.stderr.strip() or "unknown error"
                    if not failure_notified:
                        log.warning("Auto-update: git pull failed — %s", err)
                        await notifier.broadcast(
                            f"⚠️ Auto-update: pull failed — {err}. Manual intervention needed.",
                        )
                        failure_notified = True
                    continue

            # 5. pip install (non-fatal)
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
                    cwd=str(config._PROJECT_ROOT),
                    capture_output=True, text=True, timeout=120,
                )
            except Exception:
                log.warning("Auto-update: pip install failed (non-fatal)", exc_info=True)

            # 6. Request reboot first (just a list append — can't fail)
            failure_notified = False
            runner.request_reboot({
                "message": f"Auto-update: pulled {n_commits} commits",
            })

            # 7. Notify (best-effort — reboot already queued)
            try:
                await notifier.broadcast(
                    f"🔄 Auto-update: pulled {n_commits} commit(s) — `{latest_msg}`\nRebooting...",
                )
            except Exception:
                log.warning("Auto-update: notification failed (reboot still queued)")
            return  # reboot requested, exit loop

        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Auto-update: unexpected error")
            if not failure_notified:
                await notifier.broadcast("⚠️ Auto-update: unexpected error — check logs.")
                failure_notified = True


async def run() -> None:
    """Main async entry point."""
    setup_logging()
    log.info("Starting Claude Bot...")

    if not _acquire_pid_lock():
        return

    start_time = time.time()
    stop_event = asyncio.Event()

    # Initialize shared store
    store = StateStore(
        state_file=config.STATE_FILE,
        results_dir=config.RESULTS_DIR,
        retention_days=config.INSTANCE_RETENTION_DAYS,
    )

    orphans = store.mark_orphans()
    if orphans:
        log.warning("Marked %d orphaned instances as failed", len(orphans))

    archive_count = store.archive_old()
    if archive_count:
        log.info("Archived %d old instances", archive_count)

    # Initialize shared runner
    runner = ClaudeRunner()

    try:
        cli_version = await runner.check_cli()
        log.info("Claude CLI version: %s", cli_version)
    except RuntimeError as e:
        log.error("Claude CLI self-test failed: %s", e)
        cli_version = f"FAILED: {e}"

    # Initialize engine module state
    engine_commands.init(start_time, cli_version, shutdown_fn=lambda: stop_event.set())

    # Capture deploy state baselines for all repos
    from bot.engine.deploy import (
        capture_boot_baselines, make_deploy_config,
        is_deploy_protected, scan_deploy_config,
    )
    capture_boot_baselines(store, str(config._PROJECT_ROOT))

    # Auto-register deploy configs
    for _rname, _rpath in store.list_repos().items():
        _ds = store.get_deploy_state(_rname)
        _existing_cfg = store.get_deploy_config(_rname)
        # Self-managed repos: auto-register with approved=True
        if _ds and _ds.self_managed and not _existing_cfg:
            store.set_deploy_config(_rname, make_deploy_config(
                "self", source="auto", approved=True,
            ))
            log.info("Auto-registered self-managed deploy config for %s", _rname)
        # File-based configs: register unapproved — skip protected repos
        elif not is_deploy_protected(_existing_cfg, _ds) and not _existing_cfg:
            _file_cfg = scan_deploy_config(_rpath)
            if _file_cfg:
                store.set_deploy_config(_rname, make_deploy_config(
                    "command",
                    command=_file_cfg["command"],
                    label=_file_cfg.get("label", "Deploy"),
                    cwd=_file_cfg.get("cwd"),
                    source="file", approved=False,
                ))
                log.info("Auto-registered file-based deploy config for %s (pending approval)", _rname)

    # Emergency signal handler: if the bot is killed (e.g. by a Claude Code instance
    # running taskkill), save context and auto-relaunch so we come back online.
    def _emergency_reboot_handler(signum, frame):
        import json as _json
        import subprocess as _sp
        log.warning("Caught signal %s — emergency reboot", signum)
        # Find any running instance's channel to send confirmation after restart
        reboot_data: dict = {}
        for inst in store.list_instances()[:20]:
            if inst.status == InstanceStatus.RUNNING and inst.message_ids:
                for platform, msg_ids in inst.message_ids.items():
                    if msg_ids:
                        # Use the thread/channel of the first running instance
                        reboot_data = {"channel_id": msg_ids[0], "platform": platform}
                        # For Discord, the channel is the thread the msg was sent to
                        # We need the actual channel_id, not msg_id — check forum threads
                        break
                if reboot_data:
                    break
        # Always save something so new process knows it was a reboot
        if not reboot_data:
            reboot_data = {}
        try:
            config.REBOOT_MSG_FILE.write_text(
                _json.dumps(reboot_data), encoding="utf-8",
            )
        except Exception:
            pass
        # Spawn relaunch and exit
        launcher = config._PROJECT_ROOT / "scripts" / "relaunch.py"
        try:
            _sp.Popen(
                [sys.executable, str(launcher), str(config._PROJECT_ROOT)],
                creationflags=_sp.DETACHED_PROCESS | _sp.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
        except Exception:
            pass
        store.save()
        _release_pid_lock()
        os._exit(1)

    # Register for SIGTERM and SIGBREAK (Windows) / SIGTERM (Unix)
    signal.signal(signal.SIGTERM, _emergency_reboot_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _emergency_reboot_handler)

    # Notification service
    notifier = NotificationService()

    # Reboot coalescing: register idle callback that executes pending reboots
    # after all tasks finish. Multiple autopilots requesting reboots produce
    # a single reboot instead of racing.
    async def _execute_pending_reboots() -> None:
        """Called by runner when idle + reboots pending. Runs at most once."""
        import json as _json
        reboots = runner.pending_reboots()
        if not reboots:
            runner._reboot_executing = False
            return

        try:
            last = reboots[-1]
            reason = last.get("message", "reboot requested")

            # Notify all distinct channels that requested reboots
            channels_seen: set[tuple[str, str]] = set()
            for r in reboots:
                ch = r.get("channel_id")
                plat = r.get("platform")
                if ch and plat and (ch, plat) not in channels_seen:
                    channels_seen.add((ch, plat))
                    if plat in notifier._messengers:
                        messenger, _ = notifier._messengers[plat]
                        try:
                            await messenger.send_text(ch, f"🔄 Rebooting: {reason}")
                        except Exception:
                            log.warning("Failed to notify %s:%s about reboot", plat, ch)

            # Merge resume prompts from all requesters
            resume_parts = [r["resume_prompt"] for r in reboots if r.get("resume_prompt")]
            merged_prompt = "\n---\n".join(resume_parts) if resume_parts else None

            reboot_data = {
                "channel_id": last.get("channel_id", ""),
                "platform": last.get("platform", ""),
            }
            if merged_prompt:
                reboot_data["resume_prompt"] = merged_prompt

            try:
                config.REBOOT_MSG_FILE.write_text(
                    _json.dumps(reboot_data), encoding="utf-8",
                )
            except Exception:
                log.warning("Failed to write reboot message file", exc_info=True)

            log.info("Executing coalesced reboot (%d requests merged)", len(reboots))

            # Spawn relaunch script and trigger shutdown
            import subprocess as _sp
            launcher = config._PROJECT_ROOT / "scripts" / "relaunch.py"
            _sp.Popen(
                [sys.executable, str(launcher), str(config._PROJECT_ROOT)],
                creationflags=_sp.DETACHED_PROCESS | _sp.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
            runner.clear_reboots()
            stop_event.set()
        except Exception:
            log.exception("Reboot executor failed — resetting for retry")
            runner.clear_reboots()
            runner._reboot_executing = False

    runner.set_on_idle_reboot(_execute_pending_reboots)

    # Schedule result callback
    async def on_schedule_result(instance, result, changed):
        if instance.status == InstanceStatus.FAILED:
            escaped = redact_secrets(instance.error or 'Unknown error')
            await notifier.broadcast(
                f"⚠️ **Scheduled task failed**\n{instance.display_id()}: {escaped}",
            )
        elif changed:
            escaped = redact_secrets(instance.summary or 'No summary')
            await notifier.broadcast(
                f"{instance.display_id()} (scheduled)\n{escaped}",
                silent=True,
            )

    scheduler = Scheduler(store, runner, on_result=on_schedule_result)
    scheduler.recalculate_next_runs()

    # Background tasks
    async def auto_save_loop():
        while True:
            await asyncio.sleep(60)
            try:
                store.save_if_dirty()
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
    discord_bot = None
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
        while True:
            await asyncio.sleep(60)
            try:
                now = dt.now(tz_mod.utc)
                for inst in store.list_instances(all_=True):
                    if not inst.cooldown_retry_at or not inst.cooldown_channel_id:
                        continue
                    if inst.id in _cooldown_retrying:
                        continue
                    try:
                        retry_at = dt.fromisoformat(inst.cooldown_retry_at)
                    except (ValueError, TypeError):
                        continue
                    if now >= retry_at:
                        _cooldown_retrying.add(inst.id)
                        asyncio.create_task(
                            _do_cooldown_retry(store, runner, inst, discord_bot, _cooldown_retrying)
                        )
            except Exception:
                log.exception("Cooldown loop error")

    # Start background tasks (store refs to prevent GC)
    _bg_tasks = [
        asyncio.create_task(auto_save_loop()),
        asyncio.create_task(cooldown_loop()),
    ]
    if config.AUTO_UPDATE:
        _bg_tasks.append(asyncio.create_task(
            auto_update_loop(stop_event, runner, notifier),
        ))
    scheduler.start()

    # Scan for orphaned branches and worktrees across all repos
    active_branches = {inst.branch for inst in store.list_instances(all_=True) if inst.branch}
    active_worktrees = {inst.worktree_path for inst in store.list_instances(all_=True) if inst.worktree_path}
    for repo_name, repo_path in store.list_repos().items():
        if not Path(repo_path).is_dir():
            continue
        orphan_branches = runner.scan_orphan_branches(repo_path, active_branches)
        if orphan_branches:
            log.warning(
                "Repo '%s' has %d orphaned branches: %s",
                repo_name, len(orphan_branches),
                ", ".join(orphan_branches[:5]) + ("..." if len(orphan_branches) > 5 else ""),
            )
        orphan_wts = runner.scan_orphan_worktrees(repo_path, active_worktrees)
        if orphan_wts:
            log.warning(
                "Repo '%s' has %d orphaned worktrees: %s",
                repo_name, len(orphan_wts),
                ", ".join(orphan_wts[:5]) + ("..." if len(orphan_wts) > 5 else ""),
            )

    log.info(
        "Bot ready — PC: %s, CLI: %s, platforms: %s",
        config.PC_NAME, cli_version,
        ", ".join(name for name, _ in platform_tasks),
    )

    # Send reboot announcement + auto-resume if a reboot_message.json was saved
    reboot_data = await _send_reboot_announcement(notifier)

    if reboot_data:
        _resume_channel = reboot_data.get("channel_id")
        _resume_platform = reboot_data.get("platform")
        _resume_prompt = reboot_data.get("resume_prompt")

        if _resume_channel and _resume_platform == "discord" and discord_bot:
            # dispatch_resume waits for Discord ready, sends announcement, and runs query
            announce = f"✅ {config.PC_NAME} back online."
            asyncio.create_task(discord_bot.dispatch_resume(
                _resume_channel, _resume_prompt or "", announce=announce,
            ))
            log.info("Dispatched post-reboot resume to discord channel %s", _resume_channel)

    # Update thinking messages for orphaned instances (interrupted by restart)
    await _cleanup_orphan_messages(notifier, orphans)

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
            await runner.wait_until_idle(timeout=10)

    store.save()

    if discord_bot:
        try:
            await discord_bot.close()
        except Exception:
            log.exception("Error shutting down Discord")

    _release_pid_lock()
    log.info("Shutdown complete")


async def _send_reboot_announcement(notifier: NotificationService) -> dict | None:
    """If a reboot_message.json was left by a previous process, read and return it.

    The announcement is deferred to dispatch_resume (which waits for the bot
    to be ready and handles file cleanup).
    """
    import json
    try:
        data = json.loads(config.REBOOT_MSG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    log.info(
        "Read reboot message for %s channel %s (deferred)",
        data.get("platform"), data.get("channel_id"),
    )
    return data


async def _cleanup_orphan_messages(notifier: NotificationService, orphans: list) -> None:
    """Update thinking messages for instances that were interrupted by a restart.

    Finds the last message sent by each orphaned instance and edits it to show
    the interrupted status, so users see a clean resolution instead of a stale
    'processing...' indicator.
    """
    if not orphans:
        return
    from bot.platform.base import MessageHandle
    for inst in orphans:
        for platform, msg_ids in inst.message_ids.items():
            if not msg_ids or platform not in notifier._messengers:
                continue
            messenger, _ = notifier._messengers[platform]
            # The last message_id is typically the thinking/progress message
            last_msg_id = msg_ids[-1]
            # Find the channel — use the first msg_id's channel context
            # For Discord, message_ids are sent to the thread/channel the instance ran in
            # We need to figure out the channel_id. The thinking message handle stores it,
            # but that's lost on restart. We can try editing via the message directly.
            handle = MessageHandle(
                platform=platform,
                _data={"message_id": last_msg_id},
            )
            # Try to update — for Discord we need channel_id in the handle.
            # Since we don't have it, send a follow-up to the channel instead.
            # Find channel from instance's message context
            try:
                # For each platform, try to send a status update to the channel
                # where the instance was running. We check all msg_ids.
                # Discord adapter needs channel_id — we'll extract it from
                # the instance's origin platform data if available.
                channel_id = None
                if inst.session_id and hasattr(messenger, 'find_channel_for_session'):
                    channel_id = messenger.find_channel_for_session(inst.session_id)
                if channel_id:
                    handle._data["channel_id"] = channel_id
                    try:
                        await messenger.edit_thinking(
                            handle,
                            f"⚠️ {inst.display_id()} interrupted by bot restart",
                        )
                        log.info("Updated orphan thinking msg for %s in %s:%s",
                                 inst.id, platform, channel_id)
                    except Exception:
                        log.debug("Could not edit orphan thinking msg for %s", inst.id)
            except Exception:
                log.debug("Orphan message cleanup failed for %s", inst.id, exc_info=True)


async def _do_cooldown_retry(store, runner, inst, discord_bot, retrying_set):
    """Auto-retry an instance after usage-limit cooldown expires."""
    from bot.engine import lifecycle
    from bot.platform.formatting import running_button_specs

    try:
        channel_id = inst.cooldown_channel_id
        if not channel_id or not discord_bot:
            log.warning("No channel/bot for cooldown retry %s — clearing", inst.id)
            inst.cooldown_retry_at = None
            inst.cooldown_channel_id = None
            store.update_instance(inst)
            return

        # Build RequestContext from discord bot (like scheduler does)
        lookup = discord_bot._forums.thread_to_project(channel_id)
        t_info = lookup[1] if lookup else None
        repo_name = lookup[0].repo_name if lookup else inst.repo_name
        ctx = discord_bot._ctx(channel_id, thread_info=t_info, repo_name=repo_name)

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
        new_inst = store.create_instance(
            instance_type=inst.instance_type,
            prompt=inst.prompt,
            mode=inst.mode,
        )
        new_inst.origin = inst.origin
        new_inst.origin_platform = inst.origin_platform
        new_inst.effort = inst.effort
        new_inst.parent_id = inst.id
        new_inst.repo_name = inst.repo_name
        new_inst.repo_path = inst.repo_path
        new_inst.cooldown_retries = inst.cooldown_retries  # Carry count forward
        if inst.session_id:
            new_inst.session_id = inst.session_id
        if inst.branch:
            new_inst.branch = inst.branch
            new_inst.original_branch = inst.original_branch
            new_inst.worktree_path = inst.worktree_path
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
        finally:
            # Post-run cleanup (matches normal query flow in interactions.py)
            discord_bot._forums.persist_ctx_settings(ctx)
            discord_bot._schedule_sleep(channel_id)
            asyncio.create_task(discord_bot._try_apply_tags_after_run(channel_id))
            asyncio.create_task(discord_bot._refresh_dashboard())

    except Exception:
        log.exception("Cooldown retry failed for %s", inst.id)
        # Clear cooldown to prevent infinite retry attempts
        inst.cooldown_retry_at = None
        inst.cooldown_channel_id = None
        store.update_instance(inst)
    finally:
        retrying_set.discard(inst.id)


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

    asyncio.create_task(_run_discord())

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

    return bot
