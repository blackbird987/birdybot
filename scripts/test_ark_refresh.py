#!/usr/bin/env python3
"""Regression test: The Ark and Control Room refresh stay bounded.

Every trigger used to redraw The Ark and fire one unbounded edit per Control
Room, each carrying the usage bar, so every room differed every time. Any
HTTP error on the dashboard edit counted as "message gone" and posted and
pinned a new one: 23 pins, and tens of thousands of 429 lines in the log.
Pinned here (docs/design/ark-refresh.md):

  * coalescing: 50 requests make one pass; requests during a pass make
    exactly one follow-up, and passes are at least the floor apart;
  * only rooms whose signature moved are redrawn, a sweep renders all of
    them, hidden repos are never redrawn, and a failed room is retried;
  * an unchanged render sends nothing, and a failed edit is retried;
  * at most CONTROL_REFRESH_CONCURRENCY rooms redraw at once;
  * the dashboard is recreated only on NotFound: a 429 or a 500 posts
    nothing, keeps the stored id and logs a warning;
  * startup pin cleanup unpins stale dashboards only, keeps the live one
    (the newest when none is stored), and never touches other pins;
  * the Control Room no longer carries the usage bar.

Run:  python scripts/test_ark_refresh.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord  # noqa: E402

from bot.claude.types import InstanceStatus  # noqa: E402
from bot.discord import channels, dashboard, forums  # noqa: E402
from bot.discord.forums import ForumManager, ForumProject  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


def _http(cls, status: int):  # noqa: ANN001, ANN202
    return cls(MagicMock(status=status, reason="x"), {"message": "x", "code": 0})


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Inst(SimpleNamespace):
    pass


class _Store:
    def __init__(self) -> None:
        self.instances: list[_Inst] = []
        self.dormant: set[str] = set()
        self.state: dict = {}
        self.persisted = 0

    def list_by_repo(self, name):  # noqa: ANN001, ANN202
        return [i for i in self.instances if i.repo_name == name]

    def needs_attention(self):  # noqa: ANN202
        return [i for i in self.instances if i.needs_input]

    def get_repo_daily_cost(self, _n) -> float:  # noqa: ANN001
        return 0.0

    def get_deploy_state(self, _n):  # noqa: ANN001, ANN202
        return None

    def is_repo_dormant(self, name) -> bool:  # noqa: ANN001
        return name in self.dormant

    def get_platform_state(self, _p):  # noqa: ANN001, ANN202
        return dict(self.state)

    def set_platform_state(self, _p, state, persist=False) -> None:  # noqa: ANN001
        self.state = dict(state)
        self.persisted += 1

    def running_count(self) -> int:
        return 0


class _Forums:
    """Counts redraws per room and the peak number in flight at once."""

    def __init__(self, names: list[str]) -> None:
        self.forum_projects = {
            n: ForumProject(repo_name=n, forum_channel_id="1",
                            control_thread_id=str(100 + i),
                            control_message_id=str(200 + i))
            for i, n in enumerate(names)
        }
        self.calls: list[str] = []
        self.fail: set[str] = set()

    async def refresh_control_room(self, name: str) -> bool:
        self.calls.append(name)
        await asyncio.sleep(0)
        return name not in self.fail

    async def refresh_user_control_room(self, _uid: str) -> bool:
        return True


def _refresher(store, forums_, *, debounce=0.0, min_interval=0.0):  # noqa: ANN001, ANN202
    r = dashboard.DashboardRefresher(
        client=MagicMock(), store=store, forums=forums_,
        lobby_channel_id=lambda: None,
        debounce=debounce, min_interval=min_interval,
    )
    return r


async def _idle(r) -> None:  # noqa: ANN001
    while r._task is not None and not r._task.done():
        await asyncio.sleep(0.005)


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------

async def _arm_coalesce() -> None:
    print("coalescing")
    store = _Store()
    r = _refresher(store, _Forums([]), debounce=0.02, min_interval=0.15)
    passes_at: list[float] = []
    loop = asyncio.get_running_loop()
    orig = r._run_pass

    async def timed(*, sweep: bool) -> None:
        passes_at.append(loop.time())
        await asyncio.sleep(0.03)  # a pass takes a while
        if len(passes_at) == 1:
            for _ in range(20):
                r.request()  # arrive during the pass
        await orig(sweep=sweep)
    r._run_pass = timed

    for _ in range(50):
        r.request()
    await asyncio.sleep(0.01)
    check(len(passes_at) == 0, "nothing runs before the debounce")
    await _idle(r)
    check(len(passes_at) == 2,
          f"50 requests, then 20 during the pass: one pass and one follow-up ({len(passes_at)})")
    gap = passes_at[1] - passes_at[0] if len(passes_at) == 2 else 0
    check(gap >= 0.15 - 0.005, f"the follow-up waits out the floor ({gap:.3f}s)")


async def _arm_rooms() -> None:
    print("which rooms redraw")
    store = _Store()
    fm = _Forums(["a", "b", "c", "hidden"])
    store.dormant = {"hidden"}
    r = _refresher(store, fm)

    await r._redraw_rooms(sweep=False)
    check(sorted(fm.calls) == ["a", "b", "c"],
          f"the first pass draws every visible room ({sorted(fm.calls)})")
    check("hidden" not in fm.calls, "a hidden repo is never redrawn")

    fm.calls.clear()
    await r._redraw_rooms(sweep=False)
    check(fm.calls == [], f"nothing changed: no room is redrawn ({fm.calls})")

    store.instances.append(_Inst(id="q-1", repo_name="b", status=InstanceStatus.RUNNING,
                                 needs_input=False))
    await r._redraw_rooms(sweep=False)
    check(fm.calls == ["b"], f"an instance starting in b redraws b only ({fm.calls})")

    fm.calls.clear()
    store.instances.append(_Inst(id="q-2", repo_name="hidden",
                                 status=InstanceStatus.RUNNING, needs_input=False))
    await r._redraw_rooms(sweep=False)
    check(fm.calls == [], f"activity in a hidden repo redraws nothing ({fm.calls})")

    fm.calls.clear()
    await r._redraw_rooms(sweep=True)
    check(sorted(fm.calls) == ["a", "b", "c"],
          f"a sweep renders every visible room ({sorted(fm.calls)})")

    fm.calls.clear()
    fm.fail = {"c"}
    store.instances.append(_Inst(id="q-3", repo_name="c", status=InstanceStatus.RUNNING,
                                 needs_input=False))
    await r._redraw_rooms(sweep=False)
    fm.fail = set()
    fm.calls.clear()
    await r._redraw_rooms(sweep=False)
    check(fm.calls == ["c"], f"a room whose redraw failed is retried next pass ({fm.calls})")


class _Msg:
    def __init__(self, record: list, error: Exception | None = None) -> None:
        self.record = record
        self.error = error

    async def edit(self, **kw) -> None:  # noqa: ANN003
        if self.error:
            raise self.error
        self.record.append(kw)


class _CtlThread:
    """Stands in for ``discord.Thread`` while the arm swaps the class."""

    def __init__(self, tid: int, record: list, errors: dict) -> None:
        self.id = tid
        self.name = channels.CONTROL_ROOM_NAME
        self.record = record
        self.errors = errors

    def get_partial_message(self, mid):  # noqa: ANN001, ANN202
        return _Msg(self.record, self.errors.get(mid))


async def _arm_control_messages() -> None:
    print("control room edits")
    record: list = []
    errors: dict = {}
    in_flight = [0, 0]  # current, peak

    client = SimpleNamespace(channels={})
    client.get_channel = lambda cid: client.channels.get(cid)

    async def fetch_channel(cid):  # noqa: ANN001, ANN202
        raise AssertionError("the slow path should not be taken")
    client.fetch_channel = fetch_channel

    fm = ForumManager(client, _Store(), guild_id=1, category_id=2)
    for i in range(6):
        client.channels[100 + i] = _CtlThread(100 + i, record, errors)

    version = {"n": 0}

    async def render():  # noqa: ANN202
        in_flight[0] += 1
        in_flight[1] = max(in_flight[1], in_flight[0])
        await asyncio.sleep(0.01)
        in_flight[0] -= 1
        return discord.Embed(title=f"room v{version['n']}"), None

    async def noop() -> None:
        pass

    async def one(i: int) -> bool:
        return await fm._refresh_control_message(
            f"r{i}", str(100 + i), str(200 + i),
            forget=lambda: None, render=render, recreate=noop,
        )

    saved = forums.discord.Thread
    forums.discord.Thread = _CtlThread
    try:
        ok = await asyncio.gather(*(one(i) for i in range(6)))
        check(all(ok) and len(record) == 6, f"six changed rooms are edited ({len(record)})")
        check(in_flight[1] <= forums.CONTROL_REFRESH_CONCURRENCY,
              f"at most {forums.CONTROL_REFRESH_CONCURRENCY} at once (peak {in_flight[1]})")

        record.clear()
        ok = await asyncio.gather(*(one(i) for i in range(6)))
        check(all(ok) and record == [], "an unchanged render sends nothing")

        version["n"] += 1
        errors[200] = _http(discord.HTTPException, 429)
        ok0 = await one(0)
        check(ok0 is False and record == [], "a 429 on the edit reports failure")
        del errors[200]
        ok0 = await one(0)
        check(ok0 is True and len(record) == 1, "the next refresh retries the failed edit")
    finally:
        forums.discord.Thread = saved

    embed = channels.build_control_embed("r", "/x", "master", today_cost=1.5)
    names = [f.name for f in embed.fields]
    check("Usage" not in names and "Today" in names,
          f"the Control Room carries no usage bar, keeps Today ({names})")


class _Lobby:
    def __init__(self, edit_error: Exception | None = None) -> None:
        self.edit_error = edit_error
        self.edits = 0
        self.sent: list = []
        self.pinned: list = []

    def get_partial_message(self, mid):  # noqa: ANN001, ANN202
        lobby = self

        class _P:
            async def edit(self, **_kw) -> None:  # noqa: ANN003
                if lobby.edit_error:
                    raise lobby.edit_error
                lobby.edits += 1
        return _P()

    async def send(self, **_kw):  # noqa: ANN003, ANN202
        msg = SimpleNamespace(id=900 + len(self.sent))

        async def pin() -> None:
            self.pinned.append(msg.id)
        msg.pin = pin
        self.sent.append(msg)
        return msg


async def _arm_dashboard_recreate() -> None:
    print("dashboard edit or recreate")
    handler = _Records()
    dashboard.log.addHandler(handler)
    try:
        for status, cls in ((429, discord.HTTPException), (500, discord.DiscordServerError),
                            (403, discord.Forbidden)):
            store = _Store()
            store.state = {"dashboard_message_id": "555"}
            lobby = _Lobby(_http(cls, status))
            handler.records.clear()
            ok = await dashboard.edit_or_recreate_dashboard(store, lobby, "555", None, None)
            warned = any(r.levelno == logging.WARNING for r in handler.records)
            check(not ok and lobby.sent == [] and store.state["dashboard_message_id"] == "555"
                  and warned,
                  f"HTTP {status}: nothing posted, id kept, a warning logged")

        store = _Store()
        store.state = {"dashboard_message_id": "555"}
        lobby = _Lobby(_http(discord.NotFound, 404))
        ok = await dashboard.edit_or_recreate_dashboard(store, lobby, "555", None, None)
        check(ok and len(lobby.sent) == 1 and lobby.pinned == [900]
              and store.state["dashboard_message_id"] == "900",
              "NotFound: one new dashboard posted, pinned and recorded")

        store = _Store()
        lobby = _Lobby()
        ok = await dashboard.edit_or_recreate_dashboard(store, lobby, "555", None, None)
        check(ok and lobby.edits == 1 and lobby.sent == [], "a live dashboard is edited in place")
    finally:
        dashboard.log.removeHandler(handler)


async def _arm_ark_skip() -> None:
    print("the ark skips an unchanged render")
    store = _Store()
    store.state = {"dashboard_message_id": "555"}
    lobby = _Lobby()
    client = SimpleNamespace(get_channel=lambda _cid: lobby)
    r = dashboard.DashboardRefresher(client, store, _Forums([]), lambda: 1)
    version = {"n": 0}

    async def no_bar() -> str:
        return "bar"
    import bot.engine.usage as usage
    saved = (dashboard.discord.TextChannel, dashboard.build_dashboard_embed,
             dashboard._count_orphans, usage.get_usage_bar_async)
    dashboard.discord.TextChannel = _Lobby
    dashboard.build_dashboard_embed = lambda *_a, **_k: discord.Embed(title=f"v{version['n']}")
    dashboard._count_orphans = lambda _s: 0
    usage.get_usage_bar_async = no_bar
    try:
        await r._redraw_ark()
        await r._redraw_ark()
        check(lobby.edits == 1, f"the same render twice edits once ({lobby.edits})")
        version["n"] += 1
        lobby.edit_error = _http(discord.HTTPException, 429)
        await r._redraw_ark()
        lobby.edit_error = None
        await r._redraw_ark()
        check(lobby.edits == 2 and lobby.sent == [],
              f"a failed edit is retried with the same render, nothing posted ({lobby.edits})")
    finally:
        (dashboard.discord.TextChannel, dashboard.build_dashboard_embed,
         dashboard._count_orphans, usage.get_usage_bar_async) = saved


def _pin(mid: int, author: int, title: str | None = None, ark_button: bool = False):  # noqa: ANN202
    m = SimpleNamespace(id=mid, author=SimpleNamespace(id=author),
                        embeds=[SimpleNamespace(title=title)] if title else [],
                        components=[], unpinned=False)
    if ark_button:
        m.components = [SimpleNamespace(children=[SimpleNamespace(custom_id="ark:refresh")])]

    async def unpin() -> None:
        m.unpinned = True
    m.unpin = unpin
    return m


async def _arm_pin_cleanup() -> None:
    print("startup pin cleanup")
    bot_id = 7

    def fresh():  # noqa: ANN202
        return [
            _pin(10, bot_id, "The Ark"),
            _pin(11, bot_id, "The Ark"),
            _pin(12, bot_id, None, ark_button=True),
            _pin(13, bot_id, "The Ark"),
            _pin(14, bot_id, "Something else"),  # the bot's, but not a dashboard
            _pin(15, 99, "The Ark"),             # someone else's
        ]

    pins = fresh()
    n = await dashboard.unpin_stale_dashboards(pins, "11", bot_id, pause_seconds=0)
    gone = sorted(m.id for m in pins if m.unpinned)
    check(n == 3 and gone == [10, 12, 13], f"stale dashboards unpinned, the live one kept ({gone})")

    pins = fresh()
    await dashboard.unpin_stale_dashboards(pins, None, bot_id, pause_seconds=0)
    gone = sorted(m.id for m in pins if m.unpinned)
    check(gone == [10, 11, 12], f"no stored id: the newest dashboard stays ({gone})")
    check(not any(m.unpinned for m in pins if m.id in (14, 15)),
          "a pin that is not this bot's dashboard is never touched")

    pins = [_pin(i, bot_id, "The Ark") for i in range(1, 24)]
    n = await dashboard.unpin_stale_dashboards(pins, "23", bot_id, pause_seconds=0)
    check(n == 22, f"23 pins become 1 ({n} unpinned)")


async def main() -> int:
    await _arm_coalesce()
    await _arm_rooms()
    await _arm_control_messages()
    await _arm_dashboard_recreate()
    await _arm_ark_skip()
    await _arm_pin_cleanup()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
