"""Focused probe for _restore_stash filter narrowing (t-4016).

Sets up an ephemeral git repo that reproduces the q-8035 scenario:
- a tracked change is stashed
- an untracked file is present in the working tree
- _restore_stash is invoked

Pre-fix behavior: skips pop ("tree not clean") -> tracked edits stranded.
Post-fix behavior: ignores untracked, pops cleanly, tracked change restored.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Make bot/ importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.claude.runner import ClaudeRunner  # noqa: E402


def _git(cwd: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
    )
    if check and r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: rc={r.returncode}\n"
            f"stdout={r.stdout}\nstderr={r.stderr}"
        )
    return r


def _stash_list_count(repo: str) -> int:
    r = _git(repo, "stash", "list")
    return sum(1 for line in r.stdout.splitlines() if line.strip())


def _file_contents(repo: str, name: str) -> str:
    return (Path(repo) / name).read_text()


def run_scenario_a_untracked_only() -> tuple[bool, str]:
    """Tracked change stashed + untracked file present. Pop must succeed."""
    repo = tempfile.mkdtemp(prefix="verify-restore-stash-")
    try:
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        (Path(repo) / "f.txt").write_text("base\n")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", "base")

        # Make tracked change, stash it
        (Path(repo) / "f.txt").write_text("modified\n")
        _git(repo, "stash", "push", "-q", "-m", "test-stash")

        # Create an untracked file (the q-8035 trigger condition)
        (Path(repo) / "untracked.txt").write_text("unrelated\n")

        # Sanity-check the precondition
        porcelain = _git(repo, "status", "--porcelain").stdout
        if "?? untracked.txt" not in porcelain:
            return False, f"precondition wrong, porcelain:\n{porcelain}"
        if _stash_list_count(repo) != 1:
            return False, "precondition wrong, expected 1 stash"

        # Invoke the function under test (sync method, safe to call directly)
        runner = ClaudeRunner.__new__(ClaudeRunner)  # bypass __init__
        msg = runner._restore_stash(repo)

        # Verify outcome
        post_stash_count = _stash_list_count(repo)
        post_content = _file_contents(repo, "f.txt").strip()

        if post_stash_count != 0:
            return False, (
                f"FAIL: stash NOT popped (count={post_stash_count}). "
                f"Status string: {msg!r}"
            )
        if post_content != "modified":
            return False, (
                f"FAIL: tracked change NOT restored. "
                f"f.txt content={post_content!r}. Status: {msg!r}"
            )
        if "restored" not in msg.lower():
            return False, f"FAIL: status string unexpected: {msg!r}"
        return True, f"OK: stash popped cleanly. Status: {msg.strip()!r}"
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def run_scenario_b_tracked_dirty_blocks() -> tuple[bool, str]:
    """Tracked change present in worktree -> pop must be skipped."""
    repo = tempfile.mkdtemp(prefix="verify-restore-stash-")
    try:
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        (Path(repo) / "a.txt").write_text("base-a\n")
        (Path(repo) / "b.txt").write_text("base-b\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "base")

        # Stash a change to a.txt
        (Path(repo) / "a.txt").write_text("stashed-a\n")
        _git(repo, "stash", "push", "-q", "-m", "test-stash")

        # Now introduce a TRACKED dirty state on b.txt (must block pop)
        (Path(repo) / "b.txt").write_text("dirty-b\n")

        runner = ClaudeRunner.__new__(ClaudeRunner)
        msg = runner._restore_stash(repo)

        post_stash_count = _stash_list_count(repo)
        if post_stash_count != 1:
            return False, (
                f"FAIL: stash should be preserved when tracked-dirty, "
                f"but count={post_stash_count}. Status: {msg!r}"
            )
        if "not auto-restored" not in msg:
            return False, f"FAIL: expected skip-message, got: {msg!r}"
        return True, f"OK: skipped pop, stash preserved. Status: {msg.strip()!r}"
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def run_scenario_c_clean_tree() -> tuple[bool, str]:
    """No untracked + no tracked dirty -> pop succeeds (regression guard)."""
    repo = tempfile.mkdtemp(prefix="verify-restore-stash-")
    try:
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        (Path(repo) / "f.txt").write_text("base\n")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-q", "-m", "base")

        (Path(repo) / "f.txt").write_text("changed\n")
        _git(repo, "stash", "push", "-q", "-m", "test-stash")

        runner = ClaudeRunner.__new__(ClaudeRunner)
        msg = runner._restore_stash(repo)

        if _stash_list_count(repo) != 0:
            return False, f"FAIL: stash not popped. Status: {msg!r}"
        if "restored" not in msg.lower():
            return False, f"FAIL: status: {msg!r}"
        return True, f"OK: clean tree, popped cleanly. Status: {msg.strip()!r}"
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def main() -> int:
    scenarios = [
        ("A: untracked-only (q-8035 scenario)", run_scenario_a_untracked_only),
        ("B: tracked-dirty blocks pop", run_scenario_b_tracked_dirty_blocks),
        ("C: clean tree pops (regression guard)", run_scenario_c_clean_tree),
    ]
    all_pass = True
    for name, fn in scenarios:
        ok, detail = fn()
        marker = "PASS" if ok else "FAIL"
        print(f"[{marker}] {name}: {detail}")
        if not ok:
            all_pass = False
    print()
    print("RESULT:", "PASS" if all_pass else "FAIL")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
