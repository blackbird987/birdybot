#!/usr/bin/env python3
"""Regression test: an unresumable conversation must not fake an account outage.

The incident this pins down (thread 1503787276294033559, 2026-08-05/06): a
conversation existed on no account under any spelling, so every ``--resume``
returned "No conversation found".  Recovery is supposed to fall back to a blank
run, and a blank run needs no particular account — but the layer that tries the
*other* account had already marked the account it just used as "tried", and the
blank run inherited that mark.  With the backup signed out, the picker was left
with zero candidates and refused to spawn at all, on a machine whose primary
account was perfectly healthy.

The user saw "Every Claude account is signed out".  Both halves were false: the
blank run never happened, and only the backup was signed out.  The refusal then
restored the dead conversation id, so the whole cycle re-queued every 16
minutes from 22:15 to 01:03.

Asserted here:

  * the blank retry actually spawns, on the healthy account
  * its argv carries no ``--resume``
  * the run returns real output rather than a synthetic usage-limit refusal
  * the dead conversation id is NOT handed back to the retry queue

Strategy follows scripts/test_failover_session.py: stub the subprocess
boundary only, so the real recovery cascade executes.

Run: ``python scripts/test_dead_session_recovery.py``  (exit 0 on pass).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Importing bot.config runs a real path-map init, which would drop a root
# marker in whoever's home this runs under and write a roots.json next to the
# checkout. The cascade under test does not depend on the map, so switch it
# off: an empty map means every path passes through unchanged, and the test
# stops varying with the machine it runs on.
os.environ.setdefault("BOT_PATHS_DISABLED", "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude import runner as runner_mod
from bot.claude.runner import ClaudeRunner
from bot.claude.types import Instance, InstanceStatus, InstanceType, RunResult

# A conversation id that exists nowhere on disk — the whole point of the test.
DEAD_SESSION = "3450fc61-8d8e-4531-aa90-c3b737b69205"


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
    _next_pid = 91001

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


async def _amain() -> int:
    tmp = tempfile.mkdtemp(prefix="dead_session_test_")
    primary = os.path.join(tmp, "acct_primary")
    backup = os.path.join(tmp, "acct_backup")
    repo_dir = os.path.join(tmp, "repo")
    for p in (primary, backup, repo_dir):
        os.makedirs(p, exist_ok=True)
    # Deliberately plant NO session JSONL anywhere.

    saved_accounts = list(config.CLAUDE_ACCOUNTS)
    saved_spawn = asyncio.create_subprocess_exec
    saved_unusable = runner_mod.unusable_reason
    config.CLAUDE_ACCOUNTS[:] = [primary, backup]

    # The machine's real state during the incident: primary signed in and
    # healthy, backup signed out.  Patched at the module level because the
    # credential check has two callers (the account picker and the refusal
    # message) and stubbing only one would let scratch dirs with no
    # credentials file read as "primary is dead too".
    runner_mod.unusable_reason = lambda acct: (  # type: ignore[assignment]
        "no refresh token — not logged in" if acct == backup else None
    )

    spawn_calls: list[list[str]] = []
    spawn_accounts: list[str | None] = []

    async def fake_spawn(*args, **kwargs):
        spawn_calls.append(list(args))
        env = kwargs.get("env") or {}
        spawn_accounts.append(env.get("CLAUDE_CONFIG_DIR"))
        return _FakeProc()

    asyncio.create_subprocess_exec = fake_spawn  # type: ignore[assignment]

    runner = ClaudeRunner()
    stream_calls = {"n": 0}

    async def fake_stream_output(proc, instance, on_progress, on_stall, **kw):
        stream_calls["n"] += 1
        if stream_calls["n"] == 1:
            # What the CLI really returns for a conversation it cannot find.
            msg = f"No conversation found with session ID: {DEAD_SESSION}"
            return RunResult(is_error=True, error_message=msg, result_text=msg)
        # The blank retry: this is what the user was owed and never got.
        return RunResult(
            is_error=False,
            session_id="99999999-aaaa-bbbb-cccc-dddddddddddd",
            result_text="fresh start, work done",
        )

    runner._stream_output = fake_stream_output  # type: ignore[assignment]

    instance = Instance(
        id="q-dead",
        name=None,
        instance_type=InstanceType.QUERY,
        prompt="continue",
        repo_name="The-Citadel",
        repo_path=repo_dir,
        status=InstanceStatus.RUNNING,
        session_id=DEAD_SESSION,
        mode="explore",
    )

    failures: list[str] = []
    try:
        result = await runner.run(instance)

        if len(spawn_calls) < 2:
            failures.append(
                "the blank retry never spawned — the bot refused to run at all "
                f"(spawns={len(spawn_calls)}). This is the reported bug."
            )
        else:
            first, second = spawn_calls[0], spawn_calls[1]
            if "--resume" not in first:
                failures.append("first spawn should have tried --resume")
            if "--resume" in second:
                failures.append(
                    "the retry still passed --resume: " + " ".join(second)[:200]
                )
            if spawn_accounts[1] != primary:
                failures.append(
                    "the blank retry did not run on the healthy account "
                    f"(got {spawn_accounts[1]!r})"
                )

        if result.is_error:
            failures.append(f"final result was an error: {result.error_message!r}")

        if result.retry_reason == "accounts_logged_out":
            failures.append(
                "reported every account as signed out while the primary was "
                "healthy"
            )

        if (result.result_text or "").strip() != "fresh start, work done":
            failures.append(
                f"blank retry output was lost: {result.result_text!r}"
            )

        # The dead id must not come back — restoring it is what rebuilt the
        # 16-minute retry loop.
        if result.session_id == DEAD_SESSION:
            failures.append(
                "the unresumable conversation id was handed back to the retry "
                "queue; it would loop forever"
            )
        if instance.session_id == DEAD_SESSION:
            failures.append("instance still pinned to the unresumable id")

        if not result.session_recovery_exhausted:
            failures.append(
                "recovery was not flagged, so the user gets no 'lost prior "
                "context' notice"
            )
    finally:
        asyncio.create_subprocess_exec = saved_spawn  # type: ignore[assignment]
        runner_mod.unusable_reason = saved_unusable  # type: ignore[assignment]
        config.CLAUDE_ACCOUNTS[:] = saved_accounts
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL: dead-session recovery")
        for f in failures:
            print(f"  - {f}")
        print(f"\nspawn argvs ({len(spawn_calls)}):")
        for i, argv in enumerate(spawn_calls):
            print(f"  [{i}] {' '.join(argv)[:200]}")
        return 1

    print("PASS: an unresumable conversation falls back to a blank run on the")
    print("      healthy account, and its dead id is not re-queued.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
