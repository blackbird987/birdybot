#!/usr/bin/env python3
"""Regression test: an un-compactable session is abandoned for a fresh one
instead of wedging the thread it is bound to.

The incident (thread 1544694612025679975, 2026-09-03). The Claude Code CLI
aborts, exit 1, with

    Prompt is too long · automatic compaction failed: summarization produced
    empty response

Five runs died that way against the SAME session (1dbf08aa) — t-7998, t-7999,
t-8000, q-16143, q-16158 — because nothing recognised the error. Each failure
left the thread bound to that session, so the next message resumed it and died
again in seconds. The user's report was "prompt too long just stops and doesn't
recover", and there was no recovery to have: the only escape was to abandon the
thread by hand.

It is the exact inverse of the thrash next door (test_context_thrash.py). A
thrash counter lives in the CLI *process*, so resuming clears it. This lives in
the *session*: the oversized transcript is on disk and every resume feeds it
back to the summariser that just failed.

Asserted here:

  * both real CLI wordings are recognised; ordinary failures, thrash text and
    prose that merely mentions the phrase are not
  * one overflow is retried on the SAME session — the two failures seen in the
    wild came from the summariser, not the transcript, and that retry costs
    seconds
  * an overflow that survives the retry abandons the session: the next spawn
    carries no ``--resume``, and its prompt opens with the recovery note plus
    the thread history the platform supplied
  * the user is told once, with wording that matches what actually happened
  * the abandoned attempt's record of work survives into the final result
  * ``session_recovery_exhausted`` is set, and ``should_bind_session`` accepts
    it — this is the assertion that unwedges the thread, because a thread that
    keeps the dead id is a thread that dies on its next message too
  * a briefing callback that raises costs the new session its memory, not its
    existence
  * CONTEXT_OVERFLOW_FRESH=0 restores the old surface-the-failure behaviour
  * the one-shot note never leaks into a later attempt
  * a plain build failure never enters any of this

Strategy follows scripts/test_context_thrash.py: stub the subprocess boundary
only, so the real recovery cascade in _run_impl executes.

Run: ``python scripts/test_context_overflow.py``  (exit 0 on pass).
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import copy
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Same reasoning as test_context_thrash: importing bot.config runs a real
# path-map init that would drop a root marker in whoever's home this runs under.
os.environ.setdefault("BOT_PATHS_DISABLED", "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude import runner as runner_mod
from bot.claude.parser import (
    is_account_agnostic_error,
    is_context_overflow_error,
    is_context_thrash_error,
)
from bot.claude.runner import ClaudeRunner
from bot.claude.types import Instance, InstanceStatus, InstanceType, RunResult
from bot.engine.lifecycle import make_progress_callbacks, should_bind_session

# Verbatim from data/logs/bot.log, 2026-09-03 09:40:27 and 10:35:17. The "·"
# is the CLI's own separator between the abort and why compaction couldn't
# save it.
OVERFLOW = (
    "Prompt is too long · automatic compaction failed: summarization "
    "produced empty response"
)
OVERFLOW_FLAGGED = (
    "Prompt is too long · automatic compaction failed: API Error: Fable "
    "5's safeguards flagged this message "
    "(https://www.anthropic.com/legal/aup). This sometimes happens with safe, "
    "normal conversations."
)

INSTANCE_ID = "t-overflow"
# The conversation that will not compact — the one that must be let go of.
DEAD_SESSION = "1dbf08aa-73b5-4ace-a632-1dbeba2de550"
# What the fresh spawn creates in its place.
FRESH_SESSION = "07b1618c-2e8a-4f61-9c3d-6a1f0b2e4d55"

# What ForumManager.build_prime_briefing returns: nonce-fenced quoted Discord
# messages, and nothing that says how to read them.
RAW_DIGEST = (
    "NONCE: 0123456789abcdef\n"
    "<<<PRIOR-0123456789abcdef kind=prior_user_msg\n"
    "fix the cache\n"
    "PRIOR-0123456789abcdef>>>"
)
# What lifecycle.on_context_reset hands the runner: the same digest wrapped in
# the frame that makes it data. _check_reset_block asserts the real closure
# produces exactly this.
BRIEFING = (
    f"{config.prime_preamble(config.PRIME_SITUATION_LOST)}\n\n"
    f"{RAW_DIGEST}\n\n---"
)


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
    _next_pid = 94001

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

    def __init__(self, tmp: str, outcomes: list[RunResult], *, briefing=BRIEFING):
        self.account = os.path.join(tmp, "acct_primary")
        self.repo = os.path.join(tmp, "repo")
        for p in (self.account, self.repo):
            os.makedirs(p, exist_ok=True)
        self.outcomes = outcomes
        # None = no platform briefing available (a non-forum caller);
        # an Exception instance = the platform tried and blew up.
        self.briefing = briefing
        self.spawn_argvs: list[list[str]] = []
        self.prompts: list[str] = []
        self.progress: list[tuple[str, str]] = []
        self.recoveries: list[tuple[str, str | None, str | None]] = []
        self.reset_calls = 0

    async def run(self, *, session_id: str | None = DEAD_SESSION):
        saved_accounts = list(config.CLAUDE_ACCOUNTS)
        saved_spawn = asyncio.create_subprocess_exec
        saved_unusable = runner_mod.unusable_reason
        config.CLAUDE_ACCOUNTS[:] = [self.account]
        runner_mod.unusable_reason = lambda acct: None  # type: ignore[assignment]

        async def fake_spawn(*args, **kwargs):
            self.spawn_argvs.append(list(args))
            return _FakeProc(self.prompts)

        asyncio.create_subprocess_exec = fake_spawn  # type: ignore[assignment]

        runner = ClaudeRunner()
        calls = {"n": 0}

        async def fake_stream_output(proc, instance, on_progress, on_stall, **kw):
            i = calls["n"]
            calls["n"] += 1
            # Deep-copied for the same reason as the thrash harness: the real
            # _stream_output builds a fresh RunResult per attempt, so handing
            # the same object back twice would let a merge fold a result into
            # itself, which cannot happen in production.
            return copy.deepcopy(self.outcomes[min(i, len(self.outcomes) - 1)])

        runner._stream_output = fake_stream_output  # type: ignore[assignment]

        async def on_progress(headline, detail=""):
            self.progress.append((headline, detail))

        async def on_recovery(reason, lost_session_id, worktree_path):
            self.recoveries.append((reason, lost_session_id, worktree_path))

        async def on_context_reset():
            self.reset_calls += 1
            if isinstance(self.briefing, Exception):
                raise self.briefing
            return self.briefing

        instance = Instance(
            id=INSTANCE_ID,
            name=None,
            instance_type=InstanceType.TASK,
            prompt="Make the backtest chart load from cache.",
            repo_name="AIAgent",
            repo_path=self.repo,
            status=InstanceStatus.RUNNING,
            session_id=session_id,
            mode="build",
        )
        runner._active_tasks.add(instance.id)
        try:
            result = await runner.run(
                instance,
                on_progress=on_progress,
                on_recovery=on_recovery,
                on_context_reset=on_context_reset,
            )
        finally:
            runner._active_tasks.discard(instance.id)
            asyncio.create_subprocess_exec = saved_spawn  # type: ignore[assignment]
            runner_mod.unusable_reason = saved_unusable  # type: ignore[assignment]
            config.CLAUDE_ACCOUNTS[:] = saved_accounts
        return result, instance

    def resume_ids(self) -> list[str | None]:
        """Session id each spawn passed to --resume, None when it didn't."""
        out: list[str | None] = []
        for argv in self.spawn_argvs:
            if "--resume" in argv:
                out.append(argv[argv.index("--resume") + 1])
            else:
                out.append(None)
        return out


