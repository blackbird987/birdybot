"""One fresh look at a chain's plan, against the repo, before it is built.

The first step of every /chain and of Build & Ship. A session that never saw the
conversation reads the plan and the code, and raises only what it can show with a
file and a line. If it raises something Critical or High, the session that wrote the
plan decides on each item, because only that session knows what was meant. An
accepted revision goes into the chain's plan override, the one thing the build reads
first. See docs/design/plan-check.md.

It fails open. A check that cannot run leaves the plan as it was, says so in one quiet
line, and the build goes ahead. Two things stop the chain: a Kill, which is the owner
saying stop, and a reboot drain, which resumes here afterwards.

Whether it is worth its cost is an open question with a kill bar written down before
the first run: study/findings, and `data/plan_checks.jsonl` is what it reads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bot import config
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType
from bot.engine import judge, prior_art
from bot.engine.chain_git import _resolve_chain_repo_path
from bot.engine.step_parse import (
    _BUILD_PLAN_INJECT_MAX,
    _HIGH_PRIO_RE,
    _REVIEW_STATUS_RE,
    _deferred_lines,
    _extract_latest_plan_text,
)
from bot.engine.step_spawn import SpawnConfig, spawn_from
from bot.platform.base import RequestContext
from bot.platform.redact import redact_secrets
from bot.textutil import find_tilde_block

log = logging.getLogger(__name__)

CLEAN = "clean"
REVISED = "revised"
KEPT = "kept"
COULD_NOT_CHECK = "could_not_check"
SKIPPED = "skipped"

# Plans reviewed already, on a card posted before the old buttons went.
_ALREADY_REVIEWED = (InstanceOrigin.REVIEW_PLAN, InstanceOrigin.APPLY_REVISIONS)

_NEEDS_REVISION_RE = re.compile(r"^[ \t]*NEEDS_REVISION:[ \t]*(yes|no)\b", re.IGNORECASE | re.MULTILINE)
_DECISION_RE = re.compile(
    r"^[ \t]*[-*][ \t]+(.+?):[ \t]*(applied|rejected)\b[ \t,:]*(.*)$", re.IGNORECASE | re.MULTILINE,
)
_APPLY_SLOT_RE = re.compile(r"\{(plan|review|max_chars)\}")
_MAX_REVIEW_CHARS = 8_000
_MAX_NOTE_CHARS = 300


@dataclass
class CheckOutcome:
    """What the chain needs to know. `author` is what the build resumes, if it moved."""
    instances: list[Instance] = field(default_factory=list)
    author: Instance | None = None
    deferred: list[str] = field(default_factory=list)
    halted: bool = False
    halt_run: Instance | None = None


# --- The input ---

def build_input(owner_messages: list[str] | None, goal: str, plan: str, history: str) -> str:
    """The whole of what the reviewer is told. Whatever is missing is stated."""
    kept, dropped = judge._cap_owner_messages(owner_messages or [])
    if not kept:
        owner_text = (
            "(None could be read. A thread started by another session has none. "
            "Skip the check for something the owner asked for.)"
        )
    else:
        owner_text = "\n\n".join(f"[{i}] {m}" for i, m in enumerate(kept, 1))
        if dropped:
            owner_text += f"\n\n({dropped} of {len(kept) + dropped} left out for size, from the middle.)"
    return "\n\n".join([
        "=== THE OWNER'S MESSAGES, VERBATIM, OLDEST FIRST ===",
        owner_text,
        "=== THE REPO'S GOAL ===",
        goal.strip() or "(none found)",
        "=== THE PLAN ===",
        plan.strip(),
        "=== WHAT WAS ALREADY TRIED IN THE CODE IT TOUCHES ===",
        history.strip() or "(nothing found)",
        config.PLAN_CHECK_PROMPT,
    ])


def _without_instruction(tail: str) -> str:
    """The history block alone. Its instruction says to ask the user, and here nobody can answer."""
    return tail.rstrip().removesuffix(prior_art.CHAIN_INSTRUCTION).rstrip()


def _attached_history(ctx: RequestContext, session_id: str) -> str:
    """Read from the stored override, since the build's copy is capped and can lose the tail."""
    override = ctx.store.get_chain_plan_override(session_id)
    return _without_instruction(prior_art.split_attached(override)[1]) if override else ""


# --- The answers ---

