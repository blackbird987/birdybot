"""The Control Room's Sync Git button: pull (fast-forward only), then push.

Kept out of the Discord layer so the git sequence can be tested without an
interaction. ``sync_repo`` returns the one line to show the user and whether
the Control Room should be redrawn; the caller only sends and refreshes.
"""

from __future__ import annotations

import asyncio
import logging

from bot.claude.runner import RebootResult, git_fail_reason
from bot.procutil import run_capture

log = logging.getLogger(__name__)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


async def _git(repo_path: str, *args: str, timeout: int):  # noqa: ANN202
    return await asyncio.to_thread(
        run_capture, ["git", *args], cwd=repo_path, timeout=timeout,
    )


def _reason(result) -> str:  # noqa: ANN001
    return git_fail_reason((result.stderr or result.stdout or "").strip())


async def sync_repo(store, runner, repo_name: str, channel_id: str) -> tuple[str, bool]:  # noqa: ANN001
    """Bidirectional git sync: pull from remote (ff-only), then push local
    changes and tags.

    Returns ``(message, refresh)``. ``refresh`` is True only when the sync ran
    to the end, which is when the Control Room may show a new version or ref.
    """
    repo_path = store.list_repos().get(repo_name)
    if not repo_path:
        return f"Repo `{repo_name}` not found.", False
    try:
        return await _sync(store, runner, repo_name, repo_path, channel_id)
    except Exception as exc:
        return f"`{repo_name}`: Git sync failed \u2014 {exc}", False


async def _sync(store, runner, repo_name: str, repo_path: str,  # noqa: ANN001
                channel_id: str) -> tuple[str, bool]:
    ds = store.get_deploy_state(repo_name)

    # Fetch branches (must succeed), then tags with --force (non-fatal).
    fetch = await _git(repo_path, "fetch", "origin", timeout=30)
    if fetch.returncode != 0:
        err = _reason(fetch) or "unknown error"
        return f"`{repo_name}`: Fetch failed \u2014 `{err}`", False
    tag_fetch = await _git(repo_path, "fetch", "origin", "--tags", "--force", timeout=15)
    if tag_fetch.returncode != 0:
        log.warning("Tag fetch failed for %s: %s", repo_name,
                    (tag_fetch.stderr or "")[:200])

    ahead_result = await _git(repo_path, "rev-list", "--count", "@{upstream}..HEAD", timeout=10)
    behind_result = await _git(repo_path, "rev-list", "--count", "HEAD..@{upstream}", timeout=10)
    if ahead_result.returncode != 0 or behind_result.returncode != 0:
        return f"`{repo_name}`: No upstream branch configured.", False
    ahead = int(ahead_result.stdout.strip())
    behind = int(behind_result.stdout.strip())
    parts: list[str] = []

    if behind > 0:
        status = await _git(repo_path, "status", "--porcelain", timeout=10)
        if status.returncode == 0 and status.stdout.strip():
            return (f"`{repo_name}`: Working tree has uncommitted changes "
                    f"\u2014 commit or stash first."), False
        pull = await _git(repo_path, "pull", "--ff-only", timeout=30)
        if pull.returncode != 0:
            err = _reason(pull) or "unknown error"
            return f"`{repo_name}`: Pull failed (histories diverged?) \u2014 `{err}`", False
        parts.append(f"pulled {_plural(behind, 'commit')}")

        # HEAD moved forward, so the deploy state's ref and version did too.
        from bot.engine.deploy import (
            detect_version, get_head_ref, get_unreleased_changes,
        )
        if ds:
            ds.current_ref = get_head_ref(repo_path)
            ds.current_version = detect_version(repo_path)
            changes = get_unreleased_changes(repo_path)
            if changes:
                ds.pending_changes = changes
            store.set_deploy_state(repo_name, ds)

        # Self-managed repo pulled new code: queue a reboot.
        # No force=True: this path does NOT call wait_until_idle, so an
        # active build on another thread should NOT be force-killed. If
        # the busy-check defers, lifecycle.py auto-promotes the deferred
        # file at the next idle session-end (within REBOOT_DEFERRED_TTL_SECS).
        if ds and ds.self_managed:
            result = runner.request_reboot({
                "message": f"Sync Git: pulled {behind} commit(s)",
                "channel_id": channel_id,
                "platform": "discord",
            })
            if result is RebootResult.DEFERRED:
                parts.append(
                    "reboot deferred \u2014 will auto-retry within 1 h or "
                    "pulled commits will be dropped"
                )
            else:
                parts.append("reboot queued")

    if ahead > 0:
        push = await _git(repo_path, "push", timeout=30)
        if push.returncode != 0:
            err = _reason(push) or "unknown error"
            return f"`{repo_name}`: Push failed \u2014 `{err}`", False
        parts.append(f"pushed {_plural(ahead, 'commit')}")

    # Push tags (best-effort).
    tags = await _git(repo_path, "push", "--tags", timeout=30)
    if tags.returncode == 0:
        new_tags = [
            line for line in (tags.stderr or "").splitlines()
            if "new tag" in line.lower()
        ]
        if new_tags:
            parts.append(_plural(len(new_tags), "tag"))
    else:
        err = _reason(tags)
        if err:
            parts.append(f"tags failed: `{err}`")

    if not parts:
        return f"`{repo_name}`: Already in sync with remote.", True
    return f"`{repo_name}`: {', '.join(parts)}.", True
