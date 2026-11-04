"""A fresh session checks finished work against what the owner asked.

The author of a piece of work is the worst placed to notice it answered the wrong
question, so this session never sees the author's reasoning or summary: only the
owner's own words, the repo goal, the diff and the verify output, all gathered by
the bot. See docs/design/judge.md, including what the replay did and did not show.

It fails closed: an unparseable answer, a crashed run or a gap in the input is
`could_not_check`, which holds a merge. Nothing here runs for a repo that is not in
`config.JUDGE_REPOS`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import config
from bot.claude.sandbox import GIT_SAFE
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType
from bot.engine import lifecycle, step_parse
from bot.engine.commit_calls import LOG_FORMAT, match_commits, parse_log, read_commit_calls
from bot.engine.sessions import find_session_file
from bot.engine.step_policy import resolve_spawn_model
from bot.engine.step_spawn import SpawnConfig, spawn_from
from bot.engine.steps import on_verify
from bot.engine.transcript import _iter_jsonl
from bot.fsutil import read_contained_text
from bot.platform.base import ButtonSpec, RequestContext
from bot.platform.redact import redact_secrets
from bot.procutil import run_capture

log = logging.getLogger(__name__)

PASS = "pass"
FIX = "fix"
ONLY_YOU = "only_you"
COULD_NOT_CHECK = "could_not_check"
VERDICTS = (PASS, FIX, ONLY_YOU, COULD_NOT_CHECK)

MAX_REPAIR_ROUNDS = 2

_MAX_OWNER_CHARS = 30_000
_MAX_GOAL_CHARS = 4_000
_MAX_STATE_CHARS = 4_000
_MAX_DIFF_CHARS = 80_000
_GIT_TIMEOUT_SEC = 30

_LOG_NAME = "judge.jsonl"


def enabled_for(repo_name: str | None) -> bool:
    return bool(repo_name) and repo_name in config.JUDGE_REPOS


# --- The verdict ---

@dataclass(frozen=True)
class Verdict:
    kind: str
    problems: tuple[str, ...] = ()
    line: str = ""


_VERDICT_BLOCK_RE = re.compile(r"```verdict[ \t]*\n(.*?)```", re.DOTALL)
_VERDICT_LINE_RE = re.compile(r"^VERDICT:[ \t]*(\S*)[ \t]*$", re.MULTILINE)
_LINE_RE = re.compile(r"^LINE:[ \t]*(.*)$", re.MULTILINE)
_PROBLEM_RE = re.compile(r"^[ \t]*-[ \t]+(.+)$", re.MULTILINE)


def _could_not_check(why: str) -> Verdict:
    return Verdict(COULD_NOT_CHECK, line=why)


def parse_verdict(text: str | None) -> Verdict:
    """Read the last verdict block. Anything short of a clean one is could_not_check."""
    blocks = _VERDICT_BLOCK_RE.findall(text or "")
    if not blocks:
        return _could_not_check("the judge gave no verdict")
    block = blocks[-1]
    kinds = _VERDICT_LINE_RE.findall(block)
    if len(kinds) != 1 or kinds[0] not in VERDICTS:
        return _could_not_check("the judge's verdict could not be read")
    kind = kinds[0]
    line_m = _LINE_RE.search(block)
    line = line_m.group(1).strip() if line_m else ""
    problems = tuple(p.strip() for p in _PROBLEM_RE.findall(block))
    if kind == PASS:
        if problems:
            return _could_not_check("the judge said pass and also listed problems")
        return Verdict(PASS)
    if kind == FIX:
        if not problems:
            return _could_not_check("the judge said fix and listed no problems")
        return Verdict(FIX, problems=problems)
    if not line:
        return _could_not_check("the judge's verdict gave no reason")
    return Verdict(kind, line=line)


def verdict_of_run(inst: Instance | None) -> Verdict:
    """The verdict of a finished judge run. A run that did not finish never passes."""
    if inst is None:
        return _could_not_check("the judge could not start")
    if inst.status != InstanceStatus.COMPLETED:
        return _could_not_check(f"the judge run ended as {inst.status.value}")
    return parse_verdict(inst.read_result_text())


# --- The input ---

def _cap_owner_messages(messages: list[str]) -> tuple[list[str], int]:
    """Fit the budget by dropping the middle: the first asks and the latest word matter most."""
    if sum(len(m) for m in messages) <= _MAX_OWNER_CHARS:
        return list(messages), 0
    head: list[str] = []
    used = 0
    for m in messages[:5]:
        if used + len(m) > _MAX_OWNER_CHARS // 2:
            break
        head.append(m)
        used += len(m)
    tail: list[str] = []
    for m in reversed(messages[len(head):]):
        if used + len(m) > _MAX_OWNER_CHARS:
            break
        tail.insert(0, m)
        used += len(m)
    return head + tail, len(messages) - len(head) - len(tail)


def build_input(
    owner_messages: list[str],
    goal: str,
    diff: str,
    verify_output: str,
    notes: tuple[str, ...] = (),
) -> str:
    """The whole of what the judge is told. A pure function, so the replay runs the same one.

    There is no parameter for an author's summary, plan or reasoning, on purpose.
    Whatever is cut for size is stated, so a gap reads as a gap.
    """
    cuts = list(notes)
    kept, dropped = _cap_owner_messages(owner_messages)
    if dropped:
        cuts.append(f"{dropped} of {len(owner_messages)} owner messages were left out for size, from the middle.")
    shown_diff = diff
    if len(diff) > _MAX_DIFF_CHARS:
        shown_diff = diff[:_MAX_DIFF_CHARS]
        cuts.append(
            f"The diff was cut to {_MAX_DIFF_CHARS} of {len(diff)} characters. Read the files for the rest.",
        )
    if not verify_output.strip():
        cuts.append("There is no verify output for this work.")
    owner_text = "\n\n".join(f"[{i}] {m}" for i, m in enumerate(kept, 1))
    parts = [
        "=== THE OWNER'S MESSAGES, VERBATIM, OLDEST FIRST ===",
        owner_text,
        "=== THE REPO'S GOAL ===",
        goal.strip() or "(none found)",
        "=== THE DIFF ===",
        shown_diff.strip(),
        "=== VERIFY OUTPUT, WRITTEN BY THE SESSION THAT DID THE WORK ===",
        verify_output.strip() or "(none)",
    ]
    if cuts:
        parts += ["=== LEFT OUT OF THIS INPUT ===", "\n".join(f"- {c}" for c in cuts)]
    parts += ["Give your verdict."]
    return "\n\n".join(parts)


def repo_goal(repo_path: str | None) -> str:
    """The first part of the repo's instructions plus its study state.

    Read from the main checkout, contained, because a session can plant a link in
    its own tree and can rewrite its own worktree's copy.
    """
    if not repo_path:
        return ""
    parts: list[str] = []
    for name in ("CLAUDE.md", ".claude/CLAUDE.md"):
        text = read_contained_text(repo_path, name)
        if text:
            parts.append(text[:_MAX_GOAL_CHARS])
            break
    state = read_contained_text(repo_path, "study/STATE.md")
    if state:
        parts.append("Study state:\n" + state[:_MAX_STATE_CHARS])
    return "\n\n".join(parts)


def verify_output_of(inst: Instance | None) -> str:
    """Only the structured verify block. The prose around it is the author talking."""
    if inst is None:
        return ""
    blocks = step_parse._VERIFY_BLOCK_RE.findall(inst.read_result_text() or "")
    return blocks[-1].strip() if blocks else ""


async def _git(cwd: str, *args: str) -> str | None:
    """A read in a checkout a session can write to: no hooks, no external diff tools."""
    try:
        result = await asyncio.to_thread(
            run_capture, ["git", *GIT_SAFE, *args], cwd=cwd, timeout=_GIT_TIMEOUT_SEC,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


async def _branch_diff(inst: Instance) -> tuple[str | None, list[str], tuple[str, ...]]:
    """The work on a chain's branch: tracked changes against where it forked, committed or not.

    Returns (diff, commits, notes). A chain commits its last changes after verify,
    so the working tree is what counts, and untracked files are named because a
    diff cannot show them.
    """
    cwd = inst.worktree_path or inst.repo_path
    if not cwd:
        return None, [], ()
    target = inst.original_branch or "master"
    base = await _git(cwd, "merge-base", "HEAD", target)
    if not base:
        return None, [], ()
    base = base.strip()
    stat = await _git(cwd, "diff", "--no-ext-diff", "--no-textconv", "--stat", base)
    body = await _git(cwd, "diff", "--no-ext-diff", "--no-textconv", base)
    if stat is None or body is None:
        return None, [], ()
    listed = await _git(cwd, "rev-list", f"{base}..HEAD") or ""
    untracked = await _git(cwd, "ls-files", "--others", "--exclude-standard") or ""
    notes = ()
    if untracked.strip():
        notes = (f"Files not yet committed and not shown in the diff: {', '.join(untracked.split())}",)
    return f"{stat}\n{body}" if body.strip() else "", listed.split(), notes


# --- The record ---

def _log_path() -> Path:
    return Path(config.DATA_DIR) / _LOG_NAME


def _record(row: dict) -> None:
    """Append to data/judge.jsonl. Losing a line costs a figure and never a run."""
    row = {"ts": datetime.now(timezone.utc).isoformat(), **row}
    try:
        with _log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("Could not append to %s", _log_path(), exc_info=True)


def _load_records(thread: str) -> list[dict]:
    rows: list[dict] = []
    try:
        for rec in _iter_jsonl(_log_path()):
            if rec.get("thread") == thread:
                rows.append(rec)
    except OSError:
        pass
    return rows


def _record_verdict(
    ctx: RequestContext, verdict: Verdict, *,
    run: Instance | None, session_id: str | None, repo: str | None,
    commits: list[str], attempts: list[str] | None = None, **extra,
) -> None:
    _record({
        "repo": repo,
        "thread": str(ctx.channel_id),
        "session": session_id,
        "run": run.id if run else None,
        "verdict": verdict.kind,
        "problems": [redact_secrets(p) for p in verdict.problems],
        "line": redact_secrets(verdict.line),
        "commits": commits,
        "unattributed": attempts or [],
        "cost_usd": run.cost_usd if run else None,
        **extra,
    })


# --- The chain ---

def _hold_text(verdict: Verdict, *, more_steps: bool) -> str:
    if verdict.kind == ONLY_YOU:
        head = "🔎 Held before merge. Only you can judge this:"
        detail = verdict.line
    elif verdict.kind == FIX:
        head = f"🔎 Held before merge. Still has problems after {MAX_REPAIR_ROUNDS} repair rounds:"
        detail = "\n".join(f"- {p}" for p in verdict.problems)
    else:
        head = "🔎 Held before merge. The check could not be done:"
        detail = verdict.line
    tail = "\nContinue carries on with the chain." if more_steps else ""
    return f"{head}\n{detail}{tail}"


async def _hold(
    ctx: RequestContext, inst: Instance, verdict: Verdict, *, more_steps: bool,
) -> None:
    """Stop the chain on this step, the same way a blocking verify does.

    Continue resumes after the step the chain paused on, so the owner's tap is the
    decision, and it does not send the work through the judge again.
    """
    inst.needs_input = True
    inst.needs_manual_verification = True
    inst.manual_verify_reason = verdict.line or "; ".join(verdict.problems)
    ctx.store.update_instance(inst)
    buttons = [[ButtonSpec("Continue", f"continue_anyway:{inst.id}")]] if more_steps else None
    await ctx.messenger.send_text(
        ctx.channel_id, _hold_text(verdict, more_steps=more_steps), buttons=buttons,
    )


async def _judge_branch(
    ctx: RequestContext, inst: Instance,
) -> tuple[Verdict, Instance | None, list[str]]:
    """Run one judgement of a chain's work. Never raises: failing here must hold."""
    commits: list[str] = []
    try:
        owner = await ctx.messenger.owner_messages(ctx.channel_id)
        if not owner:
            return _could_not_check("the owner's messages in this thread could not be read"), None, commits
        diff, commits, notes = await _branch_diff(inst)
        if not diff:
            return _could_not_check("the diff of this work could not be read"), None, commits
        prompt = build_input(owner, repo_goal(inst.repo_path), diff, verify_output_of(inst), notes)
        run = await spawn_from(ctx, inst.id, SpawnConfig(
            instance_type=InstanceType.QUERY,
            prompt=prompt,
            mode="explore",
            origin=InstanceOrigin.JUDGE,
            status_text="Judging...",
            copy_branch=True,
            silent=True,
            permission_mode="explore",
        ), strip_source_buttons=False)
        return verdict_of_run(run), run, commits
    except Exception:
        log.exception("Judge failed on %s", inst.id)
        return _could_not_check("the judge hit an internal error"), None, commits


