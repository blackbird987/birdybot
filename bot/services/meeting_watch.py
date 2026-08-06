"""Auto-start meeting transcription from the Outlook calendar.

Polls the calendar; when an online meeting (Teams / Zoom / Google Meet) is in
progress it launches `meeting.py live <slug>` automatically and stops it when
the scheduled end passes. Runs as a long-lived background process, started once:

    python meeting_watch.py watch [--poll 60]   # the daemon
    python meeting_watch.py status               # what's active / recording now
    python meeting_watch.py once                 # a single tick (debugging)

It drives meeting.py by subprocess + the .stop sentinel, so transcription still
runs entirely locally on the GPU — no API, no cloud. Recording state lives in a
`.recording` marker per session dir, so a watcher restart neither double-starts
an in-progress meeting nor orphans a finished one.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import sys
import time

# Sibling-script imports (meeting.py / outlook.py live next to this file and do
# not depend on the bot package), regardless of the current working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meeting  # noqa: E402
import outlook  # noqa: E402

log = logging.getLogger(__name__)

MARKER = ".recording"
LOCK = ".watch.lock"
DEFAULT_LANG = os.getenv("MEETING_LANG", "nl")

# Online-meeting fingerprints looked for in the location/body of an event.
_ONLINE_HINTS = (
    "teams.microsoft.com",
    "meetup-join",
    "teams meeting",
    "zoom.us",
    "meet.google.com",
    "webex.com",
)


def _to_dt(v) -> datetime.datetime:
    """pywintypes COM time -> naive local datetime."""
    if hasattr(v, "timestamp"):
        return datetime.datetime.fromtimestamp(v.timestamp())
    return v


def _is_online(item) -> bool:
    """True if the event looks like an online meeting we can transcribe."""
    try:
        if getattr(item, "IsOnlineMeeting", False):
            return True
    except Exception:  # noqa: BLE001 - property may not exist / COM hiccup
        pass
    loc = (getattr(item, "Location", "") or "").lower()
    body = (getattr(item, "Body", "") or "").lower()
    hay = loc + " " + body
    return any(h in hay for h in _ONLINE_HINTS)


def _iter_active_items(now: datetime.datetime):
    """Yield calendar COM items overlapping *now*.

    Mirrors the proven restrict-then-fallback shape of `outlook.read_calendar`:
    the `Restrict` date filter is locale-dependent and can either raise or
    silently match nothing, which would make the watcher permanently believe
    there is never a meeting. So we probe `.Count` and fall back to a bounded
    scan of the sorted folder. The fallback drops `IncludeRecurrences` (needed
    for `.Count`/`.Item(i)` to work reliably), so a recurring meeting is only
    picked up via the primary path.
    """
    ns = outlook._get_namespace()
    cal = ns.GetDefaultFolder(outlook._FOLDER_CALENDAR)
    window_end = now + datetime.timedelta(minutes=1)

    filtered = None
    filtered_count = 0
    try:
        items = cal.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")
        restriction = (
            f"[Start] <= '{window_end.strftime('%m/%d/%Y %I:%M %p')}' AND "
            f"[End] >= '{now.strftime('%m/%d/%Y %I:%M %p')}'"
        )
        filtered = items.Restrict(restriction)
        filtered_count = filtered.Count  # can raise on a bad filter
    except Exception as e:  # noqa: BLE001 - locale-dependent filter can fail
        log.warning("calendar Restrict failed, using fallback scan: %s", e)
        filtered = None

    if filtered is not None and filtered_count > 0:
        try:
            for item in filtered:
                yield item
        except Exception as e:  # noqa: BLE001 - COM iteration can fail at boundary
            log.warning("calendar iteration stopped early: %s", e)
        return

    fb_items = cal.Items
    fb_items.Sort("[Start]")
    try:
        max_scan = min(200, fb_items.Count)
    except Exception:  # noqa: BLE001
        return
    for i in range(1, max_scan + 1):
        try:
            item = fb_items.Item(i)
            if _to_dt(item.Start) > window_end:
                break
            yield item
        except Exception:  # noqa: BLE001 - skip unreadable item
            continue


def active_meeting(now: datetime.datetime | None = None) -> dict | None:
    """Return the online meeting currently in progress, or None.

    If several overlap, the most recently started one wins (you just joined it).
    """
    now = now or datetime.datetime.now()

    def _scan():
        best = None
        for item in _iter_active_items(now):
            try:
                if getattr(item, "AllDayEvent", False):
                    continue
                start = _to_dt(item.Start)
                end = _to_dt(item.End)
                if not (start <= now < end):
                    continue
                if not _is_online(item):
                    continue
                cand = {"subject": item.Subject or "meeting", "start": start, "end": end}
                if best is None or cand["start"] > best["start"]:
                    best = cand
            except Exception:  # noqa: BLE001 - skip any unreadable item
                continue
        return best

    return outlook._with_retry(_scan)


def _slug(m: dict) -> str:
    """Stable, filesystem-safe session name: start-time + trimmed subject."""
    subj = "".join(c for c in m["subject"] if c.isalnum() or c in " -_").strip()
    subj = "_".join(subj.split())[:40] or "meeting"
    return f"{m['start'].strftime('%Y%m%d_%H%M')}_{subj}"


def _marker_path(slug: str) -> str:
    return os.path.join(meeting._session_dir(slug), MARKER)


def _meeting_py() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "meeting.py")


def _start_recording(slug: str, end_dt: datetime.datetime):
    """Spawn a detached `meeting.py live <slug>` and drop a .recording marker."""
    flags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — outlives this watcher.
        flags = 0x00000008 | 0x00000200
    # Keep the child's output: detached with DEVNULL, a live process that dies on
    # startup (no loopback device, missing faster-whisper) would leave the watcher
    # reporting "recording" with nothing to show and no way to find out why.
    logf = open(os.path.join(meeting._session_dir(slug), "live.log"), "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, _meeting_py(), "live", slug, "--lang", DEFAULT_LANG],
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    finally:
        logf.close()
    _write_marker(slug, end_dt, proc.pid)
    log.info("auto-started recording: %s (pid %s)", slug, proc.pid)
    print(f"[{_now_hm()}] started recording: {slug}")


def _write_marker(slug: str, end_dt: datetime.datetime, pid: int | None):
    with open(_marker_path(slug), "w", encoding="utf-8") as f:
        json.dump({"end": end_dt.isoformat(), "pid": pid}, f)


def _clear_marker(slug: str):
    mk = _marker_path(slug)
    if os.path.exists(mk):
        try:
            os.remove(mk)
        except OSError:
            pass


def _stop_recording(slug: str):
    """Signal the live subprocess to stop and clear its marker."""
    open(meeting._stop_path(slug), "w").close()  # live loop sees .stop and exits
    _clear_marker(slug)
    log.info("auto-stopped recording: %s", slug)
    print(f"[{_now_hm()}] stopped recording: {slug}")


def _recording_slugs() -> dict:
    """Map slug -> {"end": datetime, "pid": int|None} for in-progress recordings."""
    base = meeting._base_dir()
    out = {}
    if not os.path.isdir(base):
        return out
    for slug in os.listdir(base):
        mk = os.path.join(base, slug, MARKER)
        if not os.path.exists(mk):
            continue
        try:
            with open(mk, encoding="utf-8") as f:
                raw = f.read().strip()
            try:
                data = json.loads(raw)
                end = datetime.datetime.fromisoformat(data["end"])
                pid = data.get("pid")
            except (ValueError, TypeError, KeyError):
                # pre-JSON marker: bare ISO end-time, no pid
                end = datetime.datetime.fromisoformat(raw)
                pid = None
            out[slug] = {"end": end, "pid": pid}
        except Exception:  # noqa: BLE001 - malformed marker -> treat as ended now
            out[slug] = {"end": datetime.datetime.now(), "pid": None}
    return out


def tick(now: datetime.datetime | None = None) -> dict | None:
    """One poll: stop finished recordings, start one for the active meeting."""
    now = now or datetime.datetime.now()

    # 1) stop any recording whose meeting has ended, and clear markers whose
    #    live process is gone (crash, or a reboot that killed the detached
    #    child while the marker survived on disk) so step 2 can restart it —
    #    otherwise the rest of that meeting is silently never recorded.
    for slug, rec in _recording_slugs().items():
        if now >= rec["end"]:
            _stop_recording(slug)
        elif rec["pid"] and not _pid_alive(rec["pid"]):
            log.warning("recording %s died (pid %s) - clearing marker", slug, rec["pid"])
            print(f"[{_now_hm()}] recording died, will restart: {slug}")
            _clear_marker(slug)

    # 2) start a recording for the active online meeting, if not already running
    active = active_meeting(now)
    if active:
        slug = _slug(active)
        if not os.path.exists(_marker_path(slug)):
            _start_recording(slug, active["end"])
    return active


def _now_hm() -> str:
    n = datetime.datetime.now()
    return f"{n.hour:02d}:{n.minute:02d}"


def cmd_watch(poll: float):
    _acquire_lock()
    print(f"Watching Outlook calendar every {poll:.0f}s for online meetings. Ctrl-C to stop.")
    try:
        while True:
            try:
                active = tick()
                if active:
                    print(f"[{_now_hm()}] active: {active['subject']} (until {active['end'].strftime('%H:%M')})")
            except Exception as e:  # noqa: BLE001 - never let one bad tick kill the watcher
                log.warning("tick failed: %s", e)
                print(f"[{_now_hm()}] tick error: {str(e)[:120]}")
            time.sleep(poll)
    finally:
        _release_lock()


def cmd_status():
    active = active_meeting()
    print("Active online meeting:", (active["subject"] if active else "none"))
    if active:
        print(f"  {active['start'].strftime('%H:%M')} - {active['end'].strftime('%H:%M')}  slug={_slug(active)}")
    recs = _recording_slugs()
    if recs:
        print("Currently recording:")
        for slug, rec in recs.items():
            alive = "" if not rec["pid"] else ("" if _pid_alive(rec["pid"]) else "  [DEAD]")
            print(f"  {slug}  (until {rec['end'].strftime('%H:%M')}){alive}")
    else:
        print("Currently recording: none")


# --- single-instance lock ---

def _lock_path() -> str:
    return os.path.join(meeting._base_dir(), LOCK)


def _acquire_lock():
    os.makedirs(meeting._base_dir(), exist_ok=True)
    path = _lock_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                pid = int(f.read().strip())
        except Exception:  # noqa: BLE001
            pid = None
        if pid and _pid_alive(pid):
            raise SystemExit(f"Another watcher is already running (pid {pid}).")
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def _release_lock():
    path = _lock_path()
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """True if *pid* is a running process.

    Parsed from CSV rather than substring-matched against the table: this now
    gates whether a died-mid-meeting recording gets restarted, so a stray match
    against another column (memory figure, session id) would silently mean the
    rest of a meeting is never recorded.
    """
    if os.name == "nt":
        flags = 0x08000000  # CREATE_NO_WINDOW — don't flash a console each poll
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, creationflags=flags, timeout=15,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return True  # can't tell — assume alive rather than double-start
        for line in out.splitlines():
            fields = [f.strip('"') for f in line.strip().split('","')]
            if len(fields) >= 2 and fields[1].strip() == str(pid):
                return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    logging.basicConfig(level=logging.WARNING)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "watch":
        poll = 60.0
        if "--poll" in sys.argv:
            i = sys.argv.index("--poll")
            if i + 1 < len(sys.argv):
                poll = float(sys.argv[i + 1])
        cmd_watch(poll)
    elif cmd == "once":
        active = tick()
        print("active:", active["subject"] if active else "none")
    elif cmd == "status":
        cmd_status()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
