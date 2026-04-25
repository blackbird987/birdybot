"""Spawn logic from button callbacks — plan, build, review, commit, autopilot."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from bot import config
from bot.claude.types import (
    CODE_CHANGE_TOOLS, Instance, InstanceOrigin, InstanceStatus, InstanceType,
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
    await _notify_user(ctx, suffix)


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
    r'^\s*RESULT:\s*(pass|fail|skip)\s*$', re.IGNORECASE | re.MULTILINE
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


def _verify_passed(inst: Instance) -> bool:
    """Check if verify step reported pass or skip (fail-safe: missing block = fail).

    Treats skip like pass for chain advancement — if verify legitimately had
    nothing to run (docs-only change, library covered by tests, etc.), advance
    the chain rather than block on a false failure.
    """
    if not inst.result_file:
        return False
    try:
        text = Path(inst.result_file).read_text(encoding="utf-8")
    except OSError:
        return False
    # Take the LAST block — if the model narrates/pre-quotes the template
    # before the real output, the final block is the authoritative verdict.
    blocks = _VERIFY_BLOCK_RE.findall(text)
    if not blocks:
        return False
    block = blocks[-1]
    result_m = _VERIFY_RESULT_RE.search(block)
    if not result_m:
        return False
    result = result_m.group(1).lower()
    if result == "skip":
        summary = _VERIFY_SUMMARY_RE.search(block)
        log.info(
            "Verify skipped: %s",
            summary.group(1).strip() if summary else "no reason given",
        )
        return True
    # pass/fail path — log what was actually tested for observability
    actions = _VERIFY_ACTIONS_RE.search(block)
    endpoints = _VERIFY_ENDPOINTS_RE.search(block)
    if actions:
        log.info("Verify actions: %s", actions.group(1).strip())
    if endpoints:
        log.info("Verify endpoints: %s", endpoints.group(1).strip())
    return result == "pass"


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
    """Run verification. Auto-fix loop if tests fail (max 2 rounds).

    Respects verify_policy from .claude/test.json:
      "block"  — halt chain on failure (wait for user)
      "warn"   — flag failure but proceed (default)
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

        passed = _verify_passed(result)
        if passed:
            break

        # Last round — can't fix further
        if round_num >= MAX_FIX_ROUNDS:
            if verify_policy == "warn" and result.status == InstanceStatus.COMPLETED:
                # Instance ran fine but tests reported fail — proceed with warning
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"⚠️ Verification failed after {round_num + 1} rounds — proceeding anyway. Check results.",
                    silent=True,
                )
                break
            # "block" policy or genuine instance failure — halt the chain.
            # Set needs_input so the chain guard pauses and pings the user.
            if result.status == InstanceStatus.COMPLETED:
                result.needs_input = True
                ctx.store.update_instance(result)
            break

        # Tests failed — Claude already tried to fix inline. Re-verify.
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
            await ctx.messenger.close_conversation(ctx.channel_id, skip_mention=True)
        except Exception:
            log.debug("Failed to close conversation for channel %s", ctx.channel_id, exc_info=True)

    return result


# --- Autopilot chains ---

_AUTOPILOT_STEPS = ["review_loop", "build", "review_code", "verify", "done", "merge"]
_BUILD_AND_SHIP_STEPS = ["build", "review_code", "verify", "done", "merge"]
_AUTOPILOT_HOLD_STEPS = ["review_loop", "build", "review_code", "verify", "done"]


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
                    await _exit_chain(
                        ctx, source_id, session_id, steps, completed_steps,
                        chain_instances, result, "budget_exhausted",
                        "Cost budget reached.",
                        clear_state=True,
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
            elif step == "verify":
                result = await on_verify(ctx, current_id, current_msg)
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
                        from bot.engine.deploy import update_after_merge, rescan_deploy_config_after_merge
                        update_after_merge(ctx.store, merge_target)
                        rescan_deploy_config_after_merge(ctx.store, merge_target.repo_name, merge_target.repo_path)
                        await ctx.messenger.on_deploy_state_changed(merge_target.repo_name)
                        # Apply "merged" tag before close (tag must land before archive)
                        if ctx.on_merged:
                            await ctx.on_merged()
                        await ctx.messenger.send_text(
                            ctx.channel_id, f"✅ {merge_msg}", silent=True,
                        )
                        try:
                            # Don't skip mention — this is the user's notification
                            # that the autopilot chain completed successfully.
                            await ctx.messenger.close_conversation(ctx.channel_id)
                        except Exception:
                            log.debug("Failed to close conversation after autopilot merge")
                    else:
                        await ctx.messenger.send_text(
                            ctx.channel_id,
                            f"⚠️ Auto-merge failed: {merge_msg}\nUse /merge to resolve.",
                            silent=True,
                        )
                        completed_steps.append(step)
                        await _exit_chain(
                            ctx, source_id, session_id, steps, completed_steps,
                            chain_instances, None, "merge_failed",
                            "Merge failed — needs resolution.",
                            clear_state=True,
                        )
                        return result
                completed_steps.append(step)
                # Break unconditionally — merge is always the final step.
                # This avoids the status guard running on a non-Instance result.
                break

            if not result or result.status != InstanceStatus.COMPLETED or result.needs_input:
                # Chain paused/failed/question — notify user, state saved for resume
                outcome = "needs_input" if (result and result.needs_input) else "failed"
                await _exit_chain(
                    ctx, source_id, session_id, steps, completed_steps,
                    chain_instances, result, outcome,
                    "Needs your attention.",
                    intervention=True,
                )
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
                completed_steps.append(step)
                await _exit_chain(
                    ctx, source_id, session_id, steps, completed_steps,
                    chain_instances, result, "abandoned",
                    "Build had no changes.",
                    clear_state=True,
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
    answer feeds into the respawn). Returns None only when no chain exists.
    """
    chain = ctx.store.get_autopilot_chain(session_id)
    if not chain:
        return None
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
