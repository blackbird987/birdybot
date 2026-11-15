#!/usr/bin/env python3
"""How the /choices buttons are used: offers, taps vs typed answers, per repo.

    python scripts/choices_report.py [--days 14] [--json] [--file PATH]

Reads data/choices.jsonl (written by bot/engine/directives/choices.py). The
number that decides whether the feature stays is the tap share: of the offers
that were answered, how many were answered with a tap rather than by typing.
Offers closed by some other turn (superseded) or left for a week (expired)
were not answered and do not count either way.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ("offered", "tapped", "typed", "superseded", "expired", "refused", "suppressed")


def _default_file() -> Path:
    sys.path.insert(0, str(ROOT))
    try:
        from bot import config
        return config.DATA_DIR / "choices.jsonl"
    except Exception:
        return ROOT / "data" / "choices.jsonl"


def load(path: Path, days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
            ts = datetime.fromisoformat(row["ts"])
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            rows.append(row)
    return rows


def _summary(rows: list[dict]) -> dict:
    counts = Counter(r.get("event") for r in rows)
    tapped, typed = counts.get("tapped", 0), counts.get("typed", 0)
    answered = tapped + typed
    secs = [
        float(r["seconds"]) for r in rows
        if r.get("event") in ("tapped", "typed") and isinstance(r.get("seconds"), (int, float))
    ]
    return {
        "counts": {e: counts.get(e, 0) for e in EVENTS},
        "answered": answered,
        "tap_share": round(tapped / answered, 3) if answered else None,
        "median_seconds_to_answer": round(statistics.median(secs), 1) if secs else None,
    }


def report(rows: list[dict]) -> dict:
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_repo[r.get("repo") or "?"].append(r)
    return {
        "overall": _summary(rows),
        "repos": {repo: _summary(rs) for repo, rs in sorted(by_repo.items())},
    }


def _fmt(s: dict) -> str:
    c = s["counts"]
    share = "n/a" if s["tap_share"] is None else f"{s['tap_share']:.0%}"
    med = "n/a" if s["median_seconds_to_answer"] is None else f"{s['median_seconds_to_answer']:.0f}s"
    return (f"offered {c['offered']}, tapped {c['tapped']}, typed {c['typed']}, "
            f"superseded {c['superseded']}, expired {c['expired']}, "
            f"refused {c['refused']}, suppressed {c['suppressed']}; "
            f"tap share {share}, median answer {med}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--file", type=Path, default=None)
    args = ap.parse_args(argv)
    path = args.file or _default_file()
    rows = load(path, args.days)
    out = report(rows)
    out["days"] = args.days
    out["file"] = str(path)
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print(f"/choices, last {args.days} day(s), from {path}")
    if not rows:
        print("No events.")
        return 0
    print(f"overall: {_fmt(out['overall'])}")
    for repo, s in out["repos"].items():
        print(f"  {repo}: {_fmt(s)}")
    share = out["overall"]["tap_share"]
    if share is not None:
        verdict = "keep" if share >= 1 / 3 else "below a third: remove per TODO.md"
        print(f"verdict: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
