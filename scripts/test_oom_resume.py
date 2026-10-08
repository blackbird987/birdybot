#!/usr/bin/env python3
"""Regression test: a session the operating system killed for memory is
resumed once, after memory frees up, instead of ending on a red FAILED card.

The incident (2026-10-08). systemd-oomd shot ``claude-session-t-8999.scope``
because the session slice stalled on memory: several build sessions at full
parallelism, their build output counted as page cache against a slice sized
for anon memory only. The CLI died on SIGKILL with no result event. Nothing in
the runner knew that shape, so it surfaced as "Exit code -9", a FAILED card,
and a user retrying by hand while the work sat on disk. Worse, a session killed
before its first turn has no output and no turns, which is the account-failover
heuristic's exact signature, so on a two-account setup it would have been
handed to the backup subscription to be killed the same way.

Asserted here:

  * a SIGKILL (-9 or 137) on a scoped run the bot did not send is classified,
    with the stable phrase ``parser.is_account_agnostic_error`` matches
  * it is resumed on the SAME session, once, and that resume waits on the
    admission hold first (so it does not re-enter the stall that killed it)
  * the resumed prompt opens with the note: which killer, what the session was
    running, lower the parallelism, /tmp is RAM
  * a chain step resumes inside its own build worktree
  * a second kill is not resumed again, and never fails over to the backup
    account
  * a SIGKILL the bot sent itself (kill() escalating) is not an OOM kill,
    and every runner path that SIGKILLs a session tree marks it as the bot's
  * an unscoped run, an ordinary crash and the bot's own memory reap are not
  * an uncorroborated kill is still resumed, worded as unconfirmed
  * a Kill landing during the admission hold stops the resume
  * OOM_KILL_RESUME_RETRIES=0 classifies but never resumes
  * the journal reader distinguishes oomd from the kernel and fails open

Strategy follows scripts/test_context_overflow.py: stub the subprocess
boundary only, so the real recovery cascade in _run_impl executes.

Run: ``python scripts/test_oom_resume.py``  (exit 0 on pass).
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import ast
import asyncio
import copy
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("BOT_PATHS_DISABLED", "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude import cgroups
from bot.claude import runner as runner_mod
from bot.claude.parser import is_account_agnostic_error
from bot.claude.runner import ClaudeRunner
from bot.claude.types import Instance, InstanceStatus, InstanceType, RunResult

PHRASE = "killed by the system's out-of-memory protection"
INSTANCE_ID = "t-oom"
SESSION = "5e1f0c2a-8b1d-4c7e-9a3f-0d2b6e4c8a11"
BUILD_CMD = "dotnet build DegenAI.sln"


class _FakeStdin:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def write(self, data):
        self._sink.append(
            data.decode("utf-8") if isinstance(data, bytes) else str(data)
        )

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None


class _FakeProc:
    _next_pid = 95001

    def __init__(self, sink: list[str]):
        self.pid = _FakeProc._next_pid
        _FakeProc._next_pid += 1
        self.returncode = 0
        self.stdin = _FakeStdin(sink)
        self.stdout = None
        self.stderr = None
        self.signals: list[str] = []

    def terminate(self):
        self.signals.append("TERM")

    def kill(self):
        self.signals.append("KILL")

    async def wait(self):
        return 0


def _killed(*, exit_code: int = -9, work: bool = True) -> RunResult:
    """What _stream_output hands back for a CLI that died on SIGKILL."""
    return RunResult(
        is_error=True,
        error_message=f"Exit code {exit_code}",
        session_id=SESSION,
        num_turns=0,
        tools_used=["Read", "Bash"] if work else [],
        bash_commands=[BUILD_CMD] if work else [],
        last_tool="Bash" if work else "",
        exit_code=exit_code,
    )


def _ok() -> RunResult:
    return RunResult(
        is_error=False, result_text="Build green.", session_id=SESSION,
        num_turns=3, tools_used=["Edit"], exit_code=0,
    )


class _Harness:
    def __init__(
        self, tmp: str, outcomes: list[RunResult], *,
        scoped: bool = True, evidence: str | None = "systemd-oomd",
        accounts: int = 1, on_stream=None, on_hold=None,
        worktree: bool = False,
    ):
        self.accounts = []
        for i in range(accounts):
            p = os.path.join(tmp, f"acct_{i}")
            os.makedirs(p, exist_ok=True)
            self.accounts.append(p)
        self.repo = os.path.join(tmp, "repo")
        os.makedirs(self.repo, exist_ok=True)
        # A chain step: it runs on a build branch in its own worktree, and the
        # resume has to land back in that worktree, not the main checkout.
        self.worktree = None
        if worktree:
            self.worktree = os.path.join(self.repo, ".worktrees", INSTANCE_ID)
            os.makedirs(self.worktree, exist_ok=True)
        self.outcomes = outcomes
        self.scoped = scoped
        self.evidence = evidence
        self.on_stream = on_stream
        self.on_hold = on_hold
        self.events: list[str] = []
        self.spawn_argvs: list[list[str]] = []
        self.spawn_accounts: list[str | None] = []
        self.spawn_cwds: list[str | None] = []
        self.prompts: list[str] = []
        self.progress: list[tuple[str, str]] = []
        self.journal_units: list[str] = []
        self.runner: ClaudeRunner | None = None

    async def run(self):
        saved = {
            "accounts": list(config.CLAUDE_ACCOUNTS),
            "spawn": asyncio.create_subprocess_exec,
            "unusable": runner_mod.unusable_reason,
            "supported": cgroups._scope_supported,
            "ensure": cgroups.ensure_scope_support,
            "evidence": cgroups.oom_kill_evidence,
        }
        config.CLAUDE_ACCOUNTS[:] = list(self.accounts)
        runner_mod.unusable_reason = lambda acct: None  # type: ignore[assignment]
        cgroups._scope_supported = self.scoped

        async def _ensure():
            return self.scoped

        cgroups.ensure_scope_support = _ensure  # type: ignore[assignment]

        async def _evidence(unit):
            self.journal_units.append(unit)
            return self.evidence

        cgroups.oom_kill_evidence = _evidence  # type: ignore[assignment]

        async def fake_spawn(*args, **kwargs):
            self.events.append("spawn")
            self.spawn_argvs.append(list(args))
            env = kwargs.get("env") or {}
            self.spawn_accounts.append(env.get("CLAUDE_CONFIG_DIR"))
            self.spawn_cwds.append(kwargs.get("cwd"))
            return _FakeProc(self.prompts)

        asyncio.create_subprocess_exec = fake_spawn  # type: ignore[assignment]

        runner = ClaudeRunner()
        self.runner = runner
        calls = {"n": 0}

        async def fake_stream_output(proc, instance, on_progress, on_stall, **kw):
            i = calls["n"]
            calls["n"] += 1
            if self.on_stream is not None:
                await self.on_stream(runner, proc, instance, i)
            return copy.deepcopy(self.outcomes[min(i, len(self.outcomes) - 1)])

        runner._stream_output = fake_stream_output  # type: ignore[assignment]

        async def no_adopt(*a, **k):
            return None

        runner._adopt_session_scope = no_adopt  # type: ignore[assignment]

        holds = {"n": 0}

        async def fake_hold(instance, on_progress):
            holds["n"] += 1
            self.events.append("hold")
            if self.on_hold is not None:
                await self.on_hold(runner, instance, holds["n"])

        runner._await_memory_headroom = fake_hold  # type: ignore[assignment]

        async def on_progress(headline, detail=""):
            self.progress.append((headline, detail))

        instance = Instance(
            id=INSTANCE_ID,
            name=None,
            instance_type=InstanceType.TASK,
            prompt="Fix the build.",
            repo_name="AIAgent",
            repo_path=self.repo,
            status=InstanceStatus.RUNNING,
            session_id=SESSION,
            mode="build",
        )
        if self.worktree:
            instance.branch = f"claude-bot/{INSTANCE_ID}"
            instance.worktree_path = self.worktree
        runner._active_tasks.add(instance.id)
        try:
            result = await runner.run(instance, on_progress=on_progress)
        finally:
            runner._active_tasks.discard(instance.id)
            asyncio.create_subprocess_exec = saved["spawn"]  # type: ignore[assignment]
            runner_mod.unusable_reason = saved["unusable"]  # type: ignore[assignment]
            cgroups._scope_supported = saved["supported"]
            cgroups.ensure_scope_support = saved["ensure"]  # type: ignore[assignment]
            cgroups.oom_kill_evidence = saved["evidence"]  # type: ignore[assignment]
            config.CLAUDE_ACCOUNTS[:] = saved["accounts"]
        return result, instance

    def resume_ids(self) -> list[str | None]:
        out: list[str | None] = []
        for argv in self.spawn_argvs:
            if "--resume" in argv:
                out.append(argv[argv.index("--resume") + 1])
            else:
                out.append(None)
        return out

    def wrapped(self) -> list[bool]:
        return [bool(a) and a[0] == "systemd-run" for a in self.spawn_argvs]


async def _case_resumes_once(tmp: str, failures: list[str]) -> None:
    h = _Harness(tmp, [_killed(), _ok()])
    result, instance = await h.run()
    if len(h.spawn_argvs) != 2:
        failures.append(f"resume: expected 2 spawns, got {len(h.spawn_argvs)}")
        return
    if not all(h.wrapped()):
        failures.append("resume: the harness did not exercise a scoped spawn")
    if h.resume_ids()[1] != SESSION:
        failures.append(
            f"resume: the second spawn resumed {h.resume_ids()[1]!r}, not the "
            "killed session"
        )
    # spawn, hold, spawn: run()'s own hold comes first, then the resume's.
    if h.events != ["hold", "spawn", "hold", "spawn"]:
        failures.append(
            f"resume: the resume did not wait on the admission hold before "
            f"re-spawning (events {h.events})"
        )
    prompt = h.prompts[1] if len(h.prompts) > 1 else ""
    for needle in (
        "killed for memory", "systemd-oomd", f"`{BUILD_CMD}`",
        "dotnet build -m:2", "/tmp", "git status",
    ):
        if needle not in prompt:
            failures.append(f"resume: the note is missing {needle!r}")
    if not prompt.rstrip().endswith("Fix the build."):
        failures.append("resume: the original prompt does not follow the note")
    if h.prompts and "killed for memory" in h.prompts[0]:
        failures.append("resume: the FIRST attempt already carried the note")
    if result.is_error:
        failures.append(f"resume: the resumed run did not succeed: {result.error_message}")
    if "Bash" not in result.tools_used:
        failures.append(
            "resume: the killed attempt's record of work was dropped from the "
            "final result"
        )
    if instance._memory_kill_note:
        failures.append("resume: the one-shot note leaked onto the instance")
    if not any("OOM-killed" in hd for hd, _ in h.progress):
        failures.append(
            f"resume: the card never said OOM-killed and resuming: {h.progress}"
        )
    if not h.journal_units or not h.journal_units[0].startswith("claude-session-"):
        failures.append(
            f"resume: the journal was asked about {h.journal_units}, not the "
            "session scope"
        )


async def _case_chain_worktree(tmp: str, failures: list[str]) -> None:
    h = _Harness(tmp, [_killed(), _ok()], worktree=True)
    result, instance = await h.run()
    if len(h.spawn_argvs) != 2:
        failures.append(
            f"worktree: expected the chain step to resume (spawns "
            f"{len(h.spawn_argvs)})"
        )
    elif h.spawn_cwds != [h.worktree, h.worktree]:
        failures.append(
            f"worktree: the resume ran in {h.spawn_cwds[1]!r}, not the build's "
            f"worktree {h.worktree!r}"
        )
    if instance.worktree_path != h.worktree or result.is_error:
        failures.append("worktree: the step lost its worktree or did not finish")


async def _case_second_kill(tmp: str, failures: list[str]) -> None:
    h = _Harness(tmp, [_killed(), _killed(work=False)], accounts=2)
    result, _ = await h.run()
    if len(h.spawn_argvs) != 2:
        failures.append(
            f"second kill: expected exactly 2 spawns (one resume, no "
            f"failover), got {len(h.spawn_argvs)}"
        )
    if len(set(h.spawn_accounts)) > 1:
        failures.append(
            f"second kill: the work moved accounts {h.spawn_accounts}; memory "
            "is the machine's, not the subscription's"
        )
    err = result.error_message or ""
    if not result.is_error or PHRASE not in err.lower():
        failures.append(f"second kill: final error lacks the phrase: {err!r}")
    if not is_account_agnostic_error(err):
        failures.append("second kill: the final error is not account-agnostic")


async def _case_bot_signalled(tmp: str, failures: list[str]) -> None:
    async def kill_it(runner, proc, instance, i):
        if i == 0:
            runner._processes[instance.id] = proc
            await runner.kill(instance.id)  # not intentional: shutdown's shape

    h = _Harness(tmp, [_killed(), _ok()], on_stream=kill_it)
    result, _ = await h.run()
    if len(h.spawn_argvs) != 1:
        failures.append(
            f"bot-sent SIGKILL: was resumed as an OOM kill "
            f"({len(h.spawn_argvs)} spawns)"
        )
    if PHRASE in (result.error_message or "").lower():
        failures.append("bot-sent SIGKILL: was worded as an OOM kill")
    if h.journal_units:
        failures.append("bot-sent SIGKILL: the journal was consulted at all")
    if INSTANCE_ID in h.runner._bot_signalled:
        failures.append("bot-sent SIGKILL: the mark outlived its run")


async def _case_not_oom(tmp: str, failures: list[str]) -> None:
    # Unscoped: oomd could not have picked this session out from the bot.
    h = _Harness(tmp, [_killed(), _ok()], scoped=False)
    result, _ = await h.run()
    if len(h.spawn_argvs) != 1 or PHRASE in (result.error_message or "").lower():
        failures.append("unscoped -9: classified as an OOM kill")
    if any(h.wrapped()):
        failures.append("unscoped -9: the spawn was wrapped anyway")

    # An ordinary crash is not a kill.
    h = _Harness(tmp, [_killed(exit_code=1), _ok()])
    result, _ = await h.run()
    if len(h.spawn_argvs) != 1 or PHRASE in (result.error_message or "").lower():
        failures.append("exit 1: classified as an OOM kill")

    # The bot's own reap carries its own note and its own branch.
    reaped = _killed()
    reaped.memory_kill_note = "--- the bot reaped you ---"
    reaped.exit_code = None
    h = _Harness(tmp, [reaped, _ok()])
    result, _ = await h.run()
    if h.journal_units:
        failures.append("memory reap: misread as an outside OOM kill")
    if len(h.prompts) > 1 and "killed by the operating system" in h.prompts[1]:
        failures.append("memory reap: resumed with the OOM note instead of its own")

    # 137 is the same signal through a shell.
    h = _Harness(tmp, [_killed(exit_code=137), _ok()])
    result, _ = await h.run()
    if len(h.spawn_argvs) != 2:
        failures.append("exit 137: not recognised as a SIGKILL")


async def _case_uncorroborated(tmp: str, failures: list[str]) -> None:
    h = _Harness(tmp, [_killed(), _killed(work=False)], evidence=None)
    result, _ = await h.run()
    if len(h.spawn_argvs) != 2:
        failures.append(
            "uncorroborated: an outside -9 on a scope was not resumed; the "
            "journal is corroboration, not a gate"
        )
    err = (result.error_message or "").lower()
    if PHRASE not in err or "no journal entry confirmed" not in err:
        failures.append(f"uncorroborated: wording does not admit it: {err!r}")


async def _case_kill_during_hold(tmp: str, failures: list[str]) -> None:
    async def kill_in_hold(runner, instance, n):
        if n == 2:
            await runner.kill(instance.id, reason="kill")

    h = _Harness(tmp, [_killed(), _ok()], on_hold=kill_in_hold)
    await h.run()
    if len(h.spawn_argvs) != 1:
        failures.append(
            "kill during hold: the resume spawned anyway after the user "
            "stopped it"
        )


async def _case_disabled(tmp: str, failures: list[str]) -> None:
    saved = config.OOM_KILL_RESUME_RETRIES
    config.OOM_KILL_RESUME_RETRIES = 0
    try:
        h = _Harness(tmp, [_killed(), _ok()], accounts=2)
        result, _ = await h.run()
    finally:
        config.OOM_KILL_RESUME_RETRIES = saved
    if len(h.spawn_argvs) != 1:
        failures.append(
            f"retries=0: still resumed or failed over ({len(h.spawn_argvs)} spawns)"
        )
    if PHRASE not in (result.error_message or "").lower():
        failures.append("retries=0: the failure is no longer named for what it was")


def _own_calls(fn: ast.AST):
    """Calls made in ``fn`` itself, not inside a function nested in it."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _case_bot_sigkills_are_marked(failures: list[str]) -> None:
    """Every runner path that SIGKILLs a session tree records it as the bot's.

    The memory reap kills through ``cgroup.kill`` and ``kill_tree``, both
    SIGKILL, and when it raced a completed turn it stands down and falls
    through to the normal exit path with a real -9. Unmarked, that -9 is
    classified as the operating system's OOM killer and resumed, redoing a
    turn that had finished. Asserted on the source because the harness stubs
    _stream_output, where the reap lives.
    """
    src = Path(__file__).resolve().parents[1] / "bot" / "claude" / "runner.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    found = 0
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        calls = list(_own_calls(fn))
        names = {
            ast.unparse(c.func) for c in calls
        } | {
            ast.unparse(a) for c in calls for a in c.args
        }
        if not ({"session_cg.kill", "memory.kill_tree"} & names):
            continue
        found += 1
        if "self._bot_signalled.add" not in names:
            failures.append(
                f"runner.{fn.name} SIGKILLs a session tree without marking "
                "it in _bot_signalled, so its -9 can be resumed as an OOM kill"
            )
    if not found:
        failures.append(
            "no runner path kills through session_cg.kill or memory.kill_tree; "
            "the reap moved and this check no longer guards it"
        )


