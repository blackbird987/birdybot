"""Regression test for watches — the event-triggered self-wake.

A self-wake fires on a clock, so a session facing a long job has to guess a
delay. A *watch* fires on the job: the poller checks the process (or a
done-marker in its log) on the scheduler's 30s tick and, when it finishes,
calls ``store.add_wake`` with ``next_run_at=now``. Everything downstream of a
wake is therefore inherited rather than reimplemented, and this harness locks
in exactly that: a fired watch must leave behind a **thread-bound Schedule**,
not some parallel resume mechanism.

Runs the real production code against a real StateStore on a temp file and
real OS processes — no mocking of liveness, because "is this process still
running" is the one thing a mock would happily get wrong forever.

Covers:
  - directive parsing, including the quoted/fenced EXAMPLE guards
  - a directive with no trigger (no pid, no done marker) is refused
  - duration parsing (``6h`` / ``90m`` / bare seconds / garbage -> default)
  - liveness against a live process, an exited one, and a RECYCLED pid
  - firing on process exit, on a done-marker, and on timeout
  - the fired watch becomes a due, thread-bound wake carrying the resume prompt
  - progress parsing (1 group = percent, 2 groups = cur/total) and its
    degradation to elapsed-only on a bad regex
  - the heartbeat posts once then EDITS, and disarms on fire
  - one watch per thread; arming a watch retires a stale timer

Run: python scripts/test_watch.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot import config
from bot.engine import watches
from bot.store.state import StateStore

_failures: list[str] = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  ok:   {name}")
    else:
        _failures.append(f"{name}: want={want!r} got={got!r}")
        print(f"  FAIL: {name}: want={want!r} got={got!r}")


def ok(name: str, cond: bool) -> None:
    check(name, bool(cond), True)


class FakeMessenger:
    """Records posts and edits so the heartbeat can be asserted on."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []       # (channel, text)
        self.edits: list[tuple[str, str, str]] = []  # (channel, msg_id, text)
        self.last_buttons = "unset"
        self._n = 0

    async def send_text(self, channel_id, text, buttons=None, silent=False):
        self._n += 1
        self.posts.append((channel_id, text))
        self.last_buttons = buttons
        return f"msg-{self._n}"

    async def edit_text(self, channel_id, msg_id, text, buttons=None):
        self.edits.append((channel_id, msg_id, text or ""))
        self.last_buttons = buttons


def new_store(tmp: Path) -> StateStore:
    return StateStore(tmp / "state.json", tmp / "results")


def arm(store, tmp, **kw):
    """Build + store a watch with sane test defaults."""
    data = {
        "prompt": kw.pop("prompt", "Read the log and report."),
        "pid": kw.pop("pid", None),
        "done": kw.pop("done", ""),
        "log": kw.pop("log", ""),
        "progress": kw.pop("progress", ""),
        "label": kw.pop("label", "test job"),
        "timeout": kw.pop("timeout", None),
        "every": kw.pop("every", None),
    }
    w = watches.build_watch(
        data, channel_id=kw.pop("channel_id", "chan-1"),
        repo_path=str(tmp), now=kw.pop("now", None),
    )
    return store.add_watch(w)


# ---------------------------------------------------------------- parsing ---
print("Directive parsing")

d = watches.parse_watch_directive(
    '[BOT_CMD: /watch pid=4242 log="run.log" progress="step (\\d+)/(\\d+)" '
    'label="model fit" timeout=6h]\n~~~watch\nRead the log.\n~~~\n'
)
ok("parses a full directive", d is not None)
check("  pid", d["pid"], 4242)
check("  log", d["log"], "run.log")
check("  progress", d["progress"], "step (\\d+)/(\\d+)")
check("  label", d["label"], "model fit")
check("  prompt", d["prompt"], "Read the log.")

check(
    "done= alone is a valid trigger",
    (watches.parse_watch_directive(
        '[BOT_CMD: /watch done="=== finished ==="]\n~~~watch\nGo.\n~~~\n'
    ) or {}).get("done"),
    "=== finished ===",
)
check(
    "no trigger (no pid, no done) is refused",
    watches.parse_watch_directive(
        '[BOT_CMD: /watch label="nothing"]\n~~~watch\nGo.\n~~~\n'
    ),
    None,
)
check(
    "no ~~~watch body is refused",
    watches.parse_watch_directive("[BOT_CMD: /watch pid=1]"),
    None,
)
# The guards that stop this feature from arming itself when it is discussed.
check(
    "quoted line is an example, not a request",
    watches.parse_watch_directive(
        "> [BOT_CMD: /watch pid=1]\n~~~watch\nGo.\n~~~\n"
    ),
    None,
)
check(
    "inside a ``` fence is an example",
    watches.parse_watch_directive(
        "```\n[BOT_CMD: /watch pid=1]\n~~~watch\nGo.\n~~~\n"
    ),
    None,
)
check(
    "inline backticks are an example",
    watches.parse_watch_directive(
        "use `[BOT_CMD: /watch pid=1]` like so\n~~~watch\nGo.\n~~~\n"
    ),
    None,
)
# An example shown ABOVE a real directive must not shadow it.
d = watches.parse_watch_directive(
    "> [BOT_CMD: /watch pid=1]\n\n"
    "[BOT_CMD: /watch pid=99]\n~~~watch\nReal one.\n~~~\n"
)
check("a real directive after an example still wins", (d or {}).get("pid"), 99)

