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
_NOWND: dict = config.NOWND


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
            capture_output=True, text=True, timeout=10, **_NOWND,
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
                    ["git", "fetch", "origin", "--tags", "--force"],
                    cwd=str(config._PROJECT_ROOT),
                    capture_output=True, text=True, timeout=30, **_NOWND,
                )
                if fetch.returncode != 0:
                    err = fetch.stderr.strip() or "unknown error"
                    if not failure_notified:
                        log.warning("Auto-update: git fetch failed — %s", err)
                        await notifier.broadcast(
                            f"⚠️ Auto-update: git fetch failed — {err}",
                            ttl=10,
                        )
                        failure_notified = True
                    continue

                # 2. Compare HEAD vs remote
                local_head = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(config._PROJECT_ROOT),
                    capture_output=True, text=True, timeout=10, **_NOWND,
                )
                remote_head = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "rev-parse", f"origin/{branch}"],
                    cwd=str(config._PROJECT_ROOT),
                    capture_output=True, text=True, timeout=10, **_NOWND,
                )
                if local_head.returncode != 0 or remote_head.returncode != 0:
                    if not failure_notified:
                        log.warning("Auto-update: git rev-parse failed (branch=%s)", branch)
                        await notifier.broadcast(
                            f"⚠️ Auto-update: can't resolve branch `{branch}` — check AUTO_UPDATE_BRANCH",
                            ttl=10,
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
                    capture_output=True, text=True, timeout=10, **_NOWND,
                )
                if log_result.returncode == 0 and log_result.stdout.strip():
                    commits = log_result.stdout.strip().splitlines()
                    n_commits = len(commits)
                    # Strip the short SHA prefix from the first commit message
                    latest_msg = commits[0].split(" ", 1)[1] if " " in commits[0] else commits[0]
                else:
                    # No new commits on remote — local is ahead or diverged
                    if not failure_notified:
                        log.warning(
                            "Auto-update: HEAD differs from origin/%s but no new "
                            "remote commits — local may be ahead/diverged", branch,
                        )
                        await notifier.broadcast(
                            f"⚠️ Auto-update: local HEAD differs from `origin/{branch}` "
                            "but remote has no new commits. Manual sync may be needed.",
                            ttl=10,
                        )
                        failure_notified = True
                    continue

                log.info("Auto-update: %d new commit(s) on origin/%s",
                         n_commits, branch)

                # 4. Pull (ff-only)
                pull = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "pull", "--ff-only", "origin", branch],
                    cwd=str(config._PROJECT_ROOT),
                    capture_output=True, text=True, timeout=30, **_NOWND,
                )
                if pull.returncode != 0:
                    err = pull.stderr.strip() or "unknown error"
                    if not failure_notified:
                        log.warning("Auto-update: git pull failed — %s", err)
                        await notifier.broadcast(
                            f"⚠️ Auto-update: pull failed — {err}. Manual intervention needed.",
                            ttl=10,
                        )
                        failure_notified = True
                    continue

                # 4b. Verify HEAD actually moved (defensive — Fix 1 should
                # catch the no-op case, but guard against edge cases)
                post_pull = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(config._PROJECT_ROOT),
                    capture_output=True, text=True, timeout=10, **_NOWND,
                )
                if post_pull.returncode == 0 and post_pull.stdout.strip() == local_sha:
                    log.error(
                        "Auto-update: pull reported success but HEAD unchanged "
                        "— skipping reboot (local likely diverged)"
                    )
                    continue

            # 5. pip install (non-fatal)
            try:
                await asyncio.to_thread(
                    subprocess.run,
                    [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
                    cwd=str(config._PROJECT_ROOT),
                    capture_output=True, text=True, timeout=120, **_NOWND,
                )
            except Exception:
                log.warning("Auto-update: pip install failed (non-fatal)", exc_info=True)

            # 6. Build human-friendly strings (reused in reboot request + broadcast)
            failure_notified = False
            count_str = f"pulled {n_commits} commit(s)"
            detail = f"`{latest_msg}`"

            runner.request_reboot({
                "message": f"Auto-update: {count_str}",
            })

            # 7. Notify (best-effort — reboot already queued)
            try:
                await notifier.broadcast(
                    f"🔄 Auto-update: {count_str} — {detail}\nRebooting...",
                    ttl=10,
                )
            except Exception:
                log.warning("Auto-update: notification failed (reboot still queued)")
            return  # reboot requested, exit loop

        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Auto-update: unexpected error")
            if not failure_notified:
                await notifier.broadcast("⚠️ Auto-update: unexpected error — check logs.", ttl=10)
                failure_notified = True


def _migrate_deferred_to_todo(store: StateStore) -> None:
    """One-time migration: data/deferred/*.md → repo TODO.md files.

    Reads old deferred files directly, deduplicates items, writes them
    into each repo's TODO.md, then deletes the data/deferred/ directory.
    """
    import re
    import shutil

    deferred_dir = config.DATA_DIR / "deferred"
    if not deferred_dir.exists():
        return

    def _slug(name: str) -> str:
        """Inline slug logic (config.safe_repo_slug is removed)."""
        s = re.sub(r'[^\w\-.]', '-', name.strip()).strip('.-')
        return s or 'unknown'

    repos = store.list_repos()
    # Build slug → repo_name lookup
    slug_to_repo: dict[str, str] = {}
    for name in repos:
        slug_to_repo[_slug(name)] = name

    migrated_total = 0
    for fpath in deferred_dir.glob("*.md"):
        slug = fpath.stem
        repo_name = slug_to_repo.get(slug)
        if not repo_name:
            log.warning("Deferred migration: no repo for slug %r, skipping %s", slug, fpath.name)
            continue

        # Read old file directly
        text = fpath.read_text(encoding="utf-8")
        raw_items = [line.strip()[2:] for line in text.splitlines()
                     if line.strip().startswith("- ")]

        # Deduplicate
        seen: set[str] = set()
        unique: list[str] = []
        for item in raw_items:
            norm = StateStore.deferred_dedup_key(item)
            if norm not in seen:
                seen.add(norm)
                unique.append(item)

        if unique:
            store.append_deferred(repo_name, unique)
            migrated_total += len(unique)
            log.info("Migrated %d unique deferred items for %s", len(unique), repo_name)

    shutil.rmtree(deferred_dir, ignore_errors=True)
    if migrated_total:
        log.info("Deferred migration complete: %d total unique items → TODO.md files", migrated_total)
    else:
        log.info("Deferred migration: no items to migrate, cleaned up empty directory")


async def run() -> None:
    """Main async entry point."""
    setup_logging()
    log.info("Starting Claude Bot...")

    if config.CLAUDE_ACCOUNTS:
        log.info(
            "Claude accounts configured: %d (%s)",
            len(config.CLAUDE_ACCOUNTS),
            ", ".join(config.CLAUDE_ACCOUNTS),
        )
        valid: list[str] = []
        for acct in config.CLAUDE_ACCOUNTS:
            acct_path = Path(acct)
            if not acct_path.is_dir():
                log.error(
                    "CLAUDE_ACCOUNTS entry dropped: %s (dir does not exist). "
                    "See CLAUDE.md -> Multi-Account Setup.",
                    acct,
                )
            elif not (acct_path / ".credentials.json").is_file():
                log.error(
                    "CLAUDE_ACCOUNTS entry dropped: %s (no .credentials.json -- "
                    "not logged in). See CLAUDE.md -> Multi-Account Setup.",
                    acct,
                )
            else:
                valid.append(acct)
        if len(valid) != len(config.CLAUDE_ACCOUNTS):
            log.warning(
                "Multi-account failover degraded: %d of %d accounts usable",
                len(valid),
                len(config.CLAUDE_ACCOUNTS),
            )
            config.CLAUDE_ACCOUNTS = valid

    if not config.CLAUDE_ACCOUNTS:
        log.warning(
            "CLAUDE_ACCOUNTS unset or all invalid -- running single-account "
            "from default ~/.claude. Failover is DISABLED. "
            "See CLAUDE.md -> Multi-Account Setup."
        )

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

    # One-time migration: data/deferred/*.md → repo TODO.md files
    _migrate_deferred_to_todo(store)

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
        is_deploy_protected, scan_deploy_config, _safe_int,
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
                    auto_fix=bool(_file_cfg.get("auto_fix")),
                    auto_fix_redeploy=bool(_file_cfg.get("auto_fix_redeploy")),
                    auto_fix_retries=_safe_int(_file_cfg.get("auto_fix_retries"), 1),
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
            runner.purge_drain_queue()  # discard stale queued messages
            runner._reboot_executing = False

    runner.set_on_idle_reboot(_execute_pending_reboots)

    # Schedule result callback
    async def on_schedule_result(instance, result, changed):
        if instance.status == InstanceStatus.FAILED:
            escaped = redact_secrets(instance.error or 'Unknown error')
            await notifier.broadcast(
                f"⚠️ **Scheduled task failed**\n{instance.display_id()}: {escaped}",
                ttl=15,
            )
        elif changed:
            escaped = redact_secrets(instance.summary or 'No summary')
            await notifier.broadcast(
                f"{instance.display_id()} (scheduled)\n{escaped}",
                silent=True, ttl=15,
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

                for inst in all_instances:
                    if not inst.cooldown_retry_at or not inst.cooldown_channel_id:
                        continue
                    if inst.id in _cooldown_retrying:
                        continue
                    try:
                        retry_at = dt.fromisoformat(inst.cooldown_retry_at)
                    except (ValueError, TypeError):
                        continue
                    if now >= retry_at:
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
        asyncio.create_task(auto_save_loop()),
        asyncio.create_task(cooldown_loop()),
    ]
    if config.AUTO_UPDATE:
        _bg_tasks.append(asyncio.create_task(
            auto_update_loop(stop_event, runner, notifier),
        ))
    if config.LOG_TRIAGE_ENABLED and discord_bot:
        from bot.discord.log_triage import run_triage_service
        _bg_tasks.append(asyncio.create_task(
            run_triage_service(discord_bot, stop_event),
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
        # Wait for notifier to have at least one messenger registered
        for _ in range(60):
            if notifier._messengers:
                break
            await asyncio.sleep(1)

        # Always broadcast "back online" to The Ark (persistent — no TTL)
        reason = reboot_data.get("message", "")
        back_msg = f"✅ {config.PC_NAME} back online."
        if reason:
            back_msg += f" ({reason})"
        await notifier.broadcast(back_msg)

        # Additionally resume a specific thread if channel_id was set
        _resume_channel = reboot_data.get("channel_id")
        _resume_platform = reboot_data.get("platform")
        _resume_prompt = reboot_data.get("resume_prompt")

        if _resume_channel and _resume_platform == "discord" and discord_bot:
            asyncio.create_task(discord_bot.dispatch_resume(
                _resume_channel, _resume_prompt or "", announce=None,
            ))
            log.info("Dispatched post-reboot resume to discord channel %s", _resume_channel)

        # Clean up reboot file (dispatch_resume also tries via unlink(missing_ok=True))
        try:
            config.REBOOT_MSG_FILE.unlink(missing_ok=True)
        except Exception:
            pass

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

    # Collect session_ids that will be auto-resumed (chain resume or drain
    # callback) so orphan cleanup can skip those threads
    auto_resuming_sessions: set[str] = set()
    all_chains = store.get_all_autopilot_chains()
    if all_chains:
        auto_resuming_sessions.update(all_chains.keys())

    # Resume autopilot chains that were interrupted by the restart
    if discord_bot:
        asyncio.create_task(
            _resume_interrupted_chains(store, discord_bot, drain_callback_channel_ids)
        )

    # Update thinking messages for orphaned instances (interrupted by restart).
    # Skips instances that will be auto-resumed to avoid confusing
    # "interrupted" → immediate restart sequence.
    await _cleanup_orphan_messages(
        notifier, orphans, auto_resuming_sessions, drain_callback_channel_ids,
    )

    # Restore interactive pending-prompt entries (Steer/Queue embeds) from
    # before the reboot.  Instances they reference are dead — we edit the
    # embed to "Lost on restart" so the user knows to resend.
    if discord_bot:
        asyncio.create_task(_restore_pending_prompts(discord_bot))

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


async def _send_reboot_announcement(notifier: NotificationService) -> dict | None:
    """If a reboot_message.json was left by a previous process, read and return it.

    The caller handles broadcasting, optional thread resume, and file cleanup.
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


async def _restore_pending_prompts(discord_bot) -> None:
    """Reconcile persisted pending-prompt entries after a reboot.

    At reboot all instances are gone — so every persisted pending entry is
    stale (the run it was queued behind is dead).  For each entry: edit the
    Queued embed to say "Lost on restart — please resend", then drop it from
    disk.  If a live replacement is needed later, the user sends again.
    """
    from bot.engine import pending as pending_mod

    if not await discord_bot._wait_for_ready("restore_pending_prompts"):
        return
    entries = pending_mod.load_from_disk()
    if not entries:
        return
    log.info("Reconciling %d pending-prompt entries from prior run", len(entries))
    for p in entries:
        if not p.message_id:
            continue
        try:
            await discord_bot.messenger.edit_text(
                p.channel_id, p.message_id,
                "⚠ Lost on restart — please resend your message.", None,
            )
        except Exception:
            log.debug("Could not edit stale pending embed %s", p.message_id)
    pending_mod.clear_persisted_file()


async def _cleanup_orphan_messages(
    notifier: NotificationService,
    orphans: list,
    auto_resuming_sessions: set[str] | None = None,
    drain_callback_channel_ids: set[str] | None = None,
) -> None:
    """Update thinking messages for instances that were interrupted by a restart.

    Finds the last message sent by each orphaned instance and edits it to show
    the interrupted status, so users see a clean resolution instead of a stale
    'processing...' indicator.

    Skips instances that will be auto-resumed (via chain resume or drain queue
    callback) to avoid the confusing "interrupted" → immediate restart sequence.
    """
    if not orphans:
        return
    _resuming = auto_resuming_sessions or set()
    _drain_cbs = drain_callback_channel_ids or set()
    from bot.platform.base import MessageHandle
    for inst in orphans:
        # Skip instances that will auto-resume via chain or drain callback
        if inst.session_id and inst.session_id in _resuming:
            continue

        for platform, msg_ids in inst.message_ids.items():
            if not msg_ids or platform not in notifier._messengers:
                continue
            messenger, _ = notifier._messengers[platform]
            # The last message_id is typically the thinking/progress message
            last_msg_id = msg_ids[-1]
            try:
                channel_id = None
                if inst.session_id and hasattr(messenger, 'find_channel_for_session'):
                    channel_id = messenger.find_channel_for_session(inst.session_id)
                # Also skip if channel is in drain callback set
                if channel_id and channel_id in _drain_cbs:
                    continue
                if channel_id:
                    handle = MessageHandle(
                        platform=platform,
                        _data={"message_id": last_msg_id, "channel_id": channel_id},
                    )
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


def _find_last_completed_instance(store, session_id: str):
    """Find the most recent COMPLETED instance for a session.

    Returns the last successfully completed step — the one whose output
    the next chain step should build upon.  Skips FAILED instances
    (which include the interrupted step marked by mark_orphans).
    """
    for inst in store.list_instances(all_=True):
        if inst.session_id == session_id and inst.status == InstanceStatus.COMPLETED:
            return inst
    return None


async def _resume_interrupted_chains(
    store, discord_bot, drain_callback_channel_ids: set[str],
) -> None:
    """Find autopilot chains that were interrupted by restart and resume them.

    Skips sessions whose thread already has a callback entry in the drain queue
    (that callback will replay the equivalent work).  Text-only drain entries
    do NOT suppress chain resume — they answer a user message but don't
    continue the autopilot chain.

    Brief delay lets drain queue dispatch start first and acquire its locks.
    """
    from bot.engine import workflows
    from bot.engine.commands import _get_channel_lock

    if not await discord_bot._wait_for_ready("resume_interrupted_chains"):
        return

    # Brief delay to let drain queue dispatch start and acquire channel locks
    await asyncio.sleep(5)

    all_chains = store.get_all_autopilot_chains()
    if not all_chains:
        return

    log.info("Found %d interrupted autopilot chains to resume", len(all_chains))
    for session_id, steps in all_chains.items():
        # Find the thread for this session
        lookup = discord_bot._forums.session_to_thread(session_id)
        if not lookup:
            log.warning(
                "No thread found for interrupted chain session %s — clearing",
                session_id,
            )
            store.clear_autopilot_chain(session_id)
            store.clear_chain_entry_sha(session_id)
            continue

        thread_id, info = lookup

        # Skip sessions whose thread has a callback in the drain queue
        if thread_id in drain_callback_channel_ids:
            log.info(
                "Skipping chain resume for session %s — thread %s covered by drain queue callback",
                session_id, thread_id,
            )
            continue

        # Find the last COMPLETED instance to use as source
        source = _find_last_completed_instance(store, session_id)
        if not source:
            log.warning(
                "No completed instance for interrupted chain session %s — clearing",
                session_id,
            )
            store.clear_autopilot_chain(session_id)
            store.clear_chain_entry_sha(session_id)
            continue

        log.info(
            "Resuming autopilot chain for session %s in thread %s (steps: %s)",
            session_id, thread_id, steps,
        )

        ctx = discord_bot._ctx(thread_id, session_id=session_id, thread_info=info)
        discord_bot._forums.attach_session_callbacks(ctx, info, thread_id)

        # Notify user that chain is resuming
        try:
            await ctx.messenger.send_text(
                thread_id, "Resuming interrupted chain...", silent=True,
            )
        except Exception:
            log.debug("Could not send chain resume notice to %s", thread_id)

        # Acquire channel lock to prevent racing with user messages
        lock = _get_channel_lock(thread_id)
        async with lock:
            try:
                # Re-run from chain[0] — the interrupted step that never
                # completed.  This differs from resume_autopilot_chain()
                # which skips [0] (designed for needs_input pauses where
                # [0] DID complete).
                await workflows._run_autopilot_chain(
                    ctx, source.id, None, steps, session_id,
                )
            except Exception:
                log.exception(
                    "Failed to resume autopilot chain for session %s", session_id,
                )
            finally:
                discord_bot._forums.persist_ctx_settings(ctx)
                asyncio.create_task(discord_bot._try_apply_tags_after_run(thread_id))
                discord_bot._schedule_sleep(thread_id)


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
        # Post-run cleanup (matches normal query flow in interactions.py)
        discord_bot._forums.persist_ctx_settings(ctx)
        discord_bot._schedule_sleep(channel_id)
        asyncio.create_task(discord_bot._try_apply_tags_after_run(channel_id))
        asyncio.create_task(discord_bot._refresh_dashboard())


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

    # Startup auth sync — pull credentials from Discord if local auth is broken
    try:
        from bot.services.auth_sync import startup_auth_check
        await startup_auth_check(bot)
    except Exception:
        log.exception("Startup auth check failed (non-fatal)")

    return bot
