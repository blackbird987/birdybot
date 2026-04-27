"""Spawn logic from button callbacks — plan, build, review, commit, autopilot."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bot import config
from bot.claude.types import (
    CODE_CHANGE_TOOLS, ChainPhaseState, Instance, InstanceOrigin, InstanceStatus,
    InstanceType, PHASE_GATES, Phase,
)
from bot.engine import lifecycle, sessions as sessions_mod
from bot.platform.base import ButtonSpec, RequestContext
from bot.platform.formatting import action_button_specs, running_button_specs

log = logging.getLogger(__name__)


async def _notify_user(ctx: RequestContext, suffix: str = "") -> None:
    """Send a @mention to notify the user that the chain needs attention."""
    if not ctx.user_id:
        return
    # If the thread has already been archived (e.g. autopilot merged + closed),
    # skip the mention — re-pinging would force an unwanted unarchive and
    # re-notify the user about an already-completed chain.
    try:
        if await ctx.messenger.is_conversation_closed(ctx.channel_id):
            log.info("Suppressing chain mention: %s is closed", ctx.channel_id)
            return
    except Exception:
        log.debug("is_conversation_closed check raised for %s", ctx.channel_id, exc_info=True)
    mention = ctx.messenger.format_mention(ctx.user_id)
    if not mention:
        return
    text = f"{mention} {suffix}" if suffix else mention
    try:
        await ctx.messenger.send_text(ctx.channel_id, text, silent=False)
    except Exception:
        log.warning("Failed to send chain mention for %s", ctx.channel_id, exc_info=True)
        # Fallback: plain text without mention
        fallback = suffix.strip() if suffix.strip() else "Chain completed"
        try:
            await ctx.messenger.send_text(ctx.channel_id, fallback, silent=False)
        except Exception:
            log.exception("Fallback notification also failed for %s", ctx.channel_id)


async def _exit_chain(
    ctx: RequestContext,
    source_id: str,
    session_id: str | None,
    steps: list[str],
    completed_steps: list[str],
    chain_instances: list[Instance],
    result: Instance | None,
    outcome: str,
    suffix: str,
    *,
    clear_state: bool = False,
    intervention: bool = False,
) -> None:
    """Consistent chain exit: evaluate, optionally clear state, notify user.

    Callers MUST return immediately after calling this.
    """
    if result:
        chain_instances.append(result)
    _eval_chain_safe(ctx.store, source_id, steps, completed_steps,
                     chain_instances, outcome, intervention=intervention)
    if clear_state:
        ctx.store.clear_autopilot_chain(session_id)
        ctx.store.clear_chain_deferred(session_id)
        ctx.store.clear_chain_phases(session_id)
    await _notify_user(ctx, suffix)


# Mention text per chain-pause outcome — keeps wording consistent and ensures
# the send_text fallback inside _notify_user is never empty.
_CHAIN_EXIT_MESSAGES: dict[str, str] = {
    "needs_input": "Needs your attention.",
    "failed": "Chain failed — needs your attention.",
    "phantom_detected": (
        "Release verifier flagged phantom claims. "
        "Tap Amend to fix, or Continue to ship anyway."
    ),
    "budget_exhausted": "Cost budget reached.",
    "merge_failed": "Merge failed — needs resolution.",
    "abandoned": "Build had no changes.",
}


async def _exit_chain_needs_input(
    ctx: RequestContext,
    source_id: str,
    session_id: str | None,
    steps: list[str],
    completed_steps: list[str],
    chain_instances: list[Instance],
    result: Instance | None,
    outcome: Literal[
        "needs_input", "failed", "phantom_detected",
        "budget_exhausted", "merge_failed", "abandoned",
    ],
    *,
    clear_state: bool | None = None,
    intervention: bool | None = None,
    suffix_override: str | None = None,
) -> None:
    """Chain exit for halt/intervention paths.

    Wraps `_exit_chain` with outcome-specific defaults so each callsite no
    longer has to hand-craft mention text or pick `clear_state` /
    `intervention`. Pass an override only if the default is wrong for the
    callsite (e.g. tests that need a specific wording).

    Callers MUST return immediately after calling this.
    """
    suffix = suffix_override or _CHAIN_EXIT_MESSAGES.get(outcome) or "Chain stopped."

    # Defaults: phantom_detected and needs_input keep state for resume;
    # everything else is terminal and clears chain state.
    if clear_state is None:
        clear_state = outcome not in ("needs_input", "phantom_detected")
    if intervention is None:
        intervention = outcome in ("needs_input", "phantom_detected", "failed")

    await _exit_chain(
        ctx, source_id, session_id, steps, completed_steps,
        chain_instances, result, outcome, suffix,
        clear_state=clear_state, intervention=intervention,
    )


@dataclass
class SpawnConfig:
    instance_type: InstanceType
    prompt: str
    mode: str
    origin: InstanceOrigin
    status_text: str = "Processing..."
    resume_session: bool = False
    copy_branch: bool = False
    auto_branch: bool = False
    silent: bool = False
    # Hard read-only floor: when set to "explore", spawn_from forces the new
    # instance to explore mode AND bash_policy="none", overriding any value
    # that would otherwise be inherited from the parent (e.g. a build-mode
    # parent leaking write tools into a triage subagent).
    permission_mode: str | None = None


def _enforce_readonly_floor(
    permission_mode: str | None,
    spawn_mode: str,
    bash_policy: str,
) -> tuple[str, str]:
    """Named hook: clamp mode + bash_policy when a permission_mode floor is set.

    Currently the only supported floor is ``"explore"``: clamps to explore
    mode AND ``bash_policy="none"`` so the spawned subagent can't write
    files via Edit/Write/NotebookEdit OR via Bash. Raises ValueError on
    unknown floor so a typo can't silently bypass the safety net.
    """
    if permission_mode is None:
        return (spawn_mode, bash_policy)
    if permission_mode == "explore":
        return ("explore", "none")
    raise ValueError(f"unknown permission_mode floor: {permission_mode!r}")


# --- Helpers ---

_REVIEW_STATUS_RE = re.compile(r'```review-status\s*\n(.*?)```', re.DOTALL)
_HIGH_PRIO_RE = re.compile(r'(?:Critical|High)\s*[·|]', re.IGNORECASE)
_TRIAGE_RESULT_RE = re.compile(r'```triage-result\s*\n(.*?)```', re.DOTALL)


def _last_msg_id(inst: Instance, platform: str) -> str | None:
    """Get the last message ID for an instance on a platform."""
    msgs = inst.message_ids.get(platform, [])
    return msgs[-1] if msgs else None


def _needs_revision(inst: Instance) -> bool:
    """Check if a plan review found Critical/High revisions.

    Parses the review-status block first; falls back to regex on prose.
    """
    if not inst.result_file:
        return False
    try:
        text = Path(inst.result_file).read_text(encoding="utf-8")
    except Exception:
        return False
    m = _REVIEW_STATUS_RE.search(text)
    if m:
        return "needs_revision: yes" in m.group(1).lower()
    # Fallback: scan prose for Critical/High markers
    return bool(_HIGH_PRIO_RE.search(text))


def _parse_deferred_block(inst: Instance, pattern: re.Pattern[str]) -> list[str]:
    """Parse DEFERRED items from a structured block matching *pattern*."""
    if not inst.result_file:
        return []
    try:
        text = Path(inst.result_file).read_text(encoding="utf-8")
    except Exception:
        return []
    m = pattern.search(text)
    if not m:
        return []
    lines: list[str] = []
    in_deferred = False
    for line in m.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("DEFERRED:"):
            in_deferred = True
        elif stripped.startswith("- ") and in_deferred:
            lines.append(stripped[2:])
        elif stripped and not stripped.startswith("-"):
            in_deferred = False
    return lines


def _extract_deferred(inst: Instance) -> list[str]:
    """Extract deferred revision items from review-status block."""
    return _parse_deferred_block(inst, _REVIEW_STATUS_RE)


def _extract_triage_deferred(inst: Instance) -> list[str]:
    """Extract still-deferred items after LLM triage."""
    return _parse_deferred_block(inst, _TRIAGE_RESULT_RE)


# --- Phase plan parsing & gate helpers ---

_PHASE_PLAN_RE = re.compile(r'```phase-plan\s*\n(.*?)```', re.DOTALL)


def _extract_phase_plan(inst: Instance | None) -> list[Phase]:
    """Parse a `phase-plan` fenced block from an instance's result file.

    Each line inside the block looks like:
      `- id: <slug> | title: <title> | gate: mechanical|design|risk [| reason: <text>]`
    Lines with missing/invalid id or gate are silently skipped.
    Returns [] if the block is absent or malformed.
    """
    if not inst or not inst.result_file:
        return []
    try:
        text = Path(inst.result_file).read_text(encoding="utf-8")
    except OSError:
        return []
    # Take the LAST block — if the plan was revised, the most recent
    # phase-plan supersedes earlier drafts in the same result file.
    blocks = _PHASE_PLAN_RE.findall(text)
    if not blocks:
        return []
    phases: list[Phase] = []
    for raw in blocks[-1].splitlines():
        line = raw.strip()
        if not line.startswith("-"):
            continue
        line = line.lstrip("-").strip()
        fields: dict[str, str] = {}
        for part in line.split("|"):
            if ":" not in part:
                continue
            k, _, v = part.partition(":")
            fields[k.strip().lower()] = v.strip()
        pid = fields.get("id", "")
        title = fields.get("title", "")
        gate = fields.get("gate", "").lower()
        reason = fields.get("reason", "")
        if not pid or gate not in PHASE_GATES:
            continue
        phases.append(Phase(id=pid, title=title, gate=gate, reason=reason))
    return phases


def _find_phase_plan(store, inst: Instance | None, max_depth: int = 24) -> list[Phase]:
    """Walk parent_id chain to find the most recent phase-plan block.

    The plan-review loop (review → apply → triage) often produces follow-up
    instances whose result files don't echo the original `phase-plan` block
    (e.g. TRIAGE_DEFERRED_PROMPT only asks for the triage section). Without
    this walk, autopilot would silently fall back to single-shot for any
    plan that went through revisions. Depth must accommodate the worst-case
    review loop: up to MAX_ROUNDS=5 review/apply pairs (10 instances) plus
    plan + triage + the originating question, so we budget 24 to leave
    headroom for nested chain entry points. Bounded to protect against any
    future parent-id cycle.
    """
    visited: set[str] = set()
    current = inst
    while current and current.id not in visited and len(visited) < max_depth:
        visited.add(current.id)
        phases = _extract_phase_plan(current)
        if phases:
            return phases
        if not current.parent_id:
            return []
        current = store.get_instance(current.parent_id)
    return []


async def _git_head(path: str | None) -> str | None:
    """Return the current git HEAD SHA at *path*, or None if unavailable."""
    if not path or not Path(path).is_dir():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", path, "rev-parse", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            return stdout.decode().strip() or None
    except Exception:
        log.debug("git rev-parse failed for %s", path, exc_info=True)
    return None


def _phase_gate_suffix(phase: Phase, where: str) -> str:
    """Build the user-facing notification text for a phase gate pause."""
    if where == "pre":
        head = f"Phase `{phase.id}` ({phase.title}) needs your input before starting"
    else:
        head = f"Phase `{phase.id}` ({phase.title}) shipped — review before next phase"
    if phase.reason:
        head += f": {phase.reason}"
    return head + "."


# --- Spawn ---

async def spawn_from(
    ctx: RequestContext,
    source_id: str,
    cfg: SpawnConfig,
    strip_source_buttons: bool = True,
    source_msg_id: str | None = None,
) -> Instance | None:
    """Common pattern for spawning a new instance from a button press.

    Returns the new Instance, or None if guards fail.
    """
    source = ctx.store.get_instance(source_id)
    if not source:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return None

    # Block spawns during reboot drain. Same-session overlap is allowed —
    # the channel lock + Queued embed serialize it visibly.
    check_session = source.session_id if cfg.resume_session else None
    spawn_err = ctx.runner.check_spawn_allowed(check_session)
    if spawn_err:
        if ctx.runner.is_draining:
            # If this session has an active autopilot chain, don't queue the
            # individual step — chain state in state.json handles full resume.
            # Queuing here would put the thread in drain_callback_channel_ids,
            # causing chain resume to skip it (replaying one step, not the chain).
            has_chain = check_session and ctx.store.get_autopilot_chain(check_session)
            if not has_chain:
                ctx.runner.queue_for_replay({
                    "channel_id": ctx.channel_id,
                    "platform": ctx.platform,
                    "type": "callback",
                    "action": cfg.origin.value,
                    "instance_id": source_id,
                    "source_msg_id": source_msg_id,
                    "repo_name": source.repo_name,
                    "user_id": source.user_id or (ctx.user_id or ""),
                    "user_name": source.user_name or (ctx.user_name or ""),
                    "is_owner": source.is_owner_session,
                })
            await ctx.messenger.send_text(
                ctx.channel_id,
                "Reboot in progress — this action will auto-resume after restart.",
            )
        else:
            await ctx.messenger.send_text(ctx.channel_id, spawn_err)
        return None

    # Budget guard (lazy import to avoid circular: commands -> workflows -> commands)
    from bot.engine.commands import check_budget
    if not check_budget(ctx):
        await ctx.messenger.send_text(
            ctx.channel_id, "Daily budget exceeded. Use /budget reset to override.",
        )
        return None

    # Repo guard
    if not source.repo_path or not Path(source.repo_path).is_dir():
        await ctx.messenger.send_text(ctx.channel_id, "Repo path no longer valid.")
        return None

    # Session guard
    if cfg.resume_session and not source.session_id:
        await ctx.messenger.send_text(ctx.channel_id, "No session to resume.")
        return None

    # Enforce mode ceiling for non-owner sessions
    effective_spawn_mode = cfg.mode
    if ctx.mode_ceiling:
        _rank = {"explore": 0, "plan": 1, "build": 2}
        if _rank.get(cfg.mode, 0) > _rank.get(ctx.mode_ceiling, 0):
            effective_spawn_mode = ctx.mode_ceiling

    # Apply permission_mode floor BEFORE create_instance so the new mode
    # is what gets persisted from the start (no race window where a parent's
    # build mode leaks into the child's command line).
    inherited_bash_policy = source.bash_policy
    effective_spawn_mode, inherited_bash_policy = _enforce_readonly_floor(
        cfg.permission_mode, effective_spawn_mode, inherited_bash_policy,
    )

    new_inst = ctx.store.create_instance(
        instance_type=cfg.instance_type,
        prompt=cfg.prompt,
        mode=effective_spawn_mode,
    )
    new_inst.origin = cfg.origin
    new_inst.origin_platform = ctx.platform
    new_inst.effort = ctx.effective_effort
    # Route mechanical explore steps to a lighter model if configured.
    # Plan stays on Opus (core architectural thinking); review/apply are structured follow-ups.
    _EXPLORE_MODEL_ORIGINS = frozenset({InstanceOrigin.REVIEW_PLAN, InstanceOrigin.APPLY_REVISIONS})
    if cfg.origin in _EXPLORE_MODEL_ORIGINS and config.EXPLORE_MODEL:
        new_inst.model = config.EXPLORE_MODEL
    new_inst.parent_id = source.id
    new_inst.repo_name = source.repo_name
    new_inst.repo_path = source.repo_path
    # Inherit user identity and access control from parent
    new_inst.user_id = source.user_id or (ctx.user_id or "")
    new_inst.user_name = source.user_name or (ctx.user_name or "")
    new_inst.is_owner_session = source.is_owner_session
    new_inst.bash_policy = inherited_bash_policy
    new_inst.deferred_revisions = source.deferred_revisions

    if cfg.resume_session:
        new_inst.session_id = source.session_id
    if cfg.copy_branch:
        new_inst.branch = source.branch
        new_inst.original_branch = source.original_branch
        new_inst.worktree_path = source.worktree_path
    elif cfg.auto_branch:
        new_inst.branch = f"{config.BRANCH_PREFIX}/{new_inst.id}"

    ctx.store.update_instance(new_inst)

    # Strip workflow buttons from source message, keep expand/log if truncated
    if strip_source_buttons and source_msg_id:
        try:
            preserve = None
            if source.result_file and Path(source.result_file).exists():
                try:
                    size = Path(source.result_file).stat().st_size
                    if size >= 2000:
                        full = action_button_specs(source, show_expand=True)
                        preserve = [
                            row for row in full
                            if any(b.callback_data.startswith(("expand:", "log:"))
                                   for b in row)
                        ]
                        if not preserve:
                            preserve = None
                except OSError:
                    pass
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, None, buttons=preserve)
        except Exception:
            pass

    # Send thinking message
    escaped = ctx.messenger.escape(new_inst.display_id())
    escaped_status = ctx.messenger.escape(cfg.status_text.lower())
    handle = await ctx.messenger.send_thinking(
        ctx.channel_id, f"⏳ {escaped} {escaped_status}",
        buttons=running_button_specs(new_inst.id),
    )
    # Track the thinking message
    if handle.get("message_id"):
        new_inst.message_ids.setdefault(ctx.platform, []).append(
            handle.get("message_id")
        )
        ctx.store.update_instance(new_inst)

    await lifecycle.run_instance(ctx, new_inst, handle=handle, silent=cfg.silent)
    return new_inst


# --- Individual workflow steps (all return Instance | None) ---

async def on_plan(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    source = ctx.store.get_instance(source_id)
    if not source:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return None
    prompt = config.PLAN_PROMPT_PREFIX + source.prompt
    return await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.QUERY, prompt=prompt,
        mode="explore", origin=InstanceOrigin.PLAN,
        status_text="Planning...", resume_session=True,
    ), source_msg_id=source_msg_id)


async def on_build(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    source = ctx.store.get_instance(source_id)
    if not source:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return None
    is_plan = source.plan_active
    return await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.TASK,
        prompt=config.BUILD_FROM_PLAN_PROMPT if is_plan else config.BUILD_FROM_QUERY_PROMPT,
        mode="build", origin=InstanceOrigin.BUILD,
        status_text="Building...", resume_session=True,
        auto_branch=True, silent=True,
    ), source_msg_id=source_msg_id)


async def on_review_plan(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    return await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.QUERY, prompt=config.PLAN_REVIEW_PROMPT,
        mode="explore", origin=InstanceOrigin.REVIEW_PLAN,
        status_text="Reviewing plan...", resume_session=True,
    ), source_msg_id=source_msg_id)


async def on_apply_revisions(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    return await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.QUERY, prompt=config.APPLY_REVISIONS_PROMPT,
        mode="explore", origin=InstanceOrigin.APPLY_REVISIONS,
        status_text="Applying revisions...", resume_session=True,
    ), source_msg_id=source_msg_id)


async def on_review_code(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    """Review code, auto-looping until no issues remain (max 5 rounds)."""
    MAX_ROUNDS = 5
    current_source = source_id
    current_msg = source_msg_id
    result: Instance | None = None
    for round_num in range(MAX_ROUNDS):
        status = "Reviewing code..." if round_num == 0 else f"Re-reviewing (round {round_num + 1})..."
        result = await spawn_from(ctx, current_source, SpawnConfig(
            instance_type=InstanceType.TASK, prompt=config.CODE_REVIEW_PROMPT,
            mode="build", origin=InstanceOrigin.REVIEW_CODE,
            status_text=status, resume_session=True,
            copy_branch=True, silent=True,
        ), source_msg_id=current_msg)

        if not result:
            break

        # Clean review (no code changes) — done
        if not (CODE_CHANGE_TOOLS & set(result.tools_used or [])):
            break

        # Changes were made — strip buttons from this result, review again
        current_source = result.id
        current_msg = _last_msg_id(result, ctx.platform)

    return result


# --- Verify ---

_VERIFY_BLOCK_RE = re.compile(
    r'```verify\s*\n(.*?)\n```', re.IGNORECASE | re.DOTALL
)
_VERIFY_RESULT_RE = re.compile(
    r'^\s*RESULT:\s*(pass|fail|skip|manual)\s*$', re.IGNORECASE | re.MULTILINE
)
_VERIFY_ACTIONS_RE = re.compile(
    r'^\s*ACTIONS_TESTED:\s*(.+)$', re.IGNORECASE | re.MULTILINE
)
_VERIFY_ENDPOINTS_RE = re.compile(
    r'^\s*ENDPOINTS_USED:\s*(.+)$', re.IGNORECASE | re.MULTILINE
)
_VERIFY_SUMMARY_RE = re.compile(
    r'^\s*SUMMARY:\s*(.+)$', re.IGNORECASE | re.MULTILINE
)
_VERIFY_WHY_RE = re.compile(
    r'^\s*WHY:\s*(.+)$', re.IGNORECASE | re.MULTILINE
)


VerifyOutcome = Literal["pass", "fail", "manual", "skip", "crashed"]


def _verify_outcome(inst: Instance) -> VerifyOutcome:
    """Classify a verify-instance run into one of five outcomes.

    Status check runs FIRST: a non-COMPLETED instance is `crashed` regardless
    of whatever block text the runner happened to capture. This prevents a
    stale `RESULT:` line from a prior fix-loop round (or partial stdout) from
    masquerading as a real verdict when the verify environment actually died.

    On healthy instances we parse the LAST ```verify``` block — if the model
    narrates/pre-quotes the template before the real output, the final block
    is authoritative. Missing block / unparseable RESULT collapses to `fail`
    so the fix-loop runs (Claude forgot to emit it ≠ environment crashed).
    """
    if inst.status != InstanceStatus.COMPLETED:
        return "crashed"
    if not inst.result_file:
        return "fail"
    try:
        text = Path(inst.result_file).read_text(encoding="utf-8")
    except OSError:
        return "fail"
    blocks = _VERIFY_BLOCK_RE.findall(text)
    if not blocks:
        return "fail"
    block = blocks[-1]
    result_m = _VERIFY_RESULT_RE.search(block)
    if not result_m:
        return "fail"
    result = result_m.group(1).lower()
    if result == "skip":
        summary = _VERIFY_SUMMARY_RE.search(block)
        log.info(
            "Verify skipped: %s",
            summary.group(1).strip() if summary else "no reason given",
        )
        return "skip"
    if result == "manual":
        why = _VERIFY_WHY_RE.search(block)
        log.info(
            "Verify manual: %s",
            why.group(1).strip() if why else "no WHY given",
        )
        return "manual"
    # pass/fail path — log what was actually tested for observability
    actions = _VERIFY_ACTIONS_RE.search(block)
    endpoints = _VERIFY_ENDPOINTS_RE.search(block)
    if actions:
        log.info("Verify actions: %s", actions.group(1).strip())
    if endpoints:
        log.info("Verify endpoints: %s", endpoints.group(1).strip())
    return "pass" if result == "pass" else "fail"


def _verify_why(inst: Instance) -> str | None:
    """Extract WHY: line from the LAST verify block, or None if absent."""
    if not inst.result_file:
        return None
    try:
        text = Path(inst.result_file).read_text(encoding="utf-8")
    except OSError:
        return None
    blocks = _VERIFY_BLOCK_RE.findall(text)
    if not blocks:
        return None
    m = _VERIFY_WHY_RE.search(blocks[-1])
    if not m:
        return None
    why = m.group(1).strip()
    return why or None


async def _enroll_verify_board(
    ctx: RequestContext, inst: Instance, why: str,
) -> None:
    """Push a Verify Board item for this instance + reason.

    Delegates to the platform's `add_verify_item` callback (Discord populates
    it; Telegram is None → no-op). Lock-protected mutation + state save +
    debounced board refresh all happen inside the callback. The engine never
    touches forum_projects directly.

    Picks the best human-readable origin label available without breaking
    platform agnosticism: `inst.summary` > `inst.name` > `inst.display_id()`.
    The Discord side may further enrich this with the actual thread title.
    """
    if not ctx.add_verify_item or not inst.repo_name:
        return
    label = (
        (inst.summary or "").strip()
        or (inst.name or "").strip()
        or inst.display_id()
    )
    if len(label) > 60:
        label = label[:59].rstrip() + "…"
    thread_id = str(ctx.channel_id) if ctx.channel_id else None
    try:
        await ctx.add_verify_item(
            inst.repo_name, why, thread_id, label, inst.id,
        )
    except Exception:
        log.exception(
            "Failed to enroll Verify Board item for %s in %s",
            inst.id, inst.repo_name,
        )


def _load_verify_policy(source_inst: Instance | None) -> str:
    """Read verify_policy from .claude/test.json. Default: 'warn'."""
    if not source_inst or not source_inst.repo_path:
        return "warn"
    test_json = Path(source_inst.repo_path) / ".claude" / "test.json"
    if test_json.exists():
        try:
            cfg = json.loads(test_json.read_text(encoding="utf-8"))
            return cfg.get("verify_policy", "warn")
        except Exception:
            pass
    return "warn"


async def on_verify(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    """Run verification. Auto-fix loop only on `fail`; other outcomes break.

    Outcomes (`_verify_outcome`):
      pass    — break, chain advances
      skip    — break, chain advances (verify legitimately had nothing to run)
      manual  — verification was impossible. Under `warn`, enroll Verify Board
                + flag the instance + advance. Under `block`, halt with
                needs_input (block opt-in exists to refuse "I couldn't verify").
      fail    — re-enter the fix-loop; after MAX_FIX_ROUNDS, enroll under `warn`
                or halt under `block`.
      crashed — verify environment died (status != COMPLETED). Under `warn`,
                enroll + advance. Under `block`, halt.

    Respects verify_policy from .claude/test.json (default: "warn").
    """
    MAX_FIX_ROUNDS = 2
    current_source = source_id
    current_msg = source_msg_id
    source_inst = ctx.store.get_instance(source_id)
    verify_policy = _load_verify_policy(source_inst)

    result: Instance | None = None
    for round_num in range(MAX_FIX_ROUNDS + 1):
        status = "Verifying..." if round_num == 0 else f"Re-verifying (fix {round_num})..."
        result = await spawn_from(ctx, current_source, SpawnConfig(
            instance_type=InstanceType.TASK,
            prompt=config.VERIFY_PROMPT,
            mode="build",
            origin=InstanceOrigin.VERIFY,
            status_text=status,
            resume_session=True,
            copy_branch=True,
            silent=True,
        ), source_msg_id=current_msg)

        if not result:
            break

        outcome = _verify_outcome(result)

        if outcome in ("pass", "skip"):
            break

        if outcome == "manual":
            why = _verify_why(result) or "verification couldn't run automatically"
            if verify_policy == "warn":
                await _enroll_verify_board(ctx, result, why)
                result.needs_manual_verification = True
                result.manual_verify_reason = why
                ctx.store.update_instance(result)
            else:
                # block: refuse to advance on "I couldn't verify"
                result.needs_input = True
                ctx.store.update_instance(result)
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"⛔ Verify reported `manual` under block policy — halting. {why}",
                    silent=True,
                )
            break

        if outcome == "crashed":
            why = "verify environment crashed — manual check needed"
            if verify_policy == "warn":
                await _enroll_verify_board(ctx, result, why)
                # Note: status != COMPLETED so we cannot persist the flag
                # on this instance — the Verify Board entry is the signal.
            else:
                # block: halt. The instance already failed so needs_input
                # would not be reached by the chain guard; surface the reason.
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"⛔ Verify environment crashed under block policy — halting. {why}",
                    silent=True,
                )
            break

        # outcome == "fail" — Claude already tried to fix inline. Decide
        # whether to re-enter the fix-loop or terminate.
        if round_num >= MAX_FIX_ROUNDS:
            if verify_policy == "warn":
                why = (
                    f"verify failed after {MAX_FIX_ROUNDS + 1} rounds — "
                    "manual check needed"
                )
                await _enroll_verify_board(ctx, result, why)
                result.needs_manual_verification = True
                result.manual_verify_reason = why
                ctx.store.update_instance(result)
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"⚠️ Verification failed after {round_num + 1} rounds — "
                    "proceeding; item posted to Verify Board.",
                    silent=True,
                )
            else:
                # block policy — halt the chain via needs_input
                result.needs_input = True
                ctx.store.update_instance(result)
            break

        # Re-enter fix-loop: feed this instance into the next round.
        current_source = result.id
        current_msg = _last_msg_id(result, ctx.platform)

    return result


async def on_commit(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    return await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.TASK, prompt=config.COMMIT_PROMPT,
        mode="build", origin=InstanceOrigin.COMMIT,
        status_text="Committing...", resume_session=True,
        copy_branch=True, silent=True,
    ), source_msg_id=source_msg_id)


async def on_done(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None = None,
    *,
    prompt_variant: Literal["chain", "standalone"] = "standalone",
    extra_context: str | None = None,
) -> Instance | None:
    """Commit all changes, update changelog, then close the conversation.

    `prompt_variant`:
      - "standalone" (default): full /done — commit + cut release + tag.
      - "chain": commit + update [Unreleased] only. Release/tag run later
        as the autopilot `release` step, after the `verify_release` gate.

    `extra_context` is prepended to the prompt and used by the Amend button
    to surface the verifier's phantom-bullet rationale.
    """
    if prompt_variant == "chain":
        prompt = config.DONE_PROMPT_CHAIN
    else:
        prompt = config.DONE_PROMPT_STANDALONE
    if extra_context:
        prompt = f"{extra_context}\n\n{prompt}"

    result = await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.TASK, prompt=prompt,
        mode="build", origin=InstanceOrigin.DONE,
        status_text="Wrapping up...", resume_session=True,
        copy_branch=True, silent=True,
    ), source_msg_id=source_msg_id)

    if not result or result.status != InstanceStatus.COMPLETED:
        return result

    # In chain mode the `release` step still runs after this — never close
    # the thread mid-chain, even when there is no branch to merge.
    if prompt_variant == "chain":
        return result

    # Standalone Done: if a build branch is open, attempt the same merge +
    # tag + close sequence the autopilot chain runs. On failure the
    # Merge/Discard buttons remain (current behavior) so the user can
    # resolve manually.
    if result.branch and result.original_branch:
        merged_ok = await _finalize_merge(ctx, result, close_silent=True)
        if not merged_ok:
            try:
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    "⚠️ Auto-merge failed. Use /merge or the Merge "
                    "button to resolve.",
                    silent=True,
                )
            except Exception:
                log.debug("Failed to send merge-failed hint", exc_info=True)
        return result

    # No branch to merge — close the thread directly.
    try:
        await ctx.messenger.close_conversation(ctx.channel_id, skip_mention=True)
    except Exception:
        log.debug("Failed to close conversation for channel %s", ctx.channel_id, exc_info=True)

    return result


# --- Autopilot chains ---

_AUTOPILOT_STEPS = [
    "review_loop", "build", "review_code", "verify",
    "done", "verify_release", "release", "merge",
]
_BUILD_AND_SHIP_STEPS = [
    "build", "review_code", "verify",
    "done", "verify_release", "release", "merge",
]
_AUTOPILOT_HOLD_STEPS = [
    "review_loop", "build", "review_code", "verify",
    "done", "verify_release", "release",
]


async def _review_plan_loop(
    ctx: RequestContext, source_id: str, source_msg_id: str | None,
) -> Instance | None:
    """Review plan, auto-applying Critical/High revisions. Max 5 rounds.

    Stores deferred revisions on the returned Instance.
    """
    MAX_ROUNDS = 5
    current_source = source_id
    current_msg = source_msg_id
    last_review: Instance | None = None

    # Build review prompt with prior deferred context.
    # Use the store's normalized dedup key (same one append_deferred uses)
    # so the injection list collapses semantically-equivalent rewordings —
    # otherwise nearly-identical items pile up across review rounds.
    source = ctx.store.get_instance(source_id)
    prior_deferred: list[str] = []
    if source and source.repo_name:
        raw_items = ctx.store.get_deferred_items(source.repo_name)
        seen_keys: set[str] = set()
        for item in raw_items:
            key = ctx.store.deferred_dedup_key(item)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            prior_deferred.append(item)

    review_prompt = config.PLAN_REVIEW_PROMPT
    if prior_deferred:
        items_text = "\n".join(f"- {d}" for d in prior_deferred[:15])
        review_prompt = (
            "Previously deferred review items for this repo "
            "(address if relevant to this plan):\n"
            f"{items_text}\n\n{review_prompt}"
        )

    for round_num in range(MAX_ROUNDS):
        status = "Reviewing plan..." if round_num == 0 else f"Re-reviewing plan (round {round_num + 1})..."
        review = await spawn_from(ctx, current_source, SpawnConfig(
            instance_type=InstanceType.QUERY,
            prompt=review_prompt,
            mode="explore",
            origin=InstanceOrigin.REVIEW_PLAN,
            status_text=status,
            resume_session=True,
        ), source_msg_id=current_msg)

        if not review or review.status != InstanceStatus.COMPLETED:
            return review
        if review.needs_input:
            return review

        last_review = review

        # No Critical/High revisions — converged
        if not _needs_revision(review):
            deferred = _extract_deferred(review)

            # LLM triage: let the model decide which Medium/Low to apply
            if deferred:
                deferred_text = "\n".join(f"- {d}" for d in deferred)
                triage_prompt = config.TRIAGE_DEFERRED_PROMPT.format(
                    deferred_items=deferred_text,
                )
                pre_triage_id = review.id
                pre_triage_msg = _last_msg_id(review, ctx.platform)

                triage_result = await spawn_from(ctx, review.id, SpawnConfig(
                    instance_type=InstanceType.QUERY,
                    prompt=triage_prompt,
                    mode="explore",
                    origin=InstanceOrigin.APPLY_REVISIONS,
                    status_text="Triaging medium/low revisions...",
                    resume_session=True,
                    # Hard floor: triage is read-only by intent; even if the
                    # parent is a build-mode session, this subagent must not
                    # touch files. Pinned via the named enforcement hook.
                    permission_mode="explore",
                ), source_msg_id=pre_triage_msg)

                if (triage_result
                        and triage_result.status == InstanceStatus.COMPLETED
                        and not triage_result.needs_input):
                    triage_result.deferred_revisions = _extract_triage_deferred(triage_result)
                    ctx.store.update_instance(triage_result)
                    return triage_result

                # Triage needs user input — pause chain as normal
                if triage_result and triage_result.needs_input:
                    return triage_result

                # Triage hit usage limit — pause chain, let cooldown retry handle it
                if triage_result and triage_result.cooldown_retry_at:
                    return triage_result

                # Triage failed — log, notify, fall back to pre-triage review
                log.warning(
                    "Triage step failed (status=%s), falling back to pre-triage review",
                    triage_result.status if triage_result else "None",
                )
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    "\u26a0\ufe0f Medium/Low triage step failed \u2014 deferring all items.",
                    silent=True,
                )
                review = ctx.store.get_instance(pre_triage_id) or review

            review.deferred_revisions = deferred
            ctx.store.update_instance(review)
            return review

        # Apply only Critical/High
        applied = await spawn_from(ctx, review.id, SpawnConfig(
            instance_type=InstanceType.QUERY,
            prompt=config.APPLY_HIGH_PRIORITY_PROMPT,
            mode="explore",
            origin=InstanceOrigin.APPLY_REVISIONS,
            status_text=f"Applying revisions (round {round_num + 1})...",
            resume_session=True,
        ), source_msg_id=_last_msg_id(review, ctx.platform))

        if not applied or applied.status != InstanceStatus.COMPLETED:
            return applied
        if applied.needs_input:
            return applied

        current_source = applied.id
        current_msg = _last_msg_id(applied, ctx.platform)

    # Max rounds hit — store whatever the last review found
    if last_review:
        last_review.deferred_revisions = _extract_deferred(last_review)
        ctx.store.update_instance(last_review)
    return last_review


async def _finalize_merge(
    ctx: RequestContext, merge_target: Instance, *, close_silent: bool = False,
) -> bool:
    """Merge a build branch + post-merge bookkeeping + close the thread.

    Shared between the autopilot `merge` step and the standalone Done button
    so both call sites apply the same tag/deploy/close sequence.

    Returns True on successful merge (caller may chain on this), False if
    the merge command reported failure. On failure, caller is responsible
    for posting the "use /merge to resolve" hint and surfacing buttons —
    `_finalize_merge` only mutates state on the success path.

    `close_silent`: pass True when the caller already pinged the user (or
    intentionally wants the close to skip the participant mention). The
    autopilot path defaults to a loud close (chain-completion notification);
    the standalone Done path opts in to silent close.
    """
    if not merge_target.branch or not merge_target.original_branch:
        return False
    branch_name = merge_target.branch
    merge_msg = await ctx.runner.merge_branch(merge_target)
    ctx.store.update_instance(merge_target)
    log.info("Auto-merge: %s", merge_msg)
    if "failed" in merge_msg.lower():
        return False
    clear_stale_branches(ctx.store, branch_name)
    from bot.engine.deploy import update_after_merge, rescan_deploy_config_after_merge
    update_after_merge(ctx.store, merge_target)
    rescan_deploy_config_after_merge(
        ctx.store, merge_target.repo_name, merge_target.repo_path,
    )
    await ctx.messenger.on_deploy_state_changed(merge_target.repo_name)
    # Apply "merged" tag before close (tag must land before archive)
    if ctx.on_merged:
        await ctx.on_merged()
    await ctx.messenger.send_text(
        ctx.channel_id, f"✅ {merge_msg}", silent=True,
    )
    try:
        await ctx.messenger.close_conversation(
            ctx.channel_id, skip_mention=close_silent,
        )
    except Exception:
        log.debug("Failed to close conversation after merge", exc_info=True)
    return True


def _find_mergeable_instance(store, session_id: str | None) -> Instance | None:
    """Find a completed done instance with a branch, for merge-step resume."""
    if not session_id:
        return None
    # list_instances returns newest-first — first match is the most recent
    all_insts = store.list_instances(all_=True)
    # Prefer a done instance
    for inst in all_insts:
        if (inst.session_id == session_id
                and inst.origin == InstanceOrigin.DONE
                and inst.status == InstanceStatus.COMPLETED
                and inst.branch and inst.original_branch):
            return inst
    # Fallback: any instance in the session with a branch
    for inst in all_insts:
        if (inst.session_id == session_id
                and inst.branch and inst.original_branch):
            return inst
    return None


_NOWND: dict = config.NOWND
_DIFF_PAYLOAD_CAP = 20 * 1024  # 20KB before truncation

# Anchored regex: ## [Unreleased] header, capture until next ## header or EOF.
_UNRELEASED_RE = re.compile(
    r'^##\s*\[Unreleased\]\s*$\n(.*?)(?=^##\s|\Z)',
    re.DOTALL | re.MULTILINE,
)

# Pull a fenced ```json``` block out of verifier output.
_JSON_BLOCK_RE = re.compile(
    r'```json\s*\n(.*?)```', re.DOTALL,
)


def _git_head_sha(repo_path: str) -> str | None:
    """Return current HEAD SHA in *repo_path*, or None on failure."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, capture_output=True, timeout=5, text=True, **_NOWND,
        )
        if r.returncode != 0:
            return None
        sha = r.stdout.strip()
        return sha or None
    except Exception:
        log.warning("git rev-parse HEAD failed in %s", repo_path, exc_info=True)
        return None


