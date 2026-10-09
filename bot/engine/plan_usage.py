"""Real Claude plan usage, read from Anthropic per account.

The ccusage bar in ``usage.py`` adds up dollar costs from local session logs
and divides by a limit it guessed, so on 2026-10-09 it read "93% est" for a
session Anthropic had at 15%, and it had no weekly percentage at all. The
numbers that actually gate the account (session %, weekly %, per-model weekly
%) come from the endpoint Claude Code's own ``/usage`` screen calls:

    GET https://api.anthropic.com/api/oauth/usage
    Authorization: Bearer <claudeAiOauth.accessToken>
    anthropic-beta: oauth-2025-04-20

Four rules, each of which costs something real if it drifts:

- **We never refresh a token.** The refresh token rotates on use, and the CLI
  owns it: a refresh from here would invalidate the CLI's copy and sign the
  account out. An expired access token is reported as expired; the CLI
  refreshes it on that account's next run, and the fingerprint of the
  credentials file busts our cache the moment it does.
- **The endpoint is undocumented.** Every key is optional. The ``limits`` list
  is preferred and the older ``five_hour`` / ``seven_day`` fields are the
  fallback; a response carrying neither is an error, not zeros.
- **It fails open.** Every failure is a state on the account, never an
  exception, and a failed refresh serves the last good reading with its age.
  When no account has data at all the caller falls back to the ccusage bar.
- **It is cached per account.** The dashboard and every Control Room redraw on
  each instance start and completion. Good readings live
  ``PLAN_USAGE_TTL_SECS``; failures live ``_ERROR_TTL_SECS`` so a 429 or a
  dead network is not retried on every redraw.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from bot import config
from bot.claude.auth_health import account_label

log = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_BETA_HEADER = "oauth-2025-04-20"
_HTTP_TIMEOUT_SECS = 8.0
_ERROR_TTL_SECS = 300
# A last-good reading older than this says more about the past than the
# present: the session window is five hours.
_LAST_GOOD_MAX_AGE_SECS = 3 * 3600
# Embed field values are capped at 1024 chars by Discord.
_FIELD_LIMIT = 1024

STATE_OK = "ok"
STATE_SIGNED_OUT = "signed_out"
STATE_EXPIRED = "expired"
STATE_ERROR = "error"


@dataclass
class AccountUsage:
    label: str
    config_dir: str
    state: str
    session_pct: float | None = None
    session_resets_at: datetime | None = None
    week_pct: float | None = None
    week_resets_at: datetime | None = None
    # (display name, percent, resets_at) for per-model weekly allowances
    scoped: list[tuple[str, float, datetime | None]] = field(default_factory=list)
    error: str = ""
    fetched_at: float = 0.0  # wall clock of the reading the figures came from
    stale: bool = False  # True when a failed refresh served the last good one

    @property
    def has_data(self) -> bool:
        return self.week_pct is not None or self.session_pct is not None


# config_dir -> (expires_monotonic, credentials fingerprint, reading)
_cache: dict[str, tuple[float, tuple[int, int] | None, AccountUsage]] = {}
_last_good: dict[str, AccountUsage] = {}
_last_state: dict[str, str] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


def account_dirs() -> list[str]:
    """The config dirs to report on, in rotation order. Empty when disabled."""
    if not config.PLAN_USAGE_API_ENABLED or config.PROVIDER != "claude":
        return []
    if config.CLAUDE_ACCOUNTS:
        return list(config.CLAUDE_ACCOUNTS)
    return [os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")]


def _cred_path(config_dir: str) -> Path:
    return Path(config_dir).expanduser() / ".credentials.json"


def _fingerprint(config_dir: str) -> tuple[int, int] | None:
    try:
        st = _cred_path(config_dir).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def read_access_token(config_dir: str) -> tuple[str | None, int | None]:
    """``(access_token, expires_at_ms)`` from the account's credentials file.

    Never raises: a missing, unreadable or malformed file is ``(None, None)``.
    """
    try:
        data = json.loads(_cred_path(config_dir).read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None, None
    token = oauth.get("accessToken")
    if not (isinstance(token, str) and token.strip()):
        token = None
    expires = oauth.get("expiresAt")
    if not isinstance(expires, (int, float)) or isinstance(expires, bool) or expires <= 0:
        expires = None
    return token, int(expires) if expires is not None else None


async def _http_get(token: str) -> tuple[int, object]:
    """``(status, parsed_json_or_None)``. Patched by the harness."""
    import httpx

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECS) as client:
        resp = await client.get(
            USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": _BETA_HEADER,
                "Content-Type": "application/json",
            },
        )
    try:
        body = resp.json()
    except Exception:
        body = None
    return resp.status_code, body


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _scope_name(scope: object) -> str:
    if isinstance(scope, dict):
        for key in ("model", "surface"):
            part = scope.get(key)
            if isinstance(part, dict):
                name = part.get("display_name") or part.get("id")
                if isinstance(name, str) and name:
                    return name
            elif isinstance(part, str) and part:
                return part
    return "scoped"


def parse_usage(body: object, *, label: str, config_dir: str) -> AccountUsage:
    """Turn an endpoint response into an ``AccountUsage``. Never raises."""
    usage = AccountUsage(label=label, config_dir=config_dir, state=STATE_OK,
                         fetched_at=time.time())
    if not isinstance(body, dict):
        usage.state, usage.error = STATE_ERROR, "unreadable response"
        return usage

    limits = body.get("limits")
    if isinstance(limits, list):
        for item in limits:
            if not isinstance(item, dict):
                continue
            pct = _num(item.get("percent"))
            if pct is None:
                continue
            resets = _parse_time(item.get("resets_at"))
            kind = item.get("kind")
            if kind == "session" and usage.session_pct is None:
                usage.session_pct, usage.session_resets_at = pct, resets
            elif kind == "weekly_all" and usage.week_pct is None:
                usage.week_pct, usage.week_resets_at = pct, resets
            elif kind == "weekly_scoped":
                usage.scoped.append((_scope_name(item.get("scope")), pct, resets))

    # Older shape, and the fallback for whatever ``limits`` did not carry.
    for key, attr in (("five_hour", "session"), ("seven_day", "week")):
        if getattr(usage, f"{attr}_pct") is not None:
            continue
        window = body.get(key)
        if isinstance(window, dict):
            pct = _num(window.get("utilization"))
            if pct is not None:
                setattr(usage, f"{attr}_pct", pct)
                setattr(usage, f"{attr}_resets_at", _parse_time(window.get("resets_at")))
    if not usage.scoped:
        for key, name in (("seven_day_opus", "Opus"), ("seven_day_sonnet", "Sonnet")):
            window = body.get(key)
            if isinstance(window, dict):
                pct = _num(window.get("utilization"))
                if pct is not None:
                    usage.scoped.append((name, pct, _parse_time(window.get("resets_at"))))

    if not usage.has_data:
        usage.state, usage.error = STATE_ERROR, "response carried no usage figures"
    return usage


def _api_error(status: int, body: object) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return f"HTTP {status}: {err['message'][:80]}"
    return f"HTTP {status}"


async def _fetch_uncached(config_dir: str) -> AccountUsage:
    label = account_label(config_dir)
    token, expires_ms = read_access_token(config_dir)
    if not token:
        return AccountUsage(label, config_dir, STATE_SIGNED_OUT)
    if expires_ms is not None and expires_ms / 1000 < time.time():
        return AccountUsage(label, config_dir, STATE_EXPIRED,
                            error="access token expired")
    try:
        status, body = await _http_get(token)
    except Exception as exc:  # network, timeout, TLS: all just "no reading"
        return AccountUsage(label, config_dir, STATE_ERROR,
                            error=type(exc).__name__)
    if status == 401:
        return AccountUsage(label, config_dir, STATE_EXPIRED,
                            error=_api_error(status, body))
    if status != 200:
        return AccountUsage(label, config_dir, STATE_ERROR,
                            error=_api_error(status, body))
    return parse_usage(body, label=label, config_dir=config_dir)


def _with_last_good(fresh: AccountUsage) -> AccountUsage:
    """A failed refresh serves the last good reading, marked stale."""
    if fresh.state not in (STATE_EXPIRED, STATE_ERROR):
        return fresh
    good = _last_good.get(fresh.config_dir)
    if good is None or time.time() - good.fetched_at > _LAST_GOOD_MAX_AGE_SECS:
        return fresh
    return replace(good, stale=True, error=fresh.error)


async def fetch_account_usage(config_dir: str, *, force: bool = False) -> AccountUsage:
    """One account's reading, through the cache. Never raises."""
    fp = _fingerprint(config_dir)
    entry = _cache.get(config_dir)
    if not force and entry and entry[1] == fp and time.monotonic() < entry[0]:
        return entry[2]
    async with _lock(config_dir):
        entry = _cache.get(config_dir)
        if not force and entry and entry[1] == fp and time.monotonic() < entry[0]:
            return entry[2]
        fresh = await _fetch_uncached(config_dir)
        if fresh.state == STATE_OK:
            _last_good[config_dir] = fresh
            ttl = config.PLAN_USAGE_TTL_SECS
        else:
            ttl = _ERROR_TTL_SECS
        if _last_state.get(config_dir) != fresh.state:
            log.log(
                logging.INFO if fresh.state in (STATE_OK, STATE_SIGNED_OUT) else logging.WARNING,
                "Plan usage for %s: %s%s", fresh.label, fresh.state,
                f" ({fresh.error})" if fresh.error else "",
            )
            _last_state[config_dir] = fresh.state
        result = _with_last_good(fresh)
        _cache[config_dir] = (time.monotonic() + ttl, fp, result)
        return result


