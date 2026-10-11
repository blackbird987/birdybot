#!/usr/bin/env python3
"""Harness for bot/fsutil.py, the one atomic file writer.

Pins the three things its callers rely on: a reader sees the old file or the
new one and never a mix, a failure leaves no temp file behind (repo.json is
written into a user's repo, where a stray .tmp shows up in git status), and
two concurrent writers never share a temp name.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import fsutil  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def _leftovers(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.name.endswith(".tmp"))


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="fsutil_"))

    # 1. Writes, creates parents, replaces.
    target = root / "a" / "b" / "x.json"
    fsutil.atomic_write_json(target, {"k": 1}, trailing_newline=True)
    check("creates parent dirs and writes", json.loads(target.read_text()) == {"k": 1})
    check("trailing newline honoured", target.read_text().endswith("\n"))
    fsutil.atomic_write_json(target, {"k": 2})
    check("replaces an existing file", json.loads(target.read_text()) == {"k": 2})
    check("no temp file after success", _leftovers(target.parent) == [])

    # 2. ensure_ascii passes through.
    fsutil.atomic_write_json(target, {"s": "é"}, ensure_ascii=False)
    check("ensure_ascii=False keeps raw unicode", "é" in target.read_text(encoding="utf-8"))

    # 3. A failed rename keeps the old file and strands nothing.
    with mock.patch.object(fsutil.os, "replace", side_effect=OSError("boom")):
        try:
            fsutil.atomic_write_text(target, "new")
            raised = False
        except OSError:
            raised = True
    check("rename failure raises", raised)
    check("rename failure keeps the old content", "é" in target.read_text(encoding="utf-8"))
    check("rename failure leaves no temp file", _leftovers(target.parent) == [],
          str(_leftovers(target.parent)))

    # 4. An unencodable value raises before anything is created.
    fresh = root / "never.json"
    try:
        fsutil.atomic_write_json(fresh, {"x": object()})
        raised = False
    except TypeError:
        raised = True
    check("unencodable value raises TypeError", raised)
    check("unencodable value creates nothing", not fresh.exists() and _leftovers(root) == [])

    # 5. Concurrent writers each land a whole file; temp names never collide.
    shared = root / "shared.json"
    payloads = [{"w": i, "pad": "x" * 20000} for i in range(16)]
    errors: list[BaseException] = []

    def writer(p: dict) -> None:
        try:
            for _ in range(20):
                fsutil.atomic_write_json(shared, p)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("concurrent writers raise nothing", not errors, repr(errors[:1]))
    try:
        final = json.loads(shared.read_text())
        whole = final in payloads
    except ValueError:
        whole = False
    check("concurrent writers leave one whole payload", whole)
    check("concurrent writers leave no temp files", _leftovers(root) == [])

    # 6. Callers moved onto it: no hand-rolled fixed-name temp left behind.
    repo = Path(__file__).resolve().parent.parent
    for rel in ("bot/paths.py", "bot/discord/bot.py", "bot/engine/repo_desc.py",
                "bot/engine/usage.py", "bot/engine/eval.py"):
        src = (repo / rel).read_text(encoding="utf-8")
        check(f"{rel} writes through fsutil", "atomic_write_json(" in src)
        check(f"{rel} has no hand-rolled os.replace", "os.replace(" not in src)

    shutil.rmtree(root, ignore_errors=True)
    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
