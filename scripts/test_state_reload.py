#!/usr/bin/env python3
"""Regression test: reloading state.json after an outside edit.

``StateStore.reload_if_changed`` runs every scheduler tick and every autonomy
sweep. It used to call ``_load()`` straight into the live store, and ``_load``
only ever *adds* to the record maps, so an instance, schedule or watch deleted
on disk came straight back from memory. Asserted here:

  * a record deleted on disk is gone after the reload, and a record changed
    on disk takes the file's value;
  * unsaved changes win: with the store dirty, the reload does nothing, so a
    record created since the last save is not dropped;
  * a file that will not parse leaves memory untouched, since the next save
    would otherwise write the damage back over the real state;
  * a payload that parses but fails part-way restores the previous records
    instead of leaving the store half-empty;
  * an unchanged file is not re-read.

Run: python scripts/test_state_reload.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.claude.types import InstanceType, Watch  # noqa: E402
from bot.store import state as state_mod  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}" + (f"  ({detail})" if detail else ""))
        _failures.append(name)


def _edit_on_disk(path: Path, mutate) -> None:  # noqa: ANN001
    """Rewrite the file the way an outside tool would, with a newer mtime."""
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    future = time.time() + 5
    os.utime(path, (future, future))


def _store(tmp: Path) -> tuple[StateStore, str, str]:
    results = tmp / "results"
    results.mkdir(exist_ok=True)
    s = StateStore(tmp / "state.json", results)
    a = s.create_instance(InstanceType.QUERY, "keep me")
    b = s.create_instance(InstanceType.QUERY, "delete me on disk")
    s.add_schedule("every hour", interval_secs=3600)
    s.add_watch(Watch(id="w-001", channel_id="c1", prompt="job done", pid=1))
    s.save()
    return s, a.id, b.id


def check_deleted_record_stays_deleted() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        s, keep, gone = _store(tmp)

        def _mutate(data: dict) -> None:
            data["instances"] = [i for i in data["instances"] if i["id"] != gone]
            for i in data["instances"]:
                if i["id"] == keep:
                    i["prompt"] = "edited on disk"
            data["schedules"] = []
            data["watches"] = []
        _edit_on_disk(tmp / "state.json", _mutate)

        _check("reload: reports that it reloaded", s.reload_if_changed() is True)
        _check("reload: an instance deleted on disk is gone", s.get_instance(gone) is None)
        kept = s.get_instance(keep)
        _check("reload: a changed instance takes the file's value",
               kept is not None and kept.prompt == "edited on disk",
               kept.prompt if kept else "missing")
        _check("reload: a schedule deleted on disk is gone", s.list_schedules() == [])
        _check("reload: a watch deleted on disk is gone", s.list_watches() == [])
        _check("reload: an unchanged file is not re-read", s.reload_if_changed() is False)


def check_dirty_state_wins() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        s, keep, _gone = _store(tmp)
        fresh = s.create_instance(InstanceType.QUERY, "not saved yet")
        s.mark_dirty()
        _edit_on_disk(tmp / "state.json", lambda data: data.update(instances=[]))
        logging.getLogger(state_mod.__name__).setLevel(logging.ERROR)
        try:
            reloaded = s.reload_if_changed()
        finally:
            logging.getLogger(state_mod.__name__).setLevel(logging.NOTSET)
        _check("dirty: the reload stands down", reloaded is False)
        _check("dirty: a record created since the last save survives",
               s.get_instance(fresh.id) is not None)
        _check("dirty: saved records are not dropped either", s.get_instance(keep) is not None)
        _check("dirty: the outside edit is not retried every tick", s.reload_if_changed() is False)


def check_corrupt_file_keeps_memory() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        s, keep, gone = _store(tmp)
        path = tmp / "state.json"
        path.write_text('{"instances": [', encoding="utf-8")
        future = time.time() + 5
        os.utime(path, (future, future))
        logging.getLogger(state_mod.__name__).setLevel(logging.CRITICAL)
        try:
            reloaded = s.reload_if_changed()
        finally:
            logging.getLogger(state_mod.__name__).setLevel(logging.NOTSET)
        _check("corrupt: no reload", reloaded is False)
        _check("corrupt: memory is untouched",
               s.get_instance(keep) is not None and s.get_instance(gone) is not None
               and len(s.list_schedules()) == 1 and len(s.list_watches()) == 1)


def check_failed_apply_restores_records() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        s, keep, gone = _store(tmp)
        _edit_on_disk(tmp / "state.json", lambda data: data.update(instances=[{"bogus": 1}]))
        logging.getLogger(state_mod.__name__).setLevel(logging.CRITICAL)
        try:
            s.reload_if_changed()
        finally:
            logging.getLogger(state_mod.__name__).setLevel(logging.NOTSET)
        _check("bad payload: the previous records are restored",
               s.get_instance(keep) is not None and s.get_instance(gone) is not None
               and len(s.list_schedules()) == 1 and len(s.list_watches()) == 1)


def main() -> int:
    check_deleted_record_stays_deleted()
    check_dirty_state_wins()
    check_corrupt_file_keeps_memory()
    check_failed_apply_restores_records()
    print()
    if _failures:
        print(f"FAIL: {len(_failures)} state-reload check(s) failed")
        return 1
    print("PASS: a reload replaces records, yields to unsaved changes and survives a bad file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
