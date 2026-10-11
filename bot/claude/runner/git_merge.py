"""Merging, discarding and auto-committing build branches."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from bot import config
from bot.procutil import run_capture
from bot.claude.gitpaths import git_common_dir, git_dir, git_toplevel, ref_sha
from bot.claude.types import (
    Instance, REPO_UNUSABLE_MARKER,
)
from bot.claude.runner.common import _NOWND
from bot.claude.runner.git_release import git_fail_reason, version_tags

log = logging.getLogger(__name__)


@dataclass
class PrecheckResult:
    """Outcome of ``ClaudeRunner._check_main_repo_clean``.

    Backwards-compatible replacement for the prior ``Optional[str]`` return
    type. ``error`` mirrors the old "None means clean, string means bail"
    contract for legacy callers. ``recovery_note`` carries non-fatal
    user-facing detail (e.g. a stash sha created during orphaned-index
    recovery) that the merge_branch success path appends to its return
    string so the user is told what we did to their repo.
    """

    error: str | None = None
    recovery_note: str | None = None


# Failure-kind taxonomy for merge_branch outcomes. Persisted on
# ``pending_merge["failure_kind"]`` so commands.py can render the right
# user-facing banner (see §4 of the t-4114 plan). String-valued so the
# JSON state round-trip is transparent.
MERGE_FAIL_GENERIC = "generic_failure"
MERGE_FAIL_ORPHANED_INDEX = "orphaned_index"
MERGE_FAIL_RECOVERY_FAILED = "recovery_failed"
MERGE_FAIL_DIVERGED = "diverged"


class DiscardOutcome(NamedTuple):
    """Result of discard_branch.

    `preserved_branch` is set when uncommitted work was found and rolled into
    a WIP commit so the branch survives — callers can surface a recovery hint.
    `failed` means the branch was not discarded and is still there, so callers
    must keep its pending merge and leave the instances that point at it alone.
    """
    message: str
    preserved_branch: str | None = None
    failed: bool = False

    def __str__(self) -> str:  # back-compat with callers that f-string the result
        return self.message


class MergeMixin:
    """Merging, discarding and auto-committing build branches. Mixed into ClaudeRunner."""

    # --- Diff ---

    async def _save_diff(self, instance: Instance) -> None:
        """Save git diff for a build task."""
        if not instance.branch:
            return
        if not instance.worktree_path and not instance.repo_path:
            return
        await asyncio.to_thread(self._save_diff_sync, instance)

    def _save_diff_sync(self, instance: Instance) -> None:
        try:
            # Diff runs in worktree (where changes are) against the merge base
            diff_cwd = instance.worktree_path or instance.repo_path
            base = instance.original_branch or "HEAD~1"
            result = run_capture(["git", "diff", base, "--", "."], cwd=diff_cwd)
            if (result.stdout or "").strip():
                diff_path = config.RESULTS_DIR / f"{instance.id}.diff"
                diff_path.write_text(result.stdout, encoding="utf-8")
                instance.diff_file = str(diff_path)
        except Exception:
            log.exception("Failed to save diff for %s", instance.id)

    # --- Merge / Discard ---

    @staticmethod
    def _check_main_repo_clean(repo: str) -> PrecheckResult:
        """Detect and recover from leftover merge state in the main repo.

        Two failure modes are handled:

        **Path A — leftover MERGE_HEAD** (Bug 2): when an in-process merge
        dies on a non-CalledProcessError exception (or the bot crashes
        mid-merge), MERGE_HEAD persists in ``.git/`` and poisons every
        subsequent operation. Run ``git merge --abort`` to clear it
        (idempotent, safe when no merge is active).

        **Path B — orphaned unmerged-index without MERGE_HEAD** (t-4114):
        ``git merge --abort`` followed by a stash-pop that itself produced
        conflicts can leave the index with unmerged entries while clearing
        MERGE_HEAD. The next ``git checkout`` in the main repo then fails
        with ``you need to resolve your current index first``, and the Try
        Merge Again loop spins forever (the worktree-side replay always
        succeeds; only the main-repo merge fails). Recover with
        ``git reset --merge``; if that's blocked by dirty overlapping
        edits, snapshot to a labeled stash and ``git reset --hard HEAD``
        (safe nuke because work is preserved in the stash).

        Returns a ``PrecheckResult`` where ``error`` is None on the clean
        or auto-recovered path, ``recovery_note`` carries a user-facing
        note when a stash was created during recovery (so merge_branch can
        surface it in the success message — no silent data creation).
        """
        # Path A: leftover MERGE_HEAD.  The registered repo dir may be a
        # subdirectory of the actual repo, so resolve the real gitdir.
        gd = git_dir(repo)
        merge_head = (
            Path(gd) / "MERGE_HEAD" if gd
            else Path(repo) / ".git" / "MERGE_HEAD"
        )
        if merge_head.exists():
            log.warning(
                "Main repo %s has leftover MERGE_HEAD — attempting cleanup", repo,
            )
            try:
                run_capture(["git", "merge", "--abort"], cwd=repo)
            except Exception:
                log.warning(
                    "git merge --abort raised in %s", repo, exc_info=True,
                )
            if merge_head.exists():
                return PrecheckResult(
                    error=(
                        f"Main repo {repo} has unresolved MERGE_HEAD that could "
                        f"not be auto-aborted — manual resolution needed "
                        f"(`git merge --abort` in the main repo, then retry)."
                    ),
                )
            log.info("Cleared leftover MERGE_HEAD in %s", repo)

        # Path B: orphaned unmerged-index without MERGE_HEAD.
        # `git ls-files --unmerged` returns rows iff the index still has
        # unmerged stages. After Path A's abort succeeds, the index is
        # usually clean — but the abort+stash-pop interaction can leave
        # residue, so always check.
        unmerged = run_capture(["git", "ls-files", "--unmerged"], cwd=repo)
        if unmerged.returncode != 0 or not (unmerged.stdout or "").strip():
            return PrecheckResult()

        files = sorted({
            line.split("\t", 1)[-1]
            for line in unmerged.stdout.strip().splitlines()
            if "\t" in line
        })
        log.warning(
            "Main repo %s has orphaned unmerged index entries (%d files): %s "
            "— attempting recovery via `git reset --merge`",
            repo, len(files), ", ".join(files[:5]),
        )

        # First try the gentle path: `git reset --merge` keeps unrelated
        # working-tree edits intact and just drops merge-only stages.
        reset_r = run_capture(["git", "reset", "--merge"], cwd=repo)
        if reset_r.returncode == 0:
            recheck = run_capture(["git", "ls-files", "--unmerged"], cwd=repo)
            if not (recheck.stdout or "").strip():
                log.info(
                    "Cleared orphaned unmerged index in %s via `git reset --merge`",
                    repo,
                )
                return PrecheckResult()

        # `reset --merge` refused (dirty working-tree files overlap the
        # conflict). Snapshot the world via `stash create` + `stash store`
        # (no pathspec — `stash push -- <unmerged paths>` is too
        # version-fragile), then `git reset --hard HEAD` to clear the
        # index. The work is preserved in the labeled stash; we never
        # auto-pop so the user can inspect deliberately.
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        label = f"auto-stash poisoned-index-recovery {timestamp}"

        create_r = run_capture(["git", "stash", "create"], cwd=repo)
        stash_sha = (create_r.stdout or "").strip()
        if create_r.returncode != 0 or not stash_sha:
            log.error(
                "git stash create failed in %s while recovering orphaned index: rc=%d %s",
                repo, create_r.returncode, (create_r.stderr or "").strip(),
            )
            return PrecheckResult(
                error=(
                    f"Could not snapshot main repo {repo} for recovery "
                    f"(`git stash create` failed). Manual resolution needed: "
                    f"`cd` to the main repo and run `git reset --merge` or "
                    f"`git status` to inspect."
                ),
            )

        store_r = run_capture(["git", "stash", "store", "-m", label, stash_sha], cwd=repo)
        if store_r.returncode != 0:
            # Stash object exists as a loose commit; user can still recover
            # it via `git stash apply <sha>` even without the ref. Warn but
            # bail — don't reset --hard without a reachable stash.
            log.error(
                "git stash store failed in %s (sha=%s): rc=%d %s",
                repo, stash_sha[:8], store_r.returncode,
                (store_r.stderr or "").strip(),
            )
            return PrecheckResult(
                error=(
                    f"Recovery aborted — `git stash store` failed for sha "
                    f"{stash_sha[:12]} in {repo}. Your work survives as a "
                    f"loose object until git gc; recover with "
                    f"`git stash apply {stash_sha}`."
                ),
            )

        # Now safe to nuke: working tree + index are captured in the stash.
        hard_r = run_capture(["git", "reset", "--hard", "HEAD"], cwd=repo)
        if hard_r.returncode != 0:
            log.error(
                "git reset --hard HEAD failed in %s after stash: rc=%d %s",
                repo, hard_r.returncode, (hard_r.stderr or "").strip(),
            )
            return PrecheckResult(
                error=(
                    f"Recovery failed — your changes are preserved in stash "
                    f"{stash_sha[:12]} (label: '{label}'). Recover with "
                    f"`git stash apply {stash_sha}` once the index is "
                    f"manually unstuck."
                ),
            )

        # Final sanity check.
        final = run_capture(["git", "ls-files", "--unmerged"], cwd=repo)
        if (final.stdout or "").strip():
            log.error(
                "Orphaned unmerged index survived `git reset --hard HEAD` in %s",
                repo,
            )
            return PrecheckResult(
                error=(
                    f"Recovery failed — index still has unmerged entries after "
                    f"`git reset --hard HEAD` in {repo}. Your work is preserved "
                    f"in stash {stash_sha[:12]} (label: '{label}'). Manual "
                    f"intervention required."
                ),
            )

        log.info(
            "Recovered orphaned merge state in %s; stashed as %s (%s)",
            repo, stash_sha[:8], label,
        )
        return PrecheckResult(
            recovery_note=(
                f"ℹ️ Recovered orphaned merge state; preserved local changes "
                f"in stash `{stash_sha[:12]}` (label: '{label}'). To restore: "
                f"`git stash apply {stash_sha}` or "
                f"`git stash list | grep poisoned-index-recovery`."
            ),
        )

    @staticmethod
    def _ensure_union_merge_driver(repo: str) -> None:
        """Mark CHANGELOG.md ``merge=union`` in the repo's shared gitdir.

        Resolves the real common gitdir — the registered repo dir may be a
        subdirectory of the repo, where ``<repo>/.git`` does not exist and
        writing it would create a junk dir git ignores (found literally on
        disk in the AIAgent subdir before this fix).  Skips quietly when
        the gitdir can't be resolved rather than mkdir-ing a fake one.

        Also exercised directly by ``scripts/merge_drill.py`` so the drill
        tests this exact code path, not a hand-mirrored copy.
        """
        try:
            gd = git_common_dir(repo)
            if gd is None:
                log.debug("No gitdir resolvable for %s — skipping merge driver", repo)
                return
            attrs_dir = Path(gd) / "info"
            attrs_dir.mkdir(exist_ok=True)
            attrs_file = attrs_dir / "attributes"
            # errors="replace": a stray non-UTF8 byte in a user's attributes
            # file must not abort the merge — UnicodeDecodeError is a
            # ValueError and would sail past the except below.
            attrs_content = (
                attrs_file.read_text(encoding="utf-8", errors="replace")
                if attrs_file.exists() else ""
            )
            if "CHANGELOG.md" not in attrs_content:
                with open(attrs_file, "a", encoding="utf-8") as f:
                    f.write("CHANGELOG.md merge=union\n")
        except OSError:
            log.debug("Could not set up merge driver for %s", repo)

    @staticmethod
    def _classify_post_abort_state(repo: str) -> str:
        """Classify a merge failure AFTER ``git merge --abort`` has run.

        Reads ``git ls-files --unmerged`` — entries remaining mean the
        abort left orphaned index stages that will poison the next merge
        attempt (the t-4114 symptom). Caller writes the result to
        ``self._last_merge_failure_kind[instance.id]`` so commands.py can
        render the right banner.

        Must be called AFTER the abort (not before) so legitimate mid-flight
        conflicts — which legitimately have MERGE_HEAD + unmerged entries
        until the abort clears them — don't get misclassified.
        """
        try:
            r = run_capture(["git", "ls-files", "--unmerged"], cwd=repo)
        except Exception:
            log.warning(
                "ls-files --unmerged raised during classification in %s",
                repo, exc_info=True,
            )
            return MERGE_FAIL_GENERIC
        if r.returncode == 0 and (r.stdout or "").strip():
            return MERGE_FAIL_ORPHANED_INDEX
        return MERGE_FAIL_GENERIC

    async def merge_branch(self, instance: Instance) -> str:
        """Merge worktree branch into master. Returns status message."""
        if not instance.branch or not instance.original_branch:
            if instance.original_branch and not instance.branch:
                return f"Already merged ({config.BRANCH_PREFIX}/{instance.id} → {instance.original_branch})"
            return "No branch to merge"
        if not instance.repo_path:
            return "No repo path"
        repo_lock = self._get_repo_lock(instance.repo_path)
        async with repo_lock:
            return await asyncio.to_thread(self._merge_branch_sync, instance)

    def _merge_branch_sync(self, instance: Instance) -> str:
        stashed = False
        repo = instance.repo_path
        target = instance.original_branch
        # Fresh attempt — clear any stale failure-kind from a prior run.
        # Success paths intentionally don't clear: pending_merge is wiped by
        # the platform layer on success, so the side-channel entry simply
        # ages out the next time merge_branch is invoked for this instance.
        self._last_merge_failure_kind.pop(instance.id, None)
        # Idempotency: if the branch ref no longer exists, a prior run already
        # merged + cleaned it up — state just didn't persist before a restart.
        # Treat as success so the resume path doesn't surface a scary "failed"
        # message.  Manual `git branch -D` outside the bot would also land
        # here, which is acceptable: that workflow isn't supported, and the
        # alternative (failing loudly on the legitimate restart case) is worse.
        ref_check = run_capture(
            ["git", "rev-parse", "--verify", f"refs/heads/{instance.branch}"], cwd=repo,
        )
        if ref_check.returncode != 0:
            # Branch is gone but the worktree dir may linger if the prior run
            # crashed between `git merge` and `git worktree remove`.  Best-
            # effort cleanup so we don't leak it for /branches to find later.
            if instance.worktree_path and Path(instance.worktree_path).exists():
                run_capture(
                    ["git", "worktree", "remove", instance.worktree_path, "--force"], cwd=repo,
                )
            stale_branch = instance.branch
            instance.branch = None
            instance.worktree_path = None
            return (
                f"Already merged ({stale_branch} → {instance.original_branch})"
            )
        # Bug 2 pre-flight: detect a poisoned tree from a prior session's
        # crashed merge BEFORE we layer our own merge on top. Bails cleanly
        # if the leftover state can't be auto-recovered.
        precheck = self._check_main_repo_clean(repo)
        if precheck.error:
            # Classify so the platform layer can render the right banner.
            # If `_check_main_repo_clean` returned an error, recovery
            # attempted and failed — distinct from a generic merge failure.
            self._last_merge_failure_kind[instance.id] = MERGE_FAIL_RECOVERY_FAILED
            return f"Merge failed: {precheck.error}"
        recovery_note = precheck.recovery_note
        merge_succeeded = False
        try:
            # Copy session files back before cleanup.  account_dir=None lets
            # the helper fall through to instance.session_account (the account
            # that hosted the run), which is correct for the post-merge case
            # where we no longer have the live runner context.
            self._copy_session_from_worktree(instance, None)

            # Re-verify original_branch exists; re-detect if stale
            r = run_capture(["git", "rev-parse", "--verify", f"refs/heads/{target}"], cwd=repo)
            if r.returncode != 0:
                target = self._get_default_branch(repo)
                instance.original_branch = target

            # Stash uncommitted changes that would block checkout.  Capture
            # the file list (capped for display) and the true total so the
            # post-merge message can report state honestly without hiding
            # the count behind the cap.
            dirty_files: list[str] = []
            dirty_total = 0
            status_r = run_capture(["git", "status", "--porcelain"], cwd=repo)
            if status_r.stdout.strip():
                parsed = [
                    line[3:].strip() for line in status_r.stdout.strip().splitlines()
                ]
                dirty_total = len(parsed)
                dirty_files = parsed[:5]  # display cap only — dirty_total carries truth
                log.info("Stashing dirty working tree in %s before merge (%d files): %s",
                         repo, dirty_total, ", ".join(dirty_files))
                stash_r = run_capture(
                    ["git", "stash", "push", "-m",
                     f"auto-stash for merge {instance.branch}"],
                    cwd=repo,
                )
                if stash_r.returncode == 0:
                    stashed = True

            # Set up union merge driver for changelog (prevents conflict
            # failures).
            self._ensure_union_merge_driver(repo)

            # Ensure main repo is on the correct branch before merging
            run_capture(["git", "checkout", target], cwd=repo, check=True)

            # Sync target with origin before merging: releases can land on
            # origin from another machine first, and merging/tagging against
            # a stale local target mints versions below the remote's.
            # Offline-tolerant — a failed fetch degrades to a local merge
            # with a visible note rather than blocking.
            sync_note = ""
            has_remote = run_capture(
                ["git", "remote", "get-url", "origin"], cwd=repo,
            ).returncode == 0
            if has_remote:
                fetch_err: str | None = None
                try:
                    fetch_r = run_capture(
                        ["git", "fetch", "origin", "--tags", "--prune"], cwd=repo, timeout=30,
                    )
                    if fetch_r.returncode != 0:
                        fetch_err = (fetch_r.stderr or fetch_r.stdout or "").strip()
                except subprocess.TimeoutExpired:
                    fetch_err = "timed out (30s)"
                except Exception as e:
                    fetch_err = f"{type(e).__name__}"
                if fetch_err is not None:
                    log.warning("Fetch origin failed in %s before merge: %s",
                                repo, fetch_err)
                    sync_note = (
                        "\nCould not sync with origin — merged against local state"
                    )
                    # Same reasoning as the push note below: without git's own
                    # sentence this says only that something went wrong, and
                    # this is the branch the credential hang actually landed
                    # in ("Fetch origin failed ... timed out (30s)").
                    if reason := git_fail_reason(fetch_err):
                        sync_note += f"\n`{reason}`"
                else:
                    ahead = self._rev_count(repo, f"origin/{target}..{target}")
                    behind = self._rev_count(repo, f"{target}..origin/{target}")
                    if ahead and behind:
                        # Local target and origin have diverged — merging
                        # now would build on a history origin rejects.
                        self._last_merge_failure_kind[instance.id] = (
                            MERGE_FAIL_DIVERGED
                        )
                        log.error(
                            "Local %s and origin/%s diverged in %s "
                            "(+%d local / +%d remote) — aborting merge",
                            target, target, repo, ahead, behind,
                        )
                        stash_status = self._restore_stash(repo) if stashed else ""
                        recovery_suffix = (
                            f"\n{recovery_note}" if recovery_note else ""
                        )
                        return (
                            f"Merge failed: local {target} and origin/{target} "
                            f"have diverged (+{ahead} local / +{behind} remote). "
                            f"Run `git pull --rebase` in the main repo, then "
                            f"Try Merge Again."
                            f"{stash_status}{recovery_suffix}"
                        )
                    if behind and not ahead:
                        ff_r = run_capture(
                            ["git", "merge", "--ff-only", f"origin/{target}"], cwd=repo,
                        )
                        if ff_r.returncode != 0:
                            ff_detail = (ff_r.stderr or ff_r.stdout or "").strip()
                            log.warning(
                                "Fast-forward of %s to origin failed in %s: %s",
                                target, repo, ff_detail,
                            )
                            sync_note = (
                                "\nCould not fast-forward to origin — "
                                "merged against local state"
                            )
                            if reason := git_fail_reason(ff_detail):
                                sync_note += f"\n`{reason}`"
                        else:
                            log.info(
                                "Fast-forwarded %s to origin/%s (+%d) in %s",
                                target, target, behind, repo,
                            )

            # Merge the worktree's branch.  No -X ours: real conflicts
            # surface and flow through _auto_resolve_merge_conflicts, which
            # tries three-way merge-file first (preserving both sides'
            # non-overlapping changes via the union-style result) and falls
            # back to the feature branch only on textual hunk overlap.
            # -X ours silently kept master's hunk on every conflict, which
            # invisibly clobbered the second of two parallel builds touching
            # the same file.
            merge_r = run_capture(
                ["git", "merge", instance.branch, "--no-ff",
                 "-m", f"Merge {instance.branch} ({instance.display_id()})"],
                cwd=repo,
            )

            auto_resolved = 0
            if merge_r.returncode != 0:
                detail = (merge_r.stderr or merge_r.stdout or "").strip()
                log.warning("Merge conflict for %s into %s: %s — attempting auto-resolve",
                            instance.branch, target, detail)
                try:
                    auto_resolved = self._auto_resolve_merge_conflicts(
                        repo, instance.branch, detail,
                    )
                except Exception:
                    log.warning("Auto-resolve raised unexpected error for %s",
                                instance.branch, exc_info=True)
                    auto_resolved = -1
                if auto_resolved < 0:
                    run_capture(["git", "merge", "--abort"], cwd=repo)
                    self._last_merge_failure_kind[instance.id] = (
                        self._classify_post_abort_state(repo)
                    )
                    log.error("Merge failed for %s into %s in %s: %s",
                              instance.branch, target, repo, detail)
                    stash_status = self._restore_stash(repo) if stashed else ""
                    # Surface precheck recovery_note even on failure — if Path B
                    # stashed local changes during recovery, the user must be
                    # told regardless of whether the subsequent merge worked.
                    recovery_suffix = f"\n{recovery_note}" if recovery_note else ""
                    return f"Merge failed: {detail}{stash_status}{recovery_suffix}"

            # --- Cleanup (reached on successful merge) ---

            # Remove worktree (--force handles uncommitted changes)
            if instance.worktree_path and Path(instance.worktree_path).exists():
                r = run_capture(
                    ["git", "worktree", "remove", instance.worktree_path, "--force"], cwd=repo,
                )
                if r.returncode != 0:
                    log.warning("git worktree remove failed for %s: %s",
                                instance.worktree_path, r.stderr.strip())
                    # Fallback: manual removal + prune
                    try:
                        shutil.rmtree(instance.worktree_path, ignore_errors=True)
                        run_capture(["git", "worktree", "prune"], cwd=repo)
                    except Exception:
                        pass

            # Delete branch (-d safe after merge; -D fallback if -d fails)
            r = run_capture(["git", "branch", "-d", instance.branch], cwd=repo)
            if r.returncode != 0:
                run_capture(["git", "branch", "-D", instance.branch], cwd=repo)

            # Clean up worktree project dir
            self._cleanup_worktree_session_dir(instance)

            # Deferred tag creation: the LLM committed `vX.Y.Z: Summary` but
            # did not run `git tag` in the worktree. Now that the merge
            # landed on master, walk the branch-unique window and tag the
            # release commit. Wrapped in defense-in-depth so a future bug
            # in the helper can't poison the merge push path.
            candidate_shas: list[str] = []
            tag_warning = ""
            tag_name: str | None = None
            try:
                tag_name, candidate_shas = self._tag_release_at(repo, "HEAD^2")
                tag_warning = self._stale_version_warning(repo, tag_name, "HEAD^2")
            except Exception:
                log.exception("Tag-release step raised in %s", repo)
                candidate_shas = []

            # Containment, which the version-number check above cannot see:
            # `target` is now the tree that ships, so the release before this
            # one has to be reachable from it. Checked *after* tagging so the
            # release we just cut is the ceiling rather than its own subject.
            tag_warning += self._release_containment_warning(repo, tag_name)

            # Push merged result to origin.  `has_remote` was probed by the
            # pre-merge sync block above.
            push_note = ""
            try:
                if not has_remote:
                    log.info("No remote 'origin' in %s — skipping push", repo)
                    push_note = "\nℹ️ No remote configured — local merge is fine"
                elif (push_r := run_capture(
                    ["git", "push", "origin", target], cwd=repo, timeout=30,
                )).returncode != 0:
                    push_detail = (push_r.stderr or push_r.stdout or "").strip()
                    log.error("Push to origin after merge in %s: %s",
                              repo, push_detail)
                    push_note = f"\nCould not push to origin (exit {push_r.returncode})"
                    if reason := git_fail_reason(push_detail):
                        push_note += f"\n`{reason}`"
                else:
                    log.info("Pushed %s to origin after merge in %s", target, repo)
                    # Push tags pointing at any commit in the branch-unique
                    # window. Covers the bot-created release tag plus any
                    # legacy LLM-self-tagged commits that may still exist
                    # on those shas.
                    try:
                        tag_names: list[str] = []
                        seen: set[str] = set()
                        for sha in candidate_shas:
                            tag_r = run_capture(["git", "tag", "--points-at", sha], cwd=repo)
                            if tag_r.returncode != 0:
                                log.debug(
                                    "git tag --points-at %s failed in %s (rc=%d), skipping",
                                    sha[:7], repo, tag_r.returncode,
                                )
                                continue
                            for name in (tag_r.stdout or "").splitlines():
                                name = name.strip()
                                if name and name not in seen:
                                    seen.add(name)
                                    tag_names.append(name)
                        if tag_names:
                            tag_push_r = run_capture(
                                ["git", "push", "origin"] + tag_names, cwd=repo, timeout=30,
                            )
                            if tag_push_r.returncode != 0:
                                tag_detail = (tag_push_r.stderr or tag_push_r.stdout or "").strip()
                                log.error("Tag push to origin in %s: %s", repo, tag_detail)
                                push_note += f"\nTags not pushed (exit {tag_push_r.returncode})"
                                if reason := git_fail_reason(tag_detail):
                                    push_note += f"\n`{reason}`"
                            else:
                                tag_list = ", ".join(tag_names)
                                log.info("Pushed tags [%s] to origin in %s", tag_list, repo)
                                push_note += f"\nTags pushed: {tag_list}"
                    except subprocess.TimeoutExpired:
                        log.error("Tag push to origin timed out (30s) in %s", repo)
                        push_note += "\nTag push timed out (30s)"
                    except Exception as e:
                        log.error("Tag push error in %s: %s", repo, e)
                        push_note += f"\nTag push error: {type(e).__name__}"
            except subprocess.TimeoutExpired:
                log.error("Push to origin timed out (30s) in %s", repo)
                push_note = "\nPush to origin timed out (30s)"
            except Exception as e:
                log.error("Push to origin error in %s: %s", repo, e)
                push_note = f"\nPush to origin error: {type(e).__name__}"

            instance.branch = None
            instance.worktree_path = None
            suffix = ""
            if auto_resolved > 0:
                suffix = f" (auto-resolved {auto_resolved} conflict{'s' if auto_resolved != 1 else ''})"
            dirty_note = ""
            if dirty_total:
                files_str = ", ".join(f"`{f}`" for f in dirty_files)
                if dirty_total > len(dirty_files):
                    dirty_note = (
                        f"\nℹ️ Stashed {dirty_total} uncommitted file(s) before merge "
                        f"(showing first {len(dirty_files)}): {files_str}"
                    )
                else:
                    dirty_note = (
                        f"\nℹ️ Stashed {dirty_total} uncommitted file(s) before merge: {files_str}"
                    )
            stash_status = self._restore_stash(repo) if stashed else ""
            merge_succeeded = True
            # Surface any recovery note from the precheck path (e.g. orphaned
            # index recovery stashed local changes) so we never silently
            # mutate the user's main repo without telling them.
            recovery_suffix = f"\n{recovery_note}" if recovery_note else ""
            return (
                f"Merged into {target}{suffix}{dirty_note}{sync_note}"
                f"{tag_warning}{push_note}{stash_status}{recovery_suffix}"
            )
        except subprocess.CalledProcessError as e:
            # Abort any in-progress merge to keep main repo clean for other sessions
            run_capture(["git", "merge", "--abort"], cwd=instance.repo_path)
            self._last_merge_failure_kind[instance.id] = (
                self._classify_post_abort_state(repo)
            )
            detail = (e.stderr or e.stdout or "").strip()
            log.error("Merge failed for %s into %s in %s: %s",
                      instance.branch, target, repo, detail)
            stash_status = self._restore_stash(repo) if stashed else ""
            # Same rationale as the auto_resolved<0 branch above — if Path B
            # stashed during recovery, surface it on failure too.
            recovery_suffix = f"\n{recovery_note}" if recovery_note else ""
            return f"Merge failed: {detail}{stash_status}{recovery_suffix}"
        finally:
            # Bug 2 fix: any non-CalledProcessError exception (OSError,
            # KeyboardInterrupt, SystemExit, etc.) raised after `git merge`
            # but before we recorded success would otherwise propagate
            # uncaught and leave MERGE_HEAD in main .git/, poisoning every
            # later session's view of the repo. Catch-all unconditional
            # cleanup. Idempotent — `git merge --abort` is a no-op when no
            # merge is active.
            _gd = git_dir(repo)
            _merge_head = (
                Path(_gd) / "MERGE_HEAD" if _gd
                else Path(repo) / ".git" / "MERGE_HEAD"
            )
            if not merge_succeeded and _merge_head.exists():
                try:
                    run_capture(["git", "merge", "--abort"], cwd=repo)
                    log.warning(
                        "Aborted leftover merge in %s during finally cleanup", repo,
                    )
                except Exception:
                    log.exception("git merge --abort raised during finally cleanup")

    def _unmerged_paths(self, repo: str) -> list[str]:
        """Tracked paths git still considers unmerged (conflict markers on disk).

        Used to verify a rollback rather than trust its exit code. Returns
        an empty list when the check itself cannot run — callers treat that
        as "nothing proven wrong", and the exit code they already have is
        the fallback signal.
        """
        try:
            r = run_capture(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo)
            if r.returncode != 0:
                return []
            return [ln for ln in r.stdout.splitlines() if ln.strip()]
        except (subprocess.SubprocessError, OSError):
            log.debug("unmerged-path check failed in %s", repo, exc_info=True)
            return []

    def _restore_stash(self, repo: str) -> str:
        """Pop the auto-stash if safe; return user-facing status string.

        Distinguishes five user-facing outcomes (icon shown is what the user sees;
        log severity is set independently per call site):
          - clean pop succeeded → ℹ️ "Stashed changes restored after merge"
          - skipped (tracked changes present) → ℹ️ "not auto-restored, recover manually"
          - pop conflicted, rolled back → "pop conflicted, aborted; stash preserved"
          - pop conflicted, rollback failed → "tree may contain conflict markers"
          - subprocess raised → "could not verify stash state"

        Only tracked changes block the pop. Untracked entries (`??`) are
        ignored — a stash pop only fails on them if there's a filename
        collision, and the conflict-rollback branch below handles that
        case (the pop fails atomically, `git checkout -- .` is a no-op
        on untracked paths, stash stays intact).

        Never claims "safe" when safety can't actually be verified — the
        rollback path tracks its own success explicitly.  All git
        subprocesses are wrapped so any unexpected failure surfaces a
        soft warning rather than propagating; the caller can still
        return its merge-result string with stash status appended.
        """
        try:
            check_r = run_capture(["git", "status", "--porcelain"], cwd=repo)
            # Narrow the dirty check to TRACKED changes only. Untracked
            # entries (`??`) don't block a stash pop unless there's an
            # actual filename collision — and if there is, the existing
            # pop-conflict rollback path below handles it cleanly. The old
            # behaviour (any porcelain output → skip pop) was the root cause
            # of the q-8035 incident: a foreign concurrent merge left a `??`
            # entry behind, this check refused the pop, and the user's
            # tracked edits got stranded in stash@{0}.
            tracked_dirty_lines = [
                line for line in check_r.stdout.splitlines()
                if line and not line.startswith("??")
            ]
            if tracked_dirty_lines:
                dirty = "\n".join(tracked_dirty_lines)
                log.warning(
                    "Skipping stash pop — tracked changes present in %s; stash preserved. "
                    "Dirty paths:\n%s",
                    repo,
                    dirty,
                )
                return (
                    "\nℹ️ Stashed changes not auto-restored (tracked changes present after merge). "
                    "Recover with `git stash pop` — list with `git stash list`."
                )
            pop_r = run_capture(["git", "stash", "pop"], cwd=repo)
            if pop_r.returncode != 0:
                # A conflicted pop leaves UNMERGED index entries, and
                # `git checkout -- .` refuses to touch those ("path ... is
                # unmerged") — so the old rollback failed on every single
                # occasion it was actually needed, leaving conflict markers
                # in every conflicted file. `reset --hard` is the operation
                # that clears an unmerged index, and it is safe *here*
                # specifically because the tracked-dirty check above already
                # established the tree carried no other tracked work: the
                # only changes it can discard are the ones the failed pop
                # just wrote. A conflicting pop never drops the stash, so
                # the user's work still survives in stash@{0} either way.
                rollback_ok = False
                try:
                    rb_r = run_capture(["git", "reset", "--hard", "HEAD"], cwd=repo)
                    rollback_ok = rb_r.returncode == 0
                    if not rollback_ok:
                        log.warning(
                            "Rollback `git reset --hard HEAD` returned rc=%d in %s: %s",
                            rb_r.returncode, repo,
                            (rb_r.stderr or rb_r.stdout or "").strip(),
                        )
                    else:
                        # Don't trust the exit code alone. The entire point
                        # of this branch is that a rollback can report
                        # success while leaving the repo unusable, and the
                        # cost of believing it is a repo that no longer
                        # imports. Confirm against the index itself.
                        leftover = self._unmerged_paths(repo)
                        if leftover:
                            rollback_ok = False
                            log.error(
                                "Rollback ran but %d path(s) are still unmerged in "
                                "%s: %s",
                                len(leftover), repo, ", ".join(leftover[:10]),
                            )
                except (subprocess.SubprocessError, OSError):
                    log.warning(
                        "Rollback `git reset --hard HEAD` raised in %s",
                        repo, exc_info=True,
                    )
                    rollback_ok = False

                if rollback_ok:
                    log.warning(
                        "Stash pop conflicted in %s, rolled back; stash preserved",
                        repo,
                    )
                    return (
                        "\nStash pop conflicted — aborted. "
                        "Your changes are safe in `stash@{0}`."
                    )
                else:
                    log.error(
                        "Stash pop conflicted AND rollback failed in %s — "
                        "tree contains conflict markers and the repo will not "
                        "run until it is repaired",
                        repo,
                    )
                    return (
                        f"\n{REPO_UNUSABLE_MARKER} — the stash pop conflicted and "
                        "could not be undone, so tracked files still contain "
                        "conflict markers. Code in this repo will not import "
                        "until it is repaired. Your pre-merge work is safe in "
                        "`stash@{0}`. Repair with `git reset --hard HEAD`, then "
                        "re-apply the stash."
                    )
            # Clean pop — confirm restoration so the user never has to guess
            # where their pre-merge work lives.
            log.info("Restored stashed changes after merge in %s", repo)
            return "\nℹ️ Stashed changes restored after merge."
        except (subprocess.SubprocessError, OSError):
            log.warning(
                "Stash restore subprocess failed in %s",
                repo, exc_info=True,
            )
            return (
                "\nCould not verify stash state — check `git stash list` manually."
            )

    def _auto_resolve_merge_conflicts(
        self, repo: str, branch: str, detail: str,
    ) -> int:
        """Auto-resolve merge conflicts by preferring the feature branch.

        Called when a merge is in-progress with unresolved conflicts.
        For UU (both-modified) files, attempts a three-way merge-file first
        to preserve both sides' changes. Falls back to --theirs for remaining
        conflicts. Returns number of resolved files, or -1 on failure.

        All pathspec-consuming commands run from the git toplevel, NOT the
        registered repo dir: ``status --porcelain`` paths are
        toplevel-relative, so resolving them from a subdirectory-registered
        repo missed every file ("pathspec did not match") and auto-resolve
        never once succeeded before this fix.
        """
        toplevel = git_toplevel(repo) or repo
        # -z: NUL-separated, paths never C-quoted — porcelain v1 lines quote
        # paths with non-ASCII/special chars, which would feed literal
        # quotes into the pathspecs below and miss the file.  utf-8 matches
        # git's on-disk path encoding so the round-trip survives non-ASCII.
        status_r = run_capture(
            ["git", "status", "--porcelain", "-z"],
            cwd=toplevel, encoding="utf-8", errors="replace",
        )

        conflicts: list[tuple[str, str]] = []
        entries = (status_r.stdout or "").split("\0")
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4 or entry[2] != " ":
                continue
            code = entry[:2]
            filepath = entry[3:]
            if code[0] in ("R", "C"):
                # Rename/copy entries carry the original path as an extra
                # NUL field — consume it so it isn't parsed as an entry.
                i += 1
            if code in ("UU", "AA", "DU", "UD", "DD", "AU", "UA"):
                conflicts.append((code, filepath))

        if not conflicts:
            log.warning("Merge failed but no conflicted files for %s: %s",
                        branch, detail)
            return -1

        log.info("Auto-resolving %d conflict(s) for %s: %s",
                 len(conflicts), branch,
                 ", ".join(f"{c}:{f}" for c, f in conflicts))

        for code, filepath in conflicts:
            if code in ("UD", "DD"):
                # Feature branch deleted — accept deletion
                r = run_capture(["git", "rm", "--", filepath], cwd=toplevel)
                if r.returncode != 0:
                    log.warning("git rm failed for %s: %s",
                                filepath, r.stderr.strip())
                    return -1
            elif code == "UU":
                # Both modified — try three-way merge-file to keep both sides
                if not self._try_merge_file(toplevel, filepath):
                    # Fallback: accept feature branch version
                    if not self._checkout_theirs(toplevel, filepath):
                        return -1
            else:
                # AA, AU, UA, DU — accept feature branch version
                if not self._checkout_theirs(toplevel, filepath):
                    return -1

        commit_r = run_capture(["git", "commit", "--no-edit"], cwd=toplevel)
        if commit_r.returncode != 0:
            log.warning("Failed to commit auto-resolved merge for %s: %s",
                        branch, commit_r.stderr.strip())
            return -1

        log.info("Auto-resolved merge for %s completed successfully", branch)
        return len(conflicts)

    def _try_merge_file(self, toplevel: str, filepath: str) -> bool:
        """Attempt three-way merge-file for a UU conflict.

        Extracts base/ours/theirs from index stages, runs git merge-file,
        and writes the result back if successful. Returns True on success.

        ``toplevel`` must be the git working-tree root — ``filepath`` comes
        from porcelain output and is toplevel-relative.
        """
        tmp_base = tmp_ours = tmp_theirs = None
        try:
            # Extract the three stages into temp files (Windows-safe)
            tmp_base = tempfile.NamedTemporaryFile(
                delete=False, suffix=".base", prefix="merge_")
            tmp_ours = tempfile.NamedTemporaryFile(
                delete=False, suffix=".ours", prefix="merge_")
            tmp_theirs = tempfile.NamedTemporaryFile(
                delete=False, suffix=".theirs", prefix="merge_")
            # Close handles before git writes to them (Windows requirement)
            tmp_base.close()
            tmp_ours.close()
            tmp_theirs.close()

            stages = [
                (":1:" + filepath, tmp_base.name),   # base
                (":2:" + filepath, tmp_ours.name),    # ours
                (":3:" + filepath, tmp_theirs.name),  # theirs
            ]
            for ref, dest in stages:
                r = subprocess.run(
                    ["git", "show", ref],
                    cwd=toplevel, capture_output=True, **_NOWND,
                )
                if r.returncode != 0:
                    log.debug("Could not extract %s for merge-file", ref)
                    return False
                Path(dest).write_bytes(r.stdout)

            # Run merge-file: exit 0 = clean, 1 = conflicts, <0 = error
            mf = subprocess.run(
                ["git", "merge-file", "-p",
                 tmp_ours.name, tmp_base.name, tmp_theirs.name],
                capture_output=True, **_NOWND,
            )

            if mf.returncode < 0:
                # Signal/error — output is garbage
                log.debug("merge-file crashed for %s (rc=%d)", filepath, mf.returncode)
                return False

            if mf.returncode == 0:
                # Clean merge — write result back
                target = Path(toplevel) / filepath
                target.write_bytes(mf.stdout)
                run_capture(["git", "add", "--", filepath], cwd=toplevel)
                log.info("merge-file resolved %s cleanly", filepath)
                return True

            # returncode 1 = still has conflicts, fall back to --theirs
            log.debug("merge-file had remaining conflicts for %s, falling back", filepath)
            return False

        except Exception:
            log.debug("merge-file failed for %s", filepath, exc_info=True)
            return False
        finally:
            for tmp in (tmp_base, tmp_ours, tmp_theirs):
                if tmp is not None:
                    try:
                        os.unlink(tmp.name)
                    except OSError:
                        pass

    def _checkout_theirs(self, toplevel: str, filepath: str) -> bool:
        """Resolve a conflict by accepting the feature branch version.

        ``toplevel`` must be the git working-tree root — ``filepath`` is
        toplevel-relative porcelain output.
        """
        r1 = run_capture(["git", "checkout", "--theirs", "--", filepath], cwd=toplevel)
        if r1.returncode != 0:
            log.warning("checkout --theirs failed for %s: %s",
                        filepath, r1.stderr.strip())
            return False
        r2 = run_capture(["git", "add", "--", filepath], cwd=toplevel)
        if r2.returncode != 0:
            log.warning("git add failed for %s: %s",
                        filepath, r2.stderr.strip())
            return False
        return True

    async def auto_commit_dirty_worktree(
        self, instance: Instance,
    ) -> str | None:
        """Commit any dirty changes in the instance's worktree in place.

        Unlike `discard_branch(preserve_if_dirty=True)` this leaves the
        worktree intact so the chain (review/verify/commit/done) can keep
        running on it. Returns the commit SHA on success, or None if the
        worktree is clean / missing / commit failed. Used by the autopilot
        no-commits guard to silently rescue builds where the agent wrote
        files but forgot to run `git commit`, instead of halting the chain.

        Runs under the per-repo lock: on t-5592 the `git status` here came
        back clean/failing while a release chain was mutating the same
        repo's shared gitdir — one second later the (locked) discard saw
        the very same worktree dirty and WIP-preserved it. Lock-safety
        audit: both call sites (the autopilot no-commit guards in
        workflows.py) hold no repo lock themselves, and the discard_branch
        that may follow on rescue failure acquires the lock only after
        this helper has returned — sequential, never nested, no
        re-entrancy hazard.
        """
        if not instance.worktree_path:
            log.warning(
                "auto_commit_dirty_worktree: %s has no worktree_path — "
                "nothing to rescue", instance.id,
            )
            return None
        wt = instance.worktree_path
        if not Path(wt).exists():
            log.warning(
                "auto_commit_dirty_worktree: worktree %s missing for %s — "
                "nothing to rescue", wt, instance.id,
            )
            return None
        if instance.repo_path:
            repo_lock = self._get_repo_lock(instance.repo_path)
            async with repo_lock:
                return await asyncio.to_thread(
                    self._auto_commit_dirty_worktree_sync, instance,
                )
        log.warning(
            "auto_commit_dirty_worktree: %s has no repo_path — rescuing "
            "without the per-repo lock", instance.id,
        )
        return await asyncio.to_thread(self._auto_commit_dirty_worktree_sync, instance)

    def _auto_commit_dirty_worktree_sync(self, instance: Instance) -> str | None:
        wt = instance.worktree_path
        # Transient-race guard: retry once after a short pause when status
        # fails or reports clean — t-5592's rescue bailed on exactly one of
        # these previously-silent exits while the worktree was demonstrably
        # dirty seconds later.
        status = None
        for attempt in (1, 2):
            status = run_capture(["git", "status", "--porcelain"], cwd=wt)
            if status.returncode == 0 and status.stdout.strip():
                break
            log.warning(
                "auto_commit_dirty_worktree: status attempt %d for %s: "
                "rc=%d, dirty=%s, stderr=%s",
                attempt, wt, status.returncode,
                bool(status.stdout.strip()),
                (status.stderr or "").strip() or "(none)",
            )
            if attempt == 1:
                time.sleep(2.0)
        if status.returncode != 0 or not status.stdout.strip():
            log.warning(
                "auto_commit_dirty_worktree: giving up for %s — worktree "
                "clean or status failing after retry", instance.id,
            )
            return None
        add_r = run_capture(["git", "add", "-A"], cwd=wt)
        if add_r.returncode != 0:
            log.warning(
                "auto_commit_dirty_worktree: git add failed for %s: %s",
                wt, add_r.stderr.strip(),
            )
            return None
        commit_msg = (
            f"build: auto-commit dirty worktree ([{instance.id}])\n\n"
            f"Build agent wrote files but didn't run `git commit` before "
            f"finishing. The autopilot guard committed the changes so the "
            f"chain could continue."
        )
        commit_r = run_capture(["git", "commit", "-m", commit_msg, "--no-verify"], cwd=wt)
        if commit_r.returncode != 0:
            log.warning(
                "auto_commit_dirty_worktree: git commit failed for %s: %s",
                wt, commit_r.stderr.strip(),
            )
            return None
        sha = ref_sha(wt) or "(unknown)"
        log.info(
            "auto_commit_dirty_worktree: rescued %s with commit %s on %s",
            instance.id, sha[:8], instance.branch,
        )
        return sha

    async def discard_branch(
        self, instance: Instance, *, preserve_if_dirty: bool = False,
    ) -> DiscardOutcome:
        """Delete worktree and branch without merging.

        When `preserve_if_dirty` is True and the worktree has uncommitted
        changes, the changes are first rolled into a WIP commit on the
        branch and only the worktree is removed — the branch survives so
        the user can recover with `git checkout <branch>`. This is the
        safety net for the autopilot no-commits halt path, where the build
        agent forgot to commit and we'd otherwise destroy its work via
        `git worktree remove --force`.
        """
        if not instance.branch or not instance.original_branch:
            if instance.original_branch and not instance.branch:
                return DiscardOutcome(
                    f"Already discarded ({config.BRANCH_PREFIX}/{instance.id})"
                )
            return DiscardOutcome("No branch to discard", failed=True)
        if not instance.repo_path:
            return DiscardOutcome("No repo path", failed=True)
        repo_lock = self._get_repo_lock(instance.repo_path)
        async with repo_lock:
            return await asyncio.to_thread(
                self._discard_branch_sync, instance,
                preserve_if_dirty,
            )

    def _discard_branch_sync(
        self, instance: Instance, preserve_if_dirty: bool = False,
    ) -> DiscardOutcome:
        repo = instance.repo_path
        errors: list[str] = []
        preserved_branch: str | None = None
        # Bug 2 pre-flight: discard touches the main repo (worktree remove,
        # branch -D). A leftover MERGE_HEAD from a sibling session can make
        # those operations misbehave or attribute the residue to this
        # session's user. Auto-recover or surface an explicit error.
        precheck = self._check_main_repo_clean(repo)
        if precheck.error:
            return DiscardOutcome(f"Discard skipped: {precheck.error}", failed=True)
        recovery_note = precheck.recovery_note

        worktree_path = instance.worktree_path
        wt_exists = bool(worktree_path) and Path(worktree_path).exists()

        # Optional safety net: if the caller asked us to preserve dirty
        # work AND the worktree has uncommitted changes, roll them into a
        # WIP commit so the branch lives on. Without this, the
        # `worktree remove --force` below either silently auto-stashes
        # (orphaned, only fsck-recoverable) or destroys the changes
        # outright depending on git's mood.
        if preserve_if_dirty and wt_exists:
            status = run_capture(["git", "status", "--porcelain"], cwd=worktree_path)
            if status.returncode == 0 and status.stdout.strip():
                add_r = run_capture(["git", "add", "-A"], cwd=worktree_path)
                if add_r.returncode == 0:
                    commit_msg = (
                        f"WIP: build halted with uncommitted changes "
                        f"({instance.id})"
                    )
                    commit_r = run_capture(
                        ["git", "commit", "-m", commit_msg, "--no-verify"], cwd=worktree_path,
                    )
                    if commit_r.returncode == 0:
                        preserved_branch = instance.branch
                        log.info(
                            "Preserved dirty worktree as WIP commit on %s "
                            "(instance %s)",
                            instance.branch, instance.id,
                        )
                    else:
                        log.warning(
                            "WIP commit failed for %s, falling back to "
                            "force-discard: %s",
                            instance.branch, commit_r.stderr.strip(),
                        )
                else:
                    log.warning(
                        "git add -A failed for %s before WIP commit: %s",
                        worktree_path, add_r.stderr.strip(),
                    )

        # Release tags that live only on this branch, read BEFORE the branch
        # ref is deleted. Deleting a branch does not delete its tags, it
        # strands them: the version keeps printing in `git tag` and looks
        # shipped while its content is reachable from nothing, and after the
        # delete there is no cheap way back to "which branch was that".
        # Naming it here is the only chance the user gets to notice.
        #
        # Both refs must be resolvable or the filters mean the opposite of
        # what is being asked: `version_tags` with no `merged=` lists every
        # release in the repo, and blaming one discard for all of them is
        # worse than saying nothing. The public `discard_branch` already
        # refuses an instance missing either, so this only costs a comparison.
        stranded: list[tuple[str, tuple[int, int, int, int]]] = []
        if (
            config.RELEASE_ANCESTRY_CHECK and not preserved_branch
            and instance.branch and instance.original_branch
        ):
            try:
                stranded = version_tags(
                    repo, merged=instance.branch, no_merged=instance.original_branch,
                )
            except Exception:
                log.exception("Stranded-release scan raised in %s", repo)
                stranded = []

        # Each cleanup step is independent — continue on failure
        if wt_exists:
            r = run_capture(["git", "worktree", "remove", worktree_path, "--force"], cwd=repo)
            if r.returncode != 0:
                log.warning("Failed to remove worktree %s: %s", worktree_path, r.stderr.strip())
                errors.append(f"worktree remove: {r.stderr.strip()}")

        # Skip branch deletion when we just preserved a WIP commit on it —
        # that's the whole point of preservation.
        branch_deleted = False
        if not preserved_branch:
            r = run_capture(["git", "branch", "-D", instance.branch], cwd=repo)
            if r.returncode == 0:
                branch_deleted = True
            elif "not found" in r.stderr:
                # Already gone (deleted by hand): nothing left to discard.
                branch_deleted = True
            else:
                log.warning("Failed to delete branch %s: %s", instance.branch, r.stderr.strip())
                errors.append(f"branch delete: {r.stderr.strip()}")

        # Worded only once the ref is actually gone. A failed `branch -D`
        # leaves those commits perfectly reachable, and telling the user a
        # release was stranded when it was not sends them hunting for a
        # problem they do not have.
        stranded_note = ""
        if stranded and branch_deleted:
            names = ", ".join(f"`{n}`" for n, _ in stranded[:5])
            more = f" (+{len(stranded) - 5} more)" if len(stranded) > 5 else ""
            log.warning(
                "Discarding %s stranded release tag(s) %s in %s",
                instance.branch, [n for n, _ in stranded], repo,
            )
            stranded_note = (
                f"\n⚠️ Release tag(s) {names}{more} existed only on this branch "
                f"and are now unreachable from {instance.original_branch}. "
                f"Delete them (`git tag -d <name>`) or the version looks shipped."
            )

        self._cleanup_worktree_session_dir(instance)

        # Clear worktree_path always (it's gone). Clear branch only when we
        # actually deleted it; otherwise the surviving branch (a WIP commit,
        # or one git refused to delete) stays tracked on the instance so
        # Merge/Discard still have something to act on.
        instance.worktree_path = None
        if branch_deleted:
            instance.branch = None

        # Surface any Path B recovery note so a precheck-stashed change set is
        # never invisible to the user.
        recovery_suffix = f"\n{recovery_note}" if recovery_note else ""
        if preserved_branch:
            return DiscardOutcome(
                f"Preserved uncommitted work on `{preserved_branch}` (WIP commit). "
                f"Recover: `git checkout {preserved_branch}`{recovery_suffix}",
                preserved_branch=preserved_branch,
            )
        if errors:
            return DiscardOutcome(
                f"Discarded (with warnings: {'; '.join(errors)})"
                f"{recovery_suffix}{stranded_note}",
                failed=not branch_deleted,
            )
        return DiscardOutcome(
            f"Discarded branch, back on {instance.original_branch}"
            f"{recovery_suffix}{stranded_note}",
        )