async def _case_journal_reader(failures: list[str]) -> None:
    if sys.platform != "linux":
        return
    saved_run = subprocess.run
    saved_which = cgroups.shutil.which
    cgroups.shutil.which = lambda name: "/usr/bin/" + name  # type: ignore[assignment]
    outputs = {
        "oomd": (
            b"claude-session-t-8999.scope: systemd-oomd killed 4 process(es) "
            b"in this unit.\nclaude-session-t-8999.scope: Failed with result "
            b"'oom-kill'.\n"
        ),
        "kernel": (
            b"A process of this unit has been killed by the OOM killer.\n"
            b"claude-session-x.scope: Failed with result 'oom-kill'.\n"
        ),
        "none": b"Started claude-session-x.scope.\n",
    }
    try:
        for key, want in (
            ("oomd", "systemd-oomd"),
            ("kernel", "the kernel's OOM killer"),
            ("none", None),
        ):
            def fake_run(cmd, _out=outputs[key], **kw):
                return SimpleNamespace(stdout=_out, returncode=0)

            subprocess.run = fake_run  # type: ignore[assignment]
            got = await cgroups.oom_kill_evidence("claude-session-x.scope")
            if got != want:
                failures.append(f"journal {key}: got {got!r}, want {want!r}")

        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 5)

        subprocess.run = boom  # type: ignore[assignment]
        if await cgroups.oom_kill_evidence("claude-session-x.scope") is not None:
            failures.append("journal timeout: did not fail open")
    finally:
        subprocess.run = saved_run  # type: ignore[assignment]
        cgroups.shutil.which = saved_which  # type: ignore[assignment]


async def _main() -> int:
    failures: list[str] = []
    if not is_account_agnostic_error(
        "Killed by the system's out-of-memory protection (systemd-oomd) while "
        "running the Bash tool"
    ):
        failures.append("the phrase is not in is_account_agnostic_error")
    with tempfile.TemporaryDirectory() as tmp:
        for case in (
            _case_resumes_once, _case_chain_worktree, _case_second_kill,
            _case_bot_signalled,
            _case_not_oom, _case_uncorroborated, _case_kill_during_hold,
            _case_disabled,
        ):
            try:
                await case(tmp, failures)
            except Exception as exc:  # a crash is a failure, not an abort
                import traceback
                traceback.print_exc()
                failures.append(f"{case.__name__} raised {exc!r}")
    await _case_journal_reader(failures)
    _case_bot_sigkills_are_marked(failures)

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: OOM-killed sessions resume once, after the hold, and only them")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