def _git_log_messages(repo_path: str, entry_sha: str) -> str:
    """Concatenated commit messages from entry_sha..HEAD (newest first)."""
    try:
        r = subprocess.run(
            ["git", "log", f"{entry_sha}..HEAD", "--format=%B%x00"],
            cwd=repo_path, capture_output=True, timeout=10, text=True, **_NOWND,
        )
        if r.returncode != 0:
            return ""
        # Split on NUL, strip empties
        parts = [p.strip() for p in r.stdout.split("\x00") if p.strip()]
        return "\n\n---\n\n".join(parts)
    except Exception:
        log.warning("git log failed in %s", repo_path, exc_info=True)
        return ""


def _git_diff_stat(repo_path: str, entry_sha: str) -> str:
    try:
        r = subprocess.run(
            ["git", "diff", "--stat", f"{entry_sha}..HEAD"],
            cwd=repo_path, capture_output=True, timeout=10, text=True, **_NOWND,
        )
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _git_diff_payload(repo_path: str, entry_sha: str) -> tuple[str, bool, list[str]]:
    """Return (diff_text, truncated, file_list).

    Truncation is at _DIFF_PAYLOAD_CAP bytes; file_list is the full
    `git diff --name-only` so a downstream pass can re-fetch any file
    that fell outside the window.
    """
    try:
        r = subprocess.run(
            ["git", "diff", f"{entry_sha}..HEAD"],
            cwd=repo_path, capture_output=True, timeout=15, text=True, **_NOWND,
        )
        diff = r.stdout if r.returncode == 0 else ""
    except Exception:
        diff = ""

    truncated = False
    if len(diff) > _DIFF_PAYLOAD_CAP:
        diff = diff[:_DIFF_PAYLOAD_CAP] + "\n[TRUNCATED]"
        truncated = True

    files: list[str] = []
    try:
        rf = subprocess.run(
            ["git", "diff", "--name-only", f"{entry_sha}..HEAD"],
            cwd=repo_path, capture_output=True, timeout=10, text=True, **_NOWND,
        )
        if rf.returncode == 0:
            files = [ln.strip() for ln in rf.stdout.splitlines() if ln.strip()]
    except Exception:
        pass

    return diff, truncated, files


