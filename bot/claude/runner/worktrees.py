"""Build worktrees: creation, recovery and orphan cleanup."""

from __future__ import annotations

import asyncio
import json as _json_mod
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from bot import config
from bot.textutil import parse_iso_utc
from bot.procutil import run_capture
from bot.claude.branch_utils import canonical_branch, clear_stale_branches
from bot.claude.gitpaths import git_common_dir
from bot.claude.provider import ProviderConfig
from bot.claude.types import (
    Instance, InstanceOrigin, InstanceStatus, merge_msg_is_failure,
)

log = logging.getLogger(__name__)

# The hook scripts live in bot/claude/hooks/, one level above this package.
# Resolving next to this file broke silently when runner.py became a package.
_HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"


class WorktreeRecoveryEvent(NamedTuple):
    """Result of a single worktree-recovery decision at startup.

    `status` is one of:
      - "recovered": metadata was missing, content matched branch tip,
        the registration was rebuilt (``_reregister_worktree_sync``).
      - "manual_recovery_needed": metadata missing AND working-tree content
        had drifted from branch tip — refused to overwrite, parked the
        instance for human review (``manual_recovery_needed=True`` set on
        the Instance dataclass).
      - "skipped": couldn't decide (git error, missing dir, etc.); recovery
        deferred. The instance is left alone so the user can investigate.
    """
    instance_id: str
    repo_name: str
    branch: str
    status: str  # "recovered" | "manual_recovery_needed" | "skipped"
    detail: str = ""


class ReleaseRecoveryEvent(NamedTuple):
    """A non-terminal release/done instance whose worktree carries a
    release-shaped commit (``vX.Y.Z: ...``) that has no matching git tag.

    Surfaced at startup so the user knows a release session was interrupted
    after the version bump but before the tag was created — letting another
    release pick the same version would re-collide.
    """
    instance_id: str
    repo_name: str
    branch: str | None
    commit_sha: str
    bumped_version: str  # e.g. "v1.2.3" — what the commit message claimed
    detail: str = ""


