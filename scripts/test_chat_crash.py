#!/usr/bin/env python3
"""Regression test: a chat turn that crashes or is cancelled still ends.

``lifecycle.run_instance`` was fixed for t-8954: an exception before
``finalize_run`` marks the instance FAILED and resolves its card, and a
shutdown cancellation marks it interrupted. The chat path,
``commands._execute_query``, which runs every message typed into a thread and
every /spawn child's turns, had neither. A runner exception left the instance
RUNNING in state forever with its card spinning on "thinking...", and a
cancelled turn did the same until the next boot swept it.

Asserted here, driving the real ``_execute_query`` with the runner stubbed:

  * a runner that raises leaves the instance FAILED with the stage in its
    error, resolves the card to a failure, and re-raises;
  * a failure after the run was finalized keeps the run's real status;
  * a cancelled run is marked interrupted and its card says so;
  * the runner's task slot is released on every one of those exits.

Run: python scripts/test_chat_crash.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.claude.types import Instance, InstanceStatus, InstanceType, RunResult  # noqa: E402
from bot.engine import commands, lifecycle, query  # noqa: E402
from bot.platform.base import MessageHandle, RequestContext  # noqa: E402

_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}" + (f"  ({detail})" if detail else ""))
        _failures.append(name)


class _Messenger:
    def __init__(self) -> None:
        self.edits: list[str] = []

    def escape(self, text: str) -> str:
        return text

    async def send_text(self, *a, **kw):  # noqa: ANN002, ANN003
        return "msg-1"

    async def edit_text(self, *a, **kw):  # noqa: ANN002, ANN003
        return True

    async def send_thinking(self, *a, **kw):  # noqa: ANN002, ANN003
        return MessageHandle(platform="test")

    async def edit_thinking(self, handle, text, **kw):  # noqa: ANN001, ANN003
        self.edits.append(text)
        return True


class _Store:
    verbose_level = 1
    context = None
    active_session_id = None
    mode = "explore"
    effort = None
    model = None

    def __init__(self, inst: Instance) -> None:
        self.inst = inst

    def get_instance(self, _id: str) -> Instance:
        return self.inst

    def update_instance(self, inst: Instance, critical: bool = False) -> None:
        pass

    def list_instances(self, *a, **kw):  # noqa: ANN002, ANN003
        return [self.inst]

    def list_by_status(self, _status):  # noqa: ANN001
        return []

    def list_repos(self) -> dict[str, str]:
        return {"harness": tempfile.gettempdir()}

    def create_instance(self, **kw) -> Instance:  # noqa: ANN003
        return self.inst


class _Runner:
    def __init__(self, outcome) -> None:  # noqa: ANN001
        self.outcome = outcome
        self.ended: list[str] = []

    def begin_task(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
        pass

    def end_task(self, instance_id: str) -> None:
        self.ended.append(instance_id)

    def check_spawn_allowed(self) -> None:
        return None

    async def run(self, instance, **kw):  # noqa: ANN001, ANN003
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _Case:
    def __init__(self) -> None:
        self.inst: Instance | None = None
        self.raised: BaseException | None = None
        self.edits: list[str] = []
        self.ended: list[str] = []


async def _drive(outcome, *, send_result_raises: bool = False) -> _Case:  # noqa: ANN001
    case = _Case()
    inst = Instance(
        id="t-99301", name=None, instance_type=InstanceType.TASK, prompt="anything",
        repo_name="harness", repo_path=tempfile.gettempdir(), status=InstanceStatus.QUEUED,
    )
    case.inst = inst
    messenger = _Messenger()
    runner = _Runner(outcome)

    async def _send_result(*a, **kw):  # noqa: ANN002, ANN003
        if send_result_raises:
            raise RuntimeError("card post failed")

    async def _anoop(*a, **kw):  # noqa: ANN002, ANN003
        return None

    async def _no_cooldown(*a, **kw):  # noqa: ANN002, ANN003
        return False

    async def _heartbeat() -> None:
        return None

    def _callbacks(*a, **kw):  # noqa: ANN002, ANN003
        return (None, None, _heartbeat, None, None)

    ctx = RequestContext(
        messenger=messenger,  # type: ignore[arg-type]
        channel_id="c1", platform="test",
        store=_Store(inst),  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        repo_name="harness", is_owner=True, source="user_message",
    )
    patches = [
        (lifecycle, "send_result", _send_result),
        (lifecycle, "make_progress_callbacks", _callbacks),
        (lifecycle, "bind_thread_session", _anoop),
        (lifecycle, "schedule_cooldown_retry", _no_cooldown),
        (lifecycle, "check_reboot_request", _anoop),
        (lifecycle, "check_wake_request", _anoop),
        (query, "check_budget", lambda ctx: True),
        (query, "budget_warning", _anoop),
        (query, "_execute_bot_commands", _anoop),
    ]
    saved = [(mod, name, getattr(mod, name)) for mod, name, _ in patches]
    for mod, name, value in patches:
        setattr(mod, name, value)
    logging.getLogger(query.__name__).setLevel(logging.CRITICAL)
    try:
        await commands._execute_query(ctx, "anything")
    except BaseException as e:  # noqa: BLE001
        case.raised = e
    finally:
        logging.getLogger(query.__name__).setLevel(logging.NOTSET)
        for mod, name, value in saved:
            setattr(mod, name, value)
        await asyncio.sleep(0)
    case.edits = messenger.edits
    case.ended = runner.ended
    return case


async def main_async() -> None:
    crash = await _drive(RuntimeError("worktree setup failed"))
    inst = crash.inst
    _check("runner raises: the exception still propagates",
           isinstance(crash.raised, RuntimeError), repr(crash.raised))
    _check("runner raises: instance is FAILED, not left RUNNING",
           inst.status == InstanceStatus.FAILED, str(inst.status))
    _check("runner raises: the error names the stage and the cause",
           "before the run could start" in (inst.error or "")
           and "worktree setup failed" in (inst.error or ""), inst.error or "")
    _check("runner raises: finished_at is set", bool(inst.finished_at))
    _check("runner raises: the card is resolved to a failure",
           any(e.startswith("❌") and e.endswith("failed") for e in crash.edits), str(crash.edits))
    _check("runner raises: the task slot is released", crash.ended == [inst.id], str(crash.ended))

    late = await _drive(RunResult(session_id="s-1", result_text="Done."), send_result_raises=True)
    _check("fails after finalize: the run keeps its real status",
           late.inst.status == InstanceStatus.COMPLETED and not late.inst.error,
           f"{late.inst.status} {late.inst.error!r}")
    _check("fails after finalize: the task slot is released", late.ended == [late.inst.id])

    cancel = await _drive(asyncio.CancelledError())
    _check("cancelled: the cancellation still propagates",
           isinstance(cancel.raised, asyncio.CancelledError), repr(cancel.raised))
    _check("cancelled: instance is marked interrupted",
           cancel.inst.status == InstanceStatus.FAILED
           and cancel.inst.error == "Bot restarted, instance interrupted",
           f"{cancel.inst.status} {cancel.inst.error!r}")
    _check("cancelled: the card says it was interrupted",
           "Interrupted by bot restart" in cancel.edits, str(cancel.edits))
    _check("cancelled: the task slot is released", cancel.ended == [cancel.inst.id])


def main() -> int:
    asyncio.run(main_async())
    print()
    if _failures:
        print(f"FAIL: {len(_failures)} chat-crash check(s) failed")
        return 1
    print("PASS: a crashed or cancelled chat turn is recorded and its card resolved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
