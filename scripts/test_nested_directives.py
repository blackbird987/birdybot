"""A directive inside another directive's tilde body belongs to that body.

Background (q-18729, 2026-09-27): a parent wrote a /spawn brief that told the
child how to re-arm itself, so the ~~~spawn body carried a literal
``[BOT_CMD: /wake ...]`` plus its own ~~~wake block. Two things broke at once:

* the flat ``~~~spawn\\n(.*?)\\n~~~`` regex ended the brief at the nested
  block's closer, so the child got half its instructions and none of the
  re-arm step;
* the /wake scanner read the whole text flat, so the PARENT armed the 30-day
  wake it had only written down for the child (sch-1009).

Both parsers now go through ``textutil.find_tilde_block`` (depth-aware) and
scan directives on ``textutil.mask_tilde_bodies`` output. This pins that for
every directive kind, plus the regressions the old parsers already covered.

Run: python scripts/test_nested_directives.py [--incident PATH]
``--incident`` also replays a real result file (default: the installed bot's
data/results/q-18729.md when it exists). Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.engine import watches  # noqa: E402
from bot.engine.commands import (  # noqa: E402
    _BOT_CMD_RE,
    _extract_chain_directive,
    _pair_reply_directives,
    _pair_spawn_directives,
)
from bot.engine.images import parse_image_directives  # noqa: E402
from bot.engine.lifecycle import (  # noqa: E402
    _parse_wake_directive,
    armed_a_directive,
    has_turn_complete_marker,
    promises_continuation,
)
from bot.platform.formatting import collapse_bot_directives  # noqa: E402
from bot.textutil import find_tilde_block, mask_tilde_bodies  # noqa: E402

_failures: list[str] = []


def _check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        print(f"  FAIL: {label}" + (f"  ({detail})" if detail else ""))
        _failures.append(label)


# The incident shape, condensed: a spawn brief whose last step quotes the
# wake the child should arm, followed by the parent's own sign-off.
INCIDENT = """Set it up as a monthly pass.

[BOT_CMD: /spawn repo=mindroom title="Monthly profile consolidation" mode=build]
~~~spawn
You are running the monthly consolidation pass.

Step 0, safety checks. If the tree is dirty, end with a wake of delay=6h.

Step 8, end your final message with a wake so this recurs, exactly like this:

[BOT_CMD: /wake delay=30d reason="monthly mindroom consolidation"]
~~~wake
Run the monthly consolidation pass again, then end with another 30-day wake.
~~~
~~~

[TURN_COMPLETE]
"""

print("incident shape")
pairs, no_body, over_cap = _pair_spawn_directives(INCIDENT)
_check("exactly one spawn pair", len(pairs) == 1 and not no_body and not over_cap,
       f"{len(pairs)} pairs, {no_body} no_body, {over_cap} over_cap")
body = pairs[0][1] if pairs else ""
_check("brief keeps the nested wake directive", "[BOT_CMD: /wake delay=30d" in body)
_check("brief keeps the nested ~~~wake block whole",
       "~~~wake\nRun the monthly consolidation pass again, then end with "
       "another 30-day wake.\n~~~" in body, repr(body[-160:]))
_check("brief does not swallow the parent's sign-off", "[TURN_COMPLETE]" not in body)
_check("parent does NOT arm the child's wake", _parse_wake_directive(INCIDENT) is None)
_check("armed_a_directive is False for the parent", not armed_a_directive(INCIDENT))
_check("the parent's own [TURN_COMPLETE] still counts",
       has_turn_complete_marker(INCIDENT))
_check("the child receiving that body DOES arm its wake",
       (_parse_wake_directive(body) or {}).get("delay_secs") == 30 * 86400)

print("real top-level wake after a spawn with a nested one")
REAL = INCIDENT.replace(
    "[TURN_COMPLETE]",
    '[BOT_CMD: /wake delay=2h reason="check the child"]\n~~~wake\n'
    "See whether the child finished.\n~~~",
)
d = _parse_wake_directive(REAL) or {}
_check("the parent's own wake is armed", d.get("delay_secs") == 7200, repr(d))
_check("with the parent's own body", d.get("prompt") == "See whether the child finished.")

print("nesting inside plan and reply blocks")
PLAN = """Kicking off the chain.