async def gate_after_verify(
    ctx: RequestContext, verified: Instance | None, *, more_steps: bool,
) -> Instance | None:
    """Judge a chain's verified work. The caller only calls this for a listed repo.

    pass returns silently. fix goes back to the author to repair and re-verify, at
    most MAX_REPAIR_ROUNDS times. Everything else holds, even under preset=ship.
    Returns what the chain's own guard should look at next.
    """
    if not verified or verified.status != InstanceStatus.COMPLETED or verified.needs_input:
        return verified
    current = verified
    for round_no in range(MAX_REPAIR_ROUNDS + 1):
        verdict, run, commits = await _judge_branch(ctx, current)
        _record_verdict(
            ctx, verdict, run=run, session_id=current.session_id,
            repo=current.repo_name, commits=commits, path="chain", round=round_no,
        )
        if verdict.kind == PASS:
            return current
        if verdict.kind != FIX or round_no >= MAX_REPAIR_ROUNDS:
            await _hold(ctx, current, verdict, more_steps=more_steps)
            return current
        repaired = await spawn_from(ctx, current.id, SpawnConfig(
            instance_type=InstanceType.TASK,
            prompt=repair_prompt(verdict),
            mode="build",
            origin=InstanceOrigin.BUILD,
            status_text=f"Repairing (round {round_no + 1})...",
            resume_session=True,
            copy_branch=True,
            silent=True,
        ), strip_source_buttons=False)
        if not repaired or repaired.status != InstanceStatus.COMPLETED or repaired.needs_input:
            return repaired
        reverified = await on_verify(ctx, repaired.id)
        if not reverified or reverified.status != InstanceStatus.COMPLETED or reverified.needs_input:
            return reverified
        current = reverified
    return current


