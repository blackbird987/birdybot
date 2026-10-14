#!/usr/bin/env python3
"""Pins the spawn-wave parent notification: a finished child tells its parent.

``/spawn`` fans work out into child threads and the orchestrator joins the wave
back to the parent (see docs/design/spawn-wave-join.md). The join only works if
every way a child turn can finish ends in
``RequestContext.notify_parent_on_finalize(status_label, text)``: the platform
closure (``bot/discord/thread_ctx.py``, ``_notify_parent``) is bound to the child's
thread and posts the report back into the parent's.

Two engine paths finish a turn and both go through ``lifecycle.notify_parent``:
the finalize tail of ``lifecycle.run_instance`` (builds, chain steps, /bg,
retries) and the chat turn in ``commands._execute_query``, which is how a
/spawn child's own turns run. The chat path used to skip the hook, so a child
only closed its wave through the five-minute sweep and a child that parked on
a question was never reported at all.

What this pins, driving the real ``run_instance`` with the runner stubbed:

  * a successful run awaits the hook exactly once, labelled COMPLETED, carrying
    the displayed result text, with the instance already COMPLETED when it fires;
  * a failed run awaits it exactly once, labelled FAILED;
  * a run that parked on a question awaits it once, labelled BLOCKED (not
    COMPLETED: finalize marks both COMPLETED, the label is what tells them apart);
  * an intentional kill (Kill/Steer) does NOT call it;
  * a hook that raises never aborts the finalize;
  * no hook on the ctx (every platform but Discord) is a quiet no-op;
  * the hook is awaited after the result card is posted, not before;
  * the chat path awaits it the same way, except that a BLOCKED turn a human
    typed (``source == "user_message"``) does not ping the parent.

The hook's signature is ``(status, summary_text)``; it does not receive the
instance, the platform closure holds the child's thread. The harness therefore
records the instance's status at call time as the proof of "the finished
instance".

Run: python scripts/test_parent_notify.py
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

from bot.claude.types import (  # noqa: E402
    Instance,
    InstanceStatus,
    InstanceType,
    RunResult,
)
from bot.engine import lifecycle  # noqa: E402
from bot.platform.base import MessageHandle, RequestContext  # noqa: E402

_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}" + (f"  ({detail})" if detail else ""))
        _failures.append(name)


# ---------------------------------------------------------------------------
# Stubs (same approach as scripts/test_run_instance_crash.py)
# ---------------------------------------------------------------------------


class _Messenger:
    def escape(self, text: str) -> str:
        return text

    async def send_text(self, *a, **kw):  # noqa: ANN002, ANN003
        return "msg-1"

    async def edit_text(self, *a, **kw):  # noqa: ANN002, ANN003
        return True

    async def edit_thinking(self, handle, text, **kw):  # noqa: ANN001, ANN003
        return True


class _Store:
    verbose_level = 1
    context = None

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


class _Runner:
    """Returns a canned RunResult instead of spawning the CLI."""

    def __init__(self, result: RunResult) -> None:
        self.result = result

    def begin_task(self, *a, **kw) -> None:  # noqa: ANN002, ANN003
        pass

    def end_task(self, instance_id: str) -> None:
        pass

    async def run(self, instance, **kw):  # noqa: ANN001, ANN003
        return self.result


def _make_instance() -> Instance:
    return Instance(
        id="t-99201",
        name=None,
        instance_type=InstanceType.TASK,
        prompt="anything",
        repo_name="harness",
        repo_path=tempfile.gettempdir(),
        status=InstanceStatus.QUEUED,
    )


class _Case:
    """One run_instance drive: what the hook saw, in order, with the card post."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.hook_calls: list[tuple[str, str, InstanceStatus]] = []
        self.sent_cards: list[str] = []
        self.raised: BaseException | None = None
        self.inst: Instance | None = None


async def _drive(
    result: RunResult,
    *,
    hook: bool = True,
    hook_raises: bool = False,
) -> _Case:
    """Run lifecycle.run_instance once with the runner stubbed to ``result``."""
    case = _Case()
    inst = _make_instance()
    case.inst = inst

    async def _notify(status: str, text: str) -> None:
        case.events.append("notify")
        case.hook_calls.append((status, text, inst.status))
        if hook_raises:
            raise RuntimeError("parent thread archived")

    async def _send_result(ctx, instance, text, **kw):  # noqa: ANN001, ANN003
        case.events.append("card")
        case.sent_cards.append(text)

    ctx = RequestContext(
        messenger=_Messenger(),            # type: ignore[arg-type]
        channel_id="c1",
        platform="test",
        store=_Store(inst),                # type: ignore[arg-type]
        runner=_Runner(result),            # type: ignore[arg-type]
    )
    if hook:
        ctx.notify_parent_on_finalize = _notify

    # Patched names: lifecycle.send_result (the Discord card post, needs a real
    # messenger) is replaced by a recorder. Everything else in run_instance's
    # finalize path runs for real against the stubs above.
    saved_send_result = lifecycle.send_result
    lifecycle.send_result = _send_result
    try:
        await lifecycle.run_instance(ctx, inst, MessageHandle(platform="test"))
    except BaseException as e:  # noqa: BLE001
        case.raised = e
    finally:
        lifecycle.send_result = saved_send_result
        # run_instance fires a fire-and-forget near-limit tag clear; let it land.
        await asyncio.sleep(0)
    return case


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


