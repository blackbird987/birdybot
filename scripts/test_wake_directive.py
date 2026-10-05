"""Regression test for the [BOT_CMD: /wake] self-wake directive parser.

Background: a session continues after its turn ONLY via a self-wake. The
primary, reliable channel is a ``[BOT_CMD: /wake ...]`` directive in the turn's
output — the same proven path as ``/spawn`` — parsed by
``lifecycle._parse_wake_directive``. This locks that parser so a future edit
can't (a) stop recognizing a real directive (which would reopen the silent
dead-end the directive was built to close), or (b) start firing on a fenced/
quoted EXAMPLE (which would let meta-discussion of the feature self-trigger a
wake — the exact loop we just fixed).

Calls the real production function so the test can't drift from what
``check_wake_request`` actually consumes.

Run: python scripts/test_wake_directive.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot import config
from bot.engine import lifecycle
from bot.engine.lifecycle import _parse_wake_directive

_failures: list[str] = []


def _check(label: str, cond: bool) -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


# ---- A real directive parses (body = prompt) ----
print("Real directive parses")
_d = _parse_wake_directive(
    'On it.\n\n[BOT_CMD: /wake delay=240 reason="deploy landing"]\n'
    "~~~wake\nRe-check the deploy; run the tests if live.\n~~~\n"
)
_check("returns a dict", isinstance(_d, dict))
_check("prompt from body", _d and _d["prompt"] == "Re-check the deploy; run the tests if live.")
_check("delay parsed as int", _d and _d["delay_secs"] == 240)
_check("reason parsed", _d and _d["reason"] == "deploy landing")

# ---- delay default when omitted / non-numeric ----
print("Missing or garbage delay defaults to int (still arms)")
_d2 = _parse_wake_directive(
    "[BOT_CMD: /wake reason=x]\n~~~wake\nkeep going\n~~~"
)
_check("missing delay -> fallback int",
       _d2 and _d2["delay_secs"] == config.WAKE_FALLBACK_DELAY_SECS)
_dg = _parse_wake_directive("[BOT_CMD: /wake delay=soon]\n~~~wake\ngo\n~~~")
_check("garbage delay -> fallback int (no drop)",
       _dg and _dg["delay_secs"] == config.WAKE_FALLBACK_DELAY_SECS)

# ---- unit-suffixed delays and the 30d ceiling ----
# The clamp used to stop at 24h, and delay= only accepted a bare number — so
# "delay=3d" silently became the 180s fallback. Both halves are pinned here.
print("Unit suffixes and the long-timer ceiling")
for _raw, _want in (("45s", 45), ("90m", 5400), ("6h", 21600),
                    ("3d", 259200), ("2w", 1209600)):
    _du = _parse_wake_directive(
        f"[BOT_CMD: /wake delay={_raw}]\n~~~wake\ncheck back\n~~~"
    )
    _check(f"delay={_raw} -> {_want}s", _du and _du["delay_secs"] == _want)
_dt = _parse_wake_directive("[BOT_CMD: /wake delay=3days]\n~~~wake\ngo\n~~~")
_check("a unit typo falls back rather than parsing its prefix",
       _dt and _dt["delay_secs"] == config.WAKE_FALLBACK_DELAY_SECS)

print("A multi-day delay survives the clamp")
_sched = lifecycle._wake_schedule_at({"delay_secs": 3 * 86400})
_check("3d is scheduled as 3d, not truncated to 24h",
       _sched is not None and _sched[1] == 3 * 86400)
_check("the ceiling is past a day", config.WAKE_MAX_DELAY_SECS > 86400)
_over = lifecycle._wake_schedule_at({"delay_secs": 365 * 86400})
_check("an absurd delay still clamps to the ceiling",
       _over is not None and _over[1] == config.WAKE_MAX_DELAY_SECS)
_under = lifecycle._wake_schedule_at({"delay_secs": 1})
_check("the floor still holds",
       _under is not None and _under[1] == config.WAKE_MIN_DELAY_SECS)

# ---- delay_secs alias accepted ----
print("delay_secs alias")
_d3 = _parse_wake_directive("[BOT_CMD: /wake delay_secs=600]\n~~~wake\np\n~~~")
_check("delay_secs alias parsed", _d3 and _d3["delay_secs"] == 600)

# ---- args-less directive (body carries the prompt) ----
print("Args-less directive parses from body")
_d4 = _parse_wake_directive("[BOT_CMD: /wake]\n~~~wake\njust keep going\n~~~")
_check("[BOT_CMD: /wake] + body parses", _d4 and _d4["prompt"] == "just keep going")
_check("args-less delay -> fallback int",
       _d4 and _d4["delay_secs"] == config.WAKE_FALLBACK_DELAY_SECS)

# ---- directive with NO prompt is treated as absent ----
print("Prompt-less directive -> None (lets backstop engage)")
_check("no body, no prompt= -> None",
       _parse_wake_directive("[BOT_CMD: /wake delay=300 reason=x]") is None)

# ---- No directive present ----
print("No directive -> None")
_check("plain completion is None",
       _parse_wake_directive("All done, tests pass. Nothing pending.") is None)
_check("empty is None", _parse_wake_directive("") is None)
# A different /BOT_CMD token must not match the /wake parser.
_check("/wakeup is not /wake",
       _parse_wake_directive("[BOT_CMD: /wakeup now]\n~~~wake\nx\n~~~") is None)

# ---- Fenced / quoted example must NOT parse (meta-discussion guard) ----
print("Fenced/quoted example must NOT fire")
_fenced = (
    "Here's how the directive looks:\n\n"
    "```\n[BOT_CMD: /wake delay=300 reason=demo]\n~~~wake\nexample\n~~~\n```\n"
)
_check("``` code-fenced directive ignored", _parse_wake_directive(_fenced) is None)
_quoted = "> [BOT_CMD: /wake delay=300 reason=quoted]\n> ~~~wake\n> x\n> ~~~"
_check("> quoted directive ignored", _parse_wake_directive(_quoted) is None)
_inline_code = (
    "Use the `[BOT_CMD: /wake delay=300 reason=x]` directive to continue."
)
_check("inline-backtick directive ignored",
       _parse_wake_directive(_inline_code) is None)


# ---- Summary ----
print()
if _failures:
    print(f"FAILED ({len(_failures)}):")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("All cases passed.")
sys.exit(0)
