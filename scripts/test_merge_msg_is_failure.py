"""Regression test for ``bot.claude.types.merge_msg_is_failure``.

Pins the prefix contract that classifies ``ClaudeRunner.merge_branch``
return strings. The bug this guards against: a substring match on
``"failed"`` falsely flagged successful-but-noisy merge messages (where
a trailing stash-pop rollback error contained the word "failed") as
merge failures, triggering the "Resolve with Claude" flow on branches
that had already landed on master.

Run: python scripts/test_merge_msg_is_failure.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.claude.types import merge_msg_is_failure  # noqa: E402


_failures: list[str] = []
_total = 0


def _check(label: str, msg: str, expected: bool) -> None:
    global _total
    _total += 1
    got = merge_msg_is_failure(msg)
    if got != expected:
        _failures.append(
            f"{label}: expected merge_msg_is_failure={expected}, got {got}\n  msg={msg!r}"
        )


# The bug case: merge succeeded, stash-pop rollback failed. The literal
# string from runner.py's _restore_stash path contains "failed" in the
# suffix, which the old substring check tripped on.
_check(
    "success with stash-pop rollback failed in suffix",
    "Merged into master\n"
    "ℹ️ Stashed 12 uncommitted file(s) before merge: `a.txt`\n"
    "⚠️ Stash pop conflicted AND rollback failed — your tree may "
    "contain conflict markers. Run `git status` and inspect `stash@{0}` "
    "manually before continuing.",
    expected=False,
)

# Plain success.
_check(
    "plain success",
    "Merged into master",
    expected=False,
)

# Success with auto-resolved conflicts.
_check(
    "success with auto-resolved conflicts",
    "Merged into master (auto-resolved 2 conflicts)",
    expected=False,
)

# Idempotent re-merge after a restart.
_check(
    "already merged",
    "Already merged (claude-bot/t-1234 → master)",
    expected=False,
)

# Real merge failure — the only thing that should classify as failure.
_check(
    "merge failed CONFLICT",
    "Merge failed: CONFLICT (content): Merge conflict in src/main.py",
    expected=True,
)

# Merge skipped due to leftover MERGE_HEAD. The plan intentionally
# preserves prior substring-check semantics here: flowed through as
# non-failure before, continues to flow through as non-failure.
_check(
    "merge skipped (leftover MERGE_HEAD)",
    "Merge skipped: leftover MERGE_HEAD from sibling session",
    expected=False,
)

# Degenerate states from the merge_branch entry guards. Both previously
# matched the "not failure" branch of the substring check; same now.
_check(
    "no branch to merge",
    "No branch to merge",
    expected=False,
)
_check(
    "no repo path",
    "No repo path",
    expected=False,
)


if _failures:
    print(f"FAIL ({len(_failures)} case(s)):")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)

print(f"OK — {_total} cases passed.")
sys.exit(0)