async def _check_success_notifies_once() -> None:
    case = await _drive(RunResult(session_id="s-ok", result_text="Child finished the job."))
    _check("success: run_instance did not raise", case.raised is None, repr(case.raised))
    _check(
        "success: hook awaited exactly once",
        len(case.hook_calls) == 1,
        f"calls={case.hook_calls}",
    )
    if case.hook_calls:
        status, text, inst_status = case.hook_calls[0]
        _check("success: label is COMPLETED", status == "COMPLETED", status)
        _check("success: carries the displayed result text", text == "Child finished the job.", repr(text))
        _check(
            "success: instance already COMPLETED when the hook fires",
            inst_status == InstanceStatus.COMPLETED,
            inst_status.value,
        )
    _check(
        "success: hook fires after the result card is posted",
        case.events == ["card", "notify"],
        str(case.events),
    )


async def _check_failure_notifies_once() -> None:
    case = await _drive(
        RunResult(
            session_id="s-bad",
            is_error=True,
            result_text="Build exploded.",
            error_message="Build exploded.",
        )
    )
    _check("failure: run_instance did not raise", case.raised is None, repr(case.raised))
    _check(
        "failure: hook awaited exactly once",
        len(case.hook_calls) == 1,
        f"calls={case.hook_calls}",
    )
    if case.hook_calls:
        status, _text, inst_status = case.hook_calls[0]
        _check("failure: label is FAILED", status == "FAILED", status)
        _check(
            "failure: instance already FAILED when the hook fires",
            inst_status == InstanceStatus.FAILED,
            inst_status.value,
        )


async def _check_needs_input_is_blocked() -> None:
    case = await _drive(
        RunResult(session_id="s-ask", needs_input=True, result_text="Which database should I use?")
    )
    _check(
        "needs_input: hook awaited exactly once",
        len(case.hook_calls) == 1,
        f"calls={case.hook_calls}",
    )
    if case.hook_calls:
        _check(
            "needs_input: label is BLOCKED, not COMPLETED",
            case.hook_calls[0][0] == "BLOCKED",
            case.hook_calls[0][0],
        )


async def _check_intentional_kill_does_not_notify() -> None:
    case = await _drive(
        RunResult(
            session_id="s-kill",
            is_error=True,
            killed_intentionally=True,
            kill_reason="kill",
            error_message="Killed by user",
        )
    )
    _check("kill: hook not awaited", case.hook_calls == [], f"calls={case.hook_calls}")
    _check("kill: instance is KILLED", case.inst.status == InstanceStatus.KILLED, case.inst.status.value)


async def _check_hook_failure_does_not_abort_finalize() -> None:
    # run_instance logs the hook's exception with a traceback by design; keep
    # that expected noise out of the harness output.
    lg = logging.getLogger(lifecycle.__name__)
    saved_level = lg.level
    lg.setLevel(logging.CRITICAL)
    try:
        case = await _drive(RunResult(session_id="s-ok2", result_text="Done."), hook_raises=True)
    finally:
        lg.setLevel(saved_level)
    _check("raising hook: run_instance did not raise", case.raised is None, repr(case.raised))
    _check("raising hook: hook was still awaited once", len(case.hook_calls) == 1)
    _check(
        "raising hook: instance finalized COMPLETED anyway",
        case.inst.status == InstanceStatus.COMPLETED,
        case.inst.status.value,
    )


async def _check_no_hook_is_noop() -> None:
    case = await _drive(RunResult(session_id="s-ok3", result_text="Done."), hook=False)
    _check("no hook: run_instance did not raise", case.raised is None, repr(case.raised))
    _check("no hook: result card still posted", case.sent_cards == ["Done."], str(case.sent_cards))


# ---------------------------------------------------------------------------
# The chat path (E1): commands._execute_query
# ---------------------------------------------------------------------------


class _ChatStore(_Store):
    active_session_id = None
    mode = "explore"
    effort = None
    model = None

    def list_repos(self) -> dict[str, str]:
        return {"harness": tempfile.gettempdir()}

    def create_instance(self, **kw) -> Instance:  # noqa: ANN003
        return self.inst


class _ChatMessenger(_Messenger):
    async def send_thinking(self, *a, **kw):  # noqa: ANN002, ANN003
        return MessageHandle(platform="test")


