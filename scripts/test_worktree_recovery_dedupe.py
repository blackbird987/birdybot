"""Regression tests for ``ClaudeRunner._select_recovery_candidates``.

Locks in the v0.92.18 spam fix: the worktree-recovery scan must emit at most
one decision per (repo, branch), skip FAILED/KILLED status, and stay quiet on
branches where any sibling has already been parked manual_recovery_needed.

Run: python scripts/test_worktree_recovery_dedupe.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.claude.runner import ClaudeRunner
from bot.claude.types import InstanceStatus


_failures: list[str] = []


def _check(actual, expected, label: str) -> None:
    if actual != expected:
        _failures.append(label)
        print(f"  FAIL: {label} — got {actual!r}, expected {expected!r}")
    else:
        print(f"  ok:   {label}")


# Minimal Instance stand-in. The real dataclass has 50+ fields; the candidate
# selector touches only six (id, branch, worktree_path, repo_path, status,
# manual_recovery_needed), so a duck-typed shim keeps the test fast and
# robust against unrelated schema churn.
@dataclass
class _FakeInst:
    id: str
    branch: str | None
    worktree_path: str | None
    repo_path: str | None
    status: InstanceStatus = InstanceStatus.COMPLETED
    manual_recovery_needed: bool = False
    created_at: str = ""  # only used to drive newest-first iteration order


@dataclass
class _FakeStore:
    # Caller passes instances already in newest-first order; this matches
    # what the real StateStore.list_instances(all_=True) returns.
    instances: list[_FakeInst] = field(default_factory=list)

    def list_instances(self, all_: bool = False) -> list[_FakeInst]:
        return list(self.instances)


def _ids(insts) -> list[str]:
    return [i.id for i in insts]


def test_chained_siblings_dedupe_to_one_event() -> None:
    """t-3714 chain: 4 instances on branch claude-bot/t-3714 → 1 candidate."""
    print("\n[chained siblings dedupe by branch]")
    siblings = [
        _FakeInst("t-3731", "claude-bot/t-3714", "/repo/.worktrees/t-3731", "/repo"),
        _FakeInst("t-3729", "claude-bot/t-3714", "/repo/.worktrees/t-3729", "/repo"),
        _FakeInst("t-3728", "claude-bot/t-3714", "/repo/.worktrees/t-3728", "/repo"),
        _FakeInst("t-3714", "claude-bot/t-3714", "/repo/.worktrees/t-3714", "/repo"),
    ]
    store = _FakeStore(siblings)
    out = ClaudeRunner._select_recovery_candidates(store)
    _check(len(out), 1, "exactly one candidate for shared branch")
    _check(out[0].id, "t-3731", "newest sibling wins (newest-first iteration)")


def test_failed_killed_skipped() -> None:
    """FAILED/KILLED do not get drift warnings — cleanup pass handles them."""
    print("\n[FAILED/KILLED skipped, COMPLETED kept]")
    instances = [
        _FakeInst("a", "claude-bot/a", "/repo/.worktrees/a", "/repo",
                  status=InstanceStatus.COMPLETED),
        _FakeInst("b", "claude-bot/b", "/repo/.worktrees/b", "/repo",
                  status=InstanceStatus.FAILED),
        _FakeInst("c", "claude-bot/c", "/repo/.worktrees/c", "/repo",
                  status=InstanceStatus.KILLED),
        _FakeInst("d", "claude-bot/d", "/repo/.worktrees/d", "/repo",
                  status=InstanceStatus.RUNNING),
        _FakeInst("e", "claude-bot/e", "/repo/.worktrees/e", "/repo",
                  status=InstanceStatus.QUEUED),
    ]
    out = ClaudeRunner._select_recovery_candidates(_FakeStore(instances))
    _check(_ids(out), ["a", "d", "e"],
           "COMPLETED+RUNNING+QUEUED kept, FAILED+KILLED dropped")


def test_completed_with_drift_is_scanned() -> None:
    """Positive lock: a single COMPLETED instance must be selected.

    Without this, a future filter tweak could silently drop COMPLETED from
    scope (the v0.92.17 status — awaiting Merge/Discard tap — that the
    recovery scan was specifically built to cover).
    """
    print("\n[COMPLETED is selected]")
    inst = _FakeInst("c1", "claude-bot/c1", "/repo/.worktrees/c1", "/repo",
                     status=InstanceStatus.COMPLETED)
    out = ClaudeRunner._select_recovery_candidates(_FakeStore([inst]))
    _check(_ids(out), ["c1"], "single COMPLETED produces one candidate")


def test_already_flagged_branch_silences_siblings() -> None:
    """If any sibling on a branch is parked, all siblings stay quiet.

    Without this, a parked t-3597 sibling would let the next-newest sibling
    fire the same drift warning on every reboot.
    """
    print("\n[flagged sibling silences whole branch]")
    instances = [
        # newest non-flagged sibling — would re-fire without cross-pass dedupe
        _FakeInst("t-3599", "claude-bot/t-3597", "/repo/.worktrees/t-3599", "/repo"),
        _FakeInst("t-3598", "claude-bot/t-3597", "/repo/.worktrees/t-3598", "/repo"),
        # oldest sibling, already parked from a prior reboot
        _FakeInst("t-3597", "claude-bot/t-3597", "/repo/.worktrees/t-3597", "/repo",
                  manual_recovery_needed=True),
        # unrelated branch — should still pass
        _FakeInst("t-9999", "claude-bot/t-9999", "/repo/.worktrees/t-9999", "/repo"),
    ]
    out = ClaudeRunner._select_recovery_candidates(_FakeStore(instances))
    _check(_ids(out), ["t-9999"],
           "only the unflagged-branch instance survives")


def test_flagged_appears_after_unflagged_in_iteration() -> None:
    """Newest-first means a NEW flag can land after we've already added an
    older sibling to candidates. The post-filter must drop it.
    """
    print("\n[flag-found-late post-filter]")
    instances = [
        # newest non-flagged on branch X — added to candidates first
        _FakeInst("new", "claude-bot/x", "/repo/.worktrees/new", "/repo"),
        # older flagged on same branch — discovered later in iteration
        _FakeInst("old", "claude-bot/x", "/repo/.worktrees/old", "/repo",
                  manual_recovery_needed=True),
    ]
    out = ClaudeRunner._select_recovery_candidates(_FakeStore(instances))
    _check(_ids(out), [], "newer non-flagged dropped by post-filter")


def test_missing_required_fields_skipped() -> None:
    """Instances without all of branch+worktree_path+repo_path are skipped."""
    print("\n[missing fields skipped]")
    instances = [
        _FakeInst("no-branch", None, "/repo/.worktrees/x", "/repo"),
        _FakeInst("no-wt", "claude-bot/x", None, "/repo"),
        _FakeInst("no-repo", "claude-bot/x", "/repo/.worktrees/x", None),
        _FakeInst("ok", "claude-bot/y", "/repo/.worktrees/y", "/repo"),
    ]
    out = ClaudeRunner._select_recovery_candidates(_FakeStore(instances))
    _check(_ids(out), ["ok"], "only fully-populated instance kept")


if __name__ == "__main__":
    test_chained_siblings_dedupe_to_one_event()
    test_failed_killed_skipped()
    test_completed_with_drift_is_scanned()
    test_already_flagged_branch_silences_siblings()
    test_flagged_appears_after_unflagged_in_iteration()
    test_missing_required_fields_skipped()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)}")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print("All tests passed.")
    sys.exit(0)
