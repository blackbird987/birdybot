"""Usage tracking via ccusage CLI tool.

Shells out to ``ccusage`` (or ``npx ccusage`` as fallback) to read Claude
Code's local JSONL session files, providing accurate token counts including
cache creation/read tokens.

Results are cached with adaptive TTL: 60s normally, 15s when approaching
rate limits (remainingMinutes < 30).  Failures are negatively cached to
prevent subprocess storms, and a circuit breaker backs off after repeated
failures.

The daily range is always fetched as 7 days in a single subprocess call;
today's and weekly aggregates are derived from the same cached result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess as _sp
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from bot import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ccusage command detection — prefer global install over npx (much faster)
# ---------------------------------------------------------------------------

def _detect_ccusage_cmd() -> list[str]:
    """Build the base command for invoking ccusage.

    Prefers a global ``ccusage`` binary on PATH (fast, ~1-2s).
    Falls back to ``npx ccusage`` if not found (slow, 18-30s on Windows).
    """
    found = shutil.which("ccusage")
    if sys.platform == "win32":
        if found:
            return ["cmd", "/c", "ccusage"]
        log.warning("ccusage not on PATH, using npx (slow) — run 'npm i -g ccusage' to fix")
        return ["cmd", "/c", "npx", "ccusage"]
    else:
        if found:
            return ["ccusage"]
        log.warning("ccusage not on PATH, using npx (slow) — run 'npm i -g ccusage' to fix")
        return ["npx", "ccusage"]

_CCUSAGE_CMD: list[str] = _detect_ccusage_cmd()

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, object]] = {}
_DEFAULT_TTL = getattr(config, "CCUSAGE_CACHE_TTL", 60)
_URGENT_TTL = 15  # When approaching rate limits

# ---------------------------------------------------------------------------
# Concurrency locks (one per cache key, prevents duplicate subprocesses)
# ---------------------------------------------------------------------------

_locks: dict[str, asyncio.Lock] = {}


def _get_lock(key: str) -> asyncio.Lock:
    """Get or create a lock for *key* (sync-safe, no race in asyncio)."""
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

_fail_count: int = 0
_last_fail_time: float = 0
_MAX_CONSECUTIVE_FAILS = 3
_BACKOFF_TTL = 300  # 5 min backoff after repeated failures


# ---------------------------------------------------------------------------
# Process tree kill helper
# ---------------------------------------------------------------------------


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill *proc* and all its children.

    On Windows ``proc.kill()`` only terminates the immediate ``cmd.exe``
    wrapper — the child ``node.exe`` spawned by npx survives.  We follow
    up with ``taskkill /T /F`` to reap the entire tree.
    """
    pid = proc.pid
    if pid is None:
        return

    # Fast path: kill the immediate process
    try:
        proc.kill()
    except (OSError, ProcessLookupError):
        pass

    # Kill the full tree
    if sys.platform == "win32":
        try:
            _sp.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=5,
                creationflags=_sp.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class UsageBlock:
    """Current 5h billing block."""

    start_time: str
    end_time: str
    is_active: bool
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    total_tokens: int
    cost_usd: float
    models: list[str]
    burn_rate_cost_per_hour: float
    projected_cost: float
    remaining_minutes: int


@dataclass
class UsageDaily:
    """Daily usage summary."""

    date: str
    total_tokens: int
    cost_usd: float


@dataclass
class UsageWeekly:
    """Weekly usage aggregate (sum of daily entries)."""

    total_tokens: int
    cost_usd: float
    days: int


# ---------------------------------------------------------------------------
# Cache TTL helper
# ---------------------------------------------------------------------------


def _cache_ttl(data: object) -> float:
    """Return the appropriate TTL for a cached value."""
    if isinstance(data, dict):
        for block in data.get("blocks", []):
            proj = block.get("projection", {})
            if proj.get("remainingMinutes", 999) < 30:
                return _URGENT_TTL
    return _DEFAULT_TTL


# ---------------------------------------------------------------------------
# Stale cache helper
# ---------------------------------------------------------------------------


def _get_any_cached(cache_key: str) -> tuple[object | None, float]:
    """Return (data, age_seconds) from cache regardless of TTL.

    Negative-cached failures (data is None) are NOT served as stale —
    returns (None, 0) so callers fall through to the unavailable path.
    """
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if data is None:
            return None, 0
        return data, time.monotonic() - ts
    return None, 0


# ---------------------------------------------------------------------------
# ccusage subprocess runner with caching, dedup & circuit breaker
# ---------------------------------------------------------------------------


