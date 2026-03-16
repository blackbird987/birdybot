"""Multi-platform orchestrator — starts Telegram and/or Discord bots with shared state."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from bot import config
from bot.claude.runner import ClaudeRunner
from bot.claude.types import InstanceStatus
from bot.engine import commands as engine_commands
from bot.platform.base import NotificationService
from bot.platform.formatting import format_digest_md, redact_secrets
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
    logging.getLogger("telegram").setLevel(logging.WARNING)
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
        runner.clear_reboots()
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
            stop_event.set()
        except Exception:
            log.exception("Reboot executor failed — resetting for retry")
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

    async def daily_digest_loop():
        import datetime as dt
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            target = now.replace(
                hour=config.DIGEST_HOUR, minute=0, second=0, microsecond=0
            )
            if target <= now:
                target += dt.timedelta(days=1)
            wait_secs = (target - now).total_seconds()
            await asyncio.sleep(wait_secs)
            try:
                repo_name, _ = store.get_active_repo()
                text = format_digest_md(
                    instance_count=store.instance_count_today(),
                    daily_cost=store.get_daily_cost(),
                    failures=store.failure_count_today(),
                    repo_name=repo_name,
                    mode=store.mode,
                )
                await notifier.broadcast(text, silent=True)
            except Exception:
                log.exception("Failed to send daily digest")

    # Signal handling
    def signal_handler(sig, frame):
        log.info("Received signal %s, shutting down...", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start platform tasks
    platform_tasks = []

    # --- Telegram ---
    telegram_app = None
    if config.TELEGRAM_ENABLED:
        try:
            telegram_app = await _start_telegram(store, runner, notifier, stop_event, cli_version)
            platform_tasks.append(("telegram", telegram_app))
            log.info("Telegram platform started")
        except Exception:
            log.exception("Failed to start Telegram platform")

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

    # Start background tasks (store refs to prevent GC)
    _bg_tasks = [
        asyncio.create_task(auto_save_loop()),
        asyncio.create_task(daily_digest_loop()),
    ]
    scheduler.start()

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

        elif _resume_prompt and _resume_channel and _resume_platform in notifier._messengers:
            # Generic fallback (Telegram, etc): build context from notifier
            from bot.engine import commands as _cmds
            from bot.platform.base import RequestContext
            _messenger, _ = notifier._messengers[_resume_platform]
            _ctx = RequestContext(
                messenger=_messenger,
                channel_id=_resume_channel,
                platform=_resume_platform,
                store=store,
                runner=runner,
            )
            asyncio.create_task(_cmds.on_text(_ctx, _resume_prompt))
            log.info("Dispatched post-reboot resume to %s channel %s", _resume_platform, _resume_channel)

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

    if telegram_app:
        try:
            await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except Exception:
            log.exception("Error shutting down Telegram")

    if discord_bot:
        try:
            await discord_bot.close()
        except Exception:
            log.exception("Error shutting down Discord")

    _release_pid_lock()
    log.info("Shutdown complete")


async def _send_reboot_announcement(notifier: NotificationService) -> dict | None:
    """If a reboot_message.json was left by a previous process, read and return it.

    For Discord, the announcement is deferred to dispatch_resume (which waits
    for the bot to be ready). For other platforms, send immediately.
    """
    import json
    try:
        data = json.loads(config.REBOOT_MSG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    try:
        platform = data.get("platform")
        channel_id = data.get("channel_id")

        # Discord: defer to dispatch_resume (which deletes the file after success)
        if platform == "discord":
            log.info("Read reboot message for discord channel %s (deferred)", channel_id)
            return data

        # Non-Discord: consume file and send immediately
        config.REBOOT_MSG_FILE.unlink(missing_ok=True)
        text = f"✅ {config.PC_NAME} back online."
        if channel_id and platform and platform in notifier._messengers:
            messenger, _ = notifier._messengers[platform]
            await messenger.send_text(channel_id, text)
            log.info("Sent reboot confirmation to %s channel %s", platform, channel_id)
        else:
            await notifier.broadcast(text)
            log.info("Broadcast reboot announcement (no specific channel)")

        return data
    except Exception:
        log.warning("Failed to send reboot announcement", exc_info=True)
        return None


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


async def _start_telegram(store, runner, notifier, stop_event, cli_version):
    """Start the Telegram platform."""
    from telegram.error import Conflict
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        filters,
    )

    from bot.telegram.adapter import TelegramMessenger
    from bot.telegram.bridge import TelegramBridge

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Create messenger and bridge
    messenger = TelegramMessenger(app.bot, config.TELEGRAM_USER_ID)
    bridge = TelegramBridge(messenger, store, runner)

    # Register with notifier
    notifier.register(messenger, str(config.TELEGRAM_USER_ID))

    # Register command handlers
    app.add_handler(CommandHandler("start", bridge.on_help))
    app.add_handler(CommandHandler("help", bridge.on_help))
    app.add_handler(CommandHandler("new", bridge.on_new))
    app.add_handler(CommandHandler("bg", bridge.on_bg))
    app.add_handler(CommandHandler("release", bridge.on_release))
    app.add_handler(CommandHandler("list", bridge.on_list))
    app.add_handler(CommandHandler("kill", bridge.on_kill))
    app.add_handler(CommandHandler("retry", bridge.on_retry))
    app.add_handler(CommandHandler("log", bridge.on_log))
    app.add_handler(CommandHandler("diff", bridge.on_diff))
    app.add_handler(CommandHandler("merge", bridge.on_merge))
    app.add_handler(CommandHandler("discard", bridge.on_discard))
    app.add_handler(CommandHandler("cost", bridge.on_cost))
    app.add_handler(CommandHandler("status", bridge.on_status))
    app.add_handler(CommandHandler("logs", bridge.on_logs))
    app.add_handler(CommandHandler("mode", bridge.on_mode))
    app.add_handler(CommandHandler("context", bridge.on_context))
    app.add_handler(CommandHandler("alias", bridge.on_alias))
    app.add_handler(CommandHandler("schedule", bridge.on_schedule))
    app.add_handler(CommandHandler("repo", bridge.on_repo))
    app.add_handler(CommandHandler("budget", bridge.on_budget))
    app.add_handler(CommandHandler("clear", bridge.on_clear))
    app.add_handler(CommandHandler("verbose", bridge.on_verbose))
    app.add_handler(CommandHandler("effort", bridge.on_effort))
    app.add_handler(CommandHandler("session", bridge.on_session))
    app.add_handler(CommandHandler("shutdown", bridge.on_shutdown))
    app.add_handler(CommandHandler("reboot", bridge.on_reboot))

    # Callback query handler
    app.add_handler(CallbackQueryHandler(bridge.on_callback_query))

    # Photo/document handlers
    app.add_handler(MessageHandler(filters.PHOTO, bridge.on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, bridge.on_document))

    # Unknown command handler
    app.add_handler(MessageHandler(filters.COMMAND, bridge.on_unknown_command))

    # Text handler (last)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bridge.on_text))

    # Error handler
    async def on_error(update, context):
        if isinstance(context.error, Conflict):
            log.warning("Another bot instance is polling — %s shutting down", config.PC_NAME)
            try:
                await app.bot.send_message(
                    chat_id=config.TELEGRAM_USER_ID,
                    text=f"Bot stopped on {config.PC_NAME}: another instance took over.",
                    disable_notification=True,
                )
            except Exception:
                pass
            stop_event.set()
            return
        log.exception("Unhandled Telegram error", exc_info=context.error)

    app.add_error_handler(on_error)

    # Start polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    return app


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