print()
print("Duration parsing")
check("6h", watches.parse_duration("6h", 0), 21600)
check("90m", watches.parse_duration("90m", 0), 5400)
check("45s", watches.parse_duration("45s", 0), 45)
check("bare number is seconds", watches.parse_duration("300", 0), 300)
check("2d", watches.parse_duration("2d", 0), 172800)
check("garbage falls back", watches.parse_duration("soonish", 777), 777)
check("None falls back", watches.parse_duration(None, 777), 777)


# ------------------------------------------------------------- liveness ---
print()
print("Process liveness (real processes, no mocks)")

proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
start_token = watches.read_pid_start(proc.pid)
ok("start token readable for a live process", bool(start_token) or os.name != "posix")
ok("live process reads as alive", watches.process_alive(proc.pid, start_token))
ok(
    "a DIFFERENT start token means the pid was recycled",
    not watches.process_alive(proc.pid, "999999999999"),
)
proc.kill()
proc.wait()
# The parent reaped it, so /proc/<pid> is gone entirely.
ok("killed process reads as finished", not watches.process_alive(proc.pid, start_token))


# ------------------------------------------------------------ progress ---
print()
print("Progress parsing")
tail = "step 10/3000\nstep 1410/3000 loss 0.02\n"
got = watches.parse_progress(tail, r"step (\d+)/(\d+)")
ok("two groups -> fraction", got is not None and abs(got[0] - 1410 / 3000) < 1e-9)
check("two groups -> detail", got[1] if got else None, "1410/3000")
got = watches.parse_progress("progress: 73%\n", r"progress: (\d+)%")
ok("one group -> percent", got is not None and abs(got[0] - 0.73) < 1e-9)
check("bad regex degrades to None", watches.parse_progress(tail, "step (("), None)
check("no match degrades to None", watches.parse_progress(tail, r"epoch (\d+)"), None)
check("zero total degrades to None", watches.parse_progress("1/0", r"(\d+)/(\d+)"), None)


# ------------------------------------------------------- firing: on exit ---
print()
print("Firing — process exit becomes a due, thread-bound wake")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    store = new_store(tmp)
    msg = FakeMessenger()

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    w = arm(store, tmp, pid=proc.pid, label="sculpt fit", prompt="Extract the mesh.")
    check("armed with a real pid", w.pid, proc.pid)

    fired = asyncio.run(watches.poll_watches(store, msg))
    check("running job does not fire", fired, 0)
    ok("still armed", store.watch_for_channel("chan-1") is not None)
    check("heartbeat posted once", len(msg.posts), 1)
    ok("heartbeat names the job", "sculpt fit" in msg.posts[0][1])
    ok("heartbeat carries a Stop button", bool(msg.last_buttons))

    # Second poll inside the cadence must not post again.
    asyncio.run(watches.poll_watches(store, msg))
    check("no second heartbeat post inside cadence", len(msg.posts), 1)

    proc.kill()
    proc.wait()
    time.sleep(0.2)

    fired = asyncio.run(watches.poll_watches(store, msg))
    check("exited job fires", fired, 1)
    check("watch disarmed after firing", store.watch_for_channel("chan-1"), None)

    wakes = [s for s in store.list_schedules() if s.resume_thread]
    check("exactly one wake left behind", len(wakes), 1)
    sched = wakes[0]
    check("wake is bound to the thread", sched.channel_id, "chan-1")
    check("wake is one-shot", sched.is_recurring, False)
    ok("wake carries the resume prompt", "Extract the mesh." in sched.prompt)
    ok("wake explains why it woke", "finished" in sched.prompt)
    ok(
        "wake is due immediately",
        datetime.fromisoformat(sched.next_run_at)
        <= datetime.now(timezone.utc) + timedelta(seconds=1),
    )
    ok("heartbeat edited to a final line", any("finished after" in e[2] for e in msg.edits))
    check("final edit drops the Stop button", msg.last_buttons, None)


