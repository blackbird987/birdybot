"""Quiet hours: the owner is asleep, so a turn should not stop on a question.

Inside the window, and only while the owner has not been seen recently, owner
sessions get ``config.QUIET_HOURS_BLOCK`` at the top of their turn. The block
tells them to decide what can be undone, act, and list those calls under
"Decided while you were away:", which the Needs you list then surfaces.

Presence beats the clock: an owner typing at 03:30 is awake, so the block is
not sent while they were seen in the last ``PRESENCE_GRACE_S``. Only the owner
counts, by author id; a guest or a test webhook never does.

State lives in platform_state under ``quiet``: ``window`` (a /quiet override)
and ``owner_seen`` (epoch seconds). docs/design/quiet-hours.md
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from bot import config
from bot.claude.types import InstanceOrigin

log = logging.getLogger(__name__)

# How recently the owner must have been seen to count as awake.
PRESENCE_GRACE_S = 20 * 60

_STATE_KEY = "quiet"
_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")

# Turns that talk to the owner and could end on a question. Read-only
# reviewers, mechanical steps and the machine link keep their own formats.
QUIET_ORIGINS = frozenset({
    InstanceOrigin.DIRECT, InstanceOrigin.PLAN, InstanceOrigin.APPLY_REVISIONS,
    InstanceOrigin.PLAN_CHECK_APPLY, InstanceOrigin.BUILD, InstanceOrigin.BG,
    InstanceOrigin.VERIFY, InstanceOrigin.RETRY,
})


class Window(NamedTuple):
    start: int  # minutes after local midnight
    end: int


def parse_window(text: str | None) -> Window | None:
    """``"03:00-09:00"`` to a Window. ``off``, empty or malformed is None.

    A window may cross midnight (``23:00-07:00``). Equal ends mean no window.
    """
    if not text:
        return None
    m = _WINDOW_RE.match(text)
    if not m:
        return None
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if start == end:
        return None
    return Window(start, end)


def format_window(w: Window) -> str:
    return f"{w.start // 60:02d}:{w.start % 60:02d}-{w.end // 60:02d}:{w.end % 60:02d}"


def _now() -> datetime:
    """The clock, one place for the harness to move it."""
    return datetime.now(timezone.utc)


def _owner_tz():
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(config.OWNER_TZ)
    except Exception:
        return timezone.utc


def in_window(local: datetime, w: Window) -> bool:
    minute = local.hour * 60 + local.minute
    if w.start < w.end:
        return w.start <= minute < w.end
    return minute >= w.start or minute < w.end


def window_end(local: datetime, w: Window) -> datetime:
    """The next local time the window closes, at or after ``local``."""
    end = local.replace(hour=w.end // 60, minute=w.end % 60, second=0, microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end


def is_quiet(now: datetime, w: Window | None, last_seen: float | None) -> bool:
    """Pure: inside the window and the owner not seen in the grace period.

    ``now`` must be timezone-aware and already in the owner's zone.
    """
    if w is None or not in_window(now, w):
        return False
    if last_seen is not None and now.timestamp() - last_seen < PRESENCE_GRACE_S:
        return False
    return True


# --- State -----------------------------------------------------------------

def _state(store) -> dict:
    data = store.get_platform_state(_STATE_KEY)
    return data if isinstance(data, dict) else {}


def note_owner_seen(store, now: float | None = None) -> None:
    """Record that the owner just did something. Owner only: the caller checks.

    Marked dirty rather than saved: the 60s autosave is plenty for a value
    with a ``PRESENCE_GRACE_S`` grace, and this runs on every owner message and tap.
    """
    try:
        data = dict(_state(store))
        data["owner_seen"] = float(now if now is not None else time.time())
        store.set_platform_state(_STATE_KEY, data, persist=False)
        store.mark_dirty()
    except Exception:
        log.debug("could not record owner presence", exc_info=True)


def owner_seen(store) -> float | None:
    v = _state(store).get("owner_seen")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def window_text(store) -> str:
    """The active window setting: a /quiet override, else config."""
    override = _state(store).get("window")
    return str(override) if override is not None else config.QUIET_HOURS


def set_window(store, text: str | None) -> None:
    """Store a /quiet override (``"off"`` disables). None clears it."""
    data = dict(_state(store))
    if text is None:
        data.pop("window", None)
    else:
        data["window"] = text
    store.set_platform_state(_STATE_KEY, data, persist=True)


def quiet_until(store, now: datetime | None = None) -> datetime | None:
    """The local time quiet hours end, if they apply right now, else None."""
    try:
        w = parse_window(window_text(store))
        local = (now or _now()).astimezone(_owner_tz())
        if not is_quiet(local, w, owner_seen(store)):
            return None
        return window_end(local, w)
    except Exception:
        log.debug("quiet hours check failed", exc_info=True)
        return None


def quiet_block(store, instance, *, confined: bool, now: datetime | None = None) -> str:
    """The block for this turn, or '' when quiet hours do not apply to it.

    Owner sessions only: never a guest, a confined run, or the machine link.
    """
    if not config.QUIET_HOURS_BLOCK:
        return ""
    if not getattr(instance, "is_owner_session", False) or confined:
        return ""
    if getattr(instance, "origin", None) not in QUIET_ORIGINS:
        return ""
    until = quiet_until(store, now)
    if until is None:
        return ""
    return config.QUIET_HOURS_BLOCK.format(until=until.strftime("%H:%M"))


def status_text(store, now: datetime | None = None) -> str:
    """One paragraph for /quiet with no argument."""
    setting = window_text(store)
    w = parse_window(setting)
    if w is None:
        return "Quiet hours are off. Set them with `/quiet 03:00-09:00`."
    local = (now or _now()).astimezone(_owner_tz())
    seen = owner_seen(store)
    lines = [f"Quiet hours: **{format_window(w)}** ({config.OWNER_TZ})."]
    if is_quiet(local, w, seen):
        lines.append(f"Active now, until {window_end(local, w):%H:%M}.")
    elif in_window(local, w):
        lines.append(
            f"Inside the window, but you were active in the last {PRESENCE_GRACE_S // 60} minutes, "
            "so sessions still ask."
        )
    else:
        lines.append("Not active right now.")
    lines.append("Inside the window, owner sessions decide what can be undone instead of asking, and list those calls for you.")
    return "\n".join(lines)
