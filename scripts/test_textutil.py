#!/usr/bin/env python3
"""Harness for the shared helpers in bot/textutil.py.

``parse_iso_utc`` replaced a parser copied into about twenty modules, half of
which did not handle a naive timestamp: a hand-edited or migrated state file
carrying one raised ``TypeError`` against an aware ``now`` deep inside a
scheduler, cooldown or retention loop. This pins the one contract they all
share now, and that no module grows a private copy again.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.textutil import hard_clip, parse_iso_utc  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def main() -> int:
    aware = datetime(2026, 10, 12, 8, 30, tzinfo=timezone.utc)

    check("aware stamp round-trips", parse_iso_utc(aware.isoformat()) == aware)
    naive = parse_iso_utc("2026-10-12T08:30:00")
    check("naive stamp is read as UTC", naive == aware and naive.tzinfo is not None)
    check("trailing Z parses", parse_iso_utc("2026-10-12T08:30:00Z") == aware)
    plus2 = parse_iso_utc("2026-10-12T10:30:00+02:00")
    check("an explicit offset is kept, not overwritten", plus2 == aware
          and plus2.utcoffset() == timedelta(hours=2))
    for bad in (None, "", "   ", "yesterday", "2026-13-45", 42, object(), b"2026-10-12"):
        check(f"{bad!r} -> None", parse_iso_utc(bad) is None)

    # The naive case is the reason it exists: comparing against an aware now
    # must not raise.
    now = datetime.now(timezone.utc)
    try:
        _ = now - parse_iso_utc("2020-01-01T00:00:00")
        ok = True
    except TypeError:
        ok = False
    check("naive stamp subtracts from an aware now", ok)

    # hard_clip is exact: Discord rejects one char over, so it must land on
    # the limit and never past it.
    long = "word " * 500
    for n, suf in ((1024, "..."), (256, "\u2026"), (200, "\u2026")):
        out = hard_clip(long, n, suf)
        check(f"hard_clip({n}, {suf!r}) is exactly {n}", len(out) == n and out.endswith(suf))
    check("hard_clip leaves short text alone", hard_clip("abc", 10) == "abc")
    check("hard_clip of None/empty is ''", hard_clip(None, 5) == "" and hard_clip("", 5) == "")
    check("hard_clip with a limit under the suffix stays in bounds",
          len(hard_clip("abcdef", 2, "...")) == 2 and hard_clip("abc", -1) == "")

    # No module keeps a private copy: every bare fromisoformat left in bot/
    # is one of the deliberate exceptions below.
    allowed = {
        "bot/textutil.py",           # the helper itself
        "bot/services/meeting_watch.py",  # compares naive local times on purpose
        "bot/discord/orchestrator.py",    # restamps a naive entry, so must see it
    }
    offenders = []
    for path in sorted((ROOT / "bot").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in allowed:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\.fromisoformat\(", code) and "`" not in line:
                offenders.append(f"{rel}:{n}")
    check("no private ISO parser copies in bot/", not offenders, ", ".join(offenders[:5]))

    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
