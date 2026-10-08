#!/usr/bin/env python3
"""Regression test: a run that crashes before its CLI starts is not left RUNNING.

The 2026-10-07 incident (t-8954, aiagent): Merge and Done were tapped three
seconds apart on the same build. The merge held the repo lock; Done's step
queued behind it in ``_ensure_worktree``. When the merge finished it cleared
the branch off every instance sharing it, so the waiting step woke up with
``branch=None`` and died inside ``git worktree add -b None`` with a
TypeError. Two things went wrong, and this harness pins both:

  * ``lifecycle.run_instance`` had a handler for cancellation and none for an
    ordinary exception, so the instance stayed RUNNING in state forever and
    its card spun on "thinking..." for a day.
  * ``_ensure_worktree`` decided what to do before taking the lock, and
    ``_create_worktree_sync`` would quietly make a brand-new branch off HEAD
    whenever the one it was asked to reuse had been deleted.

Run:  python scripts/test_run_instance_crash.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config  # noqa: E402
from bot.claude.types import Instance, InstanceStatus, InstanceType  # noqa: E402


# ---------------------------------------------------------------------------
# Arm 1: run_instance marks a crash FAILED, resolves the card, re-raises
# ---------------------------------------------------------------------------


class _Messenger:
    def __init__(self) -> None:
        self.thinking_edits: list[str] = []

    def escape(self, text: str) -> str:
        return text

    async def send_text(self, *a, **kw):  # noqa: ANN002, ANN003
        return "msg-1"

    async def edit_text(self, *a, **kw):  # noqa: ANN002, ANN003
        return True

    async def edit_thinking(self, handle, text, **kw):  # noqa: ANN001, ANN003
        self.thinking_edits.append(text)
        return True


class _Store:
    def __init__(self, inst: Instance) -> None:
        self.inst = inst
        self.critical_updates: list[InstanceStatus] = []

    def get_instance(self, _id: str) -> Instance:
        return self.inst

    def update_instance(self, inst: Instance, critical: bool = False) -> None:
        if critical:
            self.critical_updates.append(inst.status)

    def list_instances(self, *a, **kw):  # noqa: ANN002, ANN003
        return [self.inst]

    def list_by_status(self, _status):  # noqa: ANN001
        return []

    verbose_level = 1
    context = None


class _Runner:
    def __init__(self) -> None:
        self.ended: list[str] = []

    def begin_task(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
        pass

    def end_task(self, instance_id: str) -> None:
        self.ended.append(instance_id)

    async def run(self, instance, **kw):  # noqa: ANN001, ANN003
        # The exact t-8954 failure, raised from where it was raised: the
        # runner preparing the run, before any CLI exists.
        raise TypeError("expected str, bytes or os.PathLike object, not NoneType")


def _make_instance(repo_path: str = "", branch: str | None = None) -> Instance:
    return Instance(
        id="t-99954",
        name=None,
        instance_type=InstanceType.TASK,
        prompt="anything",
        repo_name="harness",
        repo_path=repo_path or tempfile.gettempdir(),
        status=InstanceStatus.QUEUED,
        branch=branch,
    )


async def _check_crash_is_finalized(failures: list[str]) -> None:
    from bot.engine.lifecycle import run_instance
    from bot.platform.base import MessageHandle, RequestContext

    inst = _make_instance()
    runner = _Runner()
    store = _Store(inst)
    messenger = _Messenger()
    ctx = RequestContext(
        messenger=messenger,      # type: ignore[arg-type]
        channel_id="c1",
        platform="test",
        store=store,              # type: ignore[arg-type]
        runner=runner,            # type: ignore[arg-type]
    )

    raised = None
    try:
        await run_instance(ctx, inst, MessageHandle(platform="test"))
    except TypeError as e:
        raised = e

    if raised is None:
        failures.append(
            "run_instance swallowed the crash; callers (the chain runner, the "
            "button handler) must still see it fail"
        )
    if inst.status != InstanceStatus.FAILED:
        failures.append(
            f"a run that crashed before its CLI started is left {inst.status.value}; "
            "it stays RUNNING in state until a restart (t-8954)"
        )
    if not inst.error or "TypeError" not in inst.error:
        failures.append(f"the crash is not recorded on the instance: {inst.error!r}")
    if not inst.finished_at:
        failures.append("the crashed instance has no finished_at")
    if InstanceStatus.FAILED not in store.critical_updates:
        failures.append(
            "the FAILED status was not saved through a critical write, so a "
            "restart inside the 60s auto-save window brings it back RUNNING"
        )
    if not messenger.thinking_edits or "failed" not in messenger.thinking_edits[-1]:
        failures.append(
            f"the progress card was not resolved to failed: {messenger.thinking_edits}"
        )
    if runner.ended != [inst.id]:
        failures.append(
            f"end_task was not called exactly once for the crashed run: {runner.ended}"
        )


# ---------------------------------------------------------------------------
# Arm 2: a step whose branch was merged away runs in the main repo
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "h@example.com")
    _git(repo, "config", "user.name", "harness")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _branch_exists(repo: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo, capture_output=True,
    ).returncode == 0


async def _check_merged_branch(failures: list[str]) -> None:
    from bot.claude.runner import ClaudeRunner

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = _make_repo(root)
        runner = ClaudeRunner()

        # The parent build's worktree, on its own branch with its own commit.
        branch = "claude-bot/t-99920"
        parent_wt = repo / ".worktrees" / "t-99920"
        _git(repo, "worktree", "add", str(parent_wt), "-b", branch)
        (parent_wt / "b.txt").write_text("b\n", encoding="utf-8")
        _git(parent_wt, "add", "b.txt")
        _git(parent_wt, "commit", "-q", "-m", "work")
        work = _git(repo, "rev-parse", branch)

        # 2a. Worktree dir gone but the branch survives: recreate on it.
        _git(repo, "worktree", "remove", "--force", str(parent_wt))
        child = _make_instance(str(repo), branch)
        child.id = "t-99954"
        child.worktree_path = str(parent_wt)
        await runner._ensure_worktree(child)
        if not child.worktree_path or not Path(child.worktree_path).is_dir():
            failures.append(
                "a step whose parent's worktree was cleaned up (branch intact) "
                "got no worktree"
            )
        elif _git(Path(child.worktree_path), "rev-parse", "HEAD") != work:
            failures.append(
                "the recreated worktree is not on the parent's branch tip, so "
                "the step runs without the work it was meant to continue"
            )
        if child.worktree_path:
            _git(repo, "worktree", "remove", "--force", child.worktree_path)

        # 2b. Worktree dir AND branch gone (merged): main repo, no new branch.
        _git(repo, "merge", "-q", branch)
        _git(repo, "branch", "-D", branch)
        child = _make_instance(str(repo), branch)
        child.id = "t-99955"
        child.worktree_path = str(parent_wt)
        try:
            await runner._ensure_worktree(child)
        except Exception as e:  # noqa: BLE001
            failures.append(f"a step after its branch was merged crashed: {e!r}")
        if child.branch is not None or child.worktree_path is not None:
            failures.append(
                f"after a merge the step still points at branch={child.branch!r} "
                f"worktree={child.worktree_path!r}; it should run in the main repo"
            )
        if _branch_exists(repo, branch):
            failures.append(
                "a merged branch was silently recreated off HEAD, so the step "
                "runs on a stand-in branch that has nothing to do with the work"
            )

        # 2c. The t-8954 race: the merge holds the repo lock, and clears the
        # branch off this instance while the step waits for it.
        child = _make_instance(str(repo), "claude-bot/t-99956")
        child.id = "t-99957"
        _git(repo, "worktree", "add", str(repo / ".worktrees" / "t-99956"),
             "-b", "claude-bot/t-99956")
        child.worktree_path = str(repo / ".worktrees" / "gone-already")
        lock = runner._get_repo_lock(str(repo))
        await lock.acquire()
        step = asyncio.create_task(runner._ensure_worktree(child))
        await asyncio.sleep(0.05)
        # What the merge does on its way out (clear_stale_branches).
        _git(repo, "worktree", "remove", "--force",
             str(repo / ".worktrees" / "t-99956"))
        _git(repo, "branch", "-D", "claude-bot/t-99956")
        child.branch = None
        lock.release()
        try:
            await step
        except Exception as e:  # noqa: BLE001
            failures.append(
                f"a Done tapped while the merge ran crashed once the merge "
                f"finished: {e!r} (t-8954)"
            )
        if child.branch is not None or child.worktree_path is not None:
            failures.append(
                "after waiting out a merge the step still points at a worktree"
            )

        # 2d. Never `git worktree add -b None`.
        child = _make_instance(str(repo), None)
        child.id = "t-99958"
        try:
            runner._create_worktree_sync(child)
            failures.append("_create_worktree_sync accepted an instance with no branch")
        except RuntimeError:
            pass
        except Exception as e:  # noqa: BLE001
            failures.append(
                f"_create_worktree_sync with no branch raised {type(e).__name__}, "
                "not a RuntimeError that says why"
            )

        # 2e. A fresh build still makes its own branch.
        child = _make_instance(str(repo), "claude-bot/t-99959")
        child.id = "t-99959"
        await runner._ensure_worktree(child)
        if not child.worktree_path or not _branch_exists(repo, "claude-bot/t-99959"):
            failures.append("a fresh build no longer gets its own branch and worktree")


async def _amain() -> int:
    failures: list[str] = []
    saved_hook = config.WORKTREE_HOOK_ENABLED
    config.WORKTREE_HOOK_ENABLED = False
    try:
        await _check_crash_is_finalized(failures)
        await _check_merged_branch(failures)
    finally:
        config.WORKTREE_HOOK_ENABLED = saved_hook

    if failures:
        print("FAIL: run_instance crash / merged-branch worktree")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: a run that crashes before its CLI starts is marked FAILED, its")
    print("      card resolved and the error re-raised; a step whose branch was")
    print("      merged away runs in the main repo instead of crashing or")
    print("      recreating the branch.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
