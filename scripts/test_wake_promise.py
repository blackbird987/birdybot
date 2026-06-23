"""Regression test for the self-wake broken-promise heuristic.

Background: a turn only continues after it ends via a self-wake *timer* — there
is no completion event for an external probe. If a turn ends PROMISING to keep
watching a job (deploy/CI/build) but writes no wake file, check_wake_request
auto-arms one fallback re-check instead of silently stalling. That detection is
config.WAKE_PROMISE_RE, scanned after verify blocks are stripped.

This locks the heuristic so a future edit can't (a) start matching human-directed
waits ("wait for your reply", "continue once you confirm"), which would re-invoke
a session that's actually waiting on the user, or (b) stop matching genuine
job-watching promises, which would reopen the silent dead-end. It also guards the
verify-board stripping so a ```verify-board``` item describing "watch a job" can't
false-trigger the fallback.

Calls the real production predicate (``lifecycle.looks_like_watch_promise``) so
the test can't drift from what ``check_wake_request`` actually evaluates.

Run: python scripts/test_wake_promise.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.engine.lifecycle import claims_self_wake, looks_like_watch_promise

_failures: list[str] = []


def _check(text: str, expected: bool) -> None:
    got = looks_like_watch_promise(text)
    if got == expected:
        print(f"  ok:   want={expected!s:5} :: {text!r}")
    else:
        _failures.append(f"want={expected} got={got} :: {text!r}")
        print(f"  FAIL: want={expected!s:5} got={got!s:5} :: {text!r}")


def _check_claim(text: str, expected: bool) -> None:
    got = claims_self_wake(text)
    if got == expected:
        print(f"  ok:   want={expected!s:5} :: {text!r}")
    else:
        _failures.append(f"claim want={expected} got={got} :: {text!r}")
        print(f"  FAIL: want={expected!s:5} got={got!s:5} :: {text!r}")


# ---- Genuine job-watching promises → must arm ----
print("Promises that SHOULD arm a fallback")
_check("I will monitor the deploy and report back", True)
_check("I'll keep watching the build and let you know", True)
_check("I'll poll the CI pipeline until it finishes", True)
_check("I'll check back on the backtest when it completes", True)
_check("I'll wait for the build to finish, then report", True)
_check("I'll keep an eye on the deploy", True)
_check("I'll get notified when CI is done", True)

# ---- Human-directed waits / completions → must NOT arm ----
print("Waits/completions that must NOT arm")
_check("I'll wait for your reply", False)
_check("I'll let you know if anything else comes up", False)
_check("I'll continue once you confirm the deploy plan", False)  # human, not a job
_check("All done - tests pass, nothing else to do.", False)
_check("Ran the tests, everything green. Ship it.", False)
_check("", False)

# ---- Verify-board stripping: prose says done, a verify item mentions a job ----
print("Verify-board item must not false-trigger")
_vb = (
    "Done, nothing pending.\n\n"
    "```verify-board\n"
    "- Self-wake: end a turn promising to watch a job w/o a wake file\n"
    "```\n"
)
_check(_vb, False)
# ...but a real promise alongside a verify block still arms.
_check(
    "I'll keep watching the deploy.\n\n```verify-board\n- unrelated item\n```\n",
    True,
)


# ---- claims_self_wake: turn ASSERTS it armed a self-wake (no file) ----
print("Claims of a queued/scheduled self-wake must arm")
_check_claim("Self-wake queued (~4 min); I'll report the verdict.", True)
_check_claim("I scheduled a self-wake for 5 min", True)
_check_claim("Wrote the wake file, will re-check after the deploy", True)
# The real q-11865 result text that slipped through WAKE_PROMISE_RE.
# Don't echo its (unicode-laden) content to a cp1252 console — just assert.
try:
    _real = open(
        os.path.join(_ROOT, "data", "results", "q-11865.md"), encoding="utf-8"
    ).read()
    if claims_self_wake(_real):
        print("  ok:   want=True  :: <real q-11865.md>")
    else:
        _failures.append("claim want=True got=False :: <real q-11865.md>")
        print("  FAIL: want=True  got=False :: <real q-11865.md>")
except OSError:
    print("  skip: data/results/q-11865.md not present")

print("Meta-explanation / completions must NOT arm a claim")
_check_claim("self-wake lets you continue after a deploy finishes", False)
_check_claim("All done - tests pass, nothing else to do.", False)
_check_claim("", False)


# ---- Summary ----
print()
if _failures:
    print(f"FAILED ({len(_failures)}):")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("All cases passed.")
sys.exit(0)
