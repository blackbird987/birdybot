#!/usr/bin/env python3
"""Regression test: interrupting a session ends it quietly, not as a red failure.

The incident (2026-08-26, instance q-15391): the user tapped Kill one second
after a session started. The thread showed the expected "Killed [q-15391]"
message — and then, immediately below it, a red **"Failed: Exit code 143"**
card with Retry/Log buttons. The log for the same second says:

    CLI error for q-15391 (exit=143): Exit code 143 | stderr: empty
    Account error unmatched by auth patterns (no-turns heuristic) for q-15391

Nothing about the kill path was broken. The bot's entire "this was deliberate,
render a quiet tombstone" machinery was intact and unreachable, because the one
boolean it hangs off had stopped being computable.

That boolean asks "did the process really die from the signal we sent?" and it
answered by requiring a NEGATIVE returncode — which is how Python reports a
process the kernel killed because it ignored the signal. The Claude CLI is now
a compiled binary that *traps* SIGTERM, shuts down cleanly and exits normally
with 143 (the shell's 128+N convention). Positive. So every Kill and every
Steer read as a crash.

The second line of that log is the sharper edge. A killed run has no output and
no completed turns, which is byte-for-byte the signature the account-failover
branch uses for "this account fell over instantly" — so on a two-account setup
a killed session would have been silently restarted on the backup account. The
guard that prevents exactly that (``runner.py``: ``if result.killed_intentionally:
return result``) keys off the same dead boolean.

Asserted here:

  * the shape test accepts BOTH ways a signal can surface: the kernel-kill
    negative returncode AND the 128+N a process that handles the signal exits
    with. Ordinary failures (0, 1, 2, 255) still are not kills — the test has
    to stay a corroboration, or a real crash during a Steer becomes a silent
    cancellation
  * driven against REAL subprocesses, not a table: a child that traps SIGTERM
    and exits 143 the way the CLI does, and a control child that ignores it and
    has to be SIGKILLed. Both measured returncodes must read as kills. This is
    the arm that would have caught the CLI's behaviour change, because it
    asserts against what a process actually did, not against a constant we
    wrote down
  * a killed run does not look like a broken account, so it can never be handed
    to the failover branch and restarted after the user asked it to stop
  * finalize maps a killed run to KILLED (quiet tombstone) rather than FAILED
    (red card), while still keeping the underlying error text for /log
  * ``/kill`` typed as a command announces the kill as deliberate, exactly like
    the Kill button does. It used to call the runner's plain ``kill()``, whose
    ``intentional`` flag defaults to False — so even with the shape test fixed,
    the typed command would still have produced the red card
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import os
import signal
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.claude.runner import _SIGNAL_EXIT_CODES, is_kill_shape  # noqa: E402
from bot.claude.types import (  # noqa: E402
    Instance,
    InstanceStatus,
    KillOutcome,
    RunResult,
)

# Finalize runs the session evaluator on its way out. Nothing here is a real
# session, so keep the harness from writing scorecards for one.
config.EVAL_ENABLED = False

# ---------------------------------------------------------------------------
# Arm 1 — the shape test itself
# ---------------------------------------------------------------------------


def _check_shape_table(failures: list[str]) -> None:
    """Both signal shapes count as a kill; ordinary exits do not."""
    if os.name == "nt":
        # Windows terminate() always yields 1 and real failures do too, so the
        # test is deliberately a blanket True there — nothing to table-check.
        if not is_kill_shape(1):
            failures.append("Windows: terminate()'s returncode 1 must read as a kill")
        return

    # 128+N — the process handled the signal and exited cleanly. This is the
    # shape the Claude CLI produces and the one that was missing.
    for rc, name in ((143, "SIGTERM"), (137, "SIGKILL"), (130, "SIGINT")):
        if not is_kill_shape(rc):
            failures.append(
                f"exit {rc} (128+{name}, a handled signal) must read as a kill — "
                "this is exactly the q-15391 regression"
            )

    # Negative — the kernel killed a process that ignored the signal.
    for rc in (-signal.SIGTERM, -signal.SIGKILL, -signal.SIGINT):
        if not is_kill_shape(int(rc)):
            failures.append(f"returncode {int(rc)} (killed by signal) must read as a kill")

    # Everything else is a real failure. Widening past this would turn a crash
    # that happened to land during a Steer into a silent cancellation.
    for rc in (0, 1, 2, 255, 127):
        if is_kill_shape(rc):
            failures.append(f"exit {rc} is an ordinary exit and must NOT read as a kill")

    if is_kill_shape(None):
        failures.append("a still-running process (returncode None) must not read as a kill")

    if sorted(_SIGNAL_EXIT_CODES) != [130, 137, 143]:
        failures.append(
            f"accepted 128+N codes drifted: {sorted(_SIGNAL_EXIT_CODES)} != [130, 137, 143]"
        )


# ---------------------------------------------------------------------------
# Arm 2 — measured against real processes
# ---------------------------------------------------------------------------

# Traps SIGTERM, cleans up, exits 128+15 — what the Claude CLI binary does.
_GRACEFUL_CHILD = """
import signal, sys, time
def bye(signum, frame):
    sys.exit(128 + signum)
