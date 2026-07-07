"""Regression test for the unattended-turn end-of-turn protocol + auto-nudge.

Background: a turn fired by the SYSTEM (a cooldown retry or a self-wake) runs
with nobody watching. If it ends dangling — a "next I'll..." plan with no
action, no ``[BOT_CMD: /wake]``, and no ``[TURN_COMPLETE]`` marker — the thread
silently dies (the q-12314 cooldown-retry dead-end that motivated this). The
fix: ``lifecycle.check_wake_request`` auto-nudges such a turn once (capped at
``config.MAX_CONSEC_NUDGES``) by re-invoking it with an explicit instruction to
finish, wake, or signal completion.

This locks that behavior so a future edit can't (a) stop nudging an unattended
dead-end (reopening the silent stall), (b) start nudging an ATTENDED turn (the
user is right there — nudging would be noise), or (c) nudge a turn that already
signalled completion or scheduled a real wake (which would loop).

Calls the real production ``check_wake_request`` with lightweight stubs so the
test can't drift from what the engine actually does.

Run: python scripts/test_unattended_nudge.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot import config
from bot.engine import lifecycle
from bot.engine.lifecycle import (
    _NUDGE_PROMPT,
    check_wake_request,
    has_turn_complete_marker,
)
from bot.platform.formatting import strip_verify_blocks

_failures: list[str] = []


def _check(label: str, cond: bool) -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


# ---- Stubs ---------------------------------------------------------------
class _Msgr:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, cid, text, silent=False) -> None:
        self.sent.append(text)


class _Store:
    def __init__(self) -> None:
        self.wakes: list[str] = []

    def add_wake(self, prompt, channel_id, next_run_at, repo_name, repo_path) -> None:
        self.wakes.append(prompt)


class _Ctx:
    def __init__(self, source: str, nudge: int = 0) -> None:
        self.channel_id = "c1"
        self.source = source
        self.messenger = _Msgr()
        self.store = _Store()
        self._nudge = nudge
        self._wake = 0

    def bump_nudge_count(self) -> int:
        self._nudge += 1
        return self._nudge

    def reset_nudge_count(self) -> None:
        self._nudge = 0

    def bump_wake_count(self) -> int:
        self._wake += 1
        return self._wake

    def reset_wake_count(self) -> None:
        self._wake = 0


class _Inst:
    def __init__(self, branch=None, warn=False) -> None:
        self.id = "q-1"
        self.branch = branch
        self.warning_pinned = warn
        self.repo_name = "bot"
        self.repo_path = "/x"


def _run(source, text, *, branch=None, warn=False, nudge=0):
    ctx = _Ctx(source, nudge=nudge)
    asyncio.run(check_wake_request(ctx, _Inst(branch, warn), final_text=text))
    return ctx


# ---- Marker detection ----------------------------------------------------
print("Marker detection")
_check("real top-level marker detected",
       has_turn_complete_marker("done\n[TURN_COMPLETE]") is True)
_check("inline-code-quoted marker ignored",
       has_turn_complete_marker("emit `[TURN_COMPLETE]` when done") is False)
_check("absent marker -> False", has_turn_complete_marker("nothing here") is False)
_check("marker stripped from displayed text",
       "[TURN_COMPLETE]" not in strip_verify_blocks("all done\n\n[TURN_COMPLETE]\n"))

# ---- Core nudge behavior -------------------------------------------------
print("Unattended dead-end (cooldown) -> nudge scheduled")
c = _run("cooldown", "Next I'll read the roadmap and re-verify.")
_check("exactly one wake scheduled", len(c.store.wakes) == 1)
_check("scheduled prompt is the nudge prompt",
       c.store.wakes and c.store.wakes[0] == _NUDGE_PROMPT)
_check("nudge counter bumped", c._nudge == 1)

print("Unattended dead-end (self-wake fire) -> nudge scheduled")
c = _run("wake", "Plan: step 1, step 2. I'll get to it.")
_check("self-wake source is also unattended", len(c.store.wakes) == 1)

print("Unattended turn WITH [TURN_COMPLETE] -> no nudge")
c = _run("cooldown", "Refactor finished and committed.\n[TURN_COMPLETE]")
_check("no nudge when marker present", len(c.store.wakes) == 0)

print("Attended (user_message) dead-end -> no nudge")
c = _run("user_message", "Next I'll read the roadmap.")
_check("attended turn never nudged", len(c.store.wakes) == 0)

print("Default 'system' source -> not unattended, no nudge")
c = _run("system", "Next I'll do stuff.")
_check("bare system source never nudged", len(c.store.wakes) == 0)

print("Unattended build/worktree session -> no nudge (branch gate)")
c = _run("cooldown", "Next I'll do stuff.", branch="claude-bot/t-1")
_check("worktree session excluded from nudge", len(c.store.wakes) == 0)

print("Unattended + context-exhausted -> handoff notice, no nudge")
c = _run("cooldown", "Next steps...", warn=True)
_check("no nudge when out of context", len(c.store.wakes) == 0)
_check("user told to start a fresh thread",
       any("out of context" in s for s in c.messenger.sent))

print("Nudge cap -> stop notice instead of another nudge")
c = _run("wake", "still planning...", nudge=config.MAX_CONSEC_NUDGES)
_check("no further nudge past the cap", len(c.store.wakes) == 0)
_check("stop notice surfaced",
       any("Stopped after" in s for s in c.messenger.sent))

print("Real wake scheduled this turn -> nudge counter reset")
_txt = (
    "Still building.\n"
    '[BOT_CMD: /wake delay=120 reason="poll deploy"]\n'
    "~~~wake\nre-check the deploy\n~~~"
)
c = _Ctx("wake", nudge=5)
asyncio.run(check_wake_request(c, _Inst(), final_text=_txt))
_check("real wake resets nudge counter", c._nudge == 0)
_check("real wake actually scheduled",
       len(c.store.wakes) == 1 and "re-check the deploy" in c.store.wakes[0])


if _failures:
    print(f"\n{len(_failures)} case(s) FAILED:")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nAll cases passed.")
