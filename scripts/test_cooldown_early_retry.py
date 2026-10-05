"""Regression test: a parked cooldown retry fires when an account comes back.

Background (2026-09-17). One account was org-disabled and the other was on a
weekly limit whose reset was four days out, so every incoming message was
refused at spawn and parked with `cooldown_retry_at` = the weekly reset. The
disabled account was re-enabled and put back in rotation by hand minutes
later, and it ran fine from that moment on. Nothing re-armed the parked
instances: eighteen sessions across four repos sat waiting on a wall clock
for a limit that no longer blocked anything, and the only way out was typing
/retry eighteen times.

The stamp was never a deadline. `_refusal_retry_plan` writes the earliest live
cooldown into it as a *prediction* of when something would be free, so the
moment an account is free the prediction is stale.

Locks four things:
  1. cooldown_retry_is_due fires on availability regardless of the stamp,
     and still honours the stamp when nothing is free.
  2. It cannot thrash a single-account fleet: while that account's own
     cooldown is live, `has_spawnable_account()` is False, so the parked
     retry waits exactly as it did before.
  3. has_spawnable_account is the SAME `_pick_account()` question the
     refuse-to-spawn branch asks, so the loop cannot fire retries into a
     refusal or leave them parked while the fleet runs.
  4. The cooldown loop in app.py asks it once per pass and consults it in
     the due-check, rather than trusting the timestamp alone.

Run: python scripts/test_cooldown_early_retry.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import re
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot import config
from bot.claude.runner import ClaudeRunner
from bot.engine import lifecycle

_failures: list[str] = []


def _check(label: str, cond: bool) -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


_now = datetime.now(timezone.utc)
_four_days_out = (_now + timedelta(days=4)).isoformat()
_an_hour_ago = (_now - timedelta(hours=1)).isoformat()


# ------------------------------------------------------- 1. the due predicate

print("cooldown_retry_is_due — availability fires, the stamp is the fallback")

_check(
    "a stamp four days out fires anyway once an account is free "
    "(the 2026-09-17 incident)",
    lifecycle.cooldown_retry_is_due(_four_days_out, _now, accounts_free=True),
)
_check(
    "a stamp four days out keeps waiting while nothing is free",
    not lifecycle.cooldown_retry_is_due(_four_days_out, _now, accounts_free=False),
)
_check(
    "an elapsed stamp still fires with nothing free (the fallback path)",
    lifecycle.cooldown_retry_is_due(_an_hour_ago, _now, accounts_free=False),
)
_check(
    "no stamp is not a parked retry",
    not lifecycle.cooldown_retry_is_due(None, _now, accounts_free=True),
)
_check(
    "an unparseable stamp fires on availability, never on the clock",
    lifecycle.cooldown_retry_is_due("not-a-date", _now, accounts_free=True)
    and not lifecycle.cooldown_retry_is_due("not-a-date", _now, accounts_free=False),
)


# --------------------------------------------- 2/3. the availability question

print()
print("has_spawnable_account — the same question the spawn refusal asks")

runner = ClaudeRunner.__new__(ClaudeRunner)   # no I/O, no event loop
_asked: list[dict] = []


def _fake_pick(**kwargs):
    _asked.append(kwargs)
    return _fake_pick.answer


_fake_pick.answer = None
runner._pick_account = _fake_pick

_saved_accounts = config.CLAUDE_ACCOUNTS
try:
    config.CLAUDE_ACCOUNTS = ["/home/x/.claude", "/home/x/.claude-alt"]

    _fake_pick.answer = None
    _check(
        "nothing pickable reads as no account free (a single-account fleet "
        "on its own limit waits exactly as before)",
        not runner.has_spawnable_account(),
    )
    _fake_pick.answer = "/home/x/.claude-alt"
    _check(
        "an account back in rotation reads as free",
        runner.has_spawnable_account(),
    )
    _check(
        "asked with no exclusions, like the refusal branch's own pick",
        _asked and all(not k.get("exclude") for k in _asked),
    )

    config.CLAUDE_ACCOUNTS = []
    _fake_pick.answer = None
    _check(
        "a non-failover setup never waits on an account it does not have",
        runner.has_spawnable_account(),
    )
finally:
    config.CLAUDE_ACCOUNTS = _saved_accounts


# --------------------------------------------------- 4. the loop actually asks

print()
print("app.cooldown_loop — the loop consults availability, not just the clock")

_app = open(os.path.join(_ROOT, "bot", "app.py"), encoding="utf-8").read()
_loop = _app[_app.index("async def cooldown_loop"):]
_loop = _loop[:_loop.index("async def autonomy_loop")]

_check(
    "the loop asks has_spawnable_account()",
    "has_spawnable_account()" in _loop,
)
_check(
    "asked once per pass, not once per parked instance",
    len(re.findall(r"has_spawnable_account\(\)", _loop)) == 1
    and _loop.index("has_spawnable_account()") < _loop.index("for inst in all_instances"),
)
_check(
    "the due-check goes through lifecycle.cooldown_retry_is_due",
    "lifecycle.cooldown_retry_is_due(" in _loop,
)
_check(
    "no bare `now >= retry_at` decides it any more",
    "if now >= retry_at" not in _loop,
)


# ---- Summary ----
print()
if _failures:
    print(f"FAILED ({len(_failures)}):")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("All cases passed.")
sys.exit(0)
