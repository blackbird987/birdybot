#!/usr/bin/env python3
"""Regression test: repo and personal forums share one implementation.

Three pairs in ``ForumManager`` were near-copies, one for a repo forum and one
for a user's personal forum: auto-follow, the archive thread, and the Control
Room refresh. Each pair now calls one helper (``_add_followers``,
``_existing_archive_thread`` / ``_create_archive_thread``,
``_refresh_control_message``). Pinned for both sides of every pair:

  * auto-follow adds the right members once each and skips a bad id;
  * a stored archive thread is woken and reused; a deleted one has its id
    cleared, and a new post is made, recorded and followed;
  * a refresh redraws the embed and migrates a legacy thread name; a deleted
    thread clears the ids without recreating; a deleted message clears them,
    deletes the thread and recreates the post; any other error is swallowed.

Run:  python scripts/test_forum_twins.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord  # noqa: E402

from bot.discord import channels, forums  # noqa: E402
from bot.discord.access import AccessConfig, UserAccess  # noqa: E402
from bot.discord.forums import ForumManager, ForumProject  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


def _not_found() -> discord.NotFound:
    return discord.NotFound(MagicMock(status=404, reason="Not Found"), "gone")


class _Thread:
    """Stands in for ``discord.Thread``, which the arms swap for this class."""

    def __init__(self, tid: int, name: str = "x", archived: bool = False) -> None:
        self.id = tid
        self.name = name
        self.archived = archived
        self.added: list[int] = []
        self.edits: list[dict] = []
        self.deleted = False
        self.message = MagicMock()
        self.message_error: Exception | None = None

        async def edit_msg(**kw):  # noqa: ANN003
            self.message.edited = kw
        self.message.edit = edit_msg

    async def add_user(self, member) -> None:  # noqa: ANN001
        self.added.append(member.id)

    async def edit(self, **kw) -> None:  # noqa: ANN003
        self.edits.append(kw)
        for k, v in kw.items():
            setattr(self, k, v)

    async def delete(self) -> None:
        self.deleted = True

    async def fetch_message(self, _mid):  # noqa: ANN001, ANN202
        if self.message_error:
            raise self.message_error
        return self.message


class _Client:
    def __init__(self) -> None:
        self.channels: dict[int, object] = {}
        self.members = {1: SimpleNamespace(id=1), 2: SimpleNamespace(id=2),
                        3: SimpleNamespace(id=3)}
        self.guild = SimpleNamespace(get_member=lambda i: self.members.get(i))

    def get_guild(self, _gid):  # noqa: ANN001, ANN202
        return self.guild

    async def fetch_channel(self, cid):  # noqa: ANN001, ANN202
        ch = self.channels.get(cid)
        if ch is None:
            raise _not_found()
        if isinstance(ch, Exception):
            raise ch
        return ch

    def get_channel(self, cid):  # noqa: ANN001, ANN202
        return self.channels.get(cid)


class _Store:
    def is_repo_dormant(self, _n) -> bool:  # noqa: ANN001
        return False


def _manager() -> tuple[ForumManager, _Client]:
    client = _Client()
    fm = ForumManager(client, _Store(), guild_id=1, category_id=2)
    fm._discord_user_id = 1
    fm.saves = 0

    def save() -> None:
        fm.saves += 1
    fm.save_forum_map = save
    return fm, client


class _Patches:
    """Swap module attributes for the duration of one arm."""

    def __init__(self, cfg: AccessConfig) -> None:
        self.cfg = cfg
        self.cfg_saves = 0
        self.created: list[str] = []
        self.saved: list[tuple] = []

    def __enter__(self):  # noqa: ANN204
        self.saved = [
            (forums, "load_access_config", forums.load_access_config),
            (forums.access_mod, "save_access_config", forums.access_mod.save_access_config),
            (channels, "create_archive_post", channels.create_archive_post),
            (forums.discord, "Thread", forums.discord.Thread),
            (forums.discord, "ForumChannel", forums.discord.ForumChannel),
        ]
        forums.load_access_config = lambda: self.cfg

        def save_cfg(_cfg) -> None:  # noqa: ANN001
            self.cfg_saves += 1
        forums.access_mod.save_access_config = save_cfg

        async def create(forum, name):  # noqa: ANN001, ANN202
            self.created.append(name)
            return _Thread(900 + len(self.created)), None
        channels.create_archive_post = create
        forums.discord.Thread = _Thread
        forums.discord.ForumChannel = SimpleNamespace
        return self

    def __exit__(self, *_a) -> None:  # noqa: ANN002
        for mod, name, val in self.saved:
            setattr(mod, name, val)


def _cfg() -> AccessConfig:
    return AccessConfig(users={
        "2": UserAccess(user_id="2", display_name="Two", repos={"r": "build"},
                        forum_channel_id="50"),
        "3": UserAccess(user_id="3", display_name="Three", repos={"other": "build"}),
        "x": UserAccess(user_id="x", display_name="Bad", global_access=True),
    })


async def _arm_follow() -> None:
    print("auto-follow")
    fm, _client = _manager()
    with _Patches(_cfg()):
        t = _Thread(10)
        await fm._auto_follow_thread(t, "r")
        check(t.added == [1, 2], f"repo: owner and the granted user, bad id skipped ({t.added})")
        t = _Thread(11)
        await fm._auto_follow_user_thread(t, "1")
        check(t.added == [1], f"personal: the owner once when they are the user ({t.added})")
        t = _Thread(12)
        await fm._auto_follow_user_thread(t, "3")
        check(sorted(t.added) == [1, 3], f"personal: the user and the owner ({t.added})")


async def _arm_archive() -> None:
    print("archive thread")
    fm, client = _manager()
    client.channels[40] = SimpleNamespace()  # the repo forum
    client.channels[50] = SimpleNamespace()  # the user's forum
    fm._forum_projects = {"r": ForumProject(repo_name="r", forum_channel_id="40",
                                            archive_thread_id="41")}
    cfg = _cfg()
    cfg.users["2"].archive_thread_id = "51"
    with _Patches(cfg) as p:
        sleeping = _Thread(41, archived=True)
        client.channels[41] = sleeping
        got = await fm.ensure_archive_thread("r")
        check(got is sleeping and sleeping.archived is False and p.created == [],
              "repo: a stored thread is woken and reused")

        del client.channels[41]
        fm._archive_channel_ids = {41}
        got = await fm.ensure_archive_thread("r")
        proj = fm._forum_projects["r"]
        check(got is not None and proj.archive_thread_id == str(got.id) and p.created == ["r"],
              f"repo: a deleted thread is replaced ({proj.archive_thread_id}, {p.created})")
        check(fm._archive_channel_ids == {got.id} and fm.saves == 1 and got.added == [1, 2],
              "repo: the new post is registered, saved and followed")

        client.channels[51] = _Thread(51)
        got = await fm.ensure_user_archive_thread("2")
        check(got is client.channels[51] and p.cfg_saves == 0, "personal: a stored thread is reused")
        del client.channels[51]
        fm._archive_channel_ids = {51}
        got = await fm.ensure_user_archive_thread("2")
        ua = cfg.users["2"]
        check(got is not None and ua.archive_thread_id == str(got.id) and p.created[-1] == "Two",
              f"personal: a deleted thread is replaced ({ua.archive_thread_id})")
        check(fm._archive_channel_ids == {got.id} and p.cfg_saves == 2
              and sorted(got.added) == [1, 2],
              f"personal: cleared and saved, then recorded and followed (saves={p.cfg_saves})")


async def _arm_refresh() -> None:
    print("control room refresh")
    for side in ("repo", "personal"):
        fm, client = _manager()
        cfg = _cfg()
        cfg.users["2"].control_thread_id = "61"
        cfg.users["2"].control_message_id = "62"
        fm._forum_projects = {"r": ForumProject(repo_name="r", forum_channel_id="40",
                                                control_thread_id="61",
                                                control_message_id="62")}
        fm._user_control_thread_ids = {"61"}
        recreated: list[str] = []

        async def ensure_repo(name):  # noqa: ANN001
            recreated.append(name)

        async def ensure_user(uid, forum=None):  # noqa: ANN001
            recreated.append(uid)

        async def render(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
            return "EMBED", "VIEW"

        fm.ensure_control_post = ensure_repo
        fm.ensure_user_control_post = ensure_user
        fm._render_repo_control = render

        def ids():  # noqa: ANN202
            if side == "repo":
                p = fm._forum_projects["r"]
                return p.control_thread_id, p.control_message_id
            return cfg.users["2"].control_thread_id, cfg.users["2"].control_message_id

        def reset() -> None:
            p = fm._forum_projects["r"]
            p.control_thread_id, p.control_message_id = "61", "62"
            cfg.users["2"].control_thread_id = "61"
            cfg.users["2"].control_message_id = "62"
            recreated.clear()

        async def refresh() -> None:
            if side == "repo":
                await fm.refresh_control_room("r", usage_bar="")
            else:
                await fm.refresh_user_control_room("2")

        with _Patches(cfg):
            orig = (channels.build_user_control_embed, channels.build_user_control_view)
            channels.build_user_control_embed = lambda *_a: "EMBED"
            channels.build_user_control_view = lambda *_a: "VIEW"
            try:
                thread = _Thread(61, name="Control Center")
                client.channels[61] = thread
                await refresh()
                check(thread.message.edited == {"embed": "EMBED", "view": "VIEW"},
                      f"{side}: the embed is redrawn")
                check(thread.name == channels.CONTROL_ROOM_NAME,
                      f"{side}: a legacy name is migrated")

                await refresh()
                check(len(thread.edits) == 1, f"{side}: the current name is not re-sent")

                del client.channels[61]
                await refresh()
                check(ids() == (None, None) and recreated == [],
                      f"{side}: a deleted thread clears the ids and does not recreate")
                check(side == "repo" or "61" not in fm._user_control_thread_ids,
                      f"{side}: the personal control id is forgotten")

                reset()
                fm._user_control_thread_ids = {"61"}
                thread = _Thread(61)
                thread.message_error = _not_found()
                client.channels[61] = thread
                await refresh()
                check(ids() == (None, None) and thread.deleted
                      and recreated == ["r" if side == "repo" else "2"],
                      f"{side}: a deleted message clears, deletes the thread, recreates")

                reset()
                thread = _Thread(61)
                thread.message_error = RuntimeError("boom")
                client.channels[61] = thread
                await refresh()
                check(ids() == ("61", "62") and not thread.deleted,
                      f"{side}: another error is swallowed and changes nothing")
            finally:
                channels.build_user_control_embed, channels.build_user_control_view = orig


async def main() -> int:
    await _arm_follow()
    await _arm_archive()
    await _arm_refresh()
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