def repair_prompt(verdict: Verdict) -> str:
    return config.JUDGE_REPAIR_PROMPT + "\n".join(f"- {p}" for p in verdict.problems)


# --- Chat ---

async def attributed_commits(repo_path: str, session_file: Path) -> tuple[list[str], list[str]]:
    """This session's commits as full hashes, plus the calls that could not be pinned down."""
    calls = await asyncio.to_thread(read_commit_calls, session_file)
    if not calls:
        return [], []
    args = ["log", "--all", LOG_FORMAT]
    times = [c.ts for c in calls if c.ts is not None]
    if len(times) == len(calls):
        slack = timedelta(hours=3)
        for flag, edge in (("--since", min(times)), ("--until", max(times))):
            when = datetime.fromtimestamp(edge, timezone.utc)
            when = when - slack if flag == "--since" else when + slack
            args.append(f"{flag}={when.isoformat()}")
    log_text = await _git(repo_path, *args)
    if log_text is None:
        return [], [c.tool_id for c in calls]
    return match_commits(calls, parse_log(log_text))


async def _commit_diffs(repo_path: str, shas: list[str]) -> str | None:
    """Each commit's change and no commit message, which is the author talking."""
    out: list[str] = []
    for sha in shas:
        shown = await _git(
            repo_path, "show", "--format=", "--stat", "--patch",
            "--no-ext-diff", "--no-textconv", sha,
        )
        if shown is None:
            return None
        out.append(f"--- commit {sha[:12]} ---\n{shown}")
    return "\n".join(out)


