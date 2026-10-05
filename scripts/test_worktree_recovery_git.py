"""Worktree recovery and the release scan, against a real git repository.

Three paths shell out to git and had never been run against git:

- ``recover_partial_worktrees`` (startup) and
  ``workflows._attempt_inline_worktree_recovery`` (the Build button) both
  re-register a build worktree whose ``.git/worktrees/<name>/`` metadata was
  lost while its files and branch survived (the t-3700 failure mode).  They
  now share ``ClaudeRunner._reregister_worktree_sync``.
- ``scan_orphan_release_commits`` (startup) flags a release that committed
  its ``vX.Y.Z:`` bump but died before tagging it.

The two startup passes called a module-local ``_run_capture`` that the
2026-09-09 consolidation onto ``bot.procutil.run_capture`` (85bf8f7, a4b56d4)
removed.  Each pass caught the NameError in its own ``except Exception``, so
neither crashed: recovery reported every candidate as "skipped" and the
release scan found nothing, silently.  Under the rename sat an older bug, in
both recovery paths: ``git worktree add --force <dir> <branch>``, which git
refuses whenever ``<dir>`` exists ("already exists"), and an existing
directory is the premise of the feature.  So neither had ever worked (af024a6,
2026-05-05).

Run: python scripts/test_worktree_recovery_git.py   (exit 0 on pass)
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bot.claude import runner as runner_mod
from bot.claude.runner import ClaudeRunner
from bot.claude.types import InstanceOrigin, InstanceStatus
from bot.engine import workflows
from bot.procutil import NOWND


_failures: list[str] = []
_CASES = []


def _case(fn):
    _CASES.append(fn)
    return fn


def _check(cond: bool, label: str, extra: str = "") -> None:
    print(f"  {'ok:  ' if cond else 'FAIL:'} {label}{'' if cond else f' -- {extra}'}")
    if not cond:
        _failures.append(label)


def _git(cwd, *args: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
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
    instances: list[_Inst] = field(default_factory=list)
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
    # These paths use nothing from __init__ but the repo-lock table.
    r = ClaudeRunner.__new__(ClaudeRunner)
    r._repo_locks = {}
    return r


@contextlib.contextmanager
def _tracked_temp_dirs():
    """Record every scratch dir the helper makes, so the test can prove it
    cleaned up after itself (they live in the system temp dir, not the repo)."""
    made: list[str] = []
    real = runner_mod.tempfile.mkdtemp

    def _mkdtemp(*a, **kw):
        path = real(*a, **kw)
        made.append(path)
        return path

    runner_mod.tempfile.mkdtemp = _mkdtemp
    try:
        yield made
    finally:
        runner_mod.tempfile.mkdtemp = real


@contextlib.contextmanager
def _git_step_fails(step: str):
    """Make one git subcommand inside the helper fail, the rest run for real.

    ``step`` matches the subcommand word, so "reset" hits ``git reset`` and
    "repair" hits ``git worktree repair``.
    """
    real = runner_mod.run_capture

    def _run(cmd, **kw):
        if len(cmd) > 1 and cmd[0] == "git" and step in cmd[1:3]:
            return subprocess.CompletedProcess(cmd, 1, "", f"forced {step} failure")
        return real(cmd, **kw)

    runner_mod.run_capture = _run
    try:
        yield
    finally:
        runner_mod.run_capture = real


def _make_repo(root: Path, *, relative_links: bool = False) -> tuple[Path, Path, str]:
    """A repo with a build worktree holding one commit beyond master."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    # Pinned in the repo itself, not per command: the content check under
    # test runs its own git and must apply the same line-ending rules as the
    # commits did, or on a machine with autocrlf=true every file reads as
    # drifted.
    _git(repo, "config", "core.autocrlf", "false")
    if relative_links:
        _git(repo, "config", "worktree.useRelativePaths", "true")
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


def _meta(repo: Path) -> Path:
    return repo / ".git" / "worktrees" / "t-1"


