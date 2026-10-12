#!/usr/bin/env python3
"""Regression test: button taps route through one table.

``interactions.handle`` used to be a 30-branch if-chain. The buttons that act
on their own after the defer now sit in ``_ACTIONS``, with the owner-only ones
named in ``_OWNER_ONLY_ACTIONS``. Pinned here:

  * every table entry reaches the handler it names, with the right argument;
  * an owner-only action is refused for a guest before its handler runs, and
    the other table actions are not;
  * a handler replaced on the module after import is the one that runs, so
    harnesses that patch handlers keep working;
  * no action is both a table action and a query action, since the table is
    consulted first and would silently shadow the query.

Run:  python scripts/test_button_dispatch.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.discord import interactions  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


class _Response:
    def __init__(self) -> None:
        self.deferred = False

    async def defer(self) -> None:
        self.deferred = True

    def is_done(self) -> bool:
        return self.deferred


class _Followup:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text, **_kw):  # noqa: ANN001, ANN003
        self.sent.append(text)


class _Interaction:
    def __init__(self, custom_id: str, values: list[str] | None = None) -> None:
        self.response = _Response()
        self.followup = _Followup()
        self.user = SimpleNamespace(id=7, display_name="u")
        self.channel_id = 5
        self.channel = SimpleNamespace(name="thread")
        self.message = None
        self.data = {"custom_id": custom_id}
        if values is not None:
            self.data["values"] = values


class _Access:
    def __init__(self, owner: bool) -> None:
        self.allowed = True
        self.is_owner = owner


class _Bot:
    def __init__(self, owner: bool = True) -> None:
        self.access = _Access(owner)

    def _check_access(self, *_a, **_kw):  # noqa: ANN002, ANN003, ANN202
        return self.access


# action -> (module attribute the entry calls, positional arg index that
# carries the custom_id's trailing part, or None when it takes no arg).
_EXPECTED = {
    "watch_stop": ("_handle_watch_stop", 2),
    "fleet_confirm": ("_handle_fleet", 3),
    "fleet_cancel": ("_handle_fleet", 3),
    "steer": ("_handle_pending_action", 3),
    "cancel_pending": ("_handle_pending_action", 3),
    "usage_run": ("_handle_usage_action", 3),
    "usage_queue": ("_handle_usage_action", 3),
    "usage_cancel": ("_handle_usage_action", 3),
    "load_history": ("_handle_load_history", 2),
    "new_repo": ("_handle_new_repo", 2),
    "new_repo_select": ("_handle_new_repo_select", None),
    "sync_repo": ("_handle_sync_cli", 2),
    "sync_git": ("_handle_sync_git", 2),
    "resume_latest": ("_handle_resume_latest", 2),
    "reboot_repo": ("_handle_reboot_repo", 2),
    "approve_deploy": ("_handle_approve_deploy", 2),
    "refresh_control": ("_handle_refresh_control", 2),
    "refresh_user_control": ("_handle_refresh_user_control", None),
    "cancel_cooldown": ("_handle_cancel_cooldown", 2),
    "hold_merge": ("_handle_hold_merge", 2),
    "stop_all": ("_handle_stop_all", 2),
    "sess_resume": ("_handle_sess_resume", 2),
    "new": ("_handle_new_session", None),
    "branch": ("_handle_branch", 2),
}


def _run(custom_id: str, owner: bool = True) -> tuple[list[tuple[str, tuple]], _Interaction]:
    seen: list[tuple[str, tuple]] = []
    names = {name for name, _ in _EXPECTED.values()}
    saved = {n: getattr(interactions, n) for n in names}

    def fake(name):  # noqa: ANN001, ANN202
        async def f(*args):  # noqa: ANN002
            seen.append((name, args))
        return f

    for n in names:
        setattr(interactions, n, fake(n))
    inter = _Interaction(custom_id)
    try:
        asyncio.run(interactions.handle(_Bot(owner), inter))
    finally:
        for n, fn in saved.items():
            setattr(interactions, n, fn)
    return seen, inter


def main() -> int:
    print("table coverage")
    check(set(interactions._ACTIONS) == set(_EXPECTED),
          f"table and test agree (extra: {set(interactions._ACTIONS) ^ set(_EXPECTED)})")
    check(not set(interactions._ACTIONS) & interactions._QUERY_ACTIONS,
          "no action is both a table action and a query action")
    check(interactions._OWNER_ONLY_ACTIONS <= set(interactions._ACTIONS),
          "every owner-only action is in the table")

    print("each action reaches its handler (patched after import)")
    for action, (name, arg_idx) in _EXPECTED.items():
        seen, inter = _run(f"{action}:arg-1")
        ok = len(seen) == 1 and seen[0][0] == name
        if ok and arg_idx is not None:
            ok = seen[0][1][arg_idx] == "arg-1"
        check(ok and inter.response.deferred, f"{action} -> {name} ({seen and seen[0][0]})")

    print("owner-only actions")
    for action in sorted(interactions._OWNER_ONLY_ACTIONS):
        seen, inter = _run(f"{action}:arg-1", owner=False)
        check(seen == [] and inter.followup.sent == ["Owner only."],
              f"{action} refused for a guest")
    seen, inter = _run("refresh_control:arg-1", owner=False)
    check(len(seen) == 1 and inter.followup.sent == [],
          "a table action that is not owner-only runs for a guest")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
