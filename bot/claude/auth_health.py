"""Account credential health — can a CLAUDE_CONFIG_DIR actually authenticate?

Used by the runner to skip accounts that cannot possibly succeed, instead of
burning a task on a doomed spawn.  Motivating incident (t-6614): the backup
account's ``.credentials.json`` existed but carried no ``refreshToken`` and an
access token that had expired five weeks earlier, so every failover to it
returned a raw ``401 OAuth access token has expired`` — and one of those
surfaced to the user as a plain build failure.

The check is deliberately cheap and self-healing: results are cached on
``(mtime_ns, size)`` of the credentials file, so a ``/login`` that rewrites it
busts the cache on the next pick — no restart, no ``.env`` edit.  The ``size``
component is load-bearing: on filesystems with coarse mtime granularity two
writes within one tick still bust the cache because the size changes.

IMPORTANT — this is a heuristic, not proof.  A file with a refresh token can
still be revoked server-side, and a host that stores credentials outside the
config dir (macOS Keychain) would report every account unusable.  Callers must
never let it take the fleet dark: ``ClaudeRunner._pick_account`` ignores the
check entirely when *every* candidate fails it.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Container, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger(__name__)


class _Probe(NamedTuple):
    """What one read of a credentials file says, before the clock is applied."""
    reason: str | None                 # file-level verdict, None when usable
    login_ends: datetime | None = None   # refreshTokenExpiresAt
    access_ends: datetime | None = None  # expiresAt (the 8-hour access token)


# path_str -> ((mtime_ns, size), probe)
_cache: dict[str, tuple[tuple[int, int], _Probe]] = {}

REASON_NO_DIR = "dir does not exist"
REASON_NO_FILE = "no .credentials.json — not logged in"
REASON_NO_TOKEN = "no refresh token — not logged in"
REASON_UNREADABLE = "credentials file unreadable"
# The login itself has a hard end date. The CLI renews the 8-hour access token
# from the refresh token on its own, but the refresh token carries a
# ``refreshTokenExpiresAt`` the server sets at sign-in (about four weeks out)
# and does NOT move on a refresh: klerk renewed on 2026-10-25 and its date
# stayed on 2026-11-12. Past it the CLI can no longer renew, and once the
# access token it holds runs out too it treats the account as signed out (the
# same two-field rule its own login check uses), so a spawn on it is doomed.
# Unlike the other probe verdicts this one depends on the clock, not just the
# file, which is why it is applied on read rather than cached.
REASON_LOGIN_EXPIRED = "login expired: refresh token past its expiry date"

# Not produced by the probe — recorded by the runner when the CLI itself
# rejects a token that *looks* fine on disk.  Kept here so both the writer and
# the "is this account healthy again?" reader agree on the exact string.  This
# is the one verdict re-reading the file cannot retire, since the rejected file
# parses fine too: it takes either a successful run or the file being rewritten
# (see ``credentials_fingerprint``).
REASON_RUNTIME_401 = "logged out — the CLI rejected its OAuth token (401)"

# The same shape as RUNTIME_401 -- the server rejected a file that parses
# fine -- but a different *fix*, which is why it is a separate verdict rather
# than more text on that one.  A 401 is answered by signing in again; this one
# cannot be, because the account is signed in perfectly well and an admin has
# switched Claude Code off for the whole organization.  Telling the user to
# run /login here sends them to do something that provably will not work.
REASON_ORG_DISABLED = (
    "the organization disabled Claude Code for this account"
)

# Verdicts reached from a *runtime* rejection rather than from the on-disk
# probe.  Re-reading the credentials file can never retire one of these: the
# rejected file parses exactly like a working one.  Only a successful run
# clears them, or the file being rewritten by a fresh login
# (see ``credentials_fingerprint``).
RUNTIME_REJECTION_REASONS = frozenset({REASON_RUNTIME_401, REASON_ORG_DISABLED})


def account_label(account_dir: str | Path) -> str:
    """Short display name for an account dir (``.claude-klerk`` -> ``klerk``).

    Falls back to the directory name, then the full path, so a label is
    always non-empty for log lines and Discord copy.
    """
    name = Path(str(account_dir)).expanduser().name or str(account_dir)
    stripped = name.lstrip(".")
    if stripped.startswith("claude-"):
        stripped = stripped[len("claude-"):]
    return stripped or name


def _parse_expiry(raw: object) -> datetime | None:
    """``refreshTokenExpiresAt`` (epoch milliseconds) as an aware datetime.

    None for anything that is not a plausible number: an older CLI never
    wrote the field, and a malformed one must read as "unknown", never as
    "already expired".
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    try:
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _probe(cred_path: Path) -> _Probe:
    """Parse the credentials file (no clock involved, so safe to memoize)."""
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
    except Exception:
        log.debug("Unreadable credentials at %s", cred_path, exc_info=True)
        return _Probe(REASON_UNREADABLE)
    if not isinstance(data, dict):
        return _Probe(REASON_UNREADABLE)
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        oauth = {}
    token = oauth.get("refreshToken") or data.get("refreshToken")
    if not (isinstance(token, str) and token.strip()):
        return _Probe(REASON_NO_TOKEN)
    return _Probe(
        None,
        _parse_expiry(oauth.get("refreshTokenExpiresAt")),
        _parse_expiry(oauth.get("expiresAt")),
    )


