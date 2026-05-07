"""End-to-end verification of autopilot_chain_meta lifecycle.

Exercises the new pause-aware chain tracking against a temp state file.
Covers: set_autopilot_chain stamps running, set_autopilot_chain_status
writes paused, clear drops both queue and meta, persistence round-trip,
load-time migration drops unmetered queues.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Ensure the worktree's bot package is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.store.state import StateStore  # noqa: E402


def _fresh_store() -> tuple[StateStore, Path, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="verify_chain_meta_"))
    state = tmp / "state.json"
    results = tmp / "results"
    results.mkdir()
    return StateStore(state, results), state, results


def _expect(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}", file=sys.stderr)
        sys.exit(1)
    print(f"  ok: {msg}")


def main() -> None:
    print("== Test 1: set_autopilot_chain stamps running meta ==")
    store, state_file, results = _fresh_store()
    sid = "session-A"
    store.set_autopilot_chain(sid, ["build", "merge"])
    meta = store.get_autopilot_chain_meta(sid)
    _expect(meta is not None, "meta exists after set_autopilot_chain")
    _expect(meta.get("status") == "running", f"status=running (got {meta.get('status')!r})")
    _expect("updated_at" in meta, "updated_at present")

    print("== Test 2: set_autopilot_chain_status('paused') ==")
    store.set_autopilot_chain_status(sid, "paused")
    meta = store.get_autopilot_chain_meta(sid)
    _expect(meta.get("status") == "paused", f"status=paused (got {meta.get('status')!r})")

    print("== Test 3: status guard — only 'running' or 'paused' accepted ==")
    store.set_autopilot_chain_status(sid, "garbage")
    meta = store.get_autopilot_chain_meta(sid)
    _expect(meta.get("status") == "paused", "garbage status ignored, paused preserved")

    print("== Test 4: set_autopilot_chain_status no-ops when chain absent ==")
    store.set_autopilot_chain_status("missing-session", "paused")
    _expect(
        store.get_autopilot_chain_meta("missing-session") is None,
        "no zombie meta for missing chain",
    )

    print("== Test 5: clear_autopilot_chain drops both queue and meta ==")
    store.clear_autopilot_chain(sid)
    _expect(store.get_autopilot_chain(sid) is None, "queue cleared")
    _expect(store.get_autopilot_chain_meta(sid) is None, "meta cleared")

    print("== Test 6: persistence round-trip (running) ==")
    sid2 = "session-B"
    store.set_autopilot_chain(sid2, ["release", "merge"])
    store.save()
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    _expect("autopilot_chain_meta" in raw, "autopilot_chain_meta key persisted")
    _expect(sid2 in raw["autopilot_chain_meta"], "sid2 meta on disk")
    _expect(
        raw["autopilot_chain_meta"][sid2]["status"] == "running",
        "disk shows running",
    )

    # Reload — confirm meta survives
    store2 = StateStore(state_file, results)
    meta = store2.get_autopilot_chain_meta(sid2)
    _expect(meta is not None and meta.get("status") == "running", "meta survives reload")

    print("== Test 7: persistence round-trip (paused) ==")
    store2.set_autopilot_chain_status(sid2, "paused")
    store2.save()
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    _expect(
        raw["autopilot_chain_meta"][sid2]["status"] == "paused",
        "disk shows paused after status write",
    )

    print("== Test 8: load-time migration drops unmetered queue ==")
    store3, state3, res3 = _fresh_store()
    # Hand-craft a state file with an unmetered queue (simulates pre-migration)
    payload = {
        "instances": [],
        "repos": {},
        "active_repo": None,
        "autopilot_chains": {"orphan-session": ["release", "merge"]},
        # autopilot_chain_meta deliberately absent
    }
    state3.write_text(json.dumps(payload), encoding="utf-8")
    store4 = StateStore(state3, res3)
    _expect(
        store4.get_autopilot_chain("orphan-session") is None,
        "unmetered queue dropped on load",
    )

    print("== Test 9: load-time migration preserves metered queue ==")
    payload = {
        "instances": [],
        "repos": {},
        "active_repo": None,
        "autopilot_chains": {"alive-session": ["build"]},
        "autopilot_chain_meta": {
            "alive-session": {"status": "paused", "updated_at": "2026-05-07T00:00:00+00:00"}
        },
    }
    state3.write_text(json.dumps(payload), encoding="utf-8")
    store5 = StateStore(state3, res3)
    _expect(
        store5.get_autopilot_chain("alive-session") == ["build"],
        "metered queue preserved",
    )
    _expect(
        store5.get_autopilot_chain_meta("alive-session").get("status") == "paused",
        "meta preserved",
    )

    print("\nALL VERIFY CHECKS PASSED")


if __name__ == "__main__":
    main()