def _lose_metadata(repo: Path) -> None:
    shutil.rmtree(_meta(repo))


def _assert_recovered(repo: Path, wt: Path, branch: str, untracked: str | None) -> None:
    _check(_meta(repo).is_dir(), "metadata is back under the folder's own name")
    _check(workflows._is_worktree_live(str(repo), str(wt)), "the bot reads the worktree as live")
    listed = _git(repo, "worktree", "list", "--porcelain").replace("\\", "/").lower()
    _check(wt.as_posix().lower() in listed, "git lists the worktree at its real path", listed)
    _check(_git(wt, "rev-parse", "--abbrev-ref", "HEAD") == branch, "on its own branch")
    status = _git(wt, "status", "--porcelain")
    _check(status == (f"?? {untracked}" if untracked else ""),
           "tracked files clean" + (", untracked work kept" if untracked else ""), status)
    _git(repo, "worktree", "prune")
    _check(_meta(repo).is_dir(), "the registration survives a prune (back-link is live)")


def _assert_untouched(repo: Path, wt: Path, link_before: bytes, made: list[str]) -> None:
    _check(not _meta(repo).exists(), "the metadata it made is gone again")
    _check((wt / ".git").read_bytes() == link_before, "the folder's .git file is restored byte for byte")
    listed = _git(repo, "worktree", "list", "--porcelain").replace("\\", "/").lower()
    _check(wt.as_posix().lower() not in listed, "git does not list the worktree", listed)
    _check(made and not any(os.path.exists(p) for p in made), "scratch dir removed", repr(made))


@_case
def case_startup_recovers(root: Path) -> None:
    print("\n[startup: metadata lost, files intact -> re-registered]")
    repo, wt, branch = _make_repo(root)
    (wt / "notes.txt").write_text("untracked work\n")
    _lose_metadata(repo)
    store = _Store([_Inst("t-1", branch, str(wt), str(repo))])
    with _tracked_temp_dirs() as made:
        events = asyncio.run(_runner().recover_partial_worktrees(store, {"r": str(repo)}))
    _check([e.status for e in events] == ["recovered"], "event says recovered", repr(events))
    _assert_recovered(repo, wt, branch, "notes.txt")
    _check(made and not any(os.path.exists(p) for p in made), "scratch dir removed", repr(made))
    _check([p.name for p in (repo / ".worktrees").iterdir()] == ["t-1"],
           "nothing extra in the repo's worktree folder")


@_case
def case_relative_links(root: Path) -> None:
    # git >= 2.48 can write both halves of the link as relative paths.
    print("\n[worktree.useRelativePaths: links parsed relative to their own file]")
    try:
        repo, wt, branch = _make_repo(root, relative_links=True)
    except RuntimeError as e:
        print(f"  skip: this git cannot write relative links ({e})")
        return
    _lose_metadata(repo)
    failure = ClaudeRunner._reregister_worktree_sync(str(repo), str(wt), branch)
    _check(failure is None, "re-registered", repr(failure))
    _assert_recovered(repo, wt, branch, None)


@_case
def case_build_button_recovers(root: Path) -> None:
    print("\n[Build button: the inline recovery uses the same helper]")
    repo, wt, branch = _make_repo(root)
    _lose_metadata(repo)

    @dataclass
    class _Messenger:
        sent: list[str] = field(default_factory=list)

        async def send_text(self, _cid, text, **_kw):
            self.sent.append(text)

    @dataclass
    class _Ctx:
        runner: ClaudeRunner
        store: _Store
        messenger: _Messenger
        channel_id: str = "chan-1"

    ctx = _Ctx(_runner(), _Store(), _Messenger())
    asyncio.run(workflows._attempt_inline_worktree_recovery(
        ctx, _Inst("t-1", branch, str(wt), str(repo)),
    ))
    _check(any(t.startswith("Recovered prior build") for t in ctx.messenger.sent),
           "the thread is told the prior build was recovered", repr(ctx.messenger.sent))
    _assert_recovered(repo, wt, branch, None)


