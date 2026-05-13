"""Regression tests for the no-commit halt helpers in workflows.py.

Pins the behavior of ``_stop_reason_snippet`` and ``_build_halted_text``,
which build the user-facing message shown when a build (or build phase)
finishes without committing anything. The prior copy was a generic
"Build had no changes" that ignored the agent's actual stopping reason;
these helpers surface the reason and adapt CTAs for path-poisoning and
multi-phase contexts.

Run: python scripts/test_halt_text.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.engine.workflows import (  # noqa: E402
    _build_halted_text,
    _stop_reason_snippet,
)


_failures: list[str] = []
_total = 0


def _check(label: str, condition: bool, detail: str = "") -> None:
    global _total
    _total += 1
    if not condition:
        _failures.append(f"{label}: {detail}" if detail else label)


# --- _stop_reason_snippet ---------------------------------------------------

_check(
    "snippet returns None for empty/whitespace",
    _stop_reason_snippet(None) is None and _stop_reason_snippet("   \n  ") is None,
)

_check(
    "snippet strips triple-backtick fence lines",
    _stop_reason_snippet("Stopping because:\n```\nfoo\n```\nbar") is not None
    and "```" not in (_stop_reason_snippet("Stopping because:\n```\nfoo\n```\nbar") or ""),
    detail=repr(_stop_reason_snippet("Stopping because:\n```\nfoo\n```\nbar")),
)

_long = " ".join(["word"] * 200)
_truncated = _stop_reason_snippet(_long, max_chars=80)
_check(
    "snippet truncates at word boundary with ellipsis",
    _truncated is not None and _truncated.endswith("…") and len(_truncated) <= 81,
    detail=repr(_truncated),
)

_check(
    "snippet returns only first paragraph",
    _stop_reason_snippet("first para\n\nsecond para") == "first para",
    detail=repr(_stop_reason_snippet("first para\n\nsecond para")),
)


# --- _build_halted_text -----------------------------------------------------

# Reason present, no poisoning → CTA appended; mention summarizes reason.
halt, mention = _build_halted_text(
    header="Build stopped before committing.",
    summary="Bash is disabled in this turn. Can't continue.",
    preserved=None,
)
_check(
    "reason present: embed quotes the reason",
    "> Bash is disabled" in halt,
    detail=repr(halt),
)
_check(
    "reason present: embed ends with Reply CTA",
    halt.endswith("Reply to continue."),
    detail=repr(halt),
)
_check(
    "reason present: mention summarizes first line",
    mention.startswith("Build paused: Bash is disabled"),
    detail=repr(mention),
)

# No reason → fallback CTA.
halt, mention = _build_halted_text(
    header="Build stopped before committing.",
    summary=None,
    preserved=None,
)
_check(
    "no reason: fallback CTA present",
    "No reason given — reply to continue." in halt,
    detail=repr(halt),
)
_check(
    "no reason: generic mention",
    mention == "Build paused — needs your input.",
    detail=repr(mention),
)

# Path poisoning suppresses the CTA (recovery copy is the CTA).
halt, mention = _build_halted_text(
    header="Build stopped before committing.",
    summary="Couldn't write to worktree.",
    preserved=None,
    path_poisoning=["bot/app.py", "bot/config.py"],
)
_check(
    "poisoning: 'Reply to continue.' suppressed",
    "Reply to continue." not in halt,
    detail=repr(halt),
)
_check(
    "poisoning: poisoning block rendered",
    "Path poisoning detected" in halt and "`bot/app.py`" in halt,
    detail=repr(halt),
)
_check(
    "poisoning: 'Start a fresh build' recovery copy present",
    "Start a fresh build" in halt,
    detail=repr(halt),
)

# Preserved branch block renders.
halt, _ = _build_halted_text(
    header="Build stopped before committing.",
    summary="Stopping.",
    preserved="wip/claude-bot-t-1234",
)
_check(
    "preserved: WIP recovery block present",
    "`wip/claude-bot-t-1234`" in halt and "git checkout" in halt,
    detail=repr(halt),
)

# phase_label propagates to the mention prefix.
_, mention = _build_halted_text(
    header="Phase `migrate-schema` stopped before committing.",
    summary="Need a schema decision.",
    preserved=None,
    phase_label="Phase `migrate-schema`",
)
_check(
    "phase_label: mention uses phase prefix",
    mention.startswith("Phase `migrate-schema` paused: Need a schema decision"),
    detail=repr(mention),
)

# Both preserved AND poisoning: both blocks render; CTA still suppressed.
halt, _ = _build_halted_text(
    header="Build stopped before committing.",
    summary="Stopped.",
    preserved="wip/foo",
    path_poisoning=["x.py"],
)
_check(
    "preserved + poisoning: both blocks render, CTA suppressed",
    "`wip/foo`" in halt and "Path poisoning detected" in halt
    and "Reply to continue." not in halt,
    detail=repr(halt),
)


# --- Report -----------------------------------------------------------------

if _failures:
    print(f"FAIL: {len(_failures)}/{_total} assertions failed")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)

print(f"OK: {_total}/{_total} assertions passed")
