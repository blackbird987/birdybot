"""Step two of the judge replay: the hand labels on the mined candidates.

Only indexes and kinds live here. The owner's words stay under data/judge_replay/.

A candidate is kept when the owner was correcting finished work: it did the wrong
thing, did not work, or held for one instance where the owner wanted more. Dropped:
new requests, choosing between options offered, direction changes, approvals,
automated prompts, and anything whose following commit does not answer the complaint.

Labelled by hand on 2026-11-04, before any judge run. `looks` marks a miss only a person
looking at or playing the result could see, which is what "only you can judge" is for.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fetch_threads import CACHE, RAW  # noqa: E402

NWM, NARROW, BUG = "not_what_i_meant", "too_narrow", "bug"

# candidate index -> (kind, looks)
LABELS: dict[int, tuple[str, bool]] = {
    0: (NWM, True), 6: (BUG, False), 9: (NWM, True), 16: (NWM, True), 17: (NWM, True),
    23: (BUG, True), 24: (NARROW, False), 25: (NWM, False), 27: (BUG, True), 29: (NWM, True),
    32: (NWM, True), 33: (BUG, True), 37: (NWM, False), 38: (NWM, False), 42: (BUG, False),
    43: (BUG, True), 44: (NWM, False), 47: (NWM, False), 49: (NARROW, False), 50: (NARROW, False),
    51: (NARROW, False), 55: (NARROW, False), 61: (NWM, False), 65: (BUG, False), 66: (NARROW, False),
}

# The complaint in 44 is only spelled out two messages later, in 45, over the same work.
ALSO_READ: dict[int, int] = {44: 45}

_INTERRUPT = "(User interrupted the previous request with a new instruction"


def _norm(s: str) -> str:
    return " ".join(s.split())


def _when(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def full_text(candidate: dict) -> str:
    """The message in full. History only keeps its first 200 characters."""
    head = candidate["message_head"]
    if head.startswith(_INTERRUPT):
        head = head.split("\n", 1)[-1] if "\n" in head else head.split(") ", 1)[-1]
    head = _norm(head)[:40]
    msgs = json.loads((CACHE / f"{candidate['thread']}.json").read_text(encoding="utf-8"))
    sent_by = _when(candidate["message_ts"])
    hits = [m for m in msgs if _norm(m["text"]).startswith(head) and _when(m["ts"]) <= sent_by]
    return hits[-1]["text"] if hits else candidate["message_head"]


def main() -> int:
    candidates = [json.loads(ln) for ln in (RAW / "candidates.jsonl").open(encoding="utf-8")]
    kept = []
    for i, (kind, looks) in sorted(LABELS.items()):
        c = candidates[i]
        words = full_text(c)
        if i in ALSO_READ:
            words += "\n\n" + full_text(candidates[ALSO_READ[i]])
        kept.append({**c, "index": i, "kind": kind, "looks": looks, "correction": words})
    with (RAW / "labels.jsonl").open("w", encoding="utf-8") as f:
        for row in kept:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"{len(candidates)} candidates, {len(kept)} kept")
    print("by kind:", dict(Counter(r["kind"] for r in kept)))
    print("looks or feel:", sum(r["looks"] for r in kept))
    table = Counter((r["repo"], r["kind"]) for r in kept)
    for repo in sorted({r["repo"] for r in kept}):
        print(f"  {repo}:", {k: n for (rp, k), n in sorted(table.items()) if rp == repo})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