signal.signal(signal.SIGTERM, bye)
print("ready", flush=True)
time.sleep(60)
"""

# Ignores SIGTERM entirely, so it has to be SIGKILLed — the escalation path
# runner.kill() takes after its 5s grace period.
_STUBBORN_CHILD = """
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ready", flush=True)
time.sleep(60)
"""


async def _spawn_and_terminate(source: str, escalate: bool) -> int:
    """Run *source*, terminate it the way runner.kill does, return the real rc."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", source,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    # Wait until the child has installed its handler, otherwise the signal can
    # land before the trap exists and both cases collapse to the same shape.
    assert proc.stdout is not None
    await asyncio.wait_for(proc.stdout.readline(), timeout=10)
    proc.terminate()
    try:
        return await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        if not escalate:
            raise
        proc.kill()
        return await asyncio.wait_for(proc.wait(), timeout=10)


async def _check_real_processes(failures: list[str]) -> None:
    if os.name == "nt":
        return  # signal semantics differ; the blanket-True case is covered above

    try:
        graceful_rc = await _spawn_and_terminate(_GRACEFUL_CHILD, escalate=False)
    except Exception as exc:
        failures.append(f"could not drive the graceful-shutdown child: {exc!r}")
    else:
        if graceful_rc != 128 + int(signal.SIGTERM):
            failures.append(
                f"harness bug: graceful child exited {graceful_rc}, expected 143"
            )
        if not is_kill_shape(graceful_rc):
            failures.append(
                f"a process that handles SIGTERM and exits {graceful_rc} was not "
                "recognised as killed — the user's Kill would render as a red failure"
            )

    try:
        stubborn_rc = await _spawn_and_terminate(_STUBBORN_CHILD, escalate=True)
    except Exception as exc:
        failures.append(f"could not drive the SIGTERM-ignoring child: {exc!r}")
    else:
        if not is_kill_shape(stubborn_rc):
            failures.append(
                f"a process SIGKILLed after ignoring SIGTERM exited {stubborn_rc} "
                "and was not recognised as killed"
            )


# ---------------------------------------------------------------------------
# Arm 3 — what the classification protects downstream
# ---------------------------------------------------------------------------


