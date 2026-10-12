#!/usr/bin/env python3
"""Regression test: the repo pickers fit in one Discord message at any size.

``/new`` with no repo built one button per repo into a single View, and
``/repo`` built one Select with an option per repo. Discord caps a View at 25
buttons (5 rows of 5) and a Select at 25 options, and it rejects the whole
message rather than the overflow, so the 26th registered repo would have made
both commands fail outright. 22 repos were registered when this was found.

Asserted here, by driving the real slash commands on a real command tree:

  * up to 25 repos ``/new`` still offers buttons, one per repo;
  * past 25 it switches to Select menus, none over 25 options, no more than
    5 per message, and says how many it had to leave out past 125;
  * ``/repo`` splits the same way and keeps marking the active repo;
  * a pick from any of the split menus reaches the same handler a button or
    the old single menu did.

Run: python scripts/test_repo_picker_cap.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord  # noqa: E402
from discord import app_commands  # noqa: E402

from bot.discord import interactions, slash_commands  # noqa: E402

_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}" + (f"  ({detail})" if detail else ""))
        _failures.append(name)


class _Store:
    def __init__(self, n: int):
        self.repos = {f"repo{i:03d}": f"/src/repo{i:03d}" for i in range(n)}

    def list_repos(self):
        return dict(self.repos)

    def list_active_repos(self):
        return dict(self.repos)

    def list_dormant_repos(self):
        return {}

    def is_repo_dormant(self, name):
        return False

    def get_active_repo(self):
        name = next(iter(self.repos))
        return name, self.repos[name]


class _Bot:
    def __init__(self, n: int):
        self._store = _Store(n)
        self._guild_id = 1
        self.tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))

    def _is_owner(self, _uid):
        return True

    async def _run_slash(self, *_a, **_kw):  # pragma: no cover
        raise AssertionError("the picker path must not fall through to the engine")


class _Response:
    def __init__(self):
        self.sent: list[tuple[str, discord.ui.View | None]] = []

    async def send_message(self, content, *, view=None, **_kw):
        self.sent.append((content, view))

    async def defer(self, **_kw):
        pass


class _User:
    id = 1
    display_name = "owner"


class _Interaction:
    def __init__(self, data=None):
        self.response = _Response()
        self.user = _User()
        self.channel_id = 5
        self.channel = None
        self.data = data or {}


def _drive(command: str, n: int, **kwargs) -> tuple[str, discord.ui.View | None]:
    bot = _Bot(n)
    slash_commands.setup(bot)
    cmd = bot.tree.get_command(command, guild=discord.Object(id=1))
    inter = _Interaction()
    try:
        asyncio.run(cmd.callback(inter, **kwargs))
    except Exception as exc:  # the old code raised here: Discord's own limit check
        return f"raised {exc!r}", None
    return inter.response.sent[-1] if inter.response.sent else ("", None)


def _fits(view: discord.ui.View | None) -> tuple[bool, str]:
    """Does this view satisfy Discord's per-message component limits?"""
    if view is None:
        return False, "no view"
    items = view.children
    selects = [i for i in items if isinstance(i, discord.ui.Select)]
    buttons = [i for i in items if isinstance(i, discord.ui.Button)]
    rows = len(selects) + -(-len(buttons) // 5)
    over = [len(s.options) for s in selects if len(s.options) > 25]
    ids = [i.custom_id for i in items]
    ok = len(buttons) <= 25 and rows <= 5 and not over and len(ids) == len(set(ids))
    return ok, f"{len(buttons)} buttons, {len(selects)} selects, {rows} rows, oversized {over}"


def _picked(view: discord.ui.View | None) -> list[str]:
    out: list[str] = []
    for item in view.children if view else []:
        if isinstance(item, discord.ui.Select):
            out += [o.value for o in item.options]
        elif isinstance(item, discord.ui.Button):
            out.append(item.custom_id.split(":", 1)[1])
    return out


def check_new() -> None:
    _text, view = _drive("new", 25, repo="", mode="")
    ok, detail = _fits(view)
    _check("/new 25 repos: fits", ok, detail)
    _check("/new 25 repos: still buttons, one per repo",
           view is not None and all(isinstance(i, discord.ui.Button) for i in view.children)
           and len(view.children) == 25, detail)

    _text, view = _drive("new", 26, repo="", mode="")
    ok, detail = _fits(view)
    _check("/new 26 repos: fits", ok, detail)
    _check("/new 26 repos: every repo is pickable",
           sorted(_picked(view)) == sorted(_Store(26).repos), detail)

    text, view = _drive("new", 130, repo="", mode="")
    ok, detail = _fits(view)
    _check("/new 130 repos: fits", ok, detail)
    _check("/new 130 repos: shows 125 and says 5 were left out",
           len(_picked(view)) == 125 and "5 more not shown" in text, f"{detail} | {text!r}")


def check_repo_switch() -> None:
    _text, view = _drive("repo", 26, args="")
    ok, detail = _fits(view)
    _check("/repo 26 repos: fits", ok, detail)
    _check("/repo 26 repos: every repo is switchable",
           sorted(_picked(view)) == sorted(_Store(26).repos), detail)
    defaults = [o.value for s in (view.children if view else []) for o in s.options if o.default]
    _check("/repo: the active repo is still marked", defaults == ["repo000"], str(defaults))

    text, view = _drive("repo", 130, args="")
    ok, detail = _fits(view)
    _check("/repo 130 repos: fits", ok, detail)
    _check("/repo 130 repos: says some are not in the menu",
           "5 not in the menu" in text and len(text) <= 2000, text[-120:])


def check_routing() -> None:
    seen: list[tuple[str, str]] = []

    async def _new(bot, interaction, repo_name, btn_access):
        seen.append(("new", repo_name))

    async def _switch(bot, interaction, btn_access):
        seen.append(("switch", interaction.data["values"][0]))

    class _Access:
        allowed = True
        is_owner = True

    class _RouteBot:
        def _check_access(self, *_a, **_kw):
            return _Access()

    saved = interactions._handle_new_repo, interactions._handle_repo_switch
    interactions._handle_new_repo, interactions._handle_repo_switch = _new, _switch
    try:
        for data in (
            {"custom_id": "new_repo:alpha"},
            {"custom_id": "new_repo_select:3", "values": ["beta"]},
            {"custom_id": "repo_switch_select:1", "values": ["gamma"]},
            {"custom_id": "repo_switch_select", "values": ["delta"]},
        ):
            asyncio.run(interactions.handle(_RouteBot(), _Interaction(data)))
    finally:
        interactions._handle_new_repo, interactions._handle_repo_switch = saved
    _check("routing: a button, a split /new menu and both /repo menu ids all land",
           seen == [("new", "alpha"), ("new", "beta"), ("switch", "gamma"), ("switch", "delta")],
           str(seen))


def main() -> int:
    check_new()
    check_repo_switch()
    check_routing()
    print()
    if _failures:
        print(f"FAIL: {len(_failures)} repo-picker check(s) failed")
        return 1
    print("PASS: both repo pickers fit in one message at any repo count.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
