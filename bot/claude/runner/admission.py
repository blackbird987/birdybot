"""Memory admission: the hold before a spawn and fleet arbitration."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import NamedTuple

from bot import config
from bot.claude import cgroups, memory
from bot.claude.types import Instance
from bot.claude.runner.common import ProgressCallback, _notify

log = logging.getLogger(__name__)


class AdmissionBlock(NamedTuple):
    """Why a starting session is being held, and whose fault the shortage is.

    ``ours`` is the load-bearing half. Pressure the bot created clears on its
    own as soon as a running session finishes, so waiting for it is waiting
    for something that reliably happens; pressure it did not create may never
    clear, so a hold on that one has to give up quickly and start anyway. The
    two get very different deadlines in _await_memory_headroom.
    """

    reason: str
    ours: bool


class AdmissionMixin:
    """Memory admission: the hold before a spawn and fleet arbitration. Mixed into ClaudeRunner."""

    def _watched_pids(self) -> set[int]:
        """Pids an armed /watch is waiting on — never reclaimable.

        A watch exists precisely because a session launched a long job with
        ``setsid nohup ... &`` and detached from it, which makes that job
        reparented, unowned, and indistinguishable by parentage from a leaked
        build daemon. Killing one would silently destroy the job the thread is
        visibly sitting and waiting for.
        """
        if not self._store:
            return set()
        try:
            return {
                w.pid for w in self._store.list_watches()
                if getattr(w, "pid", None)
            }
        except Exception:
            # Never let a bookkeeping failure widen what the reaper considers
            # fair game. No answer means protect nothing new, which the caller
            # turns into "reap nothing" only because the pattern list is also
            # required — but an empty set here is still the cautious direction
            # relative to raising into a watchdog.
            log.debug("Could not read watch pids", exc_info=True)
            return set()

    async def _reclaim_idle_daemons(self, why: str) -> list[str]:
        """Kill leaked, idle build daemons in our cgroup. Returns what went.

        This is the first thing tried whenever memory is short, and it runs
        before anything that costs a session its work, because a build server
        sitting on gigabytes of nothing is free to give up and a session's run
        is not. In the kernel's task table from the 2026-08-21 OOM a
        detached `dotnet` at 4.10 GB was the second-largest process on the
        whole machine, while each session held about 0.3 GB — nothing the
        per-session guard could see, and nothing killing a session would free.

        Rate-limited and serialised — every session's watchdog can call it.
        """
        if not config.MEM_ORPHAN_SWEEP:
            return []
        async with self._reclaim_lock:
            now = asyncio.get_event_loop().time()
            if now - self._reclaim_last < config.SESSION_MEM_CHECK_SECS:
                return []
            self._reclaim_last = now
            protected = self._watched_pids()
            try:
                orphans = await asyncio.to_thread(
                    memory.find_orphans,
                    os.getpid(),
                    protected,
                    config.MEM_ORPHAN_MIN_MB,
                    config.MEM_ORPHAN_MIN_AGE_SECS,
                    config.MEM_ORPHAN_CPU_IDLE_PCT,
                    memory.RECLAIMABLE_DAEMONS,
                    # Sessions no longer live in the bot's own cgroup, so
                    # "our cgroup minus our process tree" stopped covering
                    # the case this whole path exists for: a Roslyn server a
                    # session detached stays in that session's scope after
                    # the session ends, where the bot's cgroup.procs will
                    # never list it. Empty tuple when scopes are not in use,
                    # which restores the previous behaviour exactly.
                    cgroups.session_slice_roots(),
                )
            except Exception:
                log.debug("Orphan scan failed", exc_info=True)
                return []
            if not orphans:
                return []
            # Logged whether or not anything is reaped. Memory in our cgroup
            # that belongs to no session is worth seeing every time — it is the
            # class of problem that is invisible until someone goes looking.
            log.warning(
                "Unowned memory in our cgroup (%s): %s",
                why,
                ", ".join(
                    f"{o.label()}{'' if o.reclaimable else ' [keeping]'}"
                    for o in orphans
                ),
            )
            reclaimable = [o for o in orphans if o.reclaimable]
            if not reclaimable:
                return []
            reaped = await asyncio.to_thread(
                memory.reap_orphans, reclaimable, 3.0,
                config.MEM_ORPHAN_CPU_IDLE_PCT,
            )
            if reaped:
                # Summed over what actually went, not over what was offered:
                # reap_orphans drops candidates whose pid was reused or that
                # went busy since the scan, and a headline that counted those
                # would report memory that is still very much allocated.
                by_label = {o.label(): o.rss_mb for o in reclaimable}
                freed = sum(by_label.get(label, 0.0) for label in reaped)
                log.warning(
                    "Reclaimed %.1fGB of idle build daemons (%s): %s",
                    freed / 1024, why, ", ".join(reaped),
                )
            return reaped

    async def _fleet_arbitration(
        self, instance_id: str, tree: memory.TreeMemory,
    ) -> memory.MemoryPressure | None:
        """Should this session be reaped for the machine? Reading, or None.

        Answers the question the per-session ceiling structurally cannot: five
        sessions at 2.5 GB never individually breach an 8 GB limit, and together
        they finish a 32 GB machine that is also running a browser. Before this
        the outcome of that was the cgroup OOM killer choosing a victim — safe,
        because OOMPolicy=continue keeps the unit alive, but silent: the session
        that died was never told why, and the user saw an unexplained failure.

        Every gate here exists to stop a wrong kill, in the order that costs
        least to evaluate:

        1. Too small to be worth taking. Reaping the largest of five 400 MB
           sessions frees nothing worth having and destroys real work.
        2. The machine is not actually in trouble.
        3. We are not the ones filling it. Machine-critical pressure caused by
           something outside this cgroup is not ours to solve by killing our
           own work — the offender would re-take the memory immediately and the
           session's run would be gone for nothing.
        4. Reclaim first. An idle build daemon holding gigabytes gives them up
           for free; a session gives them up at the cost of its work. If that
           alone clears it, nothing else happens.
        5. Not the largest. Exactly one session can be the maximum of a shared
           snapshot, which is what keeps two watchdogs from both volunteering.
        6. A reap happened recently. The kernel needs time to reclaim, and a
           second watchdog reading the same not-yet-recovered pressure would
           take a second session for a spike the first kill already fixed.
        """
        if not config.SESSION_MEM_FLEET_ARBITRATION:
            return None
        # SESSION_MEM_KILL_MB=0 is documented as "warnings only". Reaping a
        # session for the machine while the per-session kill is switched off
        # would break that promise, and every message on this path quotes the
        # ceiling the session was under — which reads as "under its own 0.0 GB
        # ceiling" when there is no ceiling at all.
        if config.SESSION_MEM_KILL_MB <= 0:
            return None
        if tree.total_mb < config.SESSION_MEM_FLEET_MIN_VICTIM_MB:
            return None

        pressure = await asyncio.to_thread(self._read_pressure)
        if not pressure.is_critical() or not pressure.over_own_high():
            return None

        await self._reclaim_idle_daemons(f"machine critical, {instance_id} live")
        pressure = await asyncio.to_thread(self._read_pressure)
        if not pressure.is_critical() or not pressure.over_own_high():
            log.info(
                "Machine recovered without reaping a session (%s)",
                pressure.summary(),
            )
            return None

        # Compare only against sessions that are still running. A finished
        # session's last sample lingers until its cleanup runs, and a dead
        # session must never win the "largest" contest and thereby spare a live
        # one that is genuinely the problem.
        live = {
            iid: sample for iid, sample in self._tree_samples.items()
            if iid in self._processes
        }
        if not live:
            return None
        # Tie broken on the id so the choice is deterministic: two sessions
        # reading an identical maximum must not both conclude they are it.
        largest = max(live.items(), key=lambda kv: (kv[1].total_mb, kv[0]))[0]
        if largest != instance_id:
            return None

        now = asyncio.get_event_loop().time()
        if now - self._fleet_last_reap < config.FLEET_REAP_COOLDOWN_SECS:
            log.warning(
                "Machine still critical (%s) and %s is largest, but a session "
                "was reaped %ds ago — waiting rather than cascading",
                pressure.summary(), instance_id,
                int(now - self._fleet_last_reap),
            )
            return None
        self._fleet_last_reap = now
        return pressure

    def _read_pressure(self) -> memory.MemoryPressure:
        """Machine pressure at the configured thresholds. Never raises.

        The oomd limit is read from systemd rather than configured, because it
        is systemd's number and can be changed in a drop-in without touching
        this repo. It is cached after the first call: it cannot move without a
        daemon-reload, and this function runs on every admission check and
        every fleet-arbitration tick. Passing None keeps the whole oomd rule
        out of the classifier, which is what happens off Linux and on a
        machine where oomd is not managing our slice.
        """
        try:
            limit: float | None = None
            if config.OOMD_AWARE_ENABLED:
                policy = memory.oomd_policy()
                if policy.active():
                    limit = policy.limit_pct
            return memory.read_pressure(
                critical_avail_mb=config.MEM_PRESSURE_CRITICAL_AVAIL_MB,
                tight_avail_mb=config.MEM_PRESSURE_TIGHT_AVAIL_MB,
                critical_swap_pct=config.MEM_PRESSURE_CRITICAL_SWAP_PCT,
                critical_psi_pct=config.MEM_PRESSURE_CRITICAL_PSI_PCT,
                tight_psi_pct=config.MEM_PRESSURE_TIGHT_PSI_PCT,
                oomd_limit_pct=limit,
                oomd_tight_fraction=config.OOMD_TIGHT_FRACTION,
                oomd_critical_fraction=config.OOMD_CRITICAL_FRACTION,
                own_cgroup=self._workload_cgroup(),
            )
        except Exception:
            log.debug("Pressure read failed", exc_info=True)
            return memory.MemoryPressure()

    @staticmethod
    def _workload_cgroup() -> Path | None:
        """The cgroup our sessions run in, or None to fall back to our own.

        Every "are we the ones filling this" reading has to be taken where the
        work is, and since v0.101.27 that is no longer where the bot is: the
        sessions moved into scopes under their own slice, leaving the
        supervisor's cgroup holding a ~250 MB asyncio loop. Both readings that
        answer that question (``over_own_high`` and ``own_psi_pct``) would
        otherwise measure the supervisor and answer no however much the fleet
        was thrashing, switching off fleet arbitration and telling the user
        "it is mostly not us" during a kill our own sessions caused.

        None when there is no sessions slice, which covers both the machine
        that cannot make scopes and the moment before the probe has run. In
        both cases the sessions are inside the bot's own cgroup anyway, and
        that is what ``read_pressure`` falls back to.
        """
        try:
            return cgroups.session_slice_path()
        except Exception:
            log.debug("Sessions slice lookup failed", exc_info=True)
            return None

    def _admission_blocked(
        self, pressure: memory.MemoryPressure,
    ) -> AdmissionBlock | None:
        """Why a starting session should wait, or None to let it through.

        One function so that the three places the hold loop asks the question
        cannot answer it differently, which is how a hold that never releases
        or one that never engages gets written. The classification lives here
        for the same reason: the hold loop needs to know whose pressure it is
        waiting on, and a second copy of that judgement in the loop is how the
        two start disagreeing.

        ``ours`` means the bot's own sessions are what is filling the machine.
        That distinction decides how long the hold is willing to wait, because
        only one of the two clears by itself: a running session finishes, a
        browser holding 6 GB does not. It is deliberately **not** read off the
        oomd verdict, which is the tempting shortcut and the wrong one: that
        number is the stall in the parent slice, which holds the browser, the
        desktop and everything else the user is running, so blaming ourselves
        for it would give a foreign shortage our own long deadline. Readings
        taken on cgroups we actually own answer it instead, any one of which
        is enough, and they cover each other's blind spots:

        * The session slice is out of budget. Measured directly, and the
          sharpest signal there is, but it exists only on a machine that can
          make scopes.
        * Our workload cgroup is over its MemoryHigh watermark, or is carrying
          at least half the parent slice's stall. These come from the pressure
          read, which takes them on the sessions slice where there is one and
          on the bot's own cgroup where there is not, so they still answer on
          the fallback path that has no slice to measure.

        Neither can see a shortage, so a machine where nothing is readable
        classifies as not-ours and gets the short deadline. That is the right
        way round: proceeding is the safe default when there is no evidence to
        wait on.
        """
        floor = config.MEM_ADMISSION_MIN_SLICE_HEADROOM_MB
        headroom = self._slice_headroom_mb() if floor > 0 else None
        # None means the slice has no limit, or the files would not read.
        # Treated as no finding, the same rule every pressure reader here
        # follows: a budget check that cannot measure must not refuse work.
        out_of_budget = headroom is not None and headroom < floor
        ours = (
            out_of_budget
            or pressure.over_own_high()
            or bool(pressure.stall_is_ours())
        )
        if pressure.is_critical():
            return AdmissionBlock(
                pressure.human() or "the machine is out of memory", ours,
            )
        if pressure.oomd_at_least(memory.PRESSURE_TIGHT):
            return AdmissionBlock(
                pressure.human() or "this unit is close to being killed", ours,
            )
        # The `headroom is not None` half is implied by out_of_budget and is
        # repeated so the formatting below cannot be read as dividing a None.
        if out_of_budget and headroom is not None:
            return AdmissionBlock(
                f"the session budget has {headroom / 1024:.1f} GB left, "
                f"under the {floor / 1024:.1f} GB needed to start one more",
                ours=True,
            )
        return None

    @staticmethod
    def _slice_headroom_mb() -> float | None:
        """Megabytes left under the session slice's soft ceiling, or None."""
        try:
            return cgroups.slice_headroom_mb()
        except Exception:
            log.debug("Slice headroom read failed", exc_info=True)
            return None

    async def _await_memory_headroom(
        self,
        instance: Instance,
        on_progress: ProgressCallback | None,
    ) -> None:
        """Hold a starting session while the machine is out of memory.

        MAX_CONCURRENT bounds how many sessions run at once and says nothing
        about whether the machine can afford the next one. Before this, the
        bot's only response to a starving machine was to add another process to
        it — which is how five sessions, a browser and a detached compiler
        server came to share 32 GB until the kernel picked a victim.

        Three deliberate properties:

        * It reclaims before it waits. An idle build daemon holding gigabytes
          is usually the whole problem, and giving it up costs nothing.
        * The wait is bounded, then it proceeds anyway with a warning. Pressure
          the bot did not create would otherwise block work forever, and
          refusing all work because a browser is fat is a worse failure than
          starting one more CLI.
        * It holds on CRITICAL, and on TIGHT from the oomd rule alone. Not on
          TIGHT machine-wide: this desktop reads tight most of the day and
          holding on it would train the user to ignore the message. The oomd
          reading is different in kind. TIGHT there means the slice is at 60%
          of the stall percentage that gets the whole unit SIGKILLed, which on
          the 2026-09-21 timeline is minutes of warning before every live
          session dies at once. See MemoryPressure.oomd_level.
        """
        if not config.MEM_ADMISSION_ENABLED:
            return
        pressure = await asyncio.to_thread(self._read_pressure)
        blocked = self._admission_blocked(pressure)
        if not blocked:
            return
        # A hold is one more window with no process to signal, and this file
        # already carries the scar tissue for those: see
        # _stopped_before_spawning. A Kill arriving while a session sits here
        # would find nothing to terminate, evaporate, and the session would
        # spawn minutes later into a thread that already said it had stopped.
        # Returning is enough — _run_impl checks the mark before it spawns.
        if instance.id in self._intentional_kills:
            return

        await self._reclaim_idle_daemons(f"{instance.id} waiting to start")
        pressure = await asyncio.to_thread(self._read_pressure)
        blocked = self._admission_blocked(pressure)
        if not blocked:
            log.info(
                "%s released immediately — reclaim cleared the pressure (%s)",
                instance.id, pressure.summary(),
            )
            return

        log.warning(
            "Holding %s before spawn: %s (%s pressure)",
            instance.id, pressure.summary(), "own" if blocked.ours else "foreign",
        )
        # The same formatter the /wake confirmation uses, so "up to 5 min" and
        # "waited 45s" read the same way everywhere and a sub-minute admission
        # wait cannot render as "0 min".
        from bot.platform.formatting import format_delay_secs

        def _budget_secs(block: AdmissionBlock) -> int:
            # Recomputed every pass rather than fixed at the start, because
            # which kind of shortage this is can change while we wait: a
            # session finishing turns our own slice pressure into a browser's,
            # and the deadline has to follow the shortage rather than the one
            # it happened to open on.
            secs = (
                config.MEM_ADMISSION_OWN_MAX_WAIT_SECS if block.ours
                else config.MEM_ADMISSION_MAX_WAIT_SECS
            )
            return max(0, secs)

        told = False
        last_told = 0.0
        started = asyncio.get_event_loop().time()
        while True:
            now = asyncio.get_event_loop().time()
            if now >= started + _budget_secs(blocked):
                break
            # One message when the hold opens, then a refresh every
            # MEM_ADMISSION_NOTIFY_SECS. An own-pressure hold can legitimately
            # run half an hour, and a thread that says "waiting for memory"
            # once and then goes silent for thirty minutes is indistinguishable
            # from a thread that died.
            # Never more often than we poll: a refresh between two polls would
            # repeat a reading nothing has re-read.
            refresh = max(
                config.MEM_ADMISSION_POLL_SECS, config.MEM_ADMISSION_NOTIFY_SECS,
            )
            due = not told or now - last_told >= refresh
            if due and on_progress:
                budget = format_delay_secs(_budget_secs(blocked))
                waited = int(now - started)
                body = (
                    f"There is no room to start this yet: {blocked.reason}. "
                    f"Starting another session now would make that worse, "
                    f"so this one waits for room (up to {budget}, then it "
                    f"starts anyway)."
                )
                if told:
                    body = (
                        f"Still waiting for memory after "
                        f"{format_delay_secs(waited)}: {blocked.reason}. "
                        f"Holding this session rather than making it worse "
                        f"(up to {budget} from the start, then it starts "
                        f"anyway)."
                    )
                told = True
                last_told = now
                await _notify(on_progress, "Waiting for memory before starting", body, "on memory hold")
            await asyncio.sleep(max(1, config.MEM_ADMISSION_POLL_SECS))
            if instance.id in self._intentional_kills:
                log.info(
                    "Memory hold for %s abandoned — stop requested", instance.id,
                )
                return
            pressure = await asyncio.to_thread(self._read_pressure)
            blocked = self._admission_blocked(pressure)
            if not blocked:
                waited = int(asyncio.get_event_loop().time() - started)
                log.info(
                    "Starting %s after %ds of memory hold (%s)",
                    instance.id, waited, pressure.summary(),
                )
                if told and on_progress:
                    await _notify(
                        on_progress,
                        "Starting now — memory freed up",
                        f"Held for {waited}s. {pressure.summary()}.",
                        "on memory release",
                    )
                return

        # Bounded, so this is a normal outcome and not a failure. Say plainly
        # that the wait was given up on rather than implying it succeeded.
        # Elapsed, not the budget. The budget is recomputed from the *current*
        # shortage and a hold that opened on our own pressure can end on a
        # browser's, so quoting it would report a half-hour wait as five
        # minutes (or the reverse) on exactly the runs worth understanding.
        waited = format_delay_secs(int(asyncio.get_event_loop().time() - started))
        log.warning(
            "Starting %s anyway, still short after %s (%s)",
            instance.id, waited, pressure.summary(),
        )
        if told and on_progress:
            await _notify(
                on_progress,
                "Starting anyway — memory never freed up",
                f"Waited {waited} and "
                f"the machine is still short ({pressure.human()}). Starting "
                f"regardless rather than blocking your work indefinitely — "
                f"but expect this session to be slow, and consider closing "
                f"something.",
                "on memory hold timeout",
            )
