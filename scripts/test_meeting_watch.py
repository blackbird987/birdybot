#!/usr/bin/env python3
"""Does the calendar watcher start and stop recordings at the right moments?

The watcher only does its real job during an actual Teams call, so the logic
that matters -- is this event an online meeting, is one running now, is the
recording still alive -- would otherwise never be exercised until a meeting is
already being missed. This drives it against fake calendar items and fake
markers in a temp directory: no Outlook, no microphone, no spawned recorder.

Covers the failure the marker was redesigned for: if the detached live process
dies (crash, or a reboot that killed the child while the marker survived on
disk), the watcher used to see the marker, assume all was well, and silently
record nothing for the rest of the meeting.

    python scripts/test_meeting_watch.py
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot" / "services"))
import meeting  # noqa: E402
import meeting_watch as mw  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


class FakeItem:
    """Stands in for an Outlook calendar COM item."""

    def __init__(self, subject, start, end, location="", body="",
                 all_day=False, online=False):
        self.Subject = subject
        self.Start = start
        self.End = end
        self.Location = location
        self.Body = body
        self.AllDayEvent = all_day
        self.IsOnlineMeeting = online


def test_is_online():
    print("\nRecognising an online meeting:")
    t = datetime.datetime(2026, 8, 6, 10, 0)
    check("Teams join link in the body",
          mw._is_online(FakeItem("Standup", t, t, body=
              "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc")))
    check("Zoom link in the location",
          mw._is_online(FakeItem("Sync", t, t, location="https://acme.zoom.us/j/123")))
    check("Outlook's own online-meeting flag",
          mw._is_online(FakeItem("Review", t, t, online=True)))
    check("a room booking is not an online meeting",
          not mw._is_online(FakeItem("Coffee", t, t, location="Room 3.14",
                                     body="see you there")))


def test_slug():
    print("\nSession naming:")
    m = {"subject": "Q3 Planning / roadmap!", "start": datetime.datetime(2026, 8, 6, 14, 30)}
    slug = mw._slug(m)
    check("start time + subject", slug == "20260806_1430_Q3_Planning_roadmap", slug)
    # If the recorder's own sanitiser rewrote the slug, the marker written under
    # one name would be looked up under another and every meeting would double-start.
    safe = "".join(c for c in slug if c.isalnum() or c in "-_")
    check("survives the recorder's sanitiser unchanged", safe == slug, f"{slug} -> {safe}")
    check("a subject of pure punctuation still yields a name",
          mw._slug({"subject": "!!!", "start": m["start"]}).endswith("meeting"))


def test_active_meeting_selection(monkey_items):
    print("\nPicking the meeting that is running now:")
    now = datetime.datetime(2026, 8, 6, 10, 30)
    earlier = FakeItem("All-hands", now - datetime.timedelta(hours=1),
                       now + datetime.timedelta(hours=1), online=True)
    joined = FakeItem("Design review", now - datetime.timedelta(minutes=5),
                      now + datetime.timedelta(minutes=25), online=True)
    over = FakeItem("Earlier call", now - datetime.timedelta(hours=3),
                    now - datetime.timedelta(hours=2), online=True)
    offline = FakeItem("Lunch", now, now + datetime.timedelta(hours=1), location="Canteen")
    allday = FakeItem("Conference", now - datetime.timedelta(hours=2),
                      now + datetime.timedelta(hours=8), all_day=True, online=True)

    monkey_items([earlier, joined, over, offline, allday])
    active = mw.active_meeting(now)
    check("the most recently started overlapping call wins",
          active is not None and active["subject"] == "Design review",
          str(active))

    monkey_items([over, offline, allday])
    check("nothing running -> None", mw.active_meeting(now) is None)


def test_marker_roundtrip(tmp):
    print("\nRecording markers:")
    end = datetime.datetime(2026, 8, 6, 11, 0)
    mw._write_marker("sess1", end, 4242)
    recs = mw._recording_slugs()
    check("end time and pid survive a write/read",
          recs.get("sess1", {}).get("end") == end and recs["sess1"]["pid"] == 4242,
          str(recs))

    # A marker written by the previous build was a bare ISO timestamp.
    legacy_dir = meeting._session_dir("legacy")
    with open(os.path.join(legacy_dir, mw.MARKER), "w", encoding="utf-8") as f:
        f.write(end.isoformat())
    recs = mw._recording_slugs()
    check("a pre-JSON marker still reads",
          recs.get("legacy", {}).get("end") == end and recs["legacy"]["pid"] is None,
          str(recs.get("legacy")))

    mw._clear_marker("sess1")
    mw._clear_marker("legacy")
    check("cleared markers disappear", mw._recording_slugs() == {})
    check("clearing a marker that is not there is harmless",
          mw._clear_marker("never-existed") is None)


def test_tick_lifecycle(tmp, monkey_items):
    print("\nStarting and stopping across a tick:")
    now = datetime.datetime(2026, 8, 6, 10, 30)
    started = []
    real_start = mw._start_recording
    mw._start_recording = lambda slug, end: started.append(slug)
    try:
        # 1. meeting is over -> stop signal sent, marker cleared
        monkey_items([])
        mw._write_marker("finished", now - datetime.timedelta(minutes=1), os.getpid())
        mw.tick(now)
        check("a finished meeting gets the stop sentinel",
              os.path.exists(meeting._stop_path("finished")))
        check("a finished meeting's marker is cleared",
              "finished" not in mw._recording_slugs())

        # 2. recording still running and process alive -> left alone
        mw._write_marker("running", now + datetime.timedelta(minutes=30), os.getpid())
        mw.tick(now)
        check("a live recording is left alone",
              "running" in mw._recording_slugs() and not started)
        mw._clear_marker("running")

        # 3. the regression: process died mid-meeting -> marker cleared so it restarts
        dead_pid = _dead_pid()
        mw._write_marker("crashed", now + datetime.timedelta(minutes=30), dead_pid)
        monkey_items([])
        mw.tick(now)
        check("a dead recording's marker is cleared",
              "crashed" not in mw._recording_slugs())

        # 4. an active meeting with no marker -> recording starts
        started.clear()
        item = FakeItem("Design review", now - datetime.timedelta(minutes=5),
                        now + datetime.timedelta(minutes=25), online=True)
        monkey_items([item])
        active = mw.tick(now)
        check("an active meeting starts a recording",
              started == [mw._slug({"subject": "Design review",
                                    "start": item.Start})], str(started))
        check("tick reports the active meeting",
              active is not None and active["subject"] == "Design review")

        # 5. same meeting next tick, marker present -> no double-start
        started.clear()
        mw._write_marker(mw._slug({"subject": "Design review", "start": item.Start}),
                         item.End, os.getpid())
        mw.tick(now)
        check("the next tick does not start it twice", started == [], str(started))
    finally:
        mw._start_recording = real_start


def _dead_pid() -> int:
    """A pid that is not running (spawn a trivial process and let it exit)."""
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def main() -> int:
    print("Meeting watcher -- calendar detection and recording lifecycle")
    with tempfile.TemporaryDirectory() as tmp:
        # Keep every marker/sentinel out of the real data/meeting directory.
        meeting._base_dir = lambda: tmp

        def monkey_items(items):
            mw._iter_active_items = lambda now: iter(items)

        # active_meeting() wraps the scan in Outlook's COM retry helper.
        mw.outlook._with_retry = lambda fn: fn()

        test_is_online()
        test_slug()
        test_active_meeting_selection(monkey_items)
        test_marker_roundtrip(tmp)
        test_tick_lifecycle(tmp, monkey_items)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
