#!/usr/bin/env python3
"""Regression test: engine button callbacks route through tables.

``callbacks.handle_callback`` used to be one long elif chain. It now reads
three tables: ``_INSTANCE_ACTIONS`` (looked up instance first),
``_WORKFLOW_ACTIONS`` (a facade function name) and ``_MODE_ACTIONS``. Pinned:

  * every workflow action reaches the facade function it names, with the
    tap's arguments, even when that function is patched after import;
  * an instance action on a missing instance answers "Instance not found."
    and never runs its handler;
  * an instance action on a found instance runs its handler with it;
  * the three tables do not overlap, and the remaining named actions are not
    shadowed by any of them;
  * an unknown action names itself instead of doing nothing.

Run:  python scripts/test_callback_dispatch.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.engine import callbacks, workflows  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


class _Messenger:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, _channel, text, *_a, **_kw):  # noqa: ANN001, ANN002, ANN003
        self.sent.append(text)


class _Store:
    def __init__(self, inst=None) -> None:  # noqa: ANN001
        self.inst = inst

    def get_instance(self, _iid):  # noqa: ANN001, ANN202
        return self.inst


def _ctx(inst=None):  # noqa: ANN001, ANN202
    return SimpleNamespace(channel_id="c", messenger=_Messenger(), store=_Store(inst))


def main() -> int:
    inst_tbl = set(callbacks._INSTANCE_ACTIONS)
    wf_tbl = set(callbacks._WORKFLOW_ACTIONS)
    mode_tbl = set(callbacks._MODE_ACTIONS)

    print("tables")
    check(not (inst_tbl & wf_tbl or inst_tbl & mode_tbl or wf_tbl & mode_tbl),
          "the three tables do not overlap")
    named = {"done", "share", "resolve_merge", "resolve_cancel", "wait", "new"}
    check(not named & (inst_tbl | wf_tbl | mode_tbl),
          "the explicitly handled actions are not shadowed by a table")
    check(all(callable(getattr(workflows, n, None))
              for n in callbacks._WORKFLOW_ACTIONS.values()),
          "every workflow entry names a real facade function")

    print("workflow actions reach the facade (patched after import)")
    for action, name in callbacks._WORKFLOW_ACTIONS.items():
        seen: list[tuple] = []

        async def fake(*args, _seen=seen):  # noqa: ANN002
            _seen.append(args)

        saved = getattr(workflows, name)
        setattr(workflows, name, fake)
        ctx = _ctx()
        try:
            asyncio.run(callbacks.handle_callback(ctx, action, "q-1", "m-2"))
        finally:
            setattr(workflows, name, saved)
        check(seen == [(ctx, "q-1", "m-2")], f"{action} -> workflows.{name}")

    print("instance actions")
    for action in sorted(inst_tbl):
        ran: list[str] = []

        async def handler(_ctx, _inst, _src, _ran=ran):  # noqa: ANN001
            _ran.append("ran")

        saved = callbacks._INSTANCE_ACTIONS[action]
        callbacks._INSTANCE_ACTIONS[action] = handler
        try:
            ctx = _ctx(None)
            asyncio.run(callbacks.handle_callback(ctx, action, "q-1", "m-2"))
            missing_ok = ran == [] and ctx.messenger.sent == ["Instance not found."]
            inst = SimpleNamespace(id="q-1")
            ctx = _ctx(inst)
            asyncio.run(callbacks.handle_callback(ctx, action, "q-1", "m-2"))
            found_ok = ran == ["ran"] and ctx.messenger.sent == []
        finally:
            callbacks._INSTANCE_ACTIONS[action] = saved
        check(missing_ok and found_ok, f"{action}: missing refused, found runs")

    print("mode actions")
    for action, target in callbacks._MODE_ACTIONS.items():
        seen_mode: list[str] = []

        async def fake_mode(_ctx, tgt, _iid, _src, _seen=seen_mode):  # noqa: ANN001
            _seen.append(tgt)

        saved = callbacks._on_mode
        callbacks._on_mode = fake_mode
        try:
            asyncio.run(callbacks.handle_callback(_ctx(), action, "q-1", "m-2"))
        finally:
            callbacks._on_mode = saved
        check(seen_mode == [target], f"{action} -> {target}")

    print("unknown action")
    ctx = _ctx()
    asyncio.run(callbacks.handle_callback(ctx, "no_such_button", "q-1"))
    check(len(ctx.messenger.sent) == 1 and "`no_such_button`" in ctx.messenger.sent[0],
          f"named in the reply ({ctx.messenger.sent})")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
