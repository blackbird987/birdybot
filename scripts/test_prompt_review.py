#!/usr/bin/env python3
"""Regression test: corrections are proposed from the eval record, never applied.

Every session is scored, every recurring flag is attributed to the prompt block
that was supposed to prevent it, and until now nobody read them in aggregate.
The weekly prompt review closes that loop. It is only safe because of a handful
of properties that are easy to lose in a refactor:

  * a **retired** check's flags stop counting. `tool_hygiene` flagged the very
    Bash reads and greps that bypass-permissions mode instructs a session to
    do, so it fired ~73,000 times in 30 days and buried every real finding. The
    check is gone; its output is still on disk forever.
  * the reviewing agent runs behind the **read-only floor**, so it cannot edit
    the constraints it is reviewing. Asserted on the created Instance, not on
    the prose of a prompt.
  * only a `contradicted` or `obsolete` finding may become an edit. A rule that
    is merely **disobeyed** is reported and left alone -- deleting a rule
    because it is hard to follow is exactly backwards.
  * the weekly gate runs off a **persisted timestamp**, so a reboot neither
    re-fires it nor skips the week.
  * a **rejected** proposal is recognised on its (file, flag) targets, not on
    its diff text, so a reworded rerun of the same idea is still suppressed.

Run: python scripts/test_prompt_review.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import inspect
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.engine import eval as eval_mod  # noqa: E402
from bot.engine import prompt_review as pr  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" · {detail}" if detail else ""))
    if not ok:
        failures.append(label)


tmpdir = tempfile.TemporaryDirectory()
EVALS = Path(tmpdir.name) / "evals"
EVALS.mkdir(parents=True)
eval_mod.EVALS_DIR = EVALS


def write_eval(iid: str, flags: list[tuple[str, str]]) -> None:
    """One eval file with (category, message) flags."""
    (EVALS / f"{iid}.json").write_text(json.dumps({
        "instance_id": iid,
        "repo": "bot",
        "origin": "direct",
        "mode": "build",
        "flags": [
            {"category": c, "severity": "warning", "message": m, "evidence": ""}
            for c, m in flags
        ],
        "metrics": {"cache_hit_rate": 0.5, "resumed_session": True},
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")


# --------------------------------------------- 1. the retired check is retired

print()
print("a withdrawn check stops counting, but its files stay readable")

check("the tool-hygiene check itself is gone",
      not hasattr(eval_mod, "_check_tool_hygiene"))
check("its regexes are gone too",
      not hasattr(eval_mod, "_READ_CMD_RE") and not hasattr(eval_mod, "_SEARCH_CMD_RE"))
check("tool_hygiene is marked retired", eval_mod.is_retired_flag("tool_hygiene"))
check("a live category is not", not eval_mod.is_retired_flag("efficiency"))

# Five sessions, each carrying a mountain of retired flags and one real one.
for n in range(5):
    write_eval(f"q-{n:03d}", [("tool_hygiene", "Bash used for search")] * 40
               + [("constraint_violation", "Over-long response (5,000 chars)")])

digest = eval_mod.build_digest(days=7, min_count=3)
messages = [r.message for r in digest.rows]
check("the digest reads all five sessions", digest.sessions == 5, str(digest.sessions))
check("no retired flag reaches the digest",
      not any("Bash used" in m for m in messages), str(messages))
check("the real finding survives",
      any("Over-long response" in m for m in messages), str(messages))
check("attribution no longer claims an owner for the retired category",
      eval_mod.attribute_flag("tool_hygiene", "Bash used for search") == "unattributed")

# The files themselves must still be readable as recorded -- a per-instance view
# rewriting history is how an old session becomes unexplainable.
raw = eval_mod.load_evals(since_hours=24)
check("load_evals still returns the flags as they were recorded",
      any(f.category == "tool_hygiene" for e in raw for f in e.flags))


# ------------------------------------------------- 2. the aggregation is bounded

print()
print("the agent sees a bounded table, never the eval directory")

for n in range(40):
    write_eval(f"z-{n:03d}", [("efficiency", f"Query took {n} turns (expected <20)"),
                              ("claim_grounding", "Response contains 3 URL(s) but WebFetch/WebSearch not used")])

text = pr.build_review_input(days=7)
check("the rendered input respects its char budget",
      len(text) <= pr._MAX_INPUT_CHARS + 200, f"{len(text)} chars")
check("rows are capped", text.count("\n- [") <= pr._MAX_ROWS, str(text.count("\n- [")))
check("owner attribution is carried through to the agent", "owner=" in text)
check("session counts are carried through", "sessions=" in text)
check("no retired flag leaks into the review input", "Bash used" not in text)

prompt = pr.build_review_prompt(text, rejected=["bot/config.py :: some old flag"])
for word in (pr.CLASS_CONTRADICTED, pr.CLASS_DISOBEYED, pr.CLASS_OBSOLETE):
    check(f"the brief defines '{word}'", word in prompt)
check("the brief forbids editing a merely-disobeyed rule",
      "REPORT ONLY" in prompt)
check("the brief biases toward deletion", "Prefer deletion" in prompt)
check("the brief states the proposal cap",
      str(config.PROMPT_REVIEW_MAX_PROPOSALS) in prompt)
check("a previous rejection is named in the brief",
      "some old flag" in prompt)
check("a no-change answer is an explicit option", pr.NO_PROPOSALS in prompt)


# ------------------------------------------ 3. only editable classes get through

print()
print("a rule that is merely disobeyed is reported, never proposed")

report = pr.parse_review(f"{pr.NO_PROPOSALS}")
check("the no-change sentinel parses as empty", report.empty)

sample = """Summary of the week.

