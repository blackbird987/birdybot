"""Regression test for the self-wake broken-claim safety net.

Background: a turn only continues after it ends via a self-wake *timer* — there
is no completion event for an external probe. If a turn ends CLAIMING it queued
a self-wake ("Self-wake queued (~4 min)") but no directive parsed and no file
was written, check_wake_request auto-arms one fallback re-check instead of
silently stalling. That detection is config.WAKE_CLAIM_RE, scanned after verify
blocks are stripped.

A broader heuristic (WAKE_PROMISE_RE / looks_like_watch_promise — watch-verb
near job-noun) used to auto-arm too, but it kept false-firing on prose that
merely DISCUSSED builds/backtests/monitoring, arming phantom 3-min wakes. It
was removed; the "must NOT arm" section below locks that removal in: none of
those phrasings may trip the claim detector either.

Calls the real production predicate (``lifecycle.claims_self_wake``) so the
test can't drift from what ``check_wake_request`` actually evaluates.

Run: python scripts/test_wake_promise.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.engine.lifecycle import claims_self_wake

_failures: list[str] = []


def _check(text: str, expected: bool) -> None:
    got = claims_self_wake(text)
    if got == expected:
        print(f"  ok:   want={expected!s:5} :: {text!r}")
    else:
        _failures.append(f"want={expected} got={got} :: {text!r}")
        print(f"  FAIL: want={expected!s:5} got={got!s:5} :: {text!r}")


# ---- Turn ASSERTS it armed a self-wake but scheduled nothing → must arm ----
print("Claims of a queued/scheduled self-wake must arm")
_check("Self-wake queued (~4 min); I'll report the verdict.", True)
_check("I scheduled a self-wake for 5 min", True)
_check("Wrote the wake file, will re-check after the deploy", True)
# The real q-11865 result text that motivated the claim detector.
# Don't echo its (unicode-laden) content to a cp1252 console — just assert.
try:
    _real = open(
        os.path.join(_ROOT, "data", "results", "q-11865.md"), encoding="utf-8"
    ).read()
    if claims_self_wake(_real):
        print("  ok:   want=True  :: <real q-11865.md>")
    else:
        _failures.append("want=True got=False :: <real q-11865.md>")
        print("  FAIL: want=True  got=False :: <real q-11865.md>")
except OSError:
    print("  skip: data/results/q-11865.md not present")

# ---- Meta-explanations / completions → must NOT arm ----
print("Meta-explanation / completions must NOT arm")
_check("self-wake lets you continue after a deploy finishes", False)
_check("All done - tests pass, nothing else to do.", False)
_check("", False)

# ---- Removed promise heuristic: watch-verb + job-noun prose must NOT arm ----
# These all tripped the deleted WAKE_PROMISE_RE and armed phantom re-checks.
# Locks in the removal: ordinary prose about jobs never schedules a wake.
print("Watch-promise prose (old WAKE_PROMISE_RE) must NOT arm")
_check("I will monitor the deploy and report back", False)
_check("I'll keep watching the build and let you know", False)
_check("I'll poll the CI pipeline until it finishes", False)
_check("I can run a P&L backtest audit against your monitoring dashboard", False)
_check("I'll wait for your reply", False)

# ---- Verify-board stripping: a verify item mentioning a wake claim ----
print("Verify-board item must not false-trigger")
_vb = (
    "Done, nothing pending.\n\n"
    "```verify-board\n"
    "- Self-wake queued: end a turn asserting this w/o a real directive\n"
    "```\n"
)
_check(_vb, False)
# ...but a real claim alongside a verify block still arms.
_check(
    "Self-wake queued for 5 min.\n\n```verify-board\n- unrelated item\n```\n",
    True,
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
