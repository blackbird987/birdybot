"""State the command modules share: uptime, the CLI version, shutdown, channel locks and the budget gate."""

from __future__ import annotations

import asyncio
import logging

from bot import config
from bot.platform.base import RequestContext

log = logging.getLogger(__name__)

# --- Shared state for uptime / cli_version / shutdown ---

_start_time: float = 0.0
_cli_version: str = "unknown"
_shutdown_fn = None

# --- Per-channel query queue (prevents concurrent queries in same session) ---

_channel_locks: dict[str, asyncio.Lock] = {}


def _get_channel_lock(channel_id: str) -> asyncio.Lock:
    """Get or create a per-channel lock for serializing queries."""
    if channel_id not in _channel_locks:
        _channel_locks[channel_id] = asyncio.Lock()
    return _channel_locks[channel_id]


def init(start_time: float, cli_version: str, shutdown_fn=None) -> None:
    """Initialize module-level state."""
    global _start_time, _cli_version, _shutdown_fn
    _start_time = start_time
    _cli_version = cli_version
    _shutdown_fn = shutdown_fn


def get_start_time() -> float:
    """Return bot start timestamp (epoch seconds)."""
    return _start_time


def check_budget(ctx: RequestContext) -> bool:
    daily = ctx.store.get_daily_cost()
    return daily < config.DAILY_BUDGET_USD


NO_REPO_MSG = "No repo set. Use /repo add <name> <path> first."


async def refuse_if_over_budget(ctx: RequestContext) -> bool:
    """Tell the user and return True when today's budget is spent."""
    if check_budget(ctx):
        return False
    await ctx.messenger.send_text(
        ctx.channel_id, "Daily budget exceeded. Use /budget reset to override.",
    )
    return True


def _clear_chain_on_manual_finalize(
    store, session_id: str | None, op: str,
) -> None:
    """Drop any lingering autopilot chain state when the user finalizes manually.

    /done, /merge, and the Done/Merge/Discard buttons are all explicit
    user-driven endings. If a chain queue is still hanging around (e.g. paused
    on a needs_input that the user resolved by hand), it must NOT be eligible
    for post-reboot resume — the work is already done.
    """
    if not session_id:
        return
    if store.get_autopilot_chain(session_id):
        log.info(
            "Manual %s — clearing autopilot chain queue for session %s",
            op, session_id,
        )
        store.clear_chain_state(session_id)


async def budget_warning(ctx: RequestContext) -> None:
    daily = ctx.store.get_daily_cost()
    if daily >= config.DAILY_BUDGET_USD * 0.8:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Budget warning: ${daily:.4f} / ${config.DAILY_BUDGET_USD:.2f} "
            f"({daily / config.DAILY_BUDGET_USD * 100:.0f}%)",
        )
