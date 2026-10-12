"""Spawning a workflow step: the shared spawn path every button and chain step goes through."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from bot import config
from bot.claude.types import (
    Instance, InstanceOrigin,
    InstanceStatus, InstanceType,
)
from bot.engine import cmd_state, lifecycle
from bot.platform.base import RequestContext
from bot.platform.formatting import (
    action_button_specs,
    running_button_specs,
)

from bot.engine.step_policy import resolve_spawn_model

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
    # When set with copy_branch=True, spawn_from copies branch/worktree from
    # this instance ID instead of from `source`. Used by on_build to chain
    # successive builds in the same thread onto the prior build's branch
    # rather than re-cutting from master each time.
    chain_from: str | None = None
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


def _last_msg_id(inst: Instance, platform: str) -> str | None:
    """Get the last message ID for an instance on a platform."""
    msgs = inst.message_ids.get(platform, [])
    return msgs[-1] if msgs else None


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

    # Defensive duplicate-spawn guard: catches paths that bypassed the
    # per-channel lock contract (e.g. unlocked cooldown retry, auto_fix chain
    # step, future regressions).  The lock is the primary defense; this is
    # a backstop so a missed lock can't double-spawn on the same channel.
    # In normal flows (chain steps, button clicks on a completed instance)
    # the previous instance's end_task has already cleared this mapping, so
    # this only fires when something is genuinely still running.
    if ctx.channel_id is not None:
        active_inst_id = ctx.runner.active_instance_for_channel(ctx.channel_id)
        if active_inst_id:
            log.warning(
                "spawn_from blocked: channel %s already has active instance %s "
                "(attempted spawn from %s, origin=%s)",
                ctx.channel_id, active_inst_id, source_id, cfg.origin.value,
            )
            await ctx.messenger.send_text(
                ctx.channel_id,
                "Another session is already running on this thread — wait for it to finish.",
            )
            return None

    # Block spawns during reboot drain. Same-session overlap is allowed —
    # the channel lock + Queued embed serialize it visibly.
    #
    # check_session is read only by the drain branch below.  It used to be
    # computed for check_spawn_allowed as well, and was deleted along with
    # that argument (42907fd), which left the drain branch raising NameError.
    check_session = source.session_id if cfg.resume_session else None
    spawn_err = ctx.runner.check_spawn_allowed()
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

    # Budget guard.
    if await cmd_state.refuse_if_over_budget(ctx):
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
    #
    # Feed the BASELINE (un-clamped policy) into the floor, not source.bash_policy.
    # Otherwise a triage parent clamped to "none" would leak that clamp into
    # every subsequent child — including a later build child — and the build
    # would run with --disallowed-tools Bash.
    inherited_baseline = source.bash_policy_baseline
    effective_spawn_mode, inherited_bash_policy = _enforce_readonly_floor(
        cfg.permission_mode, effective_spawn_mode, inherited_baseline,
    )

    new_inst = ctx.store.create_instance(
        instance_type=cfg.instance_type,
        prompt=cfg.prompt,
        mode=effective_spawn_mode,
    )
    new_inst.origin = cfg.origin
    new_inst.origin_platform = ctx.platform
    new_inst.effort = ctx.effective_effort
    # Per-origin model routing (plan-vs-build split). None leaves instance.model
    # unset so it falls through to DEFAULT_SESSION_MODEL at command-build time —
    # the same last-resort DIRECT sessions get. See resolve_spawn_model.
    routed_model = resolve_spawn_model(cfg.origin, ctx.effective_model)
    if routed_model:
        new_inst.model = routed_model
    new_inst.parent_id = source.id
    new_inst.repo_name = source.repo_name
    new_inst.repo_path = source.repo_path
    # Inherit user identity and access control from parent
    new_inst.user_id = source.user_id or (ctx.user_id or "")
    new_inst.user_name = source.user_name or (ctx.user_name or "")
    new_inst.is_owner_session = source.is_owner_session
    new_inst.bash_policy = inherited_bash_policy
    new_inst.bash_policy_baseline = inherited_baseline
    new_inst.deferred_revisions = source.deferred_revisions
    # Carry the conceptual session's baseline forward — what was on master
    # when this thread of work first started. Resumed/chained spawns inherit
    # so the "what shipped since" check anchors to the original start, not
    # the most recent button press.
    new_inst.master_baseline_head = source.master_baseline_head

    if cfg.resume_session:
        new_inst.session_id = source.session_id
    if cfg.copy_branch:
        branch_src: Instance | None = source
        if cfg.chain_from:
            candidate = ctx.store.get_instance(cfg.chain_from)
            if candidate and candidate.branch and candidate.worktree_path:
                branch_src = candidate
            else:
                # chain_from is an explicit declaration that we want to stack
                # on a specific prior build. If that prior vanished between
                # the on_build lookup and now, abort the spawn rather than
                # silently fall back to source (which usually has no branch
                # for chain callers) or to a fresh master branch (which is
                # the very behavior we were trying to avoid).
                log.error(
                    "spawn_from %s: chain_from=%s missing or no live branch — aborting",
                    new_inst.id, cfg.chain_from,
                )
                new_inst.status = InstanceStatus.FAILED
                new_inst.error = (
                    f"chain_from={cfg.chain_from} vanished before spawn"
                )
                ctx.store.update_instance(new_inst)
                return None
        new_inst.branch = branch_src.branch
        new_inst.original_branch = branch_src.original_branch
        new_inst.worktree_path = branch_src.worktree_path
        if cfg.chain_from:
            new_inst.chained_from = cfg.chain_from
    elif cfg.auto_branch:
        new_inst.branch = f"{config.BRANCH_PREFIX}/{new_inst.id}"

    ctx.store.update_instance(new_inst)

    # Strip workflow buttons from the source message, but keep Expand/log if
    # that message is a preview card with the rest of the answer behind it.
    # This used to be inferred from the result FILE being >= 2000 bytes, which
    # is not the same quantity the delivery path measures (it measures display
    # text, after directives are folded away) -- so it could hang an Expand
    # button on a message that already holds the whole answer inline, where
    # tapping it overwrites that text. The delivery path now records what it
    # actually did.
    if strip_source_buttons and source_msg_id:
        try:
            preserve = None
            if source.result_collapsed and source.result_file:
                full = action_button_specs(source, show_expand=True)
                preserve = [
                    row for row in full
                    if any(b.callback_data.startswith(("expand:", "log:"))
                           for b in row)
                ] or None
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


async def spawn_resolver_detached(
    ctx: RequestContext,
    source_id: str,
    prompt: str,
    status_text: str = "Resolving merge conflicts...",
) -> tuple[Instance, asyncio.Task] | None:
    """Spawn a RESOLVE_MERGE instance into the source's worktree.

    Unlike spawn_from, the run is wrapped in asyncio.create_task so the caller
    can persist the resolver's instance id (for cancel + crash recovery) before
    awaiting completion. Returns (instance, task) or None if guards fail.
    """
    source = ctx.store.get_instance(source_id)
    if not source:
        await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
        return None
    if not source.branch or not source.worktree_path:
        await ctx.messenger.send_text(
            ctx.channel_id,
            "Cannot spawn resolver: source has no branch/worktree.",
        )
        return None

    # Mirrors spawn_from guards: active-channel, drain, budget, repo.
    if ctx.channel_id is not None:
        active_inst_id = ctx.runner.active_instance_for_channel(ctx.channel_id)
        if active_inst_id:
            log.warning(
                "spawn_resolver blocked: channel %s already has active instance %s",
                ctx.channel_id, active_inst_id,
            )
            await ctx.messenger.send_text(
                ctx.channel_id,
                "Another session is already running on this thread — wait for it to finish.",
            )
            return None

    spawn_err = ctx.runner.check_spawn_allowed()
    if spawn_err:
        await ctx.messenger.send_text(ctx.channel_id, spawn_err)
        return None

    if await cmd_state.refuse_if_over_budget(ctx):
        return None

    if not source.repo_path or not Path(source.repo_path).is_dir():
        await ctx.messenger.send_text(ctx.channel_id, "Repo path no longer valid.")
        return None

    new_inst = ctx.store.create_instance(
        instance_type=InstanceType.TASK,
        prompt=prompt,
        mode="build",
    )
    new_inst.origin = InstanceOrigin.RESOLVE_MERGE
    # Manual spawn (bypasses spawn_from) — apply the same plan-vs-build routing
    # so the conflict resolver runs on the strong build model, not the default.
    new_inst.model = resolve_spawn_model(new_inst.origin, ctx.effective_model)
    new_inst.origin_platform = ctx.platform
    new_inst.effort = ctx.effective_effort
    new_inst.parent_id = source.id
    new_inst.repo_name = source.repo_name
    new_inst.repo_path = source.repo_path
    new_inst.user_id = source.user_id or (ctx.user_id or "")
    new_inst.user_name = source.user_name or (ctx.user_name or "")
    new_inst.is_owner_session = source.is_owner_session
    # Resolver always spawns build mode (no floor), so the effective policy
    # is the un-clamped baseline — restoring Bash if the source had been
    # clamped by an upstream triage floor.
    new_inst.bash_policy = source.bash_policy_baseline
    new_inst.bash_policy_baseline = source.bash_policy_baseline
    new_inst.master_baseline_head = source.master_baseline_head
    new_inst.branch = source.branch
    new_inst.original_branch = source.original_branch
    new_inst.worktree_path = source.worktree_path
    new_inst.chained_from = source.id
    ctx.store.update_instance(new_inst)

    escaped = ctx.messenger.escape(new_inst.display_id())
    escaped_status = ctx.messenger.escape(status_text.lower())
    handle = await ctx.messenger.send_thinking(
        ctx.channel_id, f"⏳ {escaped} {escaped_status}",
        buttons=running_button_specs(new_inst.id),
    )
    if handle.get("message_id"):
        new_inst.message_ids.setdefault(ctx.platform, []).append(
            handle.get("message_id"),
        )
        ctx.store.update_instance(new_inst)

    task = asyncio.create_task(
        lifecycle.run_instance(ctx, new_inst, handle=handle, silent=False),
        name=f"resolver-{new_inst.id}",
    )
    return new_inst, task