NET LINES: +2 -19

### PROPOSAL 1
FILE: bot/config.py
FLAG: Bash used for search
CLASS: contradicted
WHY: the harness instructs this.
```diff
- old
```

### PROPOSAL 2
FILE: CLAUDE.md
FLAG: Over-long response
CLASS: disobeyed
WHY: should never become an edit.
```diff
- delete the mobile rule
```

### PROPOSAL 3
FILE: bot/config.py
CLASS: obsolete
WHY: no FLAG line, so it cannot be identified later.
"""
report = pr.parse_review(sample)
files = [p.file for p in report.proposals]
check("a contradicted finding becomes a proposal", "bot/config.py" in files)
check("a disobeyed finding does NOT", "CLAUDE.md" not in files, str(files))
check("a block missing FLAG is dropped, not guessed at", len(report.proposals) == 1,
      str(len(report.proposals)))
check("both drops are reported rather than silent", len(report.ignored) == 2,
      str(report.ignored))
check("the net line delta is carried", report.net_lines == "+2 -19",
      str(report.net_lines))

many = "\n".join(
    f"### PROPOSAL {i}\nFILE: f{i}.py\nFLAG: flag {i}\nCLASS: obsolete\nWHY: x\n"
    for i in range(config.PROMPT_REVIEW_MAX_PROPOSALS + 3)
)
capped = pr.parse_review(many)
check("the proposal cap is enforced on parse",
      len(capped.proposals) == config.PROMPT_REVIEW_MAX_PROPOSALS,
      str(len(capped.proposals)))


# ------------------------------------------------ 4. rejection survives rewording

print()
print("a rejected idea is recognised again even when reworded")

a = pr.parse_review(
    "### PROPOSAL 1\nFILE: bot/config.py\nFLAG: Over-long response\n"
    "CLASS: obsolete\nWHY: first wording\n```diff\n- a\n```\n")
b = pr.parse_review(
    "### PROPOSAL 1\nFILE: bot/config.py\nFLAG: Over-long response\n"
    "CLASS: contradicted\nWHY: completely different prose and a different diff\n"
    "```diff\n- b\n- c\n```\n")
c = pr.parse_review(
    "### PROPOSAL 1\nFILE: bot/config.py\nFLAG: a different flag entirely\n"
    "CLASS: obsolete\nWHY: x\n")
check("the same (file, flag) target hashes the same despite new prose",
      pr.fingerprint(a.proposals) == pr.fingerprint(b.proposals))
check("a different target hashes differently",
      pr.fingerprint(a.proposals) != pr.fingerprint(c.proposals))
check("the readable target names both halves",
      a.proposals[0].target() == "bot/config.py :: Over-long response")


# ------------------------------------------------- 5. the weekly gate is a clock

print()
print("the weekly gate runs off a timestamp, so a reboot cannot shift it")

now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
check("nothing is due before the interval elapses",
      not pr.should_run_now((now - timedelta(days=6)).isoformat(), now=now, interval_days=7))
check("it is due once the interval has elapsed",
      pr.should_run_now((now - timedelta(days=7, minutes=1)).isoformat(), now=now, interval_days=7))
check("a week is not skipped when the check happens late",
      pr.should_run_now((now - timedelta(days=30)).isoformat(), now=now, interval_days=7))
check("a naive timestamp is read as UTC rather than crashing",
      pr.should_run_now("2026-08-01T00:00:00", now=now, interval_days=7))
check("an unparseable timestamp is treated as due, not as forever-not-due",
      pr.should_run_now("not a date", now=now, interval_days=7))
check("no timestamp at all seeds instead of firing",
      not pr.should_run_now(None, now=now, interval_days=7))

# A reboot re-runs the same gate against the same stored value; it must give
# the same answer both times rather than depending on process uptime.
stamp = (now - timedelta(days=3)).isoformat()
check("the gate is a pure function of (stored stamp, now)",
      pr.should_run_now(stamp, now=now) == pr.should_run_now(stamp, now=now))

# Asserted on the signature and the arithmetic, not on the prose: the
# docstring says the word "tick" precisely to explain why there is no counter.
_gate_src = inspect.getsource(pr.should_run_now)
check("the gate takes the stored stamp as its input",
      "last_run_at: str | None" in _gate_src)
check("and decides by parsing it, not by counting invocations",
      "fromisoformat(last_run_at)" in _gate_src and "timedelta(days=" in _gate_src)


# ------------------------------- 6. the reviewer cannot edit what it is reviewing

print()
print("the reviewing agent runs behind the read-only floor")

from bot.discord import prompt_review as dpr  # noqa: E402

_run_src = inspect.getsource(dpr._run_review_locked)
check("the review instance is created in explore mode",
      'mode="explore"' in _run_src)
check("Bash is disabled on it, not just the edit tools",
      'bash_policy = "none"' in _run_src)
check("the baseline is clamped too, so nothing inherits a writable policy",
      'bash_policy_baseline = "none"' in _run_src)
check("the review instance never enters a worktree",
      "worktree" not in _run_src.lower())

_approve_src = inspect.getsource(dpr._open_build)
check("approve opens a build thread rather than writing files itself",
      "get_or_create_session_thread" in _approve_src
      and "git apply" not in _approve_src)
check("the approve brief tells the build not to weaken a disobeyed rule",
      "disobeyed" in dpr._APPROVE_BRIEF)

_post_src = inspect.getsource(dpr._post_outcome)
check("a re-proposed rejection is suppressed by the bot, not merely discouraged",
      "rejected_fingerprints" in _post_src)

_weekly_src = inspect.getsource(dpr.maybe_run_weekly)
check("the weekly run stamps the clock BEFORE running, never after",
      _weekly_src.rindex("_STATE_LAST_RUN") < _weekly_src.index("await run_review("))

_own_src = inspect.getsource(dpr.own_repo)
check("the target repo is resolved by path identity, never guessed",
      "list_repos()" in _own_src and "resolve()" in _own_src)


# --------------------------------- 7. the new run_instance caller does not bind

print()
print("the review's session belongs to the review, not to a thread")

_root = Path(__file__).resolve().parent.parent
_dpr_text = (_root / "bot" / "discord" / "prompt_review.py").read_text(encoding="utf-8")
check("the review runs through lifecycle.run_instance",
      "lifecycle.run_instance(" in _dpr_text)
check("and deliberately does NOT backfill a thread session",
      "backfill_thread_session" not in _dpr_text)
check("the reason is written down where the next reader will look",
      "A thread must always know its session" in _dpr_text)

_app_text = (_root / "bot" / "app.py").read_text(encoding="utf-8")
check("app.py still has exactly one direct run_instance caller",
      _app_text.count("lifecycle.run_instance(") == 1,
      str(_app_text.count("lifecycle.run_instance(")))

tmpdir.cleanup()

print()
if failures:
    print("FAIL: prompt review")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print(f"PASS: corrections are proposed, never applied ({checks} checks).")
print("      A retired check's flags stop counting but stay readable, the")
print("      agent sees a bounded table behind a read-only floor, a rule that")
print("      is merely disobeyed is reported and left alone, the weekly gate")
print("      is a persisted clock, and a rejected idea stays rejected even")
print("      when it comes back reworded.")
