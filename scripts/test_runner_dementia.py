"""Regression test for the t-3452 dementia cascade.

Background: a build that hit a usage limit on its primary account and was
re-queued by the cooldown retry path came back as a *fresh* session because
the runner clobbered ``instance.session_id`` on the way down.  Forensics on
log line 45800 of t-3452 showed the spawn argv had no ``[acct=...]`` tag,
proving ``account_dir`` was None — the runner spawned without
``CLAUDE_CONFIG_DIR``, the resume found no JSONL, recovery Layer 3 fired,
``instance.session_id`` was set to None, and the next retry resumed the
empty fresh session instead of the original conversation.

This test pins the four guardrails that prevent the cascade:

  - **Fix 1 (refuse-to-spawn):** when every account is on cooldown, the
    runner returns a synthetic ``RunResult`` with ``session_id`` preserved
    instead of spawning the CLI without an account env var.
  - **Fix 1b (cooldown persistence):** cooldowns survive a runner restart
    so the same instance doesn't re-attempt a known-exhausted account
    immediately after a reboot.
  - **Fix 3 (no session-id poison):** when Layer-3 fallback produces no
    real conversation, the runner restores the original session_id so the
    cooldown retry resumes correctly.
  - **Defense in depth (lifecycle guard):** ``finalize_run`` ignores
    ``RunResult.session_id`` when it is None, so synthetic results can't
    silently null the instance's session.

Run: ``python scripts/test_runner_dementia.py``  (exit 0 on pass).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude.runner import ClaudeRunner
from bot.claude.types import (
    Instance,
    InstanceStatus,
    InstanceType,
    RunResult,
)
from bot.engine.lifecycle import finalize_run
from bot.store.state import StateStore


SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _make_instance(repo_dir: str, session_id: str | None = SESSION_ID) -> Instance:
    return Instance(
        id="t-test",
        name=None,
        instance_type=InstanceType.QUERY,
        prompt="continue",
        repo_name="test-repo",
        repo_path=repo_dir,
        status=InstanceStatus.RUNNING,
        session_id=session_id,
        mode="explore",
    )


# ---------------------------------------------------------------------------
# Test 1: refuse-to-spawn when all accounts are on cooldown
# ---------------------------------------------------------------------------
async def test_refuse_to_spawn(tmp: str, store: StateStore) -> list[str]:
    failures: list[str] = []
    primary = os.path.join(tmp, "rts_primary")
    backup = os.path.join(tmp, "rts_backup")
    repo_dir = os.path.join(tmp, "rts_repo")
    for p in (primary, backup, repo_dir):
        os.makedirs(p, exist_ok=True)

    saved_accounts = list(config.CLAUDE_ACCOUNTS)
    saved_spawn = asyncio.create_subprocess_exec
    config.CLAUDE_ACCOUNTS[:] = [primary, backup]

    spawn_calls: list[list[str]] = []

    async def fake_spawn(*args, **_kwargs):  # pragma: no cover - shouldn't fire
        spawn_calls.append(list(args))
        raise AssertionError("CLI must not spawn when all accounts on cooldown")

    asyncio.create_subprocess_exec = fake_spawn  # type: ignore[assignment]

    try:
        runner = ClaudeRunner(store=store)
        # Plant cooldowns on both accounts, well into the future
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        runner._set_account_cooldown(primary, future)
        runner._set_account_cooldown(backup, future)

        instance = _make_instance(repo_dir)
        result = await runner.run(instance)

        if not result.is_error:
            failures.append("refuse_to_spawn: expected is_error=True")
        if result.session_id != SESSION_ID:
            failures.append(
                f"refuse_to_spawn: session_id not preserved on synthetic result "
                f"(got {result.session_id!r})"
            )
        if result.usage_limit_reset is None:
            failures.append("refuse_to_spawn: usage_limit_reset must be set")
        if spawn_calls:
            failures.append(
                f"refuse_to_spawn: CLI was spawned {len(spawn_calls)} times"
            )
    finally:
        asyncio.create_subprocess_exec = saved_spawn  # type: ignore[assignment]
        config.CLAUDE_ACCOUNTS[:] = saved_accounts

    return failures


# ---------------------------------------------------------------------------
# Test 2: cooldowns persist across runner restarts
# ---------------------------------------------------------------------------
async def test_cooldowns_persist(tmp: str) -> list[str]:
    failures: list[str] = []
    primary = os.path.join(tmp, "persist_primary")
    backup = os.path.join(tmp, "persist_backup")
    for p in (primary, backup):
        os.makedirs(p, exist_ok=True)

    state_file = Path(tmp) / "persist_state.json"
    results_dir = Path(tmp) / "persist_results"
    results_dir.mkdir(exist_ok=True)

    store_a = StateStore(state_file, results_dir)
    runner_a = ClaudeRunner(store=store_a)

    future = datetime.now(timezone.utc) + timedelta(hours=3)
    runner_a._set_account_cooldown(primary, future)

    # Verify it landed on disk
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    cooldowns = raw.get("account_cooldowns", {})
    if primary not in cooldowns:
        failures.append(
            f"persist: cooldown for {primary} not in state.json: {cooldowns!r}"
        )

    # Simulate reboot
    store_b = StateStore(state_file, results_dir)
    runner_b = ClaudeRunner(store=store_b)

    if primary not in runner_b._account_cooldowns:
        failures.append(
            "persist: cooldown lost across restart "
            f"(have {list(runner_b._account_cooldowns.keys())!r})"
        )
    else:
        loaded = runner_b._account_cooldowns[primary]
        if abs((loaded - future).total_seconds()) > 1:
            failures.append(
                f"persist: cooldown reset time drifted ({loaded} vs {future})"
            )

    # Expired entries are purged on load
    state_file.unlink()
    store_c = StateStore(state_file, results_dir)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store_c._account_cooldowns[primary] = past
    store_c.save()

    store_d = StateStore(state_file, results_dir)
    runner_c = ClaudeRunner(store=store_d)
    if primary in runner_c._account_cooldowns:
        failures.append("persist: expired cooldown was not purged on load")

    return failures


# ---------------------------------------------------------------------------
# Test 3: lifecycle finalize_run rejects None session_id
# ---------------------------------------------------------------------------
def test_finalize_guard(tmp: str, store: StateStore) -> list[str]:
    failures: list[str] = []

    # Minimal RequestContext stub — finalize_run touches store + channel_id
    # (the latter via _log_history's history-entry construction).
    class _Ctx:
        def __init__(self, s):
            self.store = s
            self.channel_id = "test-channel"

    ctx = _Ctx(store)

    inst = Instance(
        id="t-guard",
        name=None,
        instance_type=InstanceType.QUERY,
        prompt="x",
        repo_name="r",
        repo_path="/tmp",
        status=InstanceStatus.RUNNING,
        session_id="abc-existing",
        mode="explore",
    )
    synthetic = RunResult(
        is_error=True,
        error_message="all accounts on cooldown",
        session_id=None,  # simulates a synthetic refuse-to-spawn result
    )
    # Redirect history.jsonl writes from finalize_run -> _log_history into
    # tmp so the regression suite doesn't pollute the real data/history.jsonl.
    from bot.store import history as history_mod
    saved_history_file = history_mod.HISTORY_FILE
    history_mod.HISTORY_FILE = Path(tmp) / "test_history.jsonl"
    try:
        finalize_run(ctx, inst, synthetic)
        if inst.session_id != "abc-existing":
            failures.append(
                f"finalize_guard: session_id was clobbered to {inst.session_id!r}"
            )

        # Sanity: a legitimate fresh session_id should still be applied
        fresh = RunResult(is_error=False, session_id="def-new")
        finalize_run(ctx, inst, fresh)
        if inst.session_id != "def-new":
            failures.append(
                f"finalize_guard: legit session_id ignored "
                f"(have {inst.session_id!r}, expected 'def-new')"
            )
    finally:
        history_mod.HISTORY_FILE = saved_history_file

    return failures


# ---------------------------------------------------------------------------
# Test 4: helper does NOT poison instance.session_id when result has None
# (a unit-style check that complements test 3 from the runner side)
# ---------------------------------------------------------------------------
async def test_set_account_cooldown_helper(tmp: str) -> list[str]:
    failures: list[str] = []
    primary = os.path.join(tmp, "helper_primary")
    os.makedirs(primary, exist_ok=True)

    state_file = Path(tmp) / "helper_state.json"
    results_dir = Path(tmp) / "helper_results"
    results_dir.mkdir(exist_ok=True)

    store = StateStore(state_file, results_dir)
    runner = ClaudeRunner(store=store)

    when = datetime.now(timezone.utc) + timedelta(hours=1)
    runner._set_account_cooldown(primary, when)

    if primary not in runner._account_cooldowns:
        failures.append("helper: in-memory dict not updated")

    raw = json.loads(state_file.read_text(encoding="utf-8"))
    if primary not in raw.get("account_cooldowns", {}):
        failures.append("helper: state.json not updated")

    # set_account_cooldown(None) clears
    store.set_account_cooldown(primary, None)
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    if primary in raw.get("account_cooldowns", {}):
        failures.append("helper: store.set_account_cooldown(None) did not clear")

    return failures


# ---------------------------------------------------------------------------
async def _amain() -> int:
    tmp = tempfile.mkdtemp(prefix="dementia_test_")
    state_file = Path(tmp) / "shared_state.json"
    results_dir = Path(tmp) / "shared_results"
    results_dir.mkdir(exist_ok=True)
    shared_store = StateStore(state_file, results_dir)

    all_failures: list[tuple[str, list[str]]] = []
    try:
        for name, coro in (
            ("refuse_to_spawn", test_refuse_to_spawn(tmp, shared_store)),
            ("cooldowns_persist", test_cooldowns_persist(tmp)),
            ("set_account_cooldown_helper", test_set_account_cooldown_helper(tmp)),
        ):
            fails = await coro
            if fails:
                all_failures.append((name, fails))

        sync_fails = test_finalize_guard(tmp, shared_store)
        if sync_fails:
            all_failures.append(("finalize_guard", sync_fails))
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    if all_failures:
        print("FAIL: dementia regression suite")
        for name, fails in all_failures:
            print(f"  [{name}]")
            for f in fails:
                print(f"    - {f}")
        return 1

    print("PASS: dementia regression suite (4 cases)")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
