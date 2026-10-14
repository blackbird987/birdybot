"""Regression test: chain/background turns dispatch BOT_CMD directives (t-5944).

Background: lifecycle.run_instance (workflow chain steps, background tasks,
spawned children) delivered results but never ran the post-turn
[BOT_CMD: /repo|/spawn] scan that the chat path (commands.on_query tail) runs,
so a directive emitted by a Build/Review step was rendered as inert text —
no dispatch, no refusal notice. run_instance now calls
commands._execute_bot_commands after result delivery.

Drives the REAL run_instance end-to-end with fakes only at the platform
seams (messenger, runner, store):
- a successful chain turn whose result_text carries 2 directive+body pairs
  -> both children spawned, in order, audit list updated
- autopilot running -> no spawn, but an EXPLICIT refusal notice is posted
  (the silent-swallow this fix removes)
- error result -> no scan, no spawn
- directive-free result -> no spawn, no notices

Plus the /chain directive's per-session queue (bot/engine/directives/chain.py):
- a paused chain on the session is superseded, not refused
- two /chain directives before the first starts run ONE chain, newest plan
- a /chain during a running chain runs right after it, one drain task
- no stored chain -> launches with no supersede note
- the drainer set is empty afterwards, even when the chain raises

Run: python scripts/test_chain_botcmd.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from bot import config
from bot.claude.types import Instance, InstanceOrigin, RunResult
from bot.engine import lifecycle
from bot.platform.base import RequestContext, SpawnResult

# run_instance's turn-end hooks (check_reboot_request) read config paths at
# call time against the REAL data dir. A test run while a real reboot request
# was pending consumed and DELETED it (read -> unlink -> crash on the fake
# runner's missing request_reboot), silently cancelling a queued production
# reboot. Redirect the reboot files to a throwaway dir so this test can never
# touch live bot state.
_TMP_DATA = Path(tempfile.mkdtemp(prefix="chain-botcmd-test-"))
config.REBOOT_REQUEST_FILE = _TMP_DATA / "reboot_request.json"
config.REBOOT_REQUEST_DEFERRED_FILE = _TMP_DATA / "reboot_request.deferred.json"

_failures: list[str] = []


def _check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


class _FakeMessenger:
    """Captures sends; any messenger method not defined is an async no-op."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, channel_id, text, *a, **kw):
        self.sent.append(text)
        return "msg-txt"

    async def send_result(self, *a, **kw):
        return "msg-res"

    def escape(self, text):
        return text

    def markdown_to_markup(self, text):
        return text

    def chunk_message(self, text):
        return [text]

    def format_mention(self, uid):
        return f"@{uid}"

    def __getattr__(self, name):
        async def _noop(*a, **kw):
            return None
        return _noop


def _make_env(
    *,
    result_text: str,
    is_error: bool = False,
    autopilot_status: str | None = None,
):
    """Real RequestContext + real Instance; fakes at the platform seams."""
    messenger = _FakeMessenger()
    spawned: list = []

    inst = Instance(
        id="t-chain", name=None,
        instance_type="task", prompt="x",  # type: ignore[arg-type]
        repo_name="bot", repo_path="", status="running",  # type: ignore[arg-type]
        # review_plan is in _SKIP_HISTORY_ORIGINS — keeps the harness away
        # from the history file without changing the code path under test.
        origin=InstanceOrigin.REVIEW_PLAN,
    )

    chain_meta = {"status": autopilot_status} if autopilot_status else None
    store = SimpleNamespace(
        update_instance=lambda i, critical=False: None,
        list_instances=lambda all_=False: [],
        list_by_status=lambda status: [],
        context="",
        add_cost=lambda c: None,
        get_autopilot_chain_meta=lambda sid: chain_meta,
        get_autopilot_chain=lambda sid: None,
        list_repos=lambda: ["bot"],
        get_instance=lambda iid: inst,
        get_daily_cost=lambda: 0.0,
    )

    result = RunResult(
        session_id="sess-1",
        result_text=result_text,
        is_error=is_error,
        error_message="boom" if is_error else "",
    )

    async def _run(inst_, **kw):
        return result

    runner = SimpleNamespace(
        begin_task=lambda iid, session_id=None, channel_id=None: None,
        end_task=lambda iid: None,
        run=_run,
        active_instance_for_session=lambda sid: inst.id,
        active_instance_for_channel=lambda cid: inst.id,
    )

    ctx = RequestContext(
        messenger=messenger,
        channel_id="chan-1",
        platform="discord",
        store=store,
        runner=runner,
        session_id="sess-1",
        repo_name="bot",
    )

    async def _spawn(args):
        spawned.append(args)
        return SpawnResult(
            thread_id=f"thread-{len(spawned)}",
            thread_mention="<#x>", thread_url=None,
        )

    ctx.spawn_session = _spawn
    ctx.read_spawn_wave_count = lambda: 0
    return ctx, inst, messenger, spawned


