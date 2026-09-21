#!/usr/bin/env python3
"""Regression test: TL;DR explains what is on the table, and does nothing else.

"Can you summarise this in simple terms" was being retyped by hand in nearly
every thread, so it came out a different shape every time. The [TL;DR] button
and /tldr make it one shape, and the properties that make it worth having are
the ones easy to lose in a refactor:

  * **two shapes, not one.** Work that already exists ends with the caveat; a
    plan or a problem ends with the decision the user has to make. Collapsing
    them into one template forces one of the two into the wrong ending.
  * it **resumes the session**. What it explains is the conversation, not the
    diff -- a fresh session would re-derive the "what" from the code and get
    the "why" wrong.
  * it is **read-only**, at the floor that also closes Bash. An explanation
    that goes off and does more work is not an explanation.
  * it **never lands on its own card**. Re-summarising a summary says nothing
    and the recursion has no floor.
  * it **never claims a button row**. Five rows is the Discord ceiling, and a
    recap is not worth displacing a Merge or a plan row.

Run: python scripts/test_tldr.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.claude.types import (  # noqa: E402
    BUILD_ORIGINS, Instance, InstanceOrigin, InstanceStatus, InstanceType,
)
from bot.engine import commands as commands_mod  # noqa: E402
from bot.engine import workflows  # noqa: E402
from bot.platform import formatting  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" · {detail}" if detail else ""))
    if not ok:
        failures.append(label)


_root = Path(__file__).resolve().parent.parent


def _inst(iid: str = "t-1", **kw) -> Instance:
    base = dict(
        id=iid, name=None, instance_type=InstanceType.QUERY, prompt="x",
        repo_name="bot", repo_path="/tmp/bot",
        status=InstanceStatus.COMPLETED, origin=InstanceOrigin.DIRECT,
        session_id="sess-1",
    )
    base.update(kw)
    return Instance(**base)


def _ids(inst: Instance, **kw) -> list[str]:
    return [b.callback_data for row in formatting.action_button_specs(inst, **kw) for b in row]


# ------------------------------- 1. the prompt carries both endings

print()
print("the prompt asks for two shapes, not one")

_p = config.TLDR_PROMPT
check("it makes the caller decide which situation it is in",
      "A) The thing already exists" in _p and "B) The thing does not exist yet" in _p)
check("finished work ends with the caveat",
      "**The catch**" in _p and "**What's different now**" in _p)
check("a plan or a problem ends with the decision",
      "**What I need from you**" in _p and "**The situation**" in _p)
check("the decision names a default, so \"yes\" is a complete answer",
      'recommendation' in _p and '"yes" is a complete answer' in _p)
check("identifiers are banned outright, not discouraged",
      "No file paths, no function names" in _p
      and "the bullet is wrong" in _p)
check("and it is told not to narrate its own edits",
      'Never describe your own actions' in _p)
check("the example rule shows the bad and the good version",
      "is abstract and useless" in _p and "Write the second kind" in _p)
check("it is told to do no new work",
      "Do NOT do new work" in _p and "do not change anything" in _p)
check("it is bounded for a phone screen",
      "Under 900 characters" in _p)
check("no em dashes in the constant (repo writing rule)",
      "—" not in _p)


# ------------------------------- 2. the run itself is read-only and resumed

print()
print("the run resumes the conversation and writes nothing")

_src = inspect.getsource(workflows.on_tldr)
check("it resumes the session rather than starting fresh",
      "resume_session=True" in _src)
check("it runs in explore mode", 'mode="explore"' in _src)
check("behind the read-only floor, which also closes Bash",
      'permission_mode="explore"' in _src)
check("it never branches", "auto_branch" not in _src and "copy_branch" not in _src)
check("it carries its own origin", "InstanceOrigin.TLDR" in _src)
check("and it sends the shared prompt, not a local copy",
      "config.TLDR_PROMPT" in _src)

# The floor is what the mode claim rests on: "explore" clamps bash_policy to
# "none", so a recap cannot write through sed/echo even though it inherits a
# build-mode parent's session.
check("the floor really does clamp bash to none",
      workflows._enforce_readonly_floor("explore", "build", "all") == ("explore", "none"))

check("a TL;DR turn does not run on the expensive build model",
      InstanceOrigin.TLDR not in BUILD_ORIGINS)


# ------------------------------- 3. the button is wired end to end

print()
print("button and command reach the same handler")

_interactions = (_root / "bot" / "discord" / "interactions.py").read_text(encoding="utf-8")
check("the tap is recognised as a long-running query",
      '"tldr"' in _interactions.split("_QUERY_ACTIONS")[1].split("})")[0])
check("and the usage-limit gate can name it",
      '"tldr": "TL;DR"' in _interactions)

_dispatch = inspect.getsource(commands_mod.handle_callback)
check("the dispatch reaches the handler",
      'action == "tldr"' in _dispatch and "workflows.on_tldr(ctx" in _dispatch)

_slash = (_root / "bot" / "discord" / "slash_commands.py").read_text(encoding="utf-8")
check("/tldr is registered", 'name="tldr"' in _slash)
check("and it calls the same handler as the button, not a copy of it",
      "workflows.on_tldr(ctx" in _slash)
check("it refuses a channel that is not a session thread",
      "isn't a session thread" in _slash)
check("and a thread that has never run anything",
      "Nothing has run in this thread yet" in _slash)


# ------------------------------- 4. where the button may and may not appear

print()
print("the button rides an existing row, and never its own card")

_long = _ids(_inst(), show_expand=True)
check("a finished turn with a long result offers TL;DR",
      "tldr:t-1" in _long, str(_long))
check("it sits on the Expand row rather than claiming a new one",
      formatting.action_button_specs(_inst(), show_expand=True)[-1][-1].callback_data
      == "tldr:t-1")

_short = _ids(_inst())
check("a short result offers it too",
      "tldr:t-1" in _short, str(_short))
check("there it rides the Branch/Share row",
      formatting.action_button_specs(_inst())[-1][0].callback_data == "branch:t-1")

_recap = _ids(_inst("t-2", origin=InstanceOrigin.TLDR), show_expand=True)
check("but a TL;DR card never offers another TL;DR",
      not any(i.startswith("tldr:") for i in _recap), str(_recap))

_sessionless = _ids(_inst(session_id=None), show_expand=True)
check("a turn with no session to resume does not offer it",
      not any(i.startswith("tldr:") for i in _sessionless), str(_sessionless))

for _status in (InstanceStatus.RUNNING, InstanceStatus.FAILED, InstanceStatus.KILLED):
    _mid = _ids(_inst(status=_status), show_expand=True)
    check(f"nor does a {_status.value} one",
          not any(i.startswith("tldr:") for i in _mid), str(_mid))

_review = _ids(_inst("pr-1", origin=InstanceOrigin.PROMPT_REVIEW), show_expand=True)
check("the prompt review's Ark card is still button-free",
      not any(i.startswith("tldr:") for i in _review), str(_review))

# Five rows is the hard Discord ceiling and a crowded build card is exactly
# where a recap is least worth a row of its own.
_branchy = _inst("t-3", branch="claude-bot/t-3")
for _kw in ({}, {"show_expand": True}, {"has_autopilot_chain": True}):
    _rows = formatting.action_button_specs(_branchy, **_kw)
    check(f"no card exceeds five rows {_kw or '(plain)'}", len(_rows) <= 5, str(len(_rows)))
    check(f"and no row exceeds five buttons {_kw or '(plain)'}",
          all(len(r) <= 5 for r in _rows), str([len(r) for r in _rows]))

# The expanded view renders through the same function; the Collapse row is
# appended after, so TL;DR must not have stolen its Share slot.
_expanded = [b.callback_data for row in formatting.expanded_button_specs(_inst()) for b in row]
check("the expanded view keeps exactly one Share",
      sum(1 for i in _expanded if i.startswith("share:")) == 1, str(_expanded))
check("and still offers TL;DR", "tldr:t-1" in _expanded, str(_expanded))


# ------------------------------- 5. "latest" has one definition

print()
print("the command resolves the same instance the button sat on")

_store = StateStore.__new__(StateStore)
_store._instances = {}
for _iid, _created, _sess in (
    ("t-10", "2026-09-01T10:00:00", "sess-1"),
    ("t-11", "2026-09-01T12:00:00", "sess-1"),
    ("t-12", "2026-09-01T11:00:00", "sess-1"),
    ("t-13", "2026-09-01T23:00:00", "sess-2"),
):
    _store._instances[_iid] = _inst(_iid, session_id=_sess, created_at=_created)

check("the newest turn of the session wins, not the first scanned",
      _store.latest_instance_for_session("sess-1").id == "t-11")
check("a different session is not picked up",
      _store.latest_instance_for_session("sess-2").id == "t-13")
check("an unbound thread resolves to nothing",
      _store.latest_instance_for_session(None) is None)
check("and so does a session nothing has run under",
      _store.latest_instance_for_session("sess-nope") is None)

# One implementation: the wave join used to carry its own copy of this scan,
# and two copies is how "latest" drifts into "first match" on one of them.
_orch = (_root / "bot" / "discord" / "orchestrator.py").read_text(encoding="utf-8")
check("the spawn-wave join delegates to the store's scan",
      "bot._store.latest_instance_for_session(session_id)" in _orch)


# ------------------------------- 6. the recap card stays clean

print()
print("a recap card does not pretend to be a workflow step")

check("no mode toggle on a recap (its mode is always the clamped explore)",
      InstanceOrigin.TLDR in formatting._WORKFLOW_ORIGINS)
_recap_ids = _ids(_inst("t-2", origin=InstanceOrigin.TLDR), show_expand=True)
check("so no Mode button is drawn",
      not any(i.startswith("mode_") for i in _recap_ids), str(_recap_ids))
check("but the recap can still be expanded and read",
      any(i.startswith("expand:") for i in _recap_ids)
      and any(i.startswith("log:") for i in _recap_ids), str(_recap_ids))


print()
if failures:
    print("FAIL: tldr")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print(f"PASS: TL;DR explains what is on the table and nothing else ({checks} checks).")
print("      Two shapes (built vs not built yet), the session resumed rather")
print("      than restarted, read-only at the floor that closes Bash, never on")
print("      its own card, and never at the cost of a button row.")
