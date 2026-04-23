"""Notifier for Anthropic usage-limit windows (weekdays 5am–11am PT)."""

from __future__ import annotations

import asyncio
import logging
import zoneinfo
from datetime import datetime, timedelta, timezone

import discord

log = logging.getLogger(__name__)

_PT = zoneinfo.ZoneInfo("America/Los_Angeles")
_START_HOUR = 5   # 5am PT
_END_HOUR = 11    # 11am PT

_START_MSG = (
    "⚠️ Anthropic usage limits are now active (5am–11am PT). "
    "Avoid starting new Claude sessions — heavy throttling is in effect until 11am PT."
)
_END_MSG = "✅ Anthropic usage limits lifted — it's past 11am PT. You can work freely now."


def is_usage_limit_active(now_utc: datetime | None = None) -> bool:
    """True during weekday 5am-11am PT."""
    now_pt = (now_utc or datetime.now(timezone.utc)).astimezone(_PT)
    return now_pt.weekday() < 5 and _START_HOUR <= now_pt.hour < _END_HOUR


def next_window_end_utc(now_utc: datetime | None = None) -> datetime:
    """UTC datetime of the next 11am PT boundary.

    If currently inside the weekday window, returns today's 11am PT.
    Otherwise returns the next weekday's 11am PT.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    now_pt = now_utc.astimezone(_PT)
    end_today = now_pt.replace(hour=_END_HOUR, minute=0, second=0, microsecond=0)
    if now_pt.weekday() < 5 and now_pt < end_today:
        return end_today.astimezone(timezone.utc)
    d = now_pt.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, _END_HOUR, 0, 0, tzinfo=_PT).astimezone(timezone.utc)


def _next_boundary(now_pt: datetime) -> tuple[datetime, str]:
    """Return the next start/end boundary datetime (in PT) and its message."""
    today_start = now_pt.replace(hour=_START_HOUR, minute=0, second=0, microsecond=0)
    today_end = now_pt.replace(hour=_END_HOUR, minute=0, second=0, microsecond=0)
    is_weekday = now_pt.weekday() < 5  # 0=Mon … 4=Fri

    if is_weekday:
        if now_pt <= today_start:
            return today_start, _START_MSG
        if now_pt <= today_end:
            return today_end, _END_MSG

    # After 11am on weekday, or weekend — find next weekday 5am.
    # Advance by calendar date then construct the target datetime explicitly,
    # making it clear we want exactly 5am on a specific PT calendar day.
    d = now_pt.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, _START_HOUR, 0, 0, tzinfo=_PT), _START_MSG


async def usage_limit_notifier_loop(bot: discord.Client, user_id: int) -> None:
    """Runs forever, DMing user_id at each 5am/11am PT boundary on weekdays."""
    log.info("Usage limit notifier started (user_id=%s)", user_id)
    while True:
        now_utc = datetime.now(timezone.utc)
        now_pt = now_utc.astimezone(_PT)
        next_pt, message = _next_boundary(now_pt)

        sleep_secs = (next_pt.astimezone(timezone.utc) - now_utc).total_seconds()
        if sleep_secs > 0:
            h, m = divmod(int(sleep_secs) // 60, 60)
            log.info(
                "Usage notifier: next DM at %s PT (in %dh %dm)",
                next_pt.strftime("%H:%M"), h, m,
            )
            await asyncio.sleep(sleep_secs)

        try:
            user = await bot.fetch_user(user_id)
            await user.send(message)
            log.info("Usage limit DM sent to %s", user_id)
        except Exception:
            log.exception("Usage notifier: failed to DM user %s", user_id)

        await asyncio.sleep(60)  # buffer — prevents re-firing at same boundary
