#!/usr/bin/env python3
"""Regression test: /bg and /release run through the shared run path.

The background task runner used to call the runner directly. That skipped
task tracking (a reboot did not wait for the task), directive dispatch, the
self-wake check and the cleaned-up result text. It now goes through
``lifecycle.run_instance`` and then tops a sessionless thread up with
``lifecycle.backfill_thread_session``, like every other direct caller.

Pinned here:

  * ``_run_bg_task`` calls run_instance, then backfill, in that order.
  * A crash inside run_instance is logged and swallowed: the task is detached,
    and run_instance has already reported the crash to the thread.
  * /release tags only when the run completed.
  * A retry of a release (or of a standalone Done, which cuts one) holds the
    per-repo release lock for its run, like a fresh release; any other retry
    does not, and the lock is free once the retry's run is over.

Run:  python scripts/test_bg_release.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config  # noqa: E402
from bot.claude.runner.core import ClaudeRunner  # noqa: E402
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType  # noqa: E402
from bot.engine import callbacks, cmd_state, instance_cmds, lifecycle  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


def _inst(status: InstanceStatus = InstanceStatus.QUEUED) -> Instance:
    return Instance(
        id="t-1", prompt="p", instance_type=InstanceType.TASK, name="bg",
        repo_name="bot", repo_path="/tmp/not-a-real-repo", status=status,
    )


async def _arm_order() -> None:
    print("run_instance then backfill")
    calls: list[str] = []

    async def fake_run(ctx, inst, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        calls.append("run")
        inst.status = InstanceStatus.COMPLETED

    async def fake_backfill(ctx, inst):  # noqa: ANN001
        calls.append("backfill")
        return False

    orig = lifecycle.run_instance, lifecycle.backfill_thread_session
    lifecycle.run_instance, lifecycle.backfill_thread_session = fake_run, fake_backfill
    try:
        await instance_cmds._run_bg_task(SimpleNamespace(), _inst())
    finally:
        lifecycle.run_instance, lifecycle.backfill_thread_session = orig
    check(calls == ["run", "backfill"], f"calls in order (got {calls})")


async def _arm_crash() -> None:
    print("a crash is logged, not raised")
    calls: list[str] = []

    async def fake_run(ctx, inst, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        calls.append("run")
        raise RuntimeError("boom")

    async def fake_backfill(ctx, inst):  # noqa: ANN001
        calls.append("backfill")
        return False

    orig = lifecycle.run_instance, lifecycle.backfill_thread_session
    lifecycle.run_instance, lifecycle.backfill_thread_session = fake_run, fake_backfill
    raised = False
    try:
        await instance_cmds._run_bg_task(SimpleNamespace(), _inst())
    except Exception:
        raised = True
    finally:
        lifecycle.run_instance, lifecycle.backfill_thread_session = orig
    check(not raised, "crash did not escape the detached task")
    check(calls == ["run"], f"no backfill after a crash (got {calls})")


class _Messenger:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def escape(self, text: str) -> str:
        return text

    async def send_text(self, channel_id, text, **kw):  # noqa: ANN001, ANN003
        self.sent.append(text)
        return "msg-1"


class _Store:
    def __init__(self) -> None:
        self.inst = _inst()

    def get_active_repo(self):  # noqa: ANN201
        return "bot", "/tmp/not-a-real-repo"

    def create_instance(self, **kw):  # noqa: ANN003, ANN201
        return self.inst

    def update_instance(self, inst, critical=False):  # noqa: ANN001
        pass


class _Runner:
    def __init__(self) -> None:
        self.tagged: list[str] = []

    @contextlib.asynccontextmanager
    async def release_lock_scope(self, repo_path, label):  # noqa: ANN001
        yield

    def _tag_release_at(self, repo_path, ref):  # noqa: ANN001
        self.tagged.append(ref)
        return "v9.9.9", None


async def _release(final_status: InstanceStatus) -> tuple[_Runner, _Messenger]:
    store, runner, messenger = _Store(), _Runner(), _Messenger()
    ctx = SimpleNamespace(
        store=store, runner=runner, messenger=messenger, channel_id="c",
        platform="discord", effective_model=None, effective_effort=None,
        is_owner=True, thread_has_guest=False,
    )
    tasks: list[asyncio.Task] = []

    async def fake_bg(ctx, inst):  # noqa: ANN001
        inst.status = final_status

    orig_bg, orig_budget = instance_cmds._run_bg_task, cmd_state.check_budget
    orig_create = asyncio.create_task

    def track(coro, **kw):  # noqa: ANN001, ANN003
        t = orig_create(coro, **kw)
        tasks.append(t)
        return t

    instance_cmds._run_bg_task = fake_bg
    cmd_state.check_budget = lambda ctx: True
    asyncio.create_task = track
    try:
        await instance_cmds.on_release(ctx, "patch")
        await asyncio.gather(*tasks)
    finally:
        instance_cmds._run_bg_task = orig_bg
        cmd_state.check_budget = orig_budget
        asyncio.create_task = orig_create
    return runner, messenger


async def _arm_release() -> None:
    print("/release tags only a completed run")
    runner, messenger = await _release(InstanceStatus.COMPLETED)
    check(runner.tagged == ["HEAD"], f"completed run tagged HEAD (got {runner.tagged})")
    check(any("v9.9.9" in s for s in messenger.sent), "tag announced")
    runner, _ = await _release(InstanceStatus.FAILED)
    check(runner.tagged == [], f"failed run not tagged (got {runner.tagged})")


class _LockRunner:
    """The runner's real release-lock methods, without the rest of the runner."""

    _get_release_lock = ClaudeRunner._get_release_lock
    release_lock_scope = ClaudeRunner.release_lock_scope
    rerun_lock_scope = ClaudeRunner.rerun_lock_scope

    def __init__(self) -> None:
        self._release_locks: dict[str, asyncio.Lock] = {}


