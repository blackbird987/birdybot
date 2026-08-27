"""Regression test: a usage-limit turn must still bind its session to the thread.

Background (2026-08-27, three ev-nova children). All three hit the account's
weekly limit, were auto-retried on the backup account, did hours of real work,
armed a /watch, and then their wake-ups were dropped with
"thread gone/sessionless". Cause: `commands._execute_query` returned early to
schedule the cooldown retry BEFORE the line that writes the session_id onto
ThreadInfo, and the retry path (`app._do_cooldown_retry_locked` ->
`lifecycle.run_instance`) never binds at all. So the thread never learned which
session it was talking to, and every later resume path — next user message,
self-wake, fired /watch — had nothing to resume.

Locks five things:
  1. should_bind_session: success and usage-limit bind; every other error
     doesn't (a crashed run can emit a FRESH session id whose adoption would
     amputate the thread's history).
  2. bind_thread_session actually reaches ForumManager.set_thread_session and
     mutates ThreadInfo — using the real production objects.
  3. Source order in _execute_query: the bind is above the cooldown
     early-return. This is the exact regression; a future edit that swaps them
     back reintroduces the silent overnight loss.
  4. backfill_thread_session fills an EMPTY binding and never rebinds, and
     refuses an isolated worktree build outright.
  5. Every direct run_instance caller outside the chain runner backfills
     (cooldown retry, /retry, the Retry button, continue-on-pay-per-use).
     run_instance never binds, so one of these added without a backfill
     silently reintroduces the overnight loss. The chain runner is excluded
     on purpose: a step's session belongs to the step, not the conversation.

Run: python scripts/test_cooldown_session_bind.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.claude.types import Instance, InstanceStatus, InstanceType, RunResult
from bot.discord.forums import ForumManager, ForumProject, RebindResult, ThreadInfo
from bot.engine import lifecycle

_failures: list[str] = []


def _check(label: str, cond: bool) -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


# ---------------------------------------------------------------- 1. predicate

print("should_bind_session — which turns may claim the thread's session")

_reset = datetime.now(timezone.utc) + timedelta(hours=1)

_check(
    "clean success binds",
    lifecycle.should_bind_session(RunResult(session_id="s-ok")),
)
_check(
    "usage-limit failure binds (the retry resumes this exact session)",
    lifecycle.should_bind_session(
        RunResult(session_id="s-limit", is_error=True, usage_limit_reset=_reset),
    ),
)
_check(
    "ordinary crash does NOT bind",
    not lifecycle.should_bind_session(
        RunResult(session_id="s-crash", is_error=True, error_message="boom"),
    ),
)
_check(
    "recovery-exhausted crash does NOT bind",
    not lifecycle.should_bind_session(
        RunResult(session_id="s-fresh", is_error=True, session_recovery_exhausted=True),
    ),
)
_check(
    "a recovered session that THEN hit the limit still binds "
    "(old id is already unreachable; the retry resumes the new one)",
    lifecycle.should_bind_session(
        RunResult(session_id="s-new", is_error=True, usage_limit_reset=_reset,
                  session_recovery_exhausted=True),
    ),
)
_check(
    "no session id -> nothing to bind",
    not lifecycle.should_bind_session(RunResult(session_id=None)),
)
_check(
    "no session id, even on a usage limit",
    not lifecycle.should_bind_session(
        RunResult(session_id=None, is_error=True, usage_limit_reset=_reset),
    ),
)


# ------------------------------------------------------- 2. the bind lands

print()
print("bind_thread_session — reaches ThreadInfo through the real ForumManager")


class _FakeStore:
    """The one attribute bind_thread_session touches."""

    active_session_id = None


def _make_forums() -> tuple[ForumManager, ThreadInfo]:
    fm = ForumManager.__new__(ForumManager)   # skip discord.Client wiring
    fm._store = _FakeStore()
    fm._prime_cache = {}
    info = ThreadInfo(thread_id="t-1", session_id=None)
    proj = ForumProject(repo_name="bot", forum_channel_id="f-1",
                        threads={"t-1": info})
    fm._forum_projects = {"bot": proj}
    fm.save_forum_map = lambda: None          # no state.json writes in a test
    return fm, info


class _Ctx:
    """Minimal RequestContext stand-in: the fields the binder reads."""

    def __init__(self) -> None:
        self.session_id = None
        self.store = _FakeStore()
        self.on_session_resolved = None
        self.resolve_session_id = None


_fm, _info = _make_forums()
_ctx = _Ctx()


def _make_inst(iid: str, repo: str) -> Instance:
    return Instance(
        id=iid, name=None, instance_type=InstanceType.QUERY, prompt="p",
        repo_name=repo, repo_path="/tmp/repo", status=InstanceStatus.COMPLETED,
    )


_inst = _make_inst("q-1", "bot")


async def _resolved(sid, repo_name):
    _resolved.result = _fm.set_thread_session("t-1", sid, repo_name)


_resolved.result = None
_ctx.on_session_resolved = _resolved

asyncio.run(lifecycle.bind_thread_session(_ctx, _inst, "s-limit"))
_check("ThreadInfo.session_id written", _info.session_id == "s-limit")
_check("rebind accepted", _resolved.result is RebindResult.ACCEPTED)

# Re-binding the same id is a no-op, not an error.
asyncio.run(lifecycle.bind_thread_session(_ctx, _inst, "s-limit"))
_check("same session id -> NOOP", _resolved.result is RebindResult.NOOP_SAME_SESSION)

# A cross-repo id is still refused — the widened bind must not weaken this.
_inst_other = _make_inst("q-2", "some-other-repo")
asyncio.run(lifecycle.bind_thread_session(_ctx, _inst_other, "s-alien"))
_check("cross-repo rebind still refused",
       _resolved.result is RebindResult.REJECTED_REPO_MISMATCH)
_check("cross-repo attempt left the binding intact", _info.session_id == "s-limit")

# Empty session id is a silent no-op (no callback fired).
_resolved.result = None
asyncio.run(lifecycle.bind_thread_session(_ctx, _inst, None))
_check("None session id fires no callback", _resolved.result is None)


# --------------------------------------------- 3. backfill: fill a gap, never rebind

print()
print("backfill_thread_session — fills an empty binding only")

_fm2, _info2 = _make_forums()
_ctx2 = _Ctx()
_ctx2.resolve_session_id = lambda: _info2.session_id or None


async def _resolved2(sid, repo_name):
    _fm2.set_thread_session("t-1", sid, repo_name)


_ctx2.on_session_resolved = _resolved2

_ran = _make_inst("q-10", "bot")
_ran.session_id = "s-retry"

_check(
    "empty thread gets filled",
    asyncio.run(lifecycle.backfill_thread_session(_ctx2, _ran)) is True,
)
_check("…and the id landed on ThreadInfo", _info2.session_id == "s-retry")

_other = _make_inst("q-11", "bot")
_other.session_id = "s-different"
_check(
    "a bound thread is NEVER rebound (a chain step must not amputate chat history)",
    asyncio.run(lifecycle.backfill_thread_session(_ctx2, _other)) is False,
)
_check("…and the original binding survives", _info2.session_id == "s-retry")

# Worktree build into an EMPTY thread: still refused.
_fm3, _info3 = _make_forums()
_ctx3 = _Ctx()
_ctx3.resolve_session_id = lambda: _info3.session_id or None


async def _resolved3(sid, repo_name):
    _fm3.set_thread_session("t-1", sid, repo_name)


_ctx3.on_session_resolved = _resolved3

_build = _make_inst("b-1", "bot")
_build.session_id = "s-build"
_build.worktree_path = "/tmp/repo/.worktrees/b-1"
_check(
    "an isolated worktree build never becomes the thread's chat session",
    asyncio.run(lifecycle.backfill_thread_session(_ctx3, _build)) is False,
)
_check("…thread left empty", _info3.session_id is None)

_nosess = _make_inst("q-12", "bot")
_check(
    "no session id -> nothing to fill",
    asyncio.run(lifecycle.backfill_thread_session(_ctx3, _nosess)) is False,
)

# A context with no resolve_session_id isn't thread-bound (non-Discord, or a
# ctx nobody attached callbacks to) — silently do nothing.
_ctx4 = _Ctx()
_check(
    "unattached context -> no-op",
    asyncio.run(lifecycle.backfill_thread_session(_ctx4, _ran)) is False,
)


# ------------------------------------------------- 4. source order in _execute_query

print()
print("_execute_query — bind sits ABOVE the cooldown early-return")


def _func_body(source: str, header: str) -> str:
    """Slice one top-level function out of a module, header to next def.

    Scanning to end-of-file instead would let a match in a LATER function
    satisfy a check about this one.
    """
    start = source.find(header)
    if start == -1:
        return ""
    rest = source[start + len(header):]
    end = re.search(r"^(?:async def |def |class )", rest, re.MULTILINE)
    return rest[:end.start()] if end else rest


_src = _func_body(
    open(os.path.join(_ROOT, "bot", "engine", "commands.py"),
         encoding="utf-8").read(),
    "async def _execute_query(",
)
_check("_execute_query located", bool(_src))
_i_bind = _src.find("lifecycle.should_bind_session(result)")
_i_cooldown = _src.find("lifecycle.schedule_cooldown_retry(ctx, inst, result)")
_check("bind callsite present", _i_bind != -1)
_check("cooldown callsite present", _i_cooldown != -1)
_check(
    "bind precedes the cooldown return (else a limited turn is never bound)",
    _i_bind != -1 and _i_cooldown != -1 and _i_bind < _i_cooldown,
)


# ----------------------------------- 5. every direct run_instance caller backfills

print()
print("direct run_instance callers — each tops up a sessionless thread")

_app = open(os.path.join(_ROOT, "bot", "app.py"), encoding="utf-8").read()
_cmds = open(os.path.join(_ROOT, "bot", "engine", "commands.py"),
             encoding="utf-8").read()
_flows = open(os.path.join(_ROOT, "bot", "engine", "workflows.py"),
              encoding="utf-8").read()

_body = _func_body(_app, "async def _do_cooldown_retry_locked(")
_check("_do_cooldown_retry_locked located", bool(_body))
_check("cooldown retry attaches session callbacks (else it cannot bind at all)",
       "attach_session_callbacks" in _body)


def _callers_missing_backfill(source: str) -> list[int]:
    """run_instance call sites with no backfill within the following window.

    Conversation-owning callers must top up a sessionless thread themselves —
    run_instance never will.  A NEW direct caller added later without one
    silently reintroduces the overnight loss, so this is checked structurally
    rather than site-by-site.
    """
    lines = source.split("\n")
    missing = []
    for n, line in enumerate(lines):
        if "lifecycle.run_instance(" not in line:
            continue
        window = "\n".join(lines[n:n + 12])
        if "backfill_thread_session" not in window:
            missing.append(n + 1)
    return missing


for _mod, _text, _expect in (("bot/app.py", _app, 1),
                             ("bot/engine/commands.py", _cmds, 3)):
    _sites = _text.count("lifecycle.run_instance(")
    _check(f"{_mod}: found the expected {_expect} direct run_instance caller(s)",
           _sites == _expect)
    _gaps = _callers_missing_backfill(_text)
    _check(f"{_mod}: every direct run_instance caller backfills the thread session",
           not _gaps)
    if _gaps:
        print(f"        unbacked call sites at lines: {_gaps}")

_check(
    "workflows.py deliberately does NOT backfill (a step's session is the "
    "step's, not the conversation's)",
    "backfill_thread_session" not in _flows,
)

_check(
    "self-wake no longer lumps sessionless in with gone",
    "gone/sessionless" not in _app and "alive but sessionless" in _app,
)


# ---- Summary ----
print()
if _failures:
    print(f"FAILED ({len(_failures)}):")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("All cases passed.")
sys.exit(0)