@_case
def case_drifted_is_not_touched(root: Path) -> None:
    print("\n[metadata lost, a tracked file edited -> parked, not re-registered]")
    repo, wt, branch = _make_repo(root)
    (wt / "b.txt").write_text("edited after the commit\n")
    _lose_metadata(repo)
    store = _Store([_Inst("t-1", branch, str(wt), str(repo))])
    events = asyncio.run(_runner().recover_partial_worktrees(store, {"r": str(repo)}))
    _check([e.status for e in events] == ["manual_recovery_needed"],
           "event says manual recovery needed", repr(events))
    _check(not _meta(repo).exists(), "no metadata created")
    _check((wt / "b.txt").read_text() == "edited after the commit\n", "the edit is untouched")


@_case
def case_first_step_fails(root: Path) -> None:
    print("\n[git refuses the registration -> nothing changed]")
    repo, wt, branch = _make_repo(root)
    _lose_metadata(repo)
    link_before = (wt / ".git").read_bytes()
    # Checked out in the main repo, so git refuses to check it out again.
    _git(repo, "checkout", "-q", branch)
    with _tracked_temp_dirs() as made:
        failure = ClaudeRunner._reregister_worktree_sync(str(repo), str(wt), branch)
    _check(bool(failure) and "already" in failure, "git's reason is returned", repr(failure))
    _assert_untouched(repo, wt, link_before, made)


@_case
def case_late_failure_rolls_back_then_retries(root: Path) -> None:
    for step in ("repair", "reset"):
        print(f"\n[the {step} step fails -> exact rollback, then a clean retry]")
        tmp = Path(tempfile.mkdtemp(prefix="wtrec-", dir=root))
        repo, wt, branch = _make_repo(tmp)
        _lose_metadata(repo)
        link_before = (wt / ".git").read_bytes()
        with _tracked_temp_dirs() as made, _git_step_fails(step):
            failure = ClaudeRunner._reregister_worktree_sync(str(repo), str(wt), branch)
        _check(bool(failure) and f"forced {step} failure" in failure,
               "the failing step is named", repr(failure))
        _assert_untouched(repo, wt, link_before, made)
        retry = ClaudeRunner._reregister_worktree_sync(str(repo), str(wt), branch)
        _check(retry is None, "the next attempt succeeds from the restored state", repr(retry))
        _assert_recovered(repo, wt, branch, None)


@_case
def case_release_scan(root: Path) -> None:
    print("\n[startup: release committed but never tagged -> flagged]")
    repo, wt, branch = _make_repo(root)
    untagged = _Inst("t-2", branch, str(wt), str(repo), origin=InstanceOrigin.RELEASE,
                     session_id="sess-2")
    store = _Store([untagged])
    events = asyncio.run(_runner().scan_orphan_release_commits(store, {"r": str(repo)}))
    _check([e.bumped_version for e in events] == ["v1.0.1"],
           "the untagged v1.0.1 bump is reported", repr(events))
    _check(untagged.status == InstanceStatus.FAILED, "the instance is marked failed")
    _check(store.cleared_chains == ["sess-2"], "its autopilot chain is cleared")

    print("\n[startup: release committed and tagged -> left alone]")
    _git(repo, "tag", "v1.0.1", branch)
    tagged = _Inst("t-3", branch, str(wt), str(repo), origin=InstanceOrigin.RELEASE)
    events = asyncio.run(_runner().scan_orphan_release_commits(_Store([tagged]), {"r": str(repo)}))
    _check(events == [], "nothing reported", repr(events))
    _check(tagged.status == InstanceStatus.RUNNING, "status untouched")


def _rmtree(path: str) -> None:
    def _force(func, p, _exc):  # git marks object files read-only on Windows
        os.chmod(p, 0o700)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force)
    else:
        shutil.rmtree(path, onerror=_force)


if __name__ == "__main__":
    for case in _CASES:
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
    print(f"PASS: worktree recovery against real git ({len(_CASES)} cases)")
    sys.exit(0)
