"""Individual workflow steps (plan, build, review, sensors, verify, commit, done), each one button."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from bot import config
from bot.claude.branch_utils import clear_stale_branches
from bot.claude.types import (
    CODE_CHANGE_TOOLS, Instance, InstanceOrigin,
    InstanceStatus, InstanceType, merge_msg_is_failure,
    merge_msg_release_orphaned, merge_msg_repo_unusable,
)
from bot.engine import ai_project, prior_art, sessions as sessions_mod
from bot.platform.base import RequestContext
from bot.platform.formatting import (
    merge_failed_banner,
    merge_failed_button_specs,
)

from bot.engine.chain_git import (
    _find_prior_build_for_chain,
    _find_recoverable_session_predecessor,
    _resolve_chain_repo_path,
)
from bot.engine.step_parse import (
    _extract_latest_plan_text,
    _verify_outcome,
    _verify_why,
)
from bot.engine.step_policy import _load_verify_policy
from bot.engine.step_spawn import (
    SpawnConfig,
    _last_msg_id,
    spawn_from,
)

log = logging.getLogger(__name__)


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
    base_prompt = (
        config.BUILD_FROM_PLAN_PROMPT if is_plan
        else config.BUILD_FROM_QUERY_PROMPT
    )
    # Inject the verbatim plan into the build prompt so a resumed-and-
    # possibly-compacted Claude session implements the actual plan rather
    # than its in-memory recollection. on_build is the manual-button /
    # message-driven path; the autopilot chain runner has its own injection
    # call sites at the single-shot and phase-build branches. See
    # _extract_latest_plan_text for how the plan is recovered when the
    # in-flight chain list is empty (broken-chain recovery).
    plan_text = _extract_latest_plan_text(ctx, [], source_id)
    prompt = (
        config.BUILD_PLAN_INJECTION_PREFIX.replace("{plan_text}", plan_text)
        + base_prompt
        if plan_text else base_prompt
    )

    # Stack on the prior unmerged build in this thread/session if one exists,
    # so successive Builds extend the same branch instead of re-cutting from
    # master each time. Falls back to a fresh branch when there is no prior,
    # or when the prior was already merged/discarded (branch nulled), or
    # when its worktree is no longer git-registered.
    prior = _find_prior_build_for_chain(
        ctx.store, source.session_id, source.repo_path, runner=ctx.runner,
    )
    if not prior:
        # No live prior found — but maybe one is recoverable (worktree dir
        # survived, only git metadata was lost). Don't silently fall through
        # to fresh-master; that's how the t-3700 bug was invisible.
        recoverable = _find_recoverable_session_predecessor(
            ctx.store, source.session_id, source.repo_path,
        )
        if recoverable:
            await _attempt_inline_worktree_recovery(ctx, recoverable)
            # Re-run the chain lookup — recovery may have made the prior
            # live again. If it did, fall through to the chained spawn.
            prior = _find_prior_build_for_chain(
                ctx.store, source.session_id, source.repo_path,
                runner=ctx.runner,
            )

    if prior:
        log.info(
            "Build chaining: source=%s onto prior=%s (branch=%s, worktree=%s)",
            source.id, prior.id, prior.branch, prior.worktree_path,
        )
        return await spawn_from(ctx, source_id, SpawnConfig(
            instance_type=InstanceType.TASK, prompt=prompt,
            mode="build", origin=InstanceOrigin.BUILD,
            status_text="Building...", resume_session=True,
            copy_branch=True, chain_from=prior.id, silent=True,
        ), source_msg_id=source_msg_id)

    return await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.TASK, prompt=prompt,
        mode="build", origin=InstanceOrigin.BUILD,
        status_text="Building...", resume_session=True,
        auto_branch=True, silent=True,
    ), source_msg_id=source_msg_id)


async def _attempt_inline_worktree_recovery(
    ctx: RequestContext, inst: Instance,
) -> None:
    """Try to re-register a session-mate's lost worktree before fresh-master.

    Three outcomes, all surfaced to the thread (silent fallback was the
    failure mode of t-3700):
      - content matches branch tip → re-registered
        (``ClaudeRunner._reregister_worktree_sync``) →
        thread message ``Recovered prior build {branch}``.
      - content drifted → flag ``manual_recovery_needed=True`` →
        thread message ``Prior build {branch} drifted; starting fresh``.
      - error during check → leave instance alone, log, post a skip notice.

    Mutations to the Instance are persisted via store.update_instance.
    """
    runner = ctx.runner
    if runner is None:
        return
    try:
        decision, reason = await asyncio.to_thread(
            runner._check_worktree_content_matches_branch,
            inst.repo_path, inst.worktree_path, inst.branch,
        )
    except Exception as e:
        log.warning(
            "Inline recovery: divergence check raised for %s",
            inst.branch, exc_info=True,
        )
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"Couldn't verify prior build `{inst.branch}` "
                f"({e}); starting fresh from master.",
                silent=True,
            )
        except Exception:
            pass
        return

    if decision == "diverged":
        inst.manual_recovery_needed = True
        inst.manual_recovery_reason = reason
        ctx.store.update_instance(inst)
        log.info(
            "Inline recovery: %s drifted from branch tip — manual review (%s)",
            inst.branch, reason,
        )
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"Prior build `{inst.branch}` lost git metadata AND has "
                f"uncommitted drift ({reason}). Starting this Build fresh "
                f"from master — inspect the prior worktree manually before "
                f"merging anything.",
                silent=True,
            )
        except Exception:
            log.debug("Failed to send drift notice", exc_info=True)
        return

    if decision == "error":
        log.info(
            "Inline recovery: divergence check error for %s: %s",
            inst.branch, reason,
        )
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"Couldn't verify prior build `{inst.branch}` "
                f"({reason}); starting fresh from master.",
                silent=True,
            )
        except Exception:
            pass
        return

    # decision == "match" → re-register under the per-repo lock.  Same helper
    # as the startup pass: `git worktree add --force` was used here too, and
    # git refuses it for a directory that already exists.
    try:
        repo_lock = runner._get_repo_lock(inst.repo_path)
        async with repo_lock:
            failure = await asyncio.to_thread(
                runner._reregister_worktree_sync,
                inst.repo_path, inst.worktree_path, inst.branch,
            )
    except Exception as e:  # the helper never raises; this is the lock/thread
        log.warning(
            "Inline recovery: re-register raised for %s",
            inst.branch, exc_info=True,
        )
        failure = str(e)

    if failure:
        err = failure[:200]
        log.warning(
            "Inline recovery: re-register failed for %s: %s",
            inst.branch, err,
        )
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"Couldn't re-register prior build `{inst.branch}` "
                f"({err}); starting fresh from master.",
                silent=True,
            )
        except Exception:
            pass
        return

    log.info("Inline recovery: re-registered worktree for %s", inst.branch)
    try:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Recovered prior build `{inst.branch}` (lost git metadata, "
            f"content matched branch tip). Build will chain onto it.",
            silent=True,
        )
    except Exception:
        log.debug("Failed to send recovery notice", exc_info=True)


async def _prior_art_prefix(source: Instance | None) -> str:
    """The git history of what `source`'s plan touches, ready to prepend, or "".

    The reviewer runs behind the read-only floor and cannot run git itself,
    so the bot reads it and hands it over, the same way prior deferred items
    are. A worktree shares the main repo's history, so whichever of the two
    still exists gives the same answer. See bot/engine/prior_art.py.
    """
    if not source:
        return ""
    repo_path = _resolve_chain_repo_path(source)
    plan_text = source.read_result_text()
    if not repo_path or not plan_text:
        return ""
    block = await asyncio.to_thread(prior_art.collect, repo_path, plan_text)
    return f"{block}\n\n" if block else ""


async def on_review_plan(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    prefix = await _prior_art_prefix(ctx.store.get_instance(source_id))
    return await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.QUERY, prompt=prefix + config.PLAN_REVIEW_PROMPT,
        mode="explore", origin=InstanceOrigin.REVIEW_PLAN,
        status_text="Reviewing plan...", resume_session=True,
        # Hard floor: review-plan rewrites plan TEXT only — never source files.
        # Without this, mode="explore" only blocks Edit/Write/NotebookEdit and
        # Bash stays open as a write backdoor (sed/echo>/tee>). See t-3792.
        permission_mode="explore",
    ), source_msg_id=source_msg_id)


async def on_apply_revisions(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    return await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.QUERY, prompt=config.APPLY_REVISIONS_PROMPT,
        mode="explore", origin=InstanceOrigin.APPLY_REVISIONS,
        status_text="Applying revisions...", resume_session=True,
        # Hard floor — see on_review_plan.
        permission_mode="explore",
    ), source_msg_id=source_msg_id)


async def on_tldr(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    """Re-explain the current work or proposal in plain language.

    Resumes the session rather than starting fresh: what it explains is the
    conversation, not the code, and a fresh session would have to re-derive
    the "why" from a diff. It never branches and never copies a branch:
    nothing here writes, so there is nothing to isolate.

    The explore floor is the point, not a precaution: an explanation that
    goes and does more work is not an explanation. `permission_mode="explore"`
    also clamps bash_policy to "none", closing the sed/echo write backdoor
    the same way the plan-review steps do.

    `strip_source_buttons=False` is the one place this differs from every
    other button, and it is the difference between a recap and a workflow
    step. The others advance the work, so clearing the card they were tapped
    on is correct. This one does not: the card it sits on is the one carrying
    Merge, Discard, Commit and Done, and asking for a summary must not cost
    the user the buttons they were about to press. Nothing is spawned into
    that card's place, so leaving it intact strands nothing.
    """
    return await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.QUERY, prompt=config.TLDR_PROMPT,
        mode="explore", origin=InstanceOrigin.TLDR,
        status_text="Writing the short version...", resume_session=True,
        permission_mode="explore",
    ), strip_source_buttons=False, source_msg_id=source_msg_id)


async def on_review_code(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    """Review code, auto-looping until no issues remain (max 5 rounds)."""
    MAX_ROUNDS = 5
    current_source = source_id
    current_msg = source_msg_id
    result: Instance | None = None

    # LLM-shaped repos get an extra review lens (evals, output validation,
    # derived-data honesty...). Repos that don't call a model see no change at
    # all — no block, no token cost. Resolved once, not per round.
    review_prompt = config.CODE_REVIEW_PROMPT
    src = ctx.store.get_instance(source_id)
    if src and ai_project.is_llm_project(src.repo_path):
        review_prompt += config.AI_PROJECT_REVIEW_LENS

    for round_num in range(MAX_ROUNDS):
        status = "Reviewing code..." if round_num == 0 else f"Re-reviewing (round {round_num + 1})..."
        result = await spawn_from(ctx, current_source, SpawnConfig(
            instance_type=InstanceType.TASK, prompt=review_prompt,
            mode="build", origin=InstanceOrigin.REVIEW_CODE,
            status_text=status, resume_session=True,
            copy_branch=True, silent=True,
        ), source_msg_id=current_msg)

        if not result:
            break

        # Claude paused to ask the user a question. `needs_input` rides on a
        # status=COMPLETED instance (see lifecycle finalize), so without this
        # guard an "asked a question + edited a file" round reads as "changes
        # made, review again": the loop resumes the waiting session and a later
        # clean round overwrites `result`, dropping the question entirely.
        # Return this instance so the chain guard pauses on needs_input.
        # Mirrors the plan-review loop's guard.
        if result.status != InstanceStatus.COMPLETED or result.needs_input:
            break

        # Clean review (no code changes) — done
        if not (CODE_CHANGE_TOOLS & set(result.tools_used or [])):
            break

        # Changes were made — strip buttons from this result, review again
        current_source = result.id
        current_msg = _last_msg_id(result, ctx.platform)

    return result


# --- Sensors ---


async def on_sensors(
    ctx: RequestContext, source_id: str, source_msg_id: str | None = None,
) -> Instance | None:
    """Run deterministic post-build checks; feed failures back to the session.

    All pass/skip → post a compact summary and return the SOURCE instance
    unchanged (the chain's current_id doesn't move). Failures → resume the
    build session with the raw tool output (max_fix_rounds), re-running the
    sensors after each fix turn. Persistent failure halts under the default
    ``block`` policy (via needs_input, same as verify) or warns+advances
    under ``warn`` (.claude/sensors.json).
    """
    from bot.engine.sensors import load_sensor_config, run_sensors

    inst = ctx.store.get_instance(source_id)
    if not inst:
        return None
    cwd = inst.worktree_path or inst.repo_path
    if not cwd:
        return inst  # nothing to sense; chain continues

    cfg = load_sensor_config(inst.repo_path)
    report = await run_sensors(cwd, inst.repo_path)

    async def _notify(text: str) -> None:
        try:
            await ctx.messenger.send_text(ctx.channel_id, text, silent=True)
        except Exception:
            log.debug("Sensor notice failed for %s", ctx.channel_id, exc_info=True)

    # No sensors detected/configured at all (docs-only repo, unknown stack):
    # skip truly silently — a "none ran" line on every chain is just noise.
    if not report.results:
        return inst

    if report.passed:
        await _notify(report.summary_line())
        return inst

    current_source = source_id
    current_msg = source_msg_id
    result: Instance | None = inst
    for round_num in range(cfg["max_fix_rounds"]):
        result = await spawn_from(ctx, current_source, SpawnConfig(
            instance_type=InstanceType.TASK,
            prompt=config.SENSOR_FIX_PROMPT + "\n\n" + report.failure_text(),
            mode="build",
            origin=InstanceOrigin.SENSOR_FIX,
            status_text=(
                "Fixing sensor failures..." if round_num == 0
                else f"Fixing sensor failures (round {round_num + 1})..."
            ),
            resume_session=True,
            copy_branch=True,
            silent=True,
        ), source_msg_id=current_msg)

        if not result:
            return None

        # Paused with a question or died — let the chain guard handle it
        # (mirrors the review/verify loop guards).
        if result.status != InstanceStatus.COMPLETED or result.needs_input:
            return result

        fix_cwd = result.worktree_path or result.repo_path or cwd
        report = await run_sensors(fix_cwd, inst.repo_path)
        if report.passed:
            await _notify(report.summary_line())
            return result

        current_source = result.id
        current_msg = _last_msg_id(result, ctx.platform)

    # Still failing after all fix rounds.
    tail = report.failure_text()
    if len(tail) > 900:
        tail = "…\n" + tail[-900:]
    if cfg["policy"] == "warn":
        await _notify(
            f"{report.summary_line()} — proceeding under warn policy. "
            f"Latest failures:\n{tail}"
        )
        return result
    # block (default): halt the chain via needs_input, same as verify-fail.
    await _notify(
        f"{report.summary_line()} — halting (sensor failures persist after "
        f"{cfg['max_fix_rounds']} fix rounds):\n{tail}"
    )
    if result and result.status == InstanceStatus.COMPLETED:
        result.needs_input = True
        ctx.store.update_instance(result)
    return result


async def on_verify(ctx: RequestContext, source_id: str, source_msg_id: str | None = None) -> Instance | None:
    """Run verification. Auto-fix loop only on `fail`; other outcomes break.

    Outcomes (`_verify_outcome`):
      pass    — break, chain advances
      skip    — break, chain advances (verify legitimately had nothing to run)
      manual  — verification was impossible ("couldn't verify"). Under `warn`
                or `block_fail`, flag the instance for manual check + advance
                (surfaced in the chain summary). Under `block`, halt with
                needs_input.
      fail    — verify ran and the feature is broken. Re-enter the fix-loop;
                after MAX_FIX_ROUNDS, flag+advance under `warn`, else halt
                (both `block_fail` and `block` halt so a broken feature never
                merges to master).
      crashed — verify environment died (status != COMPLETED). Under `warn` or
                `block_fail`, post a notice in the thread + advance. Under
                `block`, halt.

    Respects verify_policy from .claude/test.json (default: "block_fail").
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

        # Claude paused mid-verify to ask a question. `needs_input` rides on a
        # COMPLETED instance, so _verify_outcome would misclassify it as `fail`
        # and burn fix-loop rounds re-running a session that's actually waiting
        # on the user. Return so the chain guard pauses on needs_input instead.
        if result.needs_input:
            break

        outcome = _verify_outcome(result)

        if outcome in ("pass", "skip"):
            break

        if outcome == "manual":
            why = _verify_why(result) or "verification couldn't run automatically"
            if verify_policy != "block":
                # warn + block_fail: "couldn't verify" ≠ "verified broken",
                # so flag for manual check and advance rather than hard-stop.
                result.needs_manual_verification = True
                result.manual_verify_reason = why
                ctx.store.update_instance(result)
            else:
                # block: refuse to advance on "I couldn't verify"
                result.needs_input = True
                ctx.store.update_instance(result)
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"Verify reported `manual` under block policy — halting. {why}",
                    silent=True,
                )
            break

        if outcome == "crashed":
            why = "verify environment crashed — manual check needed"
            if verify_policy != "block":
                # warn + block_fail: environment death ≠ verified broken.
                # status != COMPLETED so the manual-verify flag can't ride
                # on this instance — a thread notice is the only signal.
                try:
                    await ctx.messenger.send_text(
                        ctx.channel_id,
                        f"Verify environment crashed — proceeding; "
                        f"give this one a manual check. {why}",
                        silent=True,
                    )
                except Exception:
                    log.debug("Crashed-verify notice failed", exc_info=True)
            else:
                # block: halt. The instance already failed so needs_input
                # would not be reached by the chain guard; surface the reason.
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"Verify environment crashed under block policy — halting. {why}",
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
                result.needs_manual_verification = True
                result.manual_verify_reason = why
                ctx.store.update_instance(result)
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"Verification failed after {round_num + 1} rounds — "
                    "proceeding; give this one a manual check.",
                    silent=True,
                )
            else:
                # block_fail / block — a broken feature must not merge to
                # master. Halt the chain via needs_input.
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

    cfg = SpawnConfig(
        instance_type=InstanceType.TASK, prompt=prompt,
        mode="build", origin=InstanceOrigin.DONE,
        status_text="Wrapping up...", resume_session=True,
        copy_branch=True, silent=True,
    )
    if prompt_variant == "standalone":
        source = ctx.store.get_instance(source_id)
        repo_path_for_lock = source.repo_path if source else None
        if repo_path_for_lock:
            async with ctx.runner.release_lock_scope(repo_path_for_lock, "done-standalone"):
                result = await spawn_from(ctx, source_id, cfg, source_msg_id=source_msg_id)
        else:
            result = await spawn_from(ctx, source_id, cfg, source_msg_id=source_msg_id)
    else:
        result = await spawn_from(ctx, source_id, cfg, source_msg_id=source_msg_id)

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
            failure_kind = ctx.runner._last_merge_failure_kind.get(result.id)
            msg = merge_failed_banner(failure_kind)
            # silent=False: this path has no follow-up _exit_chain mention, so
            # the banner post itself must ping or the user never learns the
            # merge failed (the t-4736-class invisible-rot bug).
            lead = "Done hit a merge conflict — needs your attention.\n\n"
            try:
                await ctx.messenger.send_text(
                    ctx.channel_id, lead + msg,
                    buttons=merge_failed_button_specs(result.id),
                    silent=False,
                )
            except Exception:
                log.debug("Failed to send merge-failed hint", exc_info=True)
            ctx.store.set_pending_merge(
                result.id,
                session_id=result.session_id,
                channel_id=ctx.channel_id,
                repo_name=result.repo_name,
                message=msg,
                failure_kind=failure_kind,
            )
        return result

    # No branch to merge — close the thread directly.
    try:
        await ctx.messenger.close_conversation(ctx.channel_id, skip_mention=True)
    except Exception:
        log.debug("Failed to close conversation for channel %s", ctx.channel_id, exc_info=True)

    return result


