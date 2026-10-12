#!/usr/bin/env python3
"""Regression test: queued prompts are persisted atomically.

``pending._persist`` wrote ``pending_prompts.json`` in place. A crash or a
full disk mid-write left half a JSON document, and ``load_from_disk`` treats
an unparseable file as empty, so every queued prompt was lost on the next
boot. Pinned:

  * a register round-trips through ``load_from_disk``;
  * a write that fails part-way leaves the previous file intact and
    readable, and leaves no temp file behind;
  * clearing the last prompt persists an empty list.

Run:  python scripts/test_pending_persist.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config  # noqa: E402
from bot.engine import pending  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


class _Boom(Exception):
    pass


def main() -> int:
    saved_file = config.PENDING_PROMPTS_FILE
    saved_replace = os.replace
    with tempfile.TemporaryDirectory() as td:
        config.PENDING_PROMPTS_FILE = Path(td) / "pending_prompts.json"
        pending._by_id.clear()
        pending._by_channel.clear()
        try:
            first = pending.register("c1", "s1", "first prompt", "m1", "q-1")
            loaded = pending.load_from_disk()
            check([p.id for p in loaded] == [first.id] and loaded[0].prompt_text == "first prompt",
                  "a registered prompt round-trips")

            def failing_replace(src, dst):  # noqa: ANN001
                raise _Boom("disk full")
            os.replace = failing_replace
            pending.register("c2", "s2", "second prompt", "m2", "q-2")
            os.replace = saved_replace
            loaded = pending.load_from_disk()
            check([p.id for p in loaded] == [first.id],
                  "a failed write leaves the previous file readable")
            check(sorted(os.listdir(td)) == ["pending_prompts.json"],
                  f"no temp file is left behind ({os.listdir(td)})")

            for pid in list(pending._by_id):
                pending.clear(pid)
            check(pending.load_from_disk() == [], "clearing the last prompt persists an empty list")
        finally:
            os.replace = saved_replace
            config.PENDING_PROMPTS_FILE = saved_file
            pending._by_id.clear()
            pending._by_channel.clear()

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