def _retry_inst(origin: InstanceOrigin, prompt: str = "p") -> Instance:
    inst = _inst()
    inst.origin = origin
    inst.prompt = prompt
    return inst


async def _retry_held_lock(entry, inst: Instance) -> bool:
    """Run a retry entry point with a fake run; True when the run held the lock."""
    runner = _LockRunner()
    lock = runner._get_release_lock(inst.repo_path)
    seen: list[bool] = []

    async def fake_start(ctx, src, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        return inst, {}

    async def fake_run(ctx, run_inst, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        seen.append(lock.locked())

    async def fake_backfill(ctx, run_inst):  # noqa: ANN001
        return False

    ctx = SimpleNamespace(store=SimpleNamespace(get_instance=lambda _id: inst),
                          runner=runner, channel_id="c")
    saved = (instance_cmds._start_retry, callbacks._start_retry,
             lifecycle.run_instance, lifecycle.backfill_thread_session)
    instance_cmds._start_retry = callbacks._start_retry = fake_start
    lifecycle.run_instance, lifecycle.backfill_thread_session = fake_run, fake_backfill
    try:
        await entry(ctx, inst)
    finally:
        (instance_cmds._start_retry, callbacks._start_retry,
         lifecycle.run_instance, lifecycle.backfill_thread_session) = saved
    check(not lock.locked(), f"{inst.origin.value}: lock is free after the retry")
    return seen == [True]


async def _arm_retry_lock() -> None:
    print("a retried release holds the release lock")
    standalone = config.DONE_PROMPT_STANDALONE
    entries = (
        ("/retry", lambda ctx, inst: instance_cmds.on_retry(ctx, inst.id)),
        ("Retry button", lambda ctx, inst: callbacks._on_retry(ctx, inst, None)),
    )
    for name, entry in entries:
        check(await _retry_held_lock(entry, _retry_inst(InstanceOrigin.RELEASE)),
              f"{name}: a release retry runs under the lock")
        check(await _retry_held_lock(entry, _retry_inst(InstanceOrigin.DONE, "plan\n\n" + standalone)),
              f"{name}: a standalone Done retry runs under the lock")
        check(not await _retry_held_lock(entry, _retry_inst(InstanceOrigin.DONE, config.DONE_PROMPT_CHAIN)),
              f"{name}: a chain Done retry does not take it")
        check(not await _retry_held_lock(entry, _retry_inst(InstanceOrigin.BUILD)),
              f"{name}: a build retry does not take it")

    # The other two retry sites wrap the same scope around their run.
    root = Path(__file__).resolve().parents[1]
    app_src = (root / "bot" / "app.py").read_text(encoding="utf-8")
    cb_src = (root / "bot" / "engine" / "callbacks.py").read_text(encoding="utf-8")
    check("async with ctx.runner.rerun_lock_scope(new_inst):\n            await lifecycle.run_instance("
          in app_src, "cooldown retry wraps its run in the lock")
    check(cb_src.count("async with ctx.runner.rerun_lock_scope(new_inst):") == 2,
          "Retry button and pay-per-use both wrap their run in the lock")


async def main() -> int:
    await _arm_order()
    await _arm_crash()
    await _arm_release()
    await _arm_retry_lock()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
