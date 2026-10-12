#!/usr/bin/env python3
"""One case-insensitive repo-name lookup (E20).

Five call sites matched a typed or model-emitted repo name against the
registered ones, each with its own lowercase map: the chat path, the /spawn
directive, /new, /access grant and the guest access check. They now share
``textutil.match_name``. Pinned:
an exact spelling wins over a case-folded one, a case-folded one is found,
and nothing is invented for an unknown or empty name.

Run:  python scripts/test_repo_lookup.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.textutil import match_name  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


def main() -> int:
    print("match_name")
    names = ["aiagent", "bot", "AIAgent"]
    check(match_name("AIAgent", names) == "AIAgent", "exact spelling wins")
    check(match_name("Bot", names) == "bot", "case-folded match found")
    check(match_name("BOT", {"bot": "/p"}) == "bot", "works on a dict's keys")
    check(match_name("nope", names) is None, "unknown name is None")
    check(match_name("", names) is None, "empty name is None")

    print("no hand-rolled lowercase repo maps left")
    leftovers = []
    for path in (ROOT / "bot").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.DictComp) and isinstance(node.key, ast.Call):
                fn = node.key.func
                if isinstance(fn, ast.Attribute) and fn.attr == "lower":
                    src = ast.unparse(node)
                    if "repo" in src or "repos" in ast.unparse(node.generators[0].iter):
                        leftovers.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    check(not leftovers, f"none found ({leftovers})")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