def _read_unreleased_block(repo_path: str) -> str | None:
    """Return the raw text of the ## [Unreleased] block, or None if missing/malformed."""
    cl = Path(repo_path) / "CHANGELOG.md"
    if not cl.exists():
        return None
    try:
        text = cl.read_text(encoding="utf-8")
    except Exception:
        return None
    # Normalize \r\n / \r so the multiline regex anchors work consistently
    # regardless of how the file was committed.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    m = _UNRELEASED_RE.search(text)
    if not m:
        return None
    body = m.group(1).strip()
    if not body:
        return None
    return body


def _resolve_chain_repo_path(*candidates: Instance | None) -> str | None:
    """Pick the right cwd for git operations during a chain step.

    Walks candidates in order and prefers each one's worktree (where the
    chain is committing) over its bare repo path. Returns the first
    existing directory.
    """
    for cand in candidates:
        if not cand:
            continue
        if cand.worktree_path and Path(cand.worktree_path).is_dir():
            return cand.worktree_path
        if cand.repo_path and Path(cand.repo_path).is_dir():
            return cand.repo_path
    return None


def _parse_verifier_json(text: str) -> dict | None:
    """Extract the verifier's JSON verdict from result text. None on failure.

    Takes the LAST ```json``` block in the output — if Claude narrates or
    quotes the prompt template before emitting the real answer, the final
    block is the authoritative verdict. Matches the convention used by
    `_verify_outcome`.
    """
    if not text:
        return None
    blocks = _JSON_BLOCK_RE.findall(text)
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1].strip())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("verdict") not in ("ok", "mismatch"):
        return None
    return data


