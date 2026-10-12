#!/usr/bin/env python3
"""Regression test: the startup forum reconcile, phase by phase.

``ForumManager.reconcile_forums`` was one 230-line function. It is now a fixed
sequence of named phases, and two rules have to survive the split:

  * the phases run in the same order as before (validate and publish the
    map, normalize threads, ensure repo posts, drop legacy verify boards,
    migrate archive channels, ensure personal posts, auto-follow);
  * a hidden repo is skipped only by the phase that *creates* posts in its
    forum. Legacy cleanup and auto-follow still run on it, because they
    repair state that must be right whenever the forum comes back.

Also pinned: a sleeping thread keeps its sleep marker when its name is
normalized, and auto-follow skips unset ids and re-reads personal forums.

Run:  python scripts/test_reconcile_forums.py
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

from bot.discord import channels, forums, spawn_colors  # noqa: E402
from bot.discord.access import AccessConfig, UserAccess  # noqa: E402
from bot.discord.forums import ForumManager, ForumProject  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


class _Store:
    def __init__(self, dormant: set[str] | None = None) -> None:
        self.dormant = dormant or set()

    def is_repo_dormant(self, name: str) -> bool:
        return name in self.dormant

    def list_instances(self):  # noqa: ANN201
        return []


def _manager(store: _Store, client=None) -> ForumManager:  # noqa: ANN001
    fm = ForumManager(client or MagicMock(), store, guild_id=1, category_id=2)
    fm.save_forum_map = lambda: None
    return fm


async def _arm_phase_order() -> None:
    print("phases run in order")
    category = MagicMock(spec=discord.CategoryChannel)
    guild = MagicMock()
    guild.get_channel.return_value = category
    client = MagicMock()
    client.get_guild.return_value = guild
    fm = _manager(_Store(), client)
    calls: list[str] = []

    def rec(name, result=None):  # noqa: ANN001, ANN202
        async def a(*_a, **_k):  # noqa: ANN002, ANN003
            calls.append(name)
        def s(*_a, **_k):  # noqa: ANN002, ANN003
            calls.append(name)
            return result
        return s if result is not None else a

    fm._validated_projects = rec("validate", {})
    for name in ("_normalize_forum_threads", "_ensure_repo_posts",
                 "_drop_legacy_verify_boards", "_migrate_archive_channels",
                 "_ensure_user_posts", "_auto_follow_pinned"):
        setattr(fm, name, rec(name))
    await fm.reconcile_forums()
    check(calls == ["validate", "_normalize_forum_threads", "_ensure_repo_posts",
                    "_drop_legacy_verify_boards", "_migrate_archive_channels",
                    "_ensure_user_posts", "_auto_follow_pinned"],
          f"order (got {calls})")

    calls.clear()
    client.get_guild.return_value = None
    await fm.reconcile_forums()
    check(calls == [], "no guild: nothing runs")


async def _arm_hidden_repos() -> None:
    print("hidden repos: posts skipped, repair still runs")
    fm = _manager(_Store(dormant={"parked"}))
    fm._forum_projects = {
        "live": ForumProject(repo_name="live", forum_channel_id="10"),
        "parked": ForumProject(repo_name="parked", forum_channel_id="20"),
    }
    created: list[tuple[str, str]] = []

    async def control(name):  # noqa: ANN001
        created.append(("control", name))

    async def archive(name):  # noqa: ANN001
        created.append(("archive", name))

    fm.ensure_control_post = control
    fm.ensure_archive_thread = archive
    await fm._ensure_repo_posts()
    check(created == [("control", "live"), ("archive", "live")],
          f"only the live repo got posts, control before archive (got {created})")

    # Legacy verify board cleanup still runs on the hidden repo.
    fm._forum_projects["parked"].legacy_verify_thread_id = "99"
    deleted: list[int] = []
    thread = MagicMock(spec=discord.Thread)

    async def delete():
        deleted.append(99)
    thread.delete = delete

    async def fetch(cid):  # noqa: ANN001
        return thread
    fm._client.fetch_channel = fetch
    await fm._drop_legacy_verify_boards()
    check(deleted == [99], "legacy board in a hidden forum is deleted")
    check(fm._forum_projects["parked"].legacy_verify_thread_id is None,
          "legacy id cleared")

    # Auto-follow still runs on the hidden repo.
    fm._forum_projects["parked"].control_thread_id = "21"
    followed: list[tuple[str, str]] = []

    async def fetch_thread(tid):  # noqa: ANN001
        return SimpleNamespace(id=tid)

    async def follow(ch, repo):  # noqa: ANN001
        followed.append((repo, str(ch.id)))

    fm._fetch_thread = fetch_thread
    fm._auto_follow_thread = follow
    orig_cfg, orig_sleep = forums.load_access_config, forums.asyncio.sleep
    forums.load_access_config = lambda: AccessConfig()

    async def no_sleep(_s):  # noqa: ANN001
        return None
    forums.asyncio.sleep = no_sleep
    try:
        await fm._auto_follow_pinned()
    finally:
        forums.load_access_config, forums.asyncio.sleep = orig_cfg, orig_sleep
    check(followed == [("parked", "21")], f"hidden control room followed (got {followed})")


async def _arm_auto_follow() -> None:
    print("auto-follow batches")
    fm = _manager(_Store())
    fm._forum_projects = {
        "a": ForumProject(repo_name="a", forum_channel_id="1",
                          control_thread_id="11", archive_thread_id="12"),
        "b": ForumProject(repo_name="b", forum_channel_id="2"),
    }
    fm._forum_projects["a"].monitor_thread_id = "13"
    followed: list[tuple[str, str]] = []
    sleeps: list[float] = []

    async def fetch_thread(tid):  # noqa: ANN001
        return None if tid == 12 else SimpleNamespace(id=tid)

    async def follow(ch, repo):  # noqa: ANN001
        followed.append((repo, str(ch.id)))

    async def follow_user(ch, uid):  # noqa: ANN001
        followed.append((f"user:{uid}", str(ch.id)))

    async def sleep(s):  # noqa: ANN001
        sleeps.append(s)

    fm._fetch_thread = fetch_thread
    fm._auto_follow_thread = follow
    fm._auto_follow_user_thread = follow_user
    cfg = AccessConfig(users={
        "u1": UserAccess(user_id="u1", display_name="U", control_thread_id="31",
                         archive_thread_id="32"),
    })
    orig_cfg, orig_sleep = forums.load_access_config, forums.asyncio.sleep
    forums.load_access_config = lambda: cfg
    forums.asyncio.sleep = sleep
    try:
        await fm._auto_follow_pinned()
    finally:
        forums.load_access_config, forums.asyncio.sleep = orig_cfg, orig_sleep
    check(followed == [("a", "11"), ("a", "13"), ("user:u1", "31"), ("user:u1", "32")],
          f"followed the fetchable threads (got {followed})")
    check(sleeps == [0.5, 0.5], f"one sleep per non-empty batch (got {sleeps})")


async def _arm_thread_names() -> None:
    print("thread name normalization")
    fm = _manager(_Store())
    proj = ForumProject(repo_name="r", forum_channel_id="1")
    edits: list[str] = []

    class _Thread:
        def __init__(self, name: str) -> None:
            self.id = 5
            self.name = name

        async def edit(self, name=None, **_k):  # noqa: ANN001, ANN003
            edits.append(name)
            self.name = name

    async def compose(tid, base, p, store):  # noqa: ANN001
        return base

    orig = spawn_colors.compose_name
    spawn_colors.compose_name = compose
    try:
        sleeping = channels.build_sleeping_thread_name("fix the thing")
        t = _Thread(sleeping)
        changed = await fm._normalize_thread_name(t, proj)
        check(not changed and edits == [], "an already-clean sleeping name is left alone")
        check(channels.parse_thread_name(t.name)[0], "sleep marker kept")

        t = _Thread("repo│fix the thing")
        await fm._normalize_thread_name(t, proj)
        check(edits and edits[0] == "fix the thing", f"legacy prefix stripped (got {edits})")
        check(t.name == channels.build_thread_name("fix the thing"),
              f"result is the current awake form (got {t.name!r})")
    finally:
        spawn_colors.compose_name = orig


async def main() -> int:
    await _arm_phase_order()
    await _arm_hidden_repos()
    await _arm_auto_follow()
    await _arm_thread_names()
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