class WorktreeMixin:
    """Build worktrees: creation, recovery and orphan cleanup. Mixed into ClaudeRunner."""

    # --- Git worktree management ---

    async def _ensure_worktree(self, instance: Instance, provider: ProviderConfig | None = None) -> None:
        """Create a git worktree for build isolation (idempotent)."""
        if not instance.repo_path:
            return
        # If worktree already exists (copy_branch from parent), skip creation
        if instance.worktree_path and Path(instance.worktree_path).is_dir():
            return
        # A recorded worktree_path means the branch was created by an earlier
        # step. Its directory being gone means that step's worktree was
        # cleaned up, usually by a merge or discard.
        inherited = bool(instance.worktree_path)
        if inherited:
            log.warning("Worktree %s no longer exists for %s",
                        instance.worktree_path, instance.id)
            instance.worktree_path = None
        repo_lock = self._get_repo_lock(instance.repo_path)
        async with repo_lock:
            # Decided under the lock, not before it. A merge holds this lock
            # for its whole run and clears the branch off every instance that
            # shares it when it finishes (clear_stale_branches), so a Done
            # tapped mid-merge used to wake up here with branch=None and die
            # inside `git worktree add -b None` (t-8954).
            gone = await asyncio.to_thread(
                self._worktree_branch_gone, instance, inherited,
            )
            if gone:
                log.info(
                    "%s, branch already merged or discarded, running %s in "
                    "main repo", gone, instance.id,
                )
                instance.branch = None
                instance.worktree_path = None
                return
            await asyncio.to_thread(
                self._create_worktree_sync, instance, provider,
                existing_branch=inherited,
            )

    def _worktree_branch_gone(self, instance: Instance, inherited: bool) -> str | None:
        """Say why this run can no longer use its branch, or None if it can.

        Gone means cleared off the instance while we waited, or (for an
        inherited branch only) its ref deleted. A fresh build's ref not
        existing yet is normal: _create_worktree_sync is about to make it.
        A git read that cannot answer is not evidence the branch is gone, so
        it falls through to creation, which fails loudly if it really is.
        """
        if not instance.branch:
            return "Branch cleared while waiting for the repo lock"
        if not inherited:
            return None
        try:
            ref = run_capture(
                ["git", "rev-parse", "--verify", "--quiet",
                 f"refs/heads/{instance.branch}"],
                cwd=instance.repo_path, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            log.warning("Could not check branch %s for %s",
                        instance.branch, instance.id, exc_info=True)
            return None
        if ref.returncode != 0:
            return f"Branch {instance.branch} no longer exists"
        return None

    def _create_worktree_sync(
        self, instance: Instance, provider: ProviderConfig | None = None,
        existing_branch: bool = False,
    ) -> None:
        provider = provider or self.provider
        repo = instance.repo_path
        wt_dir = str(Path(repo) / ".worktrees" / instance.id)
        branch = instance.branch
        if not branch:
            raise RuntimeError(
                f"Cannot create a worktree for {instance.id}: it has no branch"
            )
        default_branch = self._get_default_branch(repo)

        # Idempotent: skip if worktree already exists
        if Path(wt_dir).is_dir():
            instance.worktree_path = wt_dir
            instance.original_branch = default_branch
            return

        try:
            # Create .worktrees/ parent if needed
            Path(wt_dir).parent.mkdir(parents=True, exist_ok=True)

            if existing_branch:
                # The branch was made by an earlier step and carries its work.
                # Never fall back to -b here: if the ref is gone, a new branch
                # off HEAD would silently stand in for the lost one.
                run_capture(["git", "worktree", "add", wt_dir, branch], cwd=repo, check=True)
            else:
                # Create worktree with a new branch from current HEAD (master/main)
                result = run_capture(["git", "worktree", "add", wt_dir, "-b", branch], cwd=repo)
                if result.returncode != 0:
                    # Branch might already exist (retry/resume), try without -b
                    run_capture(["git", "worktree", "add", wt_dir, branch], cwd=repo, check=True)

            instance.worktree_path = wt_dir
            instance.original_branch = default_branch

            # Copy provider config dir into worktree so CLI finds
            # instruction files and project settings (worktrees have a .git
            # file, not directory — some tools may not follow it correctly)
            cfg_dir = provider.config_dir_name
            try:
                src_cfg_dir = Path(repo) / cfg_dir
                dst_cfg_dir = Path(wt_dir) / cfg_dir
                if src_cfg_dir.is_dir() and not dst_cfg_dir.exists():
                    shutil.copytree(str(src_cfg_dir), str(dst_cfg_dir),
                                    ignore=shutil.ignore_patterns("*.jsonl"))
                    log.debug("Copied %s/ into worktree %s", cfg_dir, wt_dir)
            except Exception:
                log.warning("Failed to copy %s/ into worktree %s", cfg_dir, wt_dir, exc_info=True)

            # t-3541 Layer 3: install per-worktree PreToolUse hook config.
            # No-op unless WORKTREE_HOOK_ENABLED is set (default off — must
            # stay off until Probe B Part 1 confirms hooks load under the
            # bot's CLAUDE_CONFIG_DIR setup; see config.py for procedure).
            # Runs inside the per-repo lock already held by the caller
            # (_ensure_worktree), so .git/info/exclude appends are
            # serialized across parallel build creations on the same repo.
            if config.WORKTREE_HOOK_ENABLED:
                try:
                    self._install_worktree_hook(repo, wt_dir)
                except Exception:
                    log.warning(
                        "Worktree hook install failed for %s — Layer 3 "
                        "enforcement will be missing for this build "
                        "(Layers 1, 2, 4 still active)",
                        wt_dir, exc_info=True,
                    )

            log.info("Created worktree %s (branch %s) in %s", wt_dir, branch, repo)
        except subprocess.CalledProcessError as e:
            log.error("Failed to create worktree: %s", e.stderr)
            raise RuntimeError(f"Failed to create worktree: {e.stderr}")

    @staticmethod
    def _install_worktree_hook(repo_path: str, wt_dir: str) -> None:
        """Install the per-worktree hooks (main-repo guard + test mutex).

        Writes ``.claude/settings.local.json`` inside the worktree pointing
        at the bot's hook scripts (absolute paths so CWD doesn't matter),
        and appends the settings file to ``.git/info/exclude`` in the
        common gitdir so it can never ride into the build branch and
        merge back into the repo's default branch.

        Guard hook (worktree_guard.py, PreToolUse) covers:
          - Bash: git -C <repo>, --git-dir/--work-tree <repo>, rm -r <repo>,
            git worktree remove <self>
          - Edit/Write/MultiEdit/NotebookEdit: any file_path/notebook_path
            under the main repo but not under the worktree (path-poisoning
            from a resumed planning session)

        Test-mutex hook (test_mutex.py, gated on TEST_MUTEX_ENABLED):
          - Bash PreToolUse: acquire the per-repo test-suite lock (or
            wait/block) when the command is a full test-suite run
          - Bash PostToolUse: release the lock when the run finishes

        Schema is the canonical Claude Code form:
          {"hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
                                     "hooks": [{"type": "command",
                                                "command": "...",
                                                "timeout": N}]}]}}

        Worktree path and repo path are passed as positional argv to the
        guard script (Claude Code does not propagate `env` blocks from
        settings to hook subprocesses reliably; argv is unambiguous).

        Settings load only when Claude is invoked with
        ``--setting-sources user,project,local`` — the bot adds that flag
        in the Claude provider's build_command.

        Caller MUST hold the per-repo asyncio git-admin lock (the only
        callsite, _create_worktree_sync, runs under that lock). Without
        the lock, parallel builds would race on the exclude append and
        on the deep-merge into an existing settings file.

        Deep-merges into any pre-existing settings.local.json so a project
        that ships its own hooks isn't clobbered. The bot's hook entry is
        identifiable by an `_owner: "claude-telegram-bot"` marker so we can
        replace it on re-install instead of stacking duplicates.
        """
        hook_script = _HOOKS_DIR / "worktree_guard.py"
        if not hook_script.is_file():
            log.warning("worktree_guard.py not found at %s", hook_script)
            return

        # Use the same Python that's running the bot.
        python_exe = sys.executable or "python"
        # Quote each piece individually so paths with spaces work on Windows.
        cmd_str = (
            f'"{python_exe}" "{hook_script}" "{wt_dir}" "{repo_path}"'
        )
        bot_hook_entry = {
            "matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
            "_owner": "claude-telegram-bot",
            "hooks": [
                {
                    "type": "command",
                    "command": cmd_str,
                    "timeout": 10,
                }
            ],
        }

        # Test-suite mutex (t-5976): serialize full test-suite runs across
        # parallel sessions on the same repo (they share ports, databases,
        # and CPU — worktrees only isolate files). Pre-phase acquires or
        # blocks; post-phase releases. The pre timeout must exceed the
        # script's internal 45s wait so the script, not the hook reaper,
        # decides the outcome (the wait itself sits under STALL_TIMEOUT_SECS
        # so a waiting session never trips the stall-warning embed).
        mutex_entries: list[tuple[str, dict]] = []
        if config.TEST_MUTEX_ENABLED:
            mutex_script = _HOOKS_DIR / "test_mutex.py"
            if mutex_script.is_file():
                for event, phase, timeout in (
                    ("PreToolUse", "pre", 90),
                    ("PostToolUse", "post", 15),
                ):
                    mutex_entries.append((event, {
                        "matcher": "Bash",
                        "_owner": "claude-telegram-bot-test-mutex",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    f'"{python_exe}" "{mutex_script}" {phase} '
                                    f'"{wt_dir}" "{repo_path}"'
                                ),
                                "timeout": timeout,
                            }
                        ],
                    }))
            else:
                log.warning("test_mutex.py not found at %s", mutex_script)

        settings_dir = Path(wt_dir) / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings_path = settings_dir / "settings.local.json"

        # Deep-merge with any existing settings — preserve project's own
        # hooks/permissions, replace only our own entry (matched by _owner).
        existing: dict = {}
        if settings_path.exists():
            try:
                existing = _json_mod.loads(
                    settings_path.read_text(encoding="utf-8")
                )
                if not isinstance(existing, dict):
                    log.warning(
                        "Worktree %s settings.local.json is not an object; "
                        "leaving it untouched (skipping hook install)",
                        wt_dir,
                    )
                    return
            except (OSError, ValueError) as e:
                log.warning(
                    "Could not parse %s (%s); leaving untouched",
                    settings_path, e,
                )
                return

        hooks_block = existing.setdefault("hooks", {})
        if not isinstance(hooks_block, dict):
            log.warning(
                "Worktree %s settings.local.json `hooks` is not an object; "
                "leaving it untouched", wt_dir,
            )
            return
        # Group the bot's entries by hook event, then for each event drop
        # any prior bot-owned entries (all `_owner` values share the
        # "claude-telegram-bot" prefix) and append the fresh ones.
        # PostToolUse starts present-but-empty so a re-install with the
        # test mutex flipped OFF sweeps stale mutex entries instead of
        # leaving them active.
        new_entries: dict[str, list[dict]] = {
            "PreToolUse": [bot_hook_entry],
            "PostToolUse": [],
        }
        for event, entry in mutex_entries:
            new_entries.setdefault(event, []).append(entry)

        def _bot_owned(e: object) -> bool:
            return isinstance(e, dict) and str(
                e.get("_owner", "")
            ).startswith("claude-telegram-bot")

        for event, entries in new_entries.items():
            ev_list = hooks_block.setdefault(event, [])
            if not isinstance(ev_list, list):
                log.warning(
                    "Worktree %s settings.local.json `%s` is not an "
                    "array; leaving it untouched", wt_dir, event,
                )
                return
            ev_list[:] = [e for e in ev_list if not _bot_owned(e)]
            ev_list.extend(entries)

        settings_path.write_text(
            _json_mod.dumps(existing, indent=2),
            encoding="utf-8",
        )

        # Append the settings file path to .git/info/exclude so a stray
        # `git add -A` in the worktree can't stage it onto the build branch.
        exclude_line = ".claude/settings.local.json"
        try:
            r = run_capture(["git", "rev-parse", "--git-common-dir"], cwd=repo_path, check=True)
            common_dir = r.stdout.strip()
        except subprocess.CalledProcessError:
            log.warning(
                "Could not resolve git common dir for %s; skipping "
                ".git/info/exclude append",
                repo_path,
            )
            return
        if not common_dir:
            log.warning(
                "git rev-parse --git-common-dir returned empty for %s; "
                "skipping .git/info/exclude append",
                repo_path,
            )
            return
        common_path = Path(common_dir)
        if not common_path.is_absolute():
            common_path = (Path(repo_path) / common_path).resolve()
        info_dir = common_path / "info"
        info_dir.mkdir(parents=True, exist_ok=True)
        exclude_path = info_dir / "exclude"
        existing_excl = ""
        if exclude_path.exists():
            try:
                existing_excl = exclude_path.read_text(encoding="utf-8")
            except OSError:
                existing_excl = ""
        if any(
            line.strip() == exclude_line
            for line in existing_excl.splitlines()
        ):
            return
        addendum = ""
        if existing_excl and not existing_excl.endswith("\n"):
            addendum += "\n"
        addendum += f"{exclude_line}\n"
        with exclude_path.open("a", encoding="utf-8") as f:
            f.write(addendum)
        log.info(
            "Installed worktree hook for %s; excluded %s in %s",
            wt_dir, exclude_line, exclude_path,
        )

    @staticmethod
    def _release_test_mutex(
        repo_path: str | None, worktree_path: str | None,
    ) -> None:
        """Free the per-repo test-suite mutex if *worktree_path* holds it.

        Backstop for the PostToolUse release in hooks/test_mutex.py:
        called from the post-run path once a session's CLI process has
        exited, so a session killed or crashed mid-test-run frees the
        lock immediately instead of stalling siblings until the hook's
        stale TTL. No-op when the lock is free or held by someone else.
        """
        if not repo_path or not worktree_path:
            return
        lock_dir = Path(repo_path) / ".worktrees" / ".test-mutex"
        try:
            data = _json_mod.loads(
                (lock_dir / "owner.json").read_text(encoding="utf-8")
            )
            holder = str(data.get("worktree", ""))
        except (OSError, ValueError):
            return

        def _norm(p: str) -> str:
            # Case-fold only on Windows — mirrors _norm in hooks/test_mutex.py
            # so both sides agree on what "same worktree" means.
            p = p.replace("\\", "/").rstrip("/")
            return p.lower() if os.name == "nt" else p

        if holder and _norm(holder) == _norm(str(worktree_path)):
            shutil.rmtree(lock_dir, ignore_errors=True)
            log.info(
                "Released test-suite mutex for %s (was held by %s)",
                repo_path, worktree_path,
            )

    @staticmethod
    def _get_default_branch(repo_path: str) -> str:
        """Determine the default branch (master or main)."""
        for candidate in ("master", "main"):
            r = run_capture(
                ["git", "rev-parse", "--verify", f"refs/heads/{candidate}"], cwd=repo_path,
            )
            if r.returncode == 0:
                return candidate
        # Fallback: use HEAD if it's not a bot-managed branch
        _prefix = f"{config.BRANCH_PREFIX}/"
        r = run_capture(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo_path)
        if r.returncode == 0:
            head = r.stdout.strip()
            if head and not head.startswith(_prefix):
                return head
        # Last resort: find any non-bot-managed branch
        r = run_capture(["git", "branch", "--format=%(refname:short)"], cwd=repo_path)
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                branch = line.strip()
                if branch and not branch.startswith(_prefix):
                    return branch
        log.warning("No default branch in %s — every branch is bot-managed",
                     repo_path)
        return "master"

    # --- Divergence safety check (used by startup auto-merge) ---

    @staticmethod
    def _check_branch_divergence(
        repo: str, branch: str, target: str,
    ) -> tuple[str, str]:
        """Decide whether `branch` is safe to auto-merge into `target`.

        Returns (decision, reason) where decision is one of:
          - "ok":   clean fast-forward / pure ahead — safe to merge
          - "noop": branch has no new commits (already merged or empty)
          - "skip": unsafe (diverged, missing branches, git error)
        """
        # Verify target branch exists
        r = run_capture(["git", "rev-parse", "--verify", f"refs/heads/{target}"], cwd=repo)
        if r.returncode != 0:
            return ("skip", f"target branch '{target}' missing")

        # Verify source branch exists
        r = run_capture(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], cwd=repo)
        if r.returncode != 0:
            return ("skip", f"source branch '{branch}' missing")

        # Compute ahead/behind: left=target-only, right=branch-only
        r = run_capture(
            ["git", "rev-list", "--left-right", "--count", f"{target}...{branch}"], cwd=repo,
        )
        if r.returncode != 0:
            return ("skip", f"rev-list failed: {(r.stderr or '').strip()}")
        parts = r.stdout.strip().split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return ("skip", f"unexpected rev-list output: {r.stdout!r}")
        behind, ahead = int(parts[0]), int(parts[1])

        if ahead == 0:
            # No new commits on branch — already merged (or never had any)
            return ("noop", f"branch already merged into {target} (ahead=0, behind={behind})")
        if behind > 0:
            return ("skip", f"diverged from {target} (ahead={ahead}, behind={behind})")
        return ("ok", f"safe to merge (ahead={ahead}, behind=0)")

    # --- Orphan scanning ---

    @staticmethod
    def scan_orphan_branches(repo_path: str, active_branches: set[str]) -> list[str]:
        """Find bot-managed branches not associated with active instances.

        Uses ``git for-each-ref`` instead of ``git branch --list`` so output is
        decoration-free — ``git branch`` prefixes worktree-bound branches with
        ``+ `` which previously slipped past the membership filter and caused
        every active build to be misclassified as orphan on every restart.
        Both sides of the membership check are normalised through
        ``canonical_branch`` for defense-in-depth in case any caller still
        passes pre-decorated names.
        """
        try:
            # Explicit /* glob: ``refs/heads/{prefix}/`` (no glob) is treated as
            # an exact ref name, missing all branches. ``refs/heads/{prefix}/*``
            # matches every direct child.
            result = run_capture(
                ["git", "for-each-ref",
                 "--format=%(refname:short)",
                 f"refs/heads/{config.BRANCH_PREFIX}/*"],
                cwd=repo_path, encoding="utf-8", errors="replace",
            )
            if result.returncode != 0:
                log.warning(
                    "for-each-ref failed in %s: %s",
                    repo_path, (result.stderr or "").strip(),
                )
                return []
            branches: list[str] = []
            for line in result.stdout.splitlines():
                cb = canonical_branch(line)
                if cb is not None:
                    branches.append(cb)
            active_canon = {
                canonical_branch(b) for b in active_branches
                if canonical_branch(b) is not None
            }
            return [b for b in branches if b not in active_canon]
        except Exception:
            log.debug("Failed to scan branches in %s", repo_path, exc_info=True)
            return []

    @staticmethod
    def _git_worktree_branches(repo_path: str) -> set[str]:
        """Return the set of branches currently bound to a git worktree.

        Parses ``git worktree list --porcelain``; lines starting with
        ``branch refs/heads/X`` give the bound branch X. Used as a
        protect-list during orphan cleanup so a branch with a live worktree
        can never be mistakenly deleted, even if the active-branch set fed
        in by the caller is stale.
        """
        try:
            r = run_capture(
                ["git", "worktree", "list", "--porcelain"],
                cwd=repo_path, encoding="utf-8", errors="replace",
            )
            if r.returncode != 0:
                return set()
            bound: set[str] = set()
            for line in r.stdout.splitlines():
                if line.startswith("branch refs/heads/"):
                    name = line[len("branch refs/heads/"):].strip()
                    if name:
                        bound.add(name)
            return bound
        except Exception:
            log.debug("worktree list failed in %s", repo_path, exc_info=True)
            return set()

    @staticmethod
    def _check_worktree_content_matches_branch(
        repo_path: str, worktree_path: str, branch: str,
    ) -> tuple[str, str]:
        """Cheap content-divergence check: do all tracked files in the
        worktree dir hash to the same blobs as the branch tip?

        Returns one of:
          - ("match", "") — every tracked path's content hash matches the
            branch tip's tree. Safe to re-register (``_reregister_worktree_sync``) — the
            re-register won't surprise anyone with overwrites.
          - ("diverged", reason) — at least one tracked file's content has
            been modified relative to the branch tip. Re-registering would
            silently overwrite uncommitted work — refuse.
          - ("error", reason) — couldn't determine (git error, missing dir).
            Caller should treat as "diverged" for safety.

        Implementation: ``git ls-tree -r <branch>`` enumerates expected blobs;
        ``git hash-object --stdin-paths`` rehashes the corresponding files
        in the worktree dir in a single batched call. Untracked files in the
        worktree are ignored — they don't constitute "drift" since they
        wouldn't be touched by re-registering.
        """
        wt = Path(worktree_path)
        if not wt.is_dir():
            return ("error", f"worktree dir missing: {worktree_path}")
        try:
            r = run_capture(
                ["git", "ls-tree", "-r", branch], cwd=repo_path, encoding="utf-8", errors="replace",
            )
            if r.returncode != 0:
                return ("error", f"ls-tree failed: {(r.stderr or '').strip()[:200]}")
            # Each line: "<mode> <type> <sha>\t<path>"
            expected: dict[str, str] = {}
            for line in r.stdout.splitlines():
                if not line:
                    continue
                meta, _, path = line.partition("\t")
                if not path:
                    continue
                parts = meta.split()
                if len(parts) < 3 or parts[1] != "blob":
                    continue
                expected[path] = parts[2]
            if not expected:
                return ("match", "")
            # Build batched hash-object input: only include paths that exist
            # as regular files in the worktree. Missing files mean drift
            # (deletion) — return diverged immediately.
            paths_in_order: list[str] = []
            stdin_lines: list[str] = []
            for rel in expected:
                f = wt / rel
                if not f.is_file():
                    return ("diverged", f"missing tracked file: {rel}")
                paths_in_order.append(rel)
                stdin_lines.append(str(f))
            r = run_capture(
                ["git", "hash-object", "--stdin-paths"],
                cwd=repo_path, encoding="utf-8", errors="replace",
                input="\n".join(stdin_lines) + "\n",
            )
            if r.returncode != 0:
                return ("error", f"hash-object failed: {(r.stderr or '').strip()[:200]}")
            actual = r.stdout.splitlines()
            if len(actual) != len(paths_in_order):
                return (
                    "error",
                    f"hash-object output mismatch: {len(actual)} vs {len(paths_in_order)}",
                )
            for rel, sha in zip(paths_in_order, actual):
                if sha.strip() != expected[rel]:
                    return ("diverged", f"content drift: {rel}")
            return ("match", "")
        except Exception as e:
            return ("error", f"divergence check raised: {e}")

    @staticmethod
    def _reregister_worktree_sync(
        repo_path: str, worktree_path: str | Path, branch: str,
    ) -> str | None:
        """Give an intact worktree directory back its lost git metadata.

        Returns None on success, or the reason it could not be done.  Never
        raises.  The caller must already have proved, with
        ``_check_worktree_content_matches_branch``, that every tracked file
        matches the branch tip.  Both recovery paths share this: the startup
        pass (``recover_partial_worktrees``) and the Build button's
        (``workflows._attempt_inline_worktree_recovery``).

        ``git worktree add --force <dir> <branch>`` is the obvious command and
        does not work: git refuses any target path that already exists
        ("'<dir>' already exists"), ``--force`` or not, and an existing
        directory is the whole premise here.  So the registration is built
        elsewhere and moved in:

          1. ``worktree add --no-checkout`` at a temporary path with the same
             basename creates fresh metadata for the branch, writing no files.
          2. A link to that metadata replaces the directory's ``.git`` file.
          3. ``worktree repair`` points the metadata back at the directory —
             git's documented fix for a worktree moved by hand — and the
             back-link is then read and checked, because ``git worktree
             prune`` deletes metadata whose back-link is dead.
          4. A mixed ``reset`` rebuilds the index, which lived in the lost
             metadata.  It never touches working files, and the content check
             already proved they match the branch tip, so the result is a
             clean checkout with untracked work kept.

        The metadata must be named after the directory: ``_is_worktree_live``
        looks for ``worktrees/<basename>``, so metadata under any other name
        would be registered with git and still read as dead by the bot.

        Any failure after step 1 deletes the metadata it created and puts the
        old ``.git`` file back byte for byte.  That is the state the caller
        started from, so the next attempt meets the same situation.  Prune is
        not a substitute: once step 3 has run, the back-link is live and
        prune keeps a registration whose index may still be empty.
        """
        wt_dir = Path(worktree_path)
        dot_git = wt_dir / ".git"
        if dot_git.is_dir():
            # A real repository, not a linked worktree: not ours to rewire.
            return f"{dot_git} is a directory, not a worktree link"
        try:
            old_link = dot_git.read_bytes() if dot_git.is_file() else None
        except OSError as e:
            return f"cannot read {dot_git}: {e}"

        def _git(args: list[str], cwd) -> subprocess.CompletedProcess[str]:
            return run_capture(
                ["git", *args], cwd=str(cwd), encoding="utf-8", errors="replace",
                timeout=60,
            )

        def _err(r: subprocess.CompletedProcess[str]) -> str:
            return (r.stderr or r.stdout or f"exit {r.returncode}").strip()

        def _link_target(link_file: Path) -> Path | None:
            # Both halves of a worktree link hold a path, absolute by default
            # and relative to the file's own directory under
            # worktree.useRelativePaths: the worktree's .git file reads
            # "gitdir: <metadata dir>", the metadata's gitdir file reads
            # "<worktree>/.git".
            try:
                text = link_file.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if text.startswith("gitdir:"):
                text = text[len("gitdir:"):].strip()
            if not text:
                return None
            target = Path(text)
            return target if target.is_absolute() else link_file.parent / target

        def _same(a: Path | None, b: Path) -> bool:
            return a is not None and (
                os.path.normcase(os.path.realpath(a))
                == os.path.normcase(os.path.realpath(b))
            )

        def _replace(path: Path, data: bytes | None) -> None:
            # Replaced, never overwritten in place: Git for Windows marks a
            # worktree's .git file hidden, and Windows refuses to open a
            # hidden file for overwrite (EACCES) while still allowing it to
            # be deleted.  Bytes, so no newline translation creeps in.
            if path.exists():
                os.chmod(path, 0o600)
                path.unlink()
            if data is not None:
                path.write_bytes(data)

        common = git_common_dir(repo_path)
        if not common:
            return f"cannot resolve the git directory of {repo_path}"
        meta_root = Path(common) / "worktrees"

        # Outside the repo on purpose: the startup orphan scans walk
        # .worktrees/, and a scratch directory left there by a crash would
        # look like an abandoned build to them.
        try:
            tmp_root = Path(tempfile.mkdtemp(prefix="wt-recover-"))
        except OSError as e:
            return f"cannot create a scratch directory: {e}"
        tmp_wt = tmp_root / wt_dir.name
        added = ok = False
        created: Path | None = None  # the metadata dir step 1 made
        try:
            r = _git(["worktree", "add", "--no-checkout", str(tmp_wt), branch], repo_path)
            if r.returncode != 0:
                return _err(r)
            added = True
            # Found by its back-link rather than by name, so it is identified
            # exactly even when git had to pick another name for it.
            created = next(
                (d for d in meta_root.iterdir()
                 if _same(_link_target(d / "gitdir"), tmp_wt / ".git")),
                None,
            ) if meta_root.is_dir() else None
            if created is None:
                return "git registered the branch but its metadata could not be found"
            if created.name != wt_dir.name:
                return (
                    f"git named the metadata {created.name!r}, not "
                    f"{wt_dir.name!r}; the bot would not recognise it"
                )
            _replace(dot_git, f"gitdir: {created.as_posix()}\n".encode("utf-8"))
            shutil.rmtree(tmp_root, ignore_errors=True)
            r = _git(["worktree", "repair", str(wt_dir)], repo_path)
            if r.returncode != 0:
                return f"repair: {_err(r)}"
            if not _same(_link_target(created / "gitdir"), dot_git):
                return "repair did not point the metadata back at the directory"
            r = _git(["reset", "-q"], wt_dir)
            if r.returncode != 0:
                return f"index rebuild: {_err(r)}"
            ok = True
            return None
        except Exception as e:
            return f"raised: {e}"
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)
            if added and not ok:
                try:
                    if created is not None:
                        shutil.rmtree(created)
                        _replace(dot_git, old_link)
                    else:
                        # Unidentified, so it cannot be removed by path.  Its
                        # back-link points into the temp dir just deleted,
                        # which is exactly what prune removes.
                        _git(["worktree", "prune"], repo_path)
                except Exception:
                    # The one outcome worth more than a debug line: the
                    # directory is now in a state no caller expects.
                    log.warning(
                        "Rolling back worktree re-register for %s failed",
                        wt_dir, exc_info=True,
                    )

    @staticmethod
    def _select_recovery_candidates(store) -> list[Instance]:
        """Pick the Instance records eligible for the worktree-recovery scan.

        Three filters layered on top of the basic "has branch + worktree +
        repo" precondition:

          1. Status: skip FAILED/KILLED/COMPLETED. FAILED/KILLED are noise
             on crashed/killed builds the user can't usefully act on.
             COMPLETED is terminal from the user's perspective — they've
             either merged/discarded (in which case ``worktree_path`` is
             already cleared and the candidate doesn't reach this filter)
             or moved on without doing so (in which case "inspect before
             the next Build" is the wrong framing — there isn't one).
             The on-demand chain-build path
             (``workflows._attempt_inline_worktree_recovery``) still runs
             the same divergence check when the user actually clicks Build
             against a stale COMPLETED predecessor, so the safety net for
             the only moment it's actionable is preserved.
             Worktree files stay on disk untouched: cleanup_stale_worktrees
             protects branches with ``inst.branch`` set and refuses
             ``shutil.rmtree`` on ``git worktree remove`` failure, so the
             user can still manually inspect or reuse them.
          2. In-pass dedupe by (repo_path, branch). Chained builds share a
             branch — one decision per branch, not N events per chain.
          3. Cross-pass dedupe via ``flagged_branches``. Once any sibling on
             a branch is parked ``manual_recovery_needed``, ALL siblings on
             that branch stay quiet on later reboots. Without this, the
             next-newest sibling becomes the new "first hit" and re-fires
             the warning every reboot until every sibling is flagged.

        ``store.list_instances(all_=True)`` returns newest-first. Iteration
        order means we may add a newer non-flagged sibling to ``candidates``
        before encountering an older flagged sibling on the same branch, so
        flagging is collected during the loop and applied as a post-filter.
        """
        flagged_branches: set[tuple[str, str]] = set()
        seen: set[tuple[str, str]] = set()
        candidates: list[Instance] = []
        skipped_completed_total = 0
        skipped_completed_with_worktree = 0
        for inst in store.list_instances(all_=True):
            if not (inst.branch and inst.worktree_path and inst.repo_path):
                if inst.status == InstanceStatus.COMPLETED:
                    skipped_completed_total += 1
                continue
            key = (inst.repo_path, inst.branch)
            if inst.manual_recovery_needed:
                flagged_branches.add(key)
                continue
            if inst.status == InstanceStatus.COMPLETED:
                skipped_completed_total += 1
                skipped_completed_with_worktree += 1
                continue
            if inst.status in (InstanceStatus.FAILED, InstanceStatus.KILLED):
                continue
            if key in seen:
                continue
            seen.add(key)
            candidates.append(inst)
        if skipped_completed_total:
            log.info(
                "recovery scan: skipped %d COMPLETED instances "
                "(%d with branch+worktree set)",
                skipped_completed_total, skipped_completed_with_worktree,
            )
        return [
            inst for inst in candidates
            if (inst.repo_path, inst.branch) not in flagged_branches
        ]

    async def recover_partial_worktrees(
        self, store, repos: dict[str, str],
    ) -> list[WorktreeRecoveryEvent]:
        """Re-register worktrees whose ``.git/worktrees/<name>/`` metadata is
        gone but whose working directory + branch are otherwise intact.

        This is the auto-recovery path for the t-3700 failure mode: an
        earlier orphan-cleanup pass ran ``git worktree remove`` against a
        live build (because it misidentified the branch as orphan), nuked
        the metadata, then failed to delete the branch. The branch tip and
        the working files survived; only git's link between them was lost.
        Without this method, ``_is_worktree_live`` returns False forever and
        every later Build silently restarts from master.

        Walks the deduped+filtered candidate set from
        ``_select_recovery_candidates`` (one Instance per (repo, branch),
        FAILED/KILLED skipped, branches with any flagged sibling silenced).
        For each candidate whose metadata dir is missing:
          1. Compare worktree contents against the branch tip blob hashes.
          2. On match → ``_reregister_worktree_sync`` to re-register.
          3. On drift → flag instance ``manual_recovery_needed`` so the
             user is forced to inspect before the bot touches it again.
          4. On error → emit a "skipped" event, leave instance untouched.

        Each decision is recorded as a ``WorktreeRecoveryEvent`` so the
        platform layer can post a one-line notice into the relevant thread.
        Silence is what kept the original bug invisible — we don't repeat
        that here.
        """
        events: list[WorktreeRecoveryEvent] = []
        # Build repo_path → repo_name reverse map for event labelling.
        path_to_name = {p: n for n, p in repos.items()}

        candidates = self._select_recovery_candidates(store)
        for inst in candidates:
            repo_path = inst.repo_path
            if not Path(repo_path).is_dir():
                continue
            wt_path = inst.worktree_path
            wt_dir = Path(wt_path)
            basename = wt_dir.name
            _gd = git_common_dir(repo_path)
            meta_dir = (
                Path(_gd) / "worktrees" / basename if _gd
                else Path(repo_path) / ".git" / "worktrees" / basename
            )
            # Metadata present? Nothing to recover.
            if meta_dir.is_dir():
                continue
            # Working directory missing entirely? Not our problem here —
            # the orphan-cleanup path will pick it up. (Also: we'd have no
            # files to compare against the branch tip.)
            if not wt_dir.is_dir():
                continue
            repo_name = path_to_name.get(repo_path, repo_path)
            # Ensure the branch still exists at all — if not, this isn't a
            # partial-worktree case, it's a fully-cleaned-up case.
            r = run_capture(
                ["git", "rev-parse", "--verify", f"refs/heads/{inst.branch}"],
                cwd=repo_path, encoding="utf-8", errors="replace",
            )
            if r.returncode != 0:
                events.append(WorktreeRecoveryEvent(
                    instance_id=inst.id, repo_name=repo_name,
                    branch=inst.branch, status="skipped",
                    detail="branch no longer exists",
                ))
                continue
            # Check content match vs branch tip
            try:
                decision, reason = await asyncio.to_thread(
                    self._check_worktree_content_matches_branch,
                    repo_path, wt_path, inst.branch,
                )
            except Exception as e:
                events.append(WorktreeRecoveryEvent(
                    instance_id=inst.id, repo_name=repo_name,
                    branch=inst.branch, status="skipped",
                    detail=f"divergence check raised: {e}",
                ))
                continue
            if decision == "diverged":
                inst.manual_recovery_needed = True
                inst.manual_recovery_reason = reason
                store.update_instance(inst)
                events.append(WorktreeRecoveryEvent(
                    instance_id=inst.id, repo_name=repo_name,
                    branch=inst.branch, status="manual_recovery_needed",
                    detail=reason,
                ))
                continue
            if decision == "error":
                events.append(WorktreeRecoveryEvent(
                    instance_id=inst.id, repo_name=repo_name,
                    branch=inst.branch, status="skipped",
                    detail=reason,
                ))
                continue
            # decision == "match": safe to re-register under repo lock
            try:
                repo_lock = self._get_repo_lock(repo_path)
                async with repo_lock:
                    failure = await asyncio.to_thread(
                        self._reregister_worktree_sync,
                        repo_path, wt_dir, inst.branch,
                    )
            except Exception as e:
                events.append(WorktreeRecoveryEvent(
                    instance_id=inst.id, repo_name=repo_name,
                    branch=inst.branch, status="skipped",
                    detail=f"re-register raised: {e}",
                ))
                continue
            if failure:
                events.append(WorktreeRecoveryEvent(
                    instance_id=inst.id, repo_name=repo_name,
                    branch=inst.branch, status="skipped",
                    detail=f"re-register failed: {failure[:200]}",
                ))
                continue
            events.append(WorktreeRecoveryEvent(
                instance_id=inst.id, repo_name=repo_name,
                branch=inst.branch, status="recovered",
                detail="re-registered after metadata loss",
            ))
        return events

    async def scan_orphan_release_commits(
        self, store, repos: dict[str, str],
    ) -> list[ReleaseRecoveryEvent]:
        """Detect release sessions that committed a version bump but never
        tagged it (bot died or session was killed mid-release).

        Conservative by design: only acts on instances whose own worktree
        still exists AND whose worktree HEAD carries a ``vX.Y.Z:`` commit
        with no matching tag. Other startup-stale instances (QUEUED that
        never ran, RUNNING that never committed, worktree already cleaned
        up) are left untouched — their HEAD reflects nothing about THIS
        instance's release attempt, so we'd misattribute. They get handled
        by the existing recovery / cleanup paths.
        """
        import re as _re
        events: list[ReleaseRecoveryEvent] = []
        path_to_name = {p: n for n, p in repos.items()}

        # Match `vX.Y.Z[.W]: ...` at the start of the commit subject.
        version_re = _re.compile(r"^(v\d+\.\d+\.\d+(?:\.\d+)?)[:\s]")

        target_origins = {InstanceOrigin.RELEASE, InstanceOrigin.DONE}
        non_terminal = {InstanceStatus.RUNNING, InstanceStatus.QUEUED}

        for inst in store.list_instances(all_=True):
            if inst.origin not in target_origins:
                continue
            if inst.status not in non_terminal:
                continue
            if not inst.repo_path or not Path(inst.repo_path).is_dir():
                continue
            # Require a live worktree — falling back to the main repo's HEAD
            # would misattribute another worktree's release commit to this
            # instance, which is exactly the multi-worktree scenario this
            # fix exists for.
            if not inst.worktree_path or not Path(inst.worktree_path).is_dir():
                continue

            try:
                subj_proc = await asyncio.to_thread(
                    run_capture, ["git", "log", "-1", "--pretty=%H%n%s"],
                    cwd=inst.worktree_path, encoding="utf-8", errors="replace",
                )
            except Exception:
                continue
            if subj_proc.returncode != 0:
                continue

            lines = (subj_proc.stdout or "").strip().splitlines()
            if len(lines) < 2:
                continue
            commit_sha = lines[0].strip()
            subject = lines[1].strip()
            m = version_re.match(subject)
            if not m:
                # Worktree HEAD isn't release-shaped — instance crashed
                # before bumping (or never started). Leave alone; existing
                # cleanup mechanisms own this case.
                continue
            version = m.group(1)

            try:
                tag_proc = await asyncio.to_thread(
                    run_capture,
                    ["git", "rev-parse", "--verify", f"refs/tags/{version}"],
                    cwd=inst.repo_path, encoding="utf-8", errors="replace",
                )
            except Exception:
                continue
            tag_exists = tag_proc.returncode == 0
            if tag_exists:
                # Bumped + tagged — the dangerous step is already done.
                # Don't surface; another mechanism can sweep up the stale
                # status if needed.
                continue

            repo_name = path_to_name.get(inst.repo_path, inst.repo_path)
            inst.status = InstanceStatus.FAILED
            inst.error = (
                f"Release session interrupted: commit {commit_sha[:12]} bumped "
                f"to {version} but no matching tag exists. Verify before re-running."
            )
            store.update_instance(inst)
            # Stop autopilot chain resume from silently re-spawning another
            # release attempt against the same session — leaving the bumped
            # commit on the branch and a fresh release on top is the failure
            # mode this probe is meant to prevent. User decides via the
            # surfaced notice whether to /release again or revert.
            if inst.session_id:
                try:
                    store.clear_chain_state(inst.session_id)
                except Exception:
                    log.debug(
                        "Failed to clear autopilot chain for session %s",
                        inst.session_id, exc_info=True,
                    )
            events.append(ReleaseRecoveryEvent(
                instance_id=inst.id, repo_name=repo_name,
                branch=inst.branch, commit_sha=commit_sha,
                bumped_version=version,
                detail="commit bumped version but tag was never created",
            ))
        return events

    @staticmethod
    def scan_orphan_worktrees(repo_path: str, active_worktrees: set[str]) -> list[str]:
        """Find stale .worktrees/ directories not associated with active instances."""
        wt_parent = Path(repo_path) / ".worktrees"
        if not wt_parent.is_dir():
            return []
        orphans = []
        for d in wt_parent.iterdir():
            if d.is_dir() and str(d) not in active_worktrees:
                orphans.append(d.name)
        return orphans

    # --- Startup cleanup ---

    async def cleanup_stale_worktrees(
        self, store, repos: dict[str, str],
    ) -> list[str]:
        """Merge pending done instances, fix repo HEADs, clean orphans.

        Called on startup to recover from interrupted autopilot chains.
        Returns a list of actions taken (for logging).
        """
        messages: list[str] = []

        # 1. Fix repos stuck on bot-managed branches (under repo lock)
        for name, path in repos.items():
            if not Path(path).is_dir():
                continue
            try:
                repo_lock = self._get_repo_lock(path)
                async with repo_lock:
                    msg = await asyncio.to_thread(self._fix_repo_head, path)
                if msg:
                    messages.append(f"{name}: {msg}")
            except Exception:
                log.warning("Failed to fix HEAD for %s", name, exc_info=True)

        # 2. Merge completed done instances that still have branches
        # (merge_branch already acquires repo lock internally)
        # Age gate: skip instances older than 24h to avoid silently merging
        # stale/diverged branches. User can still /merge manually.
        MAX_STALE_AGE_HOURS = 24
        now = datetime.now(timezone.utc)
        done_insts = [
            inst for inst in store.list_instances(all_=True)
            if inst.origin == InstanceOrigin.DONE
            and inst.status == InstanceStatus.COMPLETED
            and inst.branch and inst.original_branch
            and inst.repo_path and Path(inst.repo_path).is_dir()
        ]
        for inst in done_insts:
            branch_name = inst.branch
            # Check age — use finished_at, fall back to created_at
            age_ref = inst.finished_at or inst.created_at
            ref_dt = parse_iso_utc(age_ref)
            if ref_dt is None:
                messages.append(f"skip {branch_name}: no timestamp — use /merge manually")
                continue
            age_hours = (now - ref_dt).total_seconds() / 3600
            # Guard against future dates (system clock wrong) or excessively old instances
            if age_hours < 0:
                messages.append(
                    f"skip {branch_name}: timestamp in future ({-age_hours:.0f}h) — check system clock"
                )
                continue
            if age_hours > MAX_STALE_AGE_HOURS:
                messages.append(
                    f"skip {branch_name}: stale ({age_hours:.0f}h old) — use /merge manually"
                )
                continue

            # Divergence safety check — never silently merge a branch that has
            # forked from its target since the bot session started.
            try:
                decision, reason = await asyncio.to_thread(
                    self._check_branch_divergence,
                    inst.repo_path, branch_name, inst.original_branch,
                )
            except Exception as e:
                log.warning("startup auto-merge: divergence check raised for %s",
                            branch_name, exc_info=True)
                messages.append(f"skip {branch_name}: divergence check failed ({e})")
                continue
            if decision == "skip":
                messages.append(f"skip {branch_name}: {reason} — use /merge manually")
                continue
            if decision == "noop":
                messages.append(f"no-op {branch_name}: {reason}")
                # Branch already merged; clear stale branch refs on all
                # instances (including source) so this case doesn't recur
                # on every startup.
                clear_stale_branches(store, branch_name)
                continue

            try:
                msg = await self.merge_branch(inst)
                store.update_instance(inst)
                if not merge_msg_is_failure(msg):
                    clear_stale_branches(store, branch_name)
                messages.append(f"merge {branch_name}: {msg}")
            except Exception as e:
                log.warning("startup auto-merge: merge %s raised", branch_name, exc_info=True)
                messages.append(f"merge {branch_name}: error ({e})")

        # 3. Clean up remaining orphaned branches and worktrees (under repo lock)
        for name, path in repos.items():
            if not Path(path).is_dir():
                continue
            try:
                repo_lock = self._get_repo_lock(path)
                async with repo_lock:
                    cleaned = await asyncio.to_thread(
                        self._cleanup_orphans_sync, store, path,
                    )
                messages.extend(f"{name}: {c}" for c in cleaned)
            except Exception:
                log.warning("Orphan cleanup failed for %s", name, exc_info=True)

        return messages

    def _fix_repo_head(self, repo_path: str) -> str | None:
        """If repo HEAD is on a bot-managed branch, checkout default branch."""
        # Bug 2 pre-flight: a leftover MERGE_HEAD makes `git checkout` refuse
        # ("you need to resolve your current index first"). Clear it so
        # startup HEAD recovery can actually run.
        precheck = self._check_main_repo_clean(repo_path)
        if precheck.error:
            return precheck.error
        r = run_capture(["git", "symbolic-ref", "--short", "HEAD"], cwd=repo_path)
        current = r.stdout.strip() if r.returncode == 0 else ""
        if not current.startswith(f"{config.BRANCH_PREFIX}/"):
            return None
        target = self._get_default_branch(repo_path)
        r = run_capture(["git", "checkout", target], cwd=repo_path)
        if r.returncode != 0:
            log.warning("Failed to checkout %s in %s: %s",
                        target, repo_path, r.stderr.strip())
            return f"stuck on {current} (checkout {target} failed)"
        return f"switched HEAD from {current} to {target}"

    def _cleanup_orphans_sync(self, store, repo_path: str) -> list[str]:
        """Remove orphaned branches and worktrees for a repo.

        Defense-in-depth against the t-3700 bug class: never falls through to
        ``shutil.rmtree`` (would silently destroy uncommitted work in a worktree
        whose metadata was lost), and unions in ``git worktree list``-bound
        branches as a protected set. If either ``git worktree remove`` or
        ``git branch -D`` returns non-zero we log+report a skip rather than
        keep marching toward deletion.
        """
        cleaned: list[str] = []

        # Collect branches still referenced by any instance — canonicalise
        # both sides so a stray decoration in state.json (or anywhere else)
        # can't punch a hole in the protect-list.
        active_branches = {
            canonical_branch(inst.branch) for inst in store.list_instances(all_=True)
            if inst.branch and canonical_branch(inst.branch)
        }
        # Belt-and-braces: anything currently bound to a live worktree is
        # always protected, even if state.json forgot it.
        wt_protected = self._git_worktree_branches(repo_path)
        protected = active_branches | wt_protected

        orphan_branches = self.scan_orphan_branches(repo_path, active_branches)
        for branch in orphan_branches:
            # Skip anything in the protected set after the second check —
            # scan_orphan_branches uses active_branches but wt_protected is
            # a separate, independent guard.
            if branch in protected:
                cleaned.append(f"skip orphan {branch}: bound to live worktree")
                continue

            # Infer worktree dir from branch name (prefix/t-xxx → .worktrees/t-xxx)
            wt_name = branch.split("/")[-1] if "/" in branch else branch
            wt_dir = Path(repo_path) / ".worktrees" / wt_name
            if wt_dir.exists():
                r = run_capture(
                    ["git", "worktree", "remove", str(wt_dir), "--force"],
                    cwd=repo_path, encoding="utf-8", errors="replace",
                )
                if r.returncode != 0:
                    cleaned.append(
                        f"skip orphan {branch}: worktree remove failed "
                        f"({(r.stderr or '').strip()[:200]})"
                    )
                    # Do NOT fall through to rmtree — silent destruction of
                    # uncommitted work was exactly the bug we are fixing.
                    continue
            r = run_capture(
                ["git", "branch", "-D", branch], cwd=repo_path, encoding="utf-8", errors="replace",
            )
            if r.returncode != 0:
                cleaned.append(
                    f"skip orphan {branch}: branch -D failed "
                    f"({(r.stderr or '').strip()[:200]})"
                )
                continue
            cleaned.append(f"cleaned orphan {branch}")

        # Prune any worktree registrations pointing to deleted directories
        run_capture(
            ["git", "worktree", "prune"], cwd=repo_path, encoding="utf-8", errors="replace",
        )

        return cleaned
