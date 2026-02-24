"""Bootstrap, wiring, signal handlers, self-test, daily digest."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot import config
from bot.claude.runner import ClaudeRunner
from bot.scheduler import Scheduler
from bot.store.state import StateStore
from bot.telegram.callbacks import CallbackHandler
from bot.telegram.formatter import escape_md, format_digest
from bot.telegram.handlers import Handlers

log = logging.getLogger(__name__)


def setup_logging() -> None:
    """Configure rotating file handler + console."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler
    fh = RotatingFileHandler(
        str(config.LOG_FILE),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


async def run() -> None:
    """Main async entry point."""
    setup_logging()
    log.info("Starting Claude Telegram Bot...")

    start_time = time.time()

    # Initialize store
    store = StateStore(
        state_file=config.STATE_FILE,
        results_dir=config.RESULTS_DIR,
        retention_days=config.INSTANCE_RETENTION_DAYS,
    )

    # Startup recovery
    orphan_count = store.mark_orphans()
    if orphan_count:
        log.warning("Marked %d orphaned instances as failed", orphan_count)

    archive_count = store.archive_old()
    if archive_count:
        log.info("Archived %d old instances", archive_count)

    # Initialize runner
    runner = ClaudeRunner()

    # Self-test Claude CLI
    try:
        cli_version = await runner.check_cli()
        log.info("Claude CLI version: %s", cli_version)
    except RuntimeError as e:
        log.error("Claude CLI self-test failed: %s", e)
        cli_version = f"FAILED: {e}"

    # Initialize handlers
    handlers = Handlers(store, runner, cli_version, start_time)
    cb_handler = CallbackHandler(store, runner)
    cb_handler.set_handlers_ref(handlers)

    # Schedule result callback
    async def on_schedule_result(instance, result, changed):
        """Send notification for scheduled task results."""
        if instance.status.value == "failed":
            # Always notify loudly on failure
            formatted = format_digest(
                store.instance_count_today(),
                store.get_daily_cost(),
                store.failure_count_today(),
                store.get_active_repo()[0],
                store.mode,
            )
            try:
                await app.bot.send_message(
                    chat_id=config.TELEGRAM_USER_ID,
                    text=f"⚠️ *Scheduled task failed*\n{escape_md(instance.display_id())}: "
                         f"{escape_md(instance.error or 'Unknown error')}",
                    parse_mode="MarkdownV2",
                )
            except Exception:
                await app.bot.send_message(
                    chat_id=config.TELEGRAM_USER_ID,
                    text=f"Scheduled task failed: {instance.display_id()}: {instance.error}",
                )
        elif changed:
            # Notify silently if result changed
            try:
                await app.bot.send_message(
                    chat_id=config.TELEGRAM_USER_ID,
                    text=f"📋 {escape_md(instance.display_id())} \\(scheduled\\)\n"
                         f"{escape_md(instance.summary or 'No summary')}",
                    parse_mode="MarkdownV2",
                    disable_notification=True,
                )
            except Exception:
                await app.bot.send_message(
                    chat_id=config.TELEGRAM_USER_ID,
                    text=f"Scheduled: {instance.display_id()}: {instance.summary}",
                    disable_notification=True,
                )

    # Initialize scheduler
    scheduler = Scheduler(store, runner, on_result=on_schedule_result)
    scheduler.recalculate_next_runs()

    # Build Telegram application
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Register command handlers
    app.add_handler(CommandHandler("start", handlers.on_help))
    app.add_handler(CommandHandler("help", handlers.on_help))
    app.add_handler(CommandHandler("bg", handlers.on_bg))
    app.add_handler(CommandHandler("list", handlers.on_list))
    app.add_handler(CommandHandler("kill", handlers.on_kill))
    app.add_handler(CommandHandler("retry", handlers.on_retry))
    app.add_handler(CommandHandler("log", handlers.on_log))
    app.add_handler(CommandHandler("diff", handlers.on_diff))
    app.add_handler(CommandHandler("merge", handlers.on_merge))
    app.add_handler(CommandHandler("discard", handlers.on_discard))
    app.add_handler(CommandHandler("cost", handlers.on_cost))
    app.add_handler(CommandHandler("status", handlers.on_status))
    app.add_handler(CommandHandler("logs", handlers.on_logs))
    app.add_handler(CommandHandler("mode", handlers.on_mode))
    app.add_handler(CommandHandler("context", handlers.on_context))
    app.add_handler(CommandHandler("alias", handlers.on_alias))
    app.add_handler(CommandHandler("schedule", handlers.on_schedule))
    app.add_handler(CommandHandler("repo", handlers.on_repo))
    app.add_handler(CommandHandler("budget", handlers.on_budget))
    app.add_handler(CommandHandler("clear", handlers.on_clear))

    # Callback query handler (inline buttons)
    app.add_handler(CallbackQueryHandler(cb_handler.handle))

    # Photo handler
    app.add_handler(MessageHandler(filters.PHOTO, handlers.on_photo))

    # Text handler (must be last — catches everything)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handlers.on_text
    ))

    # Auto-save task
    async def auto_save_loop():
        while True:
            await asyncio.sleep(60)
            try:
                store.save()
            except Exception:
                log.exception("Auto-save failed")

    # Daily digest task
    async def daily_digest_loop():
        import datetime as dt
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            # Calculate seconds until next digest hour
            target = now.replace(
                hour=config.DIGEST_HOUR, minute=0, second=0, microsecond=0
            )
            if target <= now:
                target += dt.timedelta(days=1)
            wait_secs = (target - now).total_seconds()
            await asyncio.sleep(wait_secs)

            # Send digest
            try:
                repo_name, _ = store.get_active_repo()
                text = format_digest(
                    instance_count=store.instance_count_today(),
                    daily_cost=store.get_daily_cost(),
                    failures=store.failure_count_today(),
                    repo_name=repo_name,
                    mode=store.mode,
                )
                await app.bot.send_message(
                    chat_id=config.TELEGRAM_USER_ID,
                    text=text,
                    parse_mode="MarkdownV2",
                    disable_notification=True,
                )
            except Exception:
                log.exception("Failed to send daily digest")

    # Start background tasks
    async def post_init(application: Application) -> None:
        asyncio.create_task(auto_save_loop())
        asyncio.create_task(daily_digest_loop())
        scheduler.start()

        # Send startup notification
        cli_status = f"✅ {cli_version}" if "FAILED" not in cli_version else f"❌ {cli_version}"
        repo_name, repo_path = store.get_active_repo()
        repo_info = f"\nRepo: {repo_name} ({repo_path})" if repo_name else "\nNo repo set"
        try:
            await application.bot.send_message(
                chat_id=config.TELEGRAM_USER_ID,
                text=(
                    f"🤖 *Bot started*\n"
                    f"CLI: {escape_md(cli_status)}\n"
                    f"Mode: `{store.mode}`"
                    f"{escape_md(repo_info)}\n"
                    f"Schedules: {len(store.list_schedules())}"
                ),
                parse_mode="MarkdownV2",
            )
        except Exception:
            log.exception("Failed to send startup notification")

    async def post_shutdown(application: Application) -> None:
        scheduler.stop()
        store.save()
        log.info("Shutdown complete")

    app.post_init = post_init
    app.post_shutdown = post_shutdown

    # Run polling
    log.info("Starting Telegram polling...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Wait for shutdown signal
    stop_event = asyncio.Event()

    def signal_handler(sig, frame):
        log.info("Received signal %s, shutting down...", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    await stop_event.wait()

    # Graceful shutdown
    log.info("Shutting down...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