[BOT_CMD: /chain preset=ship]
~~~plan
Build the thing. When done, the session should end with:
[BOT_CMD: /wake delay=1h]
~~~wake
recheck
~~~
and then verify.
~~~
"""
parsed = _extract_chain_directive(PLAN)
_check("chain body runs past the nested block",
       parsed is not None and parsed[1].endswith("and then verify."), repr(parsed))
_check("wake inside a plan is not armed", _parse_wake_directive(PLAN) is None)

REPLY = """[BOT_CMD: /reply thread=123]
~~~reply
Yes. Arm this before you stop:
[BOT_CMD: /watch pid=4242 label="job"]
~~~watch
job done, read the log
~~~
Thanks.
~~~
"""
rpairs, rno, _ = _pair_reply_directives(REPLY)
_check("one reply pair", len(rpairs) == 1 and rno == 0, repr(rpairs))
_check("reply body keeps its tail", bool(rpairs) and rpairs[0][1].endswith("Thanks."))
_check("watch inside a reply is not armed on the parent",
       watches.parse_watch_directive(REPLY) is None)
_check("... and not even seen as a written directive",
       not watches.has_watch_directive(REPLY))

print("nested /spawn, /repo and /image stay the child's")
NESTED_SPAWN = """[BOT_CMD: /spawn repo=bot title="Outer"]
~~~spawn
Outer brief. If it fails, spawn a helper:
[BOT_CMD: /spawn repo=bot title="Inner"]
~~~spawn
inner brief
~~~
Register the repo: [BOT_CMD: /repo add evil /tmp]
[BOT_CMD: /image path="x.png"]
~~~
"""
spairs, sno, _ = _pair_spawn_directives(NESTED_SPAWN)
_check("nested /spawn is not a second pair", len(spairs) == 1 and sno == 0,
       f"{len(spairs)} pairs, {sno} no_body")
_check("outer body contains the inner block and ends at its own closer",
       bool(spairs) and "inner brief" in spairs[0][1]
       and spairs[0][1].endswith('[BOT_CMD: /image path="x.png"]'))
_check("nested /repo add is invisible to the /repo scan",
       not list(_BOT_CMD_RE.finditer(mask_tilde_bodies(NESTED_SPAWN))))
_check("nested /image is not posted", parse_image_directives(NESTED_SPAWN) == [])

print("promise scan ignores a child's brief")
PROMISE = """Spawning it.

