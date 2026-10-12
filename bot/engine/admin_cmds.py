"""Status and admin commands: /status, /cost, /evals, /logs, /budget, /reboot, /help and friends."""

from __future__ import annotations

import asyncio
import logging
import time as _time

from bot import config
from bot.engine import (
    cmd_state,
    sessions as sessions_mod,
)
from bot.platform.base import ButtonSpec, RequestContext
from bot.platform.formatting import format_status_md
from bot.textutil import clip

log = logging.getLogger(__name__)


# --- /evals ---

async def on_evals(ctx: RequestContext, days: int = 7) -> None:
    """Report which quality flags keep recurring, and what owns each one."""
    from bot.engine import eval as eval_mod

    if not config.EVAL_ENABLED:
        await ctx.messenger.send_text(
            ctx.channel_id,
            "Session evaluation is disabled (`EVAL_ENABLED=0`) — nothing to report.",
        )
        return

    days = max(1, min(days, 90))
    digest = await asyncio.to_thread(eval_mod.build_digest, days)

    lines: list[str] = [f"**Eval digest — last {digest.days}d**"]
    lines.append(f"{digest.sessions} session(s) evaluated")

    if digest.median_cache_hit_rate is not None:
        lines.append(
            f"Prompt cache: {digest.median_cache_hit_rate * 100:.0f}% median reuse "
            f"across {digest.resumed_sessions} resumed session(s)"
        )
    elif digest.sessions:
        lines.append("Prompt cache: no resumed sessions in this window")

    # send_text hard-truncates at Discord's 2000 chars — it does not split. So
    # fill rows against a character budget, not a fixed row count: a fixed count
    # of long rows overflows, and the thing cut off is the "...and N more"
    # footer, which is exactly the line that says the list is incomplete.
    ROW_BUDGET = 1700
    if digest.rows:
        lines.append("")
        lines.append("**Recurring flags**")
        used = sum(len(ln) + 1 for ln in lines)
        shown = 0
        for row in digest.rows:
            mark = {"issue": "✗", "warning": "!", "info": "·"}.get(row.severity, "·")
            share = f"{row.count}/{digest.sessions} sessions"
            # Occurrences only add information when a flag fires repeatedly
            # inside the same session — otherwise it's the same number twice.
            if row.occurrences > row.count:
                share += f", {row.occurrences} hits"
            pair = [
                f"{mark} **{share}** — {clip(row.message, 110)}",
                f"   owner: `{row.owner}` · e.g. {', '.join(row.examples)}",
            ]
            cost = sum(len(ln) + 1 for ln in pair)
            # `shown` guard: always emit at least one row, even a huge one.
            if shown and used + cost > ROW_BUDGET:
                break
            lines.extend(pair)
            used += cost
            shown += 1
        if shown < len(digest.rows):
            lines.append(f"_...and {len(digest.rows) - shown} more._")
    elif digest.sessions:
        lines.append("")
        lines.append("No flag recurred often enough to report.")
    else:
        lines.append("")
        lines.append(
            "No sessions were evaluated in this window — try a longer one, "
            "e.g. `/evals days:30`."
        )

    if digest.suppressed_rows:
        lines.append("")
        lines.append(
            f"_{digest.suppressed_rows} one-off flag(s) below the reporting "
            f"threshold not shown._"
        )

    text = "\n".join(lines)
    markup = ctx.messenger.markdown_to_markup(text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- /cost ---

async def on_cost(ctx: RequestContext) -> None:
    from bot.engine.usage import _fetch_daily_range, _pct_label

    daily, weekly = await _fetch_daily_range()

    if not daily and not weekly:
        await ctx.messenger.send_text(
            ctx.channel_id, "Cost data unavailable \u2014 is `ccusage` installed?"
        )
        return

    lines: list[str] = ["**Cost**"]
    if daily:
        lines.append(
            f"Today: {_pct_label(daily.cost_usd, config.PLAN_DAILY_LIMIT_USD, 'daily limit')}"
        )
    if weekly:
        lines.append(
            f"This Week: {_pct_label(weekly.cost_usd, config.PLAN_WEEKLY_LIMIT_USD, 'weekly limit')}"
            f" ({weekly.days}d)"
        )

    text = "\n".join(lines)
    markup = ctx.messenger.markdown_to_markup(text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- /usage ---

async def on_usage(ctx: RequestContext, *, force: bool = False) -> str:
    """Build usage text.  Returns the formatted string (caller sends it)."""
    from bot.engine.usage import get_usage_details

    return await get_usage_details(force=force)


# --- /status ---

def _accounts_status_line(store, is_owner: bool = True) -> str | None:
    """`**Accounts** — 1/2 usable | signed out: `klerk`` — or None if there's
    no failover configured. Names the dead account so a silently degraded
    rotation is visible at a glance instead of only at failover time.

    Counts an account the *server* rejected as down too, not just one whose
    credentials file is visibly empty — otherwise the exact failure this whole
    feature exists for reads as "2/2 usable" here while The Ark says the
    opposite.

    The "/auth to fix" pointer is owner-only, because the command is: /status
    is open to allow-listed users, and sending them to a slash command that
    will just reject them is worse than saying nothing.
    """
    accounts = list(config.CLAUDE_ACCOUNTS)
    if len(accounts) < 2:
        return None
    from bot.claude.auth_health import account_label, split_accounts

    _, sidelined = split_accounts(accounts, store.sidelined_accounts())
    line = f"**Accounts** — {len(accounts) - len(sidelined)}/{len(accounts)} usable"
    if sidelined:
        names = ", ".join(f"`{account_label(a)}`" for a in sidelined)
        line += f" | signed out: {names}"
        line += " — run /auth to fix" if is_owner else " — failover unavailable"
    return line


async def on_status(ctx: RequestContext) -> None:
    uptime = _time.time() - cmd_state._start_time
    active_repo, _ = ctx.store.get_active_repo()
    recent = ctx.store.list_instances()[:5]

    # Determine active platforms
    platforms = []
    if config.DISCORD_ENABLED:
        platforms.append("Discord")

    text = format_status_md(
        accounts_line=_accounts_status_line(ctx.store, ctx.is_owner),
        uptime_secs=uptime,
        running=ctx.store.running_count(),
        instances_today=ctx.store.instance_count_today(),
        failures_today=ctx.store.failure_count_today(),
        total_instances=ctx.store.instance_count(),
        repos=ctx.store.list_repos(),
        active_repo=active_repo,
        context=ctx.effective_context,
        schedule_count=len(ctx.store.list_schedules()),
        cli_version=cmd_state._cli_version,
        pc_name=config.PC_NAME,
        platforms=platforms,
        recent=recent,
    )
    markup = ctx.messenger.markdown_to_markup(text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- /logs ---

async def on_logs(ctx: RequestContext) -> None:
    log_path = config.LOG_FILE
    if not log_path.exists():
        await ctx.messenger.send_text(ctx.channel_id, "No log file found.")
        return

    # Read only the last ~16KB to avoid loading 10MB into memory
    import os as _os
    size = _os.path.getsize(log_path)
    read_bytes = min(size, 16384)
    with open(log_path, "rb") as f:
        f.seek(max(0, size - read_bytes))
        raw = f.read().decode("utf-8", errors="replace")
    tail = raw.splitlines()[-50:]
    max_len = 4096 - 20
    while tail:
        joined = "\n".join(tail)
        escaped = ctx.messenger.escape(joined)
        if len(escaped) <= max_len:
            break
        tail = tail[1:]

    if not tail:
        joined = "(log too large to display)"

    text = "```\n" + joined + "\n```"
    markup = ctx.messenger.markdown_to_markup(text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- /budget ---

async def on_budget(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if text == "reset":
        ctx.store.reset_daily_budget()
        await ctx.messenger.send_text(ctx.channel_id, "Daily budget reset.")
    else:
        daily = ctx.store.get_daily_cost()
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Today: ${daily:.4f} / ${config.DAILY_BUDGET_USD:.2f}",
        )


# --- /clear ---

async def on_clear(ctx: RequestContext) -> None:
    count = ctx.store.archive_old()
    await ctx.messenger.send_text(ctx.channel_id, f"Archived {count} old instances.")


# --- /shutdown ---

async def on_shutdown(ctx: RequestContext) -> None:
    if cmd_state._shutdown_fn:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Shutting down {config.PC_NAME}. Start the bot on another PC to switch.",
        )
        cmd_state._shutdown_fn()
    else:
        await ctx.messenger.send_text(ctx.channel_id, "Shutdown not available.")


async def on_reboot(ctx: RequestContext) -> None:
    """Shut down and relaunch the bot process.

    Uses the same coalesced reboot path as autopilot-requested reboots.
    If other instances are still running, waits for them to finish first.
    """
    if not cmd_state._shutdown_fn:
        await ctx.messenger.send_text(ctx.channel_id, "Reboot not available.")
        return

    # Wait for active instances/tasks to finish before queueing
    if ctx.runner.is_busy:
        ids = ", ".join(ctx.runner.active_ids) or "(between steps)"
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"⏳ Waiting for active work to finish: {ids}",
        )
        idle = await ctx.runner.wait_until_idle(timeout=300)
        if not idle:
            remaining = ", ".join(ctx.runner.active_ids)
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"Timed out waiting. Force-rebooting with {len(ctx.runner.active_ids)} still running: {remaining}",
            )

    # Queue reboot and trigger — since we waited for idle, this fires immediately.
    # force=True: explicit user-initiated reboot already coordinated with
    # wait_until_idle (with timeout warning above); bypass busy-check.
    ctx.runner.request_reboot({
        "message": f"Manual reboot from {ctx.platform}",
        "channel_id": ctx.channel_id,
        "platform": ctx.platform,
    }, force=True)


# --- /session ---

async def on_session(ctx: RequestContext, text: str) -> None:
    text = text.strip()

    if text.startswith("resume "):
        sid = text[7:].strip()
        if len(sid) < 36:
            fpath = await asyncio.to_thread(sessions_mod.find_session_file, sid)
            if not fpath:
                escaped = ctx.messenger.escape(sid)
                await ctx.messenger.send_text(
                    ctx.channel_id, f"No session found matching '{escaped}'.",
                )
                return
            sid = fpath.stem
        ctx.store.active_session_id = sid
        short = sid[:12]
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Session set: `{short}…`\nNext message will continue this session.",
        )

    elif text == "drop":
        ctx.store.active_session_id = None
        await ctx.messenger.send_text(ctx.channel_id, "Session cleared. Next message starts fresh.")

    else:
        scan_limit = 5  # Discord allows max 5 button rows
        session_list = await asyncio.to_thread(sessions_mod.scan_sessions, scan_limit, ctx.store.list_repos())
        active = ctx.store.active_session_id

        if not session_list:
            await ctx.messenger.send_text(ctx.channel_id, "No sessions found.")
            return

        buttons = []
        for s in session_list:
            is_active = active and s["id"] == active
            project = s.get("project", "?")
            age = s["age"]
            topic = s["topic"]

            # Discord button labels max 80 chars
            # Format: "✅ [bot] Add Discord Support · 2m ago"
            prefix = "✅ " if is_active else ""
            suffix = f" · {age}"
            tag = f"[{project}] "
            max_topic = 80 - len(prefix) - len(tag) - len(suffix)
            if len(topic) > max_topic:
                topic = topic[:max_topic - 1] + "…"
            btn_label = f"{prefix}{tag}{topic}{suffix}"

            buttons.append([ButtonSpec(btn_label, f"sess_resume:{s['id']}")])

        await ctx.messenger.send_text(
            ctx.channel_id, "**Recent Sessions**", buttons=buttons,
        )