async def on_verify_release(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None = None,
    *,
    entry_sha: str | None = None,
) -> Instance | None:
    """Cross-check `done`-step claims (commit messages + CHANGELOG) vs. real diff.

    Spawns a verifier Claude with a bounded payload. On `mismatch` (phantom
    claims), sets `result.needs_input = True` so the chain pauses and the
    user gets Amend / Continue buttons.

    Fail-closed: any of {non-zero exit, empty result, JSON parse error,
    missing required keys, unparseable changelog, no entry SHA} is treated
    as `mismatch` so the user gets a chance to inspect.
    """
    source_inst = ctx.store.get_instance(source_id)
    repo_path = _resolve_chain_repo_path(source_inst)
    if not repo_path:
        log.warning("verify_release: cannot resolve repo path for %s", source_id)
        return None

    # Defensive guards — the dispatch loop also pre-skips on these conditions,
    # but on_verify_release is also called directly by on_amend_done.
    if not entry_sha:
        log.warning("verify_release: no entry SHA — skipping gate")
        return None
    head_sha = _git_head_sha(repo_path)
    if head_sha and head_sha == entry_sha:
        log.info("verify_release: entry == HEAD, no commits to verify")
        return None

    commit_messages = _git_log_messages(repo_path, entry_sha) or "(no commit messages)"
    diff_stat = _git_diff_stat(repo_path, entry_sha) or "(empty)"
    diff_payload, truncated, files = _git_diff_payload(repo_path, entry_sha)
    if not diff_payload:
        diff_payload = "(empty diff)"

    truncation_note = ""
    if truncated:
        files_blob = "\n".join(files) if files else "(unknown)"
        truncation_note = (
            f" — diff was truncated at {_DIFF_PAYLOAD_CAP} bytes. "
            f"Full file list:\n{files_blob}\n"
            f"If a claim references a file outside the window, list it under "
            f"`needs_inspection` instead of marking phantom."
        )

    unreleased = _read_unreleased_block(repo_path)
    changelog_block = unreleased or "(parser failure: [Unreleased] missing or malformed)"

    prompt = config.build_release_verify_prompt(
        commit_messages=commit_messages,
        changelog_unreleased=changelog_block,
        diff_stat=diff_stat,
        diff_payload=diff_payload,
        truncation_note=truncation_note,
    )

    result = await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.QUERY,
        prompt=prompt,
        mode="explore",
        origin=InstanceOrigin.VERIFY_RELEASE,
        status_text="Verifying release claims...",
        resume_session=True,
        copy_branch=True,
        silent=True,
    ), source_msg_id=source_msg_id)

    if not result:
        return None
    if result.status != InstanceStatus.COMPLETED:
        return result

    # The LLM sees the full diff and is the authoritative judge. Only fail
    # closed when its output itself is unreadable — a local CHANGELOG regex
    # miss is a tooling artifact, not a release signal.
    verifier_parse_failed = False

    verdict_data: dict | None = None
    if result.result_file and Path(result.result_file).exists():
        try:
            text = Path(result.result_file).read_text(encoding="utf-8")
        except Exception:
            text = ""
        verdict_data = _parse_verifier_json(text)
    if verdict_data is None:
        verifier_parse_failed = True
        verdict_data = {
            "verdict": "mismatch",
            "phantom_bullets": [],
            "missing_bullets": [],
            "needs_inspection": [],
            "rationale": "verifier output was unparseable — failing closed",
        }
    elif unreleased is None:
        # Local parser regressed but LLM verdict is intact — keep diagnostic
        # signal so a real regex bug doesn't hide forever.
        log.warning(
            "verify_release: local CHANGELOG parser returned None; "
            "trusting LLM verdict %s",
            verdict_data.get("verdict"),
        )

    raw_phantoms = verdict_data.get("phantom_bullets")
    # Be tolerant of a verifier that returns a non-list (e.g. a string) —
    # iterating a string would otherwise treat each character as a phantom.
    if not isinstance(raw_phantoms, list):
        raw_phantoms = []
    phantoms = [str(x) for x in raw_phantoms]
    rationale = str(verdict_data.get("rationale") or "")

    real_mismatch = (
        verdict_data.get("verdict") == "mismatch" and bool(phantoms)
    )
    is_mismatch = verifier_parse_failed or real_mismatch

    if is_mismatch:
        result.needs_input = True
        ctx.store.update_instance(result)

        # Discord's text-message ceiling is 2000 chars. Cap each bullet
        # individually so one runaway claim doesn't push the whole body
        # past the limit and break the gate UI.
        def _cap(s: str, n: int) -> str:
            return s if len(s) <= n else s[:n - 1] + "…"

        capped_phantoms = [_cap(b, 200) for b in phantoms[:10]]
        more = len(phantoms) - len(capped_phantoms)

        if capped_phantoms:
            bullets_text = "\n".join(f"• {b}" for b in capped_phantoms)
            if more > 0:
                bullets_text += f"\n• …and {more} more"
            body = (
                f"⚠️ **Release verifier flagged a mismatch.**\n\n"
                f"**Phantom claims:**\n{bullets_text}\n\n"
                f"**Rationale:** {_cap(rationale, 400)}\n\n"
                f"Tap **Amend** to re-run the wrap-up step with this feedback, "
                f"or **Continue anyway** to proceed to release."
            )
        else:
            # verifier_parse_failed path: no phantoms to enumerate, just
            # tell the user the gate is fail-closed and why.
            body = (
                f"⚠️ **Release verifier output couldn't be parsed — failing closed.**\n\n"
                f"**Rationale:** {_cap(rationale, 400)}\n\n"
                f"Tap **Amend** to retry the wrap-up step, "
                f"or **Continue anyway** to proceed to release."
            )
        try:
            await ctx.messenger.send_text(
                ctx.channel_id, body,
                buttons=[[
                    ButtonSpec("Amend", f"amend:{result.id}"),
                    ButtonSpec("Continue anyway", f"continue_anyway:{result.id}"),
                ]],
                silent=False,
            )
        except Exception:
            log.exception("verify_release: failed to post Amend/Continue UI")

    return result