def _read_review(run: Instance) -> tuple[bool, str, list[str]] | None:
    """``(needs_revision, text, deferred)``, or None when the answer cannot be trusted either way.

    `_needs_revision` reads a missing block as no revision. Here that would pass a plan
    nobody managed to check. The last block is the answer: the prompt ends on it, and an
    earlier one is the reviewer quoting a plan about this very format.
    """
    text = run.read_result_text() or ""
    blocks = _REVIEW_STATUS_RE.findall(text)
    if not blocks:
        return None
    m = _NEEDS_REVISION_RE.search(blocks[-1])
    if not m:
        return None
    return m.group(1).lower() == "yes", text, _deferred_lines(blocks[-1])


def _read_revised_plan(text: str) -> tuple[str, list[str], list[str]]:
    """``(plan, applied, rejected)`` from the author's answer. The plan is "" if absent."""
    span = find_tilde_block(text, "plan")
    if span is None:
        return "", [], []
    body_start, body_end, block_end = span
    applied: list[str] = []
    rejected: list[str] = []
    for title, decision, reason in _DECISION_RE.findall(text[block_end:]):
        if decision.lower() == "applied":
            applied.append(title.strip())
        else:
            rejected.append(f"{title.strip()}: {reason.strip()}" if reason.strip() else title.strip())
    return text[body_start:body_end].strip(), applied, rejected


# --- The record ---

