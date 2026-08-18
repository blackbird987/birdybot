"""Regression test for [BOT_CMD: ...] directive collapsing in displayed text.

Background: directives are the machine-readable control channel between a
turn's output and the dispatchers. Left raw in the displayed text they dumped
the whole ~~~body~~~ into the thread — duplicating the outcome notice each
dispatcher already posts, and eating the display budget so the real answer got
truncated. `formatting.collapse_bot_directives` folds each one to a single
`-#` line at DISPLAY time only.

Two failure modes this locks down:

  (a) Under-collapsing / over-collapsing the display — a raw ~~~ body leaking
      back into the thread, or a quoted EXAMPLE getting eaten so the feature
      can't be discussed.
  (b) The dangerous one: someone "simplifying" by collapsing BEFORE dispatch.
      That would silently disarm every directive — no wake, no spawn, no
      chain, and no error either. The final section asserts the real
      dispatch-side parsers still fire on text that has NOT been collapsed,
      and stop firing once it has.

Calls the real production functions so the test can't drift.

Run: python scripts/test_botcmd_collapse.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.engine.commands import _pair_spawn_directives
from bot.engine.lifecycle import _parse_wake_directive
from bot.platform.formatting import (
    _CHIP_MAX,
    collapse_bot_directives,
    format_delay_secs,
)

_failures: list[str] = []


def _check(label: str, cond: bool) -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def _chips(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("-# ")]


# ---- /wake: command, delay, and why on one line ----
print("/wake collapses to one line carrying delay + reason")
_wake_raw = (
    "On it — the fixture run is still going.\n\n"
    '[BOT_CMD: /wake delay=720 reason="collecting the last Gemini fixture, '
    'then starting the head-to-head"]\n'
    "~~~wake\n"
    "Check /tmp/reg-compound-gemini.json and the newest report file.\n"
    "Verify the dev server is still up.\n"
    "~~~\n"
)
_wake_out = collapse_bot_directives(_wake_raw)
_check("no BOT_CMD left", "[BOT_CMD:" not in _wake_out)
_check("no tilde body left", "~~~" not in _wake_out)
_check("prose above survives", _wake_out.startswith("On it —"))
_check("exactly one chip", len(_chips(_wake_out)) == 1)
_check("names the command", "`/wake`" in _wake_out)
_check("shows the delay", "in 12 min" in _wake_out)
_check("shows the why", "collecting the last Gemini fixture" in _wake_out)
_check("chip fits one line", all(len(c) <= _CHIP_MAX + 3 for c in _chips(_wake_out)))

# ---- delay wording matches the confirmation notice ----
# ---- /watch: the job being waited on, not a guessed delay ----
print("/watch collapses to one line naming the job and its pid")
_watch_raw = (
    "Kicked the fit off in the background.\n\n"
    '[BOT_CMD: /watch pid=959988 log="artifacts/sculpt/run.log" '
    'progress="step (\\d+)/(\\d+)" label="sculpt fit (both arms)" timeout=6h]\n'
    "~~~watch\n"
    "Read the log and pull held-out IoU for both arms.\n"
    "~~~\n\n"
    "Nothing else to do until it lands."
)
_watch_out = collapse_bot_directives(_watch_raw)
_check("watch: no BOT_CMD left", "[BOT_CMD:" not in _watch_out)
_check("watch: no tilde body left", "~~~" not in _watch_out)
_check("watch: prose above survives", _watch_out.startswith("Kicked the fit off"))
_check("watch: prose below survives", _watch_out.rstrip().endswith("until it lands."))
_check("watch: exactly one chip", len(_chips(_watch_out)) == 1)
_check("watch: names the command", "`/watch`" in _watch_out)
_check("watch: names the job", "sculpt fit (both arms)" in _watch_out)
_check("watch: shows the pid", "pid 959988" in _watch_out)
_check("watch: chip fits one line",
       all(len(c) <= _CHIP_MAX + 3 for c in _chips(_watch_out)))

_watch_done = collapse_bot_directives(
    '[BOT_CMD: /watch done="=== finished ===" label="remote run"]\n'
    "~~~watch\nGo.\n~~~\n"
)
_check("watch: done-marker trigger described",
       "until done marker" in _watch_done)


print("Chip delay wording matches check_wake_request's notice")
_check("720s renders as 12 min", format_delay_secs(720) == "12 min")
_check("chip reuses that exact string", f"in {format_delay_secs(720)}" in _wake_out)

# ---- /spawn: repo, mode, title ----
print("/spawn collapses with repo, mode and title")
_spawn_out = collapse_bot_directives(
    "Kicking off the survey.\n\n"
    '[BOT_CMD: /spawn repo=bot title="Fix the parser regression" mode=build]\n'
    "~~~spawn\n"
    "Long prompt body.\nWith several lines.\nAnd a ```fence``` inside.\n"
    "~~~\n"
)
_check("body gone", "~~~" not in _spawn_out and "Long prompt body" not in _spawn_out)
_check("names the command", "`/spawn`" in _spawn_out)
_check("shows repo", "bot" in _spawn_out)
_check("shows mode", "build" in _spawn_out)
_check("shows title", "Fix the parser regression" in _spawn_out)

# ---- multiple directives each get their own chip ----
print("A fan-out of directives yields one chip each")
_multi = collapse_bot_directives(
    "Firing three.\n\n"
    '[BOT_CMD: /spawn repo=bot title="A" mode=build]\n~~~spawn\naaa\n~~~\n'
    '[BOT_CMD: /spawn repo=bot title="B" mode=build]\n~~~spawn\nbbb\n~~~\n'
    '[BOT_CMD: /spawn repo=bot title="C" mode=plan]\n~~~spawn\nccc\n~~~\n'
)
_check("three chips", len(_chips(_multi)) == 3)
_check("no bodies leaked", not any(x in _multi for x in ("aaa", "bbb", "ccc")))
_check("titles preserved", all(f'"{t}"' in _multi for t in "ABC"))

# ---- /chain explains the preset ----
print("/chain names the preset and what it runs")
_chain_out = collapse_bot_directives(
    "Kicking off the chain.\n\n"
    "[BOT_CMD: /chain preset=ship]\n~~~plan\n" + ("plan line\n" * 400) + "~~~\n"
)
_check("plan body gone", "plan line" not in _chain_out)
_check("names the preset", "ship" in _chain_out)
_check("explains the flow", "build → review → verify → release → merge" in _chain_out)
_check("bare preset form also works", "ship" in collapse_bot_directives(
    "[BOT_CMD: /chain ship]\n~~~plan\nx\n~~~\n"
))
_check("no preset falls back to policy wording", "repo default policy" in
       collapse_bot_directives("[BOT_CMD: /chain]\n~~~plan\nx\n~~~\n"))

# ---- /repo shows its args verbatim ----
print("/repo shows the action and args")
_repo_out = collapse_bot_directives(
    "Registering it.\n\n[BOT_CMD: /repo add fundops C:\\Users\\Q\\fundops]\n"
)
_check("names the command", "`/repo`" in _repo_out)
_check("shows the args", "add fundops C:\\Users\\Q\\fundops" in _repo_out)

# ---- directive with no body still collapses ----
print("Directive with no body collapses to one line")
_nobody = collapse_bot_directives("Done.\n\n[BOT_CMD: /wake delay=300]\n\nMore prose.")
_check("no BOT_CMD left", "[BOT_CMD:" not in _nobody)
_check("shows the delay", "in 5 min" in _nobody)
_check("following prose survives", "More prose." in _nobody)

# ---- THE regression: quoted / fenced examples stay verbatim ----
print("Quoted and fenced examples are left untouched (must stay discussable)")
for _label, _prefix in (("blockquote", "> "), ("inline code", "`"), ("heading", "### ")):
    _quoted = (
        "Here's how the format looks:\n\n"
        f'{_prefix}[BOT_CMD: /wake delay=300 reason="example"]\n\n'
        "That's the shape."
    )
    _check(
        f"{_label} example unchanged",
        collapse_bot_directives(_quoted) == _quoted.rstrip(),
    )

print("Text with no directives passes through unchanged")
_plain = "Just a normal answer.\n\nWith two paragraphs."
_check("byte-identical", collapse_bot_directives(_plain) == _plain)
_check("empty string safe", collapse_bot_directives("") == "")

# ---- dispatch parity: collapsing must never happen before dispatch ----
print("Dispatch-side parsers still fire on RAW text, and go quiet on collapsed")
_check("wake parser sees the raw directive", _parse_wake_directive(_wake_raw) is not None)
_check(
    "wake parser finds nothing once collapsed",
    _parse_wake_directive(_wake_out) is None,
)
_spawn_raw = (
    '[BOT_CMD: /spawn repo=bot title="A" mode=build]\n~~~spawn\naaa\n~~~\n'
)
_check("spawn pairing works on raw", len(_pair_spawn_directives(_spawn_raw)[0]) == 1)
_check(
    "spawn pairing finds nothing once collapsed",
    len(_pair_spawn_directives(collapse_bot_directives(_spawn_raw))[0]) == 0,
)

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S):")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("All checks passed.")