async def _finalize_merge(
    ctx: RequestContext, merge_target: Instance, *, close_silent: bool = False,
    skip_close: bool = False,
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

    `skip_close`: merge + bookkeeping + tag, but leave the thread open.
    Fleet ship uses this so the post-deploy verify prompt lands in a live
    thread instead of archiving it only to immediately reopen it.
    """
    if not merge_target.branch or not merge_target.original_branch:
        return False
    branch_name = merge_target.branch
    merge_msg = await ctx.runner.merge_branch(merge_target)
    ctx.store.update_instance(merge_target)
    log.info("Auto-merge: %s", merge_msg)
    if merge_msg_is_failure(merge_msg):
        return False
    clear_stale_branches(ctx.store, branch_name)

    if merge_msg_repo_unusable(merge_msg):
        # The branch landed, but cleanup left conflict markers in tracked
        # files — the repo no longer imports and the bot will not survive a
        # restart. Two things must NOT happen here. Deploying would restart
        # into the broken tree; closing the thread would bury the only
        # notice the user gets, which is exactly how the last occurrence
        # went unseen (the warning posted and the thread archived four
        # seconds later). So: no deploy, no close, and a loud ping.
        log.error("Auto-merge left the repo unusable: %s", merge_msg)
        if ctx.on_merged:
            await ctx.on_merged()
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"⛔ **The merge landed, but the repo is now broken — "
            f"do not restart the bot until it is repaired.**\n{merge_msg}",
        )
        return True

    if merge_msg_release_orphaned(merge_msg):
        # The branch landed, but the target it landed on is missing an
        # earlier release. Deploying from here would un-ship that release
        # while the version number climbs, which is precisely the failure
        # nobody notices. So: no deploy, no close, and a loud ping, the
        # same shape as the repo-unusable branch above, for the same reason
        # (a warning posted into a thread that archives seconds later is a
        # warning nobody reads).
        log.error("Auto-merge landed on a tree missing a release: %s", merge_msg)
        if ctx.on_merged:
            await ctx.on_merged()
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"⛔ **The merge landed, but an earlier release is missing from "
            f"it. Do not deploy until it is merged in.**\n{merge_msg}",
        )
        return True

    from bot.engine.deploy import apply_post_merge_deploy
    await apply_post_merge_deploy(ctx.messenger, ctx.store, merge_target)
    # Apply "merged" tag before close (tag must land before archive)
    if ctx.on_merged:
        await ctx.on_merged()
    await ctx.messenger.send_text(
        ctx.channel_id, f"✅ {merge_msg}", silent=True,
    )
    if not skip_close:
        try:
            await ctx.messenger.close_conversation(
                ctx.channel_id, skip_mention=close_silent,
            )
        except Exception:
            log.debug("Failed to close conversation after merge", exc_info=True)
    return True


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
