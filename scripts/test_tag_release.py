"""Regression tests for ``ClaudeRunner._tag_release_at`` /
``_release_commit_window``.

Locks in the post-merge tag-deferral fix: the bot must create the
``vX.Y.Z`` tag from the release commit's subject line *after* the merge
to master, never inside the worktree before merge. This eliminates the
orphan-tag failure class where a Discarded/abandoned bot branch leaves
a tag pointing at an unreachable commit.

Run: python scripts/test_tag_release.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.claude.runner import ClaudeRunner, _NOWND  # noqa: E402


_failures: list[str] = []


def _check(actual, expected, label: str) -> None:
    if actual != expected:
        _failures.append(label)
        print(f"  FAIL: {label} - got {actual!r}, expected {expected!r}")
    else:
        print(f"  ok:   {label}")


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, **_NOWND,
    )
    if check and r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}: {r.stderr.strip()}"
        )
    return r


def _init_repo(repo: str) -> None:
    """Create a repo with one commit on master and identity configured."""
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "tester")
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("init\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")


def _commit(repo: str, message: str, *, file: str = "f.txt") -> str:
    """Create a commit with the given subject; returns the SHA."""
    path = os.path.join(repo, file)
    with open(path, "a") as f:
        f.write(message + "\n")
    _git(repo, "add", file)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _merge_branch(repo: str, branch: str) -> None:
    """Reproduce the bot's `git merge --no-ff` so HEAD^2 is the branch tip."""
    _git(repo, "checkout", "-q", "master")
    _git(repo, "merge", "--no-ff", "-q", "-m", f"Merge {branch}", branch)


def _setup_runner() -> ClaudeRunner:
    return ClaudeRunner()


def test_a_tip_is_release_commit_4part() -> None:
    """(a) Branch tip IS the release commit, 4-part version."""
    print("\n[a] tip is release commit (4-part vX.Y.Z.W)")
    runner = _setup_runner()
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        _git(tmp, "checkout", "-q", "-b", "claude-bot/t-x")
        _commit(tmp, "build: do thing")
        release_sha = _commit(tmp, "v1.3.0.255: Release thing")
        _merge_branch(tmp, "claude-bot/t-x")
        tag, shas = runner._tag_release_at(tmp, "HEAD^2")
        _check(tag, "v1.3.0.255", "tag created on 4-part version")
        _check(release_sha in shas, True, "release sha in candidate window")
        rev = _git(tmp, "rev-parse", "v1.3.0.255").stdout.strip()
        _check(rev, release_sha, "tag points at release commit")


def test_b_cleanup_commit_after_release() -> None:
    """(b) LLM committed cleanup AFTER the release commit; walk-back finds it."""
    print("\n[b] cleanup commit after release commit (walk-back)")
    runner = _setup_runner()
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        _git(tmp, "checkout", "-q", "-b", "claude-bot/t-x")
        release_sha = _commit(tmp, "v0.9.1: cut")
        cleanup_sha = _commit(tmp, "tidy up after release")
        _merge_branch(tmp, "claude-bot/t-x")
        tag, shas = runner._tag_release_at(tmp, "HEAD^2")
        _check(tag, "v0.9.1", "tag created on release commit, not cleanup tip")
        rev = _git(tmp, "rev-parse", "v0.9.1").stdout.strip()
        _check(rev, release_sha, "tag points at release commit, not cleanup")
        _check(release_sha in shas and cleanup_sha in shas, True,
               "candidate window includes both commits")


def test_c_idempotent_when_tag_already_at_target() -> None:
    """(c) Tag already exists at the right commit -> idempotent return."""
    print("\n[c] tag already at target commit (idempotent)")
    runner = _setup_runner()
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        _git(tmp, "checkout", "-q", "-b", "claude-bot/t-x")
        release_sha = _commit(tmp, "v2.0.0: big")
        _git(tmp, "tag", "v2.0.0", release_sha)
        _merge_branch(tmp, "claude-bot/t-x")
        tag, _shas = runner._tag_release_at(tmp, "HEAD^2")
        _check(tag, "v2.0.0", "idempotent - returns tag name")
        rev = _git(tmp, "rev-parse", "v2.0.0").stdout.strip()
        _check(rev, release_sha, "tag still points at original commit")


