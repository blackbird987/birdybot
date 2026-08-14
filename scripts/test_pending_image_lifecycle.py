"""Harness for the upload retention reaper — what survives, what gets reaped.

Run: ./.venv/bin/python scripts/test_pending_image_lifecycle.py

The regression this guards, from the logs of 2026-08-14 17:57: a screenshot was
saved at :05 and a second at :27. The run reading the first was killed by a
Steer at :30. At :31 both files were deleted — one by the Run Now path's
cleanup (which fires when the run RETURNS, and a killed run returns), one by
the receiving message frame's cleanup. At :32 the replacement run started
holding two paths that no longer existed and told the user the folder was
empty. It was.

The cause was scoping an upload's lifetime to the function that received the
message. That is never right for a resumable session: the path lives on in the
transcript, so a steer, a retry, or a later "look at that screenshot again"
turn can still read it. Files are reaped on a retention timer now, and these
cases pin that policy down — including the disk guard that keeps retention
from being an unbounded promise.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.discord.bot import reap_pending_images  # noqa: E402

PASS, FAIL = 0, 0
NOW = 1_800_000_000.0  # fixed clock — nothing here depends on wall time
HOUR = 3600.0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")


def put(name: str, *, age_hours: float, size: int = 1024) -> Path:
    """Write a file into the pending dir, backdated to a chosen age."""
    p = config.PENDING_IMAGES_DIR / name
    p.write_bytes(b"x" * size)
    stamp = NOW - age_hours * HOUR
    os.utime(p, (stamp, stamp))
    return p


def clear_dir() -> None:
    for f in config.PENDING_IMAGES_DIR.iterdir():
        if f.is_file():
            f.unlink(missing_ok=True)


def main() -> int:
    # Never touch the real data dir — point the module at scratch space.
    real_dir = config.PENDING_IMAGES_DIR
    tmp = tempfile.TemporaryDirectory(prefix="pending_images_test_")
    config.PENDING_IMAGES_DIR = Path(tmp.name)
    try:
        return run_cases()
    finally:
        config.PENDING_IMAGES_DIR = real_dir
        tmp.cleanup()

def run_cases() -> int:
    print("\n== A fresh upload survives (the whole point) ==")
    clear_dir()
    fresh = put("fresh.png", age_hours=0)
    just_read = put("mid_run.png", age_hours=0.001)
    reap_pending_images(set(), NOW)
    check("upload from this minute is still there", fresh.exists())
    check("upload a run is reading right now is still there", just_read.exists())

    print("\n== Past the retention window it goes ==")
    clear_dir()
    old = put("old.png", age_hours=72)
    young = put("young.png", age_hours=47)
    aged, evicted = reap_pending_images(set(), NOW, ttl_hours=48)
    check("older than the window is gone", not old.exists())
    check("inside the window survives", young.exists())
    check("counted as aged out", (aged, evicted) == (1, 0), f"{aged},{evicted}")

    print("\n== A queued prompt's upload is untouchable ==")
    clear_dir()
    queued = put("queued.png", age_hours=500)
    reap_pending_images({str(queued.resolve())}, NOW, ttl_hours=48)
    check("ancient but still referenced by a queue entry → kept",
          queued.exists())

    print("\n== Size cap evicts oldest-first ==")
    clear_dir()
    a = put("a.png", age_hours=10, size=1000)
    b = put("b.png", age_hours=5, size=1000)
    c = put("c.png", age_hours=1, size=1000)
    aged, evicted = reap_pending_images(
        set(), NOW, ttl_hours=48, max_bytes=2500, min_age_secs=60)
    check("oldest evicted to get back under the cap", not a.exists())
    check("newer ones kept", b.exists() and c.exists())
    check("stopped as soon as it fit", evicted == 1, str(evicted))

    print("\n== The young-file floor beats the cap ==")
    clear_dir()
    # Everything is minutes old and way over cap: a burst of uploads in another
    # thread must not be able to yank a picture out from under a live run.
    live = [put(f"live{i}.png", age_hours=0.05, size=1000) for i in range(4)]
    aged, evicted = reap_pending_images(
        set(), NOW, ttl_hours=48, max_bytes=1000, min_age_secs=900)
    check("nothing young was reaped even over cap",
          all(f.exists() for f in live), str(evicted))

    print("\n== Cap eviction skips the young and takes the old ==")
    clear_dir()
    stale = put("stale.png", age_hours=6, size=5000)
    recent = put("recent.png", age_hours=0.01, size=5000)
    reap_pending_images(
        set(), NOW, ttl_hours=48, max_bytes=6000, min_age_secs=900)
    check("old one evicted", not stale.exists())
    check("young one survives", recent.exists())

    print("\n== Odd inputs don't take the reaper down ==")
    clear_dir()
    (config.PENDING_IMAGES_DIR / "subdir").mkdir(exist_ok=True)
    keep = put("keep.png", age_hours=1)
    aged, evicted = reap_pending_images(set(), NOW)
    check("a directory in there is ignored, not crashed on", keep.exists())
    missing = config.PENDING_IMAGES_DIR / "gone.png"
    check("no counts from an empty pass", (aged, evicted) == (0, 0),
          f"{aged},{evicted}")
    check("nothing invented", not missing.exists())

    print("\n== 17:57 replay: steer kills the run mid-read ==")
    clear_dir()
    # Picture one is being read by the run that is about to be killed; picture
    # two arrives with the steering message. Under the old code both were gone
    # a second later. Nothing deletes them now, so the replacement run finds
    # them both.
    first = put("shot_a.png", age_hours=0.006)   # ~22s old
    second = put("shot_b.png", age_hours=0)
    reap_pending_images(set(), NOW)              # reaper runs; run was killed
    check("the picture the killed run was reading is intact", first.exists())
    check("the picture that came with the steer is intact", second.exists())

    clear_dir()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
