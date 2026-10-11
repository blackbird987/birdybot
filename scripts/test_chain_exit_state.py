#!/usr/bin/env python3
"""Regression test: every terminal chain exit clears all of the chain's state.

A chain keeps four per-session records: its step queue, deferred review items,
multi-phase build state and the HEAD snapshot ``verify_release`` diffs against.
Only the success exit cleared the snapshot, and only the failure exit cleared
the phases, so each left the other's record behind. The post-reboot resumer,
the manual Done/Merge path and the release-interrupt probe cleared just the
queue and the snapshot. A later chain on the same session could pick up a
stale diff anchor, and the leftovers sat in ``state.json`` forever.

Asserted here, on a real ``StateStore``:

  * ``clear_chain_state`` drops all four records;
  * a terminal ``_exit_chain`` (``clear_state=True``) drops all four;
  * a pausing ``_exit_chain`` keeps them all and marks the chain paused;
  * a manual Done/Merge drops all four.

Run: python scripts/test_chain_exit_state.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.claude.types import ChainPhaseState, Phase  # noqa: E402
from bot.engine import commands, workflows  # noqa: E402
from bot.platform.base import RequestContext  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

SID = "sess-e16"
_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}" + (f"  ({detail})" if detail else ""))
        _failures.append(name)


class _Messenger:
    async def is_conversation_closed(self, _cid):  # noqa: ANN001
        return False

    def format_mention(self, _uid):  # noqa: ANN001
        return None

    async def send_text(self, *a, **kw):  # noqa: ANN002, ANN003
        return "m"


def _seeded(tmp: Path) -> StateStore:
    store = StateStore(tmp / "state.json", tmp / "results")
    store.set_autopilot_chain(SID, ["verify", "merge"])
    store.set_chain_deferred(SID, ["tighten the error text"])
    store.set_chain_phases(SID, ChainPhaseState(phases=[Phase("p1", "one", "auto")]))
    store.set_chain_entry_sha(SID, "abc123")
    return store


def _left(store: StateStore) -> list[str]:
    left = []
    if store.get_autopilot_chain(SID):
        left.append("queue")
    if store.get_chain_deferred(SID):
        left.append("deferred")
    if store.get_chain_phases(SID):
        left.append("phases")
    if store.get_chain_entry_sha(SID):
        left.append("entry_sha")
    return left


def _exit(store: StateStore, *, clear_state: bool) -> None:
    ctx = RequestContext(
        messenger=_Messenger(),  # type: ignore[arg-type]
        channel_id="c1", platform="test",
        store=store, runner=None,  # type: ignore[arg-type]
        repo_name="harness", is_owner=True,
    )
    asyncio.run(workflows._exit_chain(
        ctx, "t-1", SID, ["build", "verify", "merge"], ["build"], [], None,
        "failed", "Chain blocked.", clear_state=clear_state,
    ))


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "results").mkdir()

        store = _seeded(tmp)
        _check("seeded: all four records present", len(_left(store)) == 4, str(_left(store)))
        store.clear_chain_state(SID)
        _check("clear_chain_state drops all four", _left(store) == [], str(_left(store)))

        store = _seeded(tmp)
        _exit(store, clear_state=True)
        _check("terminal chain exit drops all four", _left(store) == [], str(_left(store)))

        store = _seeded(tmp)
        _exit(store, clear_state=False)
        _check("pausing chain exit keeps all four", len(_left(store)) == 4, str(_left(store)))
        meta = store.get_autopilot_chain_meta(SID) or {}
        _check("pausing chain exit marks the chain paused", meta.get("status") == "paused", str(meta))

        store = _seeded(tmp)
        commands._clear_chain_on_manual_finalize(store, SID, "merge")
        _check("manual Done/Merge drops all four", _left(store) == [], str(_left(store)))

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} chain-exit check(s) failed")
        return 1
    print("PASS: every terminal chain exit clears the whole chain state.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
