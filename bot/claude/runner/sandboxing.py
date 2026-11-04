"""The runner's half of the session sandbox: one spawn in, one spawn out.

``bot/claude/sandbox.py`` decides what a run gets. This mixin does the async
ordering around it, and ``core._prepare_spawn`` is its only caller, so resume,
chain steps, retries and recovery re-entries all pass through here.
See docs/design/session-sandbox.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from bot import vault
from bot.claude import sandbox
from bot.claude.runner.git_merge import MERGE_FAIL_SANDBOX_PROTECTED
from bot.claude.types import RunResult

if TYPE_CHECKING:
    from bot.claude.provider import ProviderConfig
    from bot.claude.types import Instance

log = logging.getLogger(__name__)


def refused_result(instance: "Instance", reason: object) -> RunResult:
    return RunResult(
        is_error=True,
        error_message=f"{sandbox.REFUSAL_PHRASE}: {reason}",
        result_text="",
        session_id=instance.session_id,
    )


class SandboxMixin:
    def _sandbox_memory_dir(self, instance: "Instance") -> str | None:
        try:
            projects = self._get_projects_dir(instance.repo_path)
        except Exception:
            return None
        return str(projects / "memory") if projects else None

    def _sandbox_prepare_sync(
        self, instance: "Instance", provider: "ProviderConfig",
        api_fallback: bool, working_dir: str, entry: dict,
    ) -> sandbox.SandboxRun:
        repo_path = instance.repo_path
        sandbox.preflight(provider.name, api_fallback, repo_path)
        sandbox.harden_repo_git(repo_path)
        sandbox.add_stub_excludes(os.path.join(repo_path, ".git"))
        store = self._store
        other_repos = [p for p in store.list_repos().values() if p != repo_path]
        keep: set[str] = set()
        if instance.is_owner_session and instance.vault_eligible and instance.repo_name:
            keep = set(vault.env_for_repo(instance.repo_name))
        return sandbox.prepare_run(
            instance_id=instance.id, repo_path=repo_path, working_dir=working_dir,
            hosts=entry["hosts"], prompt=instance.prompt, other_repos=other_repos,
            memory_dir=self._sandbox_memory_dir(instance), keep_names=keep,
        )

    async def _sandbox_prepare(
        self, instance: "Instance", provider: "ProviderConfig",
        api_fallback: bool, working_dir: str | None,
    ) -> "sandbox.SandboxRun | RunResult | None":
        """This spawn's sandbox: a run, a refusal to return, or None when off."""
        store = getattr(self, "_store", None)
        try:
            entry = sandbox.setting_for(store, instance.repo_path)
        except Exception as exc:
            # On for this repo and unreadable: the run does not go ahead.
            log.error("Refusing %s: %s", instance.id, exc)
            return refused_result(instance, "its sandbox setting cannot be read")
        if entry is None:
            return None
        if not working_dir:
            return refused_result(instance, "it has no working directory")
        try:
            return await asyncio.to_thread(
                self._sandbox_prepare_sync, instance, provider, api_fallback,
                working_dir, entry,
            )
        except sandbox.SandboxRefused as exc:
            log.error("Refusing %s: %s", instance.id, exc)
            return refused_result(instance, exc)
        except Exception as exc:
            log.exception("Sandbox preparation failed for %s", instance.id)
            return refused_result(instance, f"preparation failed ({type(exc).__name__})")

    def _sandbox_merge_block(self, instance: "Instance") -> str | None:
        """Why an unattended merge of this branch needs the owner, or None.

        These files change what later sessions load and what runs on the host,
        so a branch from a sandboxed session does not land them unseen.
        """
        repo = instance.repo_path
        if not sandbox.is_sandboxed(getattr(self, "_store", None), repo):
            return None
        changes = sandbox.protected_changes(repo, instance.original_branch, instance.branch)
        if changes == []:
            return None
        self._last_merge_failure_kind[instance.id] = MERGE_FAIL_SANDBOX_PROTECTED
        if changes is None:
            return "Merge failed: sandboxed repo, and git could not list what this branch changes."
        listed = ", ".join(f"`{p}`" for p in changes[:8])
        more = f" and {len(changes) - 8} more" if len(changes) > 8 else ""
        return f"Merge failed: sandboxed repo, and this branch changes {listed}{more}."

    async def _sandbox_enter(
        self, instance: "Instance", run: sandbox.SandboxRun,
    ) -> RunResult | None:
        """Check the run out. A refusal to return, or None to go ahead."""
        try:
            await self._sandbox_checkout(run)
            return None
        except Exception as exc:
            if not isinstance(exc, sandbox.SandboxRefused):
                log.exception("Sandbox check-out failed for %s", instance.id)
            await asyncio.to_thread(sandbox.finish_run, run)
            return refused_result(instance, exc)

    async def _sandbox_after_attempt(self, spawn) -> str | None:
        """End one attempt's sandbox. Survives the run being cancelled mid-way."""
        run, spawn.sandbox_run = spawn.sandbox_run, None
        if run is None:
            return None
        # The session must be gone before its git directory is trusted.
        proc = spawn.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        finish = asyncio.ensure_future(self._sandbox_finish(run, proc))
        return await asyncio.shield(finish)

    async def _sandbox_checkout(self, run: sandbox.SandboxRun) -> None:
        """Move a worktree onto its private gitdir. Raises SandboxRefused."""
        if not run.is_worktree:
            run.checked_out = True
            return
        async with self._get_repo_lock(run.repo_path):
            users = self._sandbox_wt_users
            if users.get(run.working_dir, 0) == 0:
                await asyncio.to_thread(
                    sandbox.checkout_worktree, run.working_dir, run.repo_path,
                    run.instance_id,
                )
            users[run.working_dir] = users.get(run.working_dir, 0) + 1
            run.checked_out = True

    def _sandbox_other_live(self, run: sandbox.SandboxRun) -> bool:
        store = self._store
        repo = os.path.realpath(run.repo_path)
        for other_id in list(self._processes):
            if other_id == run.instance_id:
                continue
            other = store.get_instance(other_id)
            if other is None or not other.repo_path:
                return True
            if os.path.realpath(other.repo_path) == repo:
                return True
        return False

    async def _sandbox_finish(self, run: sandbox.SandboxRun, proc=None) -> str | None:
        """Check a finished attempt back in. Returns a problem to surface, or None."""
        problem: str | None = None
        try:
            if proc is not None:
                await proc.wait()
            if run.checked_out and run.is_worktree:
                async with self._get_repo_lock(run.repo_path):
                    users = self._sandbox_wt_users
                    left = users.get(run.working_dir, 1) - 1
                    if left > 0:
                        users[run.working_dir] = left
                    else:
                        users.pop(run.working_dir, None)
                        problem = await asyncio.to_thread(
                            sandbox.checkin_worktree, run.working_dir,
                        )
            if run.checked_out and not self._sandbox_other_live(run):
                await asyncio.to_thread(sandbox.clean_stubs, run.working_dir)
        except Exception as exc:
            log.exception("Sandbox check-in failed for %s", run.instance_id)
            problem = str(exc) or type(exc).__name__
        finally:
            await asyncio.to_thread(sandbox.finish_run, run)
        return problem
