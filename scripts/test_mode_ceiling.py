#!/usr/bin/env python3
"""Regression test: one mode ceiling, and welcome buttons read real state.

The rule "a mode above the ceiling is lowered to the ceiling" had four copies
(the access grant, ``RequestContext.update_mode``, the spawned workflow step,
and a rank table for the most restrictive grant). It now lives once, in
``bot.platform.modes.cap_mode``.

The Mode and Effort buttons on a session's welcome embed used to read the
*other* setting back out of the embed's field labels, so an embed posted
before the Effort field existed rebuilt its buttons on a guessed "high". They
now read the thread's own state.

Pinned here:

  * ``cap_mode`` lowers, never raises, and ignores a missing ceiling;
  * a grant with no mode recorded caps at explore (fail closed);
  * no hand-rolled mode rank table survives outside modes.py;
  * a capped user pressing Build gets the ceiling saved to the thread;
  * either button redraws the embed with the thread's real other value.

Run:  python scripts/test_mode_ceiling.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from bot.discord import interactions  # noqa: E402
from bot.discord.access import AccessResult, RepoAccess, effective_mode  # noqa: E402
from bot.discord.forums import ThreadInfo  # noqa: E402
from bot.platform.base import RequestContext  # noqa: E402
from bot.platform.modes import cap_mode  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


def _arm_cap() -> None:
    print("cap_mode")
    check(cap_mode("build", "plan") == "plan", "build capped to plan")
    check(cap_mode("explore", "plan") == "explore", "explore stays below plan")
    check(cap_mode("build", None) == "build", "no ceiling, no cap")
    check(cap_mode("build", "") == "build", "empty ceiling, no cap")
    check(effective_mode(RepoAccess(mode="plan"), "build") == "plan", "grant caps")
    check(effective_mode(RepoAccess(mode=""), "build") == "explore",
          "grant with no mode caps at explore")
    ctx = RequestContext(messenger=None, channel_id="c", platform="t", store=None, runner=None)
    ctx.mode_ceiling = "explore"
    ctx.update_mode("build")
    check(ctx.mode == "explore", f"update_mode caps (got {ctx.mode})")


def _arm_no_copies() -> None:
    print("no second rank table")
    pat = re.compile(r'["\']explore["\']\s*:\s*0\s*,')
    hits = [
        str(p.relative_to(ROOT)) for p in (ROOT / "bot").rglob("*.py")
        if p.name != "modes.py" and pat.search(p.read_text(encoding="utf-8"))
    ]
    check(hits == [], f"rank tables only in modes.py (found {hits})")


class _Response:
    def __init__(self) -> None:
        self.edits: list[dict] = []
        self.deferred = False

    async def edit_message(self, **kw):  # noqa: ANN003
        self.edits.append(kw)

    async def defer(self):  # noqa: ANN201
        self.deferred = True


def _interaction(fields: dict[str, str]) -> SimpleNamespace:
    embed = discord.Embed(title="t")
    for k, v in fields.items():
        embed.add_field(name=k, value=v, inline=True)
    return SimpleNamespace(
        channel_id=42, response=_Response(),
        message=SimpleNamespace(embeds=[embed]),
    )


def _bot(info: ThreadInfo | None) -> SimpleNamespace:
    saved: list[bool] = []

    async def tags(thread_id):  # noqa: ANN001
        return None

    forums = SimpleNamespace(
        thread_to_project=lambda tid: (SimpleNamespace(), info) if info else None,
        save_forum_map=lambda: saved.append(True),
    )
    return SimpleNamespace(
        _forums=forums, _store=SimpleNamespace(mode="explore", effort="high"),
        _try_apply_tags_after_run=tags, saved=saved,
    )


def _field(inter: SimpleNamespace, name: str) -> str | None:
    embed = inter.response.edits[-1]["embed"]
    return next((f.value for f in embed.fields if f.name == name), None)


def _disabled(inter: SimpleNamespace) -> set[str]:
    view = inter.response.edits[-1]["view"]
    return {item.custom_id for item in view.children if item.disabled}


async def _arm_buttons() -> None:
    print("welcome buttons")
    guest = AccessResult(allowed=True, is_owner=False, mode_ceiling="plan")
    owner = AccessResult(allowed=True, is_owner=True)

    info = ThreadInfo(thread_id="42", effort="max")
    bot = _bot(info)
    inter = _interaction({"Mode": "Explore"})  # an old embed: no Effort field
    await interactions._handle_mode_set(bot, inter, "build", guest)
    await asyncio.sleep(0)
    check(info.mode == "plan", f"capped user saved the ceiling (got {info.mode})")
    check(bot.saved == [True], "forum map saved")
    check(_field(inter, "Mode") == "Plan", "embed shows the capped mode")
    check(_field(inter, "Effort") == "Max",
          f"effort from the thread, not a guess (got {_field(inter, 'Effort')})")

    info = ThreadInfo(thread_id="42", mode="build")
    inter = _interaction({"Mode": "Explore", "Effort": "High"})  # a stale label
    await interactions._handle_effort_set(_bot(info), inter, "low", owner)
    check(info.effort == "low", "effort saved to the thread")
    dis = _disabled(inter)
    check(any("build" in c for c in dis),
          f"mode buttons rebuilt on the thread's mode (disabled {sorted(dis)})")

    bot = _bot(None)
    inter = _interaction({"Mode": "Explore"})
    await interactions._handle_effort_set(bot, inter, "medium", owner)
    check(bot._store.effort == "medium", "owner outside a thread sets the default")


async def main() -> int:
    _arm_cap()
    _arm_no_copies()
    await _arm_buttons()
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