async def on_amend_done(
    ctx: RequestContext,
    verify_release_instance_id: str,
    source_msg_id: str | None = None,
) -> Instance | None:
    """Amend button: re-run chain `done` with verifier feedback, then re-verify.

    The button is wired to the verify_release Instance, so we read its
    rationale (from the result file) and prepend it as `extra_context` to
    the next on_done call. After done finishes we run on_verify_release
    again with a fresh entry SHA.
    """
    vr = ctx.store.get_instance(verify_release_instance_id)
    if not vr:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return None
    if not vr.session_id:
        await ctx.messenger.send_text(
            ctx.channel_id,
            "No session attached to this verify-release run — cannot amend.",
        )
        return None
    if not ctx.store.get_autopilot_chain(vr.session_id):
        await ctx.messenger.send_text(
            ctx.channel_id, "No paused chain to amend.",
        )
        return None

    # Pull the verifier verdict from the result file (best-effort).
    verdict: dict | None = None
    if vr.result_file and Path(vr.result_file).exists():
        try:
            text = Path(vr.result_file).read_text(encoding="utf-8")
        except Exception:
            text = ""
        verdict = _parse_verifier_json(text)

    if verdict:
        raw_phantoms = verdict.get("phantom_bullets")
        if not isinstance(raw_phantoms, list):
            raw_phantoms = []
        # Cap each bullet to keep the amend prompt focused (Claude can
        # always reread the full verifier output if needed).
        capped = [str(b)[:300] for b in raw_phantoms[:20]]
        phantom_text = "\n".join(f"- {b}" for b in capped) or "(see rationale)"
        verdict_rationale = (str(verdict.get("rationale") or "(none)"))[:600]
    else:
        # Couldn't parse the verifier output — still need to instruct Claude
        # what to fix. Generic message + ask Claude to inspect on its own.
        phantom_text = (
            "(verifier output couldn't be parsed — inspect the prior commit "
            "and CHANGELOG manually for unsubstantiated claims)"
        )
        verdict_rationale = "verifier output was unparseable"

    rationale = (
        "AMEND MODE — the prior `done` step already created a commit, "
        "but the release verifier flagged a problem with its commit message "
        "and CHANGELOG. Override step 1 of the prompt below: do NOT create a "
        "new commit. Instead:\n"
        "  1. Edit CHANGELOG.md to remove or correct the phantom bullets in "
        "the [Unreleased] section.\n"
        "  2. If a phantom claim is real but unimplemented, implement the "
        "code change.\n"
        "  3. Stage CHANGELOG.md (and any new code) and run "
        "`git commit --amend` with a corrected commit message body — do not "
        "create a duplicate commit.\n"
        "  4. Then continue with steps 2-5 of the prompt below (verify "
        "CHANGELOG, no version bump, no tag, no leftover uncommitted "
        "changes).\n\n"
        f"Phantom claims:\n{phantom_text}\n\n"
        f"Verifier rationale: {verdict_rationale}"
    )

    # Clear needs_input so the chain dispatcher doesn't re-block on the
    # same instance. Do NOT re-snapshot entry SHA — the original snapshot
    # was taken before the *first* `done` ran, and it's the correct anchor
    # for diffing the union of original-commit + amend-commit. Re-snapshotting
    # would set entry_sha to the post-original-done HEAD, after which
    # verify_release would only see the amend's delta (often empty when
    # `git commit --amend` rewrites the message without touching files).
    vr.needs_input = False
    ctx.store.update_instance(vr)

    done_result = await on_done(
        ctx, verify_release_instance_id, source_msg_id,
        prompt_variant="chain", extra_context=rationale or None,
    )
    if (not done_result
            or done_result.status != InstanceStatus.COMPLETED
            or done_result.needs_input):
        # Done failed, errored, or asked a question — surface the result
        # and stop. Chain remains paused; user can reply or click again.
        return done_result

    entry_sha = ctx.store.get_chain_entry_sha(vr.session_id)
    verify_result = await on_verify_release(
        ctx, done_result.id, _last_msg_id(done_result, ctx.platform),
        entry_sha=entry_sha,
    )

    # If verify still fails, on_verify_release already re-posted the gate UI.
    # If it passed (or was skipped), auto-resume the chain at the next step.
    if verify_result is None or (
        verify_result.status == InstanceStatus.COMPLETED
        and not verify_result.needs_input
    ):
        next_source_id = (verify_result or done_result).id
        next_msg_id = _last_msg_id(verify_result or done_result, ctx.platform)
        await resume_autopilot_chain(
            ctx, next_source_id, next_msg_id, vr.session_id,
        )
    return verify_result


