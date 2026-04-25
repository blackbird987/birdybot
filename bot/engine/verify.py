"""Verify Board — per-repo human-verification items.

A verify item is a short, human-actionable check that a person needs to
eyeball in a running app (e.g. "OI indicator renders on perps"). Items
live on `ForumProject.verify_items` as dicts and are rendered into a
single living message in a pinned `verify-board` forum thread.

This module owns:
- The item shape (`VerifyItem`)
- The fenced-block parser (```verify-board ... ```)
- Pure transitions (add, set_status, prune_old) — no I/O, no Discord
"""

from __future__ import annotations

import random
import re
import string
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

VerifyStatus = Literal["pending", "claimed", "done", "dismissed"]

_VALID_STATUSES = ("pending", "claimed", "done", "dismissed")

# One fenced-block form we accept. Using "verify-board" (hyphen) to avoid
# colliding with the existing ```verify``` block emitted by VERIFY_PROMPT.
_BLOCK_RE = re.compile(r"```verify-board\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

# Bullet lines inside the block: "- foo" or "* foo" or "1. foo".
_BULLET_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$")

MAX_ITEM_CHARS = 120
MAX_ITEMS_PER_SESSION = 2
PRUNE_DAYS = 7
MAX_STORED_ITEMS = 50


@dataclass
class VerifyItem:
    id: str                                  # "v-a3f2" — short random
    text: str                                # one-liner, trimmed
    status: VerifyStatus = "pending"
    origin_thread_id: str | None = None      # Discord thread id (str)
    origin_thread_name: str | None = None    # short label e.g. "t-2842"
    origin_instance_id: str | None = None    # e.g. "t-2842"
    created_at: str = ""                     # iso utc
    resolved_at: str | None = None
    resolved_by: str | None = None           # Discord user id (str)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "VerifyItem":
        status = d.get("status", "pending")
        if status not in _VALID_STATUSES:
            status = "pending"
        # Normalise at load too — defends against pre-existing state.json
        # written before `add_item` learned to collapse whitespace, and
        # against any hand-edits that snuck a newline into an item.
        text = _normalise(str(d.get("text", "")))[:MAX_ITEM_CHARS]
        return cls(
            id=d.get("id") or _new_id(),
            text=text,
            status=status,  # type: ignore[arg-type]
            origin_thread_id=d.get("origin_thread_id"),
            origin_thread_name=d.get("origin_thread_name"),
            origin_instance_id=d.get("origin_instance_id"),
            created_at=d.get("created_at") or _now_iso(),
            resolved_at=d.get("resolved_at"),
            resolved_by=d.get("resolved_by"),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    alpha = string.ascii_lowercase + string.digits
    return "v-" + "".join(random.choices(alpha, k=4))


def _normalise(text: str) -> str:
    """Collapse all internal whitespace (incl. newlines) to single spaces.

    Defensive: modals enforce single-line via TextStyle.short, and the
    parser extracts per-line, but programmatic callers could still pass
    a multi-line string. A rogue `\\n` in item text breaks the numbered
    lane rendering.
    """
    return " ".join((text or "").split())


def parse_verify_blocks(text: str) -> list[str]:
    """Extract verify-board items from ```verify-board fenced blocks.

    Returns the raw item texts (trimmed, truncated, de-duplicated,
    capped at MAX_ITEMS_PER_SESSION). Safe on malformed input —
    returns [] when no block or no bullets are found.
    """
    if not text:
        return []
    items: list[str] = []
    seen: set[str] = set()
    for m in _BLOCK_RE.finditer(text):
        for line in m.group(1).splitlines():
            bm = _BULLET_RE.match(line)
            if not bm:
                continue
            raw = _normalise(bm.group(1))
            if not raw:
                continue
            if len(raw) > MAX_ITEM_CHARS:
                raw = raw[: MAX_ITEM_CHARS - 1].rstrip() + "…"
            key = raw.lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(raw)
            if len(items) >= MAX_ITEMS_PER_SESSION:
                return items
    return items


_EXCESS_BLANK_RE = re.compile(r"\n{3,}")


def strip_verify_blocks(text: str) -> str:
    """Remove ```verify-board``` fences from displayed text.

    Collapses the ≥3 consecutive newlines that surrounding blank lines
    leave behind so the stripped result doesn't show a visible gap.
    """
    if not text:
        return text
    out = _BLOCK_RE.sub("", text)
    out = _EXCESS_BLANK_RE.sub("\n\n", out)
    return out.rstrip()


def add_item(
    items: list[dict],
    text: str,
    *,
    origin_thread_id: str | None = None,
    origin_thread_name: str | None = None,
    origin_instance_id: str | None = None,
) -> VerifyItem | None:
    """Append a new pending item to `items` (mutates in place).

    Returns the created item, or None if the text is empty. Dedupes
    against existing pending/claimed items with the same normalized
    text (case-insensitive).
    """
    text = _normalise(text)
    if not text:
        return None
    if len(text) > MAX_ITEM_CHARS:
        text = text[: MAX_ITEM_CHARS - 1].rstrip() + "…"
    key = text.lower()
    for d in items:
        if d.get("status") in ("pending", "claimed") and str(d.get("text", "")).lower() == key:
            return None  # dedupe — silently skip
    item = VerifyItem(
        id=_new_id(),
        text=text,
        status="pending",
        origin_thread_id=origin_thread_id,
        origin_thread_name=origin_thread_name,
        origin_instance_id=origin_instance_id,
        created_at=_now_iso(),
    )
    items.append(item.to_dict())
    return item


def set_status(
    items: list[dict],
    item_id: str,
    status: VerifyStatus,
    user_id: str | None = None,
) -> VerifyItem | None:
    """Transition one item by id. Mutates `items` in place.

    Returns the updated item, or None if not found. `pending`/`claimed`
    are "open" states; `done`/`dismissed` are terminal and stamp
    resolved_at + resolved_by.
    """
    if status not in _VALID_STATUSES:
        return None
    for d in items:
        if d.get("id") != item_id:
            continue
        d["status"] = status
        if status in ("done", "dismissed"):
            d["resolved_at"] = _now_iso()
            d["resolved_by"] = user_id
        else:
            d["resolved_at"] = None
            d["resolved_by"] = None
        return VerifyItem.from_dict(d)
    return None


def prune_old(
    items: list[dict], *, days: int = PRUNE_DAYS, cap: int = MAX_STORED_ITEMS,
) -> int:
    """Drop resolved items older than `days` and cap total list size.

    Pending/claimed items are never pruned by age — they wait forever
    for a human. Returns number of items removed. Mutates in place.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    # Sentinel that's *strictly older* than cutoff so unknown-date items
    # are pruned (setting ts=cutoff would keep them — cutoff < cutoff is False).
    expired_sentinel = cutoff - timedelta(seconds=1)
    before = len(items)
    kept: list[dict] = []
    for d in items:
        status = d.get("status", "pending")
        if status in ("done", "dismissed"):
            resolved = d.get("resolved_at") or d.get("created_at") or ""
            try:
                ts = datetime.fromisoformat(resolved)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                ts = expired_sentinel  # unknown date → treat as expired
            if ts < cutoff:
                continue
        kept.append(d)
    # Cap: keep newest items first (by created_at descending)
    if len(kept) > cap:
        kept.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        kept = kept[:cap]
    items[:] = kept
    return before - len(items)


# --- Rendering helpers (pure — take already-loaded items) ---


def items_by_status(items: list[dict]) -> dict[str, list[VerifyItem]]:
    """Bucket items by status, preserving insertion order within a bucket."""
    out: dict[str, list[VerifyItem]] = {s: [] for s in _VALID_STATUSES}
    for d in items:
        try:
            vi = VerifyItem.from_dict(d)
        except Exception:
            continue
        out.setdefault(vi.status, []).append(vi)
    return out


def has_stale_pending(items: list[dict], *, hours: int = 24) -> bool:
    """True if any pending/claimed item is older than `hours`."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    for d in items:
        if d.get("status") not in ("pending", "claimed"):
            continue
        try:
            ts = datetime.fromisoformat(d.get("created_at", ""))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            return True
    return False
