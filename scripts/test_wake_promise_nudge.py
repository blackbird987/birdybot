"""Regression test for the promise-without-a-directive nudge.

Background: a turn's process EXITS when the turn ends. A turn that tells the
user "I'll report back when the tests finish" and arms neither a
``[BOT_CMD: /wake]`` nor a ``[BOT_CMD: /watch]`` leaves the thread with nothing
that can resume it, and the promised message never arrives. Observed
2026-08-31 in a thread whose session had run the test suite, promised results,
and left zero watches and zero pending wakes behind — it simply died.

``lifecycle.check_wake_request`` now catches that case and re-invokes the
session once with ``_PROMISE_NUDGE_PROMPT`` so the SESSION arms a real
directive. This test locks the two halves that matter:

* the detector (``lifecycle.promises_continuation``) fires on a real promise
  and not on prose that merely quotes or discusses one, and
* the nudge is a NUDGE, never a wake: it re-invokes, it respects the shared
  nudge cap, it stands down whenever the thread already has something to
  resume it (an armed watch, a pending wake), and it never fires from a
  build/worktree session.

The history this must not undo: an earlier ``WAKE_PROMISE_RE`` *scheduled* a
3-minute wake off this same prose and fired phantom re-checks on text that
only discussed a build. That is why every assertion below is about who gets
re-invoked, and none is about a poll being scheduled off prose.

Calls the real production ``check_wake_request`` with lightweight stubs so the
test can't drift from what the engine actually does.

Run: python scripts/test_wake_promise_nudge.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot import config
from bot.engine.lifecycle import (
    _PROMISE_NUDGE_PROMPT,
    check_wake_request,
    claims_self_wake,
    promises_continuation,
)

_failures: list[str] = []
_checks = 0


def _check(label: str, cond: bool) -> None:
    global _checks
    _checks += 1
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def _detect(text: str, expected: bool) -> None:
    got = promises_continuation(text)
    _check(f"want={expected!s:5} :: {text[:64]!r}", got == expected)


# ---- Stubs ---------------------------------------------------------------
class _Msgr:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, cid, text, silent=False) -> None:
        self.sent.append(text)


class _Store:
    """Enough StateStore to answer everything check_wake_request asks."""

    def __init__(self, watch=None, pending_wake=None) -> None:
        self.wakes: list[str] = []
        self.watches: list[object] = []
        self._watch = watch
        self._pending = pending_wake
        self.cancelled = 0

    def add_wake(self, prompt, channel_id, next_run_at, repo_name, repo_path) -> None:
        self.wakes.append(prompt)

    def watch_for_channel(self, channel_id):
        return self._watch

    def pending_wake_for_channel(self, channel_id):
        return self._pending

    def list_watches(self):
        return list(self.watches)

    def add_watch(self, watch):
        self.watches.append(watch)
        return watch

    def cancel_wakes(self, channel_id) -> int:
        self.cancelled += 1
        return 0


class _Ctx:
    def __init__(self, source="user_message", nudge=0, store=None) -> None:
        self.channel_id = "c1"
        self.source = source
        self.messenger = _Msgr()
        self.store = store or _Store()
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


def _run(text, *, source="user_message", branch=None, warn=False, nudge=0,
         watch=None, pending_wake=None):
    ctx = _Ctx(source, nudge=nudge, store=_Store(watch=watch, pending_wake=pending_wake))
    asyncio.run(check_wake_request(ctx, _Inst(branch, warn), final_text=text))
    return ctx


_PROMISE = "Tests are running. I'll report back when the tests finish."


# ---- Detection: real promises ------------------------------------------
print("A promise to continue after this turn must be detected")
_detect("I'll report back when the tests finish", True)
_detect("I'm monitoring the build and will update you", True)
_detect("Once CI completes I'll pull the numbers", True)
_detect("polling the job in the background", True)
_detect("I'm polling in the background", True)
_detect("I'll keep an eye on the deploy", True)
_detect("I'll check back once the backtest lands", True)
_detect("waiting for the suite to finish", True)
_detect("I will let you know when it's done", True)
_detect("I'll keep checking the log", True)

# ---- Detection: camouflage and near-misses -------------------------------
print("Prose that only quotes or discusses a promise must NOT be detected")
_detect("Never say ```\nI'll report back when the tests finish\n```", False)
_detect("The banned phrase is `I'll report back when it's done`.", False)
_detect('The guidance bans "I\'ll report back when the tests finish".', False)
_detect("Reply or tap a button when you want me to continue.", False)
_detect("I checked the deploy and it was clean.", False)
_detect("The tests finished and all 42 passed.", False)
_detect("I'll wait for your reply before touching it.", False)
_detect("Watch out for the None case in that branch.", False)
# The claim path's own trigger phrase is not a promise — a different check
# owns it, and both firing would double-report the same turn.
_detect("Self-wake queued (~4 min).", False)
# Real false positives found in 520 archived result files, all fixed by
# requiring "in the background" rather than a bare job noun.
_detect("Unarmed polling rules are clamped so they always run a check.", False)
_detect("However healthy the watching looks, a rule that has not run trips.", False)
_detect("Answer it by watching the original run, not by guessing.", False)

# ---- The nudge -----------------------------------------------------------
print("Promise with nothing armed -> exactly one nudge, no poll")
c = _run(_PROMISE)
_check("exactly one wake scheduled", len(c.store.wakes) == 1)
_check("it carries the promise-nudge prompt",
       c.store.wakes and c.store.wakes[0] == _PROMISE_NUDGE_PROMPT)
_check("nudge counter bumped (shared cap)", c._nudge == 1)
_check("user told the promise was unbacked",
       any("armed nothing" in s for s in c.messenger.sent))

print("Promise while a watch is already armed -> nothing scheduled")
c = _run(_PROMISE, watch=object())
_check("armed watch stands the nudge down", len(c.store.wakes) == 0)

print("Promise while a wake is already pending -> nothing scheduled")
c = _run(_PROMISE, pending_wake=object())
_check("pending wake stands the nudge down", len(c.store.wakes) == 0)

print("Promise from a build/worktree session -> nothing scheduled")
c = _run(_PROMISE, branch="claude-bot/t-1")
_check("worktree session excluded", len(c.store.wakes) == 0)

print("Promise from a context-exhausted session -> handoff notice, no nudge")
c = _run(_PROMISE, warn=True)
_check("no re-invoke when out of context", len(c.store.wakes) == 0)
_check("user told to start a fresh thread",
       any("out of context" in s for s in c.messenger.sent))

print("A turn that armed a real watch -> watch armed, no nudge")
_watch_txt = (
    "Kicked off the suite.\n"
    "I'll report back when the tests finish.\n"
    '[BOT_CMD: /watch pid=999999 log="artifacts/run.log" label="suite"]\n'
    "~~~watch\nread the tail of artifacts/run.log and report failures\n~~~"
)
c = _run(_watch_txt)
_check("watch armed", len(c.store.watches) == 1)
_check("no nudge on top of the watch", len(c.store.wakes) == 0)

print("A turn that armed a real wake -> wake armed, no nudge")
_wake_txt = (
    "Deploy is out.\n"
    "I'll report back when the deploy finishes.\n"
    '[BOT_CMD: /wake delay=300 reason="poll deploy"]\n'
    "~~~wake\ncheck the deploy status\n~~~"
)
c = _run(_wake_txt)
_check("exactly one wake, and it is the real one", len(c.store.wakes) == 1)
_check("the real prompt, not the nudge",
       c.store.wakes and "check the deploy status" in c.store.wakes[0])

print("Claim and promise together -> claim notice only, exactly one outcome")
_both = "Self-wake queued (~4 min). I'll report back when the tests finish."
_check("both detectors would fire on this text",
       claims_self_wake(_both) and promises_continuation(_both))
c = _run(_both)
_check("claim wins: nothing scheduled", len(c.store.wakes) == 0)
_check("exactly one message posted", len(c.messenger.sent) == 1)
_check("and it is the claim notice",
       c.messenger.sent and "no valid" in c.messenger.sent[0])

print("Nudge cap -> stop notice instead of another nudge")
c = _run(_PROMISE, nudge=config.MAX_CONSEC_NUDGES)
_check("no further nudge past the cap", len(c.store.wakes) == 0)
_check("stop notice surfaced",
       any("Stopped after" in s for s in c.messenger.sent))

print("A clean turn with neither -> silent, and the nudge counter resets")
c = _run("Fixed the parser and committed. Suite is green.", nudge=3)
_check("nothing scheduled", len(c.store.wakes) == 0)
_check("nothing posted", len(c.messenger.sent) == 0)
_check("nudge counter reset", c._nudge == 0)

print("An unattended dead-end still nudges (unattended path untouched)")
c = _run("Next I'll read the roadmap and re-verify.", source="cooldown")
_check("unattended dead-end still re-invoked", len(c.store.wakes) == 1)


if _failures:
    print(f"\n{len(_failures)} of {_checks} check(s) FAILED:")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"\nAll {_checks} checks passed.")
