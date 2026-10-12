"""The autonomy loop: scheduled merges, spawn-wave release, weekly prompt
review, the supervisor footprint line and the ship sweep.

Started from ``bot.app.run`` once Discord is up. Every sweep is a no-op
unless a repo opted into merge or ship autonomy via .claude/workflow.json.
"""

from __future__ import annotations

import asyncio
import logging

from bot import config
from bot.textutil import parse_iso_utc

log = logging.getLogger(__name__)


# Autonomy loop — fires due auto-merge vetoes (Phase 2) and sweeps
# ship-policy stragglers (Phase 3). Both are no-ops unless a repo opted
# into merge/ship autonomy via .claude/workflow.json.
async def autonomy_loop(store, runner, discord_bot) -> None:
    """Fire due auto-merge vetoes and run the periodic sweeps, once a minute."""
    from datetime import datetime as dt, timezone as tz_mod
    if not discord_bot:
        return
    if not await discord_bot._wait_for_ready("autonomy_loop"):
        return
    sweep_running = {"v": False}
    ticks = 0
    while True:
        await asyncio.sleep(60)
        try:
            store.reload_if_changed()
            now_iso = dt.now(tz_mod.utc).isoformat()
            for iid, meta in store.list_due_scheduled_merges(now_iso):
                try:
                    await fire_scheduled_merge(store, runner, discord_bot, iid, meta)
                except Exception:
                    log.exception("Scheduled merge fire failed for %s", iid)
            ticks += 1
            # Orchestrator: release spawn waves whose children never all
            # came back — killed during a reboot, or dead before recording
            # a session. The join is derived per child, so it self-corrects
            # for everything EXCEPT a child that never reaches a terminal
            # state at all; without this sweep such a wave would hold its
            # parent open forever, which is worse than the per-child
            # callbacks it replaced. Deliberately outside the ship-sweep
            # overlap guard: this is a cheap scan and must not be starved
            # by a fleet ship that runs for minutes.
            if ticks % 5 == 0:
                try:
                    from bot.discord.orchestrator import sweep_stale_waves
                    n_released = await sweep_stale_waves(discord_bot)
                    if n_released:
                        log.info(
                            "Orchestrator: released %d stale spawn wave(s)",
                            n_released,
                        )
                except Exception:
                    log.exception("Stale spawn-wave sweep failed")
            # Weekly prompt review. Checked on the same 5-minute cadence
            # as the sweeps, but gated on a PERSISTED timestamp rather
            # than on `ticks`: a reboot resets the counter, which would
            # otherwise either fire on every restart or skip the week.
            if ticks % 5 == 0:
                try:
                    from bot.discord.prompt_review import maybe_run_weekly
                    asyncio.create_task(maybe_run_weekly(discord_bot))
                except Exception:
                    log.exception("Prompt review scheduling failed")
            # The supervisor's own footprint. Recorded, never acted on:
            # see the note on SUPERVISOR_MEM_WARN_MB. Its own tick gate
            # rather than the 5-minute one, because this is a log line
            # for a curve read over days and a 5-minute cadence would
            # bury the thing it is meant to make visible.
            _sup_every = config.SUPERVISOR_MEM_LOG_MINS
            if _sup_every > 0 and ticks % _sup_every == 0:
                try:
                    from bot.claude import memory as _mem
                    foot = _mem.supervisor_footprint()
                    warn_at = config.SUPERVISOR_MEM_WARN_MB
                    if warn_at > 0 and foot.rss_mb > warn_at and not foot.error:
                        log.warning(
                            "Supervisor footprint: %s (above the %.0fMB "
                            "this is expected to stay under; nothing is "
                            "being reaped, this is a note to look)",
                            foot.summary(), warn_at,
                        )
                    else:
                        log.info("Supervisor footprint: %s", foot.summary())
                except Exception:
                    log.exception("Supervisor footprint read failed")
            # Ship sweep every ~5 min, guarded against overlap (a fleet
            # ship can run for minutes and may self-deploy/reboot).
            if ticks % 5 == 0 and not sweep_running["v"]:
                sweep_running["v"] = True

                async def _sweep():
                    try:
                        await ship_sweep(store, discord_bot)
                    except Exception:
                        log.exception("Autonomy ship sweep failed")
                    finally:
                        sweep_running["v"] = False

                asyncio.create_task(_sweep())
        except Exception:
            log.exception("Autonomy loop error")