_TWO_DIRECTIVES = (
    "Fanning out two sessions.\n\n"
    '[BOT_CMD: /spawn repo=bot title="Kid A" mode=build]\n'
    "~~~spawn\nPrompt A\n~~~\n"
    '[BOT_CMD: /spawn repo=bot title="Kid B" mode=explore]\n'
    "~~~spawn\nPrompt B\n~~~\n"
)


def test_chain_turn_dispatches() -> None:
    print("chain turn with 2 directives dispatches both:")
    ctx, inst, msgr, spawned = _make_env(result_text=_TWO_DIRECTIVES)
    asyncio.run(lifecycle.run_instance(ctx, inst))
    _check(len(spawned) == 2, "both directives spawned")
    _check(
        [s.title for s in spawned] == ["Kid A", "Kid B"],
        "spawned in order of appearance",
    )
    _check(
        inst.spawn_dispatched_thread_ids == ["thread-1", "thread-2"],
        "audit list records both children",
    )


def test_autopilot_gate_posts_notice() -> None:
    print("autopilot running -> explicit refusal, no spawn:")
    ctx, inst, msgr, spawned = _make_env(
        result_text=_TWO_DIRECTIVES, autopilot_status="running",
    )
    asyncio.run(lifecycle.run_instance(ctx, inst))
    _check(len(spawned) == 0, "no children spawned")
    _check(
        any("/spawn refused" in m and "autopilot" in m for m in msgr.sent),
        "refusal notice posted (not silently swallowed)",
    )


def test_error_result_not_scanned() -> None:
    print("error result is not scanned:")
    ctx, inst, msgr, spawned = _make_env(
        result_text=_TWO_DIRECTIVES, is_error=True,
    )
    asyncio.run(lifecycle.run_instance(ctx, inst))
    _check(len(spawned) == 0, "no spawn from an error turn")


def test_plain_result_no_noise() -> None:
    print("directive-free result stays quiet:")
    ctx, inst, msgr, spawned = _make_env(result_text="all done, no fan-out")
    asyncio.run(lifecycle.run_instance(ctx, inst))
    _check(len(spawned) == 0, "nothing spawned")
    _check(
        not any("/spawn" in m for m in msgr.sent),
        "no spawn notices for a plain result",
    )


# --- /chain directive queue ------------------------------------------------

from datetime import datetime, timedelta, timezone

from bot.engine import prior_art, workflows
from bot.engine.cmd_state import _get_channel_lock
from bot.engine.directives import chain as chain_mod

prior_art.collect = lambda repo_path, plan_text, **kw: ""


def _chain_text(plan: str, preset: str = "ship") -> str:
    return f"Kicking it off.\n\n[BOT_CMD: /chain preset={preset}]\n~~~plan\n{plan}\n~~~\n"


class _ChainStore:
    def __init__(self, chain=None, meta=None) -> None:
        self.chain = chain
        self.meta = meta
        self.cleared = 0
        self.overrides: list[str] = []

    def get_autopilot_chain(self, sid):
        return self.chain

    def get_autopilot_chain_meta(self, sid):
        return self.meta

    def clear_chain_state(self, sid):
        self.cleared += 1
        self.chain = None
        self.meta = None

    def set_chain_plan_override(self, sid, plan):
        self.overrides.append(plan)


def _chain_env(channel: str, store: _ChainStore):
    msgr = _FakeMessenger()
    ctx = RequestContext(
        messenger=msgr, channel_id=channel, platform="discord",
        store=store, runner=SimpleNamespace(), session_id="sess-c",
        repo_name="bot",
    )
    src = SimpleNamespace(id="q-src", session_id="sess-c", repo_path="")
    return ctx, msgr, src


class _FakeChains:
    """Stands in for workflows.on_conversational_chain (facade-patchable)."""

    def __init__(self, store: _ChainStore, gate: asyncio.Event | None = None,
                 raise_on_call: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.store = store
        self.gate = gate
        self.raise_on_call = raise_on_call

    async def __call__(self, ctx, source_id, preset, source_msg_id=None):
        self.calls.append((preset, self.store.overrides[-1] if self.store.overrides else ""))
        if self.gate is not None:
            await self.gate.wait()
        if self.raise_on_call:
            raise RuntimeError("chain blew up")
        return None


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)
    while chain_mod._chain_drainers:
        await asyncio.sleep(0.01)


def _with_fake(fake, coro_fn) -> None:
    orig = workflows.on_conversational_chain
    workflows.on_conversational_chain = fake
    try:
        asyncio.run(coro_fn())
    finally:
        workflows.on_conversational_chain = orig
        chain_mod._pending_chains.clear()


