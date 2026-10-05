"""Startup recovery against a real git repository, not a stub.

Two startup passes shell out to git and had never been run against git:

- ``recover_partial_worktrees`` re-registers a build worktree whose
  ``.git/worktrees/<name>/`` metadata was lost while its files and branch
  survived (the t-3700 failure mode).
- ``scan_orphan_release_commits`` flags a release that committed its
  ``vX.Y.Z:`` bump but died before tagging it.

Both called a module-local ``_run_capture`` that the 2026-09-09 consolidation
onto ``bot.procutil.run_capture`` (85bf8f7, a4b56d4) removed.  The NameError
was caught by each pass's own ``except Exception``, so neither ever crashed:
recovery reported every candidate as "skipped" and the release scan found
nothing, silently.  Under the rename sat an older bug.  The re-register step
was ``git worktree add --force <dir> <branch>``, which git refuses whenever
``<dir>`` exists ("already exists"), and an existing directory is the premise
of the whole feature.  So it had not worked since it was written (af024a6,
2026-05-05).  Every case below drives a temporary repository end to end.

Run: python scripts/test_worktree_recovery_git.py   (exit 0 on pass)
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bot.claude.runner import ClaudeRunner
from bot.claude.types import InstanceOrigin, InstanceStatus
from bot.procutil import NOWND


_failures: list[str] = []


def _check(cond: bool, label: str, extra: str = "") -> None:
    print(f"  {'ok:  ' if cond else 'FAIL:'} {label}{'' if cond else f' -- {extra}'}")
    if not cond:
        _failures.append(label)


def _git(cwd, *args: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t",
         *args],
        cwd=str(cwd), capture_output=True, text=True, **NOWND,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


@dataclass
class _Inst:
    id: str
    branch: str
    worktree_path: str
    repo_path: str
    status: InstanceStatus = InstanceStatus.RUNNING
    origin: InstanceOrigin = InstanceOrigin.BUILD
    session_id: str | None = None
    manual_recovery_needed: bool = False
    manual_recovery_reason: str | None = None
    error: str | None = None


@dataclass
class _Store:
    instances: list[_Inst]
    updated: list[str] = field(default_factory=list)
    cleared_chains: list[str] = field(default_factory=list)

    def list_instances(self, all_: bool = False):
        return list(self.instances)

    def update_instance(self, inst) -> None:
        self.updated.append(inst.id)

    def clear_autopilot_chain(self, sid) -> None:
        self.cleared_chains.append(sid)

    def clear_chain_entry_sha(self, sid) -> None:
        pass


def _runner() -> ClaudeRunner:
    # Both passes use nothing from __init__ but the repo-lock table.
    r = ClaudeRunner.__new__(ClaudeRunner)
    r._repo_locks = {}
    return r


def _make_repo(root: Path) -> tuple[Path, Path, str]:
    """A repo with a build worktree holding one commit beyond master."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    # Pinned in the repo itself, not per command: the content check under
    # test runs its own git and must apply the same line-ending rules as the
    # commits did, or on a machine with autocrlf=true every file reads as
    # drifted.
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "tag", "v1.0.0")
    branch = "claude-bot/t-1"
    wt = repo / ".worktrees" / "t-1"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", branch)
    (wt / "b.txt").write_text("b\n")
    _git(wt, "add", "b.txt")
    _git(wt, "commit", "-q", "-m", "v1.0.1: release")
    return repo, wt, branch


def _lose_metadata(repo: Path) -> None:
    shutil.rmtree(repo / ".git" / "worktrees" / "t-1")


def case_recover_lost_metadata(root: Path) -> None:
    print("\n[metadata lost, files intact: the worktree is re-registered]")
    repo, wt, branch = _make_repo(root)
    (wt / "notes.txt").write_text("untracked work\n")
    _lose_metadata(repo)
    store = _Store([_Inst("t-1", branch, str(wt), str(repo))])
    events = asyncio.run(_runner().recover_partial_worktrees(store, {"r": str(repo)}))
    _check(len(events) == 1, "one event for the one candidate", repr(events))
    if not events:
        return
    ev = events[0]
    _check(ev.status == "recovered", "event says recovered", f"{ev.status}: {ev.detail}")
    _check((repo / ".git" / "worktrees" / "t-1").is_dir(), "metadata dir is back")
    listed = _git(repo, "worktree", "list", "--porcelain")
    _check(wt.as_posix().lower() in listed.replace("\\", "/").lower(),
           "git lists the worktree at its real path", listed)
    _check(_git(wt, "rev-parse", "--abbrev-ref", "HEAD") == branch,
           "the worktree is on its own branch")
    _check(_git(wt, "status", "--porcelain") == "?? notes.txt",
           "tracked files clean, untracked work kept",
           _git(wt, "status", "--porcelain"))
    leftovers = [p.name for p in (repo / ".worktrees").iterdir() if p.name != "t-1"]
    _check(leftovers == [], "no temporary directory left behind", repr(leftovers))
    _git(repo, "worktree", "prune")
    _check("t-1" in _git(repo, "worktree", "list"),
           "the registration survives a prune (link points at a real dir)")