_idle_running: set[str] = set()


async def judge_idle_thread(
    ctx: RequestContext,
    *,
    session_id: str,
    repo_name: str,
    repo_path: str,
    still_idle: Callable[[], bool],
    start_repair: Callable[[str], Awaitable[bool]],
) -> Verdict | None:
    """Judge what a thread's own session committed, once, when the thread goes idle.

    Returns None when there was nothing new to judge. The work is already committed,
    so this reports and never holds anything.
    """
    channel = str(ctx.channel_id)
    if channel in _idle_running or ctx.store.get_autopilot_chain(session_id):
        return None
    _idle_running.add(channel)
    try:
        return await _judge_idle_thread(
            ctx, channel, session_id, repo_name, repo_path, still_idle, start_repair,
        )
    finally:
        _idle_running.discard(channel)


async def _judge_idle_thread(
    ctx: RequestContext,
    channel: str,
    session_id: str,
    repo_name: str,
    repo_path: str,
    still_idle: Callable[[], bool],
    start_repair: Callable[[str], Awaitable[bool]],
) -> Verdict | None:
    # A merged chain closes its thread, and its last commits came after its judgement.
    if await ctx.messenger.is_conversation_closed(channel):
        return None
    session_file = await asyncio.to_thread(find_session_file, session_id)
    if not session_file:
        return None
    resolved, unattributed = await attributed_commits(repo_path, session_file)
    if not resolved and not unattributed:
        return None

    records = await asyncio.to_thread(_load_records, channel)
    seen_commits = {c for r in records for c in r.get("commits") or []}
    seen_calls = {c for r in records for c in r.get("unattributed") or []}
    new_commits = [c for c in resolved if c not in seen_commits]
    new_calls = [c for c in unattributed if c not in seen_calls]
    if not new_commits and not new_calls:
        return None

    common = {"run": None, "session_id": session_id, "repo": repo_name, "path": "chat"}

    # The repair turn's own commits close that one round rather than opening another.
    if records and records[-1].get("repair_started"):
        _record_verdict(
            ctx, Verdict("repair"), commits=new_commits, attempts=new_calls, **common,
        )
        return None

    if new_calls:
        verdict = _could_not_check(
            "this thread's commits could not all be identified from its session",
        )
        _record_verdict(ctx, verdict, commits=new_commits, attempts=new_calls, **common)
        await _report_idle(ctx, verdict)
        return verdict

    owner = await ctx.messenger.owner_messages(ctx.channel_id)
    diff = await _commit_diffs(repo_path, new_commits)
    if not owner or not diff:
        verdict = _could_not_check("the owner's messages or the commits' diff could not be read")
        _record_verdict(ctx, verdict, commits=new_commits, **common)
        await _report_idle(ctx, verdict)
        return verdict

    run = await _run_idle_judge(ctx, repo_name, repo_path, build_input(
        owner, repo_goal(repo_path), diff, "",
        ("The checkout you can read may have moved on since these commits.",),
    ))
    verdict = verdict_of_run(run)
    common["run"] = run
    if verdict.kind == FIX and still_idle():
        started = await start_repair(repair_prompt(verdict))
        _record_verdict(
            ctx, verdict, commits=new_commits, repair_started=started, **common,
        )
        problems = "\n".join(f"- {p}" for p in verdict.problems)
        lead = "A repair turn is running now." if started else "No repair turn could start."
        await ctx.messenger.send_text(
            ctx.channel_id, f"🔎 A check of what was committed here found problems. {lead}\n{problems}",
        )
        return verdict
    _record_verdict(ctx, verdict, commits=new_commits, **common)
    await _report_idle(ctx, verdict)
    return verdict