async def fire_scheduled_merge(store, runner, discord_bot, iid: str, meta: dict) -> None:
    """Perform one due auto-merge veto: merge the branch like a Merge tap.

    Re-validates before merging (instance/branch still present, no chain took
    over, session not active again) and drops the entry if the state is stale.
    Uses ``_finalize_merge`` — the exact path the Merge button and autopilot
    use, so tag/deploy/close behavior is identical. Always clears the
    scheduled-merge record afterward so a stuck entry can't re-fire forever.
    """
    from bot.engine import workflows
    from bot.engine.commands import _get_channel_lock

    channel_id = meta.get("channel_id")
    session_id = meta.get("session_id") or None
    inst = store.get_instance(iid)

    if inst is None or not inst.branch or not inst.original_branch or not channel_id:
        store.clear_scheduled_merge(iid)
        return
    # A chain that started after scheduling owns the merge now; and a
    # re-activated session means the user is back working — don't merge under
    # either. Leave the branch for manual/chain handling.
    if store.get_autopilot_chain(session_id) or runner.is_session_active(session_id):
        store.clear_scheduled_merge(iid)
        return

    # No-op guard: a build that committed nothing leaves a branch level with
    # its base. Merging it would report "already up to date" (a non-failure)
    # and _finalize_merge would then close the thread — silently archiving a
    # build that did nothing. Skip and drop the entry instead.
    from bot.discord.fleet import _commits_ahead
    ahead = await asyncio.to_thread(
        _commits_ahead, inst.repo_path, inst.original_branch, inst.branch,
    )
    if ahead <= 0:
        store.clear_scheduled_merge(iid)
        return

    lookup = discord_bot._forums.thread_to_project(channel_id)
    info = lookup[1] if lookup else None
    ctx = discord_bot._ctx(
        channel_id, session_id=session_id, repo_name=inst.repo_name,
        thread_info=info, source="autonomy",
    )
    # Only attach callbacks when the thread is still registered — passing a
    # None thread_info would raise, and (outside the try below) that would
    # leave the entry to re-fire and re-raise every tick. Without callbacks
    # the merge still runs; only the "merged" tag is skipped.
    if info is not None:
        discord_bot._forums.attach_session_callbacks(ctx, info, channel_id)

    lock = _get_channel_lock(channel_id)
    async with lock:
        try:
            ok = await workflows._finalize_merge(ctx, inst, close_silent=True)
        except Exception:
            log.exception("Scheduled auto-merge crashed for %s", iid)
            ok = False
    store.clear_scheduled_merge(iid)
    if not ok:
        # Merge failed — surface the standard recovery UI so it isn't silent.
        try:
            from bot.platform.formatting import (
                merge_failed_banner, merge_failed_button_specs,
            )
            failure_kind = runner._last_merge_failure_kind.get(inst.id)
            msg = merge_failed_banner(failure_kind)
            await ctx.messenger.send_text(
                channel_id,
                "Auto-merge hit a conflict — needs your attention.\n\n" + msg,
                buttons=merge_failed_button_specs(inst.id), silent=False,
            )
            store.set_pending_merge(
                inst.id, session_id=session_id, channel_id=channel_id,
                repo_name=inst.repo_name, message=msg, failure_kind=failure_kind,
            )
        except Exception:
            log.debug("Failed to post auto-merge failure UI for %s", iid, exc_info=True)


async def ship_sweep(store, discord_bot) -> None:
    """Fleet-ship stragglers for repos on ``ship`` autonomy.

    Backstop for the per-build veto (Phase 2): sweeps any committed-unmerged
    work in ship-policy repos that's aged past ``ship_grace_minutes`` and isn't
    already covered by a scheduled merge. Runs the existing fleet pipeline
    (merge → deploy → verify-back). No-op when nothing qualifies.
    """
    from datetime import datetime as dt, timezone as tz_mod

    from bot.discord import fleet
    from bot.engine.workflows import load_workflow_policy

    try:
        targets = await fleet.collect_ship_targets(discord_bot)
    except Exception:
        log.debug("autonomy sweep: collect_ship_targets failed", exc_info=True)
        return
    if not targets:
        return

    now = dt.now(tz_mod.utc)
    ship_ids: list[str] = []
    for t in targets:
        policy = load_workflow_policy(t.inst.repo_path if t.inst else None)
        if policy["autonomy"] != "ship":
            continue
        # A per-build veto already owns this session — let Phase 2 handle it.
        if store.get_scheduled_merge_by_session(t.session_id):
            continue
        # Grace window: only ship work we can CONFIRM is aged past the grace
        # period. If the timestamp is missing or unparseable, fail safe (skip)
        # rather than auto-deploying work of unknown age.
        stamp = (t.inst.finished_at or t.inst.created_at) if t.inst else None
        age_min: float | None = None
        if stamp:
            parsed = parse_iso_utc(stamp)
            age_min = (now - parsed).total_seconds() / 60 if parsed else None
        if age_min is None or age_min < policy["ship_grace_minutes"]:
            continue
        ship_ids.append(t.thread_id)

    if not ship_ids:
        return
    origin = str(getattr(discord_bot, "_lobby_channel_id", "") or "")
    log.info("Autonomy sweep: fleet-shipping %d straggler(s)", len(ship_ids))
    await fleet.run_fleet_ship(discord_bot, origin, ship_ids)