def _overflow_result(*, work: bool = True) -> RunResult:
    """What the runner sees when the CLI gives up on an oversized session.

    ``work=True`` is the first abort of a build that had already been running:
    edits are on disk even though this turn produced nothing. ``work=False``
    is every abort after it — the retry dies during compaction, before the
    agent gets a turn, so it has no record of its own to contribute.

    num_turns=0 with no output is deliberate and is the real shape: it is
    also the account-failover heuristic's signature for "this account fell
    over instantly", which is why parser.is_account_agnostic_error has to
    know this wording.
    """
    return RunResult(
        is_error=True,
        error_message=OVERFLOW,
        session_id=DEAD_SESSION,
        num_turns=0,
        tools_used=["Read", "Edit"] if work else [],
        bash_commands=["dotnet build"] if work else [],
    )


def _check_detector(failures: list[str]) -> None:
    for wording in (OVERFLOW, OVERFLOW_FLAGGED):
        if not is_context_overflow_error(wording):
            failures.append(f"real CLI wording not recognised: {wording[:60]!r}")
    if not is_context_overflow_error(OVERFLOW.upper()):
        failures.append("detection is case-sensitive")
    for benign in (
        "",
        "Exit code 1",
        "Build failed: 3 compile errors",
        "No conversation found with session ID: abc",
        "Claude AI usage limit reached|1755300000",
    ):
        if is_context_overflow_error(benign):
            failures.append(f"false positive on {benign[:50]!r}")
    # The one that actually bites: this repo's own sessions write about the
    # failure constantly, and the caller falls back to result_text when
    # error_message is empty. A work product must not cost a thread its session.
    prose = (
        "I looked into the reported failure. The CLI aborts with 'Prompt is "
        "too long' when automatic compaction failed, and the bot did not "
        "recognise it, so the thread stayed bound to a session that could "
        "never load again. I have added a predicate for it in parser.py and "
        "wired a two-rung recovery into the runner, plus a harness. The fix "
        "is on the branch; here is what each part does and why the ordering "
        "of the two rungs matters for a chain that is mid-build."
    )
    if is_context_overflow_error(prose):
        failures.append(
            "a session that WROTE about this failure would have its own "
            "session abandoned — the length guard is not holding"
        )
    # An overflow abort has no output and no completed turns, which is exactly
    # the account-failover heuristic's signature for "this account fell over
    # instantly". Handing it to the backup subscription resumes the same
    # transcript on the same summariser and burns a second account's quota to
    # learn nothing.
    if not is_account_agnostic_error(OVERFLOW):
        failures.append(
            "an un-compactable session is not marked account-agnostic, so a "
            "two-account setup fails it over to the backup subscription"
        )
    # The neighbouring failure must stay in its own branch: it is answered by
    # resuming, and this one by not resuming.
    thrash = (
        "Autocompact is thrashing: the context refilled to the limit within 3 "
        "turns of the previous compact, 3 times in a row."
    )
    if is_context_overflow_error(thrash):
        failures.append("a thrash abort is misread as an un-compactable session")
    if is_context_thrash_error(OVERFLOW):
        failures.append("an un-compactable session is misread as a thrash")


