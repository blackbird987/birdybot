"""Sliding-window usage estimation from local instance data.

Tracks token usage in hourly buckets and computes 5-hour (session) and
7-day (weekly) utilization windows — inspired by claude-counter's UI.

This module intentionally has ZERO imports from bot.store to avoid
circular dependencies.  Callers pass raw data (bucket dicts, instance
dicts) rather than Store objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class UsageWindow:
    """One usage window (e.g. 5h session or 7d weekly)."""

    label: str  # "Session (5h)" or "Weekly (7d)"
    tokens_used: int  # total tokens in window
    token_limit: int  # configured cap (0 = unknown)
    utilization: float  # 0-100 percentage (0 if no limit set)
    window_hours: int  # 5 or 168
    resets_at: str | None  # ISO timestamp when oldest entry exits window
    cost_usd: float  # total cost in window


# ---------------------------------------------------------------------------
# Window computation
# ---------------------------------------------------------------------------


def compute_windows(
    buckets: dict[str, dict],
    limits: dict[str, int],
) -> dict[str, UsageWindow]:
    """Compute 5h and 7d windows from hourly token buckets.

    Parameters
    ----------
    buckets : {"2026-03-20T14": {"in": N, "out": N, "cost": F}, ...}
    limits  : {"5h": int, "7d": int} — token limits (0 = no limit)

    Returns dict keyed by "5h" and "7d".
    """
    now = datetime.now(timezone.utc)
    results: dict[str, UsageWindow] = {}

    for key, hours in [("5h", 5), ("7d", 168)]:
        cutoff = now - timedelta(hours=hours)
        total_tokens = 0
        total_cost = 0.0
        oldest_key: str | None = None

        for bucket_key, bucket in buckets.items():
            bucket_time = _parse_bucket_key(bucket_key)
            if bucket_time is None or bucket_time < cutoff:
                continue
            total_tokens += bucket.get("in", 0) + bucket.get("out", 0)
            total_cost += bucket.get("cost", 0.0)
            if oldest_key is None or bucket_key < oldest_key:
                oldest_key = bucket_key

        limit = limits.get(key, 0)
        utilization = (
            min(100.0, total_tokens / limit * 100) if limit > 0 else 0.0
        )

        # Reset time: oldest bucket + window duration
        resets_at: str | None = None
        if oldest_key and total_tokens > 0:
            oldest_time = _parse_bucket_key(oldest_key)
            if oldest_time:
                resets_at = (oldest_time + timedelta(hours=hours)).isoformat()

        label = "Session (5h)" if hours == 5 else "Weekly (7d)"
        results[key] = UsageWindow(
            label=label,
            tokens_used=total_tokens,
            token_limit=limit,
            utilization=utilization,
            window_hours=hours,
            resets_at=resets_at,
            cost_usd=total_cost,
        )

    return results


# ---------------------------------------------------------------------------
# Backfill from existing instances (one-time migration)
# ---------------------------------------------------------------------------


def backfill_buckets(instances_raw: list[dict]) -> dict[str, dict]:
    """Build hourly token buckets from raw instance data.

    Parameters
    ----------
    instances_raw : list of dicts with keys:
        finished_at (ISO str), input_tokens (int), output_tokens (int),
        cost_usd (float).

    Returns hourly bucket dict.  Only includes entries within the last
    8 days (matching the prune window).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=8)
    buckets: dict[str, dict] = {}

    for inst in instances_raw:
        finished = inst.get("finished_at")
        if not finished:
            continue
        try:
            dt = datetime.fromisoformat(finished)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if dt < cutoff:
            continue

        key = dt.strftime("%Y-%m-%dT%H")
        bucket = buckets.setdefault(key, {"in": 0, "out": 0, "cost": 0.0})
        bucket["in"] += inst.get("input_tokens", 0)
        bucket["out"] += inst.get("output_tokens", 0)
        bucket["cost"] += inst.get("cost_usd", 0.0) or 0.0

    return buckets


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_bar(pct: float, width: int = 15) -> str:
    """Unicode progress bar: ``\\u2593\\u2593\\u2593\\u2591\\u2591\\u2591``"""
    filled = round(pct / 100 * width)
    filled = max(0, min(width, filled))
    return "\u2593" * filled + "\u2591" * (width - filled)


def format_countdown(iso_timestamp: str | None) -> str:
    """Format reset countdown: '2h 15m', '4d 12h', etc."""
    if not iso_timestamp:
        return ""
    try:
        target = datetime.fromisoformat(iso_timestamp)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return ""
    diff = target - datetime.now(timezone.utc)
    if diff.total_seconds() <= 0:
        return "0m"
    total_min = int(diff.total_seconds() / 60)
    if total_min < 60:
        return f"{total_min}m"
    hours = total_min // 60
    mins = total_min % 60
    if hours < 24:
        return f"{hours}h {mins}m"
    days = hours // 24
    rem_hours = hours % 24
    return f"{days}d {rem_hours}h"


def _format_tokens(n: int) -> str:
    """Compact token count: '245K', '2.1M', or raw number."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def format_usage_field(windows: dict[str, UsageWindow]) -> str | None:
    """Format usage windows for Discord embed field value.

    Returns None when all windows have zero tokens.
    """
    has_data = any(w.tokens_used > 0 for w in windows.values())
    if not has_data:
        return None

    lines: list[str] = []
    for key in ("5h", "7d"):
        w = windows.get(key)
        if not w:
            continue
        tokens_str = _format_tokens(w.tokens_used)

        if w.token_limit > 0:
            pct_str = f"{w.utilization:.0f}%"
            reset = format_countdown(w.resets_at)
            reset_str = f" \u00b7 resets {reset}" if reset else ""
            lines.append(f"**{w.label}** {pct_str}{reset_str}")
            lines.append(f"`{format_bar(w.utilization)}` {tokens_str} tokens")
        else:
            reset = format_countdown(w.resets_at)
            reset_str = f" \u00b7 resets {reset}" if reset else ""
            lines.append(f"**{w.label}** {tokens_str} tokens{reset_str}")
    return "\n".join(lines) if lines else None


# ---------------------------------------------------------------------------
# Convenience wrapper — single call for dashboard / control room callers
# ---------------------------------------------------------------------------


def get_usage_text(
    buckets: dict[str, dict],
    limits: dict[str, int],
) -> str | None:
    """Compute windows and format in one call.

    Returns formatted string or None (no data).
    """
    windows = compute_windows(buckets, limits)
    return format_usage_field(windows)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_bucket_key(key: str) -> datetime | None:
    """Parse a bucket key like '2026-03-20T14' into a datetime."""
    try:
        return datetime.fromisoformat(key + ":00:00").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
