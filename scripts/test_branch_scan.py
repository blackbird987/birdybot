"""Regression tests for branch-name canonicalization.

Locks in the t-3700 fix: ``git branch --list`` decoration prefixes
(``+ `` for linked-worktree branches, ``* `` for HEAD) must be stripped
before the orphan-cleanup membership check, or every active build with a
worktree gets misclassified as orphan.

Run: python scripts/test_branch_scan.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.claude.branch_utils import canonical_branch


_failures: list[str] = []


def _check(actual, expected, label: str) -> None:
    if actual != expected:
        _failures.append(label)
        print(f"  FAIL: {label} — got {actual!r}, expected {expected!r}")
    else:
        print(f"  ok:   {label}")


def test_strip_decorations() -> None:
    print("\n[strip decorations]")
    cases = [
        # (input, expected, label)
        ("claude-bot/t-001", "claude-bot/t-001", "plain branch"),
        ("* claude-bot/t-002", "claude-bot/t-002", "current-HEAD * prefix"),
        ("+ claude-bot/t-003", "claude-bot/t-003", "linked-worktree + prefix (the bug)"),
        ("  claude-bot/t-004  ", "claude-bot/t-004", "leading/trailing whitespace"),
        ("*  claude-bot/t-005", "claude-bot/t-005", "* with extra space"),
        ("+  claude-bot/t-006", "claude-bot/t-006", "+ with extra space"),
    ]
    for inp, exp, label in cases:
        _check(canonical_branch(inp), exp, label)


def test_reject_invalid() -> None:
    print("\n[reject invalid]")
    cases = [
        (None, None, "None"),
        ("", None, "empty string"),
        ("   ", None, "whitespace only"),
        ("(HEAD detached at abc1234)", None, "detached HEAD placeholder"),
        ("+ ", None, "decoration with no name"),
        ("foo bar", None, "internal whitespace (parser noise)"),
        ("a\tb", None, "internal tab"),
    ]
    for inp, exp, label in cases:
        _check(canonical_branch(inp), exp, label)


def test_membership_check() -> None:
    """The actual t-3700 failure scenario as a single assertion."""
    print("\n[membership check — t-3700 scenario]")
    # Simulated for-each-ref output for the bot's branch glob — non-prefix
    # branches like master never reach this list (filtered by the glob).
    raw_lines = [
        "claude-bot/t-001",         # active build, current HEAD in this repo
        "+ claude-bot/t-002",       # linked-worktree decorated (the buggy case)
        "claude-bot/t-003",         # plain orphan (no worktree)
    ]
    # State.json's view of who's active:
    active = {"claude-bot/t-001", "claude-bot/t-002"}

    canon = [c for c in (canonical_branch(s) for s in raw_lines) if c]
    canon_active = {canonical_branch(b) for b in active}
    orphans = [b for b in canon if b not in canon_active]

    # Pre-fix this list contained "+ claude-bot/t-002" because membership
    # was case-sensitive, decoration-included. Post-fix only t-003 remains.
    _check(orphans, ["claude-bot/t-003"], "only true orphan classified as orphan")
    _check(
        "claude-bot/t-002" not in orphans,
        True,
        "linked-worktree branch is NOT classified as orphan",
    )


def main() -> int:
    test_strip_decorations()
    test_reject_invalid()
    test_membership_check()

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s) failed")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("OK: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
