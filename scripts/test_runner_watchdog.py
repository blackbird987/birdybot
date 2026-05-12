"""Regression test for the end-of-turn watchdog in `_stream_output`.

Background: `claude -p` occasionally fails to close stdout after emitting
an assistant message with `stop_reason: "end_turn"` and no `tool_use`
blocks.  The runner used to wait forever (until 4h lifetime or user
kill), reporting a false FAILED with empty result text even though the
work had landed.  See thread 1503869708183535676 / session q-8396 for
the original incident — the LLM committed v1.3.0.314 + emitted "Done.
SoC1 stage 5 complete." at 21:31:38 local, then the CLI sat silent for
35 minutes until killed.

The watchdog arms when an assistant turn ends with `stop_reason=end_turn`
and no `tool_use`.  If stdout stays silent for END_OF_TURN_GRACE_SECS,
the runner terminates the process and synthesises a successful
RunResult from the captured events.

This test pins four contracts:

  1. **Trips on silence** — events ending with end_turn + no tools and a
     simulated silent stdout cause terminate + success result with the
     final assistant text recovered.
  2. **Doesn't trip mid-hook** — system `hook_started`/`hook_response`
     events post-end_turn keep `last_output_time` fresh, so the watchdog
     never fires while a Stop/SessionEnd hook is running.
  3. **Doesn't arm on end_turn+tool_use** — when the final assistant
     event still carries a tool_use block, `end_of_turn_seen` stays False.
  4. **Sidechain filtered in fallback text** — `last_assistant_text`
     skips events with `isSidechain: true` so a Task sub-agent's trailing
     text can't shadow the main agent's last reply.

Run: ``python scripts/test_runner_watchdog.py``  (exit 0 on pass).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude.parser import last_assistant_text
from bot.claude.runner import ClaudeRunner
from bot.claude.types import Instance, InstanceStatus, InstanceType
from bot.store.state import StateStore


SESSION_ID = "9800f400-36f6-449f-8115-9b86d36ae524"
FINAL_TEXT = "Done. SoC1 stage 5 complete."


# ---------------------------------------------------------------------------
# Fake subprocess primitives
# ---------------------------------------------------------------------------
class _FakeStream:
    """Async stream that emits queued bytes then blocks until told to stop."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._eof = False

    def push_line(self, line: bytes) -> None:
        # Each push is one "line" — runner reads via readline() so trailing \n.
        if not line.endswith(b"\n"):
            line = line + b"\n"
        self._queue.put_nowait(line)

    def push_eof(self) -> None:
        self._eof = True
        self._queue.put_nowait(b"")

    async def readline(self) -> bytes:
        # If queue has data, return it.  Otherwise block "forever" so the
        # runner's 5s wait_for kicks in and exercises the watchdog path.
        if self._queue.empty() and not self._eof:
            # Park here until something is pushed.
            return await self._queue.get()
        return await self._queue.get()

    async def read(self) -> bytes:
        return b""


class _FakeProc:
    """Minimal Process stand-in for `_stream_output`."""

    def __init__(self) -> None:
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.returncode: int | None = None
        self.pid = 999_999
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 1  # mimic Windows TerminateProcess
        self.stdout.push_eof()
        self.stderr.push_eof()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.push_eof()
        self.stderr.push_eof()

    async def wait(self) -> int:
        # Tests drive .terminate() before .wait(), so this is always non-None.
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _make_instance(repo_dir: str) -> Instance:
    return Instance(
        id="t-watchdog",
        name=None,
        instance_type=InstanceType.QUERY,
        prompt="implement stage 5",
        repo_name="aiagent",
        repo_path=repo_dir,
        status=InstanceStatus.RUNNING,
        session_id=None,
        mode="build",
    )


def _assistant_end_turn_event(text: str, with_tool: bool = False) -> dict:
    content: list[dict] = [{"type": "text", "text": text}]
    if with_tool:
        content.append({
            "type": "tool_use", "id": "toolu_x", "name": "Bash",
            "input": {"command": "ls"},
        })
    return {
        "type": "assistant",
        "session_id": SESSION_ID,
        "message": {
            "role": "assistant",
            "content": content,
            "stop_reason": "end_turn",
            "model": "claude-opus-4-7",
        },
    }


