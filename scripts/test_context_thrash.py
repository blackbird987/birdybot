#!/usr/bin/env python3
"""Regression test: an autocompact-thrash abort resumes itself instead of
dead-ending a chain on a button whose only job is "run it again".

The incident (thread 1538278119352311920, 2026-08-16, and 13 more like it in
the logs since Aug 11 — every one in the same large C# repo): the Claude Code
CLI kills its own process, exit 1, with

    Autocompact is thrashing: the context refilled to the limit within 3 turns
    of the previous compact, 3 times in a row.

Two 20-minute builds died that way an hour apart, each blocking a verify chain
and paging the user, who clicked Retry once and had it work both times. It
works because that counter lives in the CLI *process*, not the conversation: a
resumed session starts from the compact summary with it back at zero. So the
click carried no information and the bot should make it itself.

Asserted here:

  * the thrash text is recognised, and ordinary build failures are not
  * a thrash on attempt 1 is auto-resumed and the run returns the SUCCESS
  * the retry carries ``--resume <id>`` even though attempt 1 was a fresh
    spawn — the id has to come from the stream, because a killed process
    never emits the ``result`` event that normally carries it
  * the retry lands on the account that owns the session
  * the resumed attempt's prompt is prefixed with the recovery note (and the
    first attempt's is not), so the agent knows its edits are still on disk
  * the user is told, once per resume
  * the aborted attempt's record of work — which tools it used, what it ran,
    any main-repo paths it reached for — survives into the final result, so a
    build that made all its edits before dying isn't reported as one that
    changed nothing
  * thrashing forever gives up after CONTEXT_THRASH_MAX_RETRIES and surfaces
    the real error rather than looping
  * a non-thrash error never enters this path
  * a resume that is refused before it can spawn (fleet on cooldown) still
    clears the "you were just aborted" flag, so the cooldown retry that
    re-queues the same instance later doesn't inherit a stale recovery note

Strategy follows scripts/test_dead_session_recovery.py: stub the subprocess
boundary only, so the real recovery cascade in _run_impl executes.

Run: ``python scripts/test_context_thrash.py``  (exit 0 on pass).
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import copy
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Same reasoning as test_dead_session_recovery: importing bot.config runs a
# real path-map init that would drop a root marker in whoever's home this runs
# under. Nothing here depends on the map.
os.environ.setdefault("BOT_PATHS_DISABLED", "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude import runner as runner_mod
from bot.claude.parser import is_context_thrash_error
from bot.claude.runner import ClaudeRunner
from bot.claude.types import Instance, InstanceStatus, InstanceType, RunResult

# Verbatim from data/logs/bot.log, 2026-08-16 16:13:25.
THRASH = (
    "Autocompact is thrashing: the context refilled to the limit within 3 "
    "turns of the previous compact, 3 times in a row. A file being read or a "
    "tool output is likely too large for the context window. Try reading in "
    "smaller chunks, or use /clear to start fresh."
)

# The conversation the killed process had created — the one the resume must
# find. Not on the instance at spawn time: attempt 1 is a fresh spawn.
BORN_SESSION = "f2a6eb8a-71f4-4977-9a9f-0ffe10ab6a4a"


class _FakeStdin:
    """Records the prompt so the recovery note can be asserted on."""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def write(self, data):
        self._sink.append(
            data.decode("utf-8") if isinstance(data, bytes) else str(data)
        )
        return None

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None


class _FakeProc:
    _next_pid = 92001

    def __init__(self, sink: list[str]):
        self.pid = _FakeProc._next_pid
        _FakeProc._next_pid += 1
        self.returncode = 0
        self.stdin = _FakeStdin(sink)
        self.stdout = None
        self.stderr = None

    def kill(self):
        return None

    async def wait(self):
        return 0


class _Harness:
    """One scripted run of the real cascade against a stubbed subprocess."""

    def __init__(self, tmp: str, outcomes: list[RunResult], *, after_attempt=None):
        self.account = os.path.join(tmp, "acct_primary")
        self.repo = os.path.join(tmp, "repo")
        for p in (self.account, self.repo):
            os.makedirs(p, exist_ok=True)
        self.outcomes = outcomes
        # Called (runner, attempt_index) as each attempt finishes. Case 4 uses
        # it to move the fleet onto cooldown between attempts.
        self.after_attempt = after_attempt
        self.spawn_argvs: list[list[str]] = []
        self.spawn_accounts: list[str | None] = []
        self.prompts: list[str] = []
        self.progress: list[tuple[str, str]] = []

    async def run(self, *, session_id: str | None = None) -> tuple[RunResult, Instance]:
        saved_accounts = list(config.CLAUDE_ACCOUNTS)
        saved_spawn = asyncio.create_subprocess_exec
        saved_unusable = runner_mod.unusable_reason
        config.CLAUDE_ACCOUNTS[:] = [self.account]
        runner_mod.unusable_reason = lambda acct: None  # type: ignore[assignment]

        async def fake_spawn(*args, **kwargs):
            self.spawn_argvs.append(list(args))
            env = kwargs.get("env") or {}
            self.spawn_accounts.append(env.get("CLAUDE_CONFIG_DIR"))
            return _FakeProc(self.prompts)

        asyncio.create_subprocess_exec = fake_spawn  # type: ignore[assignment]

        runner = ClaudeRunner()
        calls = {"n": 0}

        async def fake_stream_output(proc, instance, on_progress, on_stall, **kw):
            i = calls["n"]
            calls["n"] += 1
            # Beyond the script, keep returning the last outcome — a thrash
            # loop must be stopped by the retry cap, not by running out of
            # scripted answers.  Deep-copied because the real _stream_output
            # builds a fresh RunResult per attempt: handing the same object
            # back twice would let a merge across attempts fold a result into
            # itself, which cannot happen in production.
            outcome = copy.deepcopy(self.outcomes[min(i, len(self.outcomes) - 1)])
            if self.after_attempt:
                self.after_attempt(runner, i)
            return outcome

        runner._stream_output = fake_stream_output  # type: ignore[assignment]

        async def on_progress(headline, detail=""):
            self.progress.append((headline, detail))

        instance = Instance(
            id="t-thrash",
            name=None,
            instance_type=InstanceType.TASK,
            prompt="Implement the ledger reconciliation policy.",
            repo_name="AIAgent",
            repo_path=self.repo,
            status=InstanceStatus.RUNNING,
            session_id=session_id,
            mode="build",
        )
        try:
            result = await runner.run(instance, on_progress=on_progress)
        finally:
            asyncio.create_subprocess_exec = saved_spawn  # type: ignore[assignment]
            runner_mod.unusable_reason = saved_unusable  # type: ignore[assignment]
            config.CLAUDE_ACCOUNTS[:] = saved_accounts
        return result, instance

    def resume_ids(self) -> list[str | None]:
        """Session id each spawn passed to --resume, None when it didn't."""
        out: list[str | None] = []
        for argv in self.spawn_argvs:
            # create_subprocess_exec is called as (*cmd), so argv is flat.
            if "--resume" in argv:
                out.append(argv[argv.index("--resume") + 1])
            else:
                out.append(None)
        return out


