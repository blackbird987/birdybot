#!/usr/bin/env python3
"""Regression test: the run recovery steps keep their order.

``ClaudeRunner._run_impl`` used to carry its nine rescue branches inline, and
the order between them was enforced only by comments. They are now methods in
``bot/claude/runner/recovery.py``, run from one loop in ``_RECOVERY_STEPS``
order, and that order is pinned here:

  * the tuple names exactly the nine steps, each a coroutine on ClaudeRunner;
  * the missing-conversation cascade runs first;
  * thrash, memory reap and OOM kill (resume the same conversation) run
    before the context-overflow abandon, which is wrong past one attempt;
  * the context overflow runs before the model, usage and account steps,
    whose parsers can match an account rejection spelled as a compaction
    failure only after overflow has stood down on it;
  * the model limit runs before the usage limit, whose parser also matches
    the model-limit wording and would sideline the whole account;
  * the account-failure step runs last, since its no-turns heuristic matches
    nearly any failure that produced nothing;
  * ``_run_impl`` returns on a deliberate kill before the loop, and reads
    ``error_text`` once, after the OOM classification;
  * the old import paths of the moved helpers still resolve.

Run:  python scripts/test_run_recovery_order.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.claude import runner as runner_pkg  # noqa: E402
from bot.claude.runner import ClaudeRunner  # noqa: E402
from bot.claude.runner import recovery  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


EXPECTED = (
    "_recover_missing_conversation",
    "_recover_context_thrash",
    "_recover_memory_reap",
    "_recover_oom_kill",
    "_recover_context_overflow",
    "_recover_model_limit",
    "_recover_usage_limit",
    "_recover_transient",
    "_recover_account_failure",
)


def main() -> int:
    steps = recovery._RECOVERY_STEPS
    pos = {name: i for i, name in enumerate(steps)}

    print("the steps")
    check(steps == EXPECTED, f"exact order ({steps})")
    check(all(inspect.iscoroutinefunction(getattr(ClaudeRunner, n, None)) for n in steps),
          "each step is a coroutine method on ClaudeRunner")
    defined = {n for n in vars(recovery.RecoveryMixin) if n.startswith("_recover_")}
    check(defined == set(steps), f"no step defined but left out of the loop ({defined ^ set(steps)})")

    print("ordering rules")
    check(pos.get("_recover_missing_conversation") == 0, "missing conversation first")
    for same in ("_recover_context_thrash", "_recover_memory_reap", "_recover_oom_kill"):
        check(pos[same] < pos["_recover_context_overflow"], f"{same} before context overflow")
    for later in ("_recover_model_limit", "_recover_usage_limit", "_recover_account_failure"):
        check(pos["_recover_context_overflow"] < pos[later], f"context overflow before {later}")
    check(pos["_recover_model_limit"] < pos["_recover_usage_limit"], "model limit before usage limit")
    check(pos["_recover_account_failure"] == len(steps) - 1, "account failure last")

    print("the loop in _run_impl")
    src = inspect.getsource(ClaudeRunner._run_impl)
    bail = src.find("if result.killed_intentionally:")
    oom = src.find("_classify_oom_kill(")
    err = src.find("run.error_text = ")
    loop = src.find("for step in _RECOVERY_STEPS:")
    check(-1 not in (bail, oom, err, loop), "kill return, OOM classify, error_text and loop all found")
    check(bail < oom < err < loop, "kill return, then OOM classify, then error_text, then the loop")
    check(src.count("run.error_text = ") == 1, "error_text is read once")

    print("old import paths")
    check(runner_pkg._carry_forward_work_record is recovery._carry_forward_work_record,
          "_carry_forward_work_record re-exported from the package")
    check(runner_pkg._is_primary_model is recovery._is_primary_model,
          "_is_primary_model re-exported from the package")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
