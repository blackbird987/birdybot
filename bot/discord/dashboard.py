"""Dashboard embed generation and refresh for Discord lobby.

Builds the pinned dashboard embed showing running instances, costs,
projects, and attention items. Extracted from bot.py for isolation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from bot import config
from bot.discord.access import load_access_config
from bot.platform.formatting import mode_label

if TYPE_CHECKING:
    from bot.discord.forums import ForumManager, ForumProject
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


def build_dashboard_embed(
    store: StateStore,
    forum_projects: dict[str, ForumProject],
    orphan_count: int = 0,
    usage_text: str | None = None,
) -> discord.Embed:
    """Build the global dashboard embed — lightweight overview.

    Per-instance details (running list, completed list) now live in
    each repo's per-repo control room. This dashboard (pinned in The Ark)
    shows attention items (with repo labels for navigation), global counts,
    project links, and cost.

    Pure function — no side effects, no Discord API calls.
    """
    today_cost = store.get_daily_cost()
    total_cost = store.get_total_cost()
    active_repo, _ = store.get_active_repo()

    embed = discord.Embed(
        title="The Ark",
        color=discord.Color.blurple(),
    )

    # Needs Attention — kept detailed with repo labels + thread links
    # so user knows which per-repo control room to check
    attention = store.needs_attention()
    if attention:
        # Build session_id -> thread_id map for attention links
        session_to_thread: dict[str, str] = {}
        for proj in forum_projects.values():
            for tid, info in proj.threads.items():
                if info.session_id:
                    session_to_thread[info.session_id] = tid

        attn_lines = []
        for inst in attention[:10]:
            icon = "\u2753" if inst.needs_input else "\u274c"
            line = f"{icon} `{inst.display_id()}` \u2014 {inst.prompt[:30]}"
            if inst.repo_name:
                line = f"**{inst.repo_name}** {line}"
            thread_id = session_to_thread.get(inst.session_id or "")
            if thread_id:
                line += f" \u2022 <#{thread_id}>"
            attn_lines.append(line)
        field_val = "\n".join(attn_lines)
        if len(field_val) > 1024:
            field_val = field_val[:1021] + "..."
        embed.add_field(
            name=f"Needs Attention ({len(attention)})",
            value=field_val, inline=False,
        )

    # Global running count (details in per-repo control rooms)
    running_count = store.running_count()
    embed.add_field(name="Running", value=str(running_count), inline=True)

    # Projects with forum links (primary navigation hub)
    if forum_projects:
        proj_lines = []
        for name, proj in forum_projects.items():
            if proj.forum_channel_id and name != "_default":
                threads = len(proj.threads)
                marker = " *" if name == active_repo else ""
                proj_lines.append(f"<#{proj.forum_channel_id}> ({threads} threads){marker}")
        if proj_lines:
            embed.add_field(name="Projects", value="\n".join(proj_lines), inline=False)

    # Usage bar (pre-fetched by async caller) or fallback cost fields
    if usage_text:
        embed.add_field(name="Usage", value=usage_text, inline=False)
    else:
        embed.add_field(name="Today", value=f"${today_cost:.4f}", inline=True)
        embed.add_field(name="Total", value=f"${total_cost:.4f}", inline=True)

    embed.add_field(name="Mode", value=mode_label(store.mode), inline=True)
    embed.add_field(name="PC", value=config.PC_NAME, inline=True)
    if orphan_count:
        embed.add_field(
            name="Orphans", value=f"{orphan_count} branch/worktree",
            inline=True,
        )

    return embed


def _count_orphans(store: StateStore) -> int:
    """Count orphaned branches and worktree directories across all repos.

    Runs subprocess calls (git branch --list) — must be called from a thread.
    """
    from pathlib import Path
    from bot.claude.runner import ClaudeRunner

    instances = store.list_instances()
    active_branches = {i.branch for i in instances if i.branch}
    active_worktrees = {i.worktree_path for i in instances if i.worktree_path}
    total = 0
    for _rname, rpath in store.list_repos().items():
        if not Path(rpath).is_dir():
            continue
        total += len(ClaudeRunner.scan_orphan_branches(rpath, active_branches))
        total += len(ClaudeRunner.scan_orphan_worktrees(rpath, active_worktrees))
    return total


async def refresh_dashboard(
    client: discord.Client,
    store: StateStore,
    forums: ForumManager,
    lobby_channel_id: int | None,
    dashboard_lock: asyncio.Lock,
    dashboard_pending: list[bool],
) -> None:
    """Update or create the pinned dashboard embed in lobby.

    Serialized: only one refresh runs at a time. If more requests arrive
    while one is running, exactly one additional refresh is queued.

    dashboard_pending is a mutable list[bool] used as a flag.
    """
    if dashboard_lock.locked():
        dashboard_pending[0] = True
        return

    async with dashboard_lock:
        await _refresh_dashboard_impl(client, store, forums, lobby_channel_id)
        while dashboard_pending[0]:
            dashboard_pending[0] = False
            await _refresh_dashboard_impl(client, store, forums, lobby_channel_id)


async def _refresh_dashboard_impl(
    client: discord.Client,
    store: StateStore,
    forums: ForumManager,
    lobby_channel_id: int | None,
) -> None:
    """Inner dashboard refresh logic."""
    if not lobby_channel_id:
        return
    lobby = client.get_channel(lobby_channel_id)
    if not lobby or not isinstance(lobby, discord.TextChannel):
        return

    # Count orphaned branches/worktrees in a thread (best-effort, don't block dashboard)
    # Pre-fetch usage bar from ccusage (async)
    from bot.engine.usage import get_usage_bar_async
    try:
        orphan_count = await asyncio.to_thread(_count_orphans, store)
    except Exception:
        orphan_count = 0
    try:
        usage_text = await get_usage_bar_async()
    except Exception:
        usage_text = None
    embed = build_dashboard_embed(store, forums.forum_projects, orphan_count, usage_text=usage_text)

    # Get or create dashboard message
    dash_msg_id = store.get_platform_state("discord").get("dashboard_message_id")

    try:
        if dash_msg_id:
            try:
                msg = await lobby.fetch_message(int(dash_msg_id))
                await msg.edit(embed=embed)
            except (discord.NotFound, discord.HTTPException):
                dash_msg_id = None  # Message gone, create new one

        if not dash_msg_id:
            msg = await lobby.send(embed=embed)
            try:
                await msg.pin()
            except Exception:
                pass
            # Re-fetch state after await to avoid clobbering concurrent mutations
            state = store.get_platform_state("discord")
            state["dashboard_message_id"] = str(msg.id)
            store.set_platform_state("discord", state, persist=True)
    except Exception:
        log.debug("Failed to update dashboard", exc_info=True)

    # Always refresh per-repo control rooms, independent of dashboard success
    async def _safe_refresh(coro):
        try:
            await coro
        except Exception:
            log.debug("Control room refresh failed", exc_info=True)

    for rname, proj in forums.forum_projects.items():
        if proj.control_thread_id:
            asyncio.create_task(_safe_refresh(forums.refresh_control_room(rname, usage_bar=usage_text)))

    # Refresh user forum control rooms
    cfg = load_access_config()
    for uid, ua in cfg.users.items():
        if ua.control_thread_id:
            asyncio.create_task(_safe_refresh(forums.refresh_user_control_room(uid)))