async def on_continue_anyway(
    ctx: RequestContext,
    verify_release_instance_id: str,
    source_msg_id: str | None = None,
) -> Instance | None:
    """Continue Anyway button: clear needs_input + resume chain at next step."""
    vr = ctx.store.get_instance(verify_release_instance_id)
    if not vr:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return None
    vr.needs_input = False
    ctx.store.update_instance(vr)
    resumed = await resume_autopilot_chain(
        ctx, verify_release_instance_id, source_msg_id, vr.session_id,
    )
    if resumed is None:
        await ctx.messenger.send_text(
            ctx.channel_id, "No paused chain to continue.",
        )
    return resumed


async def on_release_chain(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None = None,
) -> Instance | None:
    """Run the autopilot `release` chain step — cuts version, updates files, tags.

    Reuses RELEASE_PROMPT (the same prompt /release uses) so behavior matches
    the manual command. Defaults to `patch` bump.
    """
    prompt = config.RELEASE_PROMPT.format(version_hint="patch")
    return await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.TASK,
        prompt=prompt,
        mode="build",
        origin=InstanceOrigin.RELEASE,
        status_text="Cutting release...",
        resume_session=True,
        copy_branch=True,
        silent=True,
    ), source_msg_id=source_msg_id)


def clear_stale_branches(store, branch_name: str) -> int:
    """Clear branch/worktree_path on ALL instances sharing a branch name.

    Also nulls the branch field in history.jsonl so resumed sessions don't
    see stale branch refs in their system prompt.

    Returns the number of instances updated.
    """
    count = 0
    for inst in store.list_instances(all_=True):
        if inst.branch == branch_name:
            inst.branch = None
            inst.worktree_path = None
            store.update_instance(inst)
            count += 1
    try:
        from bot.store import history as history_mod
        history_mod.clear_branch(branch_name)
    except Exception:
        pass
    return count


def _eval_chain_safe(
    store, root_id: str, steps_expected: list[str],
    steps_completed: list[str], instances: list[Instance],
    outcome: str, intervention: bool = False,
) -> None:
    """Run chain evaluation, never raising."""
    try:
        from bot.engine.eval import evaluate_chain
        evaluate_chain(store, root_id, steps_expected, steps_completed,
                       instances, outcome, intervention=intervention)
    except Exception:
        log.debug("Chain eval failed for %s", root_id, exc_info=True)


