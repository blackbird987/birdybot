#!/usr/bin/env python3
"""Regression test: auto-fix chains from the diagnosis turn, not the oldest one.

``auto_fix.spawn_fix_session`` runs a diagnosis query in a fresh thread, then
hands the session's newest instance to ``workflows.on_autopilot`` as the
chain's source. It found that instance as the LAST session match in
``store.list_instances()``, but that list is sorted newest-first, so the
chain was seeded from the session's OLDEST turn instead.

It now uses ``StateStore.latest_instance_for_session``, the store's single
"newest wins" scan, which CLAUDE.md asks every thread-to-instance lookup to
share. This drives the real function with fakes and asserts the source
instance and message id handed to the chain.
"""

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.claude.types import Instance, InstanceStatus, InstanceType  # noqa: E402
from bot.engine import auto_fix, commands, workflows  # noqa: E402
from bot.store.records import AutoFixState  # noqa: E402

_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name}" + (f" ({detail})" if detail else ""))
        _failures.append(name)


def _inst(iid: str, created: str, msg: str) -> Instance:
    inst = Instance(
        id=iid, name=None, instance_type=InstanceType.QUERY, prompt="p",
        repo_name="alpha", repo_path="/tmp/alpha",
        status=InstanceStatus.COMPLETED, session_id="sess-1",
        created_at=created,
    )
    inst.message_ids = {"discord": [msg]}
    return inst


class _Store:
    def __init__(self, instances):
        self._instances = {i.id: i for i in instances}
        self.state = AutoFixState()

    def get_auto_fix_state(self, repo, trigger):
        return self.state

    def set_auto_fix_state(self, repo, trigger, state):
        self.state = state

    def list_instances(self, all_=False):
        return sorted(self._instances.values(),
                      key=lambda i: i.created_at or "", reverse=True)

    # The real implementation, so the test exercises the store's own scan.
    from bot.store.state import StateStore as _S
    latest_instance_for_session = _S.latest_instance_for_session


class _ThreadInfo:
    session_id = "sess-1"


class _Thread:
    id = 555

    async def add_user(self, user):
        pass


class _Forums:
    async def get_or_create_session_thread(self, *a, **k):
        return _Thread()

    def thread_to_project(self, channel_id):
        return ("alpha", _ThreadInfo())

    def attach_session_callbacks(self, *a, **k):
        pass

    async def refresh_control_room(self, repo):
        pass


class _Messenger:
    def format_mention(self, uid):
        return ""

    async def send_text(self, *a, **k):
        return None


class _Ctx:
    def __init__(self):
        self.messenger = _Messenger()
        self.is_owner = False


class _Bot:
    _discord_user_id = None

    def __init__(self, store):
        self._store = store
        self._forums = _Forums()

    def _ctx(self, channel_id, **kw):
        return _Ctx()


async def main() -> int:
    oldest = _inst("q-1", "2026-10-01T10:00:00+00:00", "m-old")
    middle = _inst("q-2", "2026-10-01T11:00:00+00:00", "m-mid")
    newest = _inst("q-3", "2026-10-01T12:00:00+00:00", "m-new")
    store = _Store([middle, oldest, newest])
    seen: list[tuple[str, str | None]] = []

    async def fake_on_text(ctx, text):
        pass

    async def fake_on_autopilot(ctx, inst_id, msg_id, **kw):
        seen.append((inst_id, msg_id))
        return None

    saved = (commands.on_text, workflows.on_autopilot)
    commands.on_text = fake_on_text
    workflows.on_autopilot = fake_on_autopilot
    try:
        await auto_fix.spawn_fix_session(_Bot(store), "alpha", "deploy", "fix it")
        await asyncio.sleep(0)
    finally:
        commands.on_text, workflows.on_autopilot = saved

    _check("the chain is started exactly once", len(seen) == 1, str(seen))
    _check("the chain's source is the session's newest instance",
           bool(seen) and seen[0][0] == "q-3", str(seen))
    _check("the chain edits the newest instance's message",
           bool(seen) and seen[0][1] == "m-new", str(seen))

    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll auto-fix source checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
