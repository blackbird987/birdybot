"""Dashboard embed generation and refresh for Discord lobby.

Builds the pinned dashboard embed showing running instances, costs,
projects, and attention items. Extracted from bot.py for isolation.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from typing import TYPE_CHECKING, Callable

import discord

from bot.textutil import parse_iso_utc
from bot import config
from bot.claude.auth_health import split_accounts
from bot.claude.types import InstanceStatus
from bot.discord.access import load_access_config
from bot.platform.formatting import format_relative_time

if TYPE_CHECKING:
    from bot.discord.forums import ForumManager, ForumProject
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


def _preview(text: str | None, limit: int = 30) -> str:
    """One-line prompt preview, trimmed to *limit* with a … when cut.

    Collapses newlines so a multi-line prompt can't break the dashboard
    field layout, and signals truncation so the user knows there's more.
    """
    s = " ".join((text or "").split())
    if not s:
        return "session"
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


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
        self.add_item(discord.ui.Button(
            label="Claude Login",
            style=discord.ButtonStyle.primary,
            custom_id="ark:claude_login",
            row=1,
        ))
        # Provider switch button — label shows current provider
        from bot import config as _cfg
        self.add_item(discord.ui.Button(
            label=f"Provider: {_cfg.PROVIDER}",
            style=discord.ButtonStyle.secondary,
            custom_id="ark:switch_provider",
            row=1,
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

    No Discord API calls and no state mutation. Not quite pure: the account
    label stats each account's credentials file (cached on mtime+size in
    ``auth_health``) and reads the store's sideline table, which together are
    what let a signed-out backup show as ``1/2 accts`` here instead of only
    surfacing at failover time.
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

    # --- Needs Attention (needs_input + freshly failed) — clickable links ---
    attention = store.needs_attention()
    attention_session_ids: set[str] = set()
    if attention:
        attn_lines = []
        shown = attention[:min(10, items_remaining)]
        for inst in shown:
            if inst.session_id:
                attention_session_ids.add(inst.session_id)
            icon = "\u2753" if inst.needs_input else "\u274c"
            line = f"{icon} `{inst.display_id()}` \u2014 {_preview(inst.prompt)}"
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
            label = _preview(inst.prompt)
            repo_prefix = f"**{inst.repo_name}** " if inst.repo_name else ""
            idle_lines.append(
                f"{repo_prefix}`{inst.display_id()}` "
                f"\u2014 {label} \u2022 <#{tid}>"
            )
        items_remaining -= len(idle_lines)
        field_val = "\n".join(idle_lines)
        if len(field_val) > 1024:
            field_val = field_val[:1021] + "..."
        embed.add_field(
            name=f"Idle ({len(idle_with_threads)})",
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
                f"\u2014 {_preview(inst.prompt)}{link}"
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
        hidden_projects = 0
        for name, proj in forum_projects.items():
            if proj.forum_channel_id and name != "_default":
                # A hidden repo is deliberately absent here: that is what
                # hiding is for. It is counted, not listed, so it can never
                # go missing without a trace.
                if store.is_repo_dormant(name):
                    hidden_projects += 1
                    continue
                threads = len(proj.threads)
                parts = [f"<#{proj.forum_channel_id}> ({threads} threads)"]
                running = running_by_repo.get(name, 0)
                if running:
                    parts.append(f"\u2022 {running} running")
                if name == active_repo:
                    parts.append("*")
                proj_lines.append(" ".join(parts))
        if hidden_projects:
            proj_lines.append(
                f"-# {hidden_projects} hidden \u00b7 `/repo list`")
        if proj_lines:
            embed.add_field(name="Projects", value="\n".join(proj_lines), inline=False)

    # --- Usage ---
    usage_label = f"Usage · {config.PLAN_NAME}"
    total_accts = len(config.CLAUDE_ACCOUNTS)
    if total_accts > 1:
        # Show usable/total when one is signed out, so a dead backup can't
        # masquerade as working failover the way klerk did for five weeks.
        healthy, _ = split_accounts(
            config.CLAUDE_ACCOUNTS, store.sidelined_accounts(),
        )
        usable_accts = len(healthy)
        if usable_accts < total_accts:
            usage_label += f" · {usable_accts}/{total_accts} accts"
        else:
            usage_label += f" · {total_accts} accts"
    if usage_text:
        embed.add_field(name=usage_label, value=usage_text, inline=False)
    else:
        embed.add_field(name=usage_label, value="Usage data unavailable", inline=False)

    # --- Last Activity ---
    last = store.last_activity()
    if last:
        from datetime import datetime, timezone
        try:
            created = parse_iso_utc(last.created_at)
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
    """Read version — pyproject.toml first (source-run is the dev path),
    fall back to installed package metadata for true-installed deploys.

    importlib.metadata caches the version recorded at `pip install -e .`
    time, so it stays stale after every pyproject bump until reinstall.
    Reading the file first means a fresh bump is visible on the next bot
    restart with no `pip install` step.
    """
    try:
        from pathlib import Path
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        match = re.search(r'version\s*=\s*"([^"]+)"', pyproject.read_text())
        if match:
            return match.group(1)
    except Exception:
        pass
    try:
        from importlib.metadata import version as pkg_version
        return pkg_version("claude-bot")
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



# ---------------------------------------------------------------------------
# Refresh: one object owns every redraw of The Ark and the Control Rooms
# ---------------------------------------------------------------------------

# A burst of triggers (a chain step finishing fires several) is gathered for
# this long before the first redraw, and redraws are at least MIN_INTERVAL
# apart. Before this, every trigger redrew The Ark and fired one unbounded
# edit per Control Room, which is what kept the bot on Discord's global rate
# limit (docs/design/ark-refresh.md).
DEBOUNCE_SECONDS = 3.0
MIN_INTERVAL_SECONDS = 15.0
PERIODIC_SECONDS = 300


def _room_signature(store: StateStore, repo_name: str, proj: ForumProject,
                    attention_ids: tuple) -> str:
    """What a repo's Control Room is drawn from, cheaply, without rendering.

    A pass redraws only the rooms whose signature moved since the last one
    that drew them. What this misses (a branch switch, the elapsed minutes on
    a running line) the periodic sweep picks up, since it renders every room
    and lets the unchanged-render skip drop the ones that did not move.
    """
    insts = store.list_by_repo(repo_name)
    running = tuple(i.id for i in insts if i.status == InstanceStatus.RUNNING)
    completed = tuple(
        i.id for i in insts
        if i.status == InstanceStatus.COMPLETED and not i.needs_input
    )[:5]
    try:
        cost = round(store.get_repo_daily_cost(repo_name), 4)
    except Exception:
        cost = None
    try:
        deploy = repr(store.get_deploy_state(repo_name))
    except Exception:
        deploy = None
    return repr((running, attention_ids, completed, cost, deploy,
                 proj.control_message_id, len(proj.threads)))


class DashboardRefresher:
    """Coalesces every refresh request into bounded redraws.

    ``request()`` is cheap and safe to call from anywhere, as often as
    anything likes: the first one schedules a pass ``debounce`` seconds out,
    and any that arrive before or during that pass fold into it or into the
    one follow-up pass after it. Passes are at least ``min_interval`` apart.
    ``refresh_now()`` is for a person waiting on the result (the Refresh
    button) and for startup: it runs a pass at once, still one at a time.

    A pass redraws The Ark when its render changed, then the Control Rooms
    whose signature changed, skipping hidden repos. A sweep (startup, the
    periodic tick, Refresh) renders every visible room and the personal ones;
    an unchanged render still sends nothing.
    """

    def __init__(
        self,
        client: discord.Client,
        store: StateStore,
        forums: ForumManager,
        lobby_channel_id: Callable[[], int | None],
        *,
        debounce: float = DEBOUNCE_SECONDS,
        min_interval: float = MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = _time.monotonic,
    ) -> None:
        self._client = client
        self._store = store
        self._forums = forums
        self._lobby_channel_id = lobby_channel_id
        self._debounce = debounce
        self._min_interval = min_interval
        self._clock = clock
        self._wanted = False
        self._sweep = False
        self._task: asyncio.Task | None = None
        self._pass_lock = asyncio.Lock()
        self._last_pass: float | None = None
        self._ark_digest: str | None = None
        self._room_sigs: dict[str, str] = {}
        self.passes = 0  # how many passes ran; read by the harness

    def request(self, *, sweep: bool = False) -> None:
        """Ask for a redraw soon. Never blocks and never raises."""
        self._wanted = True
        self._sweep = self._sweep or sweep
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def refresh_now(self) -> None:
        """Redraw everything at once (a person is waiting on it)."""
        await self._run_pass(sweep=True)

    async def _loop(self) -> None:
        while self._wanted:
            await asyncio.sleep(self._debounce)
            if self._last_pass is not None:
                wait = self._last_pass + self._min_interval - self._clock()
                if wait > 0:
                    await asyncio.sleep(wait)
            self._wanted = False
            sweep, self._sweep = self._sweep, False
            await self._run_pass(sweep=sweep)

    async def _run_pass(self, *, sweep: bool) -> None:
        async with self._pass_lock:
            self._last_pass = self._clock()
            self.passes += 1
            try:
                await self._redraw_ark()
            except Exception:
                log.warning("Dashboard refresh failed", exc_info=True)
            try:
                await self._redraw_rooms(sweep=sweep)
            except Exception:
                log.warning("Control room refresh pass failed", exc_info=True)

    # --- The Ark ---

    async def _redraw_ark(self) -> None:
        lobby_id = self._lobby_channel_id()
        if not lobby_id:
            return
        lobby = self._client.get_channel(lobby_id)
        if not lobby or not isinstance(lobby, discord.TextChannel):
            return

        from bot.engine.usage import get_usage_bar_async
        try:
            orphan_count = await asyncio.to_thread(_count_orphans, self._store)
        except Exception:
            orphan_count = 0
        try:
            usage_text = await get_usage_bar_async()
        except Exception:
            log.warning("Usage bar fetch failed", exc_info=True)
            usage_text = None
        from bot.engine.commands import get_start_time
        embed = build_dashboard_embed(
            self._store, self._forums.forum_projects, orphan_count,
            usage_text=usage_text, start_time=get_start_time(),
        )
        view = ArkView(running_count=self._store.running_count())

        from bot.discord.forums import control_digest
        digest = control_digest(embed, view)
        dash_msg_id = self._store.get_platform_state("discord").get("dashboard_message_id")
        if dash_msg_id and digest == self._ark_digest:
            return
        self._ark_digest = None
        if await edit_or_recreate_dashboard(self._store, lobby, dash_msg_id, embed, view):
            self._ark_digest = digest

    # --- Control Rooms ---

    async def _redraw_rooms(self, *, sweep: bool) -> None:
        attention_by_repo: dict[str, list] = {}
        for inst in self._store.needs_attention():
            if inst.repo_name:
                attention_by_repo.setdefault(inst.repo_name, []).append(
                    (inst.id, bool(inst.needs_input)))

        due: list[tuple[str, str]] = []
        for name, proj in list(self._forums.forum_projects.items()):
            if not proj.control_thread_id or not proj.control_message_id:
                continue
            # A hidden repo's forum is parked out of sight: nothing reads its
            # room, so nothing redraws it (docs/design/repo-hide.md).
            if self._store.is_repo_dormant(name):
                continue
            sig = _room_signature(self._store, name, proj,
                                  tuple(attention_by_repo.get(name, ())))
            if sweep or self._room_sigs.get(name) != sig:
                due.append((name, sig))

        async def one(name: str, sig: str) -> None:
            try:
                current = await self._forums.refresh_control_room(name)
            except Exception:
                log.debug("Control room refresh failed for %s", name, exc_info=True)
                current = False
            if current:
                self._room_sigs[name] = sig
            else:
                self._room_sigs.pop(name, None)  # retried by the next pass

        # The forum manager caps how many of these edit at once.
        await asyncio.gather(*(one(n, sig) for n, sig in due))

        if sweep:
            cfg = load_access_config()
            users: list[str] = []
            for uid, ua in cfg.users.items():
                if ua.control_thread_id and ua.control_room:
                    users.append(uid)

            async def user(uid: str) -> None:
                try:
                    await self._forums.refresh_user_control_room(uid)
                except Exception:
                    log.debug("User control room refresh failed for %s", uid, exc_info=True)

            await asyncio.gather(*(user(u) for u in users))


async def edit_or_recreate_dashboard(
    store: StateStore,
    lobby: discord.TextChannel,
    dash_msg_id: str | None,
    embed: discord.Embed,
    view: discord.ui.View,
) -> bool:
    """Edit the pinned dashboard in place; post a new one only if it is gone.

    Only ``NotFound`` means the message is gone. Any other error (a 429, a
    5xx, a timeout) leaves the stored id alone and returns False, so the next
    refresh tries the same message again. Treating every HTTP error as "gone"
    is what posted and pinned a fresh dashboard on each rate-limit hit and
    left The Ark with 23 pins.
    """
    if dash_msg_id:
        try:
            await lobby.get_partial_message(int(dash_msg_id)).edit(embed=embed, view=view)
            return True
        except discord.NotFound:
            log.info("Dashboard message %s is gone, posting a new one", dash_msg_id)
        except discord.HTTPException as e:
            log.warning(
                "Dashboard edit failed (HTTP %s, code %s), keeping message %s; "
                "the next refresh retries", getattr(e, "status", "?"),
                getattr(e, "code", "?"), dash_msg_id,
            )
            return False
        except ValueError:
            log.warning("Stored dashboard id %r is not a message id, posting a new one",
                        dash_msg_id)
        except Exception:
            log.warning("Dashboard edit failed, the next refresh retries", exc_info=True)
            return False

    try:
        msg = await lobby.send(embed=embed, view=view)
    except Exception:
        log.warning("Could not post the dashboard, the next refresh retries", exc_info=True)
        return False
    try:
        await msg.pin()
    except Exception:
        log.warning("Could not pin the new dashboard %s", msg.id, exc_info=True)
    # Re-fetch state after the awaits so a concurrent write is not clobbered
    state = store.get_platform_state("discord")
    state["dashboard_message_id"] = str(msg.id)
    store.set_platform_state("discord", state, persist=True)
    return True


def is_dashboard_message(msg: discord.Message, bot_user_id: int | None) -> bool:
    """Whether a lobby message is one of this bot's dashboard posts.

    Matched on the author plus either the embed title or an Ark button, so a
    pin anyone else made (or the bot made for another reason) is never one.
    """
    author = getattr(msg, "author", None)
    if bot_user_id is None or getattr(author, "id", None) != bot_user_id:
        return False
    for e in getattr(msg, "embeds", None) or []:
        if getattr(e, "title", None) == "The Ark":
            return True
    for row in getattr(msg, "components", None) or []:
        for child in getattr(row, "children", None) or []:
            if str(getattr(child, "custom_id", "") or "").startswith("ark:"):
                return True
    return False


async def unpin_stale_dashboards(
    pins: list[discord.Message],
    live_id: str | None,
    bot_user_id: int | None,
    *,
    pause_every: int = 5,
    pause_seconds: float = 1.0,
) -> int:
    """Unpin every dashboard pin but the live one; returns how many went.

    With no live id stored, the newest dashboard pin stays. Pins that are not
    dashboards are never touched. Pauses after every few unpins so a backlog
    of stale pins does not become its own burst.
    """
    dashboards = [m for m in pins if is_dashboard_message(m, bot_user_id)]
    if not dashboards:
        return 0
    keep = str(live_id) if live_id else str(max(dashboards, key=lambda m: int(m.id)).id)
    removed = 0
    for m in dashboards:
        if str(m.id) == keep:
            continue
        try:
            await m.unpin()
            removed += 1
        except discord.NotFound:
            continue
        except Exception:
            log.warning("Could not unpin stale dashboard %s", m.id, exc_info=True)
            continue
        if pause_every and removed % pause_every == 0:
            await asyncio.sleep(pause_seconds)
    return removed


async def start_periodic_refresh(
    refresher: DashboardRefresher,
    interval_seconds: int = PERIODIC_SECONDS,
) -> None:
    """Sweep every room through the refresher, to keep usage and times current."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            refresher.request(sweep=True)
        except Exception:
            log.warning("Periodic dashboard refresh failed", exc_info=True)
