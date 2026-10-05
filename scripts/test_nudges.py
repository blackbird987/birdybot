"""Regression test for declared nudges (bot/engine/nudges.py).

A nudge is the only thing the bot sends to a real person without being asked
first, on a rhythm, for months. That makes its failure modes unusually quiet:
nothing throws, nobody complains, the messages simply stop, and "he isn't
replying" looks identical to "we stopped asking". So the properties locked
down here are the ones whose breakage is invisible in production.

  (a) Reconciliation converges. Running it three times must leave three rows,
      not nine. It runs on every boot, and a bot that restarts often would
      otherwise accumulate duplicate nudges and message somebody five times an
      evening.
  (b) It does not drift. next_occurrence is anchored to a wall clock in a real
      timezone, so a 17:30 nudge is still 17:30 after DST ends. Stepping a UTC
      instant by 86400s instead would be an hour wrong exactly in the week the
      October check-in lands.
  (c) It does not delay. Re-anchoring on boot must never push a pending fire
      later, or a bot restarted daily would postpone a daily nudge forever.
  (d) A one-shot self-wake in the same thread must not eat the nudge row. Both
      are thread-bound wakes; add_wake's supersede sweep exists to stop pollers
      accumulating and would happily delete a nudge as collateral.
  (e) Downtime collapses to one message, not a burst. Coming back after three
      days down must send one nudge, not three at a stranger's expense.
  (f) A broken config leaves the live nudges running rather than clearing them.

Calls the real production functions so the test can't drift.

Run: python scripts/test_nudges.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.engine.nudges import (  # noqa: E402
    NudgeConfigError,
    load,
    next_fire,
    next_occurrence,
    reconcile,
)
from bot.scheduler import _step_from  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

AMS = ZoneInfo("Europe/Amsterdam")
UTC = timezone.utc

_failures: list[str] = []


def check(name: str, got, want) -> None:
    if got == want:
        print(f"  pass  {name}")
    else:
        print(f"  FAIL  {name}\n          got  {got!r}\n          want {want!r}")
        _failures.append(name)


def check_true(name: str, cond, detail: str = "") -> None:
    check(name + (f" ({detail})" if detail and not cond else ""), bool(cond), True)


def local(dt: datetime) -> str:
    return dt.astimezone(AMS).strftime("%a %H:%M %Z")


def _cfg(tmp: Path, nudges: list[dict]) -> Path:
    p = tmp / "nudges.json"
    p.write_text(json.dumps({"nudges": nudges}), encoding="utf-8")
    return p


def _store(tmp: Path) -> StateStore:
    return StateStore(tmp / "state.json", tmp / "results")


DAILY = {
    "label": "daily", "repo": "", "thread_id": "111",
    "at": "17:30", "tz": "Europe/Amsterdam",
    "days": ["mon", "tue", "wed", "thu", "fri"], "every_days": 1,
    "prompt": "what did you work on today",
}
WEEKLY = {
    "label": "weekly", "repo": "", "thread_id": "111",
    "at": "16:00", "tz": "Europe/Amsterdam",
    "days": ["thu"], "every_days": 7,
    "prompt": "what did you second-guess this week",
}


def test_wall_clock_anchor() -> None:
    print("\n(b) time stays where a person expects it")
    # DST ends in the Netherlands on 2026-10-25. A UTC-stepped schedule would
    # read 16:30 local on the Monday after; a wall-clock one still reads 17:30.
    before = datetime(2026, 10, 23, 10, 0, tzinfo=UTC)   # Fri, CEST
    after = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)    # Mon, CET
    check("17:30 before DST ends",
          local(next_occurrence("17:30", DAILY["days"], "Europe/Amsterdam", before)),
          "Fri 17:30 CEST")
    check("17:30 after DST ends",
          local(next_occurrence("17:30", DAILY["days"], "Europe/Amsterdam", after)),
          "Mon 17:30 CET")

    # Weekend is skipped, not silently shifted into Saturday.
    sat = datetime(2026, 9, 12, 10, 0, tzinfo=UTC)
    check("weekday-only nudge skips the weekend",
          local(next_occurrence("17:30", DAILY["days"], "Europe/Amsterdam", sat)),
          "Mon 17:30 CEST")

    # Strictly future: asking AT the fire time must give tomorrow, never now,
    # or the scheduler would re-fire the same nudge on its next 30s tick.
    at_fire = datetime(2026, 9, 9, 15, 30, tzinfo=UTC)   # == 17:30 CEST
    nxt = next_occurrence("17:30", None, "Europe/Amsterdam", at_fire)
    check_true("exactly at fire time yields the NEXT day", nxt > at_fire)
    check("and it is the next day", nxt.astimezone(AMS).day, 10)

    # A weekly nudge crossing a year boundary still lands on its weekday.
    ny = datetime(2026, 12, 31, 17, 0, tzinfo=UTC)
    check("weekly crosses the year end onto its weekday",
          next_occurrence("16:00", ["thu"], "Europe/Amsterdam", ny).astimezone(AMS).strftime("%a"),
          "Thu")


def test_convergence_and_no_delay() -> None:
    print("\n(a)+(c) reconcile converges, and never postpones a pending fire")
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = _cfg(tmp, [DAILY, WEEKLY])
        st = _store(tmp)
        now = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)  # Wed morning

        first = reconcile(st, cfg, now=now)
        check("first pass creates both", first["created"], 2)
        armed = {s.label: s.next_run_at for s in st.list_schedules() if s.label}

        for i in (2, 3):
            again = reconcile(st, cfg, now=now)
            check(f"pass {i} creates nothing", again["created"], 0)
        rows = [s for s in st.list_schedules() if s.label]
        check("three passes leave two rows", len(rows), 2)

        # (c) A restart four hours later, still before the 17:30 fire, must
        # leave the fire time alone rather than pushing it to tomorrow.
        later = now + timedelta(hours=4)
        reconcile(st, cfg, now=later)
        still = {s.label: s.next_run_at for s in st.list_schedules() if s.label}
        check("restart before the fire does not postpone it", still, armed)

        # Shape of what got written: recurring, thread-bound, right cadence.
        by_label = {s.label: s for s in st.list_schedules() if s.label}
        check("daily is recurring", by_label["daily"].is_recurring, True)
        check("daily is thread-bound", by_label["daily"].resume_thread, True)
        check("daily interval is 1 day", by_label["daily"].interval_secs, 86400)
        check("weekly interval is 7 days", by_label["weekly"].interval_secs, 604800)
        check("daily targets the thread", by_label["daily"].channel_id, "111")

        # Editing the prompt updates in place; it must not orphan a second row
        # firing the old wording at somebody forever.
        edited = dict(DAILY, prompt="different question")
        reconcile(st, _cfg(tmp, [edited, WEEKLY]), now=now)
        rows = [s for s in st.list_schedules() if s.label]
        check("editing a prompt does not duplicate the row", len(rows), 2)
        check("editing a prompt updates it in place",
              {s.label: s.prompt for s in rows}["daily"], "different question")

        # Deleting an entry deletes the live nudge, or it fires forever.
        reconcile(st, _cfg(tmp, [WEEKLY]), now=now)
        check("removing an entry removes the nudge",
              sorted(s.label for s in st.list_schedules() if s.label), ["weekly"])

        # enabled:false parks one without deleting the wording from the file.
        reconcile(st, _cfg(tmp, [dict(DAILY, enabled=False), WEEKLY]), now=now)
        check("enabled:false keeps it out of the live set",
              sorted(s.label for s in st.list_schedules() if s.label), ["weekly"])
    finally:
        shutil.rmtree(tmp)


def test_survives_neighbours() -> None:
    print("\n(d) a nudge survives its neighbours in state")
    tmp = Path(tempfile.mkdtemp())
    try:
        cfg = _cfg(tmp, [DAILY])
        st = _store(tmp)
        now = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
        reconcile(st, cfg, now=now)

        # A plain user cron must be untouched by reconciliation.
        cron = st.add_schedule("unrelated job", interval_secs=3600)
        reconcile(st, cfg, now=now)
        check_true("a user cron survives reconcile",
                   st.get_schedule(cron.id) is not None)

        # The dangerous one: an ordinary self-wake in the SAME thread. Its
        # supersede sweep clears pending pollers and must not take the nudge.
        st.add_wake("poll a build", "111", "2026-09-09T20:00:00+00:00")
        st.add_wake("poll it again", "111", "2026-09-09T21:00:00+00:00")
        check("nudge survives two self-wakes in its own thread",
              sorted(s.label for s in st.list_schedules() if s.label), ["daily"])
        oneshots = [s for s in st.list_schedules()
                    if s.resume_thread and not s.is_recurring and s.channel_id == "111"]
        check("but the one-shot wakes still supersede each other", len(oneshots), 1)

        # And the nudge survives a save/load round trip (label is serialized).
        st.save()
        st2 = StateStore(tmp / "state.json", tmp / "results")
        check("nudge survives a state round trip",
              sorted(s.label for s in st2.list_schedules() if s.label), ["daily"])
    finally:
        shutil.rmtree(tmp)


def test_rearm_after_downtime() -> None:
    print("\n(e) downtime collapses to one message")
    due = "2026-09-09T15:30:00+00:00"      # 17:30 CEST
    # Fired 24 seconds late by the 30s tick: the next fire is still 15:30, not
    # 15:30:24, or a daily nudge drifts a full hour over three months.
    fired_late = datetime(2026, 9, 9, 15, 30, 24, tzinfo=UTC)
    check("re-arm keeps the anchor, not the fire time",
          _step_from(due, 86400, fired_late).isoformat(), "2026-09-10T15:30:00+00:00")
    # Three days of downtime: one fire, not three.
    back_up = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    check("three days down yields the next single fire",
          _step_from(due, 86400, back_up).isoformat(), "2026-09-12T15:30:00+00:00")
    # Garbage or missing due times fall back rather than crashing the tick.
    check_true("garbage due time falls back",
               _step_from("not-a-date", 86400, back_up) > back_up)
    check_true("missing due time falls back",
               _step_from(None, 86400, back_up) > back_up)
    check("naive due time is read as UTC",
          _step_from("2026-09-09T15:30:00", 86400, back_up).isoformat(),
          "2026-09-12T15:30:00+00:00")


def test_rearm_respects_weekdays() -> None:
    print("\n(e2) re-arming follows the declared weekdays, not the interval")
    tmp = Path(tempfile.mkdtemp())
    try:
        path = _cfg(tmp, [
            {"label": "weekday", "thread_id": "1", "prompt": "p", "at": "17:30",
             "tz": "Europe/Amsterdam", "days": ["mon", "tue", "wed", "thu", "fri"]},
            {"label": "thursday", "thread_id": "1", "prompt": "p", "at": "16:00",
             "tz": "Europe/Amsterdam", "days": ["thu"], "every_days": 7},
            {"label": "every3", "thread_id": "1", "prompt": "p", "at": "09:00",
             "tz": "Europe/Amsterdam", "every_days": 3},
        ])
        # The live incident: fired Friday 11 Sept 17:30:03 CEST, then Saturday.
        friday_fire = datetime(2026, 9, 11, 15, 30, 3, tzinfo=UTC)
        nxt = next_fire(path, "weekday", friday_fire)
        check("friday fire re-arms to monday", local(nxt), "Mon 17:30 CEST")
        thu_fire = datetime(2026, 10, 22, 14, 0, 5, tzinfo=UTC)
        check("thursday before the DST change stays at 16:00 local",
              local(next_fire(path, "thursday", thu_fire)), "Thu 16:00 CET")
        check("every-3-days with no weekdays defers to the interval",
              next_fire(path, "every3", friday_fire), None)
        check("unknown label defers to the interval",
              next_fire(path, "gone", friday_fire), None)
        (tmp / "broken.json").write_text("{nope")
        check("broken config defers to the interval",
              next_fire(tmp / "broken.json", "weekday", friday_fire), None)
    finally:
        shutil.rmtree(tmp)


def test_bad_config() -> None:
    print("\n(f) a bad config is loud, and leaves the live nudges alone")
    tmp = Path(tempfile.mkdtemp())
    try:
        for name, entry in [
            ("missing thread_id", {"label": "a", "prompt": "p", "at": "9:00"}),
            ("missing prompt", {"label": "a", "thread_id": "1", "at": "9:00"}),
            ("no label", {"thread_id": "1", "prompt": "p", "at": "9:00"}),
            ("bad time", {"label": "a", "thread_id": "1", "prompt": "p", "at": "25:99"}),
            ("bad weekday", {"label": "a", "thread_id": "1", "prompt": "p",
                             "at": "9:00", "days": ["funday"]}),
            ("bad timezone", {"label": "a", "thread_id": "1", "prompt": "p",
                              "at": "9:00", "tz": "Mars/Olympus"}),
            ("zero interval", {"label": "a", "thread_id": "1", "prompt": "p",
                               "at": "9:00", "every_days": 0}),
        ]:
            try:
                load(_cfg(tmp, [entry]))
                check(f"rejects {name}", "accepted", "rejected")
            except NudgeConfigError:
                check(f"rejects {name}", "rejected", "rejected")

        try:
            load(_cfg(tmp, [DAILY, dict(WEEKLY, label="daily")]))
            check("rejects a duplicate label", "accepted", "rejected")
        except NudgeConfigError:
            check("rejects a duplicate label", "rejected", "rejected")

        check("a missing file is not an error", load(tmp / "nope.json"), [])

        # The important half: a config that breaks AFTER nudges are live must
        # leave them firing, not clear them. Stopping silently is the failure
        # this module exists to prevent.
        st = _store(tmp)
        now = datetime(2026, 9, 9, 8, 0, tzinfo=UTC)
        good = _cfg(tmp, [DAILY, WEEKLY])
        reconcile(st, good, now=now)
        broken = tmp / "broken.json"
        broken.write_text("{ not json at all", encoding="utf-8")
        out = reconcile(st, broken, now=now)
        check_true("a broken config reports an error", "error" in out)
        check("a broken config leaves live nudges running",
              sorted(s.label for s in st.list_schedules() if s.label),
              ["daily", "weekly"])
    finally:
        shutil.rmtree(tmp)


def test_shipped_config() -> None:
    print("\nthe config actually shipped in this repo")
    from bot import config as botconfig
    entries = load(botconfig.NUDGES_FILE)
    check_true("config/nudges.json parses", isinstance(entries, list))
    for e in entries:
        check_true(f"{e['label']} has a thread", bool(e["thread_id"]))
        check_true(f"{e['label']} fires in the future",
                   next_occurrence(e["at"], e["days"], e["tz"]) > datetime.now(UTC))


def main() -> int:
    print("Declared nudges regression test")
    test_wall_clock_anchor()
    test_convergence_and_no_delay()
    test_survives_neighbours()
    test_rearm_after_downtime()
    test_rearm_respects_weekdays()
    test_bad_config()
    test_shipped_config()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): " + ", ".join(_failures))
        return 1
    print("All nudge checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
