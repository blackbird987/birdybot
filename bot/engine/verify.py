"""Verify Board: per-repo human-verification items.

Pure data helpers — no Discord, no I/O. Operates in place on
ForumProject.verify_items (a list[dict]). The caller is responsible
for persistence (save_forum_map) and refresh scheduling. Mutators
must be invoked under the project's repo lock — see
ForumManager._mutate_verify.

Item shape (dict, JSON-safe):
    id: str             — short opaque id, e.g. "v-a3f29c"
    text: str           — one-liner, ≤120 chars
    origin_thread_id: int | None
    origin_thread_name: str | None
    origin_instance_id: str | None
    created_at: str     — iso8601 utc
    status: "pending" | "claimed" | "done" | "dismissed"
    resolved_at: str | None
    resolved_by: int | None
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Iterable

VALID_STATUSES = ("pending", "claimed", "done", "dismissed")
_RESOLVED_STATUSES = frozenset({"done", "dismissed"})
MAX_TEXT_LEN = 120
MAX_ITEMS_PER_SESSION = 2
PRUNE_DAYS_DEFAULT = 7
PRUNE_CAP_DEFAULT = 50

_VERIFY_BLOCK_RE = re.compile(
    r"```verify\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id() -> str:
    # 24 bits → ~16M ids; collision odds stay negligible over a long-lived board
    return f"v-{secrets.token_hex(3)}"


def _truncate(text: str, n: int = MAX_TEXT_LEN) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _apply_resolution(
    item: dict, status: str, user_id: int | None, now: str | None,
) -> None:
    """Assign status + resolved_at/resolved_by in one place.

    `now` is the timestamp to stamp on resolved items; pass None to have
    set_status generate its own, or a shared value for a batch so every
    item in bulk_set_status gets the same resolved_at.
    """
    item["status"] = status
    if status in _RESOLVED_STATUSES:
        item["resolved_at"] = now or _now_iso()
        item["resolved_by"] = user_id
    else:
        item["resolved_at"] = None
        item["resolved_by"] = None


def parse_verify_blocks(text: str) -> list[str]:
    """Extract verify items from one or more ```verify fenced blocks.

    Lines inside the block starting with "- " or "* " are treated as items.
    Other lines are ignored. Returns a flat list of item texts (may exceed
    MAX_ITEMS_PER_SESSION — caller is responsible for capping).
    """
    if not text:
        return []
    items: list[str] = []
    for match in _VERIFY_BLOCK_RE.finditer(text):
        body = match.group(1)
        for line in body.splitlines():
            line = line.strip()
            if line.startswith(("- ", "* ")):
                content = line[2:].strip()
                if content:
                    items.append(_truncate(content))
    return items


def add_item(
    proj_items: list[dict],
    text: str,
    *,
    origin_thread_id: int | None = None,
    origin_thread_name: str | None = None,
    origin_instance_id: str | None = None,
) -> dict:
    """Append a new pending item. Returns the created item dict."""
    item = {
        "id": _gen_id(),
        "text": _truncate(text),
        "origin_thread_id": origin_thread_id,
        "origin_thread_name": origin_thread_name,
        "origin_instance_id": origin_instance_id,
        "created_at": _now_iso(),
        "status": "pending",
        "resolved_at": None,
        "resolved_by": None,
    }
    proj_items.append(item)
    return item


def set_status(
    proj_items: list[dict],
    item_id: str,
    status: str,
    user_id: int | None = None,
) -> dict | None:
    """Set status on a single item. Returns the updated item or None."""
    if status not in VALID_STATUSES:
        return None
    for item in proj_items:
        if item.get("id") == item_id:
            _apply_resolution(item, status, user_id, None)
            return item
    return None


def bulk_set_status(
    proj_items: list[dict],
    item_ids: Iterable[str],
    status: str,
    user_id: int | None = None,
) -> int:
    """Apply set_status to many ids. Returns the number actually updated."""
    if status not in VALID_STATUSES:
        return 0
    ids = set(item_ids)
    if not ids:
        return 0
    # Share a single timestamp across the batch so all items resolve at once
    now = _now_iso() if status in _RESOLVED_STATUSES else None
    updated = 0
    for item in proj_items:
        if item.get("id") in ids:
            _apply_resolution(item, status, user_id, now)
            updated += 1
    return updated


def get_by_lane(proj_items: list[dict], lane: str) -> list[dict]:
    """Return items in a lane: needs_check | claimed | done_today | done_recent.

    needs_check: status == pending
    claimed:     status == claimed
    done_today:  status in (done, dismissed) AND resolved within last 24h
    done_recent: status in (done, dismissed) AND resolved within last 30d
    """
    if lane == "needs_check":
        return [i for i in proj_items if i.get("status") == "pending"]
    if lane == "claimed":
        return [i for i in proj_items if i.get("status") == "claimed"]

    cutoff_hours = 24 if lane == "done_today" else 24 * 30
    cutoff = datetime.now(timezone.utc) - timedelta(hours=cutoff_hours)
    out: list[dict] = []
    for i in proj_items:
        if i.get("status") not in _RESOLVED_STATUSES:
            continue
        ra = i.get("resolved_at")
        if not ra:
            continue
        try:
            dt = datetime.fromisoformat(ra)
        except ValueError:
            continue
        if dt >= cutoff:
            out.append(i)
    return out


def has_stale_pending(proj_items: list[dict], hours: int = 24) -> bool:
    """True if any pending item is older than `hours`."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    for i in proj_items:
        if i.get("status") != "pending":
            continue
        ca = i.get("created_at")
        if not ca:
            continue
        try:
            dt = datetime.fromisoformat(ca)
        except ValueError:
            continue
        if dt < cutoff:
            return True
    return False


def prune_old(
    proj_items: list[dict],
    *,
    days: int = PRUNE_DAYS_DEFAULT,
    cap: int = PRUNE_CAP_DEFAULT,
) -> int:
    """Remove done/dismissed items older than `days` and cap total resolved count.

    Mutates proj_items in place. Returns count removed. Pending/claimed are
    never pruned.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    keep: list[dict] = []
    resolved: list[dict] = []
    for i in proj_items:
        if i.get("status") in _RESOLVED_STATUSES:
            ra = i.get("resolved_at")
            try:
                dt = datetime.fromisoformat(ra) if ra else None
            except ValueError:
                dt = None
            if dt is None or dt >= cutoff:
                resolved.append(i)
            # else: drop (older than cutoff)
        else:
            keep.append(i)

    # Cap resolved at most `cap` (newest first by resolved_at)
    def _key(item: dict) -> str:
        return item.get("resolved_at") or ""

    resolved.sort(key=_key, reverse=True)
    if len(resolved) > cap:
        resolved = resolved[:cap]

    new_list = keep + resolved
    removed = len(proj_items) - len(new_list)
    if removed:
        proj_items[:] = new_list
    return removed


def get_item(proj_items: list[dict], item_id: str) -> dict | None:
    for i in proj_items:
        if i.get("id") == item_id:
            return i
    return None
