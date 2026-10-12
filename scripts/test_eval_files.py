#!/usr/bin/env python3
"""The eval files round-trip through one writer and one reader (E24).

Session and chain evals had twin save and load functions. They share one
writer and one reader now. Pinned: each kind loads only its own files, the
weekly files are never read as session evals, a file outside the window is
ignored, and a file that will not parse is skipped instead of failing the
whole load.

Run:  python scripts/test_eval_files.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.engine import eval as eval_mod  # noqa: E402
from bot.engine.eval import ChainEval, EvalFlag, SessionEval  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


def main() -> int:
    orig = eval_mod.EVALS_DIR
    with tempfile.TemporaryDirectory() as tmp:
        eval_mod.EVALS_DIR = Path(tmp) / "evals"
        try:
            check(eval_mod.load_evals() == [], "missing directory loads nothing")
            eval_mod.EVALS_DIR.mkdir()
            eval_mod._save_eval(SessionEval(
                instance_id="q-1", repo="bot", origin="query", mode="explore",
                flags=[EvalFlag(category="narration", message="m", severity="info")],
            ))
            eval_mod._save_chain_eval(ChainEval(chain_id="c-1", repo="bot", topic="t", outcome="merged"))
            (eval_mod.EVALS_DIR / "weekly-2026-10-01.json").write_text("{}")
            (eval_mod.EVALS_DIR / "q-bad.json").write_text("{not json")
            old = eval_mod.EVALS_DIR / "q-old.json"
            old.write_text((eval_mod.EVALS_DIR / "q-1.json").read_text().replace("q-1", "q-old"))
            stale = time.time() - 3 * 86400
            os.utime(old, (stale, stale))

            sessions = eval_mod.load_evals(since_hours=24)
            check([e.instance_id for e in sessions] == ["q-1"],
                  f"one session eval, no chain/weekly/bad/old file ({[e.instance_id for e in sessions]})")
            check(sessions and sessions[0].flags[0].category == "narration", "flags round-trip")
            chains = eval_mod.load_chain_evals(since_hours=24)
            check([c.chain_id for c in chains] == ["c-1"], "one chain eval")
            check(chains and chains[0].outcome == "merged", "chain fields round-trip")
            wide = eval_mod.load_evals(since_hours=24 * 7)
            check(sorted(e.instance_id for e in wide) == ["q-1", "q-old"], "a wider window reaches the old file")
        finally:
            eval_mod.EVALS_DIR = orig
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
