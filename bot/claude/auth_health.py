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
from pathlib import Path

log = logging.getLogger(__name__)

# path_str -> ((mtime_ns, size), reason_or_None)
_cache: dict[str, tuple[tuple[int, int], str | None]] = {}

REASON_NO_DIR = "dir does not exist"
REASON_NO_FILE = "no .credentials.json — not logged in"
REASON_NO_TOKEN = "no refresh token — not logged in"
REASON_UNREADABLE = "credentials file unreadable"

# Not produced by the probe — recorded by the runner when the CLI itself
# rejects a token that *looks* fine on disk.  Kept here so both the writer and
# the "is this account healthy again?" reader agree on the exact string.  This
# is the one verdict re-reading the file cannot retire, since the rejected file
# parses fine too: it takes either a successful run or the file being rewritten
# (see ``credentials_fingerprint``).
REASON_RUNTIME_401 = "logged out — the CLI rejected its OAuth token (401)"


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


def _probe(cred_path: Path) -> str | None:
    """Parse the credentials file. Returns a reason string, or None if usable."""
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
    except Exception:
        log.debug("Unreadable credentials at %s", cred_path, exc_info=True)
        return REASON_UNREADABLE
    if not isinstance(data, dict):
        return REASON_UNREADABLE
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        oauth = {}
    token = oauth.get("refreshToken") or data.get("refreshToken")
    if not (isinstance(token, str) and token.strip()):
        return REASON_NO_TOKEN
    return None


def unusable_reason(account_dir: str | Path) -> str | None:
    """Why this account can't authenticate, or None when it looks usable."""
    p = Path(str(account_dir)).expanduser()
    key_path = str(p)
    if not p.is_dir():
        _cache.pop(key_path, None)
        return REASON_NO_DIR
    cred = p / ".credentials.json"
    try:
        st = cred.stat()
    except OSError:
        _cache.pop(key_path, None)
        return REASON_NO_FILE
    stat_key = (st.st_mtime_ns, st.st_size)
    cached = _cache.get(key_path)
    if cached and cached[0] == stat_key:
        return cached[1]
    reason = _probe(cred)
    _cache[key_path] = (stat_key, reason)
    return reason


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