def _hook_event(subtype: str) -> dict:
    return {
        "type": "system",
        "subtype": subtype,
        "hook_id": "ec1fb4a6",
        "hook_name": "PostToolUse:Bash",
        "session_id": SESSION_ID,
    }


# ---------------------------------------------------------------------------
# Contract 4 (pure): last_assistant_text filters sidechain
# ---------------------------------------------------------------------------
def test_sidechain_filtered() -> list[str]:
    failures: list[str] = []

    main = {
        "type": "assistant",
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "main answer"}],
            "stop_reason": "end_turn",
        },
    }
    sub = {
        "type": "assistant",
        "isSidechain": True,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "sub-agent chatter"}],
            "stop_reason": "end_turn",
        },
    }

    # Sub-agent event AFTER main — without filter, reverse-walk would pick sub.
    got = last_assistant_text([main, sub])
    if got != "main answer":
        failures.append(
            f"last_assistant_text should skip sidechain; got {got!r}, "
            f"expected 'main answer'"
        )

    # Main-only case still works.
    if last_assistant_text([main]) != "main answer":
        failures.append("last_assistant_text returned wrong text for main-only")

    # No assistant events → empty string (not crash).
    if last_assistant_text([]) != "":
        failures.append("last_assistant_text should return '' for empty events")

    # Sidechain-only → empty string.
    if last_assistant_text([sub]) != "":
        failures.append(
            "last_assistant_text should return '' when only sidechain events present"
        )

    return failures


# ---------------------------------------------------------------------------
# Contract 1: watchdog trips on silence after end_turn (no tool_use)
# ---------------------------------------------------------------------------
async def test_watchdog_trips_on_silence(tmp: str, store: StateStore) -> list[str]:
    failures: list[str] = []
    repo_dir = os.path.join(tmp, "wd_silence")
    os.makedirs(repo_dir, exist_ok=True)

    runner = ClaudeRunner(store=store)
    instance = _make_instance(repo_dir)

    proc = _FakeProc()
    # Stream the final assistant event with end_turn + no tools, then go silent.
    proc.stdout.push_line(json.dumps(_assistant_end_turn_event(FINAL_TEXT)).encode())

    saved_grace = config.END_OF_TURN_GRACE_SECS
    config.END_OF_TURN_GRACE_SECS = 1  # speed up test
    try:
        # _stream_output should: read the event, set end_of_turn_seen=True,
        # hit the 5s readline timeout, see idle > 1s, terminate, return success.
        result = await asyncio.wait_for(
            runner._stream_output(proc, instance, None, None),  # type: ignore[arg-type]
            timeout=20,
        )
    finally:
        config.END_OF_TURN_GRACE_SECS = saved_grace

    if not proc.terminated:
        failures.append("watchdog did not call proc.terminate()")
    if result.is_error:
        failures.append(
            f"watchdog result should be success, got is_error=True "
            f"error_message={result.error_message!r}"
        )
    if result.result_text != FINAL_TEXT:
        failures.append(
            f"watchdog result_text should recover final assistant text; "
            f"got {result.result_text!r}, expected {FINAL_TEXT!r}"
        )
    if result.session_id != SESSION_ID:
        failures.append(
            f"watchdog should propagate captured session_id; "
            f"got {result.session_id!r}, expected {SESSION_ID!r}"
        )
    return failures