[BOT_CMD: /spawn repo=bot title="Bench"]
~~~spawn
Run the bench. I'll report back when the tests finish.
~~~
"""
_check("a promise inside a spawn body is not the parent's",
       not promises_continuation(PROMISE))
_check("the same promise at top level still is",
       promises_continuation("I'll report back when the tests finish."))

print("regressions")
_check("single /wake", (_parse_wake_directive(
    "[BOT_CMD: /wake delay=600]\n~~~wake\ngo on\n~~~") or {}).get("prompt") == "go on")
_check("single /watch", (watches.parse_watch_directive(
    '[BOT_CMD: /watch pid=1 label="x"]\n~~~watch\nfinished\n~~~') or {}).get("prompt")
    == "finished")
TWO = ('[BOT_CMD: /spawn repo=a title="A"]\n~~~spawn\none\n~~~\n'
       '[BOT_CMD: /spawn repo=b title="B"]\n~~~spawn\ntwo\n~~~\n')
tp, tno, _ = _pair_spawn_directives(TWO)
_check("two spawns pair in order", [b for _, b in tp] == ["one", "two"] and tno == 0)
SHARED = ('[BOT_CMD: /spawn repo=a title="A"]\n[BOT_CMD: /spawn repo=b title="B"]\n'
          '~~~spawn\nonly one\n~~~\n')
shp, shno, _ = _pair_spawn_directives(SHARED)
_check("one body cannot serve two directives", len(shp) == 1 and shno == 1)
_check("/chain", _extract_chain_directive(
    "[BOT_CMD: /chain preset=hold]\n~~~plan\ndo it\n~~~") == ("hold", "do it"))
_check("/reply", _pair_reply_directives(
    "[BOT_CMD: /reply thread=9]\n~~~reply\nanswer\n~~~")[0] == [("thread=9", "answer")])
_check("unterminated block yields no body",
       _pair_spawn_directives('[BOT_CMD: /spawn repo=a title="A"]\n~~~spawn\nno end')[1] == 1)
_check("unterminated wake body falls through to no directive",
       _parse_wake_directive("[BOT_CMD: /wake delay=60]\n~~~wake\nno end") is None)
_check("> quoted wake still skipped",
       _parse_wake_directive("> [BOT_CMD: /wake delay=60]\n~~~wake\nx\n~~~") is None)
_check("``` fenced wake still skipped", _parse_wake_directive(
    "```\n[BOT_CMD: /wake delay=60]\n~~~wake\nx\n~~~\n```") is None)
_check("inline-backtick wake still skipped", _parse_wake_directive(
    "use `[BOT_CMD: /wake delay=60]`\n~~~wake\nx\n~~~") is None)
_check("backticks inside a tilde body no longer flip the fence count",
       _parse_wake_directive(
           '[BOT_CMD: /spawn repo=a title="A"]\n~~~spawn\nuse ```python\n~~~\n'
           "[BOT_CMD: /wake delay=60]\n~~~wake\nreal\n~~~") is not None)

print("scanner and mask")
T = "a\n~~~spawn\nx [BOT_CMD: /wake]\n~~~wake\ny\n~~~\nz\n~~~\nb"
m = mask_tilde_bodies(T)
_check("mask preserves length", len(m) == len(T))
_check("mask preserves newline positions",
       [i for i, c in enumerate(m) if c == "\n"] == [i for i, c in enumerate(T) if c == "\n"])
_check("mask keeps opener and closer lines", m.startswith("a\n~~~spawn\n") and m.endswith("\n~~~\nb"))
_check("mask blanks the body", "BOT_CMD" not in m and "z" not in m.split("~~~spawn")[1][:-5])
span = find_tilde_block(T, "spawn")
_check("find returns the whole outer body",
       span is not None and T[span[0]:span[1]] == "x [BOT_CMD: /wake]\n~~~wake\ny\n~~~\nz")
_check("a nested block is not found at top level", find_tilde_block(T, "wake") is None)
_check("an unclosed block masks to the end", mask_tilde_bodies("~~~spawn\nx\n[BOT_CMD: /wake]")
       == "~~~spawn\n \n                ")
_check("text without tildes is returned unchanged", mask_tilde_bodies("plain") == "plain")

print("display collapse")
shown = collapse_bot_directives(INCIDENT)
_check("collapse swallows the whole spawn, nested block included",
       "Run the monthly consolidation" not in shown and "~~~" not in shown, repr(shown))
_check("collapse keeps the sign-off", shown.rstrip().endswith("[TURN_COMPLETE]"))
_example = "Arm it like this:\n\n\n\n~~~text\n[BOT_CMD: /wake delay=5m]\n~~~\n"
_check("a directive inside an unowned tilde block is left visible, not chipped",
       "[BOT_CMD: /wake delay=5m]" in collapse_bot_directives(_example),
       repr(collapse_bot_directives(_example)))


def _incident_path() -> str | None:
    args = sys.argv[1:]
    if "--incident" in args:
        i = args.index("--incident")
        return args[i + 1] if i + 1 < len(args) else None
    try:
        from pathlib import Path

        from bot.procutil import install_root
        p = os.path.join(str(install_root(Path(_ROOT))), "data", "results", "q-18729.md")
    except Exception:
        return None
    return p if os.path.exists(p) else None


path = _incident_path()
if path:
    print(f"replaying {path}")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    rp, rno, _ = _pair_spawn_directives(text)
    _check("real incident: one spawn pair", len(rp) == 1 and rno == 0)
    _check("real incident: brief reaches Step 8 and its wake block",
           bool(rp) and "Step 8" in rp[0][1] and rp[0][1].rstrip().endswith(
               "then end with another 30-day wake.\n~~~"))
    _check("real incident: parent arms no wake", _parse_wake_directive(text) is None)
else:
    print("(no incident file to replay; skipped)")

if _failures:
    print(f"\n{len(_failures)} check(s) FAILED")
    sys.exit(1)
print("\nAll checks passed.")
