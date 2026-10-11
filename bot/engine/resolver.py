"""The merge-conflict resolver: a session that resolves a failed merge in the build worktree."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from bot.procutil import run_capture
from bot.claude.gitpaths import ref_sha
from bot.claude.types import Instance, InstanceStatus, KillOutcome, merge_msg_is_failure
from bot.engine import workflows
from bot.platform.base import RequestContext
from bot.platform.formatting import (
    merge_failed_banner,
    merge_failed_button_specs,
    resolver_running_button_specs,
)

from bot.claude.runner import MERGE_FAIL_DIVERGED

log = logging.getLogger(__name__)


# --- Merge-conflict resolver helpers ---

_RESOLVE_TIMEOUT_SECONDS = 900  # 15 min cap on resolver run


def _resolver_prep_worktree(inst: Instance) -> tuple[str | None, list[str], str | None]:
    """Replay the failing merge inside the worktree so conflicts are live.

    Runs synchronously — caller wraps in to_thread under the per-repo lock.
    Returns (error_message, conflict_files, pre_merge_sha). error_message is
    None on success; on failure conflict_files and pre_merge_sha are unused.
    """
    if not inst.worktree_path or not Path(inst.worktree_path).is_dir():
        return ("Worktree directory missing — cannot resolve.", [], None)
    if not inst.original_branch:
        return ("No original_branch on instance — cannot resolve.", [], None)
    wt = inst.worktree_path

    # Best-effort abort any leftover merge state. If there's no merge in
    # progress this is a harmless no-op (non-zero exit, ignored).
    run_capture(["git", "merge", "--abort"], cwd=wt)

    pre_sha = ref_sha(wt)
    if not pre_sha:
        return (f"Could not read worktree HEAD in {wt}", [], None)

    # Merge ORIGINAL into FEATURE so resolution lands on the feature ref.
    # We don't pass --no-ff; we want the natural merge attempt.
    merge_r = run_capture(["git", "merge", "--no-commit", "--no-ff", inst.original_branch], cwd=wt)
    # Conflicts → returncode != 0 AND files in --diff-filter=U.
    conflicts_r = run_capture(["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt)
    conflicts = [f.strip() for f in conflicts_r.stdout.splitlines() if f.strip()]
    if merge_r.returncode == 0 and not conflicts:
        # Merge succeeded cleanly on replay — the original failure must have
        # been transient. Commit and let the caller skip the resolver.
        run_capture(["git", "commit", "--no-edit"], cwd=wt)
        return ("CLEAN", [], pre_sha)
    if not conflicts:
        # Non-zero exit without unmerged paths — something unexpected.
        detail = (merge_r.stderr or merge_r.stdout or "").strip()
        run_capture(["git", "merge", "--abort"], cwd=wt)
        return (f"Replay failed with no conflict files: {detail}", [], None)
    return (None, conflicts, pre_sha)


def _resolver_verify(inst: Instance, pre_sha: str | None) -> tuple[bool, str]:
    """Verify the resolver completed the merge. Returns (ok, detail)."""
    if not inst.worktree_path or not Path(inst.worktree_path).is_dir():
        return (False, "worktree missing")
    wt = inst.worktree_path

    merge_head = run_capture(["git", "rev-parse", "--verify", "MERGE_HEAD"], cwd=wt)
    if merge_head.returncode == 0:
        return (False, "merge still in progress (MERGE_HEAD present)")

    if pre_sha:
        post_sha = ref_sha(wt)
        if not post_sha:
            return (False, "could not read post-merge HEAD")
        if post_sha == pre_sha:
            return (False, "branch SHA did not advance")
    return (True, "ok")


def _build_resolver_prompt(
    inst: Instance, conflicts: list[str], deferred: str,
) -> str:
    """Compose the resolver's first-turn prompt.

    The prompt names the branches, lists conflict files, and gives a strict
    completion contract: stage + commit the merge before exiting.
    """
    feature = inst.branch or "(unknown)"
    target = inst.original_branch or "(unknown)"
    files = "\n".join(f"  - {f}" for f in conflicts) if conflicts else "  (none reported)"
    extra = ""
    if deferred:
        extra = (
            "\n\nThe user has provided this guidance about how to resolve:\n"
            f"```\n{deferred}\n```\n"
        )
    return (
        f"An auto-merge of `{feature}` into `{target}` failed. You are running "
        f"INSIDE the build worktree (`{inst.worktree_path}`). Master has already "
        f"been merged INTO `{feature}` with `--no-commit`, so the working tree "
        f"is mid-merge with conflict markers.\n\n"
        f"Conflict files:\n{files}\n\n"
        "Your job:\n"
        "1. Inspect each conflict file. Resolve the markers — preserve both "
        "sides' intent where possible.\n"
        "2. For CHANGELOG.md specifically: keep both `[Unreleased]` blocks' "
        "entries merged under a single `## [Unreleased]` header.\n"
        "3. `git add` each resolved file.\n"
        "4. `git commit --no-edit` to complete the merge commit.\n"
        "5. Verify with `git status` that the working tree is clean and "
        "MERGE_HEAD is gone.\n\n"
        "Do NOT abort the merge. Do NOT push. Do NOT run the test suite. "
        "Just resolve, stage, and commit. Stop when the merge is committed."
        f"{extra}"
    )


async def _on_resolve_merge(
    ctx: RequestContext, instance_id: str, source_msg_id: str | None,
) -> None:
    """Spawn a Claude resolver into the build worktree to fix merge conflicts."""
    inst = ctx.store.get_instance(instance_id)
    if not inst:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return
    if not inst.branch or not inst.worktree_path:
        await ctx.messenger.send_text(
            ctx.channel_id,
            "Cannot resolve: this instance has no live branch/worktree.",
        )
        return
    pending = ctx.store.get_pending_merge(inst.id)
    if not pending:
        await ctx.messenger.send_text(
            ctx.channel_id, "No pending merge to resolve.",
        )
        return
    if pending.get("failure_kind") == MERGE_FAIL_DIVERGED:
        # The resolver fixes branch-vs-master conflict markers inside the
        # worktree; it never touches local-vs-origin divergence, so
        # spawning it here is a guaranteed no-op round-trip.
        await ctx.messenger.send_text(
            ctx.channel_id,
            "This failure isn't a merge conflict — local master and origin "
            "have diverged, which the resolver can't fix. Run "
            "`git pull --rebase` in the main repo to reconcile, then tap "
            "Try Merge Again.",
            buttons=merge_failed_button_specs(inst.id),
        )
        return
    if _resolver_in_flight(ctx, inst.id):
        await ctx.messenger.send_text(
            ctx.channel_id, "Resolver already running — wait or tap Cancel.",
        )
        return

    # Pre-flight: replay the merge inside the worktree so conflicts are live.
    repo_lock = ctx.runner._get_repo_lock(inst.repo_path)
    async with repo_lock:
        err, conflicts, pre_sha = await asyncio.to_thread(
            _resolver_prep_worktree, inst,
        )
    if err and err != "CLEAN":
        await ctx.messenger.send_text(
            ctx.channel_id, f"Could not start resolver: {err}",
            buttons=merge_failed_button_specs(inst.id),
        )
        return

    if err == "CLEAN":
        # Replay merged cleanly — skip resolver, go straight to merging into master.
        await ctx.messenger.send_text(
            ctx.channel_id,
            "Replay of the merge succeeded without conflicts — proceeding to merge into master.",
        )
        msg = await ctx.runner.merge_branch(inst)
        ctx.store.update_instance(inst)
        banner_suffix = ""
        if not merge_msg_is_failure(msg):
            ctx.store.clear_pending_merge(inst.id)
            workflows.clear_stale_branches(ctx.store, inst.branch or "")
        else:
            failure_kind = ctx.runner._last_merge_failure_kind.get(inst.id)
            ctx.store.set_pending_merge_failure_kind(inst.id, failure_kind)
            banner_suffix = f"\n\n{merge_failed_banner(failure_kind)}"
        # Escape only the runner output (may contain conflict file paths with
        # underscores etc.); the banner is trusted formatting and must keep
        # its **bold** + `code` markdown intact.
        await ctx.messenger.send_text(
            ctx.channel_id, f"{ctx.messenger.escape(msg)}{banner_suffix}",
        )
        return

    # Build prompt, consume any deferred user-typed guidance.
    deferred = (pending.get("deferred_text") or "").strip()
    prompt = _build_resolver_prompt(inst, conflicts, deferred)

    # Spawn detached so we can persist the resolver id before awaiting.
    spawned = await workflows.spawn_resolver_detached(
        ctx, inst.id, prompt,
    )
    if not spawned:
        # Roll back the replayed merge so a retry has a clean slate.
        # Keep deferred_text intact so the next Resolve attempt can deliver it.
        await asyncio.to_thread(run_capture, ["git", "merge", "--abort"], cwd=inst.worktree_path)
        return
    resolver_inst, task = spawned
    if deferred:
        ctx.store.clear_pending_merge_deferred_text(inst.id)
    ctx.store.set_pending_merge_resolver(inst.id, resolver_inst.id)

    # Swap the merge-failed buttons (if the click came from that message) for
    # a Cancel-only row.
    if source_msg_id:
        try:
            await ctx.messenger.edit_text(
                ctx.channel_id, source_msg_id,
                "Resolver running — Claude is editing the worktree to fix conflicts.",
                buttons=resolver_running_button_specs(inst.id),
            )
        except Exception:
            log.debug("Could not edit source message for resolver UI", exc_info=True)

    timed_out = False
    try:
        await asyncio.wait_for(task, timeout=_RESOLVE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        timed_out = True
        await ctx.runner.kill(resolver_inst.id)
        # Let the lifecycle's CancelledError handler finish bookkeeping.
        try:
            await asyncio.wait_for(task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
    except asyncio.CancelledError:
        # Outer handler cancelled us — propagate after cleanup.
        ctx.store.set_pending_merge_resolver(inst.id, None)
        raise
    except Exception:
        log.exception("Resolver task raised for %s", inst.id)

    # If cancel/discard handlers ran while we were awaiting the task, they
    # already posted UI and cleared pending state — skip the verify-fail path
    # to avoid double-messaging.
    pending_after = ctx.store.get_pending_merge(inst.id)
    externally_handled = (
        pending_after is None
        or not pending_after.get("resolver_instance_id")
    )
    ctx.store.set_pending_merge_resolver(inst.id, None)
    if externally_handled and not timed_out:
        return

    if timed_out:
        # Abort the half-resolved merge so subsequent attempts have a clean tree.
        if inst.worktree_path and Path(inst.worktree_path).is_dir():
            await asyncio.to_thread(run_capture, 
                ["git", "merge", "--abort"], cwd=inst.worktree_path,
            )
        await _post_resolver_failure(
            ctx, inst, source_msg_id,
            f"Resolver timed out after {_RESOLVE_TIMEOUT_SECONDS // 60} minutes. "
            "Tap **Resolve with Claude** to retry, **Try Merge Again**, or **Discard**.",
        )
        return

    ok, detail = await asyncio.to_thread(_resolver_verify, inst, pre_sha)
    if not ok:
        # Abort any half-merge state.
        if inst.worktree_path and Path(inst.worktree_path).is_dir():
            await asyncio.to_thread(run_capture, 
                ["git", "merge", "--abort"], cwd=inst.worktree_path,
            )
        await _post_resolver_failure(
            ctx, inst, source_msg_id,
            f"Resolver finished but the merge wasn't completed ({detail}). "
            "Tap **Resolve with Claude** to retry with different guidance, "
            "or **Discard** to abandon the branch.",
        )
        return

    # Worktree merge committed cleanly — clear the stale "Resolver running"
    # message so the lingering Cancel button doesn't confuse the user, then
    # merge feature into master.
    if source_msg_id:
        try:
            await ctx.messenger.edit_text(
                ctx.channel_id, source_msg_id,
                "Conflicts resolved — merging into master...",
                buttons=[],
            )
        except Exception:
            log.debug("Could not edit source message after resolve success", exc_info=True)
    msg = await ctx.runner.merge_branch(inst)
    ctx.store.update_instance(inst)
    banner_suffix = ""
    if not merge_msg_is_failure(msg):
        ctx.store.clear_pending_merge(inst.id)
        if inst.branch:
            workflows.clear_stale_branches(ctx.store, inst.branch)
        from bot.engine.deploy import apply_post_merge_deploy
        await apply_post_merge_deploy(ctx.messenger, ctx.store, inst)
        if ctx.on_merged:
            await ctx.on_merged()
    else:
        failure_kind = ctx.runner._last_merge_failure_kind.get(inst.id)
        ctx.store.set_pending_merge_failure_kind(inst.id, failure_kind)
        banner_suffix = f"\n\n{merge_failed_banner(failure_kind)}"
    await ctx.messenger.send_text(
        ctx.channel_id, f"{ctx.messenger.escape(msg)}{banner_suffix}",
    )


async def _post_resolver_failure(
    ctx: RequestContext,
    inst: Instance,
    source_msg_id: str | None,
    text: str,
) -> None:
    """Surface a resolver failure with merge_failed buttons.

    Resets the original 'Resolver running...' message back to the merge-failed
    UI when possible, otherwise posts a fresh message.
    """
    buttons = merge_failed_button_specs(inst.id)
    if source_msg_id:
        try:
            await ctx.messenger.edit_text(
                ctx.channel_id, source_msg_id, text, buttons=buttons,
            )
            return
        except Exception:
            log.debug("Could not edit source message for resolver failure", exc_info=True)
    await ctx.messenger.send_text(ctx.channel_id, text, buttons=buttons)


def _merge_context_prefix(meta: dict) -> str:
    """Build a system-note prefix for user text in a merge-stuck thread.

    Tells the resumed CLI session that auto-merge is unresolved so it doesn't
    reinterpret the message as a fresh task (e.g. "do another review" because
    the prior turn was a review). Sources `message` + `failure_kind` from the
    pending_merge meta; falls back to a generic note if `message` is missing.
    """
    msg = (meta.get("message") or "").strip()
    kind = (meta.get("failure_kind") or "").strip()
    kind_part = f" (kind={kind})" if kind else ""
    if not msg:
        return f"[system note: auto-merge still unresolved{kind_part}. User message follows.]"
    if len(msg) > 400:
        msg = msg[:400] + "…"
    return f"[system note: auto-merge still unresolved{kind_part}. Last failure: {msg}. User message follows.]"


def _resolver_in_flight(ctx: RequestContext, instance_id: str) -> str | None:
    """Return resolver_instance_id if a live resolver is running, else None.

    Authoritative check: only treats the resolver as running when the runner
    confirms it's active. Avoids false positives from a crashed bot that left
    a stale resolver_instance_id in pending_merge.
    """
    pending = ctx.store.get_pending_merge(instance_id)
    if not pending:
        return None
    rid = pending.get("resolver_instance_id") or ""
    if not rid:
        return None
    resolver_inst = ctx.store.get_instance(rid)
    if not resolver_inst:
        return None
    active = ctx.runner.active_instance_for_session(resolver_inst.session_id)
    if active == rid:
        return rid
    # Also accept the case where the runner is still juggling the resolver in
    # _active_tasks even if the session bookkeeping has rotated — kill() pops
    # both atomically so checking processes is a reasonable backstop.
    if rid in getattr(ctx.runner, "_processes", {}):
        return rid
    return None


async def _on_resolve_cancel(
    ctx: RequestContext, instance_id: str, source_msg_id: str | None,
) -> None:
    """Kill an in-flight merge-conflict resolver."""
    inst = ctx.store.get_instance(instance_id)
    if not inst:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return
    pending = ctx.store.get_pending_merge(inst.id)
    if not pending:
        await ctx.messenger.send_text(ctx.channel_id, "No pending merge state.")
        return
    resolver_id = pending.get("resolver_instance_id") or ""
    if not resolver_id:
        await ctx.messenger.send_text(ctx.channel_id, "No resolver running.")
        return
    # Clear resolver id BEFORE awaiting the kill — otherwise the outer
    # _on_resolve_merge wait_for unblocks the moment kill_and_wait returns and
    # may race us to the verify-fail message. The cleared flag is the signal
    # that tells the merge handler this was externally handled.
    ctx.store.set_pending_merge_resolver(inst.id, None)
    outcome = await ctx.runner.kill_and_wait(resolver_id, timeout=10)
    # FORCE_CLEARED: lifecycle wedged, never ran finalize — flip the
    # resolver instance status manually so it doesn't show stuck-RUNNING
    # forever in /list and history.
    if outcome == KillOutcome.FORCE_CLEARED:
        resolver_inst = ctx.store.get_instance(resolver_id)
        if resolver_inst and resolver_inst.status == InstanceStatus.RUNNING:
            resolver_inst.status = InstanceStatus.KILLED
            resolver_inst.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.store.update_instance(resolver_inst, critical=True)
    # Abort any half-merge so subsequent attempts start clean.
    if inst.worktree_path and Path(inst.worktree_path).is_dir():
        await asyncio.to_thread(run_capture, ["git", "merge", "--abort"], cwd=inst.worktree_path)
    text = "Resolver cancelled. Tap **Resolve with Claude**, **Try Merge Again**, or **Discard**."
    if source_msg_id:
        try:
            await ctx.messenger.edit_text(
                ctx.channel_id, source_msg_id, text,
                buttons=merge_failed_button_specs(inst.id),
            )
            return
        except Exception:
            log.debug("Could not edit source message after resolve_cancel", exc_info=True)
    await ctx.messenger.send_text(
        ctx.channel_id, text, buttons=merge_failed_button_specs(inst.id),
    )