def _check_not_an_account_failure(failures: list[str]) -> None:
    """A killed run must not look like an account that fell over.

    This is the failover guard: no output, no turns and an error is the exact
    signature of a dead account, so without the kill flag a cancelled session
    gets restarted on the backup subscription.
    """
    import inspect

    from bot.claude.runner import ClaudeRunner, _no_productive_work

    killed = RunResult(
        is_error=True,
        error_message="Exit code 143",
        result_text="",
        num_turns=0,
        killed_intentionally=True,
        kill_reason="kill",
    )
    if _no_productive_work(killed):
        failures.append(
            "a killed run reads as 'this account produced nothing' — the failover "
            "paths would stamp a usage-limit reset on work the user cancelled"
        )

    # Control: the same shape WITHOUT the kill flag is still a real account
    # suspicion, so the guard is the flag and not an accidental widening.
    genuine = RunResult(
        is_error=True, error_message="Exit code 1", result_text="", num_turns=0,
    )
    if not _no_productive_work(genuine):
        failures.append(
            "an unexplained zero-turn failure no longer reads as an account "
            "problem — real failover detection has been broken"
        )

    # Ordering invariant. The failover branch's own test is "no output, no
    # turns", which a kill matches exactly; the only thing keeping a cancelled
    # session off the backup account is that the killed early-return comes
    # FIRST. Assert the order rather than trusting a comment.
    src = inspect.getsource(ClaudeRunner._run_impl)
    bail = src.find("if result.killed_intentionally:")
    # Anchored on the branch itself. A bare "supports_account_failover" also
    # matches the account-picking code far earlier in the method, which is not
    # what this invariant is about.
    failover = src.find("if result.is_error and provider.supports_account_failover")
    if bail < 0 or failover < 0:
        failures.append(
            "could not locate the killed early-return or the failover branch in "
            "_run_impl — this guard needs rechecking by hand"
        )
    elif bail > failover:
        failures.append(
            "the killed early-return no longer precedes the account-failover "
            "branch: killing a session can hand it to the backup account and "
            "restart the work that was just cancelled"
        )


# --- minimal stubs for driving engine functions -----------------------------


class _Messenger:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []
        self.edits: list[tuple[str, str]] = []
        self.thinking_edits: list[str] = []

    def escape(self, text: str) -> str:
        return text

    async def send_text(self, channel_id, text, buttons=None, **kw):  # noqa: ANN001
        self.sent.append((text, buttons))
        return "msg-1"

    async def edit_text(self, channel_id, msg_id, text, buttons=None, **kw):  # noqa: ANN001
        self.edits.append((msg_id, text))
        return True

    async def edit_thinking(self, handle, text, **kw):  # noqa: ANN001
        self.thinking_edits.append(text)
        return True


class _Store:
    def __init__(self, inst: Instance) -> None:
        self.inst = inst
        self.updates = 0

    def get_instance(self, _id: str) -> Instance:
        return self.inst

    def update_instance(self, inst: Instance, critical: bool = False) -> None:
        self.updates += 1

    def list_instances(self):
        return [self.inst]

    def list_by_status(self, _status):  # noqa: ANN001
        return []

    def add_cost(self, _amount: float) -> None:
        pass

    # run_instance reads these through RequestContext's effective_* properties.
    verbose_level = 1
    context = None


@dataclass
class _Runner:
    """Records how the engine asked for the kill."""
    plain_kill_calls: list[str] = field(default_factory=list)
    wait_calls: list[tuple[str, str | None]] = field(default_factory=list)
    owns_card_calls: list[bool] = field(default_factory=list)
    outcome: KillOutcome = KillOutcome.FINALIZED
    run_result: RunResult | None = None

    async def kill(self, instance_id: str, *, intentional: bool = False,
                   reason: str | None = None, owns_card: bool = False) -> bool:
        self.plain_kill_calls.append(instance_id)
        return True

    async def kill_and_wait(self, instance_id: str, timeout: float = 10.0, *,
                            intentional: bool = True,
                            reason: str | None = None,
                            owns_card: bool = False) -> KillOutcome:
        self.wait_calls.append((instance_id, reason))
        self.owns_card_calls.append(owns_card)
        return self.outcome

    # --- enough surface for lifecycle.run_instance to drive a whole run ---
    def begin_task(self, *a, **kw) -> None:
        pass

    def end_task(self, *a, **kw) -> None:
        pass

    async def run(self, instance, **kw):  # noqa: ANN001
        return self.run_result or RunResult()


def _make_instance() -> Instance:
    from bot.claude.types import InstanceType

    return Instance(
        id="q-99999",
        name=None,
        instance_type=InstanceType.QUERY,
        prompt="anything",
        repo_name="harness",
        # A directory with no git repo in it: finalize asks git whether the
        # run left uncommitted changes, and we want that to be a clean "no"
        # rather than a reading of whatever this checkout happens to contain.
        repo_path=tempfile.gettempdir(),
        status=InstanceStatus.RUNNING,
    )


def _make_ctx(runner: _Runner, store: _Store, messenger: _Messenger):
    from bot.platform.base import RequestContext

    return RequestContext(
        messenger=messenger,      # type: ignore[arg-type]
        channel_id="c1",
        platform="test",
        store=store,              # type: ignore[arg-type]
        runner=runner,            # type: ignore[arg-type]
    )


