"""Spawn family color tracking.

Each `/spawn`-rooted family gets one of 7 color slots. The root parent's
thread name is prefixed with a colored square, descendants with the matching
colored dot, so the forum sidebar visually clusters a family together.

State lives in `platform_state.discord.spawn_families` (root_thread_id ->
{slot, members}) plus a `color_slot` field stamped on every member's
ThreadInfo so families can self-heal across restarts and historical color
survives a slot release.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from bot.discord.forums import ForumManager, ForumProject
    from bot.store.state import StateStore

log = logging.getLogger(__name__)

# Paired (root_square, descendant_dot) per slot.
PALETTE: list[tuple[str, str]] = [
    ("\U0001F7E5", "\U0001F534"),  # red square / red circle
    ("\U0001F7E7", "\U0001F7E0"),  # orange square / orange circle
    ("\U0001F7E8", "\U0001F7E1"),  # yellow square / yellow circle
    ("\U0001F7E9", "\U0001F7E2"),  # green square / green circle
    ("\U0001F7E6", "\U0001F535"),  # blue square / blue circle
    ("\U0001F7EA", "\U0001F7E3"),  # purple square / purple circle
    ("\U0001F7EB", "\U0001F7E4"),  # brown square / brown circle
]
_DISCORD_NAME_LIMIT = 100

# First-spawn cursor seed. Set to 1 (orange) rather than 0 (red) so the first
# spawn after deploying the round-robin fix differs visually from the buggy
# slot-0-only era. Do not "simplify" to 0 — that defeats the seed's purpose.
_INITIAL_CURSOR = 1

# Module-level lock. Serializes all reads/writes to spawn_families and to
# ThreadInfo.color_slot. Never held across Discord API awaits.
_LOCK = asyncio.Lock()


def _families(store: "StateStore") -> dict:
    state = store.get_platform_state("discord")
    if "spawn_families" not in state:
        # get_platform_state returns a fresh {} when the key is unset; that
        # orphan dict isn't stored, so mutations would vanish when
        # save_forum_map later fetches a different orphan. Re-store to anchor
        # the dict in _platform_state before handing out a sub-ref.
        state["spawn_families"] = {}
        store.set_platform_state("discord", state, persist=False)
    return state["spawn_families"]


PALETTE_CHARS: frozenset[str] = frozenset(emo for pair in PALETTE for emo in pair)


def strip_color_prefix(name: str) -> str:
    """Drop a leading PALETTE emoji + optional space. Idempotent."""
    if not name:
        return name
    if name[0] in PALETTE_CHARS:
        return name[1:].lstrip(" ")
    return name


def _maybe_clear_stale_stamps(
    store: "StateStore",
    forum_manager: "ForumManager",
) -> None:
    """One-shot migration: wipe color_slot stamps assigned by the buggy
    lowest-unused picker. Gated by its own flag so it survives upgrades
    that happen mid-family — skips silently while any family is live,
    runs and self-disables the first time families are idle.
    """
    state = store.get_platform_state("discord")
    if state.get("spawn_color_stamps_cleared"):
        return
    fams = _families(store)
    if fams:
        return  # try again later when families have drained
    cleared = 0
    for fp in forum_manager.forum_projects.values():
        for ti in fp.threads.values():
            if ti.color_slot is not None:
                ti.color_slot = None
                cleared += 1
    state["spawn_color_stamps_cleared"] = True
    store.set_platform_state("discord", state, persist=False)
    if cleared:
        log.info("Spawn-color migration: cleared %d stale color_slot stamps", cleared)


def _get_next_slot(store: "StateStore") -> int:
    """Read the cursor; seed on first use. Caller is responsible for
    invoking save_forum_map() before returning so the seed write persists —
    assign_slot saves on both the success and exhaustion paths.
    """
    state = store.get_platform_state("discord")
    if "spawn_next_slot" not in state:
        state["spawn_next_slot"] = _INITIAL_CURSOR
        store.set_platform_state("discord", state, persist=False)
    raw = state.get("spawn_next_slot", 0)
    if isinstance(raw, int) and 0 <= raw < len(PALETTE):
        return raw
    return 0


def _set_next_slot(store: "StateStore", value: int) -> None:
    state = store.get_platform_state("discord")
    state["spawn_next_slot"] = value % len(PALETTE)
    store.set_platform_state("discord", state, persist=False)


def prefix_for_root(slot: int) -> str:
    return PALETTE[slot][0]


def prefix_for_descendant(slot: int) -> str:
    return PALETTE[slot][1]


def find_root(thread_id: str, forum_project: "ForumProject") -> str:
    """Walk ThreadInfo.parent_thread_id to the topmost ancestor.

    Returns thread_id itself if no parent link is present or the chain
    is broken. Cycle-safe via a visited set.
    """
    visited = {thread_id}
    current = thread_id
    while True:
        info = forum_project.threads.get(current)
        if info is None or not info.parent_thread_id:
            return current
        parent = info.parent_thread_id
        if parent in visited:
            return current
        visited.add(parent)
        current = parent


def _stamp_color_slot(forum_project: "ForumProject", thread_id: str, slot: int) -> None:
    info = forum_project.threads.get(thread_id)
    if info is not None and info.color_slot != slot:
        info.color_slot = slot


def compose_for_slot(slot: int, base: str, *, is_root: bool) -> str:
    """Build a thread name with the color prefix for `slot`.

    Used at thread-create time, when the new thread has no ThreadInfo yet.
    Truncates `base` so the result fits Discord's 100-char limit.
    """
    if not (0 <= slot < len(PALETTE)):
        return base[:_DISCORD_NAME_LIMIT]
    emoji = PALETTE[slot][0] if is_root else PALETTE[slot][1]
    prefix = f"{emoji} "
    if base.startswith(prefix):
        return base[:_DISCORD_NAME_LIMIT]
    budget = _DISCORD_NAME_LIMIT - len(prefix)
    return (prefix + base[:budget])[:_DISCORD_NAME_LIMIT]


async def assign_slot(
    thread_id: str,
    forum_project: "ForumProject",
    store: "StateStore",
    forum_manager: "ForumManager",
) -> tuple[int, str] | None:
    """Resolve thread's family root and ensure a slot is assigned.

    Returns (slot, root_id), or None when all 7 slots are in use.
    Reuses an existing live family, then falls back to the root's
    stamped color_slot (root-revival), then picks via the persistent
    round-robin cursor.
    """
    async with _LOCK:
        # Run-first: must clear stale stamps BEFORE reading root_info.color_slot
        # below, otherwise root-revival would still drag fresh spawns back to
        # the buggy slot 0.
        _maybe_clear_stale_stamps(store, forum_manager)

        root_id = find_root(thread_id, forum_project)
        fams = _families(store)

        entry = fams.get(root_id)
        if entry is not None:
            raw = entry.get("slot")
            if isinstance(raw, int) and 0 <= raw < len(PALETTE):
                return raw, root_id

        root_info = forum_project.threads.get(root_id)
        stamped = root_info.color_slot if root_info else None
        in_use: set[int] = set()
        for v in fams.values():
            s = v.get("slot")
            if isinstance(s, int):
                in_use.add(s)

        chosen: int | None = None
        if stamped is not None and 0 <= stamped < len(PALETTE) and stamped not in in_use:
            chosen = int(stamped)
        else:
            start = _get_next_slot(store)
            for offset in range(len(PALETTE)):
                i = (start + offset) % len(PALETTE)
                if i not in in_use:
                    chosen = i
                    break
            if chosen is not None:
                _set_next_slot(store, chosen + 1)

        if chosen is None:
            # Exhausted: still persist migration/seed writes so they're not lost.
            forum_manager.save_forum_map()
            log.warning(
                "Spawn-color slots exhausted (7 in use); thread %s family unprefixed",
                thread_id,
            )
            return None

        fams[root_id] = {"slot": chosen, "members": [root_id]}
        _stamp_color_slot(forum_project, root_id, chosen)
        forum_manager.save_forum_map()
        return chosen, root_id


async def register_member(
    root_id: str,
    member_id: str,
    forum_project: "ForumProject",
    store: "StateStore",
    forum_manager: "ForumManager",
) -> None:
    """Append member_id to the family at root_id.

    If the family entry has been lost (e.g. released mid-spawn),
    recreates it from the root's stamped color_slot. Idempotent on
    repeated calls with the same member_id.
    """
    async with _LOCK:
        fams = _families(store)
        entry = fams.get(root_id)
        if entry is None:
            root_info = forum_project.threads.get(root_id)
            stamped = root_info.color_slot if root_info else None
            if stamped is None or not (0 <= stamped < len(PALETTE)):
                log.warning(
                    "register_member: no family for root %s and no stamped slot — skipping",
                    root_id,
                )
                return
            entry = {"slot": int(stamped), "members": [root_id]}
            fams[root_id] = entry

        members = entry.setdefault("members", [])
        if member_id not in members:
            members.append(member_id)

        slot_raw = entry.get("slot")
        if isinstance(slot_raw, int) and 0 <= slot_raw < len(PALETTE):
            _stamp_color_slot(forum_project, member_id, slot_raw)
        forum_manager.save_forum_map()


async def release_if_empty(
    thread_id: str,
    is_active_fn: Callable[[str], bool],
    forum_project: "ForumProject",
    store: "StateStore",
    forum_manager: "ForumManager",
) -> None:
    """Drop the family for thread_id's root if no member is still active.

    is_active_fn(thread_id) must be a pure status peek — calling any
    spawn_colors API from it would deadlock on `_LOCK`. Idempotent.
    Names are not renamed on release; the historical color survives.
    """
    async with _LOCK:
        root_id = find_root(thread_id, forum_project)
        fams = _families(store)
        entry = fams.get(root_id)
        if entry is None:
            return

        members = list(entry.get("members") or [root_id])
        for mid in members:
            try:
                if is_active_fn(mid):
                    return
            except Exception:
                log.debug("is_active_fn raised for %s; keeping family", mid, exc_info=True)
                return

        fams.pop(root_id, None)
        forum_manager.save_forum_map()
        log.info("Released spawn-color slot for family root %s", root_id)


async def compose_name(
    thread_id: str,
    base: str,
    forum_project: "ForumProject",
    store: "StateStore",
) -> str:
    """Prefix `base` with the family color for thread_id.

    Resolution order: live family entry first, then the thread's own
    stamped color_slot (preserves historical color after release).
    Returns `base` truncated to 100 chars if no slot is found.
    """
    async with _LOCK:
        root_id = find_root(thread_id, forum_project)
        fams = _families(store)

        slot: int | None = None
        entry = fams.get(root_id)
        if entry is not None:
            raw = entry.get("slot")
            if isinstance(raw, int) and 0 <= raw < len(PALETTE):
                slot = raw
        if slot is None:
            info = forum_project.threads.get(thread_id)
            stamped = info.color_slot if info else None
            if stamped is not None and 0 <= stamped < len(PALETTE):
                slot = int(stamped)

        if slot is None:
            return base[:_DISCORD_NAME_LIMIT]

        is_root = thread_id == root_id
        return compose_for_slot(slot, base, is_root=is_root)