async def _drive_chat(result: RunResult, *, source: str) -> _Case:
    """Run commands._execute_query once, the path a /spawn child's turns take.

    Everything after the run that talks to Discord or schedules follow-up work
    is replaced by a no-op; ``finalize_run`` and ``notify_parent`` run for real.
    """
    from bot.engine import cmd_state, commands, query

    case = _Case()
    inst = _make_instance()
    case.inst = inst

    async def _notify(status: str, text: str) -> None:
        case.events.append("notify")
        case.hook_calls.append((status, text, inst.status))

    async def _send_result(ctx, instance, text, **kw):  # noqa: ANN001, ANN003
        case.events.append("card")
        case.sent_cards.append(text)

    async def _anoop(*a, **kw):  # noqa: ANN002, ANN003
        return None

    async def _no_cooldown(*a, **kw):  # noqa: ANN002, ANN003
        return False

    async def _heartbeat() -> None:
        return None

    def _callbacks(*a, **kw):  # noqa: ANN002, ANN003
        return (None, None, _heartbeat, None, None)

    ctx = RequestContext(
        messenger=_ChatMessenger(),        # type: ignore[arg-type]
        channel_id="c1",
        platform="test",
        store=_ChatStore(inst),            # type: ignore[arg-type]
        runner=_Runner(result),            # type: ignore[arg-type]
        repo_name="harness",
        is_owner=True,
        source=source,
    )
    ctx.runner.check_spawn_allowed = lambda: None  # type: ignore[attr-defined]
    ctx.notify_parent_on_finalize = _notify

    patches = [
        (lifecycle, "send_result", _send_result),
        (lifecycle, "make_progress_callbacks", _callbacks),
        (lifecycle, "bind_thread_session", _anoop),
        (lifecycle, "schedule_cooldown_retry", _no_cooldown),
        (lifecycle, "check_reboot_request", _anoop),
        (lifecycle, "check_wake_request", _anoop),
        (cmd_state, "check_budget", lambda ctx: True),
        (query, "budget_warning", _anoop),
        (query, "_execute_bot_commands", _anoop),
    ]
    saved = [(mod, name, getattr(mod, name)) for mod, name, _ in patches]
    for mod, name, value in patches:
        setattr(mod, name, value)
    try:
        await commands._execute_query(ctx, "anything")
    except BaseException as e:  # noqa: BLE001
        case.raised = e
    finally:
        for mod, name, value in saved:
            setattr(mod, name, value)
        await asyncio.sleep(0)
    return case


async def _check_chat_path_notifies() -> None:
    ok = await _drive_chat(
        RunResult(session_id="s-c1", result_text="Child answered."), source="spawn_dispatch",
    )
    _check("chat success: _execute_query did not raise", ok.raised is None, repr(ok.raised))
    _check(
        "chat success: hook awaited once, COMPLETED, with the displayed text",
        [(c[0], c[1]) for c in ok.hook_calls] == [("COMPLETED", "Child answered.")],
        str(ok.hook_calls),
    )
    _check(
        "chat success: hook fires after the result card is posted",
        ok.events == ["card", "notify"],
        str(ok.events),
    )

    bad = await _drive_chat(
        RunResult(session_id="s-c2", is_error=True, result_text="Broke.", error_message="Broke."),
        source="spawn_dispatch",
    )
    _check(
        "chat failure: hook awaited once, FAILED",
        [c[0] for c in bad.hook_calls] == ["FAILED"],
        str(bad.hook_calls),
    )

    ask = await _drive_chat(
        RunResult(session_id="s-c3", needs_input=True, result_text="Which one?"),
        source="spawn_dispatch",
    )
    _check(
        "chat needs_input from the child's own brief: BLOCKED reaches the parent",
        [c[0] for c in ask.hook_calls] == ["BLOCKED"],
        str(ask.hook_calls),
    )

    # A human typing in the child's thread who gets a question back is already
    # there to answer it; pinging the parent would be noise.
    human_ask = await _drive_chat(
        RunResult(session_id="s-c4", needs_input=True, result_text="Which one?"),
        source="user_message",
    )
    _check(
        "chat needs_input on a human's turn: no BLOCKED notice",
        human_ask.hook_calls == [],
        str(human_ask.hook_calls),
    )
    human_done = await _drive_chat(
        RunResult(session_id="s-c5", result_text="Done."), source="user_message",
    )
    _check(
        "chat success on a human's turn: COMPLETED still reaches the parent",
        [c[0] for c in human_done.hook_calls] == ["COMPLETED"],
        str(human_done.hook_calls),
    )

    killed = await _drive_chat(
        RunResult(
            session_id="s-c6", is_error=True, killed_intentionally=True,
            kill_reason="kill", error_message="Killed by user",
        ),
        source="spawn_dispatch",
    )
    _check("chat kill: hook not awaited", killed.hook_calls == [], str(killed.hook_calls))


async def _amain() -> int:
    await _check_success_notifies_once()
    await _check_failure_notifies_once()
    await _check_needs_input_is_blocked()
    await _check_intentional_kill_does_not_notify()
    await _check_hook_failure_does_not_abort_finalize()
    await _check_no_hook_is_noop()
    await _check_chat_path_notifies()

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} parent-notify check(s) failed")
        return 1
    print("PASS: run_instance and the chat path await notify_parent_on_finalize")
    print("      exactly once for COMPLETED, FAILED and BLOCKED, skip it for an")
    print("      intentional kill, and survive a raising or absent hook.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
