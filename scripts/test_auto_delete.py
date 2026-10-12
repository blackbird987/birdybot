#!/usr/bin/env python3
"""Regression test: one sleep-then-delete helper for timed messages.

The notification broadcast (``ttl=``) and the slash-command wrapper that keeps
The Ark clean each had their own copy of "sleep, then delete, ignore a failed
delete". Both now use ``bot.platform.base.delete_after``.

Pinned here:

  * a broadcast with a ttl deletes its message, one without does not;
  * the Ark wrapper deletes only what it sent to the Ark;
  * a failing delete is swallowed, not raised into the event loop.

Run:  python scripts/test_auto_delete.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.discord.bot import _AutoDeleteMessenger  # noqa: E402
from bot.platform.base import NotificationService, delete_after  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


class _Messenger:
    platform_name = "fake"

    def __init__(self, fail_delete: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self._n = 0
        self._fail = fail_delete

    async def send_text(self, channel_id, text, buttons=None, silent=False):  # noqa: ANN001
        self._n += 1
        self.sent.append((channel_id, text))
        return f"m{self._n}"

    async def delete_message(self, channel_id, msg_id):  # noqa: ANN001
        if self._fail:
            raise RuntimeError("gone")
        self.deleted.append((channel_id, msg_id))


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0.02)


async def main() -> int:
    print("broadcast ttl")
    m = _Messenger()
    svc = NotificationService()
    svc.register(m, "ark")
    await svc.broadcast("keep")
    await svc.broadcast("drop", ttl=0.01)
    await _settle()
    check(m.deleted == [("ark", "m2")], f"only the ttl message deleted (got {m.deleted})")

    print("Ark wrapper")
    m = _Messenger()
    w = _AutoDeleteMessenger(m, "ark", ttl=0.01)
    await w.send_text("ark", "a")
    await w.send_text("thread", "b")
    await _settle()
    check(m.deleted == [("ark", "m1")], f"only the Ark message deleted (got {m.deleted})")
    check(w.delete_message == m.delete_message, "other calls pass through")

    print("failed delete")
    raised = False
    try:
        await delete_after(_Messenger(fail_delete=True), "ark", "m1", 0)
    except Exception:
        raised = True
    check(not raised, "a failed delete is swallowed")

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
