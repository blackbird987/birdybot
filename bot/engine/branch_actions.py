"""Merge, discard and /branches, and the cards a finished branch action leaves behind."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from bot import config
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, KillOutcome, merge_msg_is_failure
from bot.engine import workflows
from bot.platform.base import RequestContext
from bot.platform.formatting import (
    action_button_specs,
    merge_failed_banner,
)

from bot.claude.runner import (
    ClaudeRunner, DiscardOutcome, orphaned_releases,
)
from bot.engine.cmd_state import _clear_chain_on_manual_finalize
from bot.engine.resolver import _resolver_in_flight

log = logging.getLogger(__name__)


# --- /merge ---

async def on_merge(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if not text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /merge <id|name>")
        return

    inst = ctx.store.get_instance(text)
    if not inst:
        await ctx.messenger.send_text(ctx.channel_id, f"Instance '{text}' not found.")
        return

    branch_name = inst.branch  # Save before merge clears it
    msg = await ctx.runner.merge_branch(inst)
    ctx.store.update_instance(inst)
    if not merge_msg_is_failure(msg):
        ctx.store.clear_pending_merge(inst.id)
        # Resolve branch name for history cleanup (None when "already merged")
        if not branch_name:
            from bot.store import history as history_mod
            branch_name = history_mod.get_branch_for_instance(inst.id)
        if branch_name:
            workflows.clear_stale_branches(ctx.store, branch_name)
        from bot.engine.deploy import apply_post_merge_deploy
        await apply_post_merge_deploy(ctx.messenger, ctx.store, inst)
    else:
        failure_kind = ctx.runner._last_merge_failure_kind.get(inst.id)
        ctx.store.set_pending_merge_failure_kind(inst.id, failure_kind)
        msg = f"{msg}\n\n{merge_failed_banner(failure_kind)}"
    await ctx.messenger.send_text(ctx.channel_id, msg)


# --- /discard ---

async def on_discard(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if not text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /discard <id|name>")
        return

    inst = ctx.store.get_instance(text)
    if not inst:
        await ctx.messenger.send_text(ctx.channel_id, f"Instance '{text}' not found.")
        return

    if not inst.branch:
        outcome = await ctx.runner.discard_branch(inst)  # "Already discarded (...)"
        await _finish_resolved_branch(ctx, inst, outcome.message, None)
        return
    outcome = await perform_discard(ctx, inst, "/discard")
    await ctx.messenger.send_text(ctx.channel_id, outcome.message)


async def perform_discard(ctx: RequestContext, inst: Instance, op: str) -> DiscardOutcome:
    """Discard a build branch: the one path behind /discard and the Discard button.

    Stops a merge-conflict resolver still working in the worktree, ends any
    chain on the session, deletes the worktree and branch, and only when the
    branch is really gone drops the pending merge and the branch refs on every
    instance. A discard that was skipped or that git refused leaves all of that
    in place, so Merge and Discard keep working on the branch that survived.
    The two callers were near-copies that disagreed on the resolver, the chain
    and the failure test, which was a search for "failed" in the message.
    """
    # Clear resolver_id BEFORE awaiting the kill so the outer
    # _on_resolve_merge wait_for sees "externally handled" the moment it
    # resumes, and the resolver cannot race the branch teardown.
    live_resolver = _resolver_in_flight(ctx, inst.id)
    if live_resolver:
        ctx.store.set_pending_merge_resolver(inst.id, None)
        killed = await ctx.runner.kill_and_wait(live_resolver, timeout=10)
        if killed == KillOutcome.FORCE_CLEARED:
            resolver_inst = ctx.store.get_instance(live_resolver)
            if resolver_inst and resolver_inst.status == InstanceStatus.RUNNING:
                resolver_inst.status = InstanceStatus.KILLED
                resolver_inst.finished_at = datetime.now(timezone.utc).isoformat()
                ctx.store.update_instance(resolver_inst, critical=True)
    _clear_chain_on_manual_finalize(ctx.store, inst.session_id, op)
    branch_name = inst.branch  # Save before discard clears it
    outcome = await ctx.runner.discard_branch(inst)
    ctx.store.update_instance(inst)
    if not outcome.failed:
        ctx.store.clear_pending_merge(inst.id)
        if branch_name and not outcome.preserved_branch:
            workflows.clear_stale_branches(ctx.store, branch_name)
    return outcome


# --- /branches ---

async def on_branches(ctx: RequestContext) -> None:
    """List unmerged bot-managed branches across all repos."""
    repos = ctx.store.list_repos()
    if not repos:
        await ctx.messenger.send_text(ctx.channel_id, "No repos configured.")
        return

    # Collect branches and worktrees tracked by ANY instance
    active_branches: set[str] = set()
    active_worktrees: set[str] = set()
    for inst in ctx.store.list_instances(all_=True):
        if inst.branch:
            active_branches.add(inst.branch)
        if inst.worktree_path:
            active_worktrees.add(inst.worktree_path)

    lines: list[str] = []
    total_orphans = 0
    for repo_name, repo_path in repos.items():
        if not Path(repo_path).is_dir():
            continue
        # Orphan branches
        orphan_branches = ClaudeRunner.scan_orphan_branches(repo_path, active_branches)
        # Orphan worktrees
        orphan_wts = ClaudeRunner.scan_orphan_worktrees(repo_path, active_worktrees)
        # Release tags stranded outside the checked-out line. Same category
        # of leak as an orphaned branch (something the repo still lists as
        # existing that nothing can reach), but far quieter, because a tag
        # keeps its commits alive and `git tag` keeps printing the version
        # as though it shipped. Reported as a backlog here rather than on
        # every merge, where only the newest one is worth interrupting for.
        orphan_tags = (
            await asyncio.to_thread(orphaned_releases, repo_path)
            if config.RELEASE_ANCESTRY_CHECK else []
        )
        repo_orphans = len(orphan_branches) + len(orphan_wts) + len(orphan_tags)
        if repo_orphans:
            total_orphans += repo_orphans
            lines.append(f"**{repo_name}** ({repo_orphans} orphaned)")
            for b in orphan_branches[:10]:
                lines.append(f"  `{b}` (branch)")
            for w in orphan_wts[:10]:
                lines.append(f"  `{w}` (worktree)")
            for t in orphan_tags[:10]:
                lines.append(f"  `{t}` (release not in HEAD)")
            if len(orphan_tags) > 10:
                lines.append(f"  …and {len(orphan_tags) - 10} more release tags")

    if not lines:
        await ctx.messenger.send_text(
            ctx.channel_id,
            "No orphaned branches, worktrees or release tags found.",
        )
        return

    header = f"**Orphaned** ({total_orphans} total)\n\n"
    text = header + "\n".join(lines)
    markup = ctx.messenger.markdown_to_markup(text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- Post-merge/discard button cleanup ---

async def _strip_post_merge_buttons(
    ctx: RequestContext, inst: Instance, skip_msg_id: str | None = None,
) -> None:
    """Edit the result embed to remove Merge/Discard buttons after branch resolution.

    skip_msg_id: if the caller already edited this message (e.g. source_msg_id
    from a button click), skip it to avoid a double-edit race.
    """
    msg_ids = inst.message_ids.get(ctx.platform, [])
    if not msg_ids:
        return
    result_msg_id = msg_ids[-1]
    if result_msg_id == skip_msg_id:
        return  # Already handled by the caller's edit
    try:
        # Buttons only -- edit_text(text=None) leaves the body alone. It used
        # to re-render the message from format_result_md(), which on a result
        # posted inline meant replacing the last chunk of the actual answer
        # with a two-line summary card, and on a collapsed one threw away the
        # preview. Nothing about resolving a branch should rewrite what a
        # finished session said.
        buttons = action_button_specs(inst)
        await ctx.messenger.edit_text(ctx.channel_id, result_msg_id, None, buttons)
    except Exception:
        log.debug("Failed to strip buttons from %s result message", inst.id)


async def _finish_resolved_branch(
    ctx: RequestContext, inst: Instance, msg: str, source_msg_id: str | None,
) -> None:
    """Report a merge/discard against a branch that was already resolved.

    The Merge and Discard branches of the button dispatcher held byte-identical
    copies of this, which is the drift shape perform_kill was written to end:
    the history cleanup below was added to one of them first.
    """
    ctx.store.clear_pending_merge(inst.id)
    # History may still record the original branch -- clean it up so future
    # sessions don't see a stale "(branch: X)" line.
    try:
        from bot.store import history as history_mod
        stale = history_mod.get_branch_for_instance(inst.id)
        if stale:
            history_mod.clear_branch(stale)
    except Exception:
        pass
    escaped = ctx.messenger.escape(msg)
    if source_msg_id:
        await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, escaped)
    else:
        await ctx.messenger.send_text(ctx.channel_id, escaped)


async def _finish_branch_action(
    ctx: RequestContext, inst: Instance, escaped: str, source_msg_id: str | None,
) -> None:
    """Post the outcome of a merge/discard and tidy up if the branch is gone.

    `escaped` arrives ready to send because Merge appends an unescaped failure
    banner to it and Discard does not; everything after that point was the same
    thirteen lines in both branches.
    """
    # Pass updated buttons when branch was resolved (strips Merge/Discard)
    buttons = action_button_specs(inst) if not inst.branch else None
    if source_msg_id:
        await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, escaped, buttons)
    else:
        await ctx.messenger.send_text(ctx.channel_id, escaped)
    # Also strip buttons from the result embed if it's a different message
    if not inst.branch:
        await _strip_post_merge_buttons(ctx, inst, skip_msg_id=source_msg_id)
    # Close thread if this was a post-Done merge/discard (branch resolved)
    if inst.origin == InstanceOrigin.DONE and not inst.branch:
        try:
            await ctx.messenger.close_conversation(ctx.channel_id, skip_mention=True)
        except Exception:
            pass