async def _run_ccusage(args: list[str], force: bool = False) -> dict | None:
    """Run ccusage command and return parsed JSON.

    Returns None on failure or if response is missing expected keys.
    Failures are negatively cached so timeouts don't trigger retry storms.
    """
    global _fail_count, _last_fail_time

    cache_key = " ".join(args)
    now = time.monotonic()

    # --- Circuit breaker: stop trying after repeated failures ---
    if _fail_count >= _MAX_CONSECUTIVE_FAILS and now - _last_fail_time < _BACKOFF_TTL:
        cached = _cache.get(cache_key)
        return cached[1] if cached else None

    # --- Pre-lock cache check ---
    if not force and cache_key in _cache:
        ts, data = _cache[cache_key]
        if now - ts < _cache_ttl(data):
            return data

    # --- Dedup: if another coroutine is already running this command,
    #     return whatever is in cache (stale or None) rather than pile up ---
    lock = _get_lock(cache_key)
    if lock.locked():
        log.debug("ccusage dedup: %s already running, returning cached", args)
        cached = _cache.get(cache_key)
        return cached[1] if cached else None

    async with lock:
        # --- Double-checked locking: re-check cache after acquiring ---
        now = time.monotonic()
        if cache_key in _cache:
            ts, data = _cache[cache_key]
            if now - ts < _cache_ttl(data):
                return data

        try:
            cmd = [*_CCUSAGE_CMD, *args, "--json", "--offline"]
            if sys.platform == "win32":
                # Prevent console window + group the process tree so
                # taskkill /T can reap child node.exe on timeout.
                flags = _sp.CREATE_NO_WINDOW | _sp.CREATE_NEW_PROCESS_GROUP
                extra: dict = {"creationflags": flags}
            else:
                extra = {"start_new_session": True}

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **extra,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
            except asyncio.TimeoutError:
                _kill_process_tree(proc)
                _cache[cache_key] = (time.monotonic(), None)
                _fail_count += 1
                _last_fail_time = time.monotonic()
                log.warning("ccusage timed out after 45s: %s", args)
                return None

            if proc.returncode != 0:
                _cache[cache_key] = (time.monotonic(), None)
                _fail_count += 1
                _last_fail_time = time.monotonic()
                log.warning("ccusage failed (rc=%d): %s", proc.returncode, stderr.decode()[:200])
                return None

            data = json.loads(stdout.decode())
            _cache[cache_key] = (time.monotonic(), data)
            _fail_count = 0  # Reset circuit breaker on success
            return data
        except Exception as e:
            _cache[cache_key] = (time.monotonic(), None)
            _fail_count += 1
            _last_fail_time = time.monotonic()
            log.warning("ccusage error: %s", e)
            return None


# ---------------------------------------------------------------------------
# Unified daily range fetch (7 days → derive today + weekly)
# ---------------------------------------------------------------------------