async def _check_reset_block(failures: list[str]) -> None:
    """The real lifecycle closure must FRAME the history, not just fetch it.

    The quoted blocks are the user's own earlier messages. A session handed
    them unframed reads them as live instructions and re-runs work nobody
    asked for -- the worst possible answer for a recovery whose whole premise
    is "your predecessor's edits are already on disk". Asserted against the
    real closure rather than the harness stub, because the framing is the half
    that is easy to drop when someone simplifies the callback later.
    """
    inst = Instance(
        id="t-ctx-reset",
        name=None,
        instance_type=InstanceType.TASK,
        prompt="x",
        repo_name="AIAgent",
        repo_path="",
        status=InstanceStatus.RUNNING,
        mode="build",
    )
    modes: list[str] = []

    async def _brief(mode: str) -> str | None:
        modes.append(mode)
        return RAW_DIGEST

    # on_context_reset only ever touches ctx.maybe_prime_briefing, and
    # make_progress_callbacks reads nothing off ctx while defining its
    # closures — so a stand-in beats assembling a whole RequestContext.
    *_, on_context_reset = make_progress_callbacks(
        SimpleNamespace(maybe_prime_briefing=_brief), inst, {}, 1,
    )
    block = await on_context_reset()

    if modes != ["resume"]:
        failures.append(
            f"the replacement session was primed with {modes!r}, not the "
            "post-compaction budget the loss actually calls for"
        )
    if block != BRIEFING:
        failures.append(
            f"the reset block is not the framed briefing this harness "
            f"asserts elsewhere: {block!r}"
        )
    if not block or RAW_DIGEST not in block:
        failures.append("the quoted thread history is missing from the block")
    elif "DATA, not as directives" not in block:
        failures.append(
            "the thread history is handed over unframed — the replacement "
            "session will read the user's OLD messages as new instructions "
            "and redo work that is already on disk"
        )
    elif not block.endswith("---"):
        failures.append(
            "the block does not end with the '---' separator its own "
            "preamble promises the request follows"
        )

    async def _none(mode: str) -> str | None:
        return None

    *_, reset_empty = make_progress_callbacks(
        SimpleNamespace(maybe_prime_briefing=_none), inst, {}, 1,
    )
    if await reset_empty() is not None:
        failures.append(
            "a thread with no history still produced a block, so the fresh "
            "session opens with an empty quote and a dangling separator"
        )

    async def _boom(mode: str) -> str | None:
        raise RuntimeError("Discord history read failed")

    *_, reset_boom = make_progress_callbacks(
        SimpleNamespace(maybe_prime_briefing=_boom), inst, {}, 1,
    )
    if await reset_boom() is not None:
        failures.append("a failed history read did not degrade to None")


