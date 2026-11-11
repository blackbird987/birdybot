#!/usr/bin/env python3
"""A guest's personal control room can be turned off.

``control_room: false`` on a user entry in data/access.json means the bot
never makes a control room in that guest's forum, never redraws one, and
deletes the one they already have. Creation is idempotent on the stored
thread id, so deleting the post by hand would only bring it back; the flag is
what stops that.

Pinned here:
  * the flag round-trips, is written only when False, and an entry without it
    reads as True (every existing guest is unchanged);
  * flag off: no creation, no dashboard redraw, no refresh, the existing
    thread is deleted and its ids cleared, pins and auto-follow skip it;
  * a failed delete keeps the ids so the next startup retries, and the id
    stays in the skip set so nobody starts a session inside it;
  * something that is not a thread is never deleted;
  * flag absent: creation and redraws behave exactly as before.

Run:  python scripts/test_guest_control_room_off.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord  # noqa: E402

from bot.discord import access as access_mod  # noqa: E402
from bot.discord import channels, dashboard, forums  # noqa: E402
from bot.discord.access import AccessConfig, UserAccess  # noqa: E402
from bot.discord.forums import ForumManager  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


class _Store:
    def is_repo_dormant(self, name: str) -> bool:
        return False

    def list_instances(self):  # noqa: ANN201
        return []


def _thread(tid: int) -> MagicMock:
    th = MagicMock(spec=discord.Thread)
    th.id = tid
    th.delete = AsyncMock()
    return th


def _forum(fid: int) -> MagicMock:
    f = MagicMock(spec=discord.ForumChannel)
    f.id = fid
    f.name = "guest-forum"
    return f


class _Env:
    """One ForumManager over an in-memory access config."""

    def __init__(self, ua: UserAccess, fetched=None) -> None:  # noqa: ANN001
        self.cfg = AccessConfig(users={ua.user_id: ua})
        self.saves = 0
        self.created: list[str] = []
        self.forum = _forum(int(ua.forum_channel_id or 0))
        client = MagicMock()
        client.get_channel.side_effect = (
            lambda cid: self.forum if cid == self.forum.id else None)
        client.fetch_channel = AsyncMock(side_effect=fetched or (lambda cid: _thread(cid)))
        self.client = client
        self.fm = ForumManager(client, _Store(), guild_id=1, category_id=2)
        self.fm.save_forum_map = lambda: None
        self.fm._auto_follow_user_thread = AsyncMock()
        self.fm.ensure_user_archive_thread = AsyncMock()

        def save(cfg) -> None:  # noqa: ANN001
            self.saves += 1
        forums.load_access_config = lambda: self.cfg
        dashboard.load_access_config = lambda: self.cfg
        access_mod.save_access_config = save

        async def create(forum, name, repos):  # noqa: ANN001, ANN202
            self.created.append(name)
            th = _thread(999)
            msg = MagicMock()
            msg.id = 999
            return th, msg
        channels.create_user_control_post = create


def _ua(**kw) -> UserAccess:  # noqa: ANN003
    base = dict(user_id="42", display_name="Guest", forum_channel_id="500",
                archive_thread_id="600")
    base.update(kw)
    return UserAccess(**base)


def test_model() -> None:
    print("model: the flag round-trips and defaults to on")
    old = UserAccess.from_dict({"user_id": "1", "display_name": "x"})
    check(old.control_room is True, "entry without the field reads as on")
    check("control_room" not in old.to_dict(), "on is not written (files stay as they were)")
    off = UserAccess.from_dict({"user_id": "1", "control_room": False})
    check(off.control_room is False, "false reads as off")
    check(off.to_dict().get("control_room") is False, "off is written back")
    check(UserAccess.from_dict(off.to_dict()).control_room is False, "off survives a save")
    other = UserAccess.from_dict({"user_id": "1", "control_room": "false"})
    check(other.control_room is True, "only a real false turns it off")


async def test_off_no_creation() -> None:
    print("off: nothing is created")
    env = _Env(_ua(control_room=False))
    await env.fm.ensure_user_control_post("42", env.forum)
    await env.fm.ensure_user_control_post("42")
    check(env.created == [], "ensure_user_control_post makes nothing")
    await env.fm._ensure_user_posts()
    check(env.created == [], "the startup pass makes nothing")
    check(env.fm.user_control_thread_ids == set(), "nothing is tracked as a control room")


async def test_off_retires_existing() -> None:
    print("off: an existing control room is deleted and forgotten")
    th = _thread(700)
    env = _Env(_ua(control_room=False, control_thread_id="700", control_message_id="700"),
               fetched=lambda cid: th)
    await env.fm._ensure_user_posts()
    ua = env.cfg.users["42"]
    check(th.delete.await_count == 1, "the thread is deleted")
    check(ua.control_thread_id is None and ua.control_message_id is None, "its ids are cleared")
    check(env.saves >= 1, "the cleared entry is saved")
    check(env.created == [], "no replacement is made")
    check("700" not in env.fm.user_control_thread_ids, "not tracked any more")
    check(ua.archive_thread_id == "600", "the archive thread is left alone")
    # A second startup: nothing left to do, nothing made.
    th.delete.reset_mock()
    await env.fm._ensure_user_posts()
    check(th.delete.await_count == 0 and env.created == [], "a second startup is a no-op")


async def test_off_already_gone() -> None:
    print("off: a thread that is already gone counts as deleted")

    def gone(cid):  # noqa: ANN001, ANN202
        raise discord.NotFound(MagicMock(status=404), "gone")
    env = _Env(_ua(control_room=False, control_thread_id="700", control_message_id="700"),
               fetched=gone)
    ok = await env.fm.retire_user_control_room("42")
    check(ok, "reported as removed")
    check(env.cfg.users["42"].control_thread_id is None, "ids cleared")


async def test_off_delete_fails() -> None:
    print("off: a failed delete keeps the ids for the next startup")
    th = _thread(700)
    th.delete = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
    env = _Env(_ua(control_room=False, control_thread_id="700", control_message_id="700"),
               fetched=lambda cid: th)
    ok = await env.fm.retire_user_control_room("42")
    check(not ok, "reported as not removed")
    check(env.cfg.users["42"].control_thread_id == "700", "ids kept so it is retried")
    check("700" in env.fm.user_control_thread_ids, "still skipped for chat")
    check(env.created == [], "no replacement is made")


async def test_off_never_deletes_non_thread() -> None:
    print("off: an id that is not a thread is never deleted")
    other = MagicMock(spec=discord.TextChannel)
    other.delete = AsyncMock()
    env = _Env(_ua(control_room=False, control_thread_id="700", control_message_id="700"),
               fetched=lambda cid: other)
    await env.fm.retire_user_control_room("42")
    check(other.delete.await_count == 0, "a text channel is not deleted")


async def test_off_no_render() -> None:
    print("off: no redraw, no pin, no follow")
    env = _Env(_ua(control_room=False, control_thread_id="700", control_message_id="700"))
    env.fm._refresh_control_message = AsyncMock()
    await env.fm.refresh_user_control_room("42")
    check(env.fm._refresh_control_message.await_count == 0, "refresh_user_control_room draws nothing")

    # The dashboard redraw loop needs a whole live store to run; its guard is
    # pinned by source, and the refresh above is the behaviour behind it.
    import inspect
    check("if ua.control_thread_id and ua.control_room:" in inspect.getsource(dashboard),
          "the dashboard does not schedule a redraw for it")

    scopes = env.fm._pin_scopes()
    check(scopes and scopes[0][1] is None, "the pin pass has no control room to pin")

    followed: list = []

    async def fake_fetch(tid):  # noqa: ANN001, ANN202
        followed.append(tid)
        return None
    env.fm._fetch_thread = fake_fetch
    await env.fm._auto_follow_pinned()
    check(700 not in followed, "auto-follow skips it")


async def test_absent_unchanged() -> None:
    print("absent: behaves exactly as before")
    env = _Env(_ua())
    await env.fm._ensure_user_posts()
    ua = env.cfg.users["42"]
    check(env.created == ["Guest"], "a missing control room is made")
    check(ua.control_thread_id == "999", "its id is stored")
    check("999" in env.fm.user_control_thread_ids, "and tracked")

    env2 = _Env(_ua(control_thread_id="700", control_message_id="701"))
    await env2.fm._ensure_user_posts()
    check(env2.created == [] and "700" in env2.fm.user_control_thread_ids,
          "an existing one is tracked, not remade")
    env2.fm._refresh_control_message = AsyncMock()
    await env2.fm.refresh_user_control_room("42")
    check(env2.fm._refresh_control_message.await_count == 1, "and redrawn")
    scopes = env2.fm._pin_scopes()
    check(scopes and scopes[0][1] == 700, "and pinned")


async def main() -> None:
    test_model()
    await test_off_no_creation()
    await test_off_retires_existing()
    await test_off_already_gone()
    await test_off_delete_fails()
    await test_off_never_deletes_non_thread()
    await test_off_no_render()
    await test_absent_unchanged()


if __name__ == "__main__":
    asyncio.run(main())
    if FAILS:
        print(f"\n{len(FAILS)} FAILED")
        sys.exit(1)
    print("\nall passed")
