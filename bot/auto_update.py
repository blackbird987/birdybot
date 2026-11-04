"""Auto-update: poll the bot's own upstream, fast-forward, and ask for a reboot.

Dormant unless ``AUTO_UPDATE`` is set. Started from ``bot.app.run``.

With ``AUTO_UPDATE_APPROVAL`` on (the default) the poll stops short of the pull
and posts one card, and the owner's tap on Apply update fast-forwards to the
exact commit the card showed. See docs/design/peer-link.md: this is a speed
bump, not a boundary. A session with a shell on this machine can still pull.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass

from bot import config
from bot.claude.gitpaths import ref_sha
from bot.claude.runner import ClaudeRunner
from bot.platform.base import ButtonSpec, NotificationService
from bot.procutil import run_capture

log = logging.getLogger(__name__)

APPLY_ACTION = "auto_update_apply"
LATER_ACTION = "auto_update_later"

# Platform-state key holding the last sha a card went out for. Kept in state,
# not memory, so a reboot does not post the same update again.
_STATE_KEY = "auto_update"
_MAX_SUBJECTS = 10
_SUBJECT_CHARS = 100
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


async def _git(*args: str, timeout: int):
    """Run one git command in the bot's own checkout, off the event loop."""
    # utf-8/replace: text=True on Windows decodes as cp1252, and a commit
    # subject it cannot decode must not take the poll down with it.
    return await asyncio.to_thread(
        run_capture, ["git", *args], cwd=str(config._PROJECT_ROOT), timeout=timeout,
        encoding="utf-8", errors="replace",
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


def carded_sha(store) -> str | None:
    """The sha of the newest update card that went out, if any."""
    return store.get_platform_state(_STATE_KEY).get("carded_sha")


def _record_carded(store, sha: str | None) -> None:
    store.set_platform_state(_STATE_KEY, {"carded_sha": sha} if sha else {})


def current_card_custom_id(store) -> str | None:
    """The Apply button id of the live card, so the Ark's startup purge can spare it."""
    sha = carded_sha(store)
    return f"{APPLY_ACTION}:{sha}" if sha else None


def _clean_subject(subject: str) -> str:
    """A commit subject, made safe to show. Anyone who can push writes this text."""
    shown = "".join(c if c.isprintable() else " " for c in subject).strip()
    if len(shown) > _SUBJECT_CHARS:
        shown = shown[:_SUBJECT_CHARS - 3] + "..."
    # The card shows these in a code block, where nothing renders. These two
    # stop a subject from closing the block, and a mention pinging if it did.
    return shown.replace("`", "'").replace("@", "@\u200b")


def _subject_of(oneline: str) -> str:
    """Strip the short sha off one line of ``git log --oneline``."""
    return oneline.split(" ", 1)[1] if " " in oneline else oneline


def _card_text(sha: str, branch: str, commits: list[str], n_files: int | None) -> str:
    """The card's text, from ``git log --oneline`` lines, newest first."""
    subjects = [_clean_subject(_subject_of(line)) for line in commits[:_MAX_SUBJECTS]]
    if len(commits) > _MAX_SUBJECTS:
        subjects.append(f"...and {len(commits) - _MAX_SUBJECTS} more")
    files = f"{n_files} file(s) changed" if n_files is not None else "changed files unknown"
    body = "\n".join(subjects)
    return (
        f"🔄 **Update waiting:** {len(commits)} commit(s), {files}, "
        f"`{sha[:7]}` on `{branch}`.\n"
        f"```text\n{body}\n```\n"
        "Whoever can push wrote this text, and a tap does not read the code."
    )


async def _offer_update(
    runner: ClaudeRunner, notifier: NotificationService,
    branch: str, local_sha: str, remote_sha: str, commits: list[str],
) -> None:
    """Post one card for ``remote_sha``, unless one already went out for it."""
    store = runner._store
    previous = carded_sha(store)
    if previous == remote_sha:
        return
    diff = await _git("diff", "--name-only", local_sha, remote_sha, timeout=10)
    n_files = len(diff.stdout.splitlines()) if diff.returncode == 0 else None
    # Recorded before the send, so the Ark's startup purge cannot catch a card
    # that is out but not yet on record, and put back if nothing was delivered:
    # the first poll runs before Discord is up, and a card marked sent that never
    # went out would hold the update back until the next commit.
    _record_carded(store, remote_sha)
    delivered = await notifier.broadcast(
        _card_text(remote_sha, branch, commits, n_files),
        buttons=[[
            ButtonSpec("Apply update", f"{APPLY_ACTION}:{remote_sha}"),
            ButtonSpec("Later", f"{LATER_ACTION}:{remote_sha}"),
        ]],
    )
    if not delivered:
        log.info("Auto-update: card for %s not delivered, will retry", remote_sha[:7])
        _record_carded(store, previous)


@dataclass
class ApplyOutcome:
    """What a tap on Apply update did. ``message`` is for the owner."""
    message: str
    applied: bool = False
    keep_buttons: bool = False  # the card can still be tapped, say, after a failed fetch
    n_commits: int = 0
    latest_msg: str = ""


async def apply_update(
    runner: ClaudeRunner, notifier: NotificationService, sha: str,
) -> ApplyOutcome:
    """Fast-forward to exactly ``sha``, the commit the tapped card showed.

    Never to whatever origin holds at tap time: the point of the tap is lost if
    it can land code nobody was shown. Stops short of the install and reboot,
    so the caller can update the card first (see ``finish_update``).
    """
    if not config.AUTO_UPDATE:
        return ApplyOutcome("Auto-update is off on this machine, so nothing was applied.")
    if not _FULL_SHA.fullmatch(sha):
        return ApplyOutcome("This button does not name a commit, so nothing was applied.")
    store = runner._store
    root = str(config._PROJECT_ROOT)
    branch = await asyncio.to_thread(_detect_update_branch)
    async with runner._get_repo_lock(root):
        if carded_sha(store) != sha:
            return ApplyOutcome("A newer update card exists. Use that one.")
        local_sha = await asyncio.to_thread(ref_sha, root)
        if local_sha == sha:
            return ApplyOutcome("This machine is already on that commit.")

        fetch = await _git("fetch", "origin", "--tags", "--force", timeout=30)
        if fetch.returncode != 0:
            err = fetch.stderr.strip()[:300] or "unknown error"
            return ApplyOutcome(f"Nothing applied: git fetch failed, {err}", keep_buttons=True)
        remote_sha = await asyncio.to_thread(ref_sha, root, f"origin/{branch}")
        if not local_sha or not remote_sha:
            return ApplyOutcome(f"Nothing applied: can't resolve `{branch}`.", keep_buttons=True)

        if remote_sha != sha:
            moved = await _git("log", "--oneline", f"{local_sha}..{remote_sha}", timeout=10)
            commits = moved.stdout.strip().splitlines() if moved.returncode == 0 else []
            if not commits:
                return ApplyOutcome(
                    f"Nothing applied: `origin/{branch}` moved away from this commit "
                    "and has nothing new for this machine. Manual sync may be needed."
                )
            await _offer_update(runner, notifier, branch, local_sha, remote_sha, commits)
            return ApplyOutcome(
                f"Nothing applied: `origin/{branch}` moved since this card. "
                "A fresh card shows what is there now."
            )

        ancestor = await _git("merge-base", "--is-ancestor", local_sha, sha, timeout=10)
        if ancestor.returncode != 0:
            return ApplyOutcome(
                "Nothing applied: this is not a fast-forward, because this machine "
                "has commits origin does not. Manual sync needed.",
                keep_buttons=True,
            )
        listed = await _git("log", "--oneline", f"{local_sha}..{sha}", timeout=10)
        commits = listed.stdout.strip().splitlines() if listed.returncode == 0 else []

        merge = await _git("merge", "--ff-only", sha, timeout=30)
        if merge.returncode != 0:
            err = merge.stderr.strip()[:300] or "unknown error"
            log.warning("Auto-update: fast-forward failed, %s", err)
            return ApplyOutcome(f"Nothing applied: fast-forward failed, {err}", keep_buttons=True)
        if await asyncio.to_thread(ref_sha, root) != sha:
            log.error("Auto-update: fast-forward reported success but HEAD is not %s", sha[:7])
            return ApplyOutcome("Nothing applied: HEAD did not land on that commit.", keep_buttons=True)

    return ApplyOutcome(
        f"Applied `{sha[:7]}`. Installing and rebooting.", applied=True,
        n_commits=len(commits), latest_msg=_subject_of(commits[0]) if commits else "",
    )


async def finish_update(
    runner: ClaudeRunner, notifier: NotificationService, n_commits: int, latest_msg: str,
) -> None:
    """The steps after HEAD has moved: install, ask for the reboot, say so."""
    # pip install (non-fatal)
    try:
        await asyncio.to_thread(run_capture, [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
            cwd=str(config._PROJECT_ROOT), timeout=120,
        )
    except Exception:
        log.warning("Auto-update: pip install failed (non-fatal)", exc_info=True)

    # Human-friendly strings, reused in the reboot request and the broadcast
    count_str = f"pulled {n_commits} commit(s)"
    subject = _clean_subject(latest_msg)
    detail = f": `{subject}`" if subject else ""

    runner.request_reboot({
        "message": f"Auto-update: {count_str}",
    })

    # Notify (best-effort: the reboot is already queued)
    try:
        await notifier.broadcast(
            f"🔄 Auto-update: {count_str}{detail}\nRebooting...",
            ttl=10,
        )
    except Exception:
        log.warning("Auto-update: notification failed (reboot still queued)")


async def auto_update_loop(
    stop_event: asyncio.Event,
    runner: ClaudeRunner,
    notifier: NotificationService,
) -> None:
    """Periodically check for upstream changes, then pull and reboot, or ask first."""
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
                    latest_msg = _subject_of(commits[0])
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

                if config.AUTO_UPDATE_APPROVAL:
                    failure_notified = False
                    await _offer_update(runner, notifier, branch, local_sha, remote_sha, commits)
                    continue

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

            # 5. Install, reboot, notify
            failure_notified = False
            await finish_update(runner, notifier, n_commits, latest_msg)
            return  # reboot requested, exit loop

        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Auto-update: unexpected error")
            await _fail("Auto-update: unexpected error — check logs.")
