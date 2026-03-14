"""Recurring task scheduler with smart diffing."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable

from bot.claude.types import InstanceStatus, InstanceType, Schedule

if TYPE_CHECKING:
    from bot.claude.runner import ClaudeRunner
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


class Scheduler:
    """Manages scheduled/recurring tasks on an asyncio loop."""

    def __init__(
        self,
        store: StateStore,
        runner: ClaudeRunner,
        on_result: Callable | None = None,
    ) -> None:
        self._store = store
        self._runner = runner
        self._on_result = on_result  # async callback(instance, result, changed)
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("Scheduler started")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        log.info("Scheduler stopped")

    async def _loop(self) -> None:
        """Check schedules every 30 seconds."""
        while self._running:
            try:
                await self._check_schedules()
            except Exception:
                log.exception("Scheduler error")
            await asyncio.sleep(30)

    async def _check_schedules(self) -> None:
        self._store.reload_if_changed()
        now = datetime.now(timezone.utc)
        for sched in self._store.list_schedules():
            if not sched.next_run_at:
                continue
            try:
                next_run = datetime.fromisoformat(sched.next_run_at)
            except (ValueError, TypeError):
                continue

            if now >= next_run:
                await self._execute_schedule(sched)

    async def _execute_schedule(self, sched: Schedule) -> None:
        log.info("Executing schedule %s: %s", sched.id, sched.prompt[:60])

        # Create instance
        instance = self._store.create_instance(
            instance_type=InstanceType.SCHEDULED,
            prompt=sched.prompt,
            mode=sched.mode,
            schedule_id=sched.id,
        )
        # Override repo from schedule
        instance.repo_name = sched.repo_name
        instance.repo_path = sched.repo_path

        instance.status = InstanceStatus.RUNNING
        self._store.update_instance(instance)

        # Run (pass pinned context so scheduled tasks also get it)
        result = await self._runner.run(instance, context=self._store.context)

        # Update instance
        instance.session_id = result.session_id
        instance.cost_usd = result.cost_usd
        instance.duration_ms = result.duration_ms
        instance.finished_at = datetime.now(timezone.utc).isoformat()

        if result.is_error:
            instance.status = InstanceStatus.FAILED
            instance.error = result.error_message or result.result_text
        else:
            instance.status = InstanceStatus.COMPLETED

        self._store.update_instance(instance)

        # Track cost
        if result.cost_usd:
            self._store.add_cost(result.cost_usd)

        # Smart diffing: compare to last run
        changed = True
        if instance.summary and sched.last_summary:
            changed = instance.summary != sched.last_summary

        # Update schedule
        sched.last_run_at = datetime.now(timezone.utc).isoformat()
        sched.last_summary = instance.summary

        if sched.is_recurring and sched.interval_secs:
            sched.next_run_at = (
                datetime.now(timezone.utc) + timedelta(seconds=sched.interval_secs)
            ).isoformat()
        else:
            # One-shot: disable
            sched.enabled = False
            sched.next_run_at = None

        self._store.update_schedule(sched)

        # Notify
        if self._on_result:
            try:
                await self._on_result(instance, result, changed)
            except Exception:
                log.exception("Schedule result callback error")

    def recalculate_next_runs(self) -> None:
        """On startup, recalculate next run times from last execution."""
        now = datetime.now(timezone.utc)
        for sched in self._store.list_schedules():
            if not sched.is_recurring or not sched.interval_secs:
                continue
            if sched.last_run_at:
                try:
                    last = datetime.fromisoformat(sched.last_run_at)
                    next_run = last + timedelta(seconds=sched.interval_secs)
                    # If we missed runs, schedule for now
                    if next_run < now:
                        next_run = now + timedelta(seconds=30)
                    sched.next_run_at = next_run.isoformat()
                except (ValueError, TypeError):
                    sched.next_run_at = (now + timedelta(seconds=30)).isoformat()
            elif not sched.next_run_at:
                sched.next_run_at = (
                    now + timedelta(seconds=sched.interval_secs)
                ).isoformat()
            self._store.update_schedule(sched)
