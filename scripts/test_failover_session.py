"""Regression test: cross-account failover preserves instance.session_id.

Background: bot/claude/runner.py:410 used to clear ``instance.session_id =
None`` on failover, which defeated cross-account session hydration and caused
the dementia incidents on chains t-3361 and t-3386.  This test pins down the
desired behaviour so a future refactor can't silently re-introduce the bug.

Strategy: stub the CLI subprocess boundary, not _run_impl itself.  Both
real _run_impl invocations (outer + recursive failover) execute against the
real failover branch — only ``asyncio.create_subprocess_exec`` and the
streaming output parser are faked.  The test asserts:

  - the failover spawn's argv contains ``--resume <original_session_id>``
  - ``instance.session_id`` after the call equals the original id
  - ``instance._accounts_tried`` contains the exhausted primary account
  - the JSONL was hydrated into the backup account's project dir

Run: ``python scripts/test_failover_session.py``  (exit 0 on pass).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Make the bot package importable when invoked as a script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude import runner as runner_mod
from bot.claude.runner import ClaudeRunner
from bot.claude.types import Instance, InstanceStatus, InstanceType, RunResult
from bot.engine.session_fork import encode_project_path


SESSION_ID = "00000000-1111-2222-3333-444444444444"


class _FakeStdin:
    def write(self, data):  # pragma: no cover - trivial
        return None

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None


class _FakeProc:
    """Minimal asyncio.subprocess.Process stand-in for the spawn lifecycle."""

    _next_pid = 90001

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
    return Instance(
        id="t-test",
        name=None,
        instance_type=InstanceType.QUERY,
        prompt="continue",
        repo_name="test-repo",
        repo_path=repo_dir,
        status=InstanceStatus.RUNNING,
        session_id=SESSION_ID,
        mode="explore",
        # No branch -> _ensure_worktree is skipped, keeping the test small
    )


async def _amain() -> int:
    # ---- arrange tempdirs --------------------------------------------------
    tmp = tempfile.mkdtemp(prefix="failover_test_")
    primary = os.path.join(tmp, "acct_primary")
    backup = os.path.join(tmp, "acct_backup")
    repo_dir = os.path.join(tmp, "repo")
    for p in (primary, backup, repo_dir):
        os.makedirs(p, exist_ok=True)

    # Plant the session JSONL only in the primary account's project dir.
    encoded = encode_project_path(repo_dir)
    primary_proj_dir = Path(primary) / "projects" / encoded
    primary_proj_dir.mkdir(parents=True, exist_ok=True)
    primary_jsonl = primary_proj_dir / f"{SESSION_ID}.jsonl"
    primary_jsonl.write_text(
        '{"type":"summary","summary":"seed"}\n', encoding="utf-8",
    )
    backup_jsonl = Path(backup) / "projects" / encoded / f"{SESSION_ID}.jsonl"

    # ---- patch globals -----------------------------------------------------
    saved_accounts = list(config.CLAUDE_ACCOUNTS)
    saved_spawn = asyncio.create_subprocess_exec

    config.CLAUDE_ACCOUNTS[:] = [primary, backup]

    spawn_calls: list[list[str]] = []

    async def fake_spawn(*args, **_kwargs):
        spawn_calls.append(list(args))
        return _FakeProc()

    asyncio.create_subprocess_exec = fake_spawn  # type: ignore[assignment]

    runner = ClaudeRunner()

    # _stream_output: first call -> usage-limit error; second call -> success.
    stream_calls: dict[str, int] = {"n": 0}

    async def fake_stream_output(proc, instance, on_progress, on_stall, **kw):
        stream_calls["n"] += 1
        n = stream_calls["n"]
        if n == 1:
            return RunResult(
                is_error=True,
                error_message="You've hit your limit · resets 5pm",
                result_text="You've hit your limit · resets 5pm",
            )
        # Second invocation: clean success.  Echo the instance's session_id
        # so this matches what claude.exe does with --resume <id>.
        return RunResult(
            is_error=False,
            session_id=instance.session_id,
            result_text="ok",
        )

    runner._stream_output = fake_stream_output  # type: ignore[assignment]

    instance = _make_instance(repo_dir)
    original_id = instance.session_id

    # ---- act ---------------------------------------------------------------
    failures: list[str] = []
    try:
        result = await runner.run(instance)

        # ---- assertions ----------------------------------------------------
        if instance.session_id != original_id:
            failures.append(
                f"session_id was overwritten: expected {original_id!r}, "
                f"got {instance.session_id!r}"
            )

        if primary not in instance._accounts_tried:
            failures.append(
                f"primary account not in _accounts_tried: {instance._accounts_tried!r}"
            )

        if len(spawn_calls) < 2:
            failures.append(
                f"expected >=2 spawns (primary + failover), got {len(spawn_calls)}"
            )
        else:
            second_argv = spawn_calls[1]
            if "--resume" not in second_argv:
                failures.append(
                    "failover spawn argv missing --resume flag: " + " ".join(second_argv)
                )
            else:
                idx = second_argv.index("--resume")
                if idx + 1 >= len(second_argv) or second_argv[idx + 1] != original_id:
                    failures.append(
                        f"failover --resume value mismatch: expected {original_id!r}, "
                        f"got {second_argv[idx + 1] if idx + 1 < len(second_argv) else '<missing>'!r}"
                    )

        if not backup_jsonl.exists():
            failures.append(
                f"session JSONL not hydrated to backup account: {backup_jsonl}"
            )

        if result.is_error:
            failures.append(f"final result was error: {result.error_message!r}")

    finally:
        # ---- restore globals -----------------------------------------------
        asyncio.create_subprocess_exec = saved_spawn  # type: ignore[assignment]
        config.CLAUDE_ACCOUNTS[:] = saved_accounts
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    # ---- report ------------------------------------------------------------
    if failures:
        print("FAIL: cross-account failover regression")
        for f in failures:
            print(f"  - {f}")
        print(f"\nspawn argvs ({len(spawn_calls)}):")
        for i, argv in enumerate(spawn_calls):
            print(f"  [{i}] {' '.join(argv)[:200]}")
        return 1

    print("PASS: cross-account failover preserves session_id")
    print(f"  spawn 1 argv: ...{' '.join(spawn_calls[0])[-120:]}")
    print(f"  spawn 2 argv: ...{' '.join(spawn_calls[1])[-120:]}")
    print(f"  hydrated jsonl: {backup_jsonl}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
