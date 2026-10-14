"""Autopilot chains: the step loop, the plan review loop, merge finalisation and chain exits."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from bot import config
from bot.claude.types import (
    ChainPhaseState, Instance, InstanceOrigin,
    InstanceStatus, InstanceType,
)
from bot.platform.base import ButtonSpec, RequestContext
from bot.engine.cmd_state import within_dollar_budget

from bot.engine.chain_git import (
    _find_mergeable_instance,
    _git_head,
    _git_head_sha,
    _resolve_chain_repo_path,
)
from bot.engine.release_steps import (
    on_release_chain,
    on_verify_release,
    release_verified,
)
from bot.engine.step_parse import (
    _REVIEW_LOOP_MAX_ROUNDS,
    _extract_deferred,
    _extract_latest_plan_text,
    _extract_triage_deferred,
    _find_phase_plan,
    _needs_revision,
    _parse_verifier_json,
    _phase_gate_suffix,
    _stop_reason_snippet,
    _with_plan,
)
from bot.engine.step_policy import (
    _AUTOPILOT_HOLD_STEPS,
    _AUTOPILOT_STEPS,
    _BUILD_AND_SHIP_STEPS,
    _CHAIN_PRESETS,
)
from bot.engine.step_spawn import (
    SpawnConfig,
    _last_msg_id,
    _notify_user,
    spawn_from,
)
from bot.engine.steps import (
    MergeOutcome,
    _finalize_merge,
    _prior_art_prefix,
    on_done,
    on_review_code,
    on_sensors,
    on_verify,
    record_merge_failure,
)

log = logging.getLogger(__name__)


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
        ctx.store.clear_chain_state(session_id)
    else:
        # Chain queue stays for user-driven resume (needs_input,
        # phantom_detected, phase_gate_risk). Mark it paused so the
        # post-reboot resumer doesn't treat it as crashed-mid-step.
        ctx.store.set_autopilot_chain_status(session_id, "paused")
    await _notify_user(ctx, suffix)


# Mention text per chain-pause outcome — keeps wording consistent and ensures
# the send_text fallback inside _notify_user is never empty.
_CHAIN_EXIT_MESSAGES: dict[str, str] = {
    "needs_input": "Needs your attention.",
    "failed": "Chain blocked — needs your attention.",
    "phantom_detected": (
        "Release verifier flagged phantom claims. "
        "Tap Amend to fix, or Continue to ship anyway."
    ),
    "budget_exhausted": "Cost budget reached.",
    "merge_failed": "Merge conflict — tap Resolve / Retry / Discard.",
    "merge_stopped": (
        "Merged, but stopped before deploy and close. "
        "Read the warning above."
    ),
    "review_did_not_converge": "Plan review didn't converge — needs your attention.",
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
        "budget_exhausted", "merge_failed", "merge_stopped",
        "review_did_not_converge",
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
        intervention = outcome in (
            "needs_input", "phantom_detected", "failed",
            "merge_stopped", "review_did_not_converge",
        )

    await _exit_chain(
        ctx, source_id, session_id, steps, completed_steps,
        chain_instances, result, outcome, suffix,
        clear_state=clear_state, intervention=intervention,
    )


def _build_halted_text(
    *,
    header: str,
    summary: str | None,
    preserved: str | None,
    path_poisoning: list[str] | None = None,
    phase_label: str | None = None,
) -> tuple[str, str]:
    """Build the embed body + mention text for a no-commit halt.

    Returns (halt_embed_text, mention_text). The mention is short enough to
    fit in a Discord ping; the embed body quotes the agent's stopping reason
    when one is available so the user can act on the actual blocker instead
    of a generic "no changes" notice.

    CTA selection is deferred to the trailing block and suppressed when
    `path_poisoning` is set — the poisoning recovery copy says to start a
    fresh build, which directly contradicts "reply to continue".
    """
    reason = _stop_reason_snippet(summary)
    prefix = f"{phase_label} paused" if phase_label else "Build paused"

    if reason:
        quoted = "\n".join(f"> {line}" for line in reason.splitlines())
        body = f"⏸ {header} Here's what the agent said:\n{quoted}"
        first_line = reason.splitlines()[0][:140].rstrip()
        mention = (
            f"{prefix}: {first_line}" if first_line
            else f"{prefix} — needs your input."
        )
    else:
        body = f"⏸ {header}"
        mention = f"{prefix} — needs your input."

    if preserved:
        body += (
            f"\nUncommitted changes preserved on `{preserved}` (WIP commit). "
            f"Recover: `git checkout {preserved}`"
        )

    if path_poisoning:
        sample = path_poisoning[:5]
        more = len(path_poisoning) - len(sample)
        path_lines = "\n".join(f"  • `{p}`" for p in sample)
        if more > 0:
            path_lines += f"\n  • ...and {more} more"
        body += (
            "\n\n**Path poisoning detected** — the build tried to edit the "
            "main repo instead of the worktree. These writes were blocked, "
            "which is why the worktree branch is empty:\n"
            f"{path_lines}\n"
            "Likely cause: the build resumed a planning session whose context "
            "referenced main-repo paths. Start a fresh build (without `--resume` "
            "from the plan session) or re-target the edits manually."
        )
        # Poisoning case: structural fix needed, not a reply. Don't tell the
        # user to "continue" when continuing will re-trip the worktree-guard.
        return body, mention

    cta = "Reply to continue." if reason else "No reason given — reply to continue."
    halt = f"{body}\n\n{cta}" if reason else f"{body} {cta}"
    return halt, mention


async def _attempt_finishup_nudge(
    ctx: RequestContext,
    parent: Instance,
    baseline_head: str | None,
    source_msg_id: str | None,
    phase_label: str | None = None,
) -> tuple[Instance | None, bool]:
    """Spawn one corrective continuation turn for a build whose HEAD didn't move.

    Dominant cause (t-5592): the agent started its test suite with
    run_in_background and ended its turn expecting to be re-invoked — but
    headless -p processes exit with the turn, the background job dies, and
    nothing resumes the session. This resumes the same session in the same
    worktree once, with a self-contained corrective prompt (post-compact-safe:
    the parent typically died near the context ceiling, so the prompt
    re-states worktree, branch, and the parent's own final words instead of
    relying on in-memory history). The nudged instance runs under the same
    step label as the turn it continues — callers adopt it as the step result
    so step-keyed logic, eval, and downstream steps see a normal build turn.

    Returns (nudged, head_moved):
      (None, False)   — no nudge attempted: counter exhausted, session/branch/
                        worktree missing, or the spawn refused/raised. Caller
                        falls through to the rescue/halt path on `parent`.
      (nudged, True)  — nudge completed and HEAD moved off `baseline_head`:
                        caller continues the chain on `nudged`.
      (nudged, False) — a nudge turn ran but didn't converge (killed/failed/
                        needs_input, or completed with HEAD still parked).
                        Caller picks the exit from `nudged.status`.
    Never raises.
    """
    if parent.finishup_nudges >= 1:
        return (None, False)
    if not (parent.session_id and parent.branch and parent.worktree_path
            and Path(parent.worktree_path).is_dir()):
        log.info(
            "Finishup nudge skipped for %s: no resumable session/worktree",
            parent.id,
        )
        return (None, False)
    summary = (parent.summary or "").strip()
    if len(summary) > 600:
        summary = summary[:600].rsplit(" ", 1)[0] + "…"
    # .replace, not .format — summaries routinely contain `{...}`.
    prompt = (
        config.FINISHUP_NUDGE_PROMPT
        .replace("{worktree}", parent.worktree_path)
        .replace("{branch}", parent.branch)
        .replace("{summary}", summary or "(no final message captured)")
    )
    label = phase_label or "build"
    try:
        nudged = await spawn_from(
            ctx, parent.id,
            SpawnConfig(
                instance_type=InstanceType.TASK,
                prompt=prompt,
                mode="build",
                origin=InstanceOrigin.BUILD,
                status_text=f"continuing {label} — finishing tests + commit...",
                resume_session=True,
                copy_branch=True,
                silent=True,
            ),
            source_msg_id=source_msg_id,
        )
    except Exception:
        log.exception(
            "Finishup nudge spawn raised for %s — falling back to halt path",
            parent.id,
        )
        return (None, False)
    if not nudged:
        log.warning(
            "Finishup nudge spawn refused for %s — falling back to halt path",
            parent.id,
        )
        return (None, False)
    nudged.finishup_nudges = parent.finishup_nudges + 1
    ctx.store.update_instance(nudged)
    post_head = await _git_head(nudged.worktree_path or nudged.repo_path)
    head_moved = bool(
        nudged.status == InstanceStatus.COMPLETED
        and not nudged.needs_input
        and baseline_head and post_head and baseline_head != post_head
    )
    log.info(
        "Finishup nudge for %s ran as %s (status=%s, head_moved=%s)",
        parent.id, nudged.id, nudged.status.value, head_moved,
    )
    return (nudged, head_moved)


async def on_conversational_chain(
    ctx: RequestContext,
    source_id: str,
    preset: str,
    source_msg_id: str | None = None,
) -> Instance | None:
    """Launch a chain from a `/chain` directive the session agent emitted.

    ``preset`` is one of ``_CHAIN_PRESETS``. The plan text was already stored
    on the session (set_chain_plan_override) by the directive handler, so the
    build step injects it via ``_extract_latest_plan_text``. Ships quietly on
    the ``ship`` preset (silent close), mirroring Build & Ship.
    """
    steps = _CHAIN_PRESETS.get(preset)
    if steps is None:
        steps = _CHAIN_PRESETS["ship"]
    label = f"Chain ({preset})" if preset in _CHAIN_PRESETS else "Chain"
    return await _launch_chain(
        ctx, source_id, source_msg_id,
        list(steps), "build",
        silent_close=(preset == "ship"),
        chain_label=label,
    )


async def _review_plan_loop(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None,
    chain_instances: list[Instance],
) -> tuple[Instance | None, bool]:
    """Review plan, auto-applying Critical/High revisions. Max 5 rounds.

    Returns (last_review, did_not_converge). did_not_converge=True iff the
    loop hit _REVIEW_LOOP_MAX_ROUNDS without a clean review (the autopilot
    chain treats this as a halt signal — silently falling through to build
    burns a build on a plan the reviewer is still flagging).

    Mutates chain_instances: every successful intermediate APPLY_REVISIONS
    is appended so cost accounting and `_extract_latest_plan_text` can see
    them. The final return value is appended once by the chain caller (no
    duplication — early-failure returns happen before the in-loop append).

    Stores deferred revisions on the returned Instance.
    """
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
    # Read once from the plan as first written, and reused every round: the
    # revised plans touch the same files, and re-reading git per round would
    # only spend time to hand the reviewer the same block again.
    review_prompt = await _prior_art_prefix(source) + review_prompt

    for round_num in range(_REVIEW_LOOP_MAX_ROUNDS):
        status = "Reviewing plan..." if round_num == 0 else f"Re-reviewing plan (round {round_num + 1})..."
        review = await spawn_from(ctx, current_source, SpawnConfig(
            instance_type=InstanceType.QUERY,
            prompt=review_prompt,
            mode="explore",
            origin=InstanceOrigin.REVIEW_PLAN,
            status_text=status,
            resume_session=True,
            # Hard floor — review is plan-text only, no source writes via Bash.
            permission_mode="explore",
        ), source_msg_id=current_msg)

        if not review or review.status != InstanceStatus.COMPLETED:
            return review, False
        if review.needs_input:
            return review, False

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
                    return triage_result, False

                # Triage needs user input — pause chain as normal
                if triage_result and triage_result.needs_input:
                    return triage_result, False

                # Triage hit usage limit — pause chain, let cooldown retry handle it
                if triage_result and triage_result.cooldown_retry_at:
                    return triage_result, False

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
            return review, False

        # Apply only Critical/High
        applied = await spawn_from(ctx, review.id, SpawnConfig(
            instance_type=InstanceType.QUERY,
            prompt=config.APPLY_HIGH_PRIORITY_PROMPT,
            mode="explore",
            origin=InstanceOrigin.APPLY_REVISIONS,
            status_text=f"Applying revisions (round {round_num + 1})...",
            resume_session=True,
            # Hard floor — apply rewrites plan TEXT only.
            permission_mode="explore",
        ), source_msg_id=_last_msg_id(review, ctx.platform))

        if not applied or applied.status != InstanceStatus.COMPLETED:
            return applied, False
        if applied.needs_input:
            return applied, False

        # Surface intermediate apply for downstream consumers — cost accounting
        # at line ~1776 and _extract_latest_plan_text both need to see it. The
        # chain caller appends the loop's final return value separately, so an
        # apply that ends up being the final returned instance is appended by
        # the caller (not here) — early-failure returns above this line ensure
        # we never reach this point on a path that becomes the final return.
        chain_instances.append(applied)

        current_source = applied.id
        current_msg = _last_msg_id(applied, ctx.platform)

    # Max rounds hit — non-convergence signal for the chain caller.
    if last_review:
        last_review.deferred_revisions = _extract_deferred(last_review)
        ctx.store.update_instance(last_review)
    return last_review, True


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

    # Passed: auto-resume the chain at the next step. A mismatch has already
    # re-posted the gate UI from on_verify_release. None means no verifier ran
    # at all, which is "not passed" here exactly as it is in the chain runner:
    # the amend is unverified, so the chain stays paused and the user decides.
    if release_verified(verify_result):
        await resume_autopilot_chain(
            ctx, verify_result.id,
            _last_msg_id(verify_result, ctx.platform), vr.session_id,
        )
    elif verify_result is None:
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                "\u26a0\ufe0f The release verifier did not run after the "
                "amend, so the release is unverified and the chain stays "
                "paused. Tap **Amend** to try again, or **Continue anyway** "
                "to release without it.",
                buttons=[[
                    ButtonSpec("Amend", f"amend:{vr.id}"),
                    # On vr, not done_result: Continue anyway resumes the
                    # chain keyed by that instance's session, which is vr's.
                    ButtonSpec("Continue anyway", f"continue_anyway:{vr.id}"),
                ]],
                silent=False,
            )
        except Exception:
            log.exception("amend: failed to post the unverified-release notice")
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
    *,
    silent_close: bool = False,
    chain_label: str = "Autopilot",
) -> Instance | None:
    """Execute a sequence of autopilot steps. Pauses on failure/needs_input.

    silent_close — when True, the merge step's close-conversation handshake
    skips the participant mention (Build & Ship: ship quietly on success).
    Default False matches loud-close autopilot behavior.

    chain_label — human-readable name used to prefix outcome-varied failure
    messages (e.g. "Build & Ship blocked at review").
    """
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
    # Individual steps run through lifecycle.run_instance, which deliberately
    # does NOT bind their session onto the thread (a step's session belongs to
    # the step, not the conversation — see "A thread must always know its
    # session" in CLAUDE.md).
    ctx.runner.begin_task(chain_task_id)
    try:
        for step in steps:
            # Update remaining steps in session state for resume
            remaining = steps[steps.index(step):]
            ctx.store.set_autopilot_chain(session_id, remaining)

            # Cost budget guard (used by auto-fix sessions)
            if cost_budget_usd is not None and chain_instances:
                chain_cost = sum(i.cost_usd or 0 for i in chain_instances)
                if not within_dollar_budget(chain_cost, cost_budget_usd):
                    await ctx.messenger.send_text(
                        ctx.channel_id,
                        f"Cost budget exhausted (${chain_cost:.2f} / ${cost_budget_usd:.2f}). Pausing chain.",
                        silent=True,
                    )
                    await _exit_chain_needs_input(
                        ctx, source_id, session_id, steps, completed_steps,
                        chain_instances, result, "budget_exhausted",
                    )
                    return result

            if step == "review_loop":
                result, did_not_converge = await _review_plan_loop(
                    ctx, current_id, current_msg, chain_instances,
                )
                if did_not_converge and result:
                    src_for_halt = ctx.store.get_instance(source_id)
                    src_ref = f" {src_for_halt.display_id()}" if src_for_halt else ""
                    await ctx.messenger.send_text(
                        ctx.channel_id,
                        f"Plan{src_ref} didn't converge after "
                        f"{_REVIEW_LOOP_MAX_ROUNDS} review rounds — "
                        f"halting chain. This usually means the plan is "
                        f"operational/advisory rather than a code change, "
                        f"or the reviewer keeps finding new issues. "
                        f"Review the latest revision and either retry the "
                        f"build manually or simplify the plan.",
                        silent=True,
                    )
                    # Do NOT append "review_loop" to completed_steps — it
                    # hung, didn't complete; mirror the general failure-exit
                    # convention further below in this loop.
                    await _exit_chain_needs_input(
                        ctx, source_id, session_id, steps, completed_steps,
                        chain_instances, result, "review_did_not_converge",
                    )
                    return result
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

                        # Inject the latest plan text only on the first phase —
                        # compaction risk is highest at the plan→build handoff.
                        # Subsequent phases run inside the same build session
                        # with just-committed code as their anchor, so the
                        # extra prompt budget isn't worth it on every phase.
                        if is_first:
                            phase_prompt = _with_plan(
                                _extract_latest_plan_text(
                                    ctx, chain_instances, source_id,
                                ),
                                phase_prompt,
                            )

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
                        # HEAD didn't move. First spend the one finishup nudge
                        # (orphaned-background-job recovery), then:
                        #   (a) Worktree dirty → agent forgot to commit.
                        #       Auto-commit in place and continue silently.
                        #   (b) Worktree clean → genuinely no work done.
                        #       Reroute to needs_input with rich halt context.
                        post_head = await _git_head(phase_result.worktree_path)
                        head_parked = bool(
                            pre_head and post_head and pre_head == post_head
                        )
                        if head_parked:
                            # Finishup nudge first — same orphaned-background
                            # hole as the single-build guard (see
                            # _attempt_finishup_nudge). Each phase passes its
                            # own per-phase baseline; on success the phase
                            # loop continues to the next phase as normal.
                            nudged, head_moved = await _attempt_finishup_nudge(
                                ctx, phase_result, pre_head, current_msg,
                                phase_label=f"phase {phase.id}",
                            )
                            if nudged is not None:
                                phase_result = nudged
                                result = nudged
                                if (nudged.status != InstanceStatus.COMPLETED
                                        or nudged.needs_input):
                                    # Mirror the mid-phase failure exit: pause
                                    # at "pre" so resume re-runs this phase.
                                    outcome = (
                                        "needs_input" if nudged.needs_input
                                        else "failed"
                                    )
                                    ctx.store.set_phase_pause(session_id, "pre")
                                    await _exit_chain(
                                        ctx, source_id, session_id, steps,
                                        completed_steps, chain_instances,
                                        nudged, outcome,
                                        f"Phase `{phase.id}` needs your attention.",
                                        intervention=True,
                                    )
                                    return nudged
                                head_parked = not head_moved
                        if head_parked:
                            rescued_sha = await ctx.runner.auto_commit_dirty_worktree(phase_result)
                            if rescued_sha:
                                ctx.store.update_instance(phase_result)
                                log.info(
                                    "Phase %s had no commit but dirty worktree — "
                                    "auto-committed %s, continuing chain",
                                    phase.id, rescued_sha[:8],
                                )
                                # Fall through to post-phase gate / next phase.
                            else:
                                preserved: str | None = None
                                # Only the first phase owns the worktree lifecycle —
                                # tear it down so Merge/Discard don't appear empty.
                                if is_first and phase_result.branch:
                                    outcome = await ctx.runner.discard_branch(
                                        phase_result, preserve_if_dirty=True,
                                    )
                                    preserved = outcome.preserved_branch
                                    ctx.store.update_instance(phase_result)
                                halt_text, mention = _build_halted_text(
                                    header=f"Phase `{phase.id}` stopped before committing.",
                                    summary=phase_result.summary,
                                    preserved=preserved,
                                    path_poisoning=phase_result.path_poisoning,
                                    phase_label=f"Phase `{phase.id}`",
                                )
                                try:
                                    await ctx.messenger.send_text(
                                        ctx.channel_id, halt_text, silent=True,
                                    )
                                except Exception:
                                    log.exception(
                                        "Phase halt notice failed to send for %s",
                                        ctx.channel_id,
                                    )
                                completed_steps.append(step)
                                ctx.store.set_phase_pause(session_id, "pre")
                                await _exit_chain(
                                    ctx, source_id, session_id, steps, completed_steps,
                                    chain_instances, phase_result, "needs_input",
                                    mention,
                                    clear_state=False,
                                    intervention=True,
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
                    base_prompt = (
                        config.BUILD_FROM_PLAN_PROMPT if is_plan
                        else config.BUILD_FROM_QUERY_PROMPT
                    )
                    build_prompt = _with_plan(
                        _extract_latest_plan_text(
                            ctx, chain_instances, source_id,
                        ),
                        base_prompt,
                    )
                    result = await spawn_from(ctx, current_id, SpawnConfig(
                        instance_type=InstanceType.TASK,
                        prompt=build_prompt,
                        mode="build", origin=InstanceOrigin.BUILD,
                        status_text="Building...", resume_session=True,
                        auto_branch=True, silent=True,
                    ), source_msg_id=current_msg)
            elif step == "sensors":
                result = await on_sensors(ctx, current_id, current_msg)
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
                    # close_silent is driven by the chain entry point:
                    # Autopilot keeps the loud chain-completion ping; Build &
                    # Ship opts into a silent close (the in-thread "✅
                    # {merge_msg}" summary + tag flip carry the signal).
                    # stop_silent: on a STOPPED merge the exit mention below
                    # is the ping, so the warning itself posts quietly.
                    merge_outcome = await _finalize_merge(
                        ctx, merge_target, close_silent=silent_close,
                        stop_silent=True,
                    )
                    if merge_outcome is MergeOutcome.STOPPED:
                        # The branch landed on a tree that must not ship:
                        # _finalize_merge already skipped deploy and close and
                        # posted the warning. Not a conflict, so no Resolve /
                        # Retry banner, and not a finished chain either, so it
                        # must not be recorded as merged.
                        completed_steps.append(step)
                        await _exit_chain_needs_input(
                            ctx, source_id, session_id, steps, completed_steps,
                            chain_instances, None, "merge_stopped",
                            suffix_override=(
                                f"{chain_label} merged, but stopped before "
                                f"deploy and close. Read the warning above."
                            ),
                        )
                        return result
                    if merge_outcome is MergeOutcome.FAILED:
                        await record_merge_failure(
                            ctx, merge_target, session_id=merge_target.session_id,
                        )
                        completed_steps.append(step)
                        repo_ref = merge_target.repo_name or "repo"
                        branch_ref = merge_target.branch or "branch"
                        merge_suffix = (
                            f"{chain_label} hit a merge conflict on "
                            f"{repo_ref}:{branch_ref} — "
                            f"tap Resolve / Retry / Discard."
                        )
                        await _exit_chain_needs_input(
                            ctx, source_id, session_id, steps, completed_steps,
                            chain_instances, None, "merge_failed",
                            suffix_override=merge_suffix,
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

                # Outcome-varied mention: lead with chain label + which step
                # failed so the Discord notification preview alone tells the
                # user what happened. `needs_input` keeps its generic
                # _CHAIN_EXIT_MESSAGES copy (already specific). `phantom_detected`
                # has a parse-failed sub-case whose ping must match the body
                # ("couldn't be parsed" rather than "flagged phantom claims").
                suffix_override: str | None = None
                if outcome == "failed":
                    suffix_override = (
                        f"{chain_label} blocked at {step} — "
                        f"needs your attention."
                    )
                elif (
                    outcome == "phantom_detected"
                    and result._verifier_parse_failed
                ):
                    suffix_override = (
                        "Release verifier output couldn't be parsed. "
                        "Tap Amend to retry, or Continue to ship anyway."
                    )
                await _exit_chain_needs_input(
                    ctx, source_id, session_id, steps, completed_steps,
                    chain_instances, result, outcome,
                    suffix_override=suffix_override,
                )
                return result

            # Guard: build produced no commits.
            # HEAD movement is authoritative; the prior `code_active` check
            # gave false positives because that flag inherits from session
            # siblings (lifecycle.py:441) — a build that wrote nothing in a
            # session that previously made edits would still report True.
            # Skipped for multi-phase: each phase ran its own per-phase
            # HEAD-movement check at line ~1867.
            #
            # When HEAD didn't move, first spend the one finishup nudge
            # (orphaned-background-job recovery — _attempt_finishup_nudge),
            # then fall back to the two legacy cases:
            #   (a) Worktree dirty → agent wrote files but forgot to commit.
            #       Auto-commit in place and continue the chain silently.
            #   (b) Worktree clean → agent genuinely did nothing.
            #       Halt the chain and surface to the user.
            if step == "build" and result and not multi_phase_ran:
                post_build_head = await _git_head(
                    result.worktree_path or result.repo_path
                )
                no_changes = bool(
                    pre_build_head and post_build_head
                    and pre_build_head == post_build_head
                )
                if no_changes:
                    # Finishup nudge first: the usual cause is an orphaned
                    # background job (t-5592 — the agent backgrounded its test
                    # suite and ended the turn expecting to be re-invoked).
                    # Resume the session once for a corrective foreground turn
                    # before falling back to rescue/halt.
                    nudged, head_moved = await _attempt_finishup_nudge(
                        ctx, result, pre_build_head, current_msg,
                    )
                    if nudged is not None:
                        result = nudged
                        current_msg = _last_msg_id(result, ctx.platform)
                        if (result.status != InstanceStatus.COMPLETED
                                or result.needs_input):
                            # Nudge turn killed/failed/asked a question —
                            # generic step-failure exit. No rescue cycle on
                            # top of it, no extra halt notice.
                            outcome = (
                                "needs_input" if result.needs_input
                                else "failed"
                            )
                            suffix_override = (
                                f"{chain_label} blocked at {step} — "
                                f"needs your attention."
                            ) if outcome == "failed" else None
                            await _exit_chain_needs_input(
                                ctx, source_id, session_id, steps,
                                completed_steps, chain_instances, result,
                                outcome, suffix_override=suffix_override,
                            )
                            return result
                        no_changes = not head_moved
                if no_changes:
                    rescued_sha = await ctx.runner.auto_commit_dirty_worktree(result)
                    if rescued_sha:
                        # Case (a): silent rescue — fall through to normal
                        # chain continuation. No user-visible halt.
                        ctx.store.update_instance(result)
                        log.info(
                            "Build %s had no commit but dirty worktree — "
                            "auto-committed %s, continuing chain",
                            result.id, rescued_sha[:8],
                        )
                    else:
                        # Case (b): genuinely empty build — preserve any
                        # uncommitted edits as a WIP branch (safety net), then
                        # halt with a rich notice routed to needs_input so the
                        # user can reply with guidance.
                        preserved: str | None = None
                        if result.branch:
                            outcome = await ctx.runner.discard_branch(
                                result, preserve_if_dirty=True,
                            )
                            preserved = outcome.preserved_branch
                            ctx.store.update_instance(result)
                        halt_text, mention = _build_halted_text(
                            header="Build stopped before committing.",
                            summary=result.summary,
                            preserved=preserved,
                            path_poisoning=result.path_poisoning,
                        )
                        try:
                            await ctx.messenger.send_text(
                                ctx.channel_id, halt_text, silent=True,
                            )
                        except Exception:
                            log.exception(
                                "Empty-diff halt notice failed to send for %s",
                                ctx.channel_id,
                            )
                        completed_steps.append(step)
                        await _exit_chain_needs_input(
                            ctx, source_id, session_id, steps, completed_steps,
                            chain_instances, result, "needs_input",
                            suffix_override=mention,
                        )
                        return result
            # An all-pass sensors step returns the build instance unchanged —
            # don't append the same instance twice (would duplicate it in
            # chain eval and the manual-verify handoff list).
            if not chain_instances or chain_instances[-1].id != result.id:
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
            ctx.store.append_deferred(result.repo_name, chain_deferred)

        # Evaluate the completed chain
        outcome = "merged" if "merge" in completed_steps else "completed"
        _eval_chain_safe(ctx.store, source_id, steps, completed_steps,
                         chain_instances, outcome)

        # Terminal-state handoff: if any chain instance is flagged for manual
        # verification, surface the WHY lines so the user knows what to eyeball.
        manual_items = [
            i for i in chain_instances if i.needs_manual_verification
        ]
        if manual_items:
            why_lines = "\n".join(
                f"• {i.manual_verify_reason}"
                for i in manual_items if i.manual_verify_reason
            )
            try:
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"ℹ️ Couldn't be verified automatically — worth "
                    f"eyeballing by hand:\n{why_lines}",
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

        ctx.store.clear_chain_state(session_id)
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

    LOCK CONTRACT: callers MUST hold ``commands._get_channel_lock(channel_id)``
    while invoking this function and for the duration of the returned
    awaitable.  Without the lock, a concurrent text/button on the same
    thread can spawn a second session in parallel — see the t-3501 incident.
    Direct callers and how they satisfy the contract:
      - ``commands.handle_callback`` (continue_autopilot action) — locked by
        ``interactions.handle`` for query-class actions.
      - ``on_amend_done``, ``on_continue_anyway`` (both below):
        invoked via the "amend" / "continue_anyway" buttons, same lock path.
      - ``app._do_cooldown_retry_locked`` — wraps the full retry in the lock.
    The companion ``_run_autopilot_chain`` is also called directly by
    ``boot_recovery.resume_interrupted_chains`` under its own lock acquisition.

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
    label, silent_close = ctx.store.get_chain_kwargs(session_id)
    return await _run_autopilot_chain(
        ctx, source_id, source_msg_id, remaining, session_id,
        silent_close=silent_close,
        chain_label=label,
    )


async def _launch_chain(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None,
    steps: list[str],
    start_from: str,
    cost_budget_usd: float | None = None,
    *,
    silent_close: bool = False,
    chain_label: str = "Autopilot",
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
    # Persist entry-point kwargs so resume paths (Continue button +
    # `boot_recovery.resume_interrupted_chains`) restore the originating chain label
    # and silent_close preference, instead of defaulting to "Autopilot" /
    # loud close on continuation.
    if source.session_id:
        ctx.store.set_chain_kwargs(
            source.session_id,
            label=chain_label,
            silent_close=silent_close,
        )
    return await _run_autopilot_chain(
        ctx, source_id, source_msg_id,
        steps[idx:], source.session_id,
        cost_budget_usd=cost_budget_usd,
        silent_close=silent_close,
        chain_label=chain_label,
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
    """Build → Review Code → Verify → Done → Merge.

    Ships quietly on success (silent_close=True): the close-conversation
    handshake skips the participant mention, while in-thread summary + tag
    flip provide visibility. Failures still ping via _exit_chain mention.
    """
    return await _launch_chain(
        ctx, source_id, source_msg_id,
        _BUILD_AND_SHIP_STEPS, start_from,
        silent_close=True,
        chain_label="Build & Ship",
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
