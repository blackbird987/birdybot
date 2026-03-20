"""Usage tracking via ccusage CLI tool.

Shells out to ``npx ccusage`` to read Claude Code's local JSONL session
files, providing accurate token counts including cache creation/read tokens.

Results are cached with adaptive TTL: 60s normally, 15s when approaching
rate limits (remainingMinutes < 30).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from bot import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, object]] = {}
_DEFAULT_TTL = getattr(config, "CCUSAGE_CACHE_TTL", 60)
_URGENT_TTL = 15  # When approaching rate limits


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


# ---------------------------------------------------------------------------
# ccusage subprocess runner with caching
# ---------------------------------------------------------------------------


async def _run_ccusage(args: list[str], force: bool = False) -> dict | None:
    """Run ccusage command and return parsed JSON.

    Returns None on failure or if response is missing expected keys.
    """
    cache_key = " ".join(args)
    now = time.monotonic()

    if not force and cache_key in _cache:
        ts, data = _cache[cache_key]
        ttl = _DEFAULT_TTL
        # Use shorter TTL if last result showed we're near limits
        if isinstance(data, dict):
            for block in data.get("blocks", []):
                proj = block.get("projection", {})
                if proj.get("remainingMinutes", 999) < 30:
                    ttl = _URGENT_TTL
                    break
        if now - ts < ttl:
            return data

    try:
        # On Windows, npx is a .cmd file that create_subprocess_exec can't
        # find directly — route through cmd.exe.
        if sys.platform == "win32":
            cmd = ["cmd", "/c", "npx", "ccusage", *args, "--json", "--offline"]
        else:
            cmd = ["npx", "ccusage", *args, "--json", "--offline"]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("ccusage timed out after 30s: %s", args)
            return None
        if proc.returncode != 0:
            log.warning("ccusage failed (rc=%d): %s", proc.returncode, stderr.decode()[:200])
            return None
        data = json.loads(stdout.decode())
        _cache[cache_key] = (now, data)
        return data
    except Exception as e:
        log.warning("ccusage error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_current_block(force: bool = False) -> UsageBlock | None:
    """Get the currently active 5h billing block."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    data = await _run_ccusage(["blocks", "--since", today], force=force)
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


async def get_daily_summary(force: bool = False) -> UsageDaily | None:
    """Get today's total usage."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    data = await _run_ccusage(["daily", "--since", today], force=force)
    if not data or not isinstance(data.get("daily"), list) or not data["daily"]:
        return None
    d = data["daily"][-1]  # Latest day entry
    return UsageDaily(
        date=d.get("date", today),
        total_tokens=d.get("totalTokens", 0),
        cost_usd=d.get("totalCost", 0),
    )


async def get_usage_text_async() -> str | None:
    """Compact usage text for embed fields. Returns None if no data.

    Format: ``Block: $X.XX · $Y.YY/hr · Zm left\\nToday: $X.XX``
    """
    block, daily = await asyncio.gather(
        get_current_block(),
        get_daily_summary(),
    )
    if not block and not daily:
        return None

    lines: list[str] = []
    if block:
        lines.append(
            f"**Block** ${block.cost_usd:.2f}"
            f" \u00b7 ${block.burn_rate_cost_per_hour:.2f}/hr"
            f" \u00b7 {block.remaining_minutes}m left"
        )
    if daily:
        lines.append(f"**Today** ${daily.cost_usd:.2f}")

    return "\n".join(lines) if lines else None


async def get_usage_details(force: bool = False) -> str:
    """Rich usage text for /usage command."""
    block, daily = await asyncio.gather(
        get_current_block(force=force),
        get_daily_summary(force=force),
    )

    if not block and not daily:
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
        lines.append(
            f"**Today** ${daily.cost_usd:.2f}"
            f" \u00b7 {_format_tokens(daily.total_tokens)} tokens"
        )
        lines.append("")

    return "\n".join(lines).rstrip()


async def warmup() -> None:
    """Prime the npx/ccusage cache. Fire-and-forget at startup."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        await _run_ccusage(["daily", "--since", today])
        log.info("ccusage warmup complete")
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
