"""Contract tests for fleet ship (t-5948).

Fleet ship merges every committed session, deploys per repo, then replays
a verify prompt into each shipped thread. The reboot-safety of that
pipeline hangs on a few seams this script pins down:

- pending-verify persistence: add -> pop round-trip, dedup on re-add,
  pop of a missing repo is a no-op returning None
- drain source list: repo_name=None must see every persisted repo
- _finalize_merge accepts skip_close (fleet's stay-open merge)
- _post_deploy_healthcheck returns True with no commands configured
  (deploy success — not the optional healthcheck — is the drain trigger)
- verify prompt wording keys off the deploy method
- _commits_ahead returns 0 on garbage input (no-op branches never ship)

Run: python scripts/test_fleet_ship.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import asyncio
import inspect

from bot.discord import fleet
from bot.discord.fleet import (
    ShipTarget,
    _commits_ahead,
    _get_pending,
    _verify_prompt,
    add_pending_verify,
    pop_pending_verify,
)

_failures: list[str] = []


def _check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


class FakeStore:
    """Just enough of Store for the platform_state seam."""

    def __init__(self) -> None:
        self._ps: dict = {}
        self.saves = 0

    def get_platform_state(self, platform: str) -> dict:
        return self._ps.get(platform, {})

    def set_platform_state(self, platform: str, data: dict, *, persist: bool = True) -> None:
        self._ps[platform] = data
        if persist:
            self.saves += 1


def _target(tid: str, repo: str = "bot") -> ShipTarget:
    return ShipTarget(
        thread_id=tid, session_id=f"sess-{tid}", inst=None,  # inst unused here
        repo_name=repo, title=f"Thread {tid}",
    )


def test_pending_verify_roundtrip() -> None:
    print("test_pending_verify_roundtrip")
    store = FakeStore()
    add_pending_verify(store, "bot", "origin-1", [_target("100"), _target("200")], deploy="self")

    entry = pop_pending_verify(store, "bot")
    _check(entry is not None, "entry persisted and popped")
    _check(entry["origin"] == "origin-1", "origin preserved")
    _check(entry["deploy"] == "self", "deploy method preserved")
    tids = [e["thread_id"] for e in entry["entries"]]
    _check(tids == ["100", "200"], f"both threads persisted in order (got {tids})")
    _check(entry["entries"][0]["session_id"] == "sess-100", "session_id carried")
    _check(entry["entries"][0]["title"] == "Thread 100", "title carried")
    _check(pop_pending_verify(store, "bot") is None, "second pop returns None")
    _check(pop_pending_verify(store, "ghost") is None, "pop of unknown repo is a no-op None")


def test_pending_verify_dedup_and_merge() -> None:
    print("test_pending_verify_dedup_and_merge")
    store = FakeStore()
    add_pending_verify(store, "bot", "origin-1", [_target("100")], deploy="command")
    # Re-add same thread + one new one; origin/deploy refresh to latest call
    add_pending_verify(store, "bot", "origin-2", [_target("100"), _target("300")], deploy="self")

    entry = pop_pending_verify(store, "bot")
    tids = [e["thread_id"] for e in entry["entries"]]
    _check(tids == ["100", "300"], f"re-add dedups thread 100 (got {tids})")
    _check(entry["origin"] == "origin-2", "origin refreshed to latest add")
    _check(entry["deploy"] == "self", "deploy method refreshed to latest add")


def test_drain_sees_all_repos() -> None:
    print("test_drain_sees_all_repos")
    store = FakeStore()
    add_pending_verify(store, "bot", "o", [_target("1", "bot")], deploy="self")
    add_pending_verify(store, "webapp", "o", [_target("2", "webapp")], deploy="command")
    pending = _get_pending(store)
    _check(set(pending.keys()) == {"bot", "webapp"},
           "repo_name=None drain source lists every persisted repo")


def test_finalize_merge_accepts_skip_close() -> None:
    print("test_finalize_merge_accepts_skip_close")
    from bot.engine.workflows import _finalize_merge
    params = inspect.signature(_finalize_merge).parameters
    _check("skip_close" in params, "_finalize_merge has skip_close param")
    _check(params["skip_close"].default is False, "skip_close defaults to False (existing callers unchanged)")
    _check(params["skip_close"].kind is inspect.Parameter.KEYWORD_ONLY, "skip_close is keyword-only")


def test_healthcheck_empty_passes() -> None:
    print("test_healthcheck_empty_passes")
    from bot.discord.interactions import _post_deploy_healthcheck
    # No commands configured -> immediately healthy, no sleep taken
    result = asyncio.run(
        _post_deploy_healthcheck(None, "repo", {}, {"commands": [], "delay_secs": 0})
    )
    _check(result is True, "no healthcheck commands -> returns True")


def test_verify_prompt_wording() -> None:
    print("test_verify_prompt_wording")
    p_self = _verify_prompt("bot", "self")
    p_cmd = _verify_prompt("web", "command")
    p_none = _verify_prompt("lib", "none")
    _check("rebooted" in p_self, "self-deploy prompt mentions the reboot")
    _check("deploy command" in p_cmd, "command-deploy prompt mentions the deploy command")
    _check("no deploy is configured" in p_none, "no-deploy prompt says so")
    for p in (p_self, p_cmd, p_none):
        _check("merged to master" in p, "prompt states the merge happened")


def test_commits_ahead_garbage_is_zero() -> None:
    print("test_commits_ahead_garbage_is_zero")
    _check(_commits_ahead("Z:/definitely/not/a/repo", "master", "nope") == 0,
           "bad repo path -> 0 (target never ships)")
    _check(_commits_ahead(_ROOT, "master", "no-such-branch-xyz") == 0,
           "unknown branch -> 0 (target never ships)")


def main() -> int:
    test_pending_verify_roundtrip()
    test_pending_verify_dedup_and_merge()
    test_drain_sees_all_repos()
    test_finalize_merge_accepts_skip_close()
    test_healthcheck_empty_passes()
    test_verify_prompt_wording()
    test_commits_ahead_garbage_is_zero()

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("All fleet-ship contract tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
