"""Regression test for the orphan safety-net in `_stream_output`.

Background — the incident this exists to prevent (2026-08-27, q-15433):
a benchmark run in Ev-nova-remake was farming image reads out to subagents in
batches of three.  It had been running for four hours, which was the entire
reason it died: the safety-net asked only how OLD the process was, never
whether it was still doing anything.  Its last real output was five and a half
minutes before the kill, it had not gone silent for even sixty seconds in its
final two hours, and it was sitting at 350 MB with live HTTPS connections open.
Four hours of work then came back as an empty red FAILED card, because the kill
path returned a bare RunResult with no text, no tools and no session id.

Two contracts came out of that, plus three that guard the other direction:

  1. **Old but still talking survives** — past the age threshold, a process
     that keeps producing output is not reaped.  Age is only the point at
     which we start asking; silence is the answer that kills.
  2. **Old and silent is reaped** — once it has produced nothing for
     MAX_PROCESS_SILENCE_SECS it dies, so a genuinely hung CLI still gets
     cleaned up.
  3. **The hard ceiling still applies** — something that heartbeats forever
     without finishing is not immortal just because it is noisy.
  4. **The reap keeps the work** — the failure carries the recovered assistant
     text, the session id (so Retry resumes rather than restarting) and the
     tools used, and its wording still contains "lifetime limit" so
     `is_account_agnostic_error` suppresses the no-turns account-failover
     heuristic instead of handing the run to the backup subscription.
  5. **A requested stop wins the race** — if the user hits Kill in the same
     few seconds the watchdog decides to reap, the run renders as the quiet
     tombstone they asked for, not as an orphan reap.

Run: ``python scripts/test_lifetime_cap.py``  (exit 0 on pass).
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude.parser import is_account_agnostic_error
from bot.claude.runner import ClaudeRunner, _lifetime_kill_result
from bot.claude.types import Instance, InstanceStatus, InstanceType
from bot.store.state import StateStore


SESSION_ID = "90ee2a74-106e-4338-8b6e-cebf36733c09"
LAST_TEXT = "Wrote shuttle-02-z1.json; dispatching the next crosshair batch."


# ---------------------------------------------------------------------------
# Fake subprocess primitives (same shape as scripts/test_runner_watchdog.py)
# ---------------------------------------------------------------------------
class _FakeStream:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    def push_line(self, line: bytes) -> None:
        if not line.endswith(b"\n"):
            line = line + b"\n"
        self._queue.put_nowait(line)

    def push_eof(self) -> None:
        self._queue.put_nowait(b"")

    async def readline(self) -> bytes:
        return await self._queue.get()

    async def read(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(self) -> None:
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.returncode: int | None = None
        # Never a real pid: the memory guard's reap calls kill_tree(pid), and
        # os.getpid() here would aim it at the test runner. _Knobs disables
        # that guard, but a fake proc should not depend on that to be safe.
        self.pid = 999_999
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 143
        self.stdout.push_eof()
        self.stderr.push_eof()

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _make_instance(repo_dir: str) -> Instance:
    return Instance(
        id="q-lifetime",
        name=None,
        instance_type=InstanceType.QUERY,
        prompt="run bench B018",
        repo_name="Ev-nova-remake",
        repo_path=repo_dir,
        status=InstanceStatus.RUNNING,
        session_id=None,
        mode="build",
    )


def _assistant_event(text: str, with_tool: bool = True) -> dict:
    """An assistant turn that is still working — tool_use, so no end_turn arm."""
    content: list[dict] = [{"type": "text", "text": text}]
    if with_tool:
        content.append({
            "type": "tool_use", "id": "toolu_b018", "name": "Bash",
            "input": {"command": "cat > /tmp/b018/reads/shuttle-02-z1.json"},
        })
    return {
        "type": "assistant",
        "session_id": SESSION_ID,
        "message": {
            "role": "assistant",
            "content": content,
            "stop_reason": "tool_use",
            "model": "claude-opus-5",
        },
    }


class _Knobs:
    """Scale the whole watchdog down so the suite runs in seconds, not hours."""

    FIELDS = (
        "MAX_PROCESS_LIFETIME_SECS",
        "MAX_PROCESS_SILENCE_SECS",
        "MAX_PROCESS_HARD_LIFETIME_SECS",
        "WATCHDOG_TICK_SECS",
        "SESSION_MEM_KILL_MB",
        "SESSION_MEM_WARN_MB",
    )

    def __init__(self, **overrides) -> None:
        self.overrides = overrides
        self.saved: dict[str, object] = {}

    def __enter__(self):
        for f in self.FIELDS:
            self.saved[f] = getattr(config, f)
        # The memory guard is a different watchdog with its own incident and
        # its own harness; switch it off so it can't decide these outcomes.
        config.SESSION_MEM_KILL_MB = 0
        config.SESSION_MEM_WARN_MB = 0
        for k, v in self.overrides.items():
            setattr(config, k, v)
        return self

    def __exit__(self, *exc) -> None:
        for f, v in self.saved.items():
            setattr(config, f, v)


async def _feed(stream: _FakeStream, seconds: float, every: float) -> None:
    """Keep a stream chatty for a while — the 'still working' signal."""
    deadline = asyncio.get_event_loop().time() + seconds
    while asyncio.get_event_loop().time() < deadline:
        stream.push_line(json.dumps(_assistant_event(LAST_TEXT)).encode())
        await asyncio.sleep(every)
    stream.push_eof()


# ---------------------------------------------------------------------------
# Contract 1: old but still producing output is NOT reaped
# ---------------------------------------------------------------------------
async def test_old_but_alive_survives(tmp: str, store: StateStore) -> list[str]:
    failures: list[str] = []
    repo_dir = os.path.join(tmp, "alive")
    os.makedirs(repo_dir, exist_ok=True)

    runner = ClaudeRunner(store=store)
    instance = _make_instance(repo_dir)
    proc = _FakeProc()

    # Instantly "past" the age threshold, but silence must reach 5s to reap,
    # and we keep talking every 0.3s for 3s. This is q-15433's exact shape.
    with _Knobs(
        MAX_PROCESS_LIFETIME_SECS=0,
        MAX_PROCESS_SILENCE_SECS=5,
        MAX_PROCESS_HARD_LIFETIME_SECS=0,   # backstop off for this case
        WATCHDOG_TICK_SECS=0.2,
    ):
        feeder = asyncio.create_task(_feed(proc.stdout, seconds=3.0, every=0.3))
        result = await asyncio.wait_for(
            runner._stream_output(proc, instance, None, None),  # type: ignore[arg-type]
            timeout=30,
        )
        await feeder

    if proc.terminated:
        failures.append(
            "a process past the age threshold that never went silent was "
            "terminated — this is the q-15433 regression"
        )
    if result.error_message and "lifetime limit" in result.error_message.lower():
        failures.append(
            f"reported a lifetime kill for a chatty process: "
            f"{result.error_message!r}"
        )
    return failures


# ---------------------------------------------------------------------------
# Contract 2 + 4: old AND silent is reaped, and the reap keeps the work
# ---------------------------------------------------------------------------
async def test_silent_is_reaped_with_work_kept(
    tmp: str, store: StateStore,
) -> list[str]:
    failures: list[str] = []
    repo_dir = os.path.join(tmp, "silent")
    os.makedirs(repo_dir, exist_ok=True)

    runner = ClaudeRunner(store=store)
    instance = _make_instance(repo_dir)
    proc = _FakeProc()

    notices: list[tuple[str, str]] = []

    async def on_progress(headline: str, detail: str) -> None:
        notices.append((headline, detail))

    # Two real turns land, then stdout goes quiet forever.
    proc.stdout.push_line(json.dumps(_assistant_event("Reading the card.")).encode())
    proc.stdout.push_line(json.dumps(_assistant_event(LAST_TEXT)).encode())

    with _Knobs(
        MAX_PROCESS_LIFETIME_SECS=0,
        MAX_PROCESS_SILENCE_SECS=1,
        MAX_PROCESS_HARD_LIFETIME_SECS=0,
        WATCHDOG_TICK_SECS=0.2,
    ):
        result = await asyncio.wait_for(
            runner._stream_output(proc, instance, on_progress, None),  # type: ignore[arg-type]
            timeout=30,
        )

    if not proc.terminated:
        failures.append("a silent, over-age process was not terminated")
    if not result.is_error:
        failures.append("a reaped run should report is_error=True")
    msg = (result.error_message or "").lower()
    if "lifetime limit" not in msg:
        failures.append(
            f"error_message must contain 'lifetime limit' (parser matches on "
            f"it to suppress account failover); got {result.error_message!r}"
        )
    if not is_account_agnostic_error(result.error_message or ""):
        failures.append(
            "is_account_agnostic_error did not match the reap message — the "
            "run would be handed to the backup account to burn the same hours"
        )
    # The whole point of contract 4: the work is not thrown away.
    if result.session_id != SESSION_ID:
        failures.append(
            f"reap must carry the captured session_id so Retry resumes; got "
            f"{result.session_id!r}"
        )
    if LAST_TEXT not in (result.result_text or ""):
        failures.append(
            f"reap must recover the last assistant text; got "
            f"{result.result_text!r}"
        )
    # num_turns is deliberately NOT synthesised here — see
    # _carry_forward_work_record, which refuses to merge it for the same
    # reason: the account-failover heuristic reads >1 turn as proof the
    # account took the turn, so inventing turns could hide a dead backup.
    # The salvage that matters is the tool record, which the chain reads to
    # decide whether a build changed code at all.
    if "Bash" not in (result.tools_used or []):
        failures.append(
            f"reap must report tools_used (the chain reads it to decide "
            f"whether a build changed anything); got {result.tools_used!r}"
        )
    # on_progress also carries ordinary streaming updates, so look for the
    # kill notice among them rather than assuming it is the first.
    kill_notices = [n for n in notices if "hung" in n[0].lower()]
    if not kill_notices:
        failures.append(
            f"the session was killed without being told why; notices seen: "
            f"{[n[0] for n in notices]}"
        )
    elif "produced nothing" not in kill_notices[0][1]:
        failures.append(
            f"kill notice should lead with the silence, not the age; got "
            f"{kill_notices[0][1]!r}"
        )
    return failures


# ---------------------------------------------------------------------------
# Contract 3: the hard ceiling kills a chatty process anyway
# ---------------------------------------------------------------------------
async def test_hard_cap_kills_chatty(tmp: str, store: StateStore) -> list[str]:
    failures: list[str] = []
    repo_dir = os.path.join(tmp, "hardcap")
    os.makedirs(repo_dir, exist_ok=True)

    runner = ClaudeRunner(store=store)
    instance = _make_instance(repo_dir)
    proc = _FakeProc()

    # Never silent (60s would be needed), but the hard ceiling is 1s.
    with _Knobs(
        MAX_PROCESS_LIFETIME_SECS=0,
        MAX_PROCESS_SILENCE_SECS=60,
        MAX_PROCESS_HARD_LIFETIME_SECS=1,
        WATCHDOG_TICK_SECS=0.2,
    ):
        feeder = asyncio.create_task(_feed(proc.stdout, seconds=6.0, every=0.2))
        result = await asyncio.wait_for(
            runner._stream_output(proc, instance, None, None),  # type: ignore[arg-type]
            timeout=30,
        )
        feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feeder

    if not proc.terminated:
        failures.append(
            "the hard ceiling did not stop a process that heartbeats forever"
        )
    msg = (result.error_message or "").lower()
    if "hard lifetime limit" not in msg:
        failures.append(
            f"hard-ceiling kill should say so; got {result.error_message!r}"
        )
    return failures


# ---------------------------------------------------------------------------
# Contract 4: a reap that races a requested stop stands down
# ---------------------------------------------------------------------------
async def test_requested_stop_beats_reap(tmp: str, store: StateStore) -> list[str]:
    """Kill/Steer inside the reap window must still render a quiet tombstone.

    The orphan reap returns early, so it has to honour the same stand-downs the
    memory guard does. Without them a user's Kill lands as a red FAILED card —
    and worse, a failure with no turns is the account-failover heuristic's
    signature, so the run can be restarted on the backup subscription.
    """
    failures: list[str] = []
    repo_dir = os.path.join(tmp, "stopped")
    os.makedirs(repo_dir, exist_ok=True)

    runner = ClaudeRunner(store=store)
    instance = _make_instance(repo_dir)
    proc = _FakeProc()

    proc.stdout.push_line(json.dumps(_assistant_event(LAST_TEXT)).encode())

    async def mark_intentional() -> None:
        # Not at t=0: _stream_output defensively discards a stale marker on
        # entry, so the stop has to be announced after the run is under way.
        await asyncio.sleep(0.5)
        runner._intentional_kills.add(instance.id)

    with _Knobs(
        MAX_PROCESS_LIFETIME_SECS=0,
        MAX_PROCESS_SILENCE_SECS=1,
        MAX_PROCESS_HARD_LIFETIME_SECS=0,
        WATCHDOG_TICK_SECS=0.2,
    ):
        marker = asyncio.create_task(mark_intentional())
        try:
            result = await asyncio.wait_for(
                runner._stream_output(proc, instance, None, None),  # type: ignore[arg-type]
                timeout=30,
            )
        finally:
            marker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await marker
            runner._intentional_kills.discard(instance.id)

    msg = (result.error_message or "").lower()
    if "lifetime limit" in msg:
        failures.append(
            f"a requested stop was reported as an orphan reap: "
            f"{result.error_message!r}"
        )
    if not result.killed_intentionally:
        failures.append(
            "the stop was not classified as intentional, so it renders as a "
            "red FAILED card instead of a quiet tombstone"
        )
    return failures


# ---------------------------------------------------------------------------
# Contract 5 (pure): both wordings survive the failover-suppression match
# ---------------------------------------------------------------------------
def test_both_wordings_suppress_failover() -> list[str]:
    failures: list[str] = []
    events = [_assistant_event(LAST_TEXT)]
    for hard in (False, True):
        res = _lifetime_kill_result(
            events, SESSION_ID, None,
            elapsed_secs=14407, silence_secs=2600, hard_cap=hard,
        )
        label = "hard cap" if hard else "silence"
        if not is_account_agnostic_error(res.error_message or ""):
            failures.append(
                f"{label} wording not matched by is_account_agnostic_error: "
                f"{res.error_message!r}"
            )
        if not res.is_error:
            failures.append(f"{label} result should be is_error=True")
    # The duration formatter is what makes these messages readable — "14407s"
    # is what the original log said and nobody converts that in their head.
    res = _lifetime_kill_result(
        events, SESSION_ID, None,
        elapsed_secs=14407, silence_secs=2600, hard_cap=False,
    )
    if "4h" not in (res.error_message or ""):
        failures.append(
            f"elapsed time should read in hours, got {res.error_message!r}"
        )
    return failures


# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------
async def _amain() -> int:
    tmp = tempfile.mkdtemp(prefix="lifetime-test-")
    state_file = Path(tmp) / "state.json"
    results_dir = Path(tmp) / "results"
    results_dir.mkdir(exist_ok=True)

    saved_results_dir = config.RESULTS_DIR
    config.RESULTS_DIR = results_dir

    store = StateStore(state_file, results_dir)
    all_failures: list[tuple[str, list[str]]] = []

    try:
        sync_fails = test_both_wordings_suppress_failover()
        if sync_fails:
            all_failures.append(("wordings_suppress_failover", sync_fails))

        for name, coro in (
            ("old_but_alive_survives",
             test_old_but_alive_survives(tmp, store)),
            ("silent_is_reaped_with_work_kept",
             test_silent_is_reaped_with_work_kept(tmp, store)),
            ("hard_cap_kills_chatty",
             test_hard_cap_kills_chatty(tmp, store)),
            ("requested_stop_beats_reap",
             test_requested_stop_beats_reap(tmp, store)),
        ):
            fails = await coro
            if fails:
                all_failures.append((name, fails))
    finally:
        config.RESULTS_DIR = saved_results_dir
        shutil.rmtree(tmp, ignore_errors=True)

    if all_failures:
        print("FAIL: process lifetime cap suite")
        for name, fails in all_failures:
            print(f"  [{name}]")
            for f in fails:
                print(f"    - {f}")
        return 1

    print("PASS: process lifetime cap suite (5 cases)")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
