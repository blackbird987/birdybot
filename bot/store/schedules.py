"""Schedules, self-wakes, declared nudges and watches.

A mixin of ``StateStore`` (bot/store/state.py), which owns the dicts these
methods read and the persistence that saves them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.claude.types import Schedule, Watch


class SchedulesMixin:
    # --- Schedules ---

    def add_schedule(self, prompt: str, interval_secs: int | None = None,
                     run_at: str | None = None, mode: str = "explore") -> Schedule:
        self._schedule_counter += 1
        sid = f"sch-{self._schedule_counter:03d}"
        repo_name, repo_path = self.get_active_repo()
        sched = Schedule(
            id=sid,
            prompt=prompt,
            repo_name=repo_name or "",
            repo_path=repo_path or "",
            mode=mode,
            interval_secs=interval_secs,
            run_at=run_at,
            is_recurring=interval_secs is not None,
        )
        # Calculate next_run_at
        now = datetime.now(timezone.utc)
        if interval_secs:
            sched.next_run_at = (now + timedelta(seconds=interval_secs)).isoformat()
        elif run_at:
            sched.next_run_at = run_at
        self._schedules[sid] = sched
        self.save()
        return sched

    def add_wake(self, prompt: str, channel_id: str, next_run_at: str,
                 repo_name: str = "", repo_path: str = "") -> Schedule:
        """Register a one-shot, thread-bound self-wake.

        Unlike ``add_schedule`` (which spawns a fresh instance and broadcasts to
        The Ark), a wake is pinned to ``channel_id`` and fires by resuming that
        thread's session via ``_replay_to_thread`` — see
        ``Scheduler._execute_schedule`` and ``check_wake_request``.

        Invariant: at most one pending *one-shot* wake per thread. Any existing
        one for this channel is superseded so interleaved turns or a busy re-arm
        racing an active turn can't accumulate multiple pollers. Recurring
        thread-bound rows (nudges, see bot.engine.nudges) are deliberately
        exempt: they are declared config, not ephemera, and a turn that happens
        to arm its own wake must not silently delete the thread's nudge. Superseded wakes
        are deleted (not just disabled) — they're machine-generated ephemera with
        no post-supersede value, so lingering disabled rows would bloat state.json
        for a heavily-polling bot.
        """
        stale = [sid for sid, s in self._schedules.items()
                 if s.resume_thread and s.channel_id == channel_id and s.enabled
                 and not s.is_recurring]
        for sid in stale:
            del self._schedules[sid]
        self._schedule_counter += 1
        sid = f"sch-{self._schedule_counter:03d}"
        sched = Schedule(
            id=sid,
            prompt=prompt,
            repo_name=repo_name or "",
            repo_path=repo_path or "",
            is_recurring=False,
            next_run_at=next_run_at,
            resume_thread=True,
            channel_id=channel_id,
        )
        self._schedules[sid] = sched
        self.save()
        return sched

    def upsert_nudge(self, label: str, prompt: str, channel_id: str,
                     interval_secs: int, next_run_at: str,
                     repo_name: str = "", repo_path: str = "") -> tuple[Schedule, str]:
        """Create or update the declared nudge identified by ``label``.

        Idempotent by design: this runs on every startup off
        ``config/nudges.json``, so it must converge rather than accumulate.
        Matching is on ``label``, never on the generated id, so editing a
        nudge's wording updates the existing row instead of leaving an orphan
        firing the old text.

        ``next_run_at`` is always re-anchored from the caller's freshly computed
        value (the next wall-clock occurrence of the nudge's time, in its own
        timezone). Preserving the stored value instead would look safer but
        drifts: stepping a UTC instant by 86400s crosses a DST boundary an hour
        wrong, and the check-in this whole workspace targets is the week DST
        ends. Recomputing is idempotent ("next occurrence strictly in the
        future" is the same answer however often it is asked), so a restart can
        neither delay a nudge nor double-fire one.

        Returns ``(schedule, action)`` where action is "created", "updated" or
        "unchanged", so startup can log what it actually did.
        """
        existing = next((sc for sc in self._schedules.values()
                         if sc.label == label), None)
        if existing is not None:
            action = "unchanged"
            if (existing.prompt != prompt
                    or existing.channel_id != channel_id
                    or existing.interval_secs != interval_secs
                    or not existing.enabled
                    or not existing.resume_thread
                    or not existing.is_recurring):
                action = "updated"
            existing.prompt = prompt
            existing.channel_id = channel_id
            existing.interval_secs = interval_secs
            existing.repo_name = repo_name
            existing.repo_path = repo_path
            existing.resume_thread = True
            existing.is_recurring = True
            existing.enabled = True
            existing.next_run_at = next_run_at
            self.save()
            return existing, action

        self._schedule_counter += 1
        sid = f"sch-{self._schedule_counter:03d}"
        sched = Schedule(
            id=sid,
            prompt=prompt,
            repo_name=repo_name or "",
            repo_path=repo_path or "",
            interval_secs=interval_secs,
            is_recurring=True,
            next_run_at=next_run_at,
            resume_thread=True,
            channel_id=channel_id,
            label=label,
        )
        self._schedules[sid] = sched
        self.save()
        return sched, "created"

    def delete_nudges_except(self, labels: set[str]) -> list[str]:
        """Drop declared nudges whose label is no longer in config/nudges.json.

        Without this, deleting an entry from the config would leave it firing
        forever, since nothing else ever touches a labelled row. Only labelled
        rows are considered, so user cron jobs and one-shot wakes are untouched.
        """
        gone = [sid for sid, sc in self._schedules.items()
                if sc.label and sc.label not in labels]
        for sid in gone:
            del self._schedules[sid]
        if gone:
            self.save()
        return gone

    def get_schedule(self, sid: str) -> Schedule | None:
        return self._schedules.get(sid)

    def delete_schedule(self, sid: str) -> bool:
        if sid in self._schedules:
            del self._schedules[sid]
            self.save()
            return True
        return False

    def list_schedules(self) -> list[Schedule]:
        return [s for s in self._schedules.values() if s.enabled]

    def update_schedule(self, sched: Schedule) -> None:
        self._schedules[sched.id] = sched
        self.save()

    # --- Watches (event-triggered self-wakes) -----------------------------

    def add_watch(self, watch: Watch) -> Watch:
        """Arm an event-triggered self-wake for ``watch.channel_id``.

        Same one-per-thread invariant as ``add_wake``: a thread has a single
        thing it is waiting on, so a fresh watch supersedes any existing one
        rather than accumulating pollers that would each fire a resume.
        """
        for wid in [w.id for w in self._watches.values()
                    if w.channel_id == watch.channel_id]:
            del self._watches[wid]
        self._watch_counter += 1
        watch.id = f"w-{self._watch_counter:03d}"
        self._watches[watch.id] = watch
        self.save()
        return watch

    def cancel_wakes(self, channel_id: str) -> int:
        """Drop any pending self-wake for ``channel_id``. Returns how many.

        A thread waits on ONE thing. ``add_wake`` already supersedes wakes with
        wakes; this is the cross-mechanism half — arming a watch must retire a
        clock-based wake left over from an earlier turn, or the timer fires
        while the job it replaced is still running.
        """
        stale = [sid for sid, s in self._schedules.items()
                 if s.resume_thread and s.channel_id == channel_id]
        for sid in stale:
            del self._schedules[sid]
        if stale:
            self.save()
        return len(stale)

    def pending_wake_for_channel(self, channel_id: str) -> Schedule | None:
        """The pending self-wake bound to ``channel_id``, if any.

        The read half of ``add_wake``'s one-wake-per-thread invariant. Callers
        that want to know whether a thread already has something to resume it
        ask here rather than scanning schedules themselves — a wake is a
        ``resume_thread`` Schedule, which is not obvious from the outside.
        """
        for sched in self._schedules.values():
            if sched.resume_thread and sched.channel_id == channel_id and sched.enabled:
                return sched
        return None

    def list_watches(self) -> list[Watch]:
        return list(self._watches.values())

    def get_watch(self, wid: str) -> Watch | None:
        return self._watches.get(wid)

    def watch_for_channel(self, channel_id: str) -> Watch | None:
        for w in self._watches.values():
            if w.channel_id == channel_id:
                return w
        return None

    def update_watch(self, watch: Watch) -> None:
        self._watches[watch.id] = watch
        self.save()

    def delete_watch(self, wid: str) -> bool:
        if wid in self._watches:
            del self._watches[wid]
            self.save()
            return True
        return False
