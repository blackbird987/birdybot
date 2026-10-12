#!/usr/bin/env python3
"""Harness for the slash commands' one authorization guard.

Forty-six commands each opened with a hand-copied three-line "Unauthorized"
check, in two shapes: owner or channel grant, and owner only. They go through
``_refused`` now. This drives real registered commands with a fake bot so the
two shapes cannot quietly swap, and pins that no command grows a copy again.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discord  # noqa: E402
from discord import app_commands  # noqa: E402

from bot.discord import slash_commands  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


class _Bot:
    def __init__(self, owner: bool, granted: bool):
        self._guild_id = 1
        self.tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
        self.owner, self.granted = owner, granted
        self.ran: list[object] = []
        self.access_channels: list[str | None] = []

    def _is_owner(self, _uid):
        return self.owner

    def _check_access(self, _uid, channel_id=None):
        self.access_channels.append(channel_id)
        return SimpleNamespace(allowed=self.granted)

    async def _run_slash(self, _interaction, handler):
        self.ran.append(handler)


class _Response:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, content, **_kw):
        self.sent.append(content)

    async def defer(self, **_kw):
        pass


class _Interaction:
    def __init__(self):
        self.response = _Response()
        self.user = SimpleNamespace(id=7, display_name="u")
        self.channel_id = 42
        self.channel = None
        self.data = {}


def _drive(name: str, *, owner: bool, granted: bool):
    bot = _Bot(owner, granted)
    slash_commands.setup(bot)
    cmd = bot.tree.get_command(name, guild=discord.Object(id=1))
    inter = _Interaction()
    asyncio.run(cmd.callback(inter))
    return bot, inter


def main() -> int:
    # /status and /cost: owner or a grant for this channel.
    for name in ("status", "cost"):
        bot, inter = _drive(name, owner=True, granted=False)
        check(f"/{name}: the owner runs it without an access lookup",
              bot.ran and not inter.response.sent and not bot.access_channels)
        bot, inter = _drive(name, owner=False, granted=True)
        check(f"/{name}: a guest with a grant runs it, checked against this channel",
              bot.ran and not inter.response.sent and bot.access_channels == ["42"])
        bot, inter = _drive(name, owner=False, granted=False)
        check(f"/{name}: anyone else is refused before anything runs",
              not bot.ran and inter.response.sent == ["Unauthorized"])

    # /auth is owner only: a grant must not open it.
    bot, inter = _drive("auth", owner=False, granted=True)
    check("/auth: a granted guest is still refused (owner only)",
          inter.response.sent == ["Unauthorized"] and not bot.access_channels)

    src = (ROOT / "bot" / "discord" / "slash_commands.py").read_text(encoding="utf-8")
    copies = re.findall(r'if not bot\._is_owner\(interaction\.user\.id\)( and not bot\._check_access\(interaction\.user\.id, channel_id=)?[^\n]*:\n\s+await interaction\.response\.send_message\("Unauthorized"', src)
    check("no command carries its own copy of the guard", not copies, f"{len(copies)} copies")
    check("the guard is in use", src.count("await _refused(interaction") >= 40)

    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
