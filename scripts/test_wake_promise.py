"""Regression test for the self-wake claim detector (notice-only).

Background: a turn only continues after it ends via a self-wake *timer* — there
is no completion event for an external probe. The only way a turn arms one is
an explicit ``[BOT_CMD: /wake]`` directive parsed from its output. If a turn
instead ASSERTS it armed a self-wake while nothing parsed (e.g. a malformed
directive), check_wake_request sends a notice-only heads-up so the dead-end
isn't silent — it never schedules anything. That detection is
config.WAKE_CLAIM_RE, scanned after verify blocks, code spans, and quoted
phrases are stripped (lifecycle._CLAIM_META_RE).

History locked in below: heuristic wake SCHEDULING is gone. WAKE_PROMISE_RE
(watch-verb near job-noun) armed phantom 3-min wakes off prose that merely
discussed builds/backtests and was deleted; the claim auto-arm then misfired
on a report QUOTING the phrase "self-wake queued/scheduled" and was downgraded
to notice-only with quote/code stripping. The "must NOT match" sections keep
both failure modes dead.

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


# ---- Turn ASSERTS it armed a self-wake but nothing parsed → must notify ----
print("First-person claims of an armed self-wake must match")
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

# ---- Meta-explanations / completions → must NOT match ----
print("Meta-explanation / completions must NOT match")
_check("self-wake lets you continue after a deploy finishes", False)
_check("All done - tests pass, nothing else to do.", False)
_check("", False)

# ---- Quoted / code-span mentions of trigger phrases → must NOT match ----
# The real misfire (2026-07-02, thread 1521927445689794624's sibling): a
# verification report QUOTING the detector's own trigger phrase armed a
# phantom re-check. Quoting is discussion, not a first-person assertion.
print("Quoted/backticked trigger phrases must NOT match")
_check(
    'if a turn literally claims "self-wake queued/scheduled" but no valid '
    "directive parsed, the bot notices",
    False,
)
_check("the log line says `Self-wake queued` when the directive parses", False)
_check(
    "```\nSelf-wake queued (~4 min); I'll report the verdict.\n```\n"
    "That example never fires from a code block.",
    False,
)
# ...but a real claim NEXT TO a quoted phrase still matches.
_check('Self-wake queued for 5 min — unlike "wake file written" of old.', True)

# ---- Removed promise heuristic: watch-verb + job-noun prose must NOT match ----
# These all tripped the deleted WAKE_PROMISE_RE and armed phantom re-checks.
print("Watch-promise prose (old WAKE_PROMISE_RE) must NOT match")
_check("I will monitor the deploy and report back", False)
_check("I'll keep watching the build and let you know", False)
_check("I'll poll the CI pipeline until it finishes", False)
_check("I can run a P&L backtest audit against your monitoring dashboard", False)
_check("I'll wait for your reply", False)

# ---- Verify-board stripping ----
print("Verify-board item must not false-trigger")
_vb = (
    "Done, nothing pending.\n\n"
    "```verify-board\n"
    "- Self-wake queued: end a turn asserting this w/o a real directive\n"
    "```\n"
)
_check(_vb, False)
# ...but a real claim alongside a verify block still matches.
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
