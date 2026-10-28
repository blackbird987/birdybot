"""Regression test for the critical pass on proposals.

Background: three weeks of sessions (2026-10) held 91 user messages like "is
this the most proper way?", "are we just patching this one case or fixing the
app as a whole?" and "think of everything", 62 of them right after an agent's
proposal. When asked, the agent's second answer was usually the better one.
``config.CRITICAL_PASS`` makes that second pass happen unasked: every direct
turn that proposes something checks it first, shows one "Checked:" line, and
big proposals get a fresh-eyes reviewer.

This test locks the two halves:

* the block is in the "direct" step guidance, exactly once, with its retry
  question, its "Checked:" line and its fresh-eyes reviewer, and no em dash;
* ``eval._check_critical_pass`` flags a direct reply that asks for approval
  with no "Checked:" line, stays quiet on everything else (a checked
  proposal, a plain answer, an ask quoted inside a ``~~~`` body, an ask that
  is only discussed early in a long reply, non-direct origins), is wired into
  ``evaluate_instance``, and is attributed to CRITICAL_PASS.

Run: python scripts/test_critical_pass.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.claude.types import (  # noqa: E402
    Instance, InstanceOrigin, InstanceStatus, InstanceType,
)
from bot.engine import eval as ev  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" ({detail})" if detail else ""))
    if not ok:
        failures.append(label)


def inst(origin: InstanceOrigin = InstanceOrigin.DIRECT, result_file: str | None = None) -> Instance:
    return Instance(
        id="q-1", name=None, instance_type=InstanceType.QUERY, prompt="p",
        repo_name="bot", repo_path="/tmp", status=InstanceStatus.COMPLETED,
        origin=origin, result_file=result_file,
    )


def flags(text: str, origin: InstanceOrigin = InstanceOrigin.DIRECT) -> list:
    return ev._check_critical_pass(inst(origin), text)


# ---- The prompt block -----------------------------------------------------
print("CRITICAL_PASS is in the direct guidance")
direct = config.WORKFLOW_GUIDANCE["direct"]
check("block appended to the direct guidance", config.CRITICAL_PASS in direct)
check("block appears exactly once", direct.count(config.CRITICAL_PASS) == 1)
check("earlier direct guidance still there", "Do NOT end a reply by asking" in direct)
check("not injected into other steps",
      all(config.CRITICAL_PASS not in g
          for k, g in config.WORKFLOW_GUIDANCE.items() if k != "direct"))
for phrase, label in (
    ("whole app", "whole-app vs one-patch question"),
    ("too specifically", "too-specific question"),
    ("What is missing", "what's-missing question"),
    ("cleaner or more standard way", "cleaner-way question"),
    ("earlier attempts failed", "retry question"),
    ('"Checked:"', "Checked: line"),
    ("fresh-eyes", "fresh-eyes reviewer"),
    ("NOT your reasoning", "reviewer gets the proposal, not the conclusion"),
    ("Small fixes and UI tweaks skip the reviewer", "small fixes skip the reviewer"),
):
    check(f"block carries the {label}", phrase in config.CRITICAL_PASS)
check("no em dash in the block", "\u2014" not in config.CRITICAL_PASS)

print("the rendered system prompt carries it on direct turns only")
from bot.claude.runner import ClaudeRunner  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    runner = ClaudeRunner()
    runner._store = StateStore(Path(tmp) / "state.json", Path(tmp) / "results")
    runner._build_location_block = lambda i: ""  # type: ignore[method-assign]
    runner._build_master_context_block = lambda i: ""  # type: ignore[method-assign]
    rendered = runner._build_system_prompt(inst())
    check("direct prompt carries the block once",
          rendered.count(config.CRITICAL_PASS) == 1)
    check("it sits under 'Your Role in This Step'",
          rendered.find("--- Your Role in This Step ---")
          < rendered.find(config.CRITICAL_PASS))
    build = runner._build_system_prompt(inst(InstanceOrigin.BUILD))
    check("build prompt does not carry it", config.CRITICAL_PASS not in build)


# ---- The eval flag --------------------------------------------------------
print("eval flags an unchecked approval ask")
UNCHECKED = """Here's the plan.

**Phases:** a multi-phase build runs as one chain.

Say go and I'll start the build."""
hit = flags(UNCHECKED)
check("unchecked 'say go' flagged once", len(hit) == 1, str(hit))
check("category is critical_pass", bool(hit) and hit[0].category == "critical_pass")
check("evidence quotes the ask", bool(hit) and "go" in hit[0].evidence.lower())
check("attributed to CRITICAL_PASS",
      bool(hit) and ev.attribute_flag(hit[0].category, hit[0].message) == "CRITICAL_PASS")
for ask in ('Say "go" for P1.', "Want me to start the build?",
            "Shall I ship it?", "Should I proceed with phase two?"):
    check(f"ask form flagged: {ask}", len(flags(f"Plan.\n\n{ask}")) == 1)

print("eval stays quiet where it should")
check("a Checked: line clears it",
      flags(UNCHECKED.replace("Say go", "Checked: a per-coin special case, "
                              "dropped it for a shared rule.\n\nSay go")) == [])
check("a bold **Checked:** line clears it",
      flags("Plan.\n\n**Checked:** whole app, not one coin.\n\nSay go.") == [])
check("a bulleted Checked line clears it",
      flags("Plan.\n\n- Checked: retry cause.\n\nSay go.") == [])
check("a reply that asks nothing is quiet",
      flags("Fixed the parser. The suite is green.") == [])
check("an ask that only lives inside a ~~~ body is quiet",
      flags("Starting the build.\n\n[BOT_CMD: /chain preset=ship]\n~~~plan\n"
            "Say go and I'll start the build.\n~~~") == [])
check("a trailing directive does not hide a real ask",
      len(flags("Plan.\n\nSay go and I'll start.\n\n[BOT_CMD: /wake delay=60]\n"
                "~~~wake\nre-check\n~~~")) == 1)
check("an ask only discussed early in a long reply is quiet",
      flags("Your replies often end in 'say go'.\n\nFirst point.\n\n"
            "Second point.\n\nThird point.\n\nThat is the whole finding.") == [])
for origin in (InstanceOrigin.BUILD, InstanceOrigin.REVIEW_CODE, InstanceOrigin.PLAN):
    check(f"non-direct origin quiet: {origin.value}", flags(UNCHECKED, origin) == [])

print("evaluate_instance runs the check")
with tempfile.TemporaryDirectory() as tmp:
    result = Path(tmp) / "q-1.md"
    result.write_text(UNCHECKED, encoding="utf-8")
    saved_dir, saved_enabled = ev.EVALS_DIR, config.EVAL_ENABLED
    ev.EVALS_DIR = Path(tmp) / "evals"
    config.EVAL_ENABLED = True
    try:
        session = ev.evaluate_instance(inst(result_file=str(result)))
    finally:
        ev.EVALS_DIR, config.EVAL_ENABLED = saved_dir, saved_enabled
    check("evaluate_instance records the critical_pass flag",
          any(f.category == "critical_pass" for f in session.flags),
          str([f.category for f in session.flags]))


if failures:
    print(f"\n{len(failures)} of {checks} check(s) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"\nAll {checks} checks passed.")