def _read(account_dir: str | Path) -> _Probe:
    """The probe for *account_dir*, memoized on the credentials file's stat."""
    p = Path(str(account_dir)).expanduser()
    key_path = str(p)
    if not p.is_dir():
        _cache.pop(key_path, None)
        return _Probe(REASON_NO_DIR)
    cred = p / ".credentials.json"
    try:
        st = cred.stat()
    except OSError:
        _cache.pop(key_path, None)
        return _Probe(REASON_NO_FILE)
    stat_key = (st.st_mtime_ns, st.st_size)
    cached = _cache.get(key_path)
    if cached and cached[0] == stat_key:
        return cached[1]
    probe = _probe(cred)
    _cache[key_path] = (stat_key, probe)
    return probe


def unusable_reason(
    account_dir: str | Path, *, now: datetime | None = None,
) -> str | None:
    """Why this account can't authenticate, or None when it looks usable."""
    probe = _read(account_dir)
    if probe.reason:
        return probe.reason
    now = now or datetime.now(timezone.utc)
    # Both halves, as the CLI checks them: past the login's end date nothing
    # can renew the access token, but the one already held still works until
    # its own expiry (up to 8 hours). An unknown date is never "expired".
    if (
        probe.login_ends is not None and probe.login_ends <= now
        and (probe.access_ends is None or probe.access_ends <= now)
    ):
        return REASON_LOGIN_EXPIRED
    return None


def login_expiry(account_dir: str | Path) -> datetime | None:
    """When this account's saved login runs out, or None if unknown.

    Read straight from the file the CLI wrote; nothing here refreshes or
    contacts anything (see docs/design/plan-usage.md for why the bot must
    never touch the refresh token). None for an account with no usable login
    at all, since there is nothing left to expire.
    """
    probe = _read(account_dir)
    return None if probe.reason else probe.login_ends


def credentials_usable(account_dir: str | Path) -> bool:
    """True when *account_dir* looks able to authenticate."""
    return unusable_reason(account_dir) is None


def credentials_fingerprint(account_dir: str | Path) -> str | None:
    """``"<mtime_ns>:<size>"`` for the credentials file, or None if absent.

    The durable "has this been re-logged-in?" signal.  A server-side 401 leaves
    a file that still *looks* fine, so the only evidence a fix happened is the
    file being rewritten — which is exactly what ``/login`` does.  Same
    (mtime, size) pair the probe cache keys on, as a string so it can live in
    ``data/state.json`` and survive a reboot.

    Strictly it means "rewritten", not "re-logged-in": a routine token refresh
    rewrites the file too.  That only happens while the CLI is running under
    this config dir, and a sidelined account is one we deliberately don't spawn
    — so in practice the only writer is a login.  If something else does write
    it, treating that as recovery is the right call anyway: a refresh that
    succeeded means the account authenticates.
    """
    cred = Path(str(account_dir)).expanduser() / ".credentials.json"
    try:
        st = cred.stat()
    except OSError:
        return None
    return f"{st.st_mtime_ns}:{st.st_size}"


def split_accounts(
    accounts: Iterable[str],
    sidelined: Container[str] = (),
) -> tuple[list[str], list[str]]:
    """Partition *accounts* into (usable, unusable).

    ``sidelined`` covers what the on-disk probe structurally cannot see: an
    account whose token looks fine but which the server rejected at runtime.
    Both "how healthy is the fleet?" surfaces (``/status`` and the dashboard)
    go through here so they can never disagree with each other — or with The
    Ark, which is the whole point: the incident that started this was a backup
    that read as healthy everywhere for five weeks.
    """
    # Materialise first: this reads *accounts* twice, and a generator caller
    # would otherwise get an empty "usable" list — the worst possible way to
    # be wrong here, since it reads as "every account is down".
    items = list(accounts)
    bad = [a for a in items if a in sidelined or not credentials_usable(a)]
    dead = set(bad)
    good = [a for a in items if a not in dead]
    return good, bad


def relogin_command(account_dir: str | Path) -> str:
    """Copy-pasteable shell line that re-authenticates *account_dir*."""
    p = Path(str(account_dir)).expanduser()
    if sys.platform == "win32":
        return f'$env:CLAUDE_CONFIG_DIR="{p}"; claude'
    return f'CLAUDE_CONFIG_DIR="{p}" claude'


def clear_cache() -> None:
    """Drop the memoized probe results (tests, and after a credential write)."""
    _cache.clear()