def _daily_range_since() -> str:
    """Return the --since arg for 7-day daily fetch (YYYYMMDD)."""
    return (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y%m%d")


async def _fetch_daily_range(
    force: bool = False,
) -> tuple[UsageDaily | None, UsageWeekly | None]:
    """Fetch 7 days of daily data in one ccusage call.

    Returns (today, weekly) derived from the same result.
    """
    since = _daily_range_since()
    data = await _run_ccusage(["daily", "--since", since], force=force)
    return _parse_daily_range(data)


def _parse_daily_range(
    data: dict | None,
) -> tuple[UsageDaily | None, UsageWeekly | None]:
    """Parse daily range response into today + weekly."""
    if not data or not isinstance(data.get("daily"), list) or not data["daily"]:
        return None, None

    entries = data["daily"]
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Today = last entry if it matches today's date
    last = entries[-1]
    daily: UsageDaily | None = None
    if last.get("date") == today_str:
        daily = UsageDaily(
            date=last["date"],
            total_tokens=last.get("totalTokens", 0),
            cost_usd=last.get("totalCost", 0),
        )

    # Weekly = sum all entries
    weekly = UsageWeekly(
        total_tokens=sum(e.get("totalTokens", 0) for e in entries),
        cost_usd=sum(e.get("totalCost", 0) for e in entries),
        days=len(entries),
    )

    return daily, weekly


# ---------------------------------------------------------------------------
# Block parsing (shared by get_current_block and stale-cache path)
# ---------------------------------------------------------------------------


def _parse_block(data: dict | None) -> UsageBlock | None:
    """Parse the most recent active block from raw ccusage response."""
    if not data or not isinstance(data.get("blocks"), list):
        return None
    for block in reversed(data["blocks"]):
        if not block.get("isActive"):
            continue
        tc = block.get("tokenCounts")
        if not isinstance(tc, dict):
            continue
        br = block.get("burnRate", {})
        proj = block.get("projection", {})
        return UsageBlock(
            start_time=block.get("startTime", ""),
            end_time=block.get("endTime", ""),
            is_active=True,
            input_tokens=tc.get("inputTokens", 0),
            output_tokens=tc.get("outputTokens", 0),
            cache_creation_tokens=tc.get("cacheCreationInputTokens", 0),
            cache_read_tokens=tc.get("cacheReadInputTokens", 0),
            total_tokens=block.get("totalTokens", 0),
            cost_usd=block.get("costUSD", 0),
            models=block.get("models", []),
            burn_rate_cost_per_hour=br.get("costPerHour", 0),
            projected_cost=proj.get("totalCost", 0),
            remaining_minutes=proj.get("remainingMinutes", 0),
        )
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_current_block(force: bool = False) -> UsageBlock | None:
    """Get the currently active 5h billing block."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    data = await _run_ccusage(["blocks", "--since", today], force=force)
    return _parse_block(data)


async def get_daily_summary(force: bool = False) -> UsageDaily | None:
    """Get today's usage (thin wrapper over unified 7-day fetch)."""
    daily, _ = await _fetch_daily_range(force=force)
    return daily


async def get_weekly_summary(force: bool = False) -> UsageWeekly | None:
    """Get 7-day usage aggregate (thin wrapper over unified fetch)."""
    _, weekly = await _fetch_daily_range(force=force)
    return weekly


async def get_usage_details(force: bool = False) -> str:
    """Rich usage text for /usage command.  Returns formatted string.

    When *force* is False, serves stale cache instantly (never blocks on
    subprocess).  Only *force=True* triggers a live ccusage call.
    """
    if not force:
        # Try stale cache first — never block the user.
        # Cache keys must match what _run_ccusage uses: " ".join(args).
        since = _daily_range_since()
        daily_key = f"daily --since {since}"
        today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        block_key = f"blocks --since {today_str}"

        stale_daily, daily_age = _get_any_cached(daily_key)
        stale_block, block_age = _get_any_cached(block_key)

        if stale_daily or stale_block:
            block = _parse_block(stale_block) if stale_block else None
            daily, weekly = _parse_daily_range(stale_daily)
            age = max(daily_age, block_age)
            return _build_usage_text(block, daily, weekly, cache_age=age)

    # Live fetch (first call, or force=True)
    block, (daily, weekly) = await asyncio.gather(
        get_current_block(force=force),
        _fetch_daily_range(force=force),
    )
    return _build_usage_text(block, daily, weekly, cache_age=0)


# ---------------------------------------------------------------------------
# Plan percentage helpers
# ---------------------------------------------------------------------------


def _pct_label(used: float, limit: float, period: str) -> str:
    """Format '60% daily' or just '$287.50' when no limit configured."""
    if limit > 0:
        pct = min(used / limit * 100, 999)
        return f"${used:,.2f} / ${limit:,.0f} {period} ({pct:.0f}%)"
    return f"${used:,.2f}"


def _pct_short(used: float, limit: float, period: str) -> str:
    """Compact percentage for bar: '60% daily' or '$287'."""
    if limit > 0:
        pct = min(used / limit * 100, 999)
        return f"${used:,.0f} ({pct:.0f}% {period})"
    return f"${used:,.0f}"


# ---------------------------------------------------------------------------
# Text builders
# ---------------------------------------------------------------------------


def _build_usage_text(
    block: UsageBlock | None,
    daily: UsageDaily | None,
    weekly: UsageWeekly | None,
    *,
    cache_age: float = 0,
) -> str:
    """Build the rich /usage response text."""
    if not block and not daily and not weekly:
        return "Usage data unavailable \u2014 is `ccusage` installed? (`npx ccusage daily`)"

    lines: list[str] = []

    if block:
        lines.append("**Current Block (5h)**")
        lines.append(
            f"  ${block.cost_usd:.2f}"
            f" \u00b7 projected ${block.projected_cost:.2f}"
        )
        lines.append(
            f"  Burn: ${block.burn_rate_cost_per_hour:.2f}/hr"
            f" \u00b7 {block.remaining_minutes}m remaining"
        )
        lines.append(
            f"  Tokens: {_format_tokens(block.total_tokens)}"
            f" ({_format_tokens(block.input_tokens)} in"
            f" + {_format_tokens(block.output_tokens)} out"
            f" + {_format_tokens(block.cache_creation_tokens)} cache-write"
            f" + {_format_tokens(block.cache_read_tokens)} cache-read)"
        )
        if block.models:
            lines.append(f"  Models: {', '.join(block.models)}")
        lines.append("")

    if daily:
        daily_label = _pct_label(
            daily.cost_usd, config.PLAN_DAILY_LIMIT_USD, "daily limit"
        )
        lines.append(f"**Today** {daily_label}")

    if weekly:
        weekly_label = _pct_label(
            weekly.cost_usd, config.PLAN_WEEKLY_LIMIT_USD, "weekly limit"
        )
        lines.append(f"**This Week** {weekly_label} ({weekly.days}d)")

    # Plan savings comparison
    if (daily or weekly) and config.PLAN_MONTHLY_COST > 0:
        api_cost = weekly.cost_usd if weekly else (daily.cost_usd if daily else 0)
        period = "this week" if weekly else "today"
        lines.append("")
        lines.append(f"**Plan: {config.PLAN_NAME} (${config.PLAN_MONTHLY_COST:.0f}/mo)**")
        lines.append(f"  API equivalent {period}: ${api_cost:,.2f}")
        daily_plan = config.PLAN_MONTHLY_COST / 30
        lines.append(f"  Plan cost {period}: ~${daily_plan * (weekly.days if weekly else 1):,.2f}")

    # Show cache age only when stale (> normal TTL)
    if cache_age > _DEFAULT_TTL:
        mins = int(cache_age / 60)
        label = f"{mins}m" if mins > 0 else f"{int(cache_age)}s"
        lines.append(f"\n_(cached {label} ago)_")

    return "\n".join(lines).rstrip()


def format_usage_bar(
    block: UsageBlock | None,
    daily: UsageDaily | None,
    weekly: UsageWeekly | None = None,
) -> str | None:
    """Visual usage bar for dashboard/control-room embeds.

    Returns None only when no data at all is available.
    When block is missing but daily/weekly exist, returns a compact cost line.
    """
    if not block and not daily and not weekly:
        return None

    lines: list[str] = []

    # Full progress bar when block data is available
    if block:
        remaining = max(block.remaining_minutes, 0)
        elapsed = 300 - remaining
        progress = min(elapsed / 300, 1.0)
        filled = round(progress * 16)
        bar = "\u2588" * filled + "\u2591" * (16 - filled)

        time_label = "Block ended" if remaining == 0 else f"{remaining}m left"

        lines.append(f"`{bar}` {time_label}")
        lines.append(
            f"${block.cost_usd:.2f} used \u00b7 ${block.projected_cost:.2f} proj \u00b7 ${block.burn_rate_cost_per_hour:.2f}/hr"
        )

    # Today + weekly with plan percentages (shown with or without block)
    parts: list[str] = []
    if daily:
        parts.append(
            f"Today: {_pct_short(daily.cost_usd, config.PLAN_DAILY_LIMIT_USD, 'daily')}"
        )
    if weekly:
        parts.append(
            f"Week: {_pct_short(weekly.cost_usd, config.PLAN_WEEKLY_LIMIT_USD, 'weekly')}"
        )
    if parts:
        lines.append(" \u00b7 ".join(parts))

    return "\n".join(lines)


async def get_usage_bar_async() -> str | None:
    """Visual usage bar for embeds. Returns None if no data."""
    block, (daily, weekly) = await asyncio.gather(
        get_current_block(),
        _fetch_daily_range(),
    )
    return format_usage_bar(block, daily, weekly)


async def warmup() -> None:
    """Prime the ccusage cache for both daily and blocks data."""
    try:
        since = _daily_range_since()
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        daily_data, block_data = await asyncio.gather(
            _run_ccusage(["daily", "--since", since]),
            _run_ccusage(["blocks", "--since", today]),
        )
        if daily_data and block_data:
            log.info("ccusage warmup complete (daily + blocks)")
        elif daily_data or block_data:
            missing = "blocks" if not block_data else "daily"
            log.warning("ccusage warmup partial: %s returned no data", missing)
        else:
            log.warning("ccusage warmup: no data returned")
    except Exception:
        log.debug("ccusage warmup failed", exc_info=True)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_tokens(n: int) -> str:
    """Compact token count: '245K', '2.1M', or raw number."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)