# ---------------------------------------------------------------------------
# Contract 2: watchdog does NOT trip while hook events stream
# ---------------------------------------------------------------------------
async def test_watchdog_quiet_during_hook(tmp: str, store: StateStore) -> list[str]:
    failures: list[str] = []
    repo_dir = os.path.join(tmp, "wd_hook")
    os.makedirs(repo_dir, exist_ok=True)

    runner = ClaudeRunner(store=store)
    instance = _make_instance(repo_dir)

    proc = _FakeProc()
    # End-of-turn arrives, then a hook fires and emits system events
    # repeatedly across what would normally be the grace window.
    proc.stdout.push_line(json.dumps(_assistant_end_turn_event(FINAL_TEXT)).encode())

    saved_grace = config.END_OF_TURN_GRACE_SECS
    config.END_OF_TURN_GRACE_SECS = 2  # short but allows hook keepalive to win
    try:
        async def keepalive_then_eof():
            # Drip a hook event every 0.5s for ~5s — keeps last_output_time
            # fresh so the 2s grace window can never elapse.
            for i in range(10):
                await asyncio.sleep(0.5)
                subtype = "hook_started" if i % 2 == 0 else "hook_response"
                proc.stdout.push_line(json.dumps(_hook_event(subtype)).encode())
            # Clean EOF — simulates CLI eventually closing stdout normally.
            proc.stdout.push_eof()
            proc.returncode = 0

        keepalive_task = asyncio.create_task(keepalive_then_eof())
        result = await asyncio.wait_for(
            runner._stream_output(proc, instance, None, None),  # type: ignore[arg-type]
            timeout=20,
        )
        await keepalive_task
    finally:
        config.END_OF_TURN_GRACE_SECS = saved_grace

    if proc.terminated:
        failures.append(
            "watchdog falsely terminated CLI during streaming hook events"
        )
    return failures


# ---------------------------------------------------------------------------
# Contract 3: watchdog does NOT arm when end_turn carries tool_use
# ---------------------------------------------------------------------------
async def test_watchdog_skips_when_tool_pending(tmp: str, store: StateStore) -> list[str]:
    failures: list[str] = []
    repo_dir = os.path.join(tmp, "wd_tool")
    os.makedirs(repo_dir, exist_ok=True)

    runner = ClaudeRunner(store=store)
    instance = _make_instance(repo_dir)

    proc = _FakeProc()
    # End-of-turn with a tool_use in content — CLI is about to fire the tool,
    # watchdog must NOT arm.  Then send EOF cleanly.
    proc.stdout.push_line(
        json.dumps(_assistant_end_turn_event("calling tool", with_tool=True)).encode()
    )

    saved_grace = config.END_OF_TURN_GRACE_SECS
    config.END_OF_TURN_GRACE_SECS = 1
    try:
        async def close_cleanly():
            await asyncio.sleep(3)  # wait past what would be the grace window
            proc.stdout.push_eof()
            proc.returncode = 0

        closer = asyncio.create_task(close_cleanly())
        result = await asyncio.wait_for(
            runner._stream_output(proc, instance, None, None),  # type: ignore[arg-type]
            timeout=20,
        )
        await closer
    finally:
        config.END_OF_TURN_GRACE_SECS = saved_grace

    if proc.terminated:
        failures.append(
            "watchdog falsely terminated CLI when end_turn carried tool_use"
        )
    return failures


# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------
async def _amain() -> int:
    tmp = tempfile.mkdtemp(prefix="watchdog-test-")
    state_file = Path(tmp) / "state.json"
    results_dir = Path(tmp) / "results"
    results_dir.mkdir(exist_ok=True)

    saved_results_dir = config.RESULTS_DIR
    config.RESULTS_DIR = results_dir

    store = StateStore(state_file, results_dir)
    all_failures: list[tuple[str, list[str]]] = []

    try:
        sync_fails = test_sidechain_filtered()
        if sync_fails:
            all_failures.append(("sidechain_filtered", sync_fails))

        for name, coro in (
            ("trips_on_silence", test_watchdog_trips_on_silence(tmp, store)),
            ("quiet_during_hook", test_watchdog_quiet_during_hook(tmp, store)),
            ("skips_when_tool_pending",
             test_watchdog_skips_when_tool_pending(tmp, store)),
        ):
            fails = await coro
            if fails:
                all_failures.append((name, fails))
    finally:
        config.RESULTS_DIR = saved_results_dir
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    if all_failures:
        print("FAIL: end-of-turn watchdog regression suite")
        for name, fails in all_failures:
            print(f"  [{name}]")
            for f in fails:
                print(f"    - {f}")
        return 1

    print("PASS: end-of-turn watchdog regression suite (4 cases)")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
