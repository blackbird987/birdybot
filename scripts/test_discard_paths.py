#!/usr/bin/env python3
"""Regression test: /discard and the Discard button are one path, and a
discard that did not happen leaves the branch usable.

The two discards were near-copies that had drifted. Only the button stopped a
merge-conflict resolver still working in the worktree and ended the session's
chain. Both decided whether the discard worked by looking for "failed" in the
message, which no discard message contains, so a discard that was skipped
(leftover merge state in the main repo) or that git refused still dropped the
pending merge. The button also wiped the branch from every instance pointing
at it, unconditionally, so Merge and Discard disappeared while the branch
lived on.

Asserted here:

  * the runner reports a real discard as not failed and clears the branch;
  * a branch git refuses to delete is reported failed and stays on the
    instance;
  * both entry points stop a live resolver and end the chain;
  * on a failed discard both keep the pending merge and leave the instances
    that point at the branch alone; on success both clear them.

Run: python scripts/test_discard_paths.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config  # noqa: E402
from bot.claude.runner import ClaudeRunner, DiscardOutcome  # noqa: E402
from bot.claude.types import Instance, InstanceStatus, InstanceType, KillOutcome  # noqa: E402
from bot.engine import commands  # noqa: E402
from bot.platform.base import RequestContext  # noqa: E402
from bot.store import history as history_mod  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}" + (f"  ({detail})" if detail else ""))
        _failures.append(name)


def _git(repo: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _repo(tmp: str, branch: str) -> str:
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    Path(repo, "a.txt").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "branch", branch)
    return repo


def _build(repo: str, branch: str, wt: str | None) -> Instance:
    return Instance(
        id="t-disc", name=None, instance_type=InstanceType.TASK, prompt="",
        repo_name="r", repo_path=repo, status=InstanceStatus.COMPLETED, mode="build",
        branch=branch, original_branch="master", worktree_path=wt,
    )


def check_runner() -> None:
    runner = ClaudeRunner()
    branch = f"{config.BRANCH_PREFIX}/t-disc"
    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp, branch)
        wt = os.path.join(tmp, "wt")
        _git(repo, "worktree", "add", "-q", wt, branch)
        inst = _build(repo, branch, wt)
        out = runner._discard_branch_sync(inst)
        _check("runner: a real discard is not failed", not out.failed, out.message)
        _check("runner: a real discard clears the branch", inst.branch is None)

    with tempfile.TemporaryDirectory() as tmp:
        repo = _repo(tmp, branch)
        # Checked out in a worktree the instance does not own: git refuses -D.
        other = os.path.join(tmp, "other")
        _git(repo, "worktree", "add", "-q", other, branch)
        inst = _build(repo, branch, None)
        out = runner._discard_branch_sync(inst)
        _check("runner: a branch git refuses to delete is failed", out.failed, out.message)
        _check("runner: the surviving branch stays on the instance", inst.branch == branch,
               str(inst.branch))


class _Messenger:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def escape(self, text: str) -> str:
        return text

    async def send_text(self, _cid, text, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        self.sent.append(text)
        return "m"

    async def edit_text(self, _cid, _mid, text, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        self.sent.append(text)
        return True

    async def close_conversation(self, *a, **kw):  # noqa: ANN002, ANN003
        return None


class _Runner:
    def __init__(self, outcome: DiscardOutcome, resolver_id: str) -> None:
        self.outcome = outcome
        self.resolver_id = resolver_id
        self.killed: list[str] = []
        self._processes: dict = {}

    def active_instance_for_session(self, _sid):  # noqa: ANN001
        return None if self.killed else self.resolver_id

    async def kill_and_wait(self, iid, **kw):  # noqa: ANN001, ANN003
        self.killed.append(iid)
        return KillOutcome.FINALIZED

    async def discard_branch(self, inst, **kw):  # noqa: ANN001, ANN003
        if not self.outcome.failed:
            inst.branch = None
            inst.worktree_path = None
        return self.outcome


async def _drive(entry: str, outcome: DiscardOutcome, tmp: Path) -> dict:
    store = StateStore(tmp / "state.json", tmp / "results")
    inst = store.create_instance(InstanceType.TASK, "build")
    inst.branch, inst.original_branch, inst.repo_path = "claude-bot/t-x", "master", str(tmp)
    inst.session_id = "sess-1"
    sibling = store.create_instance(InstanceType.TASK, "sibling")
    sibling.branch = "claude-bot/t-x"
    resolver = store.create_instance(InstanceType.TASK, "resolve")
    resolver.session_id = "sess-r"
    store.set_pending_merge(inst.id, session_id="sess-1", channel_id="c1",
                            repo_name="r", message="conflict")
    store.set_pending_merge_resolver(inst.id, resolver.id)
    store.set_autopilot_chain("sess-1", ["merge"])
    runner = _Runner(outcome, resolver.id)
    ctx = RequestContext(
        messenger=_Messenger(),  # type: ignore[arg-type]
        channel_id="c1", platform="test", store=store,
        runner=runner,  # type: ignore[arg-type]
        repo_name="r", is_owner=True,
    )
    if entry == "/discard":
        await commands.on_discard(ctx, inst.id)
    else:
        await commands.handle_callback(ctx, "discard", inst.id, None)
    return {
        "resolver_killed": runner.killed == [resolver.id],
        "chain_cleared": store.get_autopilot_chain("sess-1") is None,
        "pending_kept": store.get_pending_merge(inst.id) is not None,
        "sibling_branch": store.get_instance(sibling.id).branch,
    }


def check_entry_points() -> None:
    saved = history_mod.HISTORY_FILE
    with tempfile.TemporaryDirectory() as d:
        history_mod.HISTORY_FILE = Path(d) / "history.jsonl"
        try:
            for entry in ("/discard", "button"):
                for name, outcome in (
                    ("skipped", DiscardOutcome("Discard skipped: MERGE_HEAD present", failed=True)),
                    ("ok", DiscardOutcome("Discarded branch, back on master")),
                ):
                    with tempfile.TemporaryDirectory() as t:
                        tmp = Path(t)
                        (tmp / "results").mkdir()
                        r = asyncio.run(_drive(entry, outcome, tmp))
                    label = f"{entry} {name}"
                    _check(f"{label}: stops the live resolver", r["resolver_killed"])
                    _check(f"{label}: ends the chain", r["chain_cleared"])
                    if outcome.failed:
                        _check(f"{label}: keeps the pending merge", r["pending_kept"])
                        _check(f"{label}: leaves the sibling's branch alone",
                               r["sibling_branch"] == "claude-bot/t-x", str(r["sibling_branch"]))
                    else:
                        _check(f"{label}: drops the pending merge", not r["pending_kept"])
                        _check(f"{label}: clears the sibling's branch",
                               r["sibling_branch"] is None, str(r["sibling_branch"]))
        finally:
            history_mod.HISTORY_FILE = saved


def main() -> int:
    check_runner()
    check_entry_points()
    print()
    if _failures:
        print(f"FAIL: {len(_failures)} discard check(s) failed")
        return 1
    print("PASS: both discards share one path and a failed discard keeps the branch usable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
