#!/usr/bin/env python3
"""Regression test: a tapped Merge shows it is working, and a second tap is a no-op.

A merge takes seconds (stash, merge, release tag, two pushes) and the card
showed nothing meanwhile. On 2026-10-15 the user tapped Merge five times in
nine seconds. Every extra tap queued on the repo lock, merged again, redid the
deploy bookkeeping and rewrote the card, the last one with "Already merged
(None → main)" over the first one's "Merged into main".

Asserted here:

  * the runner: a merge that queued behind one of the same instance reports
    "Already merged" with the real branch name and never runs a second merge;
  * the Merge button swaps the card for a progress line with no buttons
    before the merge runs, and ends on a check mark;
  * two overlapping taps run one merge and one deploy refresh, and /merge
    during a button merge says one is already running;
  * a failed merge puts Resolve / Try Merge Again / Discard back on the card;
  * the Discard button shows a progress line too.

Run: python scripts/test_merge_button_feedback.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.claude.runner import ClaudeRunner, DiscardOutcome  # noqa: E402
from bot.claude.types import Instance, InstanceStatus, InstanceType  # noqa: E402
from bot.engine import commands, deploy  # noqa: E402
from bot.platform.base import RequestContext  # noqa: E402
from bot.platform.formatting import merge_failed_button_specs  # noqa: E402
from bot.store import history as history_mod  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

_failures: list[str] = []

BRANCH = "claude-bot/t-x"


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}" + (f"  ({detail})" if detail else ""))
        _failures.append(name)


def check_runner_queued_merge() -> None:
    runner = ClaudeRunner()
    inst = Instance(
        id="t-q", name=None, instance_type=InstanceType.TASK, prompt="",
        repo_name="r", repo_path="/nonexistent/r", status=InstanceStatus.COMPLETED,
        mode="build", branch=BRANCH, original_branch="main",
    )
    calls: list[str] = []

    def slow_merge(instance: Instance) -> str:
        calls.append(instance.id)
        time.sleep(0.2)
        instance.branch = None  # what a real merge does to the instance
        return "Merged into main"

    runner._merge_branch_sync = slow_merge  # type: ignore[method-assign]

    async def both() -> list[str]:
        return list(await asyncio.gather(runner.merge_branch(inst), runner.merge_branch(inst)))

    first, second = asyncio.run(both())
    _check("runner: the first merge runs", first == "Merged into main", first)
    _check("runner: a merge queued behind it does not merge again", calls == ["t-q"], str(calls))
    _check("runner: and names the real branch, not None",
           second == f"Already merged ({BRANCH} → main)", second)


class _Messenger:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[tuple[str | None, list | None]] = []

    def escape(self, text: str) -> str:
        return text

    async def send_text(self, _cid, text, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        self.sent.append(text)
        return "m"

    async def edit_text(self, _cid, _mid, text, buttons=None, **kw):  # noqa: ANN001, ANN003
        self.edits.append((text, buttons))
        return True

    async def close_conversation(self, *a, **kw):  # noqa: ANN002, ANN003
        return None


class _Runner:
    def __init__(self, merge_msg: str = "Merged into main") -> None:
        self.merge_msg = merge_msg
        self.merges = 0
        self.discards = 0
        self._last_merge_failure_kind: dict = {}
        self._processes: dict = {}

    def active_instance_for_session(self, _sid):  # noqa: ANN001
        return None

    async def merge_branch(self, inst):  # noqa: ANN001
        self.merges += 1
        await asyncio.sleep(0.1)
        if self.merge_msg.startswith("Merge failed"):
            return self.merge_msg
        inst.branch = None
        inst.worktree_path = None
        return self.merge_msg

    async def discard_branch(self, inst, **kw):  # noqa: ANN001, ANN003
        self.discards += 1
        await asyncio.sleep(0.1)
        inst.branch = None
        inst.worktree_path = None
        return DiscardOutcome("Discarded branch, back on main")


def _setup(tmp: Path, runner: _Runner) -> tuple[RequestContext, Instance, _Messenger]:
    (tmp / "results").mkdir()
    store = StateStore(tmp / "state.json", tmp / "results")
    inst = store.create_instance(InstanceType.TASK, "build")
    inst.branch, inst.original_branch, inst.repo_path = BRANCH, "main", str(tmp)
    inst.session_id = "sess-1"
    store.update_instance(inst)
    messenger = _Messenger()
    ctx = RequestContext(
        messenger=messenger,  # type: ignore[arg-type]
        channel_id="c1", platform="test", store=store,
        runner=runner,  # type: ignore[arg-type]
        repo_name="r", is_owner=True,
    )
    return ctx, inst, messenger


def check_button_merge(tmp: Path, deploys: list[str]) -> None:
    runner = _Runner()
    ctx, inst, messenger = _setup(tmp, runner)

    async def taps() -> None:
        first = asyncio.create_task(commands.handle_callback(ctx, "merge", inst.id, "card"))
        await asyncio.sleep(0.02)  # the second tap lands while the first merges
        await commands.handle_callback(ctx, "merge", inst.id, "card")
        await commands.on_merge(ctx, inst.id)
        await first

    asyncio.run(taps())
    _check("button: two taps run one merge", runner.merges == 1, str(runner.merges))
    _check("button: and one deploy refresh", deploys == [inst.id], str(deploys))
    _check("/merge during a button merge says one is running",
           any("already running" in s for s in messenger.sent), str(messenger.sent))
    progress = messenger.edits[0] if messenger.edits else (None, None)
    _check("button: the card shows a progress line first",
           (progress[0] or "").startswith("⏳ Merging") and BRANCH in (progress[0] or ""),
           str(progress))
    _check("button: the progress line has no buttons to tap again", progress[1] is None)
    final = messenger.edits[-1][0] if messenger.edits else ""
    _check("button: the card ends on a check mark", (final or "").startswith("✅ Merged"),
           str(final))


def check_button_merge_failure(tmp: Path) -> None:
    runner = _Runner("Merge failed: conflict")
    ctx, inst, messenger = _setup(tmp, runner)
    asyncio.run(commands.handle_callback(ctx, "merge", inst.id, "card"))
    text, buttons = messenger.edits[-1] if messenger.edits else ("", None)
    _check("failed merge: the card carries the banner", (text or "").startswith("Merge failed"),
           str(text))
    _check("failed merge: Resolve / Try Merge Again / Discard are back",
           buttons == merge_failed_button_specs(inst.id), str(buttons))


def check_button_discard(tmp: Path) -> None:
    runner = _Runner()
    ctx, inst, messenger = _setup(tmp, runner)
    asyncio.run(commands.handle_callback(ctx, "discard", inst.id, "card"))
    first = messenger.edits[0][0] if messenger.edits else ""
    _check("discard: the card shows a progress line first",
           (first or "").startswith("⏳ Discarding"), str(first))
    _check("discard: the branch is discarded once", runner.discards == 1, str(runner.discards))


def main() -> int:
    check_runner_queued_merge()
    deploys: list[str] = []

    async def fake_deploy(messenger, store, inst):  # noqa: ANN001
        deploys.append(inst.id)

    saved = (deploy.apply_post_merge_deploy, history_mod.HISTORY_FILE)
    deploy.apply_post_merge_deploy = fake_deploy
    try:
        with tempfile.TemporaryDirectory() as d:
            history_mod.HISTORY_FILE = Path(d) / "history.jsonl"
            for case in (lambda t: check_button_merge(t, deploys),
                         check_button_merge_failure, check_button_discard):
                with tempfile.TemporaryDirectory() as t:
                    case(Path(t))
    finally:
        deploy.apply_post_merge_deploy, history_mod.HISTORY_FILE = saved
    print()
    if _failures:
        print(f"FAIL: {len(_failures)} merge feedback check(s) failed")
        return 1
    print("PASS: a tapped Merge shows progress and a second tap does not merge again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