def _check_finalize_renders_a_tombstone(failures: list[str]) -> None:
    """Killed -> KILLED (quiet), not FAILED (red card), error text preserved."""
    from bot.engine.lifecycle import finalize_run

    inst = _make_instance()
    store = _Store(inst)
    ctx = _make_ctx(_Runner(), store, _Messenger())
    result = RunResult(
        is_error=True,
        error_message="Exit code 143",
        result_text="",
        killed_intentionally=True,
        kill_reason="kill",
    )
    try:
        finalize_run(ctx, inst, result)
    except Exception as exc:
        failures.append(f"finalize_run raised on a killed run: {exc!r}")
        return

    if inst.status != InstanceStatus.KILLED:
        failures.append(
            f"a killed run finalized as {inst.status} — the thread gets the red "
            "'Failed: Exit code 143' card instead of a quiet tombstone"
        )
    if not inst.error:
        failures.append(
            "the underlying error text was dropped; /log and history lose a real "
            "crash that coincided with the kill"
        )


async def _check_typed_kill_is_deliberate(failures: list[str]) -> None:
    """/kill must announce intent, exactly like the Kill button."""
    from bot.engine.commands import on_kill

    inst = _make_instance()
    runner = _Runner()
    messenger = _Messenger()
    ctx = _make_ctx(runner, _Store(inst), messenger)

    await on_kill(ctx, inst.id)

    if runner.plain_kill_calls:
        failures.append(
            "/kill still uses the runner's plain kill(), whose intentional flag "
            "defaults to False — the run is classified as a crash and the user "
            "who asked to stop gets a red FAILED card"
        )
    if not runner.wait_calls:
        failures.append("/kill did not reach kill_and_wait at all")
        return
    _iid, reason = runner.wait_calls[0]
    if reason != "kill":
        failures.append(
            f"/kill passed reason={reason!r}; must be 'kill' so finalize renders "
            "the tombstone and skips the failure path"
        )
    if not any("Killed" in text for text, _ in messenger.sent):
        failures.append("/kill did not confirm the kill in the thread")
    if runner.owns_card_calls and runner.owns_card_calls[0]:
        failures.append(
            "/kill claimed it owns the live progress card, but it has no message "
            "to rewrite — it posts a separate one. Lifecycle then skips its "
            "terminal edit and the progress card is stranded on 'thinking...' "
            "for the rest of the thread's life"
        )

    # The Kill button is the other caller of the same path: it DOES own the
    # card, because it edits the very message it was attached to.
    from bot.engine.commands import handle_callback

    inst_b = _make_instance()
    runner_b = _Runner()
    messenger_b = _Messenger()
    await handle_callback(
        _make_ctx(runner_b, _Store(inst_b), messenger_b),
        "kill", inst_b.id, "msg-live",
    )
    if not runner_b.owns_card_calls or not runner_b.owns_card_calls[0]:
        failures.append(
            "the Kill button no longer claims the progress card, so lifecycle "
            "will also edit it — a slow finalize can overwrite the 'Killed' "
            "message and its Retry/Log buttons"
        )
    if not any(mid == "msg-live" and "Killed" in text
               for mid, text in messenger_b.edits):
        failures.append(
            "the Kill button did not rewrite the message it was attached to"
        )

    # A session that was already gone still has to say so rather than claiming
    # a kill that never happened.
    runner2 = _Runner(outcome=KillOutcome.NOT_RUNNING)
    messenger2 = _Messenger()
    await on_kill(_make_ctx(runner2, _Store(_make_instance()), messenger2), "q-99999")
    if not any("already stopped" in text for text, _ in messenger2.sent):
        failures.append(
            "/kill on a session that is no longer running must say so, not "
            "report a successful kill"
        )


# ---------------------------------------------------------------------------
# Arm 5 (--live) — measure the installed CLI itself
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Arm 6 — the live progress card always reaches a terminal state
# ---------------------------------------------------------------------------