def _record(row: dict) -> None:
    """Append to data/plan_checks.jsonl. Losing a line costs a figure and never a chain."""
    row = {"ts": datetime.now(timezone.utc).isoformat(), **row}
    try:
        with (config.DATA_DIR / "plan_checks.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        log.warning("Could not append to plan_checks.jsonl", exc_info=True)


async def _note(ctx: RequestContext, text: str) -> None:
    try:
        await ctx.messenger.send_text(ctx.channel_id, redact_secrets(f"📋 {text}")[:_MAX_NOTE_CHARS], silent=True)
    except Exception:
        log.debug("Plan check note failed for %s", ctx.channel_id, exc_info=True)


# --- The step ---

async def run_plan_check(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None,
    session_id: str | None,
    chain_instances: list[Instance],
) -> CheckOutcome:
    """Run the check. Never raises: a failure here leaves the plan as it was."""
    out = CheckOutcome()
    source = ctx.store.get_instance(source_id)
    row: dict = {
        "repo": source.repo_name if source else None,
        "thread": str(ctx.channel_id),
        "source": source_id,
        "session": session_id,
        "reviewer": None,
        "critical_high": 0,
        "medium_low": 0,
        "applied": [],
        "rejected": [],
        "adopted": False,
        "outcome": SKIPPED,
        "reason": "",
        "reviewer_cost": None,
        "author_cost": None,
    }
    try:
        await _check(ctx, source, source_msg_id, session_id, chain_instances, out, row)
    except Exception:
        log.exception("Plan check failed on %s", source_id)
        row["outcome"], row["reason"] = COULD_NOT_CHECK, "internal error"
        await _note(ctx, "Plan check hit an internal error. Building the plan as written.")
    _record(row)
    return out


def _halt(out: CheckOutcome, row: dict, run: Instance | None, reason: str) -> None:
    out.halted, out.halt_run = True, run
    row["outcome"], row["reason"] = SKIPPED, reason


async def _check(
    ctx: RequestContext,
    source: Instance | None,
    source_msg_id: str | None,
    session_id: str | None,
    chain_instances: list[Instance],
    out: CheckOutcome,
    row: dict,
) -> None:
    if not config.PLAN_CHECK_ENABLED:
        row["reason"] = "switched off"
        return
    if not source or not session_id:
        row["reason"] = "no source or session"
        return
    if source.origin in _ALREADY_REVIEWED:
        row["reason"] = "reviewed already"
        return
    text = _extract_latest_plan_text(ctx, chain_instances, source.id)
    if not text:
        row["reason"] = "no plan"
        return
    plan = prior_art.split_attached(text)[0].strip()
    attached = _attached_history(ctx, session_id)
    history = attached
    if not history:
        # Build & Ship: nothing attached yet. Shown to the reviewer and not added to the brief.
        repo_path = _resolve_chain_repo_path(source)
        if repo_path:
            history = await asyncio.to_thread(prior_art.collect, repo_path, plan)

    owner = await ctx.messenger.owner_messages(ctx.channel_id)
    review = await spawn_from(ctx, source.id, SpawnConfig(
        instance_type=InstanceType.QUERY,
        prompt=build_input(owner, judge.repo_goal(source.repo_path), plan, history),
        mode="explore",
        origin=InstanceOrigin.PLAN_CHECK,
        status_text="Checking the plan...",
        copy_branch=True,
        silent=True,
        permission_mode="explore",
    ), source_msg_id=source_msg_id)

    if review is None:
        if ctx.runner.is_draining:
            _halt(out, row, None, "reboot drain")
            return
        row["outcome"], row["reason"] = COULD_NOT_CHECK, "could not start"
        await _note(ctx, "Plan check could not start. Building the plan as written.")
        return
    row["reviewer"] = review.id
    row["reviewer_cost"] = review.cost_usd
    if review.status == InstanceStatus.KILLED:
        _halt(out, row, review, "killed")
        return
    out.instances.append(review)

    answer = None
    if review.status == InstanceStatus.COMPLETED and not review.needs_input:
        answer = _read_review(review)
    if answer is None:
        row["outcome"] = COULD_NOT_CHECK
        row["reason"] = f"reviewer ended as {review.status.value}" if review.status != InstanceStatus.COMPLETED else "no readable answer"
        await _note(ctx, f"Plan check could not finish ({row['reason']}). Building the plan as written.")
        return

    needs_revision, review_text, out.deferred = answer
    row["medium_low"] = len(out.deferred)
    row["critical_high"] = len(_HIGH_PRIO_RE.findall(review_text))
    if not needs_revision:
        row["outcome"] = CLEAN
        return

    await _apply(ctx, source, session_id, plan, attached, review_text, out, row)


async def _apply(
    ctx: RequestContext,
    source: Instance,
    session_id: str,
    plan: str,
    attached: str,
    review_text: str,
    out: CheckOutcome,
    row: dict,
) -> None:
    """The author adjudicates. Only a complete, well-formed revised plan replaces the old one."""
    fill = {
        "max_chars": str(_BUILD_PLAN_INJECT_MAX),
        "plan": plan,
        "review": review_text[:_MAX_REVIEW_CHARS],
    }
    # One pass, because a plan about this very prompt can hold these words.
    prompt = _APPLY_SLOT_RE.sub(lambda m: fill[m.group(1)], config.PLAN_CHECK_APPLY_PROMPT)
    author = await spawn_from(ctx, source.id, SpawnConfig(
        instance_type=InstanceType.QUERY,
        prompt=prompt,
        mode="explore",
        origin=InstanceOrigin.PLAN_CHECK_APPLY,
        status_text="Weighing the plan check...",
        resume_session=True,
        copy_branch=True,
        silent=True,
        permission_mode="explore",
    ))

    if author is None:
        if ctx.runner.is_draining:
            _halt(out, row, None, "reboot drain")
            return
        row["outcome"], row["reason"] = KEPT, "author could not start"
        await _note(ctx, "Plan check raised revisions, but they could not be weighed. Building the plan as written.")
        return
    row["author_cost"] = author.cost_usd
    if author.status == InstanceStatus.KILLED:
        _halt(out, row, author, "killed")
        return
    out.instances.append(author)

    if author.status != InstanceStatus.COMPLETED or author.needs_input:
        row["outcome"] = KEPT
        row["reason"] = f"author ended as {author.status.value}" if author.status != InstanceStatus.COMPLETED else "author asked a question"
        await _note(ctx, f"Plan check raised revisions, but they could not be weighed ({row['reason']}). Building the plan as written.")
        return
    out.author = author

    revised, applied, rejected = _read_revised_plan(author.read_result_text() or "")
    row["applied"] = [redact_secrets(a) for a in applied]
    row["rejected"] = [redact_secrets(r) for r in rejected]
    why_not = ""
    if not revised:
        why_not = "the answer had no complete plan block"
    elif len(revised) > _BUILD_PLAN_INJECT_MAX:
        why_not = f"the revised plan is over {_BUILD_PLAN_INJECT_MAX} characters"
    if why_not:
        row["outcome"], row["reason"] = KEPT, why_not
        await _note(ctx, f"Plan check raised revisions, but {why_not}. Building the plan as written.")
        return
    if revised == plan:
        row["outcome"], row["reason"] = KEPT, "the author changed nothing"
        await _note(ctx, _decisions_line("Plan check: plan unchanged", applied, rejected))
        return

    ctx.store.set_chain_plan_override(session_id, prior_art.attach(revised, attached))
    row["adopted"] = True
    row["outcome"] = REVISED
    await _note(ctx, _decisions_line("Plan check: plan revised", applied, rejected))


def _decisions_line(head: str, applied: list[str], rejected: list[str]) -> str:
    parts = []
    if applied:
        parts.append("applied: " + "; ".join(applied))
    if rejected:
        parts.append("rejected: " + "; ".join(rejected))
    return f"{head}. {'. '.join(parts)}" if parts else f"{head}."
