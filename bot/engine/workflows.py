"""Spawn logic from button callbacks — plan, build, review, commit, autopilot."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from bot import config
from bot.claude.types import (
    CODE_CHANGE_TOOLS, Instance, InstanceOrigin, InstanceStatus, InstanceType,
)
from bot.engine import lifecycle, sessions as sessions_mod
from bot.platform.base import RequestContext
from bot.platform.formatting import running_button_specs

log = logging.getLogger(__name__)


async def _notify_user(ctx: RequestContext, suffix: str = "") -> None:
    """Send a @mention to notify the user that the chain needs attention."""
    if not ctx.user_id:
        return
    mention = ctx.messenger.format_mention(ctx.user_id)
    if not mention:
        return
    text = f"{mention} {suffix}" if suffix else mention
    try:
        await ctx.messenger.send_text(ctx.channel_id, text, silent=False)
    except Exception:
        log.debug("Failed to send chain completion mention", exc_info=True)


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

    # Block spawns during reboot drain or if session already has a running task
    check_session = source.session_id if cfg.resume_session else None
    spawn_err = ctx.runner.check_spawn_allowed(check_session)
    if spawn_err:
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

    new_inst = ctx.store.create_instance(
        instance_type=cfg.instance_type,
        prompt=cfg.prompt,
        mode=effective_spawn_mode,
    )
    new_inst.origin = cfg.origin
    new_inst.origin_platform = ctx.platform
    new_inst.effort = ctx.effective_effort
    new_inst.parent_id = source.id
    new_inst.repo_name = source.repo_name
    new_inst.repo_path = source.repo_path
    # Inherit user identity and access control from parent
    new_inst.user_id = source.user_id or (ctx.user_id or "")
    new_inst.user_name = source.user_name or (ctx.user_name or "")
    new_inst.is_owner_session = source.is_owner_session
    new_inst.bash_policy = source.bash_policy
    new_inst.deferred_revisions = source.deferred_revisions

    if cfg.resume_session:
        new_inst.session_id = source.session_id
    if cfg.copy_branch:
        new_inst.branch = source.branch
        new_inst.original_branch = source.original_branch
        new_inst.worktree_path = source.worktree_path
    elif cfg.auto_branch:
        new_inst.branch = f"claude-bot/{new_inst.id}"

    ctx.store.update_instance(new_inst)

    # Strip buttons from source message
    if strip_source_buttons and source_msg_id:
        try:
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, None)
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


async def on_commit(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    return await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.TASK, prompt=config.COMMIT_PROMPT,
        mode="build", origin=InstanceOrigin.COMMIT,
        status_text="Committing...", resume_session=True,
        copy_branch=True, silent=True,
    ), source_msg_id=source_msg_id)


async def on_done(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    """Commit all changes, update changelog, then close the conversation."""
    result = await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.TASK, prompt=config.DONE_PROMPT,
        mode="build", origin=InstanceOrigin.DONE,
        status_text="Wrapping up...", resume_session=True,
        copy_branch=True, silent=True,
    ), source_msg_id=source_msg_id)

    if not result or result.status != InstanceStatus.COMPLETED:
        return result

    # Only close the thread if no branch is pending merge.
    # If a branch exists, the user still needs to click Merge/Discard.
    if not result.branch:
        try:
            await ctx.messenger.close_conversation(ctx.channel_id)
        except Exception:
            log.debug("Failed to close conversation for channel %s", ctx.channel_id, exc_info=True)

    return result


# --- Autopilot chains ---

_AUTOPILOT_STEPS = ["review_loop", "build", "review_code", "done", "merge"]
_BUILD_AND_SHIP_STEPS = ["build", "review_code", "done", "merge"]


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

    # Build review prompt with prior deferred context
    source = ctx.store.get_instance(source_id)
    prior_deferred: list[str] = []
    if source and source.repo_name:
        prior_deferred = ctx.store.get_deferred_items(source.repo_name)

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


def clear_stale_branches(store, branch_name: str) -> int:
    """Clear branch/worktree_path on ALL instances sharing a branch name.

    Returns the number of instances updated.
    """
    count = 0
    for inst in store.list_instances(all_=True):
        if inst.branch == branch_name:
            inst.branch = None
            inst.worktree_path = None
            store.update_instance(inst)
            count += 1
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
                is_plan = source.plan_active if source else False
                result = await spawn_from(ctx, current_id, SpawnConfig(
                    instance_type=InstanceType.TASK,
                    prompt=config.BUILD_FROM_PLAN_PROMPT if is_plan else config.BUILD_FROM_QUERY_PROMPT,
                    mode="build", origin=InstanceOrigin.BUILD,
                    status_text="Building...", resume_session=True,
                    auto_branch=True, silent=True,
                ), source_msg_id=current_msg)
            elif step == "review_code":
                result = await on_review_code(ctx, current_id, current_msg)
            elif step == "done":
                result = await on_done(ctx, current_id, current_msg)
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
                    branch_name = merge_target.branch
                    merge_msg = await ctx.runner.merge_branch(merge_target)
                    ctx.store.update_instance(merge_target)
                    log.info("Autopilot auto-merge: %s", merge_msg)
                    if "failed" not in merge_msg.lower():
                        clear_stale_branches(ctx.store, branch_name)
                        await ctx.messenger.send_text(
                            ctx.channel_id, f"✅ {merge_msg}", silent=True,
                        )
                        try:
                            await ctx.messenger.close_conversation(ctx.channel_id)
                        except Exception:
                            log.debug("Failed to close conversation after autopilot merge")
                    else:
                        await ctx.messenger.send_text(
                            ctx.channel_id,
                            f"⚠️ Auto-merge failed: {merge_msg}\nUse /merge to resolve.",
                            silent=True,
                        )
                        await _notify_user(ctx, "Merge failed — needs resolution.")
                completed_steps.append(step)
                # Break unconditionally — merge is always the final step.
                # This avoids the status guard running on a non-Instance result.
                break

            if not result or result.status != InstanceStatus.COMPLETED or result.needs_input:
                # Chain paused/failed/question — notify user, state saved for resume
                if result:
                    chain_instances.append(result)
                outcome = "needs_input" if (result and result.needs_input) else "failed"
                _eval_chain_safe(ctx.store, source_id, steps, completed_steps,
                                 chain_instances, outcome, intervention=True)
                await _notify_user(ctx, "Needs your attention.")
                return result

            # Guard: build produced no code changes — halt chain
            if step == "build" and result and not result.code_active:
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    "⚠️ Build produced no code changes. Check the plan or retry.",
                    silent=True,
                )
                # Clean up empty branch/worktree so Merge/Discard don't appear
                if result.branch:
                    await ctx.runner.discard_branch(result)
                    ctx.store.update_instance(result)
                ctx.store.clear_autopilot_chain(session_id)
                chain_instances.append(result)
                completed_steps.append(step)
                _eval_chain_safe(ctx.store, source_id, steps, completed_steps,
                                 chain_instances, "abandoned")
                await _notify_user(ctx, "Build had no changes.")
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

        ctx.store.clear_autopilot_chain(session_id)
        ctx.store.clear_chain_deferred(session_id)
        return result
    finally:
        ctx.runner.end_task(chain_task_id)


async def on_autopilot(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None = None,
    start_from: str = "review_loop",
) -> Instance | None:
    """Full autopilot: Review Plan loop → Build → Review Code → Done."""
    source = ctx.store.get_instance(source_id)
    if not source:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return None

    steps = _AUTOPILOT_STEPS
    try:
        idx = steps.index(start_from)
    except ValueError:
        idx = 0
    return await _run_autopilot_chain(
        ctx, source_id, source_msg_id,
        steps[idx:], source.session_id,
    )


async def on_build_and_ship(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None = None,
    start_from: str = "build",
) -> Instance | None:
    """Build → Review Code → Done."""
    source = ctx.store.get_instance(source_id)
    if not source:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return None

    steps = _BUILD_AND_SHIP_STEPS
    try:
        idx = steps.index(start_from)
    except ValueError:
        idx = 0
    return await _run_autopilot_chain(
        ctx, source_id, source_msg_id,
        steps[idx:], source.session_id,
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