async def _run_autopilot_chain(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None,
    steps: list[str],
    session_id: str | None,
    cost_budget_usd: float | None = None,
) -> Instance | None:
    """Execute a sequence of autopilot steps. Pauses on failure/needs_input."""
    current_id = source_id
    current_msg = source_msg_id
    result: Instance | None = None

    # Recover deferred revisions from prior run (survives reboot)
    chain_deferred = ctx.store.get_chain_deferred(session_id)

    # Track chain progress for eval
    chain_instances: list[Instance] = []
    completed_steps: list[str] = []

    # Keep a chain-level task active so reboot waits for the entire chain,
    # not just individual steps (which have gaps between them).
    chain_task_id = f"chain:{source_id}"
    # No session_id here — chain task is for reboot idle tracking only.
    # Individual steps register their session via lifecycle.run_instance.
    # Passing session_id would block spawn_from's check_spawn_allowed guard.
    ctx.runner.begin_task(chain_task_id)
    try:
        for step in steps:
            # Update remaining steps in session state for resume
            remaining = steps[steps.index(step):]
            ctx.store.set_autopilot_chain(session_id, remaining)

            # Cost budget guard (used by auto-fix sessions)
            if cost_budget_usd is not None and chain_instances:
                chain_cost = sum(i.cost_usd or 0 for i in chain_instances)
                if chain_cost >= cost_budget_usd:
                    await ctx.messenger.send_text(
                        ctx.channel_id,
                        f"⚠️ Cost budget exhausted (${chain_cost:.2f} / ${cost_budget_usd:.2f}). Pausing chain.",
                        silent=True,
                    )
                    await _exit_chain_needs_input(
                        ctx, source_id, session_id, steps, completed_steps,
                        chain_instances, result, "budget_exhausted",
                    )
                    return result

            if step == "review_loop":
                result = await _review_plan_loop(ctx, current_id, current_msg)
                # Post deferred revisions summary if any
                if result and result.status == InstanceStatus.COMPLETED and result.deferred_revisions:
                    chain_deferred = result.deferred_revisions
                    ctx.store.set_chain_deferred(session_id, chain_deferred)
                    deferred_text = "\n".join(f"• {d}" for d in result.deferred_revisions[:10])
                    await ctx.messenger.send_result(
                        ctx.channel_id,
                        f"**Deferred Revisions** (Medium/Low)\n\n{deferred_text}",
                        metadata={"_status": "completed", "_mode": "plan", "_deferred": True},
                        silent=True,
                    )
            elif step == "build":
                source = ctx.store.get_instance(current_id)
                multi_phase_ran = False

                # Phase-plan support requires a session_id (so phase state can
                # persist across reboots and resume-replies). Without one we
                # fall back to the classic single-shot build.
                phase_state: ChainPhaseState | None = None
                if session_id:
                    phase_state = ctx.store.get_chain_phases(session_id)
                    if phase_state is None:
                        discovered = _find_phase_plan(ctx.store, source)
                        if discovered:
                            phase_state = ChainPhaseState(phases=discovered, cursor=0)
                            ctx.store.set_chain_phases(session_id, phase_state)

                if phase_state and phase_state.phases:
                    # --- Multi-phase build loop ---
                    multi_phase_ran = True
                    while not phase_state.is_done():
                        # Resume after a post-phase risk gate: just advance.
                        if phase_state.paused_at == "post":
                            phase_state = ctx.store.advance_chain_phase(session_id)
                            if phase_state is None or phase_state.is_done():
                                break
                            continue

                        phase = phase_state.current()
                        if phase is None:
                            break

                        # Mid-spawn reboot recovery: pre_phase_head set with no
                        # pause means we crashed between capture and completion.
                        # If git HEAD moved, the phase committed — advance.
                        # EXCEPT for risk gates (non-final): if we crashed
                        # between phase completion and persisting paused_at,
                        # silently advancing would skip the human-review gate
                        # the user explicitly asked for. Pause now instead so
                        # the gate's intent is preserved across the crash.
                        if phase_state.paused_at is None and phase_state.pre_phase_head:
                            check_path = phase_state.worktree_path or (source.repo_path if source else None)
                            current_head = await _git_head(check_path)
                            if current_head and current_head != phase_state.pre_phase_head:
                                is_last = phase_state.cursor + 1 >= len(phase_state.phases)
                                if phase.gate == "risk" and not is_last:
                                    log.info(
                                        "Phase %s completed before crash with risk gate — pausing for review",
                                        phase.id,
                                    )
                                    ctx.store.set_phase_pause(session_id, "post")
                                    await _exit_chain(
                                        ctx, source_id, session_id, steps, completed_steps,
                                        chain_instances, None, "phase_gate_risk",
                                        _phase_gate_suffix(phase, "post"),
                                        intervention=True,
                                    )
                                    return result
                                log.info(
                                    "Phase %s appears completed (HEAD %s -> %s) after reboot — advancing",
                                    phase.id, phase_state.pre_phase_head, current_head,
                                )
                                phase_state = ctx.store.advance_chain_phase(session_id)
                                if phase_state is None or phase_state.is_done():
                                    break
                                continue

                        # Pre-phase gate (design = wait for human input first).
                        # Skip if already paused here (we got resumed past it).
                        # Don't append "build" to completed_steps — the step
                        # is partial, not done. Matches the failure-exit pattern
                        # used by the outer chain loop.
                        # Pass result=None to _exit_chain: `result` here is the
                        # PRIOR step's instance (e.g. review_loop), already
                        # appended to chain_instances at the bottom of that
                        # step's iteration; passing it again would double-count
                        # it in _eval_chain_safe.
                        if phase.gate == "design" and phase_state.paused_at != "pre":
                            ctx.store.set_phase_pause(session_id, "pre")
                            await _exit_chain(
                                ctx, source_id, session_id, steps, completed_steps,
                                chain_instances, None, "phase_gate_design",
                                _phase_gate_suffix(phase, "pre"),
                                intervention=True,
                            )
                            return result

                        # 4a: capture HEAD before spawning the phase build.
                        capture_path = phase_state.worktree_path or (source.repo_path if source else None)
                        pre_head = await _git_head(capture_path)
                        ctx.store.set_pre_phase_head(session_id, pre_head)

                        # 4b: spawn the phase. First phase auto-branches; later
                        # phases copy the branch info from the first phase's
                        # build instance so they all land on the same worktree.
                        is_first = phase_state.cursor == 0 and not phase_state.first_build_id
                        spawn_source_id = current_id
                        if not is_first and phase_state.first_build_id:
                            spawn_source_id = phase_state.first_build_id

                        # Use replace (not .format) so titles containing
                        # `{...}` (e.g. "Refactor `{get_user}`") don't crash.
                        phase_prompt = (config.BUILD_PHASE_PROMPT
                                        .replace("{id}", phase.id)
                                        .replace("{title}", phase.title))
                        phase_result = await spawn_from(
                            ctx, spawn_source_id,
                            SpawnConfig(
                                instance_type=InstanceType.TASK,
                                prompt=phase_prompt,
                                mode="build", origin=InstanceOrigin.BUILD,
                                status_text=f"Building phase {phase.id}...",
                                resume_session=True,
                                auto_branch=is_first,
                                copy_branch=not is_first,
                                silent=True,
                            ),
                            source_msg_id=current_msg,
                        )
                        result = phase_result

                        # 4c: atomically record worktree + first build id from
                        # the spawn output. Atomic so a crash between writes
                        # can't leave first_build_id None while worktree_path
                        # is set (which would re-trigger auto_branch on resume).
                        # Mirror the write into the local object so the next
                        # iteration sees the new values even if a re-fetch
                        # would fail (corruption); avoids re-running auto_branch
                        # over an existing worktree.
                        if is_first and phase_result and phase_result.worktree_path:
                            ctx.store.set_phase_spawn_metadata(
                                session_id, phase_result.worktree_path, phase_result.id,
                            )
                            phase_state.worktree_path = phase_result.worktree_path
                            phase_state.first_build_id = phase_result.id

                        # Mid-phase failure / needs_input: pause at "pre" so
                        # resume re-runs this same phase after the user replies.
                        if (not phase_result
                                or phase_result.status != InstanceStatus.COMPLETED
                                or phase_result.needs_input):
                            outcome = (
                                "needs_input"
                                if (phase_result and phase_result.needs_input)
                                else "failed"
                            )
                            ctx.store.set_phase_pause(session_id, "pre")
                            await _exit_chain(
                                ctx, source_id, session_id, steps, completed_steps,
                                chain_instances, phase_result, outcome,
                                f"Phase `{phase.id}` needs your attention.",
                                intervention=True,
                            )
                            return phase_result

                        # Per-phase empty-diff guard: phase finished but git
                        # HEAD didn't move — abandon the chain. This IS terminal,
                        # so completed_steps gets the append (matches the
                        # single-shot empty-diff path further below).
                        post_head = await _git_head(phase_result.worktree_path)
                        if pre_head and post_head and pre_head == post_head:
                            await ctx.messenger.send_text(
                                ctx.channel_id,
                                f"⚠️ Phase `{phase.id}` produced no commits. Halting chain.",
                                silent=True,
                            )
                            # Only the first phase owns the worktree lifecycle —
                            # tear it down so Merge/Discard don't appear empty.
                            if is_first and phase_result.branch:
                                await ctx.runner.discard_branch(phase_result)
                                ctx.store.update_instance(phase_result)
                            completed_steps.append(step)
                            await _exit_chain(
                                ctx, source_id, session_id, steps, completed_steps,
                                chain_instances, phase_result, "abandoned",
                                f"Phase `{phase.id}` had no changes.",
                                clear_state=True,
                            )
                            return phase_result

                        # Post-phase gate (risk = human review before next phase
                        # ships). Skip on the final phase — the rest of the
                        # autopilot chain (review/verify/done) covers final review.
                        # Append phase_result to chain_instances FIRST (eval needs
                        # to see the just-shipped phase) and pass None to
                        # _exit_chain so it doesn't double-append.
                        is_last = phase_state.cursor + 1 >= len(phase_state.phases)
                        if (phase.gate == "risk" and not is_last
                                and phase_state.paused_at != "post"):
                            chain_instances.append(phase_result)
                            ctx.store.set_phase_pause(session_id, "post")
                            await _exit_chain(
                                ctx, source_id, session_id, steps, completed_steps,
                                chain_instances, None, "phase_gate_risk",
                                _phase_gate_suffix(phase, "post"),
                                intervention=True,
                            )
                            return phase_result

                        # Successful, non-gated phase completion.
                        # Skip inline append on the last phase — the end-of-iter
                        # `chain_instances.append(result)` at the bottom of the
                        # outer loop handles it. Appending here too would
                        # double-count the final phase in the chain eval.
                        if not is_last:
                            chain_instances.append(phase_result)
                        current_id = phase_result.id
                        current_msg = _last_msg_id(phase_result, ctx.platform)

                        # Advance — atomic clear of paused_at + pre_phase_head.
                        phase_state = ctx.store.advance_chain_phase(session_id)
                        if phase_state is None:
                            break

                    # All phases done. Clear phase state and fall through to
                    # the next chain step. Safety net: if reboot recovery
                    # advanced past the last phase, `result` may be None even
                    # though the chain actually finished — recover the most
                    # recent build instance from chain_instances or first_build.
                    ctx.store.clear_chain_phases(session_id)
                    if result is None:
                        for inst in reversed(chain_instances):
                            if inst.origin == InstanceOrigin.BUILD:
                                result = inst
                                current_id = inst.id
                                current_msg = _last_msg_id(inst, ctx.platform)
                                break
                        if result is None and phase_state and phase_state.first_build_id:
                            recovered = ctx.store.get_instance(phase_state.first_build_id)
                            if recovered:
                                result = recovered
                                current_id = recovered.id
                                current_msg = _last_msg_id(recovered, ctx.platform)
                else:
                    # --- Single-shot build (no phases declared) ---
                    is_plan = source.plan_active if source else False
                    # Snapshot HEAD before the build so the empty-diff guard
                    # below can compare against the post-build worktree HEAD.
                    # Source is typically a q-* step with no worktree, so we
                    # fall back to repo_path (= worktree's initial HEAD).
                    pre_build_head: str | None = None
                    if source:
                        pre_build_head = await _git_head(
                            source.worktree_path or source.repo_path
                        )
                    result = await spawn_from(ctx, current_id, SpawnConfig(
                        instance_type=InstanceType.TASK,
                        prompt=config.BUILD_FROM_PLAN_PROMPT if is_plan else config.BUILD_FROM_QUERY_PROMPT,
                        mode="build", origin=InstanceOrigin.BUILD,
                        status_text="Building...", resume_session=True,
                        auto_branch=True, silent=True,
                    ), source_msg_id=current_msg)
            elif step == "review_code":
                result = await on_review_code(ctx, current_id, current_msg)
            elif step == "verify":
                result = await on_verify(ctx, current_id, current_msg)
            elif step == "done":
                # Snapshot HEAD before the chain `done` so verify_release has
                # a stable diff anchor (entry_sha..HEAD).
                src_for_sha = ctx.store.get_instance(current_id)
                repo_path = _resolve_chain_repo_path(src_for_sha)
                if repo_path:
                    head_sha = _git_head_sha(repo_path)
                    if head_sha:
                        ctx.store.set_chain_entry_sha(session_id, head_sha)
                result = await on_done(
                    ctx, current_id, current_msg, prompt_variant="chain",
                )
            elif step == "verify_release":
                entry_sha = ctx.store.get_chain_entry_sha(session_id)
                src_for_skip = ctx.store.get_instance(current_id)
                skip_repo = _resolve_chain_repo_path(src_for_skip)
                skip_head = _git_head_sha(skip_repo) if skip_repo else None

                # Skip pre-spawn so budget/spawn failures aren't conflated
                # with the legitimate "nothing to verify" case below.
                if not entry_sha:
                    log.warning(
                        "verify_release: no entry SHA — skipping gate (chain %s)",
                        session_id,
                    )
                    try:
                        await ctx.messenger.send_text(
                            ctx.channel_id,
                            "ℹ️ Release verifier skipped — no diff anchor "
                            "available. Proceeding without claim verification.",
                            silent=True,
                        )
                    except Exception:
                        pass
                    completed_steps.append(step)
                    continue
                if skip_head and skip_head == entry_sha:
                    log.info("verify_release: entry == HEAD, no commits to verify")
                    completed_steps.append(step)
                    continue

                result = await on_verify_release(
                    ctx, current_id, current_msg, entry_sha=entry_sha,
                )
                # From here, None means spawn failed — treated as chain
                # failure by the post-step status guard below.
            elif step == "release":
                # Cut a release if [Unreleased] has entries; the prompt itself
                # aborts cleanly when the section is empty (no harm done).
                result = await on_release_chain(ctx, current_id, current_msg)
            elif step == "merge":
                # Merge step: git operations only, no Claude instance.
                # On resume after restart, result may be None — look up the
                # done instance to merge.
                merge_target = None
                if result and result.branch and result.original_branch:
                    merge_target = result
                else:
                    merge_target = _find_mergeable_instance(ctx.store, session_id)

                if merge_target and merge_target.branch and merge_target.original_branch:
                    # close_silent=False: chain-completion close should ping
                    # the user so they see the autopilot finished.
                    merged_ok = await _finalize_merge(
                        ctx, merge_target, close_silent=False,
                    )
                    if not merged_ok:
                        await ctx.messenger.send_text(
                            ctx.channel_id,
                            "⚠️ Auto-merge failed. Use /merge to resolve.",
                            silent=True,
                        )
                        completed_steps.append(step)
                        await _exit_chain_needs_input(
                            ctx, source_id, session_id, steps, completed_steps,
                            chain_instances, None, "merge_failed",
                        )
                        return result
                completed_steps.append(step)
                # Break unconditionally — merge is always the final step.
                # This avoids the status guard running on a non-Instance result.
                break

            if not result or result.status != InstanceStatus.COMPLETED or result.needs_input:
                # Chain paused/failed/question — notify user, state saved for resume
                if result and result.needs_input and step == "verify_release":
                    outcome = "phantom_detected"
                elif result and result.needs_input:
                    outcome = "needs_input"
                else:
                    outcome = "failed"

                # Release-step partial-failure recovery hint: a half-done
                # release (commit landed but tag failed) leaves the worktree
                # in a state that's hard to debug from the chain failure
                # embed alone. Surface a one-liner before exiting.
                if step == "release" and outcome == "failed":
                    try:
                        await ctx.messenger.send_text(
                            ctx.channel_id,
                            "ℹ️ Release step halted mid-sequence. Run "
                            "`git log --oneline -3` in the worktree to "
                            "inspect; if a release commit exists without a "
                            "tag, retry the release step manually or "
                            "`git reset --soft HEAD~1` first.",
                            silent=True,
                        )
                    except Exception:
                        pass

                await _exit_chain_needs_input(
                    ctx, source_id, session_id, steps, completed_steps,
                    chain_instances, result, outcome,
                )
                return result

            # Guard: build produced no commits — halt chain.
            # HEAD movement is authoritative; the prior `code_active` check
            # gave false positives because that flag inherits from session
            # siblings (lifecycle.py:441) — a build that wrote nothing in a
            # session that previously made edits would still report True.
            # Skipped for multi-phase: each phase ran its own per-phase
            # HEAD-movement check at line ~1867.
            if step == "build" and result and not multi_phase_ran:
                post_build_head = await _git_head(
                    result.worktree_path or result.repo_path
                )
                no_changes = bool(
                    pre_build_head and post_build_head
                    and pre_build_head == post_build_head
                )
                if no_changes:
                    try:
                        await ctx.messenger.send_text(
                            ctx.channel_id,
                            "⚠️ Build produced no commits. Halting chain.",
                            silent=True,
                        )
                    except Exception:
                        log.exception(
                            "Empty-diff halt notice failed to send for %s",
                            ctx.channel_id,
                        )
                    # Clean up empty branch/worktree so Merge/Discard don't appear
                    if result.branch:
                        await ctx.runner.discard_branch(result)
                        ctx.store.update_instance(result)
                    completed_steps.append(step)
                    await _exit_chain_needs_input(
                        ctx, source_id, session_id, steps, completed_steps,
                        chain_instances, result, "abandoned",
                    )
                    return result
            chain_instances.append(result)
            completed_steps.append(step)
            current_id = result.id
            current_msg = _last_msg_id(result, ctx.platform)

        # Chain complete — carry deferred revisions to final result
        if result and chain_deferred and not result.deferred_revisions:
            result.deferred_revisions = chain_deferred
            ctx.store.update_instance(result)

        # Persist deferred revisions to per-repo backlog
        if chain_deferred and result and result.repo_name:
            original = ctx.store.get_instance(source_id)
            topic = original.prompt[:60] if original and original.prompt else ""
            ctx.store.append_deferred(
                result.repo_name, chain_deferred,
                thread_id=result.id, topic=topic,
            )

        # Evaluate the completed chain
        outcome = "merged" if "merge" in completed_steps else "completed"
        _eval_chain_safe(ctx.store, source_id, steps, completed_steps,
                         chain_instances, outcome)

        # Terminal-state handoff: if any chain instance is flagged for manual
        # verification, surface the WHY lines so the user knows what to eyeball.
        # Sent silently — the colored Verify Board is the loud signal; this
        # message is just contextual breadcrumb pointing at it.
        manual_items = [
            i for i in chain_instances if i.needs_manual_verification
        ]
        if manual_items:
            why_lines = "\n".join(
                f"• {i.manual_verify_reason}"
                for i in manual_items if i.manual_verify_reason
            )
            repo_label = result.repo_name if result and result.repo_name else "this repo"
            try:
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"ℹ️ Pending manual verification — items "
                    f"posted to Verify Board for {repo_label}:\n{why_lines}",
                    silent=True,
                )
            except Exception:
                log.debug(
                    "Manual-verify handoff message failed for %s",
                    ctx.channel_id, exc_info=True,
                )

        # Chain finished without a merge step — prompt the user to test
        # the worktree and decide. Wrapped in try/except so a transient
        # Discord failure never leaves the chain state stuck in the store.
        if "merge" not in steps and result and result.branch and result.original_branch:
            try:
                iid = result.id
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"\U0001f6ec Chain complete on branch `{result.branch}`.\n"
                    f"Test the worktree, then merge or discard.",
                    buttons=[[
                        ButtonSpec("Merge", f"merge:{iid}"),
                        ButtonSpec("Discard", f"discard:{iid}"),
                        ButtonSpec("Diff", f"diff:{iid}"),
                    ]],
                    silent=False,
                )
            except Exception:
                log.exception("Hold chain handoff message failed to send")

        ctx.store.clear_autopilot_chain(session_id)
        ctx.store.clear_chain_deferred(session_id)
        ctx.store.clear_chain_entry_sha(session_id)
        return result
    finally:
        ctx.runner.end_task(chain_task_id)


