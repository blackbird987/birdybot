#!/usr/bin/env python3
"""Regression test: every way out of a run cleans up its worktree.

``ClaudeRunner._stream_output`` has three exits once the CLI is gone: the
normal tail, a build that stopped on AskUserQuestion, and the end-of-turn
watchdog (the CLI hung after its turn ended). Only the normal tail copied the
worktree session back to the main repo and released the per-repo test mutex.
A build that asked a question therefore held the mutex until its answer
arrived, and every sibling build on that repo waited out the hook's stale TTL.

Asserted here, with a fake CLI process and a worktree build instance:

  * all three exits copy the session back and release the mutex, once each;
  * a read-only run (no worktree) does neither;
  * the question exit still returns the question, needing input, and writes
    the result file;
  * the end-of-turn exit still returns the recovered text as a success.

Run: python scripts/test_stream_exit_cleanup.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config  # noqa: E402
from bot.claude.runner import ClaudeRunner  # noqa: E402
from bot.claude.types import Instance, InstanceStatus, InstanceType  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

SESSION_ID = "5f1c0e2a-0000-4000-8000-000000000001"
_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}" + (f"  ({detail})" if detail else ""))
        _failures.append(name)


class _Stream:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    def push(self, event: dict) -> None:
        self._queue.put_nowait(json.dumps(event).encode() + b"\n")

    def eof(self) -> None:
        self._queue.put_nowait(b"")

    async def readline(self) -> bytes:
        return await self._queue.get()

    async def read(self) -> bytes:
        return b""


class _Proc:
    def __init__(self) -> None:
        self.stdout = _Stream()
        self.stderr = _Stream()
        self.returncode: int | None = None
        self.pid = 999_999  # never a real pid

    def terminate(self) -> None:
        self.returncode = 143

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _assistant(content: list[dict], stop_reason: str) -> dict:
    return {
        "type": "assistant", "session_id": SESSION_ID,
        "message": {"role": "assistant", "content": content,
                    "stop_reason": stop_reason, "model": "claude-opus-5"},
    }


def _instance(tmp: Path, worktree: bool) -> Instance:
    repo = tmp / "repo"
    repo.mkdir(exist_ok=True)
    wt = repo / ".worktrees" / "t-1"
    if worktree:
        wt.mkdir(parents=True, exist_ok=True)
    return Instance(
        id="t-exit", name=None, instance_type=InstanceType.TASK, prompt="build it",
        repo_name="repo", repo_path=str(repo), status=InstanceStatus.RUNNING,
        mode="build", worktree_path=str(wt) if worktree else None,
    )


def _runner(tmp: Path) -> tuple[ClaudeRunner, list[str]]:
    calls: list[str] = []
    runner = ClaudeRunner(store=StateStore(tmp / "state.json", tmp / "results"))
    runner._copy_session_from_worktree = lambda inst, acct: calls.append("copy")  # type: ignore[method-assign]
    runner._release_test_mutex = lambda repo, wt: calls.append("release")  # type: ignore[method-assign]
    return runner, calls


async def _run(tmp: Path, feed, *, worktree: bool = True):  # noqa: ANN001
    runner, calls = _runner(tmp)
    proc = _Proc()
    feed(proc)
    result = await asyncio.wait_for(
        runner._stream_output(proc, _instance(tmp, worktree), None, None),  # type: ignore[arg-type]
        timeout=30,
    )
    return result, calls


def _ask(proc: _Proc) -> None:
    proc.stdout.push(_assistant([
        {"type": "text", "text": "Before I go on:"},
        {"type": "tool_use", "id": "toolu_q", "name": "AskUserQuestion",
         "input": {"questions": [{"question": "Ship to prod?", "options": [
             {"label": "Yes"}, {"label": "No"}]}]}},
    ], "tool_use"))


def _hung_after_turn(proc: _Proc) -> None:
    # A finished turn, then nothing: no EOF, no result event.
    proc.stdout.push(_assistant([{"type": "text", "text": "All done, tests pass."}], "end_turn"))


def _normal(proc: _Proc) -> None:
    proc.stdout.push(_assistant([{"type": "text", "text": "Done."}], "end_turn"))
    proc.stdout.push({"type": "result", "subtype": "success", "is_error": False,
                      "result": "Done.", "session_id": SESSION_ID, "num_turns": 1})
    proc.stdout.eof()


async def main_async() -> None:
    saved = {k: getattr(config, k) for k in (
        "RESULTS_DIR", "END_OF_TURN_GRACE_SECS", "SESSION_MEM_KILL_MB", "SESSION_MEM_WARN_MB")}
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "results").mkdir()
        config.RESULTS_DIR = tmp / "results"
        config.END_OF_TURN_GRACE_SECS = 0
        config.SESSION_MEM_KILL_MB = 0
        config.SESSION_MEM_WARN_MB = 0
        try:
            result, calls = await _run(tmp, _ask)
            _check("question exit: copies the session back and releases the mutex",
                   calls == ["copy", "release"], str(calls))
            _check("question exit: still returns the question, needing input",
                   result.needs_input and not result.is_error
                   and "Ship to prod?" in (result.result_text or ""), result.result_text or "")
            _check("question exit: writes the result file",
                   (tmp / "results" / "t-exit.md").exists())

            result, calls = await _run(tmp, _hung_after_turn)
            _check("end-of-turn exit: copies the session back and releases the mutex",
                   calls == ["copy", "release"], str(calls))
            _check("end-of-turn exit: returns the recovered text as a success",
                   not result.is_error and "All done" in (result.result_text or ""),
                   result.result_text or "")

            result, calls = await _run(tmp, _normal)
            _check("normal exit: copies the session back and releases the mutex",
                   calls == ["copy", "release"], str(calls))

            _result, calls = await _run(tmp, _ask, worktree=False)
            _check("no worktree: nothing to copy or release", calls == [], str(calls))
        finally:
            for k, v in saved.items():
                setattr(config, k, v)


def main() -> int:
    asyncio.run(main_async())
    print()
    if _failures:
        print(f"FAIL: {len(_failures)} stream-exit check(s) failed")
        return 1
    print("PASS: every exit from a run copies its session back and releases the mutex.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
