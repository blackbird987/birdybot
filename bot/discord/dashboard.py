"""Dashboard embed generation and refresh for Discord lobby.

Builds the pinned dashboard embed (The Ark): the cross-repo "Needs you" list
from ``bot/engine/waiting.py``, a status line and the usage bar, and owns
every redraw of it and of the Control Rooms (``DashboardRefresher``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from typing import TYPE_CHECKING, Callable

import discord

from bot import config
from bot.claude.auth_health import split_accounts
from bot.claude.types import InstanceStatus
from bot.discord.access import load_access_config

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
    Buttons stay interactive because ``timeout=None``. The custom ids are
    what that dispatch matches on, so they never change.
    """

    def __init__(self, running_count: int = 0):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Refresh",
            style=discord.ButtonStyle.secondary,
            custom_id="ark:refresh",
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
            label="New Repo",
            style=discord.ButtonStyle.green,
            custom_id="ark:new_repo",
            row=1,
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
    waiting_items: list | None = None,
    usage_text: str | None = None,
    start_time: float = 0.0,
) -> discord.Embed:
    """The Ark: what is waiting on the owner, then status and usage.

    ``waiting_items`` comes from ``bot.engine.waiting.collect``, computed once
    per refresher pass (docs/design/needs-you.md). Every time in the text is
    Discord timestamp markup, so an unchanged state renders the same and the
    refresher's skip holds. Footers do not render that markup, so the start
    time sits in the status line.

    No Discord API calls and no state mutation. Not quite pure: the account
    label stats each account's credentials file (cached on mtime+size in
    ``auth_health``) and reads the store's sideline table, which together are
    what let a signed-out backup show as ``1/2 accts`` here instead of only
    surfacing at failover time.
    """
    from bot.engine import waiting

    items = list(waiting_items or [])
    n = len(items)
    embed = discord.Embed(
        title="The Ark",
        description=(f"**Needs you ({n})**\n" if n else "") + waiting.render_ark(items),
        color=discord.Color.blurple(),
    )

    # --- Status: running, accounts, up since ---
    total_accts = len(config.CLAUDE_ACCOUNTS)
    usable_accts = total_accts
    if total_accts > 1:
        healthy, _ = split_accounts(
            config.CLAUDE_ACCOUNTS, store.sidelined_accounts(),
        )
        usable_accts = len(healthy)
    status = [f"Running {store.running_count()}"]
    if usable_accts < total_accts:
        status.append(f"\u26a0\ufe0f {total_accts - usable_accts} of {total_accts} accounts out")
    if start_time:
        status.append(f"Up since <t:{int(start_time)}:R>")
    embed.add_field(name="Status", value=" \u00b7 ".join(status), inline=False)

    # --- Usage ---
    usage_label = f"Usage · {config.PLAN_NAME}"
    if total_accts > 1:
        # Show usable/total when one is signed out, so a dead backup can't
        # masquerade as working failover the way klerk did for five weeks.
        if usable_accts < total_accts:
            usage_label += f" · {usable_accts}/{total_accts} accts"
        else:
            usage_label += f" · {total_accts} accts"
    if usage_text:
        embed.add_field(name=usage_label, value=usage_text, inline=False)
    else:
        embed.add_field(name=usage_label, value="Usage data unavailable", inline=False)

    embed.set_footer(text=f"v{_get_version()} \u00b7 {config.PC_NAME}")
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
                    waiting_sig: tuple) -> str:
    """What a repo's Control Room is drawn from, cheaply, without rendering.

    A pass redraws only the rooms whose signature moved since the last one
    that drew them. ``waiting_sig`` is the repo's "Needs you" items as keys
    and times (``waiting.signature``), never a rendered age, so the clock
    alone moves nothing. What this misses (a branch switch) the periodic
    sweep picks up, since it renders every room and lets the unchanged-render
    skip drop the ones that did not move.
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
    return repr((running, waiting_sig, completed, cost, deploy,
                 proj.control_message_id, len(proj.threads)))


class DashboardRefresher:
    """Coalesces every refresh request into bounded redraws.

    ``request()`` is cheap and safe to call from anywhere, as often as
    anything likes: the first one schedules a pass ``debounce`` seconds out,
    and any that arrive before or during that pass fold into it or into the
    one follow-up pass after it. Passes are at least ``min_interval`` apart.
    ``refresh_now()`` is for a person waiting on the result (the Refresh
    button) and for startup: it runs a pass at once, still one at a time.

    A pass reads the "Needs you" list once (off the loop), redraws The Ark
    when its render changed, then the Control Rooms whose signature changed,
    each handed its own slice of that list, skipping hidden repos. A sweep (startup, the
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
            items = await self._collect_waiting()
            try:
                await self._redraw_ark(items)
            except Exception:
                log.warning("Dashboard refresh failed", exc_info=True)
            try:
                await self._redraw_rooms(sweep=sweep, items=items)
            except Exception:
                log.warning("Control room refresh pass failed", exc_info=True)

    async def _collect_waiting(self) -> list | None:
        """The pass's one read of the "Needs you" list. None when it failed.

        The thread index is snapshotted here, on the loop that mutates the
        forum map; the reads (store, vault, git) then run in a worker thread.
        """
        from bot.engine import waiting
        try:
            idx = waiting.thread_index(self._forums.forum_projects)
            return await asyncio.to_thread(waiting.collect, self._store, idx)
        except Exception:
            log.warning("Needs-you list could not be read this pass", exc_info=True)
            return None

    # --- The Ark ---

    async def _redraw_ark(self, items: list | None = None) -> None:
        lobby_id = self._lobby_channel_id()
        if not lobby_id:
            return
        lobby = self._client.get_channel(lobby_id)
        if not lobby or not isinstance(lobby, discord.TextChannel):
            return

        if items is None:
            # A list that could not be read is not "nothing waiting": keep
            # what The Ark shows now rather than empty it.
            return
        from bot.engine.usage import get_usage_bar_async
        try:
            usage_text = await get_usage_bar_async()
        except Exception:
            log.warning("Usage bar fetch failed", exc_info=True)
            usage_text = None
        from bot.engine.commands import get_start_time
        embed = build_dashboard_embed(
            self._store, items, usage_text=usage_text, start_time=get_start_time(),
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

    async def _redraw_rooms(self, *, sweep: bool, items: list | None = None) -> None:
        from bot.engine import waiting
        if items is None:
            # Without the list a room would render with no "Needs you" field;
            # the next pass that can read it redraws them.
            return
        by_repo = waiting.by_repo(items)

        due: list[tuple[str, str]] = []
        for name, proj in list(self._forums.forum_projects.items()):
            if not proj.control_thread_id or not proj.control_message_id:
                continue
            # A hidden repo's forum is parked out of sight: nothing reads its
            # room, so nothing redraws it (docs/design/repo-hide.md).
            if self._store.is_repo_dormant(name):
                continue
            sig = _room_signature(self._store, name, proj,
                                  waiting.signature(by_repo.get(name, ())))
            if sweep or self._room_sigs.get(name) != sig:
                due.append((name, sig))

        async def one(name: str, sig: str) -> None:
            try:
                current = await self._forums.refresh_control_room(
                    name, waiting=by_repo.get(name, []))
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