# --- /deferred ---

async def on_deferred(ctx: RequestContext, args: str = "") -> None:
    """Show or clear deferred revision items for a repo."""
    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""

    if subcmd == "clear":
        repo_name = parts[1] if len(parts) > 1 else None
        if not repo_name:
            repo_name, _ = ctx.store.get_active_repo()
        if not repo_name:
            await ctx.messenger.send_text(ctx.channel_id, "No active repo.")
            return
        count = ctx.store.clear_deferred(repo_name)
        await ctx.messenger.send_text(
            ctx.channel_id, f"Cleared {count} deferred item(s) for `{repo_name}`.",
        )
        return

    # Default: show deferred items
    repo_name = subcmd if subcmd else None
    if not repo_name:
        repo_name, _ = ctx.store.get_active_repo()
    if not repo_name:
        await ctx.messenger.send_text(ctx.channel_id, "No active repo.")
        return

    text = ctx.store.get_deferred(repo_name)
    if not text:
        await ctx.messenger.send_text(
            ctx.channel_id, f"No deferred items for `{repo_name}`.",
        )
        return

    if len(text) > 3800:
        text = text[:3800] + "\n\n*(truncated)*"
    markup = ctx.messenger.markdown_to_markup(text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- /help ---

async def on_help(ctx: RequestContext) -> None:
    help_text = (
        "**Commands**\n"
        "Send text — continues current conversation\n"
        "Send photo — image analysis\n"
        "Send file — document analysis\n"
        "`/new` — start fresh conversation\n"
        "`/bg` — background task (build mode)\n"
        "`/list` — show instances (last 24h)\n"
        "`/kill` — terminate instance\n"
        "`/retry` — re-run instance\n"
        "`/log` — full output\n"
        "`/done` — wrap up (commit, changelog, release)\n"
        "`/diff` — git diff\n"
        "`/merge` — merge branch\n"
        "`/discard` — delete branch\n"
        "`/branches` — list orphaned branches\n"
        "`/cost` — spending breakdown\n"
        "`/evals` — recurring session-quality flags\n"
        "`/status` — health dashboard\n"
        "`/auth` — Claude account status / re-login\n"
        "`/logs` — bot log\n"
        "`/mode` — explore|plan|build\n"
        "`/verbose` — progress detail (0|1|2)\n"
        "`/effort` — reasoning effort (low|medium|high|max)\n"
        "`/model` — model for this thread (or `default`)\n"
        "`/provider` — switch CLI provider (claude|cursor)\n"
        "`/context` — pinned context\n"
        "`/alias` — command shortcuts\n"
        "`/schedule` — recurring tasks\n"
        "`/deferred` — view/clear deferred review items\n"
        "`/repo` — repo management (add|remove|create|switch|list|desc)\n"
        "`/session` — list/resume desktop CLI sessions\n"
        "`/budget` — budget info/reset\n"
        "`/clear` — archive old instances\n"
        "`/shutdown` — stop the bot (switch PCs)\n"
    )
    markup = ctx.messenger.markdown_to_markup(help_text)
    await ctx.messenger.send_text(ctx.channel_id, markup)
