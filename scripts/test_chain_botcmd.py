"""Regression test: chain/background turns dispatch BOT_CMD directives (t-5944).

Background: lifecycle.run_instance (workflow chain steps, background tasks,
spawned children) delivered results but never ran the post-turn
[BOT_CMD: /repo|/spawn] scan that the chat path (commands.on_query tail) runs,
so a directive emitted by a Build/Review step was rendered as inert text —
no dispatch, no refusal notice. run_instance now calls
commands._execute_bot_commands after result delivery.

Drives the REAL run_instance end-to-end with fakes only at the platform
seams (messenger, runner, store):
- a successful chain turn whose result_text carries 2 directive+body pairs
  -> both children spawned, in order, audit list updated
- autopilot running -> no spawn, but an EXPLICIT refusal notice is posted
  (the silent-swallow this fix removes)
- error result -> no scan, no spawn
- directive-free result -> no spawn, no notices

Run: python scripts/test_chain_botcmd.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import asyncio
from types import SimpleNamespace

from bot.claude.types import Instance, InstanceOrigin, RunResult
from bot.engine import lifecycle
from bot.platform.base import RequestContext, SpawnResult

_failures: list[str] = []


def _check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


class _FakeMessenger:
    """Captures sends; any messenger method not defined is an async no-op."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, channel_id, text, *a, **kw):
        self.sent.append(text)
        return "msg-txt"

    async def send_result(self, *a, **kw):
        return "msg-res"

    def escape(self, text):
        return text

    def markdown_to_markup(self, text):
        return text

    def chunk_message(self, text):
        return [text]

    def format_mention(self, uid):
        return f"@{uid}"

    def __getattr__(self, name):
        async def _noop(*a, **kw):
            return None
        return _noop


def _make_env(
    *,
    result_text: str,
    is_error: bool = False,
    autopilot_status: str | None = None,
):
    """Real RequestContext + real Instance; fakes at the platform seams."""
    messenger = _FakeMessenger()
    spawned: list = []

    inst = Instance(
        id="t-chain", name=None,
        instance_type="task", prompt="x",  # type: ignore[arg-type]
        repo_name="bot", repo_path="", status="running",  # type: ignore[arg-type]
        # review_plan is in _SKIP_HISTORY_ORIGINS — keeps the harness away
        # from the history file without changing the code path under test.
        origin=InstanceOrigin.REVIEW_PLAN,
    )

    chain_meta = {"status": autopilot_status} if autopilot_status else None
    store = SimpleNamespace(
        update_instance=lambda i, critical=False: None,
        list_instances=lambda all_=False: [],
        list_by_status=lambda status: [],
        context="",
        add_cost=lambda c: None,
        get_autopilot_chain_meta=lambda sid: chain_meta,
        get_autopilot_chain=lambda sid: None,
        list_repos=lambda: ["bot"],
        get_instance=lambda iid: inst,
        get_daily_cost=lambda: 0.0,
    )

    result = RunResult(
        session_id="sess-1",
        result_text=result_text,
        is_error=is_error,
        error_message="boom" if is_error else "",
    )

    async def _run(inst_, **kw):
        return result

    runner = SimpleNamespace(
        begin_task=lambda iid, session_id=None, channel_id=None: None,
        end_task=lambda iid: None,
        run=_run,
        active_instance_for_session=lambda sid: inst.id,
        active_instance_for_channel=lambda cid: inst.id,
    )

    ctx = RequestContext(
        messenger=messenger,
        channel_id="chan-1",
        platform="discord",
        store=store,
        runner=runner,
        session_id="sess-1",
        repo_name="bot",
    )

    async def _spawn(args):
        spawned.append(args)
        return SpawnResult(
            thread_id=f"thread-{len(spawned)}",
            thread_mention="<#x>", thread_url=None,
        )

    ctx.spawn_session = _spawn
    ctx.read_spawn_wave_count = lambda: 0
    return ctx, inst, messenger, spawned


_TWO_DIRECTIVES = (
    "Fanning out two sessions.\n\n"
    '[BOT_CMD: /spawn repo=bot title="Kid A" mode=build]\n'
    "~~~spawn\nPrompt A\n~~~\n"
    '[BOT_CMD: /spawn repo=bot title="Kid B" mode=explore]\n'
    "~~~spawn\nPrompt B\n~~~\n"
)


def test_chain_turn_dispatches() -> None:
    print("chain turn with 2 directives dispatches both:")
    ctx, inst, msgr, spawned = _make_env(result_text=_TWO_DIRECTIVES)
    asyncio.run(lifecycle.run_instance(ctx, inst))
    _check(len(spawned) == 2, "both directives spawned")
    _check(
        [s.title for s in spawned] == ["Kid A", "Kid B"],
        "spawned in order of appearance",
    )
    _check(
        inst.spawn_dispatched_thread_ids == ["thread-1", "thread-2"],
        "audit list records both children",
    )


def test_autopilot_gate_posts_notice() -> None:
    print("autopilot running -> explicit refusal, no spawn:")
    ctx, inst, msgr, spawned = _make_env(
        result_text=_TWO_DIRECTIVES, autopilot_status="running",
    )
    asyncio.run(lifecycle.run_instance(ctx, inst))
    _check(len(spawned) == 0, "no children spawned")
    _check(
        any("/spawn refused" in m and "autopilot" in m for m in msgr.sent),
        "refusal notice posted (not silently swallowed)",
    )


def test_error_result_not_scanned() -> None:
    print("error result is not scanned:")
    ctx, inst, msgr, spawned = _make_env(
        result_text=_TWO_DIRECTIVES, is_error=True,
    )
    asyncio.run(lifecycle.run_instance(ctx, inst))
    _check(len(spawned) == 0, "no spawn from an error turn")


def test_plain_result_no_noise() -> None:
    print("directive-free result stays quiet:")
    ctx, inst, msgr, spawned = _make_env(result_text="all done, no fan-out")
    asyncio.run(lifecycle.run_instance(ctx, inst))
    _check(len(spawned) == 0, "nothing spawned")
    _check(
        not any("/spawn" in m for m in msgr.sent),
        "no spawn notices for a plain result",
    )


def main() -> int:
    test_chain_turn_dispatches()
    test_autopilot_gate_posts_notice()
    test_error_result_not_scanned()
    test_plain_result_no_noise()
    if _failures:
        print(f"\n{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("\nall chain-botcmd tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