async def get_plan_usage(*, force: bool = False) -> list[AccountUsage]:
    """Every configured account's reading, fetched in parallel."""
    dirs = account_dirs()
    if not dirs:
        return []
    return list(await asyncio.gather(
        *(fetch_account_usage(d, force=force) for d in dirs)
    ))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _bar(pct: float, width: int = 16) -> str:
    filled = round(max(0.0, min(pct, 100.0)) / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _ts(dt: datetime | None, style: str) -> str:
    return f"<t:{int(dt.timestamp())}:{style}>" if dt else ""


def _pct(p: float) -> str:
    return f"{p:.0f}%"


def _age(seconds: float) -> str:
    mins = int(seconds // 60)
    if mins < 60:
        return f"{max(mins, 1)}m"
    return f"{mins // 60}h{mins % 60:02d}m"


def _state_line(a: AccountUsage) -> str:
    if a.state == STATE_SIGNED_OUT:
        return f"`{a.label}` signed out"
    if a.state == STATE_EXPIRED:
        return f"`{a.label}` token expired, refreshes on its next run"
    return f"`{a.label}` usage unavailable ({a.error or 'unknown error'})"


def _subtext(a: AccountUsage, *, now: float) -> str:
    bits = [f"{name} {_pct(p)}" for name, p, _ in a.scoped if p >= 1]
    if a.stale:
        bits.append(f"as of {_age(now - a.fetched_at)} ago")
    return "-# " + " · ".join(bits) if bits else ""


def format_plan_usage_bar(accounts: list[AccountUsage]) -> str | None:
    """Compact bars for the dashboard and Control Room embeds.

    None when no account has figures, so the caller can fall back to ccusage.
    """
    if not any(a.has_data for a in accounts):
        return None
    now = time.time()
    labelled = len(accounts) > 1
    blocks: list[list[str]] = []
    subtexts: list[tuple[int, int]] = []  # (block index, line index)
    for a in accounts:
        if not a.has_data:
            blocks.append([_state_line(a)])
            continue
        lines: list[str] = []
        if labelled:
            lines.append(f"**{a.label}**")
        if a.week_pct is not None:
            reset = f" · resets {_ts(a.week_resets_at, 'R')}" if a.week_resets_at else ""
            lines.append(f"`{_bar(a.week_pct)}` week **{_pct(a.week_pct)}**{reset}")
        if a.session_pct is not None:
            reset = f" · resets {_ts(a.session_resets_at, 'R')}" if a.session_resets_at else ""
            lines.append(f"`{_bar(a.session_pct)}` session {_pct(a.session_pct)}{reset}")
        sub = _subtext(a, now=now)
        if sub:
            subtexts.append((len(blocks), len(lines)))
            lines.append(sub)
        blocks.append(lines)

    text = "\n".join(line for b in blocks for line in b)
    if len(text) > _FIELD_LIMIT:
        # Drop the per-model detail first, then cut whole lines.
        for bi, li in subtexts:
            blocks[bi][li] = ""
        kept: list[str] = []
        for line in (line for b in blocks for line in b if line):
            if len("\n".join(kept + [line])) > _FIELD_LIMIT - 2:
                kept.append("…")
                break
            kept.append(line)
        text = "\n".join(kept)
    return text


def format_plan_usage_details(accounts: list[AccountUsage]) -> str | None:
    """The fuller block that leads ``/usage``. None when the feature is off."""
    if not accounts:
        return None
    now = time.time()
    lines = ["**Plan usage** (from Anthropic)"]
    for a in accounts:
        if not a.has_data:
            lines.append(_state_line(a))
            continue
        head = f"**{a.label}**"
        if a.stale:
            head += f" (as of {_age(now - a.fetched_at)} ago: {a.error})"
        lines.append(head)
        if a.week_pct is not None:
            when = (f" · resets {_ts(a.week_resets_at, 'f')} ({_ts(a.week_resets_at, 'R')})"
                    if a.week_resets_at else "")
            lines.append(f"  Week: **{_pct(a.week_pct)}** used{when}")
        if a.session_pct is not None:
            when = (f" · resets {_ts(a.session_resets_at, 't')} ({_ts(a.session_resets_at, 'R')})"
                    if a.session_resets_at else "")
            lines.append(f"  Session: {_pct(a.session_pct)} used{when}")
        for name, p, _ in a.scoped:
            lines.append(f"  {name} (week): {_pct(p)} used")
    return "\n".join(lines)


def _reset_cache() -> None:
    """Harness hook."""
    _cache.clear()
    _last_good.clear()
    _last_state.clear()
    _locks.clear()
