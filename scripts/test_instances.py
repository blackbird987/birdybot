#!/usr/bin/env python3
"""Regression test: a session can read the bot's own instance registry.

`scripts/instances.py` exists because a session asked "are the children you
spawned done?" could only answer "run /list yourself" -- the bot knew, and the
session had no way to ask. What makes the answer possible is one resolution
chain, and every link in it is a place where a plausible shortcut gives a wrong
answer instead of no answer:

  * an instance's thread comes from `history.jsonl` FIRST and from the live
    session-to-thread map only as a fallback, because a thread moves on. Resolve
    thread by session alone and a finished child is reported under whatever is
    running in its thread now
  * a child is joined back to its parent through that thread, since a spawned
    child carries no `parent_id` -- only button/chain steps do
  * an instance that died before the CLI reported a session id AND never
    finalized has neither link, and must report as unlinkable rather than be
    guessed at from timing

And the constraint the whole tool rests on: it is read-only by *omission*, like
the mail and telegram readers in The Citadel. That is not a property of the
docstring, so it is asserted against the source here -- a later edit that adds
a write path fails this suite.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
sys.path.insert(0, str(_SCRIPT_DIR.parent))

import instances as mod  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if not ok:
        failures.append(f"{label}{(' — ' + detail) if detail else ''}")


# --- a synthetic registry --------------------------------------------------
#
# Shapes taken from the real incident: q-16871 spawned three children into
# three threads; one finished, one finished and had its thread reused by a
# later session, and one was killed by a bot restart before it ever reported a
# session id.

tmp = tempfile.TemporaryDirectory()
DATA = Path(tmp.name)
(DATA / "results").mkdir()

T_DONE = "1000000000000000001"      # child that completed
T_REUSED = "1000000000000000002"    # child that completed, thread reused since
T_LOST = "1000000000000000003"      # child that died before reporting a session


def inst(iid: str, **kw) -> dict:
    base = {
        "id": iid,
        "name": None,
        "instance_type": "query",
        "prompt": f"prompt for {iid}",
        "repo_name": "aiagent",
        "repo_path": "/repo/aiagent",
        "status": "completed",
        "created_at": "2026-09-08T11:00:00+00:00",
        "mode": "build",
    }
    base.update(kw)
    return base


STATE = {
    "instances": [
        inst("q-100", prompt="spawn sessions for each",
             spawn_dispatched_thread_ids=[T_DONE, T_REUSED, T_LOST],
             spawn_wave_sealed=True, spawn_wave_released=True,
             session_id="sess-parent",
             created_at="2026-09-08T11:00:00+00:00"),
        # The wave's children. None of them carries a parent_id — that is the
        # whole reason the thread hop exists.
        inst("q-101", session_id="sess-done", branch="claude-bot/q-101",
             worktree_path="/repo/aiagent/.worktrees/q-101",
             result_file="/OTHER/MACHINE/data/results/q-101.md",
             created_at="2026-09-08T11:01:00+00:00",
             finished_at="2026-09-08T11:20:00+00:00"),
        inst("q-102", session_id="sess-reused-old",
             created_at="2026-09-08T11:02:00+00:00",
             finished_at="2026-09-08T11:30:00+00:00"),
        inst("q-103", session_id=None, status="failed",
             error="Bot restarted — instance interrupted",
             created_at="2026-09-08T11:03:00+00:00"),
        # The later session that took over q-102's thread.
        inst("q-200", session_id="sess-reused-new", status="running", pid=None,
             created_at="2026-09-08T12:00:00+00:00"),
        # A button/chain step, which DOES carry a parent_id.
        inst("t-500", parent_id="q-101", origin="review_code",
             name="reviewer", branch="claude-bot/q-101",
             created_at="2026-09-08T11:25:00+00:00"),
    ],
    "repos": {"aiagent": "/repo/aiagent"},
    "platform_state": {"discord": {"forum_projects": {"aiagent": {
        "repo_name": "aiagent",
        "threads": {
            T_DONE: {"thread_id": T_DONE, "session_id": "sess-done",
                     "topic": "Funding charges", "origin": "spawn"},
            # The thread has moved on to a NEW session since q-102 ran.
            T_REUSED: {"thread_id": T_REUSED, "session_id": "sess-reused-new",
                       "topic": "Regime segmentation", "origin": "spawn"},
            T_LOST: {"thread_id": T_LOST, "session_id": "sess-gone",
                     "topic": "Composite score", "origin": "spawn"},
        },
    }}}},
}
(DATA / "state.json").write_text(json.dumps(STATE), encoding="utf-8")

# History records the thread per instance at finalize. q-103 has no entry —
# it never finalized, which is exactly why it is unlinkable.
history = [
    {"id": "q-100", "thread_id": "999", "repo": "aiagent", "status": "completed"},
    {"id": "q-101", "thread_id": T_DONE, "repo": "aiagent", "branch": "claude-bot/q-101",
     "status": "completed"},
    {"id": "q-102", "thread_id": T_REUSED, "repo": "aiagent", "status": "completed"},
    # q-200 is deliberately absent: it is still running, so nothing has
    # finalized it yet and its thread can only come from the live session map.
]
(DATA / "history.jsonl").write_text(
    "\n".join(json.dumps(h) for h in history) + "\n", encoding="utf-8")

# The recorded result path is the OTHER machine's spelling (see bot/paths.py);
# the local copy is what should be found.
(DATA / "results" / "q-101.md").write_text("line one\nline two\nline three\n",
                                           encoding="utf-8")

mod.STATE_FILE = DATA / "state.json"
mod.HISTORY_FILE = DATA / "history.jsonl"
mod.RESULTS_DIR = DATA / "results"

reg = mod.Registry()


# --- lookups ---------------------------------------------------------------

print("Lookups")

check("an instance resolves by id", reg.get("q-101") is not None)
check("and by name", (reg.get("reviewer") or object()).__dict__.get("id") == "t-500")
check("an unknown id is None, not a crash", reg.get("nope") is None)


# --- the thread hop --------------------------------------------------------

print("The thread hop")

check("history is authoritative for a finished instance's thread",
      reg.thread_of(reg.get("q-101")) == T_DONE)
check("a reused thread still resolves the OLD instance to itself",
      reg.thread_of(reg.get("q-102")) == T_REUSED,
      "resolving by session alone would find nothing here")
check("an instance with no history falls back to its live session",
      reg.thread_of(reg.get("q-200")) == T_REUSED)
check("an instance with neither is unlinkable, not guessed",
      reg.thread_of(reg.get("q-103")) is None)


# --- parents ---------------------------------------------------------------

print("Parents")

parent, kind = reg.parent_of(reg.get("q-101"))
check("a spawned child joins back to its parent through the thread",
      parent is not None and parent.id == "q-100", str(parent))
check("and is labelled as a wave, not a step", kind == "spawn wave", kind)

parent, kind = reg.parent_of(reg.get("t-500"))
check("a button/chain step joins through parent_id",
      parent is not None and parent.id == "q-101", str(parent))
check("and is labelled a step", kind == "step", kind)

parent, kind = reg.parent_of(reg.get("q-103"))
check("the unlinkable child reports no parent rather than a wrong one",
      parent is None, str(parent))

check("the root of a step is the wave parent above its own parent",
      mod._root_of(reg, reg.get("t-500")).id == "q-100")


# --- children --------------------------------------------------------------

print("Children")

kids = reg.wave_children(reg.get("q-100"))
check("every roster thread is reported", len(kids) == 3, str(len(kids)))
by_thread = {k.thread_id: k for k in kids}

done = by_thread[T_DONE]
check("a finished child reports its own terminal state", done.state == "completed")
check("and names the branch its work went to",
      (done.current.branch if done.current else None) == "claude-bot/q-101")

reused = by_thread[T_REUSED]
check("a reused thread reports what is running in it NOW",
      reused.state == "running" and reused.current.id == "q-200", reused.state)
check("without losing the child that actually ran there",
      [i.id for i in reused.instances] == ["q-102", "q-200"],
      str([i.id for i in reused.instances]))

lost = by_thread[T_LOST]
check("a thread nothing can be resolved for reads as unresolved",
      lost.state == "unresolved" and lost.current is None)

check("button/chain steps are listed separately from the wave",
      [i.id for i in reg.step_children(reg.get("q-101"))] == ["t-500"])
check("and a wave parent has no step children",
      reg.step_children(reg.get("q-100")) == [])


# --- artefacts -------------------------------------------------------------

print("Artefacts")

path = reg.result_path(reg.get("q-101"))
check("a result recorded under another machine's path falls back to the local copy",
      path is not None and path == DATA / "results" / "q-101.md", str(path))
check("an instance that never wrote a result reports None",
      reg.result_path(reg.get("q-103")) is None)


# --- the CLI surface -------------------------------------------------------

print("The CLI surface")


class _Args:
    def __init__(self, **kw):
        self.json = False
        self.diff = False
        self.tail = 0
        self.limit = 50
        self.repo = None
        self.status = None
        self.since_hours = 0.0
        self.no_root = False
        self.__dict__.update(kw)


def run(fn, **kw) -> int:
    """Call a command, swallowing its output — the harness reports, not the tool."""
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return fn(reg, _Args(**kw))


check("list runs", run(mod.cmd_list) == 0)
check("list --status filters", run(mod.cmd_list, status="failed") == 0)
check("show runs", run(mod.cmd_show, id="q-101") == 0)
check("show on an unknown id exits non-zero", run(mod.cmd_show, id="nope") == 1)
check("children runs", run(mod.cmd_children, id="q-100") == 0)
check("tree runs", run(mod.cmd_tree, id="q-100") == 0)
check("log runs", run(mod.cmd_log, id="q-101", tail=2) == 0)
check("log on an instance with no output still exits 0",
      run(mod.cmd_log, id="q-103") == 0,
      "an honest 'nothing was written' is an answer, not a failure")
check("find resolves a thread id", run(mod.cmd_find, key=T_REUSED) == 0)
check("find on nothing exits non-zero", run(mod.cmd_find, key="zzz") == 1)


# --- read-only by omission -------------------------------------------------

print("Read-only by omission")

src = (_SCRIPT_DIR / "instances.py").read_text(encoding="utf-8")
code = "\n".join(
    line for line in src.splitlines()
    if not line.lstrip().startswith("#")
)
# Strip docstrings so prose about writing does not read as a write.
code = re.sub(r'""".*?"""', "", code, flags=re.DOTALL)

banned = [
    "write_text", "write_bytes", ".save(", "mark_dirty", "unlink", "rmtree",
    "mkdir", "os.remove", "os.rename", "os.system", "subprocess", "shutil",
]
for token in banned:
    check(f"no {token} in the tool", token not in code)

for match in re.finditer(r"\.open\(([^)]*)\)|(?<![.\w])open\(([^)]*)\)", code):
    argtext = match.group(1) or match.group(2) or ""
    check("every open() is read-mode",
          not any(m in argtext for m in ('"w', "'w", '"a', "'a", '"x', "'x", "+")),
          argtext)

check("the tool never imports StateStore, whose save() is one typo away",
      "bot.store" not in code)
check("and never imports bot.config, which resolves DATA_DIR against the cwd "
      "and writes a path marker",
      "bot.config" not in code and "from bot import config" not in code)

tmp.cleanup()

print()
if failures:
    print("FAIL: instance registry reader")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print(f"PASS: a session can read the bot's instance registry ({checks} checks).")
print("      A spawned child carries no parent_id, so it is joined back to its")
print("      parent through the thread it ran in — history first, the live")
print("      session map only as a fallback, and an instance that recorded")
print("      neither reports as unlinkable instead of being guessed at. The")
print("      tool has no write path at all, asserted against its own source.")
