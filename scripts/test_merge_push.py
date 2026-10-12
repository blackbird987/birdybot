#!/usr/bin/env python3
"""Harness for the whole merge path, push half included, against a real remote.

``ClaudeRunner._merge_branch_sync`` merges a build branch, removes its
worktree, tags the release, pushes the target and the tags, and restores a
stash it made of the main repo's uncommitted files. Every other merge harness
drives one piece of it. This one builds a throwaway repo with a bare
``origin`` and runs the real function end to end:

- a release merge pushes the target and the release tag, and says so
- uncommitted files in the main repo are stashed, named and restored
- untracked files alone make no stash, and nobody else's entry is popped
- a stash another session pushes on top mid-merge is left where it is
- a repo with no ``origin`` merges locally and says that is fine
- an origin that moved ahead is fast-forwarded to before merging
- a diverged target refuses the merge and leaves the branch usable
- a rejected push is reported with git's own reason, after a good merge
- a branch already gone reports "Already merged" and removes its worktree

Run: python scripts/test_merge_push.py
Exit 0 = all pass.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config  # noqa: E402
from bot.claude.runner import ClaudeRunner  # noqa: E402
from bot.claude.types import Instance, InstanceStatus, InstanceType  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def git(cwd: str, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, **config.NOWND)
    return r.stdout.strip()


def commit(cwd: str, name: str, body: str, msg: str) -> None:
    Path(cwd, name).write_text(body, encoding="utf-8")
    git(cwd, "add", "-A")
    git(cwd, "commit", "-q", "-m", msg)


def fixture(tmp: str, *, remote: bool = True) -> tuple[str, str | None]:
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "master")
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "T")
    commit(repo, "CHANGELOG.md", "# Changelog\n\n## [Unreleased]\n", "base")
    commit(repo, "app.py", "VALUE = 0\n", "v0.1.0: First")
    commit(repo, "notes.txt", "base\n", "Notes")
    git(repo, "tag", "v0.1.0")
    origin = None
    if remote:
        origin = os.path.join(tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "master", origin],
                       capture_output=True, **config.NOWND)
        git(repo, "remote", "add", "origin", origin)
        git(repo, "push", "-q", "origin", "master", "--tags")
    return repo, origin


def build_branch(tmp: str, repo: str, inst_id: str, *, release: str | None = None) -> Instance:
    branch = f"{config.BRANCH_PREFIX}/{inst_id}"
    wt = os.path.join(tmp, f"wt-{inst_id}")
    git(repo, "worktree", "add", "-q", "-b", branch, wt)
    commit(wt, "feature.py", f"FEATURE = '{inst_id}'\n", "Add feature")
    if release:
        commit(wt, "app.py", "VALUE = 1\n", f"{release}: Feature")
    return Instance(
        id=inst_id, name=None, instance_type=InstanceType.TASK,
        prompt="", repo_name="r", repo_path=repo,
        status=InstanceStatus.COMPLETED, mode="build",
        branch=branch, original_branch="master", worktree_path=wt,
    )


def test_release_push_and_stash() -> None:
    runner = ClaudeRunner()
    with tempfile.TemporaryDirectory() as tmp:
        repo, origin = fixture(tmp)
        inst = build_branch(tmp, repo, "t-rel", release="v0.2.0")
        wt, branch = inst.worktree_path, inst.branch
        Path(repo, "notes.txt").write_text("mine\n", encoding="utf-8")
        msg = runner._merge_branch_sync(inst)
        check("a release merge reports success", msg.startswith("Merged into master"), msg)
        check("the target reached origin",
              git(repo, "rev-parse", "master") == git(origin, "rev-parse", "master"), msg)
        check("the release tag reached origin", "v0.2.0" in git(origin, "tag", "-l"), msg)
        check("the pushed tags are named", "Tags pushed: v0.2.0" in msg, msg)
        check("the stashed file is named", "Stashed 1 uncommitted file(s)" in msg and "`notes.txt`" in msg, msg)
        check("the stashed file is back", Path(repo, "notes.txt").read_text(encoding="utf-8") == "mine\n")
        check("the stash list is empty again", not git(repo, "stash", "list"))
        check("the worktree is removed", not Path(wt).exists())
        check("the branch is deleted", not git(repo, "branch", "--list", branch))
        check("the instance forgets its branch", inst.branch is None and inst.worktree_path is None)
        check("no merge failure kind is recorded", inst.id not in runner._last_merge_failure_kind)


def test_untracked_only_pops_nothing() -> None:
    runner = ClaudeRunner()
    with tempfile.TemporaryDirectory() as tmp:
        repo, _ = fixture(tmp)
        Path(repo, "notes.txt").write_text("someone else's\n", encoding="utf-8")
        git(repo, "stash", "push", "-q", "-m", "foreign")
        inst = build_branch(tmp, repo, "t-untracked")
        Path(repo, "scratch.txt").write_text("untracked\n", encoding="utf-8")
        msg = runner._merge_branch_sync(inst)
        check("untracked files alone merge cleanly", msg.startswith("Merged into master"), msg)
        check("and are not reported as stashed", "Stashed" not in msg, msg)
        check("the untracked file is untouched", Path(repo, "scratch.txt").exists())
        check("another session's stash is not popped",
              "foreign" in git(repo, "stash", "list")
              and Path(repo, "notes.txt").read_text(encoding="utf-8") == "base\n", msg)


def test_foreign_stash_on_top() -> None:
    runner = ClaudeRunner()
    with tempfile.TemporaryDirectory() as tmp:
        repo, _ = fixture(tmp)
        inst = build_branch(tmp, repo, "t-race")
        Path(repo, "notes.txt").write_text("mine\n", encoding="utf-8")
        real = runner._ensure_union_merge_driver

        def push_foreign(r: str) -> None:
            Path(r, "app.py").write_text("VALUE = 'foreign'\n", encoding="utf-8")
            git(r, "stash", "push", "-q", "-m", "foreign")
            real(r)

        runner._ensure_union_merge_driver = push_foreign
        msg = runner._merge_branch_sync(inst)
        check("a stash pushed on top mid-merge does not stop the merge", msg.startswith("Merged into master"), msg)
        check("our own stash is the one restored", Path(repo, "notes.txt").read_text(encoding="utf-8") == "mine\n", msg)
        check("the foreign entry is left in the list", "foreign" in git(repo, "stash", "list"), msg)
        check("and its change is not applied", "foreign" not in Path(repo, "app.py").read_text(encoding="utf-8"), msg)


def test_no_remote() -> None:
    runner = ClaudeRunner()
    with tempfile.TemporaryDirectory() as tmp:
        repo, _ = fixture(tmp, remote=False)
        inst = build_branch(tmp, repo, "t-local")
        msg = runner._merge_branch_sync(inst)
        check("a repo with no origin merges", msg.startswith("Merged into master"), msg)
        check("and says the local merge is fine", "No remote configured" in msg, msg)


def test_fast_forward_first() -> None:
    runner = ClaudeRunner()
    with tempfile.TemporaryDirectory() as tmp:
        repo, origin = fixture(tmp)
        other = os.path.join(tmp, "other")
        subprocess.run(["git", "clone", "-q", origin, other], capture_output=True, **config.NOWND)
        git(other, "config", "user.email", "o@example.invalid")
        git(other, "config", "user.name", "O")
        commit(other, "remote.py", "R = 1\n", "Landed elsewhere first")
        git(other, "push", "-q", "origin", "master")
        inst = build_branch(tmp, repo, "t-ff")
        msg = runner._merge_branch_sync(inst)
        check("an origin that moved ahead still merges", msg.startswith("Merged into master"), msg)
        check("the remote commit is in the merged target", Path(repo, "remote.py").exists(), msg)
        check("and the result was pushed",
              git(repo, "rev-parse", "master") == git(origin, "rev-parse", "master"), msg)


def test_diverged() -> None:
    runner = ClaudeRunner()
    with tempfile.TemporaryDirectory() as tmp:
        repo, origin = fixture(tmp)
        other = os.path.join(tmp, "other")
        subprocess.run(["git", "clone", "-q", origin, other], capture_output=True, **config.NOWND)
        git(other, "config", "user.email", "o@example.invalid")
        git(other, "config", "user.name", "O")
        commit(other, "remote.py", "R = 1\n", "Remote only")
        git(other, "push", "-q", "origin", "master")
        commit(repo, "local.py", "L = 1\n", "Local only")
        inst = build_branch(tmp, repo, "t-div")
        branch = inst.branch
        Path(repo, "notes.txt").write_text("mine\n", encoding="utf-8")
        msg = runner._merge_branch_sync(inst)
        check("a diverged target refuses the merge", msg.startswith("Merge failed: local master"), msg)
        check("and gives the way out", "git pull --rebase" in msg, msg)
        check("the branch survives for a retry", bool(git(repo, "branch", "--list", branch)))
        check("the stash is restored on failure", Path(repo, "notes.txt").exists(), msg)
        check("the failure is classified as diverged",
              runner._last_merge_failure_kind.get(inst.id) == "diverged",
              str(runner._last_merge_failure_kind))


def test_push_rejected() -> None:
    runner = ClaudeRunner()
    with tempfile.TemporaryDirectory() as tmp:
        repo, origin = fixture(tmp)
        hook = Path(origin, "hooks", "pre-receive")
        hook.write_text("#!/bin/sh\necho 'policy says no' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        inst = build_branch(tmp, repo, "t-rej", release="v0.2.0")
        msg = runner._merge_branch_sync(inst)
        check("a rejected push still reports the merge", msg.startswith("Merged into master"), msg)
        check("the push failure is reported", "Could not push to origin" in msg, msg)
        check("no tags are claimed as pushed", "Tags pushed" not in msg, msg)
        check("the merged target stays local", git(repo, "rev-parse", "master") != git(origin, "rev-parse", "master"))


def test_already_merged() -> None:
    runner = ClaudeRunner()
    with tempfile.TemporaryDirectory() as tmp:
        repo, _ = fixture(tmp)
        inst = build_branch(tmp, repo, "t-gone")
        wt = inst.worktree_path
        git(repo, "worktree", "remove", "--force", wt)
        git(repo, "worktree", "add", "-q", "--detach", wt)
        git(repo, "branch", "-D", inst.branch)
        msg = runner._merge_branch_sync(inst)
        check("a branch already gone reports Already merged", msg.startswith("Already merged"), msg)
        check("and still removes its worktree", not Path(wt).exists())


def main() -> int:
    test_release_push_and_stash()
    test_untracked_only_pops_nothing()
    test_foreign_stash_on_top()
    test_no_remote()
    test_fast_forward_first()
    test_diverged()
    test_push_rejected()
    test_already_merged()
    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