def _thrash_result() -> RunResult:
    """What the runner sees when the CLI kills itself for thrashing.

    The session id is present even though a killed process never emits the
    ``result`` event that normally carries one: _stream_output is stubbed in
    these cases, so this stands in for the already-applied init-event fallback.
    That the fallback itself works is checked separately, against the real
    stream loop, in _check_session_id_survives_a_kill.
    """
    return RunResult(
        is_error=True,
        error_message=THRASH,
        session_id=BORN_SESSION,
        num_turns=53,
        # 53 turns of real work: files were edited and commands were run, and
        # all of it is on disk even though the process died. tools_used is the
        # only record the chain has that a build changed code.
        tools_used=["Read", "Edit", "Bash"],
        bash_commands=["dotnet build"],
        cache_read_tokens=400_000,
        cache_creation_tokens=90_000,
    )


def _check_detector(failures: list[str]) -> None:
    if not is_context_thrash_error(THRASH):
        failures.append("the real CLI thrash message is not recognised")
    if not is_context_thrash_error(THRASH.upper()):
        failures.append("detection is case-sensitive")
    for benign in (
        "",
        "Exit code 1",
        "Build failed: 3 compile errors",
        "No conversation found with session ID: abc",
        "Claude AI usage limit reached|1755300000",
        # A session that merely WRITES about the guard (e.g. this very file)
        # must not be mistaken for one that tripped it.
        "I added a note about compaction to the changelog.",
    ):
        if is_context_thrash_error(benign):
            failures.append(f"false positive on {benign[:50]!r}")


