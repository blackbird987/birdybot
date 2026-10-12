"""The per-session watchdog that runs beside ``ClaudeRunner._stream_output``.

One tick every ``config.WATCHDOG_TICK_SECS`` asks three questions in a fixed
order: has a run past its age gone silent (the orphan safety-net), is its
process tree past a memory ceiling or losing a fleet arbitration (the memory
guard), and has it been quiet long enough to warn about (the stall warning,
which never kills). The first two can end the run; when they do they record
why on the watchdog, and ``_stream_output`` reads that verdict after the
reader loop ends.

This used to be a 330-line closure over a dozen ``nonlocal`` variables inside
``_stream_output``. The rules are unchanged; only where they live moved.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from bot import config
from bot.claude import memory
from bot.claude.runner.common import _notify
from bot.claude.runner.reaping import (
    _capture_process_snapshot,
    _fleet_kill_detail,
    _lifetime_kill_detail,
    _memory_kill_detail,
    _memory_warning_detail,
)

if TYPE_CHECKING:
    from bot.claude.types import Instance

log = logging.getLogger(__name__)


class _Watchdog:
    """Age, memory and silence checks for one running CLI process.

    The reader loop owns ``last_output_time``, ``end_of_turn_seen`` and
    ``stall_warned`` (it refreshes the first two from stdout and clears the
    third on output); the tick reads them. Everything named ``lifetime_*`` or
    ``memory_kill*`` is the verdict, written only by a tick that reaped.
    """

    def __init__(self, runner: Any, instance: Instance, proc: Any,
                 events: list[dict], on_progress, on_stall) -> None:
        self._runner = runner
        self._instance = instance
        self._proc = proc
        self._events = events
        self._on_progress = on_progress
        self._on_stall = on_stall

        self.last_output_time = asyncio.get_event_loop().time()
        self.start_time = self.last_output_time
        self.stall_warned = False
        # End-of-turn watchdog: set when the LLM signals stop_reason="end_turn"
        # with no tool_use blocks (genuinely done). The reader loop acts on it.
        self.end_of_turn_seen = False

        self.lifetime_exceeded = False
        # Set alongside lifetime_exceeded so the failure the user reads can say
        # WHICH of the two conditions fired and with what numbers. A message
        # that only says "4h limit" is what made the 2026-08-27 kill look
        # reasonable in the log when it was not.
        self.lifetime_elapsed_secs = 0.0
        self.lifetime_silence_secs = 0.0
        self.lifetime_hard_cap = False
        # Set by the memory guard when it reaps this session's tree. Holds the
        # numbers, not a bool, because the numbers ARE the message: both the
        # failure the user reads and the nudge the resumed attempt gets are
        # useless without them.
        self.memory_kill: memory.TreeMemory | None = None
        self.memory_kill_avail_mb: float | None = None
        # Set only when the reap was a machine-wide arbitration rather than
        # this session breaching its own ceiling. Its presence is what switches
        # every downstream message onto the other explanation, so it is a
        # reading and not a bool: the numbers are the whole content.
        self.memory_kill_pressure: memory.MemoryPressure | None = None

        # Re-log a fresh snapshot every STALL_DIAG_RELOG_SECS while still
        # stalled, which gives a paper trail of CPU/conn state over the silent
        # period so we can tell "thinking with API call open" from "actually
        # hung with nothing in flight" after the fact.
        self._stall_last_logged = 0.0
        # -inf, not 0: the clock here is monotonic, whose zero is an arbitrary
        # boot-relative instant, so 0.0 would only happen to mean "never
        # logged" because the epoch is far in the past.
        self._old_but_alive_logged = float("-inf")
        self._mem_last_checked = 0.0
        self._mem_warned = False

    def stand_down(self) -> None:
        """Forget a reap the run's own ending has overruled.

        Called for a turn that completed anyway, or a kill the user asked
        for: either way the reap is not what the user should read.
        """
        self.memory_kill = None
        self.lifetime_exceeded = False

    async def run(self) -> None:
        while True:
            await asyncio.sleep(config.WATCHDOG_TICK_SECS)
            if await self.tick():
                return

    async def tick(self) -> bool:
        """One pass of the three checks. True when this run was reaped."""
        now = asyncio.get_event_loop().time()
        elapsed_since_output = now - self.last_output_time
        elapsed_since_start = now - self.start_time
        if await self._check_lifetime(now, elapsed_since_start, elapsed_since_output):
            return True
        if await self._check_memory(now):
            return True
        await self._check_stall(now, elapsed_since_start, elapsed_since_output)
        return False

    async def _reap(self, headline: str, detail: str) -> None:
        await self._runner._reap_session(
            self._instance, self._proc, self._on_progress, headline, detail,
        )

    async def _check_lifetime(self, now: float, elapsed_since_start: float,
                              elapsed_since_output: float) -> bool:
        # Safety-net: kill truly orphaned processes.
        #
        # "Orphaned" is a claim about SILENCE, not about age, and for a
        # long time this branch only measured age. On 2026-08-27 that
        # killed a four-hour benchmark run that had produced output
        # five minutes earlier and had not gone quiet for a single
        # minute in its final two hours — it was mid-batch, waiting on
        # subagents, at 350 MB and with live HTTPS connections open.
        # Raising the number would only move the guillotine; a bench
        # that farms work out in serial batches can legitimately run
        # all day. So age is now the point at which we START asking,
        # and continued output is the answer that keeps it alive.
        #
        # The hard cap is the backstop for the other direction: a
        # process that heartbeats forever without ever finishing must
        # not be immortal just because it is noisy.
        if elapsed_since_start > config.MAX_PROCESS_LIFETIME_SECS:
            hard_cap = config.MAX_PROCESS_HARD_LIFETIME_SECS
            hit_hard_cap = (
                hard_cap > 0 and elapsed_since_start > hard_cap
            )
            silent_enough = (
                elapsed_since_output
                >= config.MAX_PROCESS_SILENCE_SECS
            )
            if not silent_enough and not hit_hard_cap:
                # Old is not orphaned. Logged on its own slow cadence
                # so a legitimate all-day run still leaves a trail
                # without writing a line every ten seconds.
                if (
                    now - self._old_but_alive_logged
                    >= config.OLD_BUT_ALIVE_RELOG_SECS
                ):
                    self._old_but_alive_logged = now
                    log.info(
                        "Past lifetime age for %s (%ds) but still "
                        "producing output %ds ago \u2014 letting it work "
                        "(reap after %ds silent)",
                        self._instance.id, int(elapsed_since_start),
                        int(elapsed_since_output),
                        config.MAX_PROCESS_SILENCE_SECS,
                    )
            else:
                self.lifetime_exceeded = True
                self.lifetime_elapsed_secs = elapsed_since_start
                self.lifetime_silence_secs = elapsed_since_output
                self.lifetime_hard_cap = hit_hard_cap
                log.warning(
                    "Lifetime limit for %s \u2014 running for %ds, "
                    "silent for %ds%s",
                    self._instance.id, int(elapsed_since_start),
                    int(elapsed_since_output),
                    " (hard cap)" if hit_hard_cap else "",
                )
                # Told BEFORE the terminate, for the same reason
                # _reap_session says its piece first: terminating
                # closes the CLI's stdout, which ends the reader loop,
                # whose ``finally`` cancels this watchdog. Anything
                # after that call is on a coin flip.
                await _notify(
                    self._on_progress,
                    "Stopped \u2014 this session looked hung",
                    _lifetime_kill_detail(
                        elapsed_since_start,
                        elapsed_since_output,
                        hit_hard_cap,
                    ),
                    "on lifetime kill",
                )
                self._proc.terminate()
                return True
        return False

    async def _check_memory(self, now: float) -> bool:
        # Memory guard. Runs on its own cadence and regardless of
        # whether the session looks stalled: the 2026-08-17 runaway was
        # never stalled, it was streaming tool output the whole way up
        # to 13.7 GB. Checked on every tick that is due, before the
        # stall branch, because a tree at the ceiling is a decision to
        # make now while a silent session is only worth a log line.
        if (
            (config.SESSION_MEM_KILL_MB > 0 or config.SESSION_MEM_WARN_MB > 0)
            and now - self._mem_last_checked >= config.SESSION_MEM_CHECK_SECS
        ):
            self._mem_last_checked = now
            try:
                tree = await asyncio.to_thread(memory.sample_tree, self._proc.pid)
            except Exception:
                # A sampler that can end a session is worse than no
                # sampler. Skip this tick and try again on the next.
                log.debug("Memory sample failed for %s", self._instance.id)
                tree = None

            # The cgroup is the honest number when there is one. The
            # tree walk can only see what is still a descendant, and
            # `dotnet build` deliberately leaves its Roslyn server
            # detached and parented to PID 1 so the next build is
            # faster -- 4.10 GB of it was invisible to this sampler
            # during the 2026-08-21 OOM. A scope keeps charging it.
            #
            # Taken as a maximum rather than a replacement: RSS
            # double-counts pages shared between forks, so on a busy
            # tree the walk can read higher than the cgroup's own
            # accounting, and this is a safety ceiling where the
            # larger of two honest numbers is the safe one. Also
            # keeps the offender label, which memory.current has no
            # way to produce.
            session_cg = self._runner._session_cgroups.get(self._instance.id)
            if tree is not None and session_cg is not None:
                try:
                    charged = await asyncio.to_thread(
                        session_cg.current_mb,
                    )
                except Exception:
                    charged = None
                if charged is not None and charged > tree.total_mb:
                    tree.total_mb = charged
                    if not tree.proc_count:
                        # Nothing left in the process tree but memory
                        # still charged to the cgroup is exactly the
                        # reparented-daemon case, and the ceiling
                        # checks below are gated on proc_count.
                        tree.proc_count = 1
                        tree.biggest_name = "detached (cgroup)"

            if tree is not None and tree.proc_count:
                kill_mb = config.SESSION_MEM_KILL_MB
                warn_mb = config.SESSION_MEM_WARN_MB
                # Shared so the fleet check below — which runs inside
                # a DIFFERENT session's watchdog — can compare trees.
                # No one session can answer "am I the largest", and
                # that is exactly the question a machine-wide crunch
                # asks.
                self._runner._tree_samples[self._instance.id] = tree
                if kill_mb > 0 and tree.total_mb >= kill_mb:
                    self.memory_kill = tree
                    self.memory_kill_avail_mb = await asyncio.to_thread(
                        memory.available_mb,
                    )
                    # The sessions slice where there is one, so the
                    # line reports what the fleet is holding rather
                    # than the supervisor's own ~250 MB.
                    cg = await asyncio.to_thread(
                        lambda: memory.cgroup_memory(
                            self._runner._workload_cgroup(),
                        ),
                    )
                    log.error(
                        "Memory limit for %s \u2014 %s over the %.1fGB "
                        "ceiling; machine has %s free, cgroup anon "
                        "%s of %s. Reaping the tree.",
                        self._instance.id, tree.summary(), kill_mb / 1024,
                        f"{self.memory_kill_avail_mb / 1024:.1f}GB"
                        if self.memory_kill_avail_mb is not None else "?",
                        f"{cg.anon_mb / 1024:.1f}GB"
                        if cg.anon_mb is not None else "?",
                        f"{cg.max_mb / 1024:.1f}GB"
                        if cg.max_mb is not None else "no limit",
                    )
                    await self._reap(
                        "Stopped \u2014 this session ran out of memory",
                        _memory_kill_detail(tree, kill_mb),
                    )
                    return True

                # Cross-session arbitration: nobody is over their own
                # ceiling, but the machine has run out anyway. This is
                # the case the per-session guard structurally cannot
                # see, and the one that actually happened — five
                # reasonable sessions plus a browser, none of them
                # individually at fault, and the kernel picking the
                # victim in the end.
                #
                # Both conditions are required before anything dies.
                # Machine-critical alone is not enough: under pressure
                # the bot did not create, reaping our own sessions
                # frees memory the real offender immediately re-takes,
                # and charges a session's work for it. Over-our-own-
                # MemoryHigh alone is not enough either — that is a
                # throttle watermark, crossed routinely, and on its own
                # it means the cgroup is working as designed.
                fleet_verdict = await self._runner._fleet_arbitration(
                    self._instance.id, tree,
                )
                if fleet_verdict is not None:
                    self.memory_kill = tree
                    self.memory_kill_pressure = fleet_verdict
                    self.memory_kill_avail_mb = fleet_verdict.avail_mb
                    log.error(
                        "Machine out of memory (%s) \u2014 %s is the "
                        "largest of %d live sessions at %s. Reaping "
                        "it rather than leaving the kernel to choose.",
                        fleet_verdict.summary(), self._instance.id,
                        sum(1 for iid in self._runner._tree_samples
                            if iid in self._runner._processes),
                        tree.summary(),
                    )
                    await self._reap(
                        "Stopped \u2014 the machine ran out of memory",
                        _fleet_kill_detail(tree, fleet_verdict),
                    )
                    return True

                if warn_mb > 0 and tree.total_mb >= warn_mb and not self._mem_warned:
                    self._mem_warned = True
                    log.warning(
                        "Memory warning for %s \u2014 %s (warn at %.1fGB, "
                        "kill at %.1fGB)",
                        self._instance.id, tree.summary(), warn_mb / 1024,
                        kill_mb / 1024 if kill_mb > 0 else 0,
                    )
                    # Told to the session, not just the log: while it is
                    # still running it can still choose a smaller batch.
                    # After the kill that choice is gone.
                    await _notify(
                        self._on_progress,
                        "Heads up \u2014 this session is using a lot of memory",
                        _memory_warning_detail(tree, kill_mb),
                        "on memory warning",
                    )
        return False

    async def _check_stall(self, now: float, elapsed_since_start: float,
                           elapsed_since_output: float) -> None:
        # Stall warning (no auto-kill)
        if elapsed_since_output > config.STALL_TIMEOUT_SECS:
            is_first = not self.stall_warned
            should_log = is_first or (
                now - self._stall_last_logged >= config.STALL_DIAG_RELOG_SECS
            )
            if should_log:
                snapshot = await _capture_process_snapshot(
                    self._proc.pid, self._events, self.end_of_turn_seen,
                )
                log.warning(
                    "Stall %s: elapsed=%ds since_output=%ds | %s",
                    self._instance.id, int(elapsed_since_start),
                    int(elapsed_since_output), snapshot.summary_line(),
                )
                self._stall_last_logged = now
                if is_first:
                    self.stall_warned = True
                    if self._on_stall:
                        try:
                            await self._on_stall(self._instance.id, snapshot)
                        except Exception:
                            log.exception("Stall callback error")

