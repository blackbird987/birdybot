"""A workflow button pressed during a reboot drain is queued, not crashed.

``spawn_from`` refuses to spawn while the runner drains for a reboot and,
instead, queues the button press so it replays after the restart -- unless the
session has an autopilot chain, whose own persisted state resumes the whole
chain (queueing the single step as well would make the chain resume skip the
thread).  Telling those apart needs the session id, held in ``check_session``.

42907fd (2026-09-09) deleted ``check_session`` together with the
``check_spawn_allowed`` argument it used to feed, and the drain branch went on
reading it: every button pressed during a drain raised NameError, so the press
was neither queued nor answered.  Nothing noticed, because a drain lasts
seconds and only happens around a reboot.

Run: python scripts/test_spawn_during_drain.py   (exit 0 on pass)
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import os
import sys
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bot.claude.types import InstanceOrigin, InstanceType
from bot.engine import workflows


_failures: list[str] = []


def _check(cond: bool, label: str) -> None:
    print(f"  {'ok:  ' if cond else 'FAIL:'} {label}")
    if not cond:
        _failures.append(label)


@dataclass
class _Source:
    session_id: str | None = "sess-1"
    repo_name: str = "OfficeBot"
    user_id: str = "u1"
    user_name: str = "owner"
    is_owner_session: bool = True


@dataclass
class _Store:
    source: _Source
    chains: dict[str, list[str]] = field(default_factory=dict)

    def get_instance(self, _id):
        return self.source

    def get_autopilot_chain(self, session_id):
        return self.chains.get(session_id) if session_id else None


@dataclass
class _Runner:
    replayed: list[dict] = field(default_factory=list)
    is_draining: bool = True

    def active_instance_for_channel(self, _cid):
        return None

    def check_spawn_allowed(self):
        return "Reboot in progress — try again shortly."

    def queue_for_replay(self, item: dict) -> None:
        self.replayed.append(item)


@dataclass
class _Messenger:
    sent: list[str] = field(default_factory=list)

    async def send_text(self, _cid, text, **_kw):
        self.sent.append(text)


@dataclass
class _Ctx:
    store: _Store
    runner: _Runner
    messenger: _Messenger = field(default_factory=_Messenger)
    channel_id: str = "chan-1"
    platform: str = "discord"
    user_id: str | None = None
    user_name: str | None = None


def _cfg(resume_session: bool) -> workflows.SpawnConfig:
    return workflows.SpawnConfig(
        instance_type=InstanceType.QUERY, prompt="p", mode="build",
        origin=InstanceOrigin.REVIEW_CODE, resume_session=resume_session,
    )


def _press(chains: dict, resume_session: bool) -> _Ctx:
    ctx = _Ctx(store=_Store(_Source(), chains), runner=_Runner())
    try:
        out = asyncio.run(workflows.spawn_from(ctx, "src-1", _cfg(resume_session)))
    except NameError as e:  # the regression, named rather than a traceback
        _failures.append(f"spawn_from raised NameError: {e}")
        print(f"  FAIL: spawn_from raised NameError: {e}")
        return ctx
    _check(out is None, "no instance is spawned during a drain")
    _check(
        any("auto-resume after restart" in t for t in ctx.messenger.sent),
        "the user is told the action resumes after the restart",
    )
    return ctx


def case_no_chain_is_queued() -> None:
    print("\n[no autopilot chain: the press is queued for replay]")
    ctx = _press({}, resume_session=True)
    _check(len(ctx.runner.replayed) == 1, "exactly one replay queued")
    if ctx.runner.replayed:
        item = ctx.runner.replayed[0]
        _check(item.get("instance_id") == "src-1", "replay names the source instance")
        _check(item.get("action") == InstanceOrigin.REVIEW_CODE.value,
               "replay names the button that was pressed")


def case_chain_is_not_queued() -> None:
    print("\n[active autopilot chain: the chain's own state resumes it]")
    ctx = _press({"sess-1": ["review_code", "verify"]}, resume_session=True)
    _check(ctx.runner.replayed == [], "no single-step replay is queued")


def case_non_resuming_step_ignores_chain() -> None:
    # Pre-regression semantics (64043b8): only a step that resumes the source
    # session is covered by that session's chain.
    print("\n[step that does not resume the session: queued even with a chain]")
    ctx = _press({"sess-1": ["verify"]}, resume_session=False)
    _check(len(ctx.runner.replayed) == 1, "replay queued")


if __name__ == "__main__":
    case_no_chain_is_queued()
    case_chain_is_not_queued()
    case_non_resuming_step_ignores_chain()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)}")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print("PASS: spawn during drain (3 cases)")
    sys.exit(0)