class _ScriptedStdout:
    """Feeds pre-canned stream-json lines to the real _stream_output loop."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _ScriptedStderr:
    def __init__(self, blob: bytes) -> None:
        self._blob = blob

    async def read(self) -> bytes:
        return self._blob


class _KilledProc:
    """A CLI that dies mid-stream: no ``result`` event, non-zero exit."""

    def __init__(self, lines: list[bytes], stderr: bytes) -> None:
        self.pid = 92999
        self.returncode = 1
        self.stdin = _FakeStdin([])
        self.stdout = _ScriptedStdout(lines)
        self.stderr = _ScriptedStderr(stderr)

    def kill(self):
        return None

    def terminate(self):
        return None

    async def wait(self):
        return 1


async def _check_session_id_survives_a_kill(failures: list[str]) -> None:
    """The load-bearing half: a killed process must still name its session.

    Everything above stubs _stream_output, so this drives the REAL loop. A
    process killed mid-run never emits the ``result`` event that normally
    carries session_id, so without the init-event fallback a fresh spawn that
    died after 50 turns reports no conversation at all — and then neither the
    auto-resume nor the user's Retry button has anything to resume.
    """
    lines = [
        json.dumps({
            "type": "system", "subtype": "init", "session_id": BORN_SESSION,
        }).encode() + b"\n",
        json.dumps({
            "type": "assistant",
            "session_id": BORN_SESSION,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Reading the ledger service."}],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
        }).encode() + b"\n",
        # ...and then the process is gone. No result event.
    ]
    runner = ClaudeRunner()
    instance = Instance(
        id="t-killed",
        name=None,
        instance_type=InstanceType.TASK,
        prompt="Implement the ledger reconciliation policy.",
        repo_name="AIAgent",
        repo_path=None,
        status=InstanceStatus.RUNNING,
        session_id=None,          # fresh spawn — nothing to fall back to
        mode="build",
    )
    proc = _KilledProc(lines, THRASH.encode())
    # The real stream loop writes recovered assistant text to RESULTS_DIR.
    # Point that at scratch space: the live bot is running out of this same
    # checkout and a test must never drop files into its data directory.
    scratch = tempfile.mkdtemp(prefix="thrash_results_")
    saved_results_dir = config.RESULTS_DIR
    config.RESULTS_DIR = Path(scratch)
    try:
        result = await runner._stream_output(proc, instance, None, None)
    finally:
        config.RESULTS_DIR = saved_results_dir
        shutil.rmtree(scratch, ignore_errors=True)

    if result.session_id != BORN_SESSION:
        failures.append(
            "a process killed before its result event reported no session id "
            f"({result.session_id!r}) — nothing could resume it, by hand or "
            "automatically"
        )
    if not result.is_error:
        failures.append("a non-zero exit was not reported as an error")
    if not is_context_thrash_error(result.error_message or ""):
        failures.append(
            "the thrash reason did not reach error_message, so the recovery "
            f"branch would never match: {result.error_message!r}"
        )


async def _amain() -> int:
    failures: list[str] = []
    _check_detector(failures)
    await _check_session_id_survives_a_kill(failures)

    tmp = tempfile.mkdtemp(prefix="thrash_test_")
    try:
        # --- Case 1: thrash once, then succeed -------------------------------
        h = _Harness(tmp, [
            _thrash_result(),
            RunResult(
                is_error=False,
                session_id=BORN_SESSION,
                result_text="reconciliation policy implemented",
                num_turns=12,
                # The resumed agent finds its edits already on disk (that is
                # what the recovery note tells it to check), so it reads and
                # reports without touching a file. No code-change tool here.
                # TodoWrite is unique to this attempt, so the assertion below
                # proves the merge appends as well as de-duplicates — with
                # only repeats, dropping the resumed list entirely would pass.
                tools_used=["Bash", "Read", "TodoWrite"],
                bash_commands=["git diff --stat"],
                cache_read_tokens=50_000,
                cache_creation_tokens=10_000,
            ),
        ])
        result, instance = await h.run(session_id=None)

        if len(h.spawn_argvs) < 2:
            failures.append(
                "the run was never auto-resumed — the user still has to click "
                f"Retry (spawns={len(h.spawn_argvs)}). This is the reported bug."
            )
        else:
            ids = h.resume_ids()
            if ids[0] is not None:
                failures.append("attempt 1 should be a fresh spawn, not a resume")
            if ids[1] != BORN_SESSION:
                failures.append(
                    "the resume did not pick up the conversation the killed "
                    f"process created (got {ids[1]!r}); every turn of work "
                    "before the abort would be thrown away"
                )
            if h.spawn_accounts[1] != h.account:
                failures.append(
                    f"resume ran on the wrong account ({h.spawn_accounts[1]!r})"
                )
            if len(h.prompts) < 2:
                failures.append("the resumed attempt sent no prompt")
            else:
                if config.CONTEXT_THRASH_NUDGE not in h.prompts[1]:
                    failures.append(
                        "the resumed agent was not told why it was aborted or "
                        "that its edits survive — it will start over"
                    )
                if config.CONTEXT_THRASH_NUDGE in h.prompts[0]:
                    failures.append(
                        "the recovery note leaked into the FIRST attempt"
                    )
                if instance.prompt not in h.prompts[1]:
                    failures.append(
                        "the original task text was dropped on resume: "
                        f"{h.prompts[1][:120]!r}"
                    )

        if result.is_error:
            failures.append(f"final result was an error: {result.error_message!r}")
        if (result.result_text or "").strip() != "reconciliation policy implemented":
            failures.append(f"the resumed run's output was lost: {result.result_text!r}")
        if instance._context_thrash_retry:
            failures.append(
                "the one-shot recovery flag was left set — a later turn on this "
                "instance would be told it had just been aborted"
            )
        notices = [p for p in h.progress if "resuming automatically" in p[0]]
        if len(notices) != 1:
            failures.append(
                f"expected exactly 1 user-visible resume notice, got {len(notices)}"
            )

        # The aborted attempt's record of work must survive into the result the
        # lifecycle layer sees — it ASSIGNS these fields onto the instance.
        if "Edit" not in (result.tools_used or []):
            failures.append(
                "the aborted attempt's file edits vanished from the tool record "
                f"({result.tools_used!r}); the chain would read this build as "
                "'no code changes made' and skip the review"
            )
        if result.tools_used != ["Read", "Edit", "Bash", "TodoWrite"]:
            failures.append(
                "tool record is not the ordered union of both attempts "
                "(aborted first, then whatever the resume added): "
                f"{result.tools_used!r}"
            )
        if result.bash_commands != ["dotnet build", "git diff --stat"]:
            failures.append(
                f"the bash log lost a run: {result.bash_commands!r}"
            )
        if result.cache_read_tokens != 450_000:
            failures.append(
                "cached-token usage was not carried across the resume "
                f"({result.cache_read_tokens})"
            )
        if result.num_turns != 12:
            failures.append(
                "turn count was summed across attempts; the failover heuristic "
                f"reads it as proof an account did real work ({result.num_turns})"
            )

        # --- Case 2: thrashes forever — must give up, not loop ---------------
        h2 = _Harness(tmp, [_thrash_result()])
        result2, _ = await h2.run(session_id=BORN_SESSION)

        expected = config.CONTEXT_THRASH_MAX_RETRIES + 1
        if len(h2.spawn_argvs) != expected:
            failures.append(
                f"a session that always thrashes spawned {len(h2.spawn_argvs)} "
                f"times, expected {expected} (cap = "
                f"{config.CONTEXT_THRASH_MAX_RETRIES} retries)"
            )
        if not result2.is_error:
            failures.append("an unrecoverable thrash was reported as success")
        if not is_context_thrash_error(result2.error_message or ""):
            failures.append(
                "the real reason was replaced by something else: "
                f"{result2.error_message!r}"
            )

        # --- Case 3: an ordinary failure must not enter this path ------------
        h3 = _Harness(tmp, [
            RunResult(
                is_error=True,
                error_message="Build failed: 3 compile errors",
                session_id=BORN_SESSION,
                num_turns=8,
                result_text="dotnet build failed",
            ),
        ])
        result3, _ = await h3.run(session_id=BORN_SESSION)
        if len(h3.spawn_argvs) != 1:
            failures.append(
                f"a plain build failure was retried {len(h3.spawn_argvs)} times "
                "— every failing build would now cost 3 runs"
            )
        if not result3.is_error:
            failures.append("a plain build failure was swallowed")
        if h3.progress:
            failures.append(
                f"a plain build failure posted a recovery notice: {h3.progress}"
            )

        # --- Case 4: the resume never gets to spawn --------------------------
        # The account goes on cooldown between attempts, so the resume hits the
        # refuse-to-spawn short-circuit and returns before _build_command — the
        # one place that clears the "you were just aborted" flag. That path
        # re-queues this same Instance for a later cooldown retry, which would
        # then open with a recovery note about an abort that happened hours ago.
        def _cooldown_after_first(runner, attempt_index):
            # A cooldown, not a signed-out account: when EVERY account fails
            # the credential probe the picker deliberately spawns anyway (the
            # probe is a heuristic and a clean sweep is what a wrong heuristic
            # looks like), so only a cooldown actually reaches the refusal.
            if attempt_index == 0:
                runner._account_cooldowns[h4.account] = (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                )

        h4 = _Harness(tmp, [_thrash_result()], after_attempt=_cooldown_after_first)
        result4, instance4 = await h4.run(session_id=BORN_SESSION)

        if len(h4.spawn_argvs) != 1:
            failures.append(
                f"expected the resume to be refused before spawning, got "
                f"{len(h4.spawn_argvs)} spawns"
            )
        if instance4._context_thrash_retry:
            failures.append(
                "the recovery flag survived a resume that never reached the "
                "prompt builder — the next cooldown retry would be told it had "
                "just been aborted"
            )
        if "Edit" not in (result4.tools_used or []):
            failures.append(
                "a refused resume dropped the aborted attempt's tool record: "
                f"{result4.tools_used!r}"
            )
        if result4.session_id != BORN_SESSION:
            failures.append(
                "the refused resume lost the conversation id, so the cooldown "
                f"retry has nothing to resume ({result4.session_id!r})"
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL: autocompact-thrash auto-resume")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASS: an autocompact-thrash abort resumes its own session (bounded at")
    print(f"      {config.CONTEXT_THRASH_MAX_RETRIES} retries), tells the agent its work survived, and tells")
    print("      the user once. Other failures are untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
