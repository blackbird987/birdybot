"""Dashboard embed generation and refresh for Discord lobby.

Builds the pinned dashboard embed showing running instances, costs,
projects, and attention items. Extracted from bot.py for isolation.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from typing import TYPE_CHECKING

import discord

from bot import config
from bot.claude.types import InstanceStatus
from bot.discord.access import load_access_config
from bot.platform.formatting import format_relative_time

if TYPE_CHECKING:
    from bot.discord.forums import ForumManager, ForumProject
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistent Ark button view
# ---------------------------------------------------------------------------

class ArkView(discord.ui.View):
    """Button view for The Ark dashboard.

    NOT registered via ``add_view()`` — this bot uses centralized
    ``on_interaction`` dispatch (interactions.handle), not per-view callbacks.
    Buttons stay interactive because ``timeout=None``.
    """

    def __init__(self, running_count: int = 0):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="New Repo",
            style=discord.ButtonStyle.green,
            custom_id="ark:new_repo",
            emoji="\u2795",
            row=0,
        ))
        if running_count > 0:
            self.add_item(discord.ui.Button(
                label=f"Stop All ({running_count})",
                style=discord.ButtonStyle.danger,
                custom_id="ark:stop_all",
                row=0,
            ))
        self.add_item(discord.ui.Button(
            label="Refresh",
            style=discord.ButtonStyle.secondary,
            custom_id="ark:refresh",
            row=0,
        ))


def build_dashboard_embed(
    store: StateStore,
    forum_projects: dict[str, ForumProject],
    orphan_count: int = 0,
    usage_text: str | None = None,
    start_time: float = 0.0,
) -> discord.Embed:
    """Build the global dashboard embed — mobile-first command center.

    Shows actionable items (attention, idle, failed) with clickable thread
    links, global stats, project navigation, cost, and system health.

    Pure function — no side effects, no Discord API calls.
    """
    active_repo, _ = store.get_active_repo()

    embed = discord.Embed(
        title="The Ark",
        color=discord.Color.blurple(),
    )

    # --- Build session_id -> thread_id map (used by multiple sections) ---
    session_to_thread: dict[str, str] = {}
    for proj in forum_projects.values():
        for tid, info in proj.threads.items():
            if info.session_id:
                session_to_thread[info.session_id] = tid

    # --- Combined item cap for mobile readability ---
    MAX_LINKED_ITEMS = 12
    items_remaining = MAX_LINKED_ITEMS

    # --- Needs Attention (failed + needs_input) — clickable links ---
    attention = store.needs_attention()
    attention_session_ids: set[str] = set()
    if attention:
        attn_lines = []
        shown = attention[:min(10, items_remaining)]
        for inst in shown:
            if inst.session_id:
                attention_session_ids.add(inst.session_id)
            icon = "\u2753" if inst.needs_input else "\u274c"
            line = f"{icon} `{inst.display_id()}` \u2014 {inst.prompt[:30]}"
            if inst.repo_name:
                line = f"**{inst.repo_name}** {line}"
            thread_id = session_to_thread.get(inst.session_id or "")
            if thread_id:
                line += f" \u2022 <#{thread_id}>"
            attn_lines.append(line)
        items_remaining -= len(shown)
        field_val = "\n".join(attn_lines)
        if len(field_val) > 1024:
            field_val = field_val[:1021] + "..."
        embed.add_field(
            name=f"\u26a0\ufe0f Needs Attention ({len(attention)})",
            value=field_val, inline=False,
        )

    # --- Idle Sessions (completed recently, not running — waiting for user) ---
    # Exclude sessions already shown in Needs Attention
    idle = store.idle_sessions(max_age_hours=2)
    idle_with_threads = []
    for inst in idle:
        if inst.session_id and inst.session_id in attention_session_ids:
            continue
        tid = session_to_thread.get(inst.session_id or "")
        if tid:
            idle_with_threads.append((inst, tid))
    if idle_with_threads and items_remaining > 0:
        idle_lines = []
        for inst, tid in idle_with_threads[:min(8, items_remaining)]:
            label = inst.prompt[:30] if inst.prompt else "session"
            repo_prefix = f"**{inst.repo_name}** " if inst.repo_name else ""
            idle_lines.append(
                f"\U0001f4ad {repo_prefix}`{inst.display_id()}` "
                f"\u2014 {label} \u2022 <#{tid}>"
            )
        items_remaining -= len(idle_lines)
        field_val = "\n".join(idle_lines)
        if len(field_val) > 1024:
            field_val = field_val[:1021] + "..."
        embed.add_field(
            name=f"\U0001f4ad Idle ({len(idle_with_threads)})",
            value=field_val, inline=False,
        )

    # --- Failed Recently (last 6h, not already in attention) ---
    attention_ids = {i.id for i in attention} if attention else set()
    recent_fails = [
        f for f in store.recent_failures(hours=6) if f.id not in attention_ids
    ]
    if recent_fails and items_remaining > 0:
        fail_lines = []
        for inst in recent_fails[:min(5, items_remaining)]:
            repo_prefix = f"**{inst.repo_name}** " if inst.repo_name else ""
            tid = session_to_thread.get(inst.session_id or "")
            link = f" \u2022 <#{tid}>" if tid else ""
            fail_lines.append(
                f"\u274c {repo_prefix}`{inst.display_id()}` "
                f"\u2014 {inst.prompt[:30]}{link}"
            )
        items_remaining -= len(fail_lines)
        field_val = "\n".join(fail_lines)
        if len(field_val) > 1024:
            field_val = field_val[:1021] + "..."
        embed.add_field(
            name=f"Failed Recently ({len(recent_fails)})",
            value=field_val, inline=False,
        )

    # --- Running / Today / Scheduled (inline row) ---
    running_count = store.running_count()
    embed.add_field(name="Running", value=str(running_count), inline=True)
    embed.add_field(name="Today", value=str(store.instance_count_today()), inline=True)
    sched_count = len(store.list_schedules())
    if sched_count:
        embed.add_field(name="Scheduled", value=str(sched_count), inline=True)

    # --- Projects (enhanced with per-repo running counts) ---
    if forum_projects:
        running_by_repo: dict[str, int] = {}
        for inst in store.list_by_status(InstanceStatus.RUNNING):
            if inst.repo_name:
                running_by_repo[inst.repo_name] = running_by_repo.get(inst.repo_name, 0) + 1

        proj_lines = []
        for name, proj in forum_projects.items():
            if proj.forum_channel_id and name != "_default":
                threads = len(proj.threads)
                parts = [f"<#{proj.forum_channel_id}> ({threads} threads)"]
                running = running_by_repo.get(name, 0)
                if running:
                    parts.append(f"\u2022 {running} running")
                if name == active_repo:
                    parts.append("*")
                proj_lines.append(" ".join(parts))
        if proj_lines:
            embed.add_field(name="Projects", value="\n".join(proj_lines), inline=False)

    # --- Usage ---
    if usage_text:
        embed.add_field(name="Usage", value=usage_text, inline=False)
    else:
        embed.add_field(name="Usage", value="Usage data unavailable", inline=False)

    # --- Last Activity ---
    last = store.last_activity()
    if last:
        from datetime import datetime, timezone
        try:
            created = datetime.fromisoformat(last.created_at)
            delta_secs = (datetime.now(timezone.utc) - created).total_seconds()
            ago = format_relative_time(delta_secs)
            if ago != "just now":
                ago += " ago"
            repo_part = f" in **{last.repo_name}**" if last.repo_name else ""
            embed.add_field(
                name="Last Activity", value=f"{ago}{repo_part}", inline=True,
            )
        except Exception:
            pass

    # --- Uptime ---
    if start_time:
        uptime_secs = _time.time() - start_time
        embed.add_field(
            name="Uptime", value=format_relative_time(uptime_secs), inline=True,
        )

    # --- PC / Orphans ---
    embed.add_field(name="PC", value=config.PC_NAME, inline=True)
    if orphan_count:
        embed.add_field(
            name="Orphans", value=f"{orphan_count} branch/worktree",
            inline=True,
        )

    # --- Footer: version ---
    embed.set_footer(text=f"v{_get_version()}")

    return embed


def _get_version() -> str:
    """Read version — try installed package first, fall back to pyproject.toml."""
    try:
        from importlib.metadata import version as pkg_version
        return pkg_version("claude-bot")
    except Exception:
        pass
    try:
        from pathlib import Path
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        match = re.search(r'version\s*=\s*"([^"]+)"', pyproject.read_text())
        if match:
            return match.group(1)
    except Exception:
        pass
    return "unknown"


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

    # Fetch orphan count and usage bar in parallel (both are independent read-only ops)
    from bot.engine.usage import get_usage_bar_async
    try:
        orphan_count = await asyncio.to_thread(_count_orphans, store)
    except Exception:
        orphan_count = 0
    try:
        usage_text = await get_usage_bar_async()
    except Exception:
        log.warning("Usage bar fetch failed", exc_info=True)
        usage_text = None
    from bot.engine.commands import get_start_time
    embed = build_dashboard_embed(
        store, forums.forum_projects, orphan_count,
        usage_text=usage_text, start_time=get_start_time(),
    )
    view = ArkView(running_count=store.running_count())

    # Get or create dashboard message
    dash_msg_id = store.get_platform_state("discord").get("dashboard_message_id")

    try:
        if dash_msg_id:
            try:
                msg = await lobby.fetch_message(int(dash_msg_id))
                await msg.edit(embed=embed, view=view)
            except (discord.NotFound, discord.HTTPException):
                dash_msg_id = None  # Message gone, create new one

        if not dash_msg_id:
            msg = await lobby.send(embed=embed, view=view)
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


async def start_periodic_refresh(
    client: discord.Client,
    store: StateStore,
    forums: ForumManager,
    lobby_channel_id: int | None,
    dashboard_lock: asyncio.Lock,
    dashboard_pending: list[bool],
    interval_seconds: int = 300,
) -> None:
    """Refresh dashboard periodically to keep usage data current."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await refresh_dashboard(
                client, store, forums,
                lobby_channel_id, dashboard_lock, dashboard_pending,
            )
        except Exception:
            log.warning("Periodic dashboard refresh failed", exc_info=True)
