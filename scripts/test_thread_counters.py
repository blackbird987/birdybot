#!/usr/bin/env python3
"""Harness for the per-thread counters wired onto every RequestContext.

The spawn-wave, self-wake and nudge caps each had a hand-written pair of
closures in ``ClaudeBot._ctx``; one factory builds them now. This drives the
real ``_ctx`` with a fake bot and checks each counter lands on its own
ThreadInfo field, saves through, and degrades to 0 for an unknown thread.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.discord.bot import ClaudeBot  # noqa: E402
from bot.discord.forums import ThreadInfo  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


class _Store:
    mode = "build"

    def __init__(self):
        self.saves = 0

    def save(self):
        self.saves += 1


def _fake_bot(info):
    threads = {"42": (SimpleNamespace(repo_name="r"), info)} if info else {}
    return SimpleNamespace(
        messenger=None, _store=_Store(), _runner=None,
        _forums=SimpleNamespace(thread_to_project=threads.get),
        _offer_usage_limit_choice=None,
    )


def main() -> int:
    info = ThreadInfo(thread_id="42")
    bot = _fake_bot(info)
    ctx = ClaudeBot._ctx(bot, "42")

    check("wake bump returns the new count", ctx.bump_wake_count() == 1
          and ctx.bump_wake_count() == 2 and info.wake_count == 2)
    check("nudge bump lands on its own field", ctx.bump_nudge_count() == 1
          and info.nudge_count == 1 and info.wake_count == 2)
    check("every bump saves through", bot._store.saves == 3, str(bot._store.saves))

    info.spawn_wave_count = 5
    check("wave read reflects the live ThreadInfo", ctx.read_spawn_wave_count() == 5)
    ctx.reset_spawn_wave_count()
    check("wave reset zeroes it and saves", info.spawn_wave_count == 0
          and bot._store.saves == 4)
    ctx.reset_spawn_wave_count()
    check("a reset of a zero count does not save", bot._store.saves == 4)
    ctx.reset_wake_count()
    ctx.reset_nudge_count()
    check("wake and nudge reset", info.wake_count == 0 and info.nudge_count == 0)

    ghost = _fake_bot(None)
    gctx = ClaudeBot._ctx(ghost, "42")
    check("an unknown thread reads 0 and bumps to 0 without saving",
          gctx.read_spawn_wave_count() == 0 and gctx.bump_wake_count() == 0
          and gctx.bump_nudge_count() == 0 and ghost._store.saves == 0)
    gctx.reset_wake_count()
    check("and its reset is a no-op", ghost._store.saves == 0)

    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