def test_chain_supersedes_paused() -> None:
    print("/chain over a paused chain supersedes it:")
    twelve_h = (datetime.now(timezone.utc) - timedelta(hours=12, minutes=5)).isoformat()
    store = _ChainStore(
        chain=["build", "sensors", "review_code", "verify"],
        meta={"status": "paused", "updated_at": twelve_h},
    )
    ctx, msgr, src = _chain_env("chan-sup", store)
    fake = _FakeChains(store)

    async def go():
        await chain_mod._handle_chain_directive(ctx, _chain_text("PLAN NEW"), src)
        await _settle()

    _with_fake(fake, go)
    _check(not any("already running" in m for m in msgr.sent), "no refusal notice")
    _check(store.cleared == 1, "stored chain state cleared once")
    _check(store.overrides == ["PLAN NEW"], "override set to the new plan")
    _check(fake.calls == [("ship", "PLAN NEW")], "one chain launched")
    _check(
        any("Replaced a paused chain from 12h ago (stopped at build)" in m
            for m in msgr.sent),
        "supersede note reads '12h ago (stopped at build)'",
    )
    _check(not chain_mod._chain_drainers, "drainer set empty")


def test_chain_coalesces_before_start() -> None:
    print("two /chain directives before the first starts -> one chain:")
    store = _ChainStore()
    ctx, msgr, src = _chain_env("chan-coal", store)
    fake = _FakeChains(store)

    async def go():
        lock = _get_channel_lock(ctx.channel_id)
        async with lock:
            await chain_mod._handle_chain_directive(ctx, _chain_text("PLAN ONE", "ship"), src)
            await chain_mod._handle_chain_directive(ctx, _chain_text("PLAN TWO", "verify"), src)
            for _ in range(10):
                await asyncio.sleep(0)
            _check(fake.calls == [], "nothing runs while the lock is held")
        await _settle()

    _with_fake(fake, go)
    _check(fake.calls == [("verify", "PLAN TWO")], "exactly one chain, newest plan and preset")
    _check(store.cleared == 0, "no supersede when nothing was stored")
    _check(
        any("Got the newer plan" in m for m in msgr.sent),
        "second directive acknowledged",
    )
    _check(not chain_mod._chain_drainers, "drainer set empty")


def test_chain_queues_during_run() -> None:
    print("/chain during a running chain runs after it:")
    store = _ChainStore()
    ctx, msgr, src = _chain_env("chan-queue", store)

    async def go():
        gate = asyncio.Event()
        fake.gate = gate
        await chain_mod._handle_chain_directive(ctx, _chain_text("PLAN A"), src)
        for _ in range(20):
            await asyncio.sleep(0)
        _check(fake.calls == [("ship", "PLAN A")], "first chain running")
        n_tasks = len(asyncio.all_tasks())
        await chain_mod._handle_chain_directive(ctx, _chain_text("PLAN B", "hold"), src)
        _check(len(asyncio.all_tasks()) == n_tasks, "no second drain task")
        _check(len(fake.calls) == 1, "second plan waits for the first")
        gate.set()
        await _settle()

    fake = _FakeChains(store)
    _with_fake(fake, go)
    _check(
        fake.calls == [("ship", "PLAN A"), ("hold", "PLAN B")],
        "both ran, in order",
    )
    _check(not chain_mod._chain_drainers, "drainer set empty")


def test_chain_fresh_launch() -> None:
    print("/chain with no stored chain launches quietly:")
    store = _ChainStore()
    ctx, msgr, src = _chain_env("chan-fresh", store)
    fake = _FakeChains(store)

    async def go():
        await chain_mod._handle_chain_directive(ctx, _chain_text("PLAN F"), src)
        await _settle()

    _with_fake(fake, go)
    _check(fake.calls == [("ship", "PLAN F")], "chain launched")
    _check(not any("Replaced" in m for m in msgr.sent), "no supersede note")
    _check(any("Starting **ship** chain" in m for m in msgr.sent), "start notice sent")


def test_chain_raise_clears_drainer() -> None:
    print("a chain that raises still releases the drainer:")
    store = _ChainStore()
    ctx, msgr, src = _chain_env("chan-raise", store)
    fake = _FakeChains(store, raise_on_call=True)

    async def go():
        await chain_mod._handle_chain_directive(ctx, _chain_text("PLAN X"), src)
        await _settle()

    _with_fake(fake, go)
    _check(len(fake.calls) == 1, "chain was attempted")
    _check(not chain_mod._chain_drainers, "drainer set empty after a raise")


def main() -> int:
    test_chain_turn_dispatches()
    test_autopilot_gate_posts_notice()
    test_error_result_not_scanned()
    test_plain_result_no_noise()
    test_chain_supersedes_paused()
    test_chain_coalesces_before_start()
    test_chain_queues_during_run()
    test_chain_fresh_launch()
    test_chain_raise_clears_drainer()
    if _failures:
        print(f"\n{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("\nall chain-botcmd tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