# --------------------------------------------------- firing: done marker ---
print()
print("Firing — done marker in the log")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    store = new_store(tmp)
    log_file = tmp / "run.log"
    log_file.write_text("step 1/10\n", encoding="utf-8")

    arm(store, tmp, done="=== done ===", log="run.log", pid=None)
    check("marker absent -> no fire", asyncio.run(watches.poll_watches(store, None)), 0)

    log_file.write_text("step 10/10\n=== done ===\n", encoding="utf-8")
    check("marker present -> fires", asyncio.run(watches.poll_watches(store, None)), 1)
    wakes = [s for s in store.list_schedules() if s.resume_thread]
    ok("done-marker wake explains itself", "done marker" in wakes[0].prompt)


# ------------------------------------------------------- firing: timeout ---
print()
print("Firing — safety timeout on a job that never ends")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    store = new_store(tmp)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        w = arm(store, tmp, pid=proc.pid, timeout="1h", now=past)
        ok("timeout deadline is in the past", w.timeout_at < datetime.now(timezone.utc).isoformat())

        check("timed-out watch fires", asyncio.run(watches.poll_watches(store, None)), 1)
        sched = [s for s in store.list_schedules() if s.resume_thread][0]
        ok("prompt says it did NOT finish", "did NOT" in sched.prompt)
        ok("prompt still carries the original instruction", "Read the log" in sched.prompt)
        ok("process really was still running", watches.process_alive(proc.pid))
    finally:
        proc.kill()
        proc.wait()

    # A watch armed with an unreadable deadline must not wait forever.
    store2 = new_store(Path(td) / "b")
    w = arm(store2, tmp, pid=None, done="never")
    w.timeout_at = "not-a-date"
    store2.update_watch(w)
    check("unreadable deadline fires rather than hanging",
          asyncio.run(watches.poll_watches(store2, None)), 1)


# ------------------------------------------------- invariants & clamping ---
print()
print("Invariants")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    store = new_store(tmp)

    arm(store, tmp, done="a", label="first")
    arm(store, tmp, done="b", label="second")
    check("one watch per thread", len(store.list_watches()), 1)
    check("the newer one wins", store.watch_for_channel("chan-1").label, "second")

    arm(store, tmp, done="c", label="other thread", channel_id="chan-2")
    check("other threads are untouched", len(store.list_watches()), 2)

    # A leftover timer must not fire mid-job — cancel_wakes is what the arming
    # path calls to retire it.
    store.add_wake(prompt="old timer", channel_id="chan-1",
                   next_run_at=datetime.now(timezone.utc).isoformat())
    check("stale wake cancelled", store.cancel_wakes("chan-1"), 1)
    check("no wakes left for that thread",
          [s for s in store.list_schedules() if s.channel_id == "chan-1"], [])

    ok("has_armed_watch sees an armed thread", watches.has_armed_watch(store, "chan-1"))
    ok("has_armed_watch is false for a bare thread",
       not watches.has_armed_watch(store, "chan-nope"))

    # Clamping: a session asking for a 10-day timeout or a 1s heartbeat.
    w = watches.build_watch(
        {"prompt": "x", "pid": None, "done": "z", "timeout": "10d", "every": "1s"},
        channel_id="chan-3",
    )
    span = (datetime.fromisoformat(w.timeout_at)
            - datetime.fromisoformat(w.armed_at)).total_seconds()
    check("timeout clamped to the 24h ceiling", int(span), config.WATCH_MAX_TIMEOUT_SECS)
    check("heartbeat clamped to the floor", w.every_secs, config.WATCH_MIN_HEARTBEAT_SECS)


# --------------------------------------------------- restart & rendering ---
print()
print("Restart survival and rendering")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    store = new_store(tmp)
    (tmp / "run.log").write_text("step 1410/3000 loss 0.0231\n", encoding="utf-8")
    arm(store, tmp, done="never", log="run.log",
        progress=r"step (\d+)/(\d+)", label="sculpt fit")

    reloaded = new_store(tmp)          # fresh process reading the same file
    w = reloaded.watch_for_channel("chan-1")
    ok("watch survives a restart", w is not None)
    check("  label survives", w.label, "sculpt fit")
    check("  progress pattern survives", w.progress_re, r"step (\d+)/(\d+)")

    body = watches.render_heartbeat(w, datetime.now(timezone.utc))
    ok("heartbeat shows the label", "sculpt fit" in body)
    ok("heartbeat draws a bar", "█" in body and "░" in body)
    ok("heartbeat shows cur/total", "1410/3000" in body)
    ok("heartbeat shows the last log line", "loss 0.0231" in body)
    ok("heartbeat fits a Discord message", len(body) < 2000)

    # No progress pattern -> still useful, just elapsed + last line.
    w.progress_re = ""
    body = watches.render_heartbeat(w, datetime.now(timezone.utc))
    ok("no pattern -> no bar", "█" not in body)
    ok("no pattern -> still shows the last line", "loss 0.0231" in body)

    # A log that has gone missing must not break the heartbeat.
    w.log_path = "vanished.log"
    body = watches.render_heartbeat(w, datetime.now(timezone.utc))
    ok("missing log still renders", "sculpt fit" in body)


