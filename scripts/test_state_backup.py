#!/usr/bin/env python3
"""Regression test: the periodic state backup rotates instead of copying.

``StateStore.save(backup=True)`` runs every 60s from the auto-save loop and
used to ``shutil.copy2`` the whole multi-megabyte ``state.json`` to
``state.bak`` on the event loop before replacing it. It now hard-links the
current file to the backup name and then replaces ``state.json``, so the
backup holds the previous snapshot without a byte being copied, and
``state.json`` exists at every instant (a rename of the live file would leave
a gap a crash could land in).

Asserted here, against a real store in a temp dir:

  * after two backed-up saves, .bak holds the first snapshot and state.json
    the second, as separate files (the link did not survive the replace);
  * no copy is made when hard links work, and no .bak.tmp is left behind;
  * a filesystem without hard links falls back to the copy;
  * a save without backup leaves .bak alone.

Run: python scripts/test_state_backup.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.store import state as state_mod  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}" + (f"  ({detail})" if detail else ""))
        _failures.append(name)


def _mode(path: Path) -> str | None:
    return json.loads(path.read_text(encoding="utf-8")).get("mode")


def main() -> int:
    real_copy = shutil.copy2
    real_link = os.link
    copies: list[tuple] = []

    def counting_copy(*a, **kw):
        copies.append(a)
        return real_copy(*a, **kw)

    state_mod.shutil.copy2 = counting_copy
    try:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            f = tmp / "state.json"
            bak = tmp / "state.bak"
            store = StateStore(f, tmp / "results")

            store._mode = "explore"  # the property setter saves on its own
            store.save(backup=True)
            _check("first backed-up save writes state.json", _mode(f) == "explore")
            store._mode = "build"  # the property setter saves on its own
            store.save(backup=True)
            _check("state.json holds the newest snapshot", _mode(f) == "build")
            _check(".bak holds the previous snapshot",
                   bak.exists() and _mode(bak) == "explore")
            _check(".bak and state.json are separate files",
                   bak.exists() and os.stat(bak).st_ino != os.stat(f).st_ino
                   and os.stat(f).st_nlink == 1)
            _check("no copy when hard links work", not copies, str(copies))
            _check("no .bak.tmp left behind", not (tmp / "state.bak.tmp").exists())

            store._mode = "plan"  # the property setter saves on its own
            store.save()
            _check("a save without backup leaves .bak alone", _mode(bak) == "explore")

            def no_link(*a, **kw):
                raise OSError(1, "Operation not permitted")

            state_mod.os.link = no_link
            store._mode = "explore"  # the property setter saves on its own
            store.save(backup=True)
            state_mod.os.link = real_link
            _check("without hard links: falls back to a copy", len(copies) == 1, str(copies))
            _check("without hard links: .bak holds the previous snapshot", _mode(bak) == "plan")
            _check("without hard links: state.json holds the newest", _mode(f) == "explore")
    finally:
        state_mod.shutil.copy2 = real_copy
        state_mod.os.link = real_link

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} state-backup check(s) failed")
        return 1
    print("PASS: the state backup rotates by hard link and falls back to a copy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