def test_d_tag_exists_elsewhere_no_clobber() -> None:
    """(d) Tag with same name already points at a DIFFERENT commit -> no clobber."""
    print("\n[d] tag exists pointing elsewhere (no clobber)")
    runner = _setup_runner()
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        master_sha = _git(tmp, "rev-parse", "HEAD").stdout.strip()
        _git(tmp, "tag", "v3.0.0", master_sha)
        _git(tmp, "checkout", "-q", "-b", "claude-bot/t-x")
        _commit(tmp, "v3.0.0: collide")
        _merge_branch(tmp, "claude-bot/t-x")
        tag, _shas = runner._tag_release_at(tmp, "HEAD^2")
        _check(tag, None, "no clobber - returns None on conflict")
        rev = _git(tmp, "rev-parse", "v3.0.0").stdout.strip()
        _check(rev, master_sha, "tag unchanged after conflict skip")


def test_e_no_release_commit_in_window() -> None:
    """(e) Branch has commits but none match release-subject pattern."""
    print("\n[e] no release commit in branch-unique range")
    runner = _setup_runner()
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        _git(tmp, "checkout", "-q", "-b", "claude-bot/t-x")
        a = _commit(tmp, "feat: thing")
        b = _commit(tmp, "fix: other")
        _merge_branch(tmp, "claude-bot/t-x")
        tag, shas = runner._tag_release_at(tmp, "HEAD^2")
        _check(tag, None, "no tag - no matching subject")
        _check(a in shas and b in shas, True,
               "candidate shas still populated for push-block to walk")


def test_g_annotated_tag_at_target() -> None:
    """(g) Annotated tag already at the right commit -> idempotent.

    Regression for the rev-parse refs/tags/<name> trap: without
    ^{commit} dereferencing, an annotated tag's object SHA never
    equals the commit SHA, falsely tripping no-clobber on a tag that
    is already correct.
    """
    print("\n[g] annotated tag already at release commit (deref)")
    runner = _setup_runner()
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        _git(tmp, "checkout", "-q", "-b", "claude-bot/t-x")
        release_sha = _commit(tmp, "v4.0.0: annotated")
        _git(tmp, "tag", "-a", "v4.0.0", "-m", "annotated", release_sha)
        _merge_branch(tmp, "claude-bot/t-x")
        tag, _shas = runner._tag_release_at(tmp, "HEAD^2")
        _check(tag, "v4.0.0", "annotated tag at right commit -> idempotent")
        rev = _git(tmp, "rev-parse", "v4.0.0^{commit}").stdout.strip()
        _check(rev, release_sha, "annotated tag still points at release commit")


def test_f_empty_window() -> None:
    """(f) Range is empty (degenerate - branch_tip == HEAD^1)."""
    print("\n[f] empty branch-unique window")
    runner = _setup_runner()
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        head_sha = _git(tmp, "rev-parse", "HEAD").stdout.strip()
        _commit(tmp, "second")
        # branch_tip == HEAD^1 -> range HEAD^1..HEAD^1 is empty.
        tag, shas = runner._tag_release_at(tmp, head_sha)
        _check(tag, None, "no tag for empty window")
        _check(shas, [], "empty candidate sha list")


def main() -> int:
    test_a_tip_is_release_commit_4part()
    test_b_cleanup_commit_after_release()
    test_c_idempotent_when_tag_already_at_target()
    test_d_tag_exists_elsewhere_no_clobber()
    test_e_no_release_commit_in_window()
    test_g_annotated_tag_at_target()
    test_f_empty_window()
    print()
    if _failures:
        print(f"FAILED {len(_failures)} check(s):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