def case_drifted_is_not_touched(root: Path) -> None:
    print("\n[metadata lost, a tracked file edited: parked, not re-registered]")
    repo, wt, branch = _make_repo(root)
    (wt / "b.txt").write_text("edited after the commit\n")
    _lose_metadata(repo)
    store = _Store([_Inst("t-1", branch, str(wt), str(repo))])
    events = asyncio.run(_runner().recover_partial_worktrees(store, {"r": str(repo)}))
    _check([e.status for e in events] == ["manual_recovery_needed"],
           "event says manual recovery needed", repr(events))
    _check(not (repo / ".git" / "worktrees" / "t-1").exists(), "no metadata created")
    _check((wt / "b.txt").read_text() == "edited after the commit\n", "the edit is untouched")


def case_failure_rolls_back(root: Path) -> None:
    print("\n[re-register cannot complete: half-made registration is removed]")
    repo, wt, branch = _make_repo(root)
    _lose_metadata(repo)
    # The branch is checked out in the main repo, so git refuses to check it
    # out a second time and the very first step fails.
    _git(repo, "checkout", "-q", branch)
    failure = ClaudeRunner._reregister_worktree_sync(str(repo), wt, branch)
    _check(bool(failure), "a reason is returned", repr(failure))
    _check(not (repo / ".git" / "worktrees").exists()
           or not any((repo / ".git" / "worktrees").iterdir()),
           "no stray metadata left")
    leftovers = [p.name for p in (repo / ".worktrees").iterdir() if p.name != "t-1"]
    _check(leftovers == [], "no temporary directory left behind", repr(leftovers))


def case_release_scan(root: Path) -> None:
    print("\n[release committed but never tagged: flagged at startup]")
    repo, wt, branch = _make_repo(root)
    untagged = _Inst("t-2", branch, str(wt), str(repo), origin=InstanceOrigin.RELEASE,
                     session_id="sess-2")
    store = _Store([untagged])
    events = asyncio.run(_runner().scan_orphan_release_commits(store, {"r": str(repo)}))
    _check([e.bumped_version for e in events] == ["v1.0.1"],
           "the untagged v1.0.1 bump is reported", repr(events))
    _check(untagged.status == InstanceStatus.FAILED, "the instance is marked failed")
    _check(store.cleared_chains == ["sess-2"], "its autopilot chain is cleared")

    print("\n[release committed and tagged: left alone]")
    _git(repo, "tag", "v1.0.1", branch)
    tagged = _Inst("t-3", branch, str(wt), str(repo), origin=InstanceOrigin.RELEASE)
    events = asyncio.run(_runner().scan_orphan_release_commits(_Store([tagged]), {"r": str(repo)}))
    _check(events == [], "nothing reported", repr(events))
    _check(tagged.status == InstanceStatus.RUNNING, "status untouched")


def _rmtree(path: str) -> None:
    def _onerror(func, p, _exc):  # git marks pack files read-only on Windows
        os.chmod(p, 0o700)
        func(p)
    shutil.rmtree(path, onerror=_onerror)


if __name__ == "__main__":
    for case in (case_recover_lost_metadata, case_drifted_is_not_touched,
                 case_failure_rolls_back, case_release_scan):
        tmp = tempfile.mkdtemp(prefix="wtrec-")
        try:
            case(Path(tmp))
        except Exception as e:
            _failures.append(f"{case.__name__} raised {type(e).__name__}: {e}")
            print(f"  FAIL: {case.__name__} raised {type(e).__name__}: {e}")
        finally:
            _rmtree(tmp)
    print()
    if _failures:
        print(f"FAILED: {len(_failures)}")
        for f in _failures:
            print(f"  - {f}")
        sys.exit(1)
    print("PASS: worktree recovery against real git (4 cases)")
    sys.exit(0)
