"""Verify Board: embed + view builders.

Pure rendering — takes a ForumProject's verify_items list and produces
a discord.Embed and a discord.ui.View. No I/O, no fetches. Origin
backlinks are passive Discord URL strings (never fetch the origin
thread).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from bot.engine import verify as verify_mod

if TYPE_CHECKING:
    from bot.discord.forums import ForumProject


_DONE_TODAY_CAP = 5
_NEEDS_CHECK_NUM_CAP = 25  # Discord select-menu limit; trim board view past this


# --- Time formatting ---


def _relative(iso_ts: str | None) -> str:
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return ""
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


# --- Backlink ---


def _backlink(guild_id: int, item: dict) -> str:
    """Render the origin backlink as plain text or a Discord URL.

    Never fetches anything; if the thread is gone Discord will surface
    a broken link but the board itself stays consistent.
    """
    name = item.get("origin_thread_name")
    tid = item.get("origin_thread_id")
    if not name and not tid:
        return ""
    label = name or f"thread {str(tid)[-6:]}"
    if tid:
        url = f"https://discord.com/channels/{guild_id}/{tid}"
        return f"[← {label}]({url})"
    return f"← {label}"


# --- Lane rendering ---


def _render_needs_check(items: list[dict], guild_id: int) -> str:
    if not items:
        return ""
    lines = ["**NEEDS CHECK**"]
    for idx, item in enumerate(items[:_NEEDS_CHECK_NUM_CAP], start=1):
        text = item.get("text", "")
        link = _backlink(guild_id, item)
        suffix = f"   {link}" if link else ""
        lines.append(f"{idx}. {text}{suffix}")
    if len(items) > _NEEDS_CHECK_NUM_CAP:
        extra = len(items) - _NEEDS_CHECK_NUM_CAP
        lines.append(f"_+{extra} more pending — verify some to see them_")
    return "\n".join(lines)


def _render_claimed(items: list[dict], guild_id: int) -> str:
    if not items:
        return ""
    lines = ["**CLAIMED**"]
    for item in items:
        text = item.get("text", "")
        link = _backlink(guild_id, item)
        suffix = f"   {link}" if link else ""
        lines.append(f"· {text}{suffix}")
    return "\n".join(lines)


def _render_done_today(items: list[dict]) -> str:
    if not items:
        return ""
    items = sorted(items, key=lambda i: i.get("resolved_at") or "", reverse=True)
    lines = ["**DONE TODAY**"]
    visible = items[:_DONE_TODAY_CAP]
    for item in visible:
        mark = "✅" if item.get("status") == "done" else "✖"
        text = item.get("text", "")
        rel = _relative(item.get("resolved_at"))
        rel_part = f" · {rel}" if rel else ""
        lines.append(f"{mark} {text}{rel_part}")
    if len(items) > _DONE_TODAY_CAP:
        lines.append(f"_+{len(items) - _DONE_TODAY_CAP} more in last 24h — see History_")
    return "\n".join(lines)


# --- Embed ---


def build_board_embed(proj: "ForumProject", guild_id: int = 0) -> discord.Embed:
    """Build the living verify-board embed for a project."""
    items = proj.verify_items
    needs = verify_mod.get_by_lane(items, "needs_check")
    claimed = verify_mod.get_by_lane(items, "claimed")
    done_today = verify_mod.get_by_lane(items, "done_today")

    n_pending = len(needs)
    n_claimed = len(claimed)
    n_done = len(done_today)

    if n_pending == 0 and n_claimed == 0:
        colour = discord.Color.dark_grey()
    elif verify_mod.has_stale_pending(items, hours=24):
        colour = discord.Color.from_rgb(220, 70, 70)
    else:
        colour = discord.Color.from_rgb(230, 165, 35)

    title = f"Verify Board — {proj.repo_name}"
    header = (
        f"{n_pending} pending · {n_claimed} claimed · "
        f"{n_done} done (24h)"
    )

    body_parts: list[str] = [header]

    if n_pending == 0 and n_claimed == 0 and n_done == 0:
        body_parts.append(
            "\n_No pending items. Tap **Add** to log something to verify, "
            "or use **Send to Verify Board** on a session result._"
        )
    else:
        needs_block = _render_needs_check(needs, guild_id)
        claimed_block = _render_claimed(claimed, guild_id)
        done_block = _render_done_today(done_today)
        for block in (needs_block, claimed_block, done_block):
            if block:
                body_parts.append("")
                body_parts.append(block)

    description = "\n".join(body_parts)
    if len(description) > 4000:
        description = description[:3997] + "…"

    embed = discord.Embed(
        title=title,
        description=description,
        color=colour,
    )
    return embed


# --- View ---


def build_board_view(repo_name: str, proj: "ForumProject") -> discord.ui.View:
    """Action row: Mark done / Claim / Dismiss / Add / History.

    Buttons are always rendered — disabling them when no items exist would
    confuse user; the menu handler explains "no items" if they tap it.
    """
    view = discord.ui.View(timeout=None)

    view.add_item(discord.ui.Button(
        label="Mark done ✅",
        style=discord.ButtonStyle.success,
        custom_id=f"verify_menu:done:{repo_name}",
        row=0,
    ))
    view.add_item(discord.ui.Button(
        label="Claim",
        style=discord.ButtonStyle.secondary,
        custom_id=f"verify_menu:claim:{repo_name}",
        row=0,
    ))
    view.add_item(discord.ui.Button(
        label="Dismiss ✖",
        style=discord.ButtonStyle.secondary,
        custom_id=f"verify_menu:dismiss:{repo_name}",
        row=0,
    ))
    view.add_item(discord.ui.Button(
        label="Add",
        style=discord.ButtonStyle.primary,
        custom_id=f"verify_add:{repo_name}",
        row=0,
    ))
    view.add_item(discord.ui.Button(
        label="History",
        style=discord.ButtonStyle.secondary,
        custom_id=f"verify_history:{repo_name}",
        row=0,
    ))

    return view


# --- Select menu builder for done/claim/dismiss flow ---


def build_lane_select_view(
    repo_name: str, action: str, verb: str, items: list[dict],
) -> discord.ui.View:
    """Build an ephemeral select menu for bulk status changes.

    `action` is one of "done", "claim", "dismiss" — carried through the
    custom_id so the verify_select handler can dispatch. `verb` is the
    human-readable verb phrase ("mark done", "claim", "dismiss") used in
    the placeholder copy.
    """
    view = discord.ui.View(timeout=120)
    if not items:
        return view

    capped = items[:25]
    options = []
    for item in capped:
        text = item.get("text", "")
        label = text if len(text) <= 100 else text[:97] + "..."
        options.append(discord.SelectOption(
            label=label,
            value=item.get("id", ""),
        ))

    select = discord.ui.Select(
        custom_id=f"verify_select:{action}:{repo_name}",
        placeholder=f"Select item(s) to {verb}...",
        min_values=1,
        max_values=len(options),
        options=options,
    )
    view.add_item(select)
    return view


# --- History (ephemeral) ---


def render_history_text(proj: "ForumProject", days: int = 30) -> str:
    """Compact 30d history of resolved items, plus current pending/claimed."""
    items = proj.verify_items
    needs = verify_mod.get_by_lane(items, "needs_check")
    claimed = verify_mod.get_by_lane(items, "claimed")
    recent = verify_mod.get_by_lane(items, "done_recent")

    lines: list[str] = [f"**Verify Board history — {proj.repo_name}**"]
    lines.append("")
    lines.append(
        f"{len(needs)} pending · {len(claimed)} claimed · "
        f"{len(recent)} resolved in last {days}d"
    )

    if needs:
        lines.append("")
        lines.append("__Pending__")
        for i in needs[:10]:
            lines.append(f"· {i.get('text', '')}")
        if len(needs) > 10:
            lines.append(f"_(+{len(needs) - 10} more)_")

    if claimed:
        lines.append("")
        lines.append("__Claimed__")
        for i in claimed[:10]:
            lines.append(f"· {i.get('text', '')}")

    if recent:
        lines.append("")
        lines.append("__Resolved__")
        sorted_recent = sorted(
            recent, key=lambda i: i.get("resolved_at") or "", reverse=True,
        )
        for i in sorted_recent[:20]:
            mark = "✅" if i.get("status") == "done" else "✖"
            rel = _relative(i.get("resolved_at"))
            rel_part = f" · {rel}" if rel else ""
            lines.append(f"{mark} {i.get('text', '')}{rel_part}")
        if len(recent) > 20:
            lines.append(f"_(+{len(recent) - 20} more)_")

    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1897] + "..."
    return text