async def _check_progress_card_is_resolved(failures: list[str]) -> None:
    """A stopped run must never leave its progress card on "thinking...".

    Two callers, two correct behaviours. The Kill button rewrites the card
    itself, so lifecycle must keep its hands off it. Typed /kill posts a
    separate message and cannot touch the card, so lifecycle must resolve it.
    Getting this backwards is invisible in the code and glaring in the thread.
    """
    from bot.engine.lifecycle import run_instance
    from bot.platform.base import MessageHandle

    async def _drive(owns_card: bool) -> list[str]:
        inst = _make_instance()
        runner = _Runner()
        runner.run_result = RunResult(
            is_error=True,
            error_message="Exit code 143",
            result_text="",
            num_turns=0,
            killed_intentionally=True,
            kill_reason="kill",
            kill_owns_card=owns_card,
        )
        messenger = _Messenger()
        ctx = _make_ctx(runner, _Store(inst), messenger)
        await run_instance(ctx, inst, MessageHandle(platform="test"))
        return messenger.thinking_edits

    edits = await _drive(owns_card=False)
    if not edits:
        failures.append(
            "/kill leaves the live progress card stranded on 'thinking...' — "
            "nobody rewrites it, so the thread looks like the session is still "
            "running long after it stopped"
        )
    elif not any("stopped" in e for e in edits):
        failures.append(
            f"the progress card for a stopped run reads {edits[-1]!r}; a kill "
            "must read as a stop, not as a failure or a steer"
        )

    edits = await _drive(owns_card=True)
    if edits:
        failures.append(
            f"lifecycle edited the progress card ({edits[-1]!r}) even though the "
            "Kill button owns it — a slow finalize overwrites the 'Killed' "
            "message and loses its Retry/Log buttons"
        )


async def _check_live_cli(failures: list[str]) -> None:
    """Terminate the real CLI and check the returncode it actually produces.

    Opt-in because it spends a real turn. This is the arm that notices the CLI
    changing its shutdown behaviour again: everything else in this file tests
    what the bot does with a returncode, only this tests what the CLI gives it.
    """
    cmd = [config.CLAUDE_BINARY, "-p", "--output-format", "stream-json", "--verbose"]
    print(f"live: spawning {config.CLAUDE_BINARY} and terminating it mid-turn...")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=tempfile.gettempdir(),
    )
    assert proc.stdin is not None
    proc.stdin.write(b"count slowly to five hundred\n")
    await proc.stdin.drain()
    proc.stdin.close()
    # Long enough for the CLI to be genuinely mid-turn — terminating during
    # startup can exit through a different path than the one users hit.
    await asyncio.sleep(6)
    proc.terminate()
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        rc = await proc.wait()
        failures.append("the CLI ignored SIGTERM and had to be SIGKILLed")

    print(f"live: CLI returncode after terminate() = {rc}")
    if not is_kill_shape(rc):
        failures.append(
            f"the installed CLI exits {rc} when terminated, which the bot does "
            "not recognise as a kill — every Kill and Steer will render as a red "
            "failure, and a killed run can reach the account-failover branch"
        )


async def _amain() -> int:
    failures: list[str] = []
    live = "--live" in sys.argv

    _check_shape_table(failures)
    await _check_real_processes(failures)
    _check_not_an_account_failure(failures)
    _check_finalize_renders_a_tombstone(failures)
    await _check_typed_kill_is_deliberate(failures)
    await _check_progress_card_is_resolved(failures)
    if live:
        await _check_live_cli(failures)

    if failures:
        print("FAIL: interrupting a session")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASS: an interrupted session ends quietly.")
    print("      A signal is recognised whether the process died from it (-15)")
    print("      or handled it and exited cleanly (143) — measured against real")
    print("      subprocesses, which is what the Claude CLI's switch to the")
    print("      second shape silently broke. Ordinary exits are still failures.")
    print("      A killed run finalizes as KILLED with no red card, keeps its")
    print("      error text for /log, and can never be mistaken for a dead")
    print("      account and restarted on the backup subscription. /kill typed")
    print("      as a command behaves exactly like the Kill button.")
    if live:
        print("      The installed CLI was terminated for real and the returncode")
        print("      it produced was recognised as a kill.")
    else:
        print("      (--live also terminates the real CLI and checks the")
        print("       returncode it actually produces — costs one turn.)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
