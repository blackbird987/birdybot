"""Tests for account-unusable failover (cancelled-subscription handling).

Covers the second cross-account failover trigger added so the bot keeps
working when one Claude subscription is cancelled (auth/subscription error,
NOT a usage limit).  See bot/claude/runner.py (the block before `return
result`) and bot/claude/parser.py classifiers.

Two layers:

1. Pure classifier unit tests — no async, no subprocess:
   - is_account_unusable_error matches auth strings, rejects transient/usage.
   - is_account_agnostic_error catches model/flag errors.

2. Integration tests against the real _run_impl failover branch, faking only
   the CLI subprocess boundary (asyncio.create_subprocess_exec +
   _stream_output), mirroring scripts/test_failover_session.py:
   - confident auth error -> cooldown set + account switched.
   - account-agnostic "currently unavailable" -> NO failover, NO cooldown.
   - both accounts dead -> each tried once, error returned, terminates
     (no infinite recursion / all-cooled deadlock).
   - run() resets a stale _accounts_tried from a prior run.

Run: ``python scripts/test_account_failover.py``  (exit 0 on pass).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude.parser import (
    is_account_agnostic_error,
    is_account_unusable_error,
)
from bot.claude.runner import ClaudeRunner
from bot.claude.types import Instance, InstanceStatus, InstanceType, RunResult


# ---------------------------------------------------------------------------
# Layer 1: pure classifier unit tests
# ---------------------------------------------------------------------------

def _test_classifiers() -> list[str]:
    failures: list[str] = []

    unusable_yes = [
        "Invalid API key · Please run /login",
        "OAuth token has expired",
        "Your authentication failed",
        "401 Unauthorized",
        "No active subscription found",
        "Your subscription has expired",
        "Credit balance is too low",
        "Please sign in again",
    ]
    for s in unusable_yes:
        if not is_account_unusable_error(s):
            failures.append(f"is_account_unusable_error should match: {s!r}")

    unusable_no = [
        "",
        "rate limit exceeded",          # transient
        "connection refused",            # transient
        "You've hit your usage limit · resets 5pm",  # usage cap
        "hit your weekly limit",         # usage cap
        "Some normal completion text",
    ]
    for s in unusable_no:
        if is_account_unusable_error(s):
            failures.append(f"is_account_unusable_error should NOT match: {s!r}")

    agnostic_yes = [
        "Claude Fable 5 is currently unavailable.",
        "model not found",
        "unknown model: foo",
        "unrecognized arguments: --bogus",
        "usage: claude [-h] ...",
    ]
    for s in agnostic_yes:
        if not is_account_agnostic_error(s):
            failures.append(f"is_account_agnostic_error should match: {s!r}")

    agnostic_no = ["", "Invalid API key", "rate limit"]
    for s in agnostic_no:
        if is_account_agnostic_error(s):
            failures.append(f"is_account_agnostic_error should NOT match: {s!r}")

    return failures


# ---------------------------------------------------------------------------
# Layer 2: integration harness (fakes only the subprocess boundary)
# ---------------------------------------------------------------------------

class _FakeStdin:
    def write(self, data):
        return None

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None


class _FakeProc:
    _next_pid = 95001

    def __init__(self):
        self.pid = _FakeProc._next_pid
        _FakeProc._next_pid += 1
        self.returncode = 0
        self.stdin = _FakeStdin()
        self.stdout = None
        self.stderr = None

    def kill(self):
        return None

    async def wait(self):
        return 0


def _make_instance(repo_dir: str) -> Instance:
    # No session_id -> hydration skipped (this suite tests the failover
    # trigger + cooldown, not session preservation).  No branch -> worktree
    # setup skipped.
    return Instance(
        id="t-acct",
        name=None,
        instance_type=InstanceType.QUERY,
        prompt="do a thing",
        repo_name="test-repo",
        repo_path=repo_dir,
        status=InstanceStatus.RUNNING,
        mode="explore",
    )


async def _run_with_streams(stream_results, *, accounts):
    """Run a fresh instance through runner.run(), faking _stream_output to
    yield ``stream_results`` (a list of RunResult) per spawn in order; the
    last entry is reused if more spawns happen.

    Returns (result, instance, runner, spawn_count).
    """
    tmp = tempfile.mkdtemp(prefix="acct_failover_")
    acct_dirs = [os.path.join(tmp, name) for name in accounts]
    repo_dir = os.path.join(tmp, "repo")
    for p in (*acct_dirs, repo_dir):
        os.makedirs(p, exist_ok=True)

    saved_accounts = list(config.CLAUDE_ACCOUNTS)
    saved_spawn = asyncio.create_subprocess_exec
    config.CLAUDE_ACCOUNTS[:] = acct_dirs

    spawn_calls: list[list[str]] = []

    async def fake_spawn(*args, **_kwargs):
        spawn_calls.append(list(args))
        return _FakeProc()

    asyncio.create_subprocess_exec = fake_spawn  # type: ignore[assignment]

    runner = ClaudeRunner()
    calls = {"n": 0}

    async def fake_stream_output(proc, instance, on_progress, on_stall, **kw):
        i = min(calls["n"], len(stream_results) - 1)
        calls["n"] += 1
        return stream_results[i]

    runner._stream_output = fake_stream_output  # type: ignore[assignment]

    instance = _make_instance(repo_dir)
    try:
        result = await runner.run(instance)
    finally:
        asyncio.create_subprocess_exec = saved_spawn  # type: ignore[assignment]
        config.CLAUDE_ACCOUNTS[:] = saved_accounts
        shutil.rmtree(tmp, ignore_errors=True)

    return result, instance, runner, acct_dirs, len(spawn_calls)


async def _test_confident_failover() -> list[str]:
    """Confident auth error -> cooldown on primary + switch to backup."""
    failures: list[str] = []
    results = [
        RunResult(is_error=True,
                  error_message="Invalid API key · Please run /login",
                  result_text="Invalid API key · Please run /login"),
        RunResult(is_error=False, result_text="ok"),
    ]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"]
    )
    primary, backup = accts
    if spawns < 2:
        failures.append(f"confident: expected >=2 spawns, got {spawns}")
    if primary not in instance._accounts_tried:
        failures.append("confident: primary not added to _accounts_tried")
    if primary not in runner._account_cooldowns:
        failures.append("confident: primary not put on cooldown")
    if result.is_error:
        failures.append(f"confident: final result errored: {result.error_message!r}")
    return failures


async def _test_agnostic_no_failover() -> list[str]:
    """Account-agnostic 'currently unavailable' -> NO failover, NO cooldown."""
    failures: list[str] = []
    results = [
        RunResult(is_error=True,
                  error_message="Claude model X is currently unavailable.",
                  result_text=""),
    ]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"]
    )
    primary, backup = accts
    if spawns != 1:
        failures.append(f"agnostic: expected exactly 1 spawn, got {spawns}")
    if runner._account_cooldowns:
        failures.append(
            f"agnostic: no cooldown expected, got {list(runner._account_cooldowns)}"
        )
    if not result.is_error:
        failures.append("agnostic: expected the raw error to be returned")
    return failures


async def _test_both_dead_terminates() -> list[str]:
    """Both accounts auth-dead -> each tried once, error returned, no hang."""
    failures: list[str] = []
    results = [
        RunResult(is_error=True,
                  error_message="OAuth token has expired",
                  result_text="OAuth token has expired"),
    ]  # reused for every spawn
    try:
        result, instance, runner, accts, spawns = await asyncio.wait_for(
            _run_with_streams(results, accounts=["primary", "backup"]),
            timeout=10,
        )
    except asyncio.TimeoutError:
        return ["both-dead: run did NOT terminate (possible infinite recursion)"]

    primary, backup = accts
    if spawns != 2:
        failures.append(f"both-dead: expected exactly 2 spawns, got {spawns}")
    if not (primary in runner._account_cooldowns
            and backup in runner._account_cooldowns):
        failures.append("both-dead: expected BOTH accounts on cooldown")
    if not result.is_error:
        failures.append("both-dead: expected final error result")
    return failures


async def _test_run_resets_accounts_tried() -> list[str]:
    """run() clears a stale _accounts_tried from a prior run."""
    failures: list[str] = []
    tmp = tempfile.mkdtemp(prefix="acct_reset_")
    primary = os.path.join(tmp, "primary")
    repo_dir = os.path.join(tmp, "repo")
    for p in (primary, repo_dir):
        os.makedirs(p, exist_ok=True)

    saved_accounts = list(config.CLAUDE_ACCOUNTS)
    saved_spawn = asyncio.create_subprocess_exec
    config.CLAUDE_ACCOUNTS[:] = [primary]

    async def fake_spawn(*args, **_kwargs):
        return _FakeProc()

    asyncio.create_subprocess_exec = fake_spawn  # type: ignore[assignment]
    runner = ClaudeRunner()

    async def fake_stream_output(proc, instance, on_progress, on_stall, **kw):
        return RunResult(is_error=False, result_text="ok")

    runner._stream_output = fake_stream_output  # type: ignore[assignment]

    instance = _make_instance(repo_dir)
    instance._accounts_tried = {"/stale/account/from/prior/run"}
    try:
        await runner.run(instance)
        if "/stale/account/from/prior/run" in instance._accounts_tried:
            failures.append("run() did not reset stale _accounts_tried")
    finally:
        asyncio.create_subprocess_exec = saved_spawn  # type: ignore[assignment]
        config.CLAUDE_ACCOUNTS[:] = saved_accounts
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


async def _amain() -> int:
    all_failures: list[tuple[str, list[str]]] = []

    all_failures.append(("classifiers", _test_classifiers()))
    all_failures.append(("confident-failover", await _test_confident_failover()))
    all_failures.append(("agnostic-no-failover", await _test_agnostic_no_failover()))
    all_failures.append(("both-dead-terminates", await _test_both_dead_terminates()))
    all_failures.append(("run-resets-tried", await _test_run_resets_accounts_tried()))

    total = sum(len(f) for _, f in all_failures)
    if total:
        print("FAIL: account-failover tests")
        for name, fails in all_failures:
            for f in fails:
                print(f"  [{name}] {f}")
        return 1

    print("PASS: account-failover tests")
    for name, _ in all_failures:
        print(f"  - {name}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
