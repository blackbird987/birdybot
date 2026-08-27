"""Regression test: a usage-limit turn must still bind its session to the thread.

Background (2026-08-27, three ev-nova children). All three hit the account's
weekly limit, were auto-retried on the backup account, did hours of real work,
armed a /watch, and then their wake-ups were dropped with
"thread gone/sessionless". Cause: `commands._run_query` returned early to
schedule the cooldown retry BEFORE the line that writes the session_id onto
ThreadInfo, and the retry path (`app._do_cooldown_retry_locked` ->
`lifecycle.run_instance`) never binds at all. So the thread never learned which
session it was talking to, and every later resume path — next user message,
self-wake, fired /watch — had nothing to resume.

Locks three things:
  1. should_bind_session: success and usage-limit bind; every other error
     doesn't (a crashed run can emit a FRESH session id whose adoption would
     amputate the thread's history).
  2. bind_thread_session actually reaches ForumManager.set_thread_session and
     mutates ThreadInfo — using the real production objects.
  3. Source order in _run_query: the bind is above the cooldown early-return.
     This is the exact regression; a future edit that swaps them back
     reintroduces the silent overnight loss.

Run: python scripts/test_cooldown_session_bind.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import os
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
    "recovery-exhausted fresh session does NOT bind",
    not lifecycle.should_bind_session(
        RunResult(session_id="s-fresh", is_error=True, session_recovery_exhausted=True),
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
    """Only the two attributes bind_thread_session touches."""

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
        self.channel_id = "t-1"


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


# ------------------------------------------------- 3. source order in _run_query

print()
print("_run_query — bind sits ABOVE the cooldown early-return")

_src = open(os.path.join(_ROOT, "bot", "engine", "commands.py"),
            encoding="utf-8").read()
_i_bind = _src.find("lifecycle.should_bind_session(result)")
_i_cooldown = _src.find("lifecycle.schedule_cooldown_retry(ctx, inst, result)")
_check("bind callsite present", _i_bind != -1)
_check("cooldown callsite present", _i_cooldown != -1)
_check(
    "bind precedes the cooldown return (else a limited turn is never bound)",
    _i_bind != -1 and _i_cooldown != -1 and _i_bind < _i_cooldown,
)


# --------------------------------------------- 4. the retry path wires callbacks

print()
print("_do_cooldown_retry_locked — attaches callbacks and re-binds after the run")

_app = open(os.path.join(_ROOT, "bot", "app.py"), encoding="utf-8").read()
_body = _app[_app.find("async def _do_cooldown_retry_locked"):]
_check("attaches session callbacks",
       "attach_session_callbacks" in _body)
_check("re-binds after run_instance",
       "bind_thread_session" in _body)
_check("worktree builds excluded from the re-bind",
       "not new_inst.worktree_path" in _body)

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
