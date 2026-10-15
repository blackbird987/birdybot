"""How many sessions may run at once, given the plan's real usage figures.

MAX_CONCURRENT is a count and knows nothing about the subscription. On
2026-10-15 every five-hour window ran out 9, 8, 4 and then 3 minutes after it
opened: the whole parked backlog started in the same minute, and every one of
them resumed a long conversation whose prompt cache had expired during the
wait, so each paid for re-reading its entire history before doing any work.
The limit is not exponential; starting everything at once is.

This module only decides the number. The runner holds a starting session while
that many are already running (``AdmissionMixin._await_usage_headroom``) and the cooldown loop
in ``app.py`` will not release a parked retry into a full house.

Rules that must not drift:

- **The cap never drops below one.** One session can always run, so a hold
  only ever waits for a running session to finish and can never deadlock.
- **No reading means no usage cap.** The figures come from an undocumented
  endpoint and the access token expires between runs (plan_usage never
  refreshes it). Missing data must not stop work; the peak-hour cap, which
  needs no reading, still applies.
- **A reading whose window has already reset counts as zero.** plan_usage
  serves the last good reading for up to three hours, and a session figure
  from before the reset says nothing about the window in force now.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from bot import config

_PACIFIC = ZoneInfo("America/Los_Angeles")
_WEEK = timedelta(days=7)


class UsageReading(Protocol):
    """The fields of ``plan_usage.AccountUsage`` this module reads."""

    session_pct: float | None
    session_resets_at: datetime | None
    week_pct: float | None
    week_resets_at: datetime | None


@dataclass(frozen=True)
class PaceDecision:
    limit: int
    reason: str = ""  # plain-language cause of the tightest cap, "" when uncapped


def parse_tiers(spec: str) -> list[tuple[float, int]]:
    """``"40:3,65:2,85:1"`` -> ``[(40.0, 3), (65.0, 2), (85.0, 1)]``. Bad pairs are skipped."""
    tiers: list[tuple[float, int]] = []
    for part in (spec or "").split(","):
        pct, _, cap = part.strip().partition(":")
        try:
            tiers.append((float(pct), max(1, int(cap))))
        except ValueError:
            continue
    return sorted(tiers)


def _peak_hours() -> tuple[int, int] | None:
    start, _, end = (config.USAGE_PACE_PEAK_HOURS_PT or "").partition("-")
    try:
        lo, hi = int(start), int(end)
    except ValueError:
        return None
    return (lo, hi) if 0 <= lo < hi <= 24 else None


def is_peak(now: datetime) -> bool:
    """Weekday peak hours, Pacific time, when Anthropic drains session limits faster."""
    hours = _peak_hours()
    if hours is None:
        return False
    local = now.astimezone(_PACIFIC)
    return local.weekday() < 5 and hours[0] <= local.hour < hours[1]


def _live_pct(pct: float | None, resets_at: datetime | None, now: datetime) -> float | None:
    if pct is None:
        return None
    if resets_at is not None and resets_at <= now:
        return 0.0
    return pct


def week_allowance_pct(week_resets_at: datetime | None, now: datetime) -> float | None:
    """Weekly % it is fine to have used by now: the share of the week gone, plus slack."""
    if week_resets_at is None:
        return None
    remaining = (week_resets_at - now) / _WEEK
    elapsed = min(1.0, max(0.0, 1.0 - remaining))
    return elapsed * 100 + config.USAGE_PACE_WEEK_SLACK_PCT


def pace_limit(
    reading: UsageReading | None,
    now: datetime | None = None,
    max_concurrent: int | None = None,
) -> PaceDecision:
    """The number of sessions that may run at once right now, and why."""
    now = now or datetime.now(timezone.utc)
    ceiling = max(1, max_concurrent if max_concurrent is not None else config.MAX_CONCURRENT)
    if not config.USAGE_PACE_ENABLED:
        return PaceDecision(ceiling)

    caps: list[tuple[int, str]] = []
    if is_peak(now):
        caps.append((config.USAGE_PACE_PEAK_CAP, "Anthropic's peak hours"))

    if reading is not None:
        session = _live_pct(reading.session_pct, reading.session_resets_at, now)
        if session is not None:
            for pct, cap in parse_tiers(config.USAGE_PACE_SESSION_TIERS):
                if session >= pct:
                    caps.append((cap, f"session usage at {session:.0f}%"))

        week = _live_pct(reading.week_pct, reading.week_resets_at, now)
        if week is not None:
            if week >= config.USAGE_PACE_WEEK_HIGH_PCT:
                caps.append((1, f"weekly usage at {week:.0f}%"))
            allowance = week_allowance_pct(reading.week_resets_at, now)
            if allowance is not None and week > allowance:
                caps.append((
                    config.USAGE_PACE_WEEK_AHEAD_CAP,
                    f"weekly usage at {week:.0f}%, ahead of the {allowance:.0f}% "
                    f"the week so far allows",
                ))

    if not caps:
        return PaceDecision(ceiling)
    cap, reason = min(caps, key=lambda c: c[0])
    limit = max(1, min(ceiling, cap))
    return PaceDecision(limit, reason if limit < ceiling else "")
