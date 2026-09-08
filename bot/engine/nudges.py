"""Declared nudges: recurring messages the bot sends into a thread unprompted.

A nudge is the opposite of everything else the scheduler does. A `/schedule`
cron spawns a fresh session against the active repo and broadcasts its summary
back to the operator; a self-wake resumes one thread, once, because a turn
asked it to. Neither can start a conversation with somebody else on a rhythm,
which is what a coaching workspace actually needs: the person being coached is
precisely the person who will not open the app on their own.

So a nudge is a *recurring* thread-bound wake, and it is declared in
``config/nudges.json`` rather than created by a command. That choice is the
point of this module:

* It survives. A schedule created by hand lives only in ``data/state.json``,
  which is gitignored runtime state. One reset and the habit engine is gone
  silently, and silence is indistinguishable from "he didn't reply".
* It is reviewable. The exact wording the bot will send to a real person sits
  in the repo, in a diff, instead of buried in a JSON blob of runtime state.
* It converges. Reconciliation is idempotent and runs on every startup, so
  editing a prompt updates the live nudge and deleting an entry removes it.

Nothing here decides *what* to say beyond the configured prompt. The prompt is
delivered to the target thread's session exactly like a self-wake, so the
project's own instructions (for vin-coach, its CLAUDE.md) govern the reply.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

_DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3,
         "fri": 4, "sat": 5, "sun": 6}


class NudgeConfigError(ValueError):
    """A nudges.json entry is unusable. Raised with the offending label."""


def next_occurrence(at: str, days: list[str] | None, tz: str,
                    now: datetime | None = None) -> datetime:
    """First UTC instant matching wall-clock ``at`` in ``tz``, strictly future.

    ``days`` restricts to weekdays (``["thu"]`` for a weekly nudge); ``None``
    means every day. Computed in local time and converted, so the nudge stays
    at the hour a person actually recognises across a DST change rather than
    sliding by an hour in late October.
    """
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise NudgeConfigError(f"unknown timezone {tz!r}") from exc
    try:
        hh, mm = (int(part) for part in at.split(":", 1))
        target = time(hh, mm)
    except (ValueError, TypeError) as exc:
        raise NudgeConfigError(f"bad time {at!r}, want HH:MM") from exc

    wanted = None
    if days:
        try:
            wanted = {_DAYS[d.strip().lower()[:3]] for d in days}
        except KeyError as exc:
            raise NudgeConfigError(f"bad weekday in {days!r}") from exc
        if not wanted:
            raise NudgeConfigError("days was empty; use null for every day")

    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone(zone)
    # 8 days covers "today already passed" plus a full week of weekday misses.
    for offset in range(9):
        day = (local_now + timedelta(days=offset)).date()
        if wanted is not None and day.weekday() not in wanted:
            continue
        candidate = datetime.combine(day, target, tzinfo=zone)
        if candidate.astimezone(timezone.utc) > now:
            return candidate.astimezone(timezone.utc)
    raise NudgeConfigError(f"no occurrence of {at} on {days} within 8 days")


def load(path: Path) -> list[dict]:
    """Parse and validate ``config/nudges.json``. Returns entries, never None.

    A missing file is normal (no nudges configured) and yields an empty list.
    A malformed one raises, because silently sending nothing is the exact
    failure this whole module exists to prevent.
    """
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("nudges", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise NudgeConfigError("nudges.json must hold a list of nudges")

    seen: set[str] = set()
    out: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise NudgeConfigError(f"entry is not an object: {entry!r}")
        if entry.get("enabled") is False:
            continue
        label = str(entry.get("label") or "").strip()
        if not label:
            raise NudgeConfigError("every nudge needs a unique 'label'")
        if label in seen:
            raise NudgeConfigError(f"duplicate label {label!r}")
        seen.add(label)
        for field in ("thread_id", "prompt", "at"):
            if not str(entry.get(field) or "").strip():
                raise NudgeConfigError(f"{label}: missing '{field}'")
        every = entry.get("every_days", 1)
        if not isinstance(every, int) or every < 1:
            raise NudgeConfigError(f"{label}: 'every_days' must be >= 1")
        days = entry.get("days")
        if days is not None and not isinstance(days, list):
            raise NudgeConfigError(f"{label}: 'days' must be a list or null")
        # Resolve the schedule here so a bad time, weekday or zone fails the
        # WHOLE file rather than silently dropping one nudge during reconcile.
        # Atomic is the safer half of the tradeoff: a config that half-applies
        # means somebody stops getting messages and nobody finds out.
        try:
            next_occurrence(str(entry["at"]).strip(), days,
                            str(entry.get("tz") or "UTC").strip())
        except NudgeConfigError as exc:
            raise NudgeConfigError(f"{label}: {exc}") from exc
        out.append({
            "label": label,
            "thread_id": str(entry["thread_id"]).strip(),
            "prompt": str(entry["prompt"]).strip(),
            "at": str(entry["at"]).strip(),
            "tz": str(entry.get("tz") or "UTC").strip(),
            "days": days,
            "every_days": every,
            "repo": str(entry.get("repo") or "").strip(),
        })
    return out


def reconcile(store, path: Path, now: datetime | None = None) -> dict:
    """Bring live schedules in line with ``config/nudges.json``.

    Returns a summary dict for logging. Never raises on a bad config: a typo in
    one nudge must not stop the bot from booting, so the error is logged loudly
    and the existing rows are left exactly as they are. Leaving them alone (as
    opposed to clearing them) is deliberate: the last known-good nudges keep
    firing while the config is broken.
    """
    try:
        entries = load(path)
    except Exception as exc:
        log.error("nudges.json is unusable, leaving live nudges untouched: %s",
                  exc)
        return {"error": str(exc), "created": 0, "updated": 0, "removed": 0}

    repos = store.list_repos()
    summary = {"created": 0, "updated": 0, "unchanged": 0, "removed": 0}
    labels: set[str] = set()
    for entry in entries:
        try:
            first = next_occurrence(entry["at"], entry["days"], entry["tz"],
                                    now=now)
        except NudgeConfigError as exc:
            log.error("nudge %s skipped: %s", entry["label"], exc)
            continue
        repo_name = entry["repo"]
        repo_path = repos.get(repo_name, "") if repo_name else ""
        if repo_name and not repo_path:
            log.warning("nudge %s names unknown repo %r; firing without one",
                        entry["label"], repo_name)
        _, action = store.upsert_nudge(
            label=entry["label"],
            prompt=entry["prompt"],
            channel_id=entry["thread_id"],
            interval_secs=entry["every_days"] * 86400,
            next_run_at=first.isoformat(),
            repo_name=repo_name,
            repo_path=repo_path,
        )
        labels.add(entry["label"])
        summary[action] = summary.get(action, 0) + 1
        log.info("nudge %s %s: next %s -> thread %s",
                 entry["label"], action, first.isoformat()[:16],
                 entry["thread_id"])

    removed = store.delete_nudges_except(labels)
    summary["removed"] = len(removed)
    if removed:
        log.info("removed %d nudge(s) no longer in nudges.json: %s",
                 len(removed), ", ".join(removed))
    return summary
