"""Usage pacing: sessions are started at a pace the plan can afford.

Background (2026-10-15). Every five-hour window ran out 9, 8, 4 and then 3
minutes after it opened. The cooldown loop released the whole parked backlog
(about sixteen sessions) in the minute each window reopened, each one resumed
a long conversation whose prompt cache had expired during the wait, and each
re-queued itself when it hit the wall again, so the backlog only grew.

Locks:
  1. pace_limit: session tiers, weekly pace and weekly ceiling, peak hours,
     never below one, no reading means no usage cap, a reading whose window
     has reset counts as zero, disabled means MAX_CONCURRENT.
  2. The runner holds a starting session while the pace limit is reached,
     lets exactly one waiter through per freed slot, abandons on Kill, and
     gives up after USAGE_PACE_MAX_WAIT_SECS.
  3. cooldown_retry_rank: nudges are dropped, typed messages go first, then
     workflow steps, then late child reports; the prefixes it keys on are the
     ones the bot actually writes.
  4. The cooldown loop in app.py releases one retry per pass, behind the
     stagger and the usage slot check, rather than one task per due retry.

Run: python scripts/test_usage_pacing.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import ast
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import config
from bot.claude import usage_pacing
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType


def _inst(id_, prompt="x", origin=InstanceOrigin.DIRECT) -> Instance:
    return Instance(id=id_, name=id_, instance_type=InstanceType.QUERY,
                    status=InstanceStatus.RUNNING, prompt=prompt,
                    repo_name="r", repo_path="/tmp", origin=origin)

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


@dataclass
class Reading:
    session_pct: float | None = None
    session_resets_at: datetime | None = None
    week_pct: float | None = None
    week_resets_at: datetime | None = None
    has_data: bool = True


# A Saturday, so peak hours never apply unless a test asks for them.
SAT = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
# A Wednesday at 08:00 Pacific (15:00 UTC): inside the 5-11 peak.
WED_PEAK = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
WED_OFF = datetime(2026, 10, 14, 22, 0, tzinfo=timezone.utc)


def test_pace_limit() -> None:
    config.USAGE_PACE_ENABLED = True
    config.USAGE_PACE_SESSION_TIERS = "40:3,65:2,85:1"
    config.USAGE_PACE_WEEK_SLACK_PCT = 10
    config.USAGE_PACE_WEEK_AHEAD_CAP = 2
    config.USAGE_PACE_WEEK_HIGH_PCT = 90
    config.USAGE_PACE_PEAK_HOURS_PT = "5-11"
    config.USAGE_PACE_PEAK_CAP = 3
    later = SAT + timedelta(hours=3)
    week_end = SAT + timedelta(days=3)  # 4/7 of the week gone, allowance ~67%

    lim = lambda r, now=SAT: usage_pacing.pace_limit(r, now, 6)  # noqa: E731

    check(lim(None).limit == 6, "no reading must mean no usage cap")
    check(lim(Reading(session_pct=10, session_resets_at=later)).limit == 6,
          "low session usage must not cap")
    check(lim(Reading(session_pct=40, session_resets_at=later)).limit == 3,
          "40% session must cap at 3")
    check(lim(Reading(session_pct=70, session_resets_at=later)).limit == 2,
          "70% session must cap at 2")
    d = lim(Reading(session_pct=99, session_resets_at=later))
    check(d.limit == 1, "99% session must cap at 1")
    check("99%" in d.reason, f"the reason must name the figure, got {d.reason!r}")
    check(lim(Reading(session_pct=99, session_resets_at=SAT - timedelta(minutes=1))).limit == 6,
          "a session figure from a window that has already reset must count as zero")

    check(lim(Reading(week_pct=50, week_resets_at=week_end)).limit == 6,
          "weekly usage on pace must not cap")
    d = lim(Reading(week_pct=80, week_resets_at=week_end))
    check(d.limit == 2, f"weekly usage ahead of pace must cap at 2, got {d.limit}")
    check("ahead" in d.reason, f"ahead-of-pace reason missing, got {d.reason!r}")
    check(lim(Reading(week_pct=95, week_resets_at=week_end)).limit == 1,
          "weekly usage at 95% must cap at 1")

    check(usage_pacing.is_peak(WED_PEAK), "Wednesday 08:00 Pacific must be peak")
    check(not usage_pacing.is_peak(WED_OFF), "Wednesday 15:00 Pacific must not be peak")
    check(not usage_pacing.is_peak(SAT), "Saturday must never be peak")
    check(lim(None, WED_PEAK).limit == 3, "peak hours must cap even with no reading")
    check(lim(None, WED_OFF).limit == 6, "off-peak with no reading must not cap")

    check(usage_pacing.pace_limit(Reading(session_pct=99), SAT, 0).limit == 1,
          "the cap must never drop below one")
    config.USAGE_PACE_SESSION_TIERS = "50:0"
    check(lim(Reading(session_pct=99)).limit == 1,
          "a tier configured to zero must still leave one session running")
    config.USAGE_PACE_SESSION_TIERS = "garbage,40:3"
    check(lim(Reading(session_pct=45)).limit == 3, "a bad tier pair must be skipped, not fatal")
    config.USAGE_PACE_SESSION_TIERS = "40:3,65:2,85:1"

    config.USAGE_PACE_ENABLED = False
    check(lim(Reading(session_pct=99), WED_PEAK).limit == 6,
          "disabled pacing must return MAX_CONCURRENT")
    config.USAGE_PACE_ENABLED = True


def _runner(reading):
    from bot.claude.runner import ClaudeRunner

    runner = ClaudeRunner(store=None)

    async def reader(_account):
        return reading[0]

    runner.set_usage_reader(reader)
    return runner


async def test_runner_hold() -> None:
    saved = (config.CLAUDE_ACCOUNTS, config.MAX_CONCURRENT,
             config.USAGE_PACE_POLL_SECS, config.USAGE_PACE_MAX_WAIT_SECS,
             config.USAGE_PACE_PEAK_HOURS_PT)
    config.CLAUDE_ACCOUNTS = []
    config.MAX_CONCURRENT = 6
    config.USAGE_PACE_POLL_SECS = 0.01
    config.USAGE_PACE_PEAK_HOURS_PT = ""  # wall-clock independent
    try:
        reading = [Reading(session_pct=99)]
        runner = _runner(reading)
        notes: list[str] = []

        async def progress(head, body):
            notes.append(head)

        inst = _inst("t-1")
        await asyncio.wait_for(runner._await_usage_headroom(inst, progress), 1)
        check(not notes, "an empty house must not hold anyone")

        # One running at the 1-cap: two waiters, one slot frees, one goes.
        runner._paced_running = 1
        released: list[str] = []

        async def waiter(name):
            i = _inst(name)
            await runner._await_usage_headroom(i, progress)
            runner._paced_running += 1  # what run() does, with no await between
            released.append(name)

        tasks = [asyncio.create_task(waiter("a")), asyncio.create_task(waiter("b"))]
        await asyncio.sleep(0.05)
        check(not released, "nobody may pass while the 1-cap is full")
        check("Waiting for usage headroom" in notes, "a held session must say why")
        runner._paced_running -= 1
        await asyncio.sleep(0.05)
        check(len(released) == 1, f"one freed slot must release exactly one, got {released}")
        reading[0] = None  # figures gone: no usage cap at all
        await asyncio.wait_for(asyncio.gather(*tasks), 1)
        check(len(released) == 2, "losing the reading must release the rest")

        # Kill while held.
        reading[0] = Reading(session_pct=99)
        runner._paced_running = 5
        k = _inst("t-kill")
        task = asyncio.create_task(runner._await_usage_headroom(k, None))
        await asyncio.sleep(0.03)
        runner._intentional_kills.add("t-kill")
        await asyncio.wait_for(task, 1)

        # Bounded wait.
        config.USAGE_PACE_MAX_WAIT_SECS = 0.05
        t = _inst("t-timeout")
        await asyncio.wait_for(runner._await_usage_headroom(t, None), 1)

        # usage_slot_free mirrors the hold.
        config.USAGE_PACE_MAX_WAIT_SECS = 3600
        runner._paced_running = 0
        check(await runner.usage_slot_free(), "an empty house must have a free slot")
        runner._paced_running = 1
        check(not await runner.usage_slot_free(), "a full 1-cap must report no slot")

        # A reader that raises is no reading, not a crash.
        async def boom(_a):
            raise RuntimeError("endpoint down")

        runner.set_usage_reader(boom)
        check((await runner.usage_pace()).limit == 6, "a failing reader must fail open")

        # With accounts, the reading is for the account the spawn would use:
        # a resume asks for its session's account, like _run_impl does.
        config.CLAUDE_ACCOUNTS = ["/acct/a", "/acct/b"]
        picks: list[dict] = []
        asked: list[str | None] = []

        def pick(**kw):
            picks.append(kw)
            return kw.get("prefer") or "/acct/a"

        async def record(account):
            asked.append(account)
            return None

        runner._pick_account = pick
        runner.set_usage_reader(record)
        resume = _inst("t-resume")
        resume.session_id = "s-1"
        resume.session_account = "/acct/b"
        await runner.usage_pace(resume)
        check(asked == ["/acct/b"], f"a resume must be read on its session's account, got {asked}")
        check(picks and picks[-1].get("prefer") == "/acct/b"
              and "avoid_model_cooldown" in picks[-1],
              f"the pacing pick must mirror the spawn's, got {picks}")
        await runner.usage_pace()
        check(asked[-1] == "/acct/a", "with no instance it is plain rotation order")
    except asyncio.TimeoutError:
        failures.append("a usage hold did not end when it should have")
    finally:
        (config.CLAUDE_ACCOUNTS, config.MAX_CONCURRENT,
         config.USAGE_PACE_POLL_SECS, config.USAGE_PACE_MAX_WAIT_SECS,
         config.USAGE_PACE_PEAK_HOURS_PT) = saved


def test_run_counts_slots() -> None:
    """run() takes the slot right after the hold and gives it back in finally."""
    src = Path(__file__).resolve().parents[1] / "bot" / "claude" / "runner" / "core.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    run = next(n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")
    with_ = next(n for n in ast.walk(run) if isinstance(n, ast.AsyncWith))
    body = with_.body
    hold = body[0]
    check(isinstance(hold, ast.Expr) and isinstance(hold.value, ast.Await)
          and getattr(hold.value.value.func, "attr", "") == "_await_usage_headroom",
          "run() must start with the usage hold inside the semaphore")
    incr = body[1]
    check(isinstance(incr, ast.AugAssign) and getattr(incr.target, "attr", "") == "_paced_running",
          "run() must count the slot on the statement right after the hold")
    tries = [n for n in body if isinstance(n, ast.Try)]
    check(any(any(isinstance(f, ast.AugAssign) and getattr(f.target, "attr", "") == "_paced_running"
                  for f in t.finalbody) for t in tries),
          "run() must give the slot back in a finally")


def test_rank() -> None:
    from bot.engine import lifecycle

    def inst(prompt, origin=InstanceOrigin.DIRECT):
        return _inst("q-1", prompt, origin)

    check(lifecycle.cooldown_retry_rank(inst(lifecycle._NUDGE_PROMPT)) is None,
          "the dead-end nudge must be dropped")
    check(lifecycle.cooldown_retry_rank(inst(lifecycle._PROMISE_NUDGE_PROMPT)) is None,
          "the promise nudge must be dropped")
    check(lifecycle.cooldown_retry_rank(inst("fix the login page")) == 0,
          "a typed message must go first")
    check(lifecycle.cooldown_retry_rank(inst("build it", InstanceOrigin.BUILD)) == 1,
          "a workflow step must come after typed messages")
    check(lifecycle.cooldown_retry_rank(inst('Late report: child "x" finished')) == 2,
          "a late child report must go last")
    check(lifecycle.cooldown_retry_rank(
        inst("[Background context only]\nSYSTEM NUDGE: quoted earlier")) == 0,
          "a typed message that merely quotes a nudge must never be dropped")

    orch = (Path(__file__).resolve().parents[1] / "bot" / "discord" / "orchestrator.py"
            ).read_text(encoding="utf-8")
    check("f'Late report" in orch or 'f"Late report' in orch,
          "the orchestrator no longer writes 'Late report'; update _LATE_REPORT_PREFIX")


def test_loop_staggers() -> None:
    src = (Path(__file__).resolve().parents[1] / "bot" / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    loop = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "cooldown_loop")
    fors = [n for n in ast.walk(loop) if isinstance(n, ast.For)]
    spawns_in_for = [
        c for f in fors for c in ast.walk(f)
        if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "create_task"
    ]
    check(not spawns_in_for,
          "the cooldown loop starts a retry per due instance again; it must "
          "release one per pass behind the stagger")
    text = ast.unparse(loop)
    for needle in ("COOLDOWN_RETRY_STAGGER_SECS", "usage_slot_free", "cooldown_retry_rank"):
        check(needle in text, f"the cooldown loop no longer consults {needle}")


async def test_reader() -> None:
    from bot.engine import plan_usage

    saved = (config.PLAN_USAGE_API_ENABLED,)
    config.PLAN_USAGE_API_ENABLED = False
    try:
        check(await plan_usage.reading_for_pacing(None) is None,
              "plan usage disabled must give pacing no reading")
        check(await plan_usage.reading_for_pacing("/nonexistent") is None,
              "plan usage disabled must give pacing no reading for a named account")
    finally:
        (config.PLAN_USAGE_API_ENABLED,) = saved


def main() -> int:
    test_pace_limit()
    asyncio.run(test_runner_hold())
    test_run_counts_slots()
    test_rank()
    test_loop_staggers()
    asyncio.run(test_reader())
    if failures:
        print("FAIL: usage pacing")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: usage pacing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
