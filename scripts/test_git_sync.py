#!/usr/bin/env python3
"""Regression test: the Control Room's Sync Git sequence.

The git half of the Sync Git button used to live inside the Discord handler,
interleaved with follow-up messages, and had no test at all. It is now
``bot.engine.git_sync.sync_repo``, which returns the line to show and whether
to redraw the Control Room. Pinned here against real throwaway repos:

  * in sync, ahead (push), behind (fast-forward pull) and a new tag;
  * a pull updates the deploy state, and queues a reboot only for a
    self-managed repo, with the deferred wording when the reboot defers;
  * a dirty tree, a missing upstream and an unknown repo stop early and do
    not ask for a redraw;
  * an exception becomes a "Git sync failed" line, never a raise.

Run:  python scripts/test_git_sync.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.claude.runner import RebootResult  # noqa: E402
from bot.engine.git_sync import sync_repo  # noqa: E402
from bot.store.records import DeployState  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)


def setup(root: Path) -> tuple[Path, Path]:
    """A bare origin, the repo under test, and a second clone to push from."""
    origin = root / "origin.git"
    git(root, "init", "-q", "--bare", "-b", "master", str(origin))
    repo = root / "repo"
    git(root, "clone", "-q", str(origin), str(repo))
    commit(repo, "a")
    git(repo, "push", "-q", "-u", "origin", "master")
    other = root / "other"
    git(root, "clone", "-q", str(origin), str(other))
    return repo, other


class _Store:
    def __init__(self, repos: dict[str, str], ds: DeployState | None = None) -> None:
        self.repos = repos
        self.ds = ds
        self.saved: list[DeployState] = []

    def list_repos(self) -> dict[str, str]:
        return dict(self.repos)

    def get_deploy_state(self, name: str) -> DeployState | None:
        return self.ds

    def set_deploy_state(self, name: str, ds: DeployState) -> None:
        self.saved.append(ds)


class _Runner:
    def __init__(self, result: RebootResult = RebootResult.QUEUED) -> None:
        self.result = result
        self.requests: list[dict] = []

    def request_reboot(self, req: dict):  # noqa: ANN201
        self.requests.append(req)
        return self.result


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, other = setup(root)
        store = _Store({"r": str(repo)})

        print("unknown repo")
        text, refresh = await sync_repo(store, _Runner(), "nope", "c")
        check(text == "Repo `nope` not found." and not refresh, f"refused ({text!r})")

        print("in sync")
        text, refresh = await sync_repo(store, _Runner(), "r", "c")
        check(text == "`r`: Already in sync with remote." and refresh, f"in sync ({text!r})")

        print("ahead")
        commit(repo, "b")
        text, refresh = await sync_repo(store, _Runner(), "r", "c")
        check(text == "`r`: pushed 1 commit." and refresh, f"pushed ({text!r})")
        check(git(root / "origin.git", "rev-parse", "master") == git(repo, "rev-parse", "HEAD"),
              "origin has the commit")

        print("new tag")
        git(repo, "tag", "v1.0.0")
        text, refresh = await sync_repo(store, _Runner(), "r", "c")
        check(text == "`r`: 1 tag." and refresh, f"tag pushed ({text!r})")

        print("behind, self-managed")
        git(other, "pull", "-q")
        commit(other, "c")
        commit(other, "d")
        git(other, "push", "-q")
        ds = DeployState(self_managed=True)
        store_ds = _Store({"r": str(repo)}, ds)
        runner = _Runner()
        text, refresh = await sync_repo(store_ds, runner, "r", "chan-7")
        check(text == "`r`: pulled 2 commits, reboot queued." and refresh, f"pulled ({text!r})")
        check(bool(ds.current_ref) and git(repo, "rev-parse", "HEAD").startswith(ds.current_ref),
              f"deploy state ref moved ({ds.current_ref!r})")
        check(store_ds.saved == [ds], "deploy state saved")
        check(len(runner.requests) == 1 and runner.requests[0]["channel_id"] == "chan-7",
              f"reboot requested for the thread ({runner.requests})")

        print("behind, reboot deferred")
        commit(other, "e")
        git(other, "push", "-q")
        text, _ = await sync_repo(_Store({"r": str(repo)}, DeployState(self_managed=True)),
                                  _Runner(RebootResult.DEFERRED), "r", "c")
        check("pulled 1 commit, reboot deferred" in text, f"deferred wording ({text!r})")

        print("behind, not self-managed")
        commit(other, "f")
        git(other, "push", "-q")
        runner = _Runner()
        text, _ = await sync_repo(_Store({"r": str(repo)}, DeployState()), runner, "r", "c")
        check(text == "`r`: pulled 1 commit." and runner.requests == [],
              f"no reboot ({text!r})")

        print("behind with a dirty tree")
        commit(other, "g")
        git(other, "push", "-q")
        (repo / "a").write_text("dirty")
        text, refresh = await sync_repo(store, _Runner(), "r", "c")
        check("uncommitted changes" in text and not refresh, f"stopped ({text!r})")
        git(repo, "checkout", "--", "a")

        print("no upstream")
        git(repo, "checkout", "-q", "-b", "lonely")
        text, refresh = await sync_repo(store, _Runner(), "r", "c")
        check(text == "`r`: No upstream branch configured." and not refresh, f"stopped ({text!r})")

        print("an exception is reported, not raised")

        class _Boom(_Store):
            def get_deploy_state(self, name):  # noqa: ANN001, ANN201
                raise RuntimeError("store down")

        text, refresh = await sync_repo(_Boom({"r": str(repo)}), _Runner(), "r", "c")
        check(text == "`r`: Git sync failed \u2014 store down" and not refresh, f"reported ({text!r})")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
