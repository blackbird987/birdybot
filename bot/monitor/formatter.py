"""Build Discord embeds from AIAgent monitor data."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import discord

log = logging.getLogger(__name__)

# Discord embed limits
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_FIELD_NAME = 256
MAX_FIELD_VALUE = 1024
MAX_FOOTER = 2048
MAX_EMBED_TOTAL = 6000

# Attention level ordering
_ATTENTION_ORDER = {"ok": 0, "warning": 1, "critical": 2}
_ATTENTION_EMOJI = {"ok": "\U0001f7e2", "warning": "\U0001f7e1", "critical": "\U0001f534"}
_ATTENTION_COLOR = {
    "ok": discord.Color.green(),
    "warning": discord.Color.yellow(),
    "critical": discord.Color.red(),
}


def worse_attention(a: str, b: str) -> str:
    """Return the worse of two attention levels."""
    return a if _ATTENTION_ORDER.get(a, 0) >= _ATTENTION_ORDER.get(b, 0) else b


def _trend(current: float | None, previous: float | None, threshold: float = 0.05) -> str:
    """Return trend arrow comparing current vs previous."""
    if current is None or previous is None or previous == 0:
        return ""
    pct = (current - previous) / abs(previous)
    if pct > threshold:
        return " \u2191"
    if pct < -threshold:
        return " \u2193"
    return " \u2192"


def _truncate(text: str, limit: int, suffix: str = "\u2026") -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(suffix)] + suffix


def _short_version(version: str | None) -> str:
    """Shorten '1.0.0+4dfd8b9f7cb37adc...' to '1.0.0+4dfd8b9'."""
    if not version:
        return ""
    if "+" in version:
        base, commit = version.split("+", 1)
        return f"{base}+{commit[:7]}"
    return version[:20]


def determine_attention(summary: dict | None) -> str:
    """Determine attention level from summary data."""
    if not summary or not isinstance(summary, dict):
        return "warning"
    level = summary.get("attentionLevel", "ok")
    if level in _ATTENTION_ORDER:
        return level
    return "ok"


def build_initial_embed(name: str, url: str) -> discord.Embed:
    """Build the placeholder embed before first fetch."""
    embed = discord.Embed(
        title=_truncate(f"\u23f3 {name} \u2014 Initializing", MAX_TITLE),
        description=f"Waiting for first successful fetch from {url}",
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Created: {datetime.now(timezone.utc).strftime('%b %d, %H:%M UTC')}")
    return embed


def build_dashboard_embed(
    name: str,
    raw: dict[str, Any],
    prev_snapshot: dict | None = None,
) -> discord.Embed:
    """Build the main dashboard embed from raw API data."""
    summary = raw.get("summary")

    # Check for auth failure
    for ep_data in raw.values():
        if isinstance(ep_data, dict) and ep_data.get("_error") == "auth_failed":
            embed = discord.Embed(
                title=_truncate(f"\u26a0\ufe0f {name} \u2014 Auth Failed", MAX_TITLE),
                description=f"Authentication failed. Check `MONITOR_{name.upper()}_AUTH` in .env",
                color=discord.Color.red(),
            )
            embed.set_footer(text=f"Last attempt: {datetime.now(timezone.utc).strftime('%b %d, %H:%M UTC')}")
            return embed

    if not summary or not isinstance(summary, dict):
        return build_initial_embed(name, "?")

    status = summary.get("currentStatus", {})

    attention = determine_attention(summary)
    emoji = _ATTENTION_EMOJI.get(attention, "\U0001f7e2")
    label = {"ok": "OK", "warning": "Warning", "critical": "Critical"}.get(attention, "OK")
    color = _ATTENTION_COLOR.get(attention, discord.Color.green())

    embed = discord.Embed(
        title=_truncate(f"{emoji} {name} \u2014 {label}", MAX_TITLE),
        color=color,
    )

    # Description: uptime + version + started at
    desc_parts = []
    uptime = status.get("serverUptime")
    version = status.get("serverVersion", "")
    started_at = status.get("serverStartedAt")
    if uptime:
        desc_parts.append(f"\u23f1 Uptime: {uptime}")
    if version:
        desc_parts.append(f"v{_short_version(version)}")
    if started_at:
        try:
            dt = datetime.fromisoformat(started_at.rstrip("Z") + "+00:00" if not started_at.endswith("Z") else started_at.replace("Z", "+00:00"))
            desc_parts.append(f"Started: {dt.strftime('%b %d, %H:%M UTC')}")
        except Exception:
            pass
    if desc_parts:
        embed.description = _truncate(" \u00b7 ".join(desc_parts), MAX_DESCRIPTION)

    # Field: Connections (inline)
    conn_lines = []
    ws = status.get("webSocketHealth", {})
    if ws:
        md_state = ws.get("marketDataState", "?")
        md_conn = "\u2705" if ws.get("marketDataConnected") else "\u274c"
        ud_conn = "\u2705" if ws.get("userDataConnected") else "\u274c"
        latency = ws.get("pingLatencyMs")
        lat_str = f" ({latency:.0f}ms)" if latency else ""
        conn_lines.append(f"\U0001f4e1 WS: {md_state}{lat_str}")
        conn_lines.append(f"Market {md_conn} \u00b7 User {ud_conn}")
        subs = ws.get("activeSubscriptions", 0)
        reconnects = ws.get("reconnectCountLast24h", 0)
        conn_lines.append(f"{subs} subs \u00b7 {reconnects} reconnects/24h")
    hl = status.get("hyperLiquid", {})
    if hl:
        cb = hl.get("circuitState", "?")
        trips = hl.get("totalTrips", 0)
        conn_lines.append(f"CB: {cb} \u00b7 {trips} trips")
    discord_ok = status.get("discordConnected")
    if discord_ok is not None:
        conn_lines.append(f"Discord: {'\u2705' if discord_ok else '\u274c'}")
    if conn_lines:
        embed.add_field(
            name="Connections",
            value=_truncate("\n".join(conn_lines), MAX_FIELD_VALUE),
            inline=True,
        )

    # Field: Automation (inline)
    auto_lines = []
    rules = status.get("automationRules", {})
    if rules:
        active = rules.get("active", 0)
        sleeping = rules.get("sleeping", 0)
        total = rules.get("total", 0)
        auto_lines.append(f"\U0001f916 {active} active \u00b7 {sleeping} sleeping \u00b7 {total} total")
    checks = summary.get("checkCount", 0)
    error_rate = status.get("errorRateLast24h")
    if checks:
        auto_lines.append(f"{checks} checks total")
    if error_rate is not None:
        auto_lines.append(f"Error rate 24h: {error_rate}%")
    # Eval snapshot
    eval_snap = status.get("evaluationSnapshot", {})
    if eval_snap:
        evals = eval_snap.get("totalEvaluationsLast30d", 0)
        rules_issues = eval_snap.get("rulesWithIssues", 0)
        if evals:
            auto_lines.append(f"{evals} evals/30d \u00b7 {rules_issues} rules w/ issues")
    if auto_lines:
        embed.add_field(
            name="Automation",
            value=_truncate("\n".join(auto_lines), MAX_FIELD_VALUE),
            inline=True,
        )

    # Field: Costs (inline)
    cost_lines = []
    cost_today = status.get("costToday")
    if cost_today is not None:
        prev_cost = prev_snapshot.get("cost_usd") if prev_snapshot else None
        trend = _trend(cost_today, prev_cost)
        cost_lines.append(f"\U0001f4b0 ${cost_today:.2f}{trend}")
    # Cost trend from statistics
    stats = summary.get("statistics", {})
    cost_trend = stats.get("costTrend", {})
    if isinstance(cost_trend, dict):
        trend_dir = cost_trend.get("trend", "")
        if trend_dir:
            cost_lines.append(f"Trend: {trend_dir}")
        last7 = cost_trend.get("last7Days")
        if isinstance(last7, list) and last7:
            avg7 = sum(last7) / len(last7)
            cost_lines.append(f"7d avg: ${avg7:.2f}")
    if cost_lines:
        embed.add_field(
            name="Costs (today)",
            value=_truncate("\n".join(cost_lines), MAX_FIELD_VALUE),
            inline=True,
        )

    # Field: Active Issues (full width)
    issues = summary.get("issues", [])
    active_issues = [i for i in issues if isinstance(i, dict) and i.get("status") == "active"]
    if active_issues:
        issue_lines = []
        sev_emoji = {"critical": "\U0001f534", "warning": "\u26a0\ufe0f", "info": "\u2139\ufe0f"}
        for issue in active_issues[:6]:
            sev = issue.get("severity", "warning")
            icon = sev_emoji.get(sev, "\u26a0\ufe0f")
            title = issue.get("title", issue.get("compositeKey", "?"))
            issue_lines.append(f"{icon} {title}")
        remaining = len(active_issues) - 6
        if remaining > 0:
            issue_lines.append(f"\u2026 and {remaining} more")
        embed.add_field(
            name=f"Active Issues ({len(active_issues)})",
            value=_truncate("\n".join(issue_lines), MAX_FIELD_VALUE),
            inline=False,
        )

    # Field: Version History (full width, if reboots happened)
    versions = summary.get("versionHistory", [])
    if versions:
        ver_lines = []
        for vh in versions[:3]:
            ver = _short_version(vh.get("version", "?"))
            first = vh.get("firstSeen", "")
            checks_v = vh.get("checksInVersion", 0)
            introduced = len(vh.get("issuesIntroduced", []))
            resolved = len(vh.get("issuesResolved", []))
            try:
                dt = datetime.fromisoformat(first.rstrip("Z") + "+00:00" if "Z" in first else first)
                date_str = dt.strftime("%b %d %H:%M")
            except Exception:
                date_str = first[:16]
            parts = [f"v{ver} \u00b7 {date_str}"]
            parts.append(f"{checks_v} checks")
            if introduced:
                parts.append(f"+{introduced} issues")
            if resolved:
                parts.append(f"-{resolved} fixed")
            ver_lines.append(" \u00b7 ".join(parts))
        embed.add_field(
            name="Deploys",
            value=_truncate("\n".join(ver_lines), MAX_FIELD_VALUE),
            inline=False,
        )

    # Footer
    embed.set_footer(
        text=_truncate(
            f"Last updated: {datetime.now(timezone.utc).strftime('%b %d, %H:%M UTC')} \u00b7 Refreshes every 4h",
            MAX_FOOTER,
        )
    )

    return embed


def build_stale_banner(name: str, failures: int, last_fetch: str | None) -> discord.Embed:
    """Build a warning embed when data is stale."""
    embed = discord.Embed(
        title=_truncate(f"\U0001f7e1 {name} \u2014 Stale Data", MAX_TITLE),
        description=f"Failed to fetch data {failures} consecutive times.\nLast successful fetch: {last_fetch or 'never'}",
        color=discord.Color.yellow(),
    )
    embed.set_footer(text=f"Updated: {datetime.now(timezone.utc).strftime('%b %d, %H:%M UTC')}")
    return embed


def build_history_embed(
    daily: list[dict],
    weekly: list[dict],
    monthly: list[dict],
) -> discord.Embed:
    """Build the history embed from snapshot data."""
    embed = discord.Embed(
        title="History",
        color=discord.Color.blurple(),
    )

    lines: list[str] = []

    # Daily
    if daily:
        lines.append("**Daily (last 7 days)**")
        for snap in daily[:7]:
            date = snap.get("date", "?")
            attn = _ATTENTION_EMOJI.get(snap.get("attention_level", "ok"), "\U0001f7e2")
            issues = snap.get("issues_active", 0)
            cost = snap.get("cost_usd")
            version = snap.get("version", "")
            reboots = snap.get("reboots", 0)
            events = snap.get("events", [])

            parts = [f"{date} \u00b7 {attn}"]
            parts.append(f"{issues} issue{'s' if issues != 1 else ''}")
            if cost is not None:
                parts.append(f"${cost:.2f}")
            if version:
                parts.append(f"v{_short_version(version)}")
            if reboots:
                parts.append(f"\U0001f504 {reboots} reboot{'s' if reboots != 1 else ''}")
            if events:
                parts.append(events[0] if len(events) == 1 else f"{len(events)} events")
            lines.append(" \u00b7 ".join(parts))
        lines.append("")

    # Weekly
    if weekly:
        lines.append("**Weekly**")
        for wk in weekly[:4]:
            start = wk.get("week_start", "?")
            end = wk.get("week_end", "?")
            avg = _ATTENTION_EMOJI.get(wk.get("avg_attention", "ok"), "\U0001f7e2")
            total = wk.get("total_issues", 0)
            resolved = wk.get("total_resolved", 0)
            cost = wk.get("cost_usd")
            versions = wk.get("versions", [])

            parts = [f"{start}\u2013{end} \u00b7 {avg} avg"]
            parts.append(f"{total} issues ({resolved} fixed)")
            if cost is not None:
                parts.append(f"${cost:.2f}")
            if versions:
                short_versions = [_short_version(v) for v in versions]
                if len(short_versions) > 1:
                    parts.append(f"v{short_versions[0]}\u2192{short_versions[-1]}")
                else:
                    parts.append(f"v{short_versions[0]}")
            lines.append(" \u00b7 ".join(parts))
        lines.append("")

    # Monthly
    if monthly:
        lines.append("**Monthly**")
        for mo in monthly[:6]:
            month = mo.get("month", "?")
            total = mo.get("total_issues", 0)
            resolved = mo.get("total_resolved", 0)
            cost = mo.get("cost_usd")
            uptime = mo.get("uptime_pct")

            parts = [month]
            parts.append(f"{total} issues \u00b7 {resolved} fixed")
            if cost is not None:
                parts.append(f"${cost:.0f}")
            if uptime is not None:
                parts.append(f"{uptime}% uptime")
            lines.append(" \u00b7 ".join(parts))

    if not lines:
        embed.description = "No history yet."
    else:
        embed.description = _truncate("\n".join(lines), MAX_DESCRIPTION)

    embed.set_footer(text="7d daily \u2192 weekly \u2192 monthly rollup")
    return embed


def extract_snapshot_data(raw: dict[str, Any]) -> dict:
    """Extract fields for a daily snapshot from raw API data."""
    summary = raw.get("summary")
    if not summary or not isinstance(summary, dict):
        return {"attention_level": "warning"}

    status = summary.get("currentStatus", {})
    attention = determine_attention(summary)
    version = _short_version(status.get("serverVersion", ""))

    # Issues
    issues = summary.get("issues", [])
    active_issues = [i for i in issues if isinstance(i, dict) and i.get("status") == "active"]
    issues_active = len(active_issues)
    issues_new = [i.get("title", i.get("compositeKey", "?")) for i in active_issues]

    stats = summary.get("statistics", {})
    issues_resolved = stats.get("totalIssuesResolved", 0)

    cost_usd = status.get("costToday")

    # Reboot detection: compare serverStartedAt
    started_at = status.get("serverStartedAt", "")

    events: list[str] = []

    # Version history as events
    ver_history = summary.get("versionHistory", [])
    if ver_history:
        latest = ver_history[-1]
        ver = _short_version(latest.get("version", ""))
        events.append(f"Deploy: v{ver}")

    checks_ok = summary.get("checkCount", 0)
    checks_fail = 0

    error_rate = status.get("errorRateLast24h", 0)

    return {
        "attention_level": attention,
        "version": version,
        "issues_active": issues_active,
        "issues_resolved": issues_resolved,
        "issues_new": issues_new,
        "cost_usd": cost_usd,
        "uptime_pct": None,  # Not directly available
        "events": events,
        "checks_ok": checks_ok,
        "checks_fail": checks_fail,
        "server_started_at": started_at,
        "error_rate_24h": error_rate,
    }