# ------------------------------------------------- end-to-end arming ---
# Everything above tests the watch machinery. This drives the real production
# entry point — lifecycle.check_wake_request, the same function the runner
# calls after every turn — so the harness cannot drift from what actually
# happens when a session emits the directive.
print()
print("End-to-end arming through check_wake_request")

from types import SimpleNamespace

from bot.claude.types import Instance, InstanceStatus, InstanceType
from bot.engine import lifecycle
from bot.platform.base import RequestContext


def run_turn(store, final_text, *, branch=None, channel="chan-1", source="user"):
    msg = FakeMessenger()
    inst = Instance(
        id="q-1", name=None, instance_type=InstanceType.QUERY, prompt="p",
        repo_name="bot", repo_path=str(_ROOT), status=InstanceStatus.COMPLETED,
    )
    inst.branch = branch
    ctx = RequestContext(
        messenger=msg, channel_id=channel, platform="test",
        store=store, runner=SimpleNamespace(),
    )
    ctx.bump_wake_count = lambda: 1
    ctx.reset_wake_count = lambda: None
    ctx.bump_nudge_count = lambda: 1
    ctx.reset_nudge_count = lambda: None
    ctx.source = source
    asyncio.run(lifecycle.check_wake_request(ctx, inst, final_text=final_text))
    return msg


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    store = new_store(tmp)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        text = (
            "Kicked the fit off in the background.\n\n"
            f'[BOT_CMD: /watch pid={proc.pid} log="run.log" label="sculpt fit"]\n'
            "~~~watch\nPull the held-out numbers.\n~~~\n"
        )
        msg = run_turn(store, text)
        w = store.watch_for_channel("chan-1")
        ok("a real turn arms the watch", w is not None)
        check("  pid captured", w.pid, proc.pid)
        ok("  start token captured", bool(w.pid_start))
        check("  resume prompt captured", w.prompt, "Pull the held-out numbers.")
        ok("  user is told it is watching",
           any("Watching" in t for _c, t in msg.posts))

        # A /wake in a LATER turn retires the watch (the session changed its mind).
        run_turn(store, '[BOT_CMD: /wake delay=60]\n~~~wake\nCheck again.\n~~~\n')
        check("a later /wake disarms the watch", store.watch_for_channel("chan-1"), None)
        check("  and leaves a timer instead",
              len([s for s in store.list_schedules() if s.resume_thread]), 1)

        # /watch outranks /wake when a turn emits both.
        store2 = new_store(tmp / "c")
        both = (
            f'[BOT_CMD: /watch pid={proc.pid} label="the job"]\n~~~watch\nGo.\n~~~\n'
            "[BOT_CMD: /wake delay=300]\n~~~wake\nGuess.\n~~~\n"
        )
        run_turn(store2, both)
        ok("/watch outranks /wake in the same turn",
           store2.watch_for_channel("chan-1") is not None)
        check("  no timer armed",
              [s for s in store2.list_schedules() if s.resume_thread], [])

        # A worktree build cannot watch — its directory may be merged away.
        store3 = new_store(tmp / "d")
        msg = run_turn(
            store3,
            f'[BOT_CMD: /watch pid={proc.pid} label="x"]\n~~~watch\nGo.\n~~~\n',
            branch="claude-bot/t-1",
        )
        check("build session refused", store3.watch_for_channel("chan-1"), None)
        ok("  and told why", any("worktree" in t for _c, t in msg.posts))
    finally:
        proc.kill()
        proc.wait()

    # A directive whose job already exited arms anyway and says so — the poller
    # fires it on the next tick rather than the session waiting on nothing.
    store4 = new_store(tmp / "e")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    msg = run_turn(
        store4,
        f'[BOT_CMD: /watch pid={dead.pid} label="already done"]\n~~~watch\nGo.\n~~~\n',
    )
    ok("already-finished job still arms", store4.watch_for_channel("chan-1") is not None)
    ok("  and the notice says so",
       any("already looks finished" in t for _c, t in msg.posts))
    check("  next poll fires it", asyncio.run(watches.poll_watches(store4, None)), 1)


# ------------------------------------------------------------- summary ---
print()
if _failures:
    print(f"FAILED ({len(_failures)}):")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("All cases passed.")
sys.exit(0)
