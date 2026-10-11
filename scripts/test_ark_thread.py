#!/usr/bin/env python3
"""Harness for channels.get_or_create_ark_thread.

The prompt review and log triage each kept a forty-line copy of "find the
thread I stored in The Ark, or make one". This pins the contract the shared
helper took over: a gone thread is replaced, a transient failure is not (or
every Discord blip would leave another orphan thread), an archived one is
woken, and the new id is handed back to the caller to persist.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from bot.discord.channels import get_or_create_ark_thread  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        FAILS.append(label)


def _http_error(cls, status: int):
    return cls(SimpleNamespace(status=status, reason="x"), "x")


class FakeThread(discord.Thread):
    def __init__(self, tid: int, archived: bool = False):  # noqa: D401 - no super
        self.id = tid
        self.archived = archived
        self.edits: list[dict] = []

    async def edit(self, **kw):
        self.edits.append(kw)
        self.archived = kw.get("archived", self.archived)


class FakeLobby(discord.TextChannel):
    def __init__(self):
        self.created: list[dict] = []

    async def create_thread(self, **kw):
        self.created.append(kw)
        return FakeThread(999)


class FakeBot:
    def __init__(self, channels=None, fetch=None):
        self._lobby_channel_id = "1"
        self.lobby = FakeLobby()
        self.channels = {1: self.lobby, **(channels or {})}
        self.fetch = fetch

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        if isinstance(self.fetch, Exception):
            raise self.fetch
        return self.fetch


def run(bot, stored):
    saved: list[int] = []
    out = asyncio.run(get_or_create_ark_thread(
        bot, name="T", stored_id=stored,
        on_created=lambda t: saved.append(t.id), label="test"))
    return out, saved


def main() -> int:
    bot = FakeBot({5: FakeThread(5)})
    out, saved = run(bot, "5")
    check("a cached thread is returned as-is", out.id == 5 and not saved
          and not bot.lobby.created)

    bot = FakeBot(fetch=FakeThread(6, archived=True))
    out, saved = run(bot, "6")
    check("a fetched archived thread is woken, not replaced",
          out.id == 6 and out.edits == [{"archived": False}] and not saved)

    bot = FakeBot(fetch=_http_error(discord.NotFound, 404))
    out, saved = run(bot, "7")
    check("a thread Discord says is gone is replaced and persisted",
          out.id == 999 and saved == [999] and len(bot.lobby.created) == 1)
    kw = bot.lobby.created[0]
    check("created as a 7-day public thread",
          kw["auto_archive_duration"] == 10080
          and kw["type"] == discord.ChannelType.public_thread and kw["name"] == "T")

    bot = FakeBot(fetch=_http_error(discord.HTTPException, 503))
    out, saved = run(bot, "8")
    check("a transient failure returns None and creates nothing",
          out is None and not saved and not bot.lobby.created)

    bot = FakeBot()
    out, saved = run(bot, None)
    check("no stored id creates one", out.id == 999 and saved == [999])

    bot = FakeBot()
    bot._lobby_channel_id = None
    check("no lobby yet returns None", run(bot, None) == (None, []))

    bot = FakeBot()
    bot.channels[1] = object()
    check("a lobby that is not a text channel returns None", run(bot, None) == (None, []))

    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
