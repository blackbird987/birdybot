"""Unit tests for bot.engine.verify (pure data layer).

Run: python scripts/test_verify.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.engine import verify as v


_failures: list[str] = []


def _check(cond: bool, msg: str) -> None:
    if not cond:
        _failures.append(msg)
        print(f"  FAIL: {msg}")
    else:
        print(f"  ok:   {msg}")


def test_parse_verify_blocks() -> None:
    print("test_parse_verify_blocks")
    src = """Some prose.

```verify
- Confirm chart renders on mobile
- Test the deploy succeeds
```

More prose with another block.

```verify
* Star bullets work too
- Another item
```
"""
    items = v.parse_verify_blocks(src)
    _check(items == [
        "Confirm chart renders on mobile",
        "Test the deploy succeeds",
        "Star bullets work too",
        "Another item",
    ], f"parses both blocks (got {items!r})")

    _check(v.parse_verify_blocks("") == [], "empty input")
    _check(v.parse_verify_blocks("no blocks here") == [], "no blocks")
    _check(v.parse_verify_blocks("```verify\n```") == [], "empty block")

    # Truncation
    long_src = "```verify\n- " + "a" * 300 + "\n```"
    items = v.parse_verify_blocks(long_src)
    _check(len(items) == 1 and len(items[0]) <= v.MAX_TEXT_LEN,
           f"long line truncated to MAX_TEXT_LEN (got len={len(items[0]) if items else 0})")


def test_add_item() -> None:
    print("test_add_item")
    items: list[dict] = []
    item = v.add_item(items, "  test item  ")
    _check(item["text"] == "test item", "strips whitespace")
    _check(item["status"] == "pending", "starts pending")
    _check(item["id"].startswith("v-"), "id has v- prefix")
    _check(items[0] is item, "appended in place")

    long = "x" * 200
    long_item = v.add_item(items, long)
    _check(len(long_item["text"]) <= v.MAX_TEXT_LEN, "truncates long text")
    _check(long_item["text"].endswith("…"), "ellipsis on truncation")

    with_origin = v.add_item(
        items, "with origin",
        origin_thread_id=12345, origin_thread_name="t-99",
        origin_instance_id="t-99",
    )
    _check(with_origin["origin_thread_id"] == 12345, "origin id stored")
    _check(with_origin["origin_thread_name"] == "t-99", "origin name stored")


def test_set_status() -> None:
    print("test_set_status")
    items: list[dict] = []
    a = v.add_item(items, "a")
    b = v.add_item(items, "b")

    upd = v.set_status(items, a["id"], "done", user_id=42)
    _check(upd is not None and upd["status"] == "done", "set done")
    _check(upd["resolved_at"] is not None, "resolved_at set")
    _check(upd["resolved_by"] == 42, "resolved_by set")

    upd = v.set_status(items, b["id"], "claimed", user_id=42)
    _check(upd["status"] == "claimed" and upd["resolved_at"] is None,
           "claim does not set resolved_at")

    # Reverting from done back to pending clears resolved fields
    upd = v.set_status(items, a["id"], "pending")
    _check(upd["resolved_at"] is None and upd["resolved_by"] is None,
           "reverting clears resolved fields")

    _check(v.set_status(items, "nope", "done") is None, "missing id returns None")
    _check(v.set_status(items, a["id"], "bogus") is None, "invalid status returns None")


def test_bulk_set_status() -> None:
    print("test_bulk_set_status")
    items: list[dict] = []
    ids = [v.add_item(items, f"i{i}")["id"] for i in range(5)]

    n = v.bulk_set_status(items, ids[:3], "done", user_id=7)
    _check(n == 3, "updates 3")
    _check(all(items[i]["status"] == "done" for i in range(3)), "first 3 done")
    _check(items[3]["status"] == "pending", "rest unchanged")

    _check(v.bulk_set_status(items, [], "done") == 0, "empty ids -> 0")
    _check(v.bulk_set_status(items, ["nope"], "done") == 0, "missing -> 0")
    _check(v.bulk_set_status(items, ids, "bogus") == 0, "invalid status -> 0")


def test_get_by_lane() -> None:
    print("test_get_by_lane")
    items: list[dict] = []
    p = v.add_item(items, "pending one")
    c = v.add_item(items, "claimed one")
    d_today = v.add_item(items, "done today")
    d_old = v.add_item(items, "done old")

    v.set_status(items, c["id"], "claimed")
    v.set_status(items, d_today["id"], "done")

    # Manually backdate d_old's resolved_at to 48h ago
    v.set_status(items, d_old["id"], "done")
    backdated = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    for i in items:
        if i["id"] == d_old["id"]:
            i["resolved_at"] = backdated

    needs = v.get_by_lane(items, "needs_check")
    _check(len(needs) == 1 and needs[0]["id"] == p["id"], "needs_check = 1 pending")

    claimed = v.get_by_lane(items, "claimed")
    _check(len(claimed) == 1 and claimed[0]["id"] == c["id"], "claimed = 1")

    today = v.get_by_lane(items, "done_today")
    today_ids = {i["id"] for i in today}
    _check(d_today["id"] in today_ids and d_old["id"] not in today_ids,
           "done_today filters to <24h")

    recent = v.get_by_lane(items, "done_recent")
    recent_ids = {i["id"] for i in recent}
    _check(d_today["id"] in recent_ids and d_old["id"] in recent_ids,
           "done_recent includes <30d")


def test_has_stale_pending() -> None:
    print("test_has_stale_pending")
    items: list[dict] = []
    fresh = v.add_item(items, "fresh")
    old = v.add_item(items, "old")
    # Backdate old's created_at to 48h ago
    old["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

    _check(v.has_stale_pending(items, hours=24), "old item is stale")
    _check(not v.has_stale_pending(items, hours=72), "no stale at 72h cutoff")

    # Done items don't count even if old
    items2 = []
    o = v.add_item(items2, "done old")
    o["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    v.set_status(items2, o["id"], "done")
    _check(not v.has_stale_pending(items2, hours=24),
           "done items don't count as stale")


def test_prune_old() -> None:
    print("test_prune_old")
    items: list[dict] = []
    p = v.add_item(items, "pending — never pruned")
    keep_done = v.add_item(items, "fresh done")
    drop_done = v.add_item(items, "old done")

    v.set_status(items, keep_done["id"], "done")
    v.set_status(items, drop_done["id"], "done")
    # Backdate the dropped one beyond cutoff
    backdated = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    for i in items:
        if i["id"] == drop_done["id"]:
            i["resolved_at"] = backdated

    removed = v.prune_old(items, days=7, cap=50)
    _check(removed == 1, f"pruned 1 (got {removed})")
    ids = {i["id"] for i in items}
    _check(p["id"] in ids, "pending kept")
    _check(keep_done["id"] in ids, "fresh done kept")
    _check(drop_done["id"] not in ids, "old done removed")

    # Cap test: 60 done items, cap=50
    items2: list[dict] = []
    for i in range(60):
        item = v.add_item(items2, f"d{i}")
        v.set_status(items2, item["id"], "done")
    removed = v.prune_old(items2, days=30, cap=50)
    _check(len(items2) == 50 and removed == 10,
           f"capped to 50 (have {len(items2)}, removed {removed})")


def test_get_item() -> None:
    print("test_get_item")
    items: list[dict] = []
    a = v.add_item(items, "a")
    _check(v.get_item(items, a["id"]) is a, "returns the item by id")
    _check(v.get_item(items, "missing") is None, "missing returns None")


def main() -> int:
    tests = [
        test_parse_verify_blocks,
        test_add_item,
        test_set_status,
        test_bulk_set_status,
        test_get_by_lane,
        test_has_stale_pending,
        test_prune_old,
        test_get_item,
    ]
    for t in tests:
        t()
        print()

    if _failures:
        print(f"FAIL — {len(_failures)} assertion(s)")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("PASS — all assertions ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
