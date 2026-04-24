"""Verify Board rendering — embed + view for the pinned verify-board thread.

Renders a three-lane Kanban-style living message: NEEDS CHECK (pending),
CLAIMED, DONE TODAY. Single embed edited in place via
`ForumManager.refresh_verify_board()`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import discord

from bot.engine.verify import (
    VerifyItem,
    has_stale_pending,
    items_by_status,
)
from bot.platform.formatting import format_age

# Colour bands signal board state at a glance in the forum sidebar preview.
_COLOR_EMPTY = 0x95a5a6   # neutral grey — nothing to check
_COLOR_PENDING = 0xf1c40f  # amber — things pending
_COLOR_STALE = 0xe74c3c    # red — pending >24h


def _fmt_age(created_at: str) -> str:
    try:
        ts = datetime.fromisoformat(created_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return ""
    return format_age(datetime.now(timezone.utc) - ts)


def _fmt_origin(item: VerifyItem) -> str:
    """Return backlink suffix ← t-2842 (as a Discord thread link if possible)."""
    name = item.origin_thread_name or ""
    if not name:
        return ""
    if item.origin_thread_id:
        return f"← <#{item.origin_thread_id}>"
    return f"← `{name}`"


def _fmt_item_line(idx: int, item: VerifyItem) -> str:
    """One pending/claimed line: `1. text   ← t-2842`."""
    origin = _fmt_origin(item)
    line = f"**{idx}.** {item.text}"
    if origin:
        line += f"  {origin}"
    return line


def _fmt_done_line(item: VerifyItem) -> str:
    age = _fmt_age(item.resolved_at or item.created_at)
    age_str = f" · {age}" if age else ""
    return f"✅ {item.text}{age_str}"


# Discord embed.description caps at 4096 chars — stay well under to leave
# room for the counts header and lane separators.
_DESC_BUDGET = 3800


def build_board_embed(repo_name: str, items: list[dict]) -> discord.Embed:
    """Build the living Verify Board embed.

    All content lives in `embed.description` (4096-char budget) rather
    than fields (1024-char per-field cap) so that 25 pending items
    don't overflow a single field and silently break the edit.
    """
    buckets = items_by_status(items)
    pending = buckets.get("pending", [])
    claimed = buckets.get("claimed", [])
    done = buckets.get("done", [])

    # Only show items resolved in the last 24h in the DONE TODAY lane.
    now = datetime.now(timezone.utc)
    cutoff_24h = now - timedelta(hours=24)

    def _resolved_recently(vi: VerifyItem) -> bool:
        if not vi.resolved_at:
            return False
        try:
            ts = datetime.fromisoformat(vi.resolved_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return False
        return ts >= cutoff_24h

    done_today = [vi for vi in done if _resolved_recently(vi)]
    done_older = len(done) - len(done_today)

    # Colour — signals board state at a glance in the forum sidebar preview.
    if not pending and not claimed:
        color = _COLOR_EMPTY
    elif has_stale_pending(items, hours=24):
        color = _COLOR_STALE
    else:
        color = _COLOR_PENDING

    sections: list[str] = []

    if not pending and not claimed and not done_today:
        sections.append(
            "_No items to verify. Add one manually or let a session "
            "auto-populate._",
        )
    else:
        # Counts header (shown only when there's something to count)
        counts = (
            f"**{len(pending)}** pending · **{len(claimed)}** claimed · "
            f"**{len(done_today)}** done today"
        )
        if done_older:
            counts += f" · {done_older} older"
        sections.append(counts)

        if pending:
            lines = [
                _fmt_item_line(i + 1, vi) for i, vi in enumerate(pending)
            ]
            sections.append("**NEEDS CHECK**\n" + "\n".join(lines))
        if claimed:
            start = len(pending) + 1
            lines = [
                _fmt_item_line(start + i, vi) for i, vi in enumerate(claimed)
            ]
            sections.append("**CLAIMED**\n" + "\n".join(lines))
        if done_today:
            if len(done_today) > 5:
                sections.append(
                    f"**DONE TODAY** — _{len(done_today)} verified in the "
                    f"last 24h_",
                )
            else:
                lines = [_fmt_done_line(vi) for vi in done_today]
                sections.append("**DONE TODAY**\n" + "\n".join(lines))

    description = "\n\n".join(sections)

    # Budget enforcement — truncate at the last newline before the cutoff
    # so we don't split mid-word or mid-line.
    if len(description) > _DESC_BUDGET:
        cutoff = _DESC_BUDGET - 50  # leave headroom for the truncation notice
        cut = description.rfind("\n", 0, cutoff)
        description = description[: cut if cut > 0 else cutoff].rstrip()
        description += "\n\n…_truncated — use History for full list_"

    return discord.Embed(
        title=f"Verify Board — {repo_name}",
        description=description,
        color=color,
    )


def _openable_items(items: list[dict]) -> list[VerifyItem]:
    """All pending+claimed items, newest-first, capped at 25 (select-menu limit)."""
    buckets = items_by_status(items)
    combined = list(buckets.get("pending", [])) + list(buckets.get("claimed", []))
    return combined[:25]


def build_board_view(repo_name: str, items: list[dict]) -> discord.ui.View:
    """Action row for the Verify Board — buttons only (no always-open selects).

    The select-menu for bulk done/claim/dismiss opens as an ephemeral
    follow-up when its button is clicked, so it doesn't eat one of the
    five permanent rows on the board view.
    """
    has_open = bool(_openable_items(items))
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Mark done",
        style=discord.ButtonStyle.success,
        custom_id=f"verify_menu:done:{repo_name}",
        emoji="✅",  # ✅
        disabled=not has_open,
        row=0,
    ))
    view.add_item(discord.ui.Button(
        label="Claim",
        style=discord.ButtonStyle.primary,
        custom_id=f"verify_menu:claim:{repo_name}",
        disabled=not has_open,
        row=0,
    ))
    view.add_item(discord.ui.Button(
        label="Dismiss",
        style=discord.ButtonStyle.secondary,
        custom_id=f"verify_menu:dismiss:{repo_name}",
        emoji="✖️",  # ✖️
        disabled=not has_open,
        row=0,
    ))
    view.add_item(discord.ui.Button(
        label="Add",
        style=discord.ButtonStyle.primary,
        custom_id=f"verify_add:{repo_name}",
        row=1,
    ))
    view.add_item(discord.ui.Button(
        label="History",
        style=discord.ButtonStyle.secondary,
        custom_id=f"verify_history:{repo_name}",
        row=1,
    ))
    return view


def build_select_options(items: list[dict]) -> list[discord.SelectOption]:
    """Select-menu options for the ephemeral bulk action popup."""
    options: list[discord.SelectOption] = []
    for vi in _openable_items(items):
        label = vi.text if len(vi.text) <= 100 else vi.text[:99] + "…"
        desc_parts = []
        if vi.origin_thread_name:
            desc_parts.append(vi.origin_thread_name)
        if vi.status == "claimed":
            desc_parts.append("claimed")
        description = " · ".join(desc_parts)[:100] if desc_parts else None
        options.append(
            discord.SelectOption(label=label, value=vi.id, description=description),
        )
    return options


def build_history_embed(repo_name: str, items: list[dict]) -> discord.Embed:
    """Ephemeral compact list of resolved items in the last 30 days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    buckets = items_by_status(items)
    recent: list[VerifyItem] = []
    for status in ("done", "dismissed"):
        for vi in buckets.get(status, []):
            ts_raw = vi.resolved_at or vi.created_at
            try:
                ts = datetime.fromisoformat(ts_raw or "")
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if ts >= cutoff:
                recent.append(vi)
    recent.sort(
        key=lambda v: v.resolved_at or v.created_at or "", reverse=True,
    )

    embed = discord.Embed(
        title=f"Verify Board history — {repo_name}",
        color=_COLOR_EMPTY,
    )
    if not recent:
        embed.description = "_No resolved items in the last 30 days._"
        return embed

    lines: list[str] = []
    truncated_count = 0
    running_len = 0
    for vi in recent:
        icon = "✅" if vi.status == "done" else "✖"
        age = _fmt_age(vi.resolved_at or vi.created_at)
        age_str = f" · {age}" if age else ""
        line = f"{icon} {vi.text}{age_str}"
        # Keep under the description budget with margin for footer/meta
        if running_len + len(line) + 1 > _DESC_BUDGET:
            truncated_count = len(recent) - len(lines)
            break
        lines.append(line)
        running_len += len(line) + 1
    if lines:
        embed.description = "\n".join(lines)
    else:
        # First item already over budget — render a placeholder rather
        # than an empty embed description (looks broken in Discord).
        embed.description = (
            f"_{len(recent)} resolved item(s) too large to preview — "
            f"check state.json directly._"
        )
    if truncated_count:
        embed.set_footer(text=f"…and {truncated_count} more older items")
    return embed
