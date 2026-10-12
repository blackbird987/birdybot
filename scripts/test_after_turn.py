#!/usr/bin/env python3
"""Regression test: one post-turn bookkeeping routine for a thread.

Nine places (a typed message, a button, a steer, the quick-task modal,
/done, both usage-gate replays, the boot-time chain resume and the cooldown
retry) each ended a turn with their own copy of: keep the thread's settings,
redraw its tags, re-arm the idle timer, redraw the dashboard. The copies had
already drifted in order and in which steps they ran. They now all call
``ClaudeBot._after_turn``.

Pinned here:

  * the steps run, in order, and the optional ones obey their flags;
  * the idle timer is armed nowhere else, so a tenth copy fails the suite.

Run:  python scripts/test_after_turn.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import ast
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.discord.bot import ClaudeBot  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


class _Forums:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def persist_ctx_settings(self, ctx):  # noqa: ANN001
        self.calls.append(f"persist:{ctx}")

    async def update_pending_thread(self, channel_id):  # noqa: ANN001
        self.calls.append(f"pending:{channel_id}")


class _Bot:
    _after_turn = ClaudeBot._after_turn

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._forums = _Forums(self.calls)

    async def _try_apply_tags_after_run(self, channel_id):  # noqa: ANN001
        self.calls.append(f"tags:{channel_id}")

    def _schedule_sleep(self, channel_id):  # noqa: ANN001
        self.calls.append(f"sleep:{channel_id}")

    async def _refresh_dashboard(self):  # noqa: ANN201
        self.calls.append("dashboard")


async def _run(**kw) -> list[str]:  # noqa: ANN003
    bot = _Bot()
    await bot._after_turn("ctx", "7", **kw)
    await asyncio.sleep(0)  # let the detached tags/dashboard tasks run
    return bot.calls


async def _arm_steps() -> None:
    print("steps and flags")
    calls = await _run()
    check(calls == ["persist:ctx", "sleep:7", "tags:7", "dashboard"],
          f"default: settings, idle timer, tags, dashboard (got {calls})")
    calls = await _run(pending_thread=True)
    check(calls[:2] == ["persist:ctx", "pending:7"],
          f"a /new thread is bound right after its settings (got {calls})")
    calls = await _run(dashboard=False)
    check("dashboard" not in calls and "tags:7" in calls,
          f"dashboard=False skips only the dashboard (got {calls})")


def _arm_single_site() -> None:
    print("idle timer armed in one place")
    offenders: list[str] = []
    for path in [*(ROOT / "bot").rglob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if fn.name == "_after_turn":
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "_schedule_sleep"):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} in {fn.name}")
    check(offenders == [], f"_schedule_sleep only called from _after_turn (found {offenders})")


async def main() -> int:
    await _arm_steps()
    _arm_single_site()
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