async def resume_autopilot_chain(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None,
    session_id: str | None,
) -> Instance | None:
    """Resume a paused autopilot chain from stored state.

    For chains with >=2 remaining steps, skip the first (the answered step)
    and run the rest. For a single remaining step, re-run it (the user's
    answer feeds into the respawn).

    Phase exception: if the chain is paused mid-build at a phase boundary
    (`chain_phases` exists, next step is "build"), do NOT skip — the build
    loop reads `paused_at` from `chain_phases` to decide which phase to run
    next. Skipping would jump past the build entirely.

    Returns None only when no chain exists.
    """
    chain = ctx.store.get_autopilot_chain(session_id)
    if not chain:
        return None
    phase_state = ctx.store.get_chain_phases(session_id) if session_id else None
    if phase_state and phase_state.phases and chain and chain[0] == "build":
        remaining = chain
    else:
        remaining = chain[1:] if len(chain) >= 2 else chain
    return await _run_autopilot_chain(
        ctx, source_id, source_msg_id, remaining, session_id,
    )


async def _launch_chain(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None,
    steps: list[str],
    start_from: str,
    cost_budget_usd: float | None = None,
) -> Instance | None:
    """Shared setup for chain entry points: look up source, slice steps, dispatch."""
    source = ctx.store.get_instance(source_id)
    if not source:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return None

    try:
        idx = steps.index(start_from)
    except ValueError:
        idx = 0
    return await _run_autopilot_chain(
        ctx, source_id, source_msg_id,
        steps[idx:], source.session_id,
        cost_budget_usd=cost_budget_usd,
    )


async def on_autopilot(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None = None,
    start_from: str = "review_loop",
    cost_budget_usd: float | None = None,
) -> Instance | None:
    """Full autopilot: Review Plan loop → Build → Review Code → Verify → Done → Merge."""
    return await _launch_chain(
        ctx, source_id, source_msg_id,
        _AUTOPILOT_STEPS, start_from, cost_budget_usd=cost_budget_usd,
    )


async def on_build_and_ship(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None = None,
    start_from: str = "build",
) -> Instance | None:
    """Build → Review Code → Verify → Done → Merge."""
    return await _launch_chain(
        ctx, source_id, source_msg_id,
        _BUILD_AND_SHIP_STEPS, start_from,
    )


async def on_autopilot_hold(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None = None,
    start_from: str = "review_loop",
) -> Instance | None:
    """Autopilot that stops before merge — leaves Merge/Discard for manual review."""
    return await _launch_chain(
        ctx, source_id, source_msg_id,
        _AUTOPILOT_HOLD_STEPS, start_from,
    )


# --- Session resume ---

async def on_sess_resume(
    ctx: RequestContext,
    session_id: str,
    source_msg_id: str | None = None,
) -> None:
    """Resume a desktop CLI session."""
    ctx.store.active_session_id = session_id

    # Strip buttons from session list message
    if source_msg_id:
        try:
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, None)
        except Exception:
            pass

    # Load recent messages and show last 2 user+assistant pairs
    fpath = await asyncio.to_thread(sessions_mod.find_session_file, session_id)
    if fpath:
        all_msgs = await asyncio.to_thread(sessions_mod.read_session_messages, fpath, 999)

        pairs = []
        i = len(all_msgs) - 1
        while i >= 0 and len(pairs) < 2:
            if all_msgs[i]["role"] == "assistant":
                assistant_msg = all_msgs[i]
                j = i - 1
                while j >= 0 and all_msgs[j]["role"] != "user":
                    j -= 1
                if j >= 0:
                    pairs.append((all_msgs[j], assistant_msg))
                else:
                    pairs.append((None, assistant_msg))
                i = j - 1
            else:
                i -= 1

        pairs.reverse()

        for user_msg, asst_msg in pairs:
            if user_msg:
                text = user_msg["text"]
                if len(text) > 400:
                    text = text[:400] + "…"
                markup = ctx.messenger.markdown_to_markup(f"**You:**\n{text}")
                try:
                    await ctx.messenger.send_text(ctx.channel_id, markup, silent=True)
                except Exception:
                    await ctx.messenger.send_text(ctx.channel_id, f"You:\n{text[:400]}", silent=True)

            text = asst_msg["text"]
            if len(text) > 400:
                text = text[:400] + "…"
            markup = ctx.messenger.markdown_to_markup(f"**Claude:**\n{text}")
            try:
                await ctx.messenger.send_text(ctx.channel_id, markup, silent=True)
            except Exception:
                await ctx.messenger.send_text(ctx.channel_id, f"Claude:\n{text[:400]}", silent=True)

    await ctx.messenger.send_text(
        ctx.channel_id, "✅ Session resumed. Send a message to continue.", silent=True,
    )
