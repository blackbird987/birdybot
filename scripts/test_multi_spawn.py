"""Regression test for multi-spawn directive pairing (t-5924).

Background: a single assistant response may now emit up to
_MAX_SPAWNS_PER_RESPONSE (5) [BOT_CMD: /spawn] directives, each paired with
its own adjacent ~~~spawn body. Before this change only the FIRST directive
ran and the rest were logged-and-skipped.

Covers the pairing helper _pair_spawn_directives:
- N directives with N bodies -> N pairs, in order
- two directives sharing one body -> 1 pair + 1 no_body rejection
- 6 directives -> 5 pairs + 1 over_cap
- quoted directives are skipped silently and don't consume the cap
- body between directive i and directive i+1 can't be claimed by i+1
- legacy Instance state with the old single-value audit key still loads

Run: python scripts/test_multi_spawn.py
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

from bot.engine.commands import (
    _MAX_SPAWNS_PER_RESPONSE,
    _MAX_SPAWN_WAVES,
    _handle_spawn_directive,
    _pair_spawn_directives,
)
from bot.claude.types import Instance
from bot.platform.base import SpawnResult

_failures: list[str] = []


def _check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def _directive(n: int) -> str:
    return f'[BOT_CMD: /spawn repo=bot title="Task {n}" mode=build]'


def _body(n: int) -> str:
    return f"~~~spawn\nPrompt body {n}\n~~~"


def test_three_pairs() -> None:
    print("three directives, three bodies:")
    text = "Spawning three sessions.\n\n" + "\n".join(
        f"{_directive(i)}\n{_body(i)}" for i in (1, 2, 3)
    )
    pairs, no_body, over_cap = _pair_spawn_directives(text)
    _check(len(pairs) == 3, "3 pairs extracted")
    _check(no_body == 0 and over_cap == 0, "no rejections")
    _check(
        [b for _, b in pairs] == ["Prompt body 1", "Prompt body 2", "Prompt body 3"],
        "bodies paired in order",
    )
    _check(
        all(f'title="Task {i+1}"' in pairs[i][0] for i in range(3)),
        "args paired with the right directive",
    )


def test_shared_body_rejected() -> None:
    print("two directives, one body after the second:")
    text = f"{_directive(1)}\n{_directive(2)}\n{_body(2)}"
    pairs, no_body, over_cap = _pair_spawn_directives(text)
    _check(len(pairs) == 1, "only the directive adjacent to the body runs")
    _check(no_body == 1, "the body-less directive is counted")
    _check('title="Task 2"' in pairs[0][0], "body goes to directive 2, not 1")


def test_cap() -> None:
    print("six directives, six bodies:")
    text = "\n".join(f"{_directive(i)}\n{_body(i)}" for i in range(1, 7))
    pairs, no_body, over_cap = _pair_spawn_directives(text)
    _check(len(pairs) == _MAX_SPAWNS_PER_RESPONSE, "capped at 5 pairs")
    _check(over_cap == 1, "sixth directive counted as over-cap")
    _check(no_body == 0, "no false no-body rejections")


def test_quoted_skipped() -> None:
    print("quoted directive lines are ignored:")
    text = (
        f"> {_directive(1)}\n"
        f"> quoted example above\n"
        f"{_directive(2)}\n{_body(2)}"
    )
    pairs, no_body, over_cap = _pair_spawn_directives(text)
    _check(len(pairs) == 1, "only the unquoted directive runs")
    _check(no_body == 0 and over_cap == 0, "quoted directive not counted as error")
    _check('title="Task 2"' in pairs[0][0], "unquoted directive got its body")


def test_no_directives() -> None:
    print("response without directives:")
    pairs, no_body, over_cap = _pair_spawn_directives("just a normal reply")
    _check(pairs == [] and no_body == 0 and over_cap == 0, "clean empty result")


def test_run_cap_headroom() -> None:
    print("run cap leaves headroom above one full wave:")
    _check(
        _MAX_SPAWN_WAVES >= 2 * _MAX_SPAWNS_PER_RESPONSE,
        "orchestration run cap fits at least two full waves",
    )


def test_legacy_audit_key_migrates() -> None:
    print("legacy single-value audit key still loads:")
    base = {
        "id": "t-legacy",
        "instance_type": "task",
        "prompt": "x",
        "repo_name": "",
        "repo_path": "",
        "status": "completed",
    }
    inst = Instance.from_dict({**base, "spawn_dispatched_thread_id": "123"})
    _check(
        inst.spawn_dispatched_thread_ids == ["123"],
        "old key migrates into the list",
    )
    inst2 = Instance.from_dict(
        {**base, "spawn_dispatched_thread_ids": ["1", "2", "3"]}
    )
    _check(
        inst2.spawn_dispatched_thread_ids == ["1", "2", "3"],
        "new list key round-trips",
    )
    inst3 = Instance.from_dict(base)
    _check(inst3.spawn_dispatched_thread_ids == [], "absent key defaults to []")


class _FakeMessenger:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, channel_id, text, **kw):
        self.sent.append(text)
        return "msg-1"


def _fake_ctx(
    *,
    autopilot_status: str | None = None,
    parent_depth: int = 0,
    wave_count: int = 0,
    daily_cost: float = 0.0,
    with_spawn_session: bool = True,
) -> tuple[SimpleNamespace, _FakeMessenger, list, Instance]:
    """Minimal stand-in for RequestContext — just what the handler touches."""
    messenger = _FakeMessenger()
    spawned: list = []
    parent = Instance(
        id="t-parent", name=None,
        instance_type="task", prompt="x",  # type: ignore[arg-type]
        repo_name="bot", repo_path="", status="running",  # type: ignore[arg-type]
        spawn_depth=parent_depth,
    )
    chain_meta = (
        {"status": autopilot_status} if autopilot_status else None
    )
    store = SimpleNamespace(
        get_autopilot_chain_meta=lambda sid: chain_meta,
        list_repos=lambda: ["bot"],
        get_instance=lambda iid: parent,
        update_instance=lambda inst, critical=False: None,
        get_daily_cost=lambda: daily_cost,
    )
    runner = SimpleNamespace(
        active_instance_for_session=lambda sid: "t-parent",
        active_instance_for_channel=lambda cid: "t-parent",
    )

    async def _spawn(args):
        spawned.append(args)
        return SpawnResult(
            thread_id=f"thread-{len(spawned)}",
            thread_mention="<#x>", thread_url=None,
        )

    ctx = SimpleNamespace(
        channel_id="chan-1",
        session_id="sess-1",
        store=store,
        runner=runner,
        messenger=messenger,
        spawn_session=_spawn if with_spawn_session else None,
        read_spawn_wave_count=lambda: wave_count,
    )
    return ctx, messenger, spawned, parent


def _dispatch(ctx, args_str='repo=bot title="Kid"', body="do the thing"):
    return asyncio.run(_handle_spawn_directive(ctx, args_str, body))


def test_handler_stop_vs_continue() -> None:
    print("handler continue/stop contract (multi-spawn loop control):")

    ctx, msgr, spawned, parent = _fake_ctx()
    _check(_dispatch(ctx) is True, "success returns True (keep dispatching)")
    _check(len(spawned) == 1, "success actually spawned")
    _check(
        parent.spawn_dispatched_thread_ids == ["thread-1"],
        "audit list records the child",
    )

    ctx, msgr, spawned, _ = _fake_ctx(autopilot_status="running")
    _check(
        _dispatch(ctx) is False,
        "autopilot gate returns False (stop the wave)",
    )
    _check(len(spawned) == 0 and len(msgr.sent) == 1, "one refusal, no spawn")

    ctx, _, spawned, _ = _fake_ctx(parent_depth=1)
    _check(_dispatch(ctx) is False, "depth cap returns False")

    ctx, _, spawned, _ = _fake_ctx(wave_count=_MAX_SPAWN_WAVES)
    _check(_dispatch(ctx) is False, "wave cap returns False")

    ctx, _, spawned, _ = _fake_ctx(daily_cost=10**9)
    _check(_dispatch(ctx) is False, "budget gate returns False")

    ctx, _, spawned, _ = _fake_ctx(with_spawn_session=False)
    _check(_dispatch(ctx) is False, "missing platform seam returns False")

    ctx, _, spawned, _ = _fake_ctx()
    _check(
        _dispatch(ctx, args_str="???not kv???") is True,
        "malformed args return True (skip just this directive)",
    )
    _check(len(spawned) == 0, "malformed args did not spawn")

    ctx, _, spawned, _ = _fake_ctx()
    _check(
        _dispatch(ctx, args_str='repo=nope title="Kid"') is True,
        "unknown repo returns True (skip just this directive)",
    )


def main() -> int:
    test_three_pairs()
    test_shared_body_rejected()
    test_cap()
    test_quoted_skipped()
    test_no_directives()
    test_run_cap_headroom()
    test_legacy_audit_key_migrates()
    test_handler_stop_vs_continue()
    if _failures:
        print(f"\n{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("\nall multi-spawn tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
