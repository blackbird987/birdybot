"""Test webhooks act as the owner only while test mode is armed from this machine.

`scripts/discord_test.py` drives the bot through Discord webhooks, and the bot
treats a message from a webhook listed in ``TEST_WEBHOOK_IDS`` as the owner.
Until 2026-11 it did so unconditionally. A webhook URL is not a private
credential, though: anyone in the server with Manage Webhooks can open
Server Settings > Integrations and copy it, and so can every bot with
Administrator. Each of them was, in effect, the owner of a bypass-mode session
on this PC.

So the pass now needs a second thing that only someone on this machine can
produce: a short-lived arm file in the bot's data dir, written by
``discord_test.py`` (``arm``, or automatically before it sends). Someone who
only has the URL cannot write it. Everything here fails closed: a file that is
missing, unreadable, malformed, a symlink, owned by someone else, readable by
group or other, expired, or dated implausibly far ahead means "not armed".

Stdlib only and free of ``bot.config`` at import time, so the test script can
load it against the installed bot's data dir from a build worktree.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path

ARM_FILE_NAME = "test_webhooks_armed.json"

DEFAULT_MINUTES = 30
MIN_MINUTES = 1
MAX_MINUTES = 240
# An expiry further ahead than this was not written by arm(): treat it as forged.
MAX_AHEAD_S = MAX_MINUTES * 60

ALERT_INTERVAL_S = 3600
ALERT_TEXT = (
    "A test-webhook message arrived while test mode was off and was ignored. "
    "If you weren't testing, that webhook's link may have leaked: delete it "
    "in Server Settings > Integrations."
)

# webhook id -> monotonic time of the last alert. Per process, which is the
# point: a restart may repeat one alert, a flood never does.
_last_alert: dict[str, float] = {}


def arm_file(data_dir: Path | str | None = None) -> Path:
    """The arm file for *data_dir*, or for the running bot's DATA_DIR."""
    if data_dir is None:
        from bot import config  # lazy: the scripts pass their own data dir
        data_dir = config.DATA_DIR
    return Path(data_dir) / ARM_FILE_NAME


def _read_expiry(path: Path) -> float | None:
    """The file's expiry, or None if it fails any of the trust checks."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None  # symlinks, directories, fifos
    if st.st_uid != os.getuid():
        return None
    if st.st_mode & 0o077:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            data = json.loads(f.read(4096))
        expires_at = float(data["expires_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if expires_at != expires_at:  # NaN
        return None
    return expires_at


def remaining(data_dir: Path | str | None = None, now: float | None = None) -> float | None:
    """Seconds of test mode left, or None when not armed."""
    expires_at = _read_expiry(arm_file(data_dir))
    if expires_at is None:
        return None
    now = time.time() if now is None else now
    left = expires_at - now
    if left <= 0 or left > MAX_AHEAD_S:
        return None
    return left


def is_armed(data_dir: Path | str | None = None, now: float | None = None) -> bool:
    try:
        return remaining(data_dir, now) is not None
    except Exception:
        return False


def is_listed_test_webhook(webhook_id, listed=None) -> bool:
    """Whether *webhook_id* is one of the configured test webhooks (armed or not)."""
    if not webhook_id:
        return False
    if listed is None:
        from bot import config
        listed = config.TEST_WEBHOOK_IDS
    return bool(listed) and str(webhook_id) in listed


def is_trusted_test_webhook(webhook_id, listed=None,
                            data_dir: Path | str | None = None,
                            now: float | None = None) -> bool:
    """Listed AND armed: the only case in which a webhook passes as the owner."""
    return is_listed_test_webhook(webhook_id, listed) and is_armed(data_dir, now)


def arm(minutes: float = DEFAULT_MINUTES, data_dir: Path | str | None = None,
        now: float | None = None) -> float:
    """Open test mode for *minutes* (clamped to 1..240). Returns the expiry."""
    minutes = max(MIN_MINUTES, min(MAX_MINUTES, float(minutes)))
    now = time.time() if now is None else now
    expires_at = now + minutes * 60
    path = arm_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".armed-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"expires_at": expires_at, "armed_by": "discord_test.py"}, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return expires_at


def disarm(data_dir: Path | str | None = None) -> bool:
    """Close test mode. Returns whether a file was removed."""
    try:
        os.unlink(arm_file(data_dir))
        return True
    except FileNotFoundError:
        return False


def should_alert(webhook_id, now: float | None = None) -> bool:
    """Rate limit for the leaked-link alert: once per hour per webhook id."""
    key = str(webhook_id)
    now = time.monotonic() if now is None else now
    last = _last_alert.get(key)
    if last is not None and now - last < ALERT_INTERVAL_S:
        return False
    _last_alert[key] = now
    return True