async def _run_idle_judge(
    ctx: RequestContext, repo_name: str, repo_path: str, prompt: str,
) -> Instance | None:
    """A fresh read-only run outside the thread's session. It never binds to the thread."""
    try:
        inst = ctx.store.create_instance(
            instance_type=InstanceType.QUERY, prompt=prompt, mode="explore",
        )
        inst.origin = InstanceOrigin.JUDGE
        inst.origin_platform = ctx.platform
        inst.repo_name = repo_name
        inst.repo_path = repo_path
        inst.bash_policy = "none"
        inst.bash_policy_baseline = "none"
        routed = resolve_spawn_model(InstanceOrigin.JUDGE)
        if routed:
            inst.model = routed
        ctx.store.update_instance(inst)
        await lifecycle.run_instance(ctx, inst, silent=True)
        return inst
    except Exception:
        log.exception("Idle judge failed on thread %s", ctx.channel_id)
        return None


async def _report_idle(ctx: RequestContext, verdict: Verdict) -> None:
    """pass says nothing. The rest get their one line, and nothing is held."""
    if verdict.kind == PASS:
        return
    if verdict.kind == ONLY_YOU:
        text = f"🔎 Only you can judge this, on what was committed here:\n{verdict.line}"
    elif verdict.kind == FIX:
        problems = "\n".join(f"- {p}" for p in verdict.problems)
        text = f"🔎 A check of what was committed here found problems:\n{problems}"
    else:
        text = f"🔎 What was committed here could not be checked:\n{verdict.line}"
    try:
        await ctx.messenger.send_text(ctx.channel_id, text)
    except Exception:
        log.warning("Could not post the judge's line in %s", ctx.channel_id, exc_info=True)