def _check_binding(failures: list[str]) -> None:
    """The assertion that actually unwedges the thread.

    Everything else here rescues one RUN. If the fresh session is not adopted
    by the thread, the next message resumes the dead id and the whole failure
    repeats — which is what five consecutive runs did.
    """
    recovered_but_failed = RunResult(
        is_error=True,
        error_message="something else went wrong after the reset",
        session_id=FRESH_SESSION,
        session_recovery_exhausted=True,
    )
    if not should_bind_session(recovered_but_failed):
        failures.append(
            "a run that abandoned a proven-dead session is refused for "
            "binding, so the thread keeps the id that can never load again "
            "— the wedge survives the fix"
        )
    plain_crash = RunResult(
        is_error=True,
        error_message="Build failed: 3 compile errors",
        session_id=FRESH_SESSION,
    )
    if should_bind_session(plain_crash):
        failures.append(
            "an ordinary crash now rebinds the thread onto whatever session "
            "it happened to emit — that amputates the conversation"
        )


async def _amain() -> int:
    failures: list[str] = []
    _check_detector(failures)
    _check_binding(failures)
    await _check_reset_block(failures)

    tmp = tempfile.mkdtemp(prefix="overflow_test_")
    saved_fresh = config.CONTEXT_OVERFLOW_FRESH
    try:
        # --- Case 1: overflow once, then the retry succeeds ------------------
        # The summariser blipped; the transcript was fine. Two spawns, both on
        # the same conversation, and no note — nothing was lost to explain.
        h = _Harness(tmp, [
            _overflow_result(),
            RunResult(
                is_error=False,
                session_id=DEAD_SESSION,
                result_text="cache wired up",
                num_turns=9,
                tools_used=["Bash"],
            ),
        ])
        result, instance = await h.run()

        if len(h.spawn_argvs) != 2:
            failures.append(
                f"a transient compaction failure spawned {len(h.spawn_argvs)} "
                "times, expected 2 (one retry on the same session)"
            )
        elif h.resume_ids()[1] != DEAD_SESSION:
            failures.append(
                "the retry did not resume the same conversation "
                f"({h.resume_ids()[1]!r}); every turn before the abort is lost"
            )
        if result.is_error:
            failures.append(f"the recovered run reported an error: {result.error_message!r}")
        if h.reset_calls:
            failures.append(
                "the thread history was rebuilt for a session that was never "
                "abandoned — that is a 12K-token briefing for nothing"
            )
        if h.recoveries:
            failures.append(
                "a recoverable blip told the user it had lost their context: "
                f"{h.recoveries}"
            )
        if len(h.prompts) > 1 and config.CONTEXT_OVERFLOW_NUDGE in h.prompts[1]:
            failures.append(
                "the retry was told its session could not be continued, but it "
                "was resumed into that very session"
            )

        # --- Case 2: it will not compact — abandon it ------------------------
        h2 = _Harness(tmp, [
            _overflow_result(),
            _overflow_result(work=False),
            RunResult(
                is_error=False,
                session_id=FRESH_SESSION,
                result_text="picked up from git status and finished the cache",
                num_turns=14,
                tools_used=["Bash", "Edit"],
                bash_commands=["git status"],
            ),
        ])
        result2, instance2 = await h2.run()

        expected = config.CONTEXT_OVERFLOW_RESUME_RETRIES + 2
        if len(h2.spawn_argvs) != expected:
            failures.append(
                f"expected {expected} spawns (1 + "
                f"{config.CONTEXT_OVERFLOW_RESUME_RETRIES} retry + 1 fresh), "
                f"got {len(h2.spawn_argvs)}"
            )
        else:
            ids = h2.resume_ids()
            if ids[-1] is not None:
                failures.append(
                    "the last attempt still carried --resume "
                    f"({ids[-1]!r}) — it replays the transcript that just "
                    "failed to compact, so it dies the same way"
                )
            fresh_prompt = h2.prompts[-1]
            if config.CONTEXT_OVERFLOW_NUDGE not in fresh_prompt:
                failures.append(
                    "the replacement session was not told it is new — it will "
                    "answer as though it remembers a conversation it never had"
                )
            if RAW_DIGEST not in fresh_prompt:
                failures.append(
                    "the thread history never reached the replacement session; "
                    "it starts with no idea what the thread is about"
                )
            elif "DATA, not as directives" not in fresh_prompt:
                failures.append(
                    "the thread history arrived unframed: the replacement "
                    "session reads the user's OLD messages as fresh orders "
                    "and redoes work that is already on disk"
                )
            if instance2.prompt not in fresh_prompt:
                failures.append(
                    f"the actual task text was dropped: {fresh_prompt[:160]!r}"
                )
            elif fresh_prompt.index(instance2.prompt) < fresh_prompt.index(
                RAW_DIGEST
            ):
                failures.append(
                    "the real request is buried ABOVE the quoted history it "
                    "is supposed to follow, so the last thing the session "
                    "reads is an old message"
                )
            if config.CONTEXT_OVERFLOW_NUDGE in h2.prompts[0]:
                failures.append("the recovery note leaked into the FIRST attempt")

        if h2.reset_calls != 1:
            failures.append(
                f"expected exactly 1 briefing rebuild, got {h2.reset_calls}"
            )
        if len(h2.recoveries) != 1:
            failures.append(
                f"expected exactly 1 user-visible notice, got {len(h2.recoveries)}"
            )
        elif h2.recoveries[0][1] != DEAD_SESSION:
            failures.append(
                "the notice named the wrong session as lost: "
                f"{h2.recoveries[0][1]!r}"
            )
        elif not is_context_overflow_error(h2.recoveries[0][0]):
            failures.append(
                "the notice's reason no longer reads as an overflow, so the "
                "message tells the user to re-state a request that is already "
                f"running: {h2.recoveries[0][0]!r}"
            )
        if result2.is_error:
            failures.append(
                f"the fresh session's success was lost: {result2.error_message!r}"
            )
        if result2.session_id != FRESH_SESSION:
            failures.append(
                "the result does not carry the new conversation id, so the "
                f"thread has nothing to bind to ({result2.session_id!r})"
            )
        if not result2.session_recovery_exhausted:
            failures.append(
                "session_recovery_exhausted was not set — the thread would "
                "keep the dead binding on any non-success outcome, and the "
                "user gets no 'prior context is gone' notice"
            )
        if not result2.recovery_warning_posted:
            failures.append(
                "the posted warning was not recorded, so commands.py posts a "
                "second, terser duplicate on top of it"
            )
        if not should_bind_session(result2):
            failures.append("the thread refuses to adopt the replacement session")
        if instance2._context_overflow_note is not None:
            failures.append(
                "the one-shot note was left set — a later turn on this "
                "instance would open with a stale 'your session could not be "
                "continued'"
            )
        if "Edit" not in (result2.tools_used or []):
            failures.append(
                "the abandoned attempt's file edits vanished from the tool "
                f"record ({result2.tools_used!r}); the chain would read this "
                "build as 'no code changes made' and skip the review"
            )
        if result2.bash_commands != ["dotnet build", "git status"]:
            failures.append(f"the bash log lost a run: {result2.bash_commands!r}")

        # --- Case 3: the briefing callback blows up --------------------------
        # Losing the thread history is a degraded recovery. Losing the recovery
        # is a wedged thread. It must fail in that direction.
        h3 = _Harness(tmp, [
            _overflow_result(),
            _overflow_result(work=False),
            RunResult(is_error=False, session_id=FRESH_SESSION, result_text="ok"),
        ], briefing=RuntimeError("Discord history read failed"))
        result3, _ = await h3.run()

        if len(h3.spawn_argvs) != expected:
            failures.append(
                "a failed briefing cost the run its replacement session "
                f"({len(h3.spawn_argvs)} spawns) — the thread stays wedged"
            )
        elif config.CONTEXT_OVERFLOW_NUDGE not in h3.prompts[-1]:
            failures.append(
                "a failed briefing also swallowed the recovery note, so the "
                "new session has neither history nor an explanation"
            )
        if result3.is_error:
            failures.append("a failed briefing was treated as a failed run")

        # --- Case 4: the escape hatch is switchable off ----------------------
        config.CONTEXT_OVERFLOW_FRESH = False
        h4 = _Harness(tmp, [_overflow_result()])
        result4, instance4 = await h4.run()
        config.CONTEXT_OVERFLOW_FRESH = saved_fresh

        if len(h4.spawn_argvs) != config.CONTEXT_OVERFLOW_RESUME_RETRIES + 1:
            failures.append(
                "CONTEXT_OVERFLOW_FRESH=0 still started a fresh session "
                f"({len(h4.spawn_argvs)} spawns)"
            )
        if not result4.is_error:
            failures.append("an unrecoverable overflow was reported as success")
        if not is_context_overflow_error(result4.error_message or ""):
            failures.append(
                "the real reason was replaced by something else: "
                f"{result4.error_message!r}"
            )
        if instance4.session_id != DEAD_SESSION:
            failures.append(
                "the session was dropped anyway with the switch off, so Retry "
                f"has nothing to resume ({instance4.session_id!r})"
            )

        # --- Case 5: an ordinary failure must not enter this path ------------
        h5 = _Harness(tmp, [
            RunResult(
                is_error=True,
                error_message="Build failed: 3 compile errors",
                session_id=DEAD_SESSION,
                num_turns=8,
                result_text="dotnet build failed",
            ),
        ])
        result5, instance5 = await h5.run()
        if len(h5.spawn_argvs) != 1:
            failures.append(
                f"a plain build failure was retried {len(h5.spawn_argvs)} times"
            )
        if h5.recoveries or h5.reset_calls:
            failures.append(
                "a plain build failure triggered context recovery: "
                f"{h5.recoveries}, briefings={h5.reset_calls}"
            )
        if instance5.session_id != DEAD_SESSION:
            failures.append(
                "a plain build failure cost the thread its session "
                f"({instance5.session_id!r})"
            )
    finally:
        config.CONTEXT_OVERFLOW_FRESH = saved_fresh
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL: context-overflow recovery")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASS: an un-compactable session is retried once, then abandoned for a")
    print("      fresh one primed with the thread's history — and the thread")
    print("      adopts it, so the next message doesn't die the same way.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
