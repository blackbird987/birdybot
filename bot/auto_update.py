"""Auto-update: poll the bot's own upstream, fast-forward, and ask for a reboot.

Dormant unless ``AUTO_UPDATE`` is set. Started from ``bot.app.run``.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from bot import config
from bot.claude.gitpaths import ref_sha
from bot.claude.runner import ClaudeRunner
from bot.platform.base import NotificationService
from bot.procutil import run_capture

log = logging.getLogger(__name__)


async def _git(*args: str, timeout: int):
    """Run one git command in the bot's own checkout, off the event loop."""
    return await asyncio.to_thread(
        run_capture, ["git", *args], cwd=str(config._PROJECT_ROOT), timeout=timeout,
    )


def _detect_update_branch() -> str:
    """Detect the remote default branch for auto-update.

    Priority: AUTO_UPDATE_BRANCH env > git symbolic-ref > fallback 'master'.
    """
    if config.AUTO_UPDATE_BRANCH:
        return config.AUTO_UPDATE_BRANCH
    try:
        result = run_capture(["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=str(config._PROJECT_ROOT), timeout=10,
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

    async def _fail(broadcast: str, *log_args) -> None:
        """Log and announce a failure, once per ongoing failure."""
        nonlocal failure_notified
        if failure_notified:
            return
        if log_args:
            log.warning(*log_args)
        await notifier.broadcast(broadcast, ttl=10)
        failure_notified = True

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
                fetch = await _git("fetch", "origin", "--tags", "--force", timeout=30)
                if fetch.returncode != 0:
                    err = fetch.stderr.strip() or "unknown error"
                    await _fail(f"Auto-update: git fetch failed — {err}",
                                "Auto-update: git fetch failed — %s", err)
                    continue

                # 2. Compare HEAD vs remote
                root = str(config._PROJECT_ROOT)
                local_sha = await asyncio.to_thread(ref_sha, root)
                remote_sha = await asyncio.to_thread(ref_sha, root, f"origin/{branch}")
                if not local_sha or not remote_sha:
                    await _fail(f"Auto-update: can't resolve branch `{branch}` — check AUTO_UPDATE_BRANCH",
                                "Auto-update: git rev-parse failed (branch=%s)", branch)
                    continue

                if local_sha == remote_sha:
                    failure_notified = False  # reset on success
                    continue

                # 3. Get commit log for notification
                log_result = await _git("log", "--oneline", f"{local_sha}..{remote_sha}", timeout=10)
                if log_result.returncode == 0 and log_result.stdout.strip():
                    commits = log_result.stdout.strip().splitlines()
                    n_commits = len(commits)
                    # Strip the short SHA prefix from the first commit message
                    latest_msg = commits[0].split(" ", 1)[1] if " " in commits[0] else commits[0]
                else:
                    # No new commits on remote — local is ahead or diverged
                    await _fail(
                        f"Auto-update: local HEAD differs from `origin/{branch}` "
                        "but remote has no new commits. Manual sync may be needed.",
                        "Auto-update: HEAD differs from origin/%s but no new "
                        "remote commits — local may be ahead/diverged", branch,
                    )
                    continue

                log.info("Auto-update: %d new commit(s) on origin/%s",
                         n_commits, branch)

                # 4. Pull (ff-only)
                pull = await _git("pull", "--ff-only", "origin", branch, timeout=30)
                if pull.returncode != 0:
                    err = pull.stderr.strip() or "unknown error"
                    await _fail(f"Auto-update: pull failed — {err}. Manual intervention needed.",
                                "Auto-update: git pull failed — %s", err)
                    continue

                # 4b. Verify HEAD actually moved (defensive — Fix 1 should
                # catch the no-op case, but guard against edge cases)
                post_pull = await asyncio.to_thread(ref_sha, str(config._PROJECT_ROOT))
                if post_pull == local_sha:
                    log.error(
                        "Auto-update: pull reported success but HEAD unchanged "
                        "— skipping reboot (local likely diverged)"
                    )
                    continue

            # 5. pip install (non-fatal)
            try:
                await asyncio.to_thread(run_capture, [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
                    cwd=str(config._PROJECT_ROOT), timeout=120,
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
            await _fail("Auto-update: unexpected error — check logs.")
