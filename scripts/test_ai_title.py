"""Regression test for read_ai_title().

Locks in that the bot reads the CLI's native `ai-title` jsonl record (clean,
no codename prefix) instead of relying on the title-gen subprocess, which
occasionally emits Docker-style codenames like "Glimmering Church …".

Self-contained: writes a fake session jsonl and monkeypatches the file lookup.
Run: python scripts/test_ai_title.py
"""

import os
import sys
import tempfile
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import bot.engine.sessions as sessions_mod
from bot.discord.titles import read_ai_title


def _write(tmp: Path, name: str, lines: list[str]) -> Path:
    p = tmp / name
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    failures = []

    # A realistic jsonl: a user record, then two ai-title records (the CLI
    # refines it as the session grows) — we must pick the LAST one. Plus a
    # garbage line that must not crash the scan.
    good = _write(tmp, "good.jsonl", [
        '{"type":"user","message":{"role":"user","content":"hi"}}',
        '{"type":"ai-title","aiTitle":"First Draft Title","sessionId":"s"}',
        'not json at all',
        '{"type":"ai-title","aiTitle":"Final Refined Title","sessionId":"s"}',
    ])
    none_title = _write(tmp, "none.jsonl", [
        '{"type":"user","message":{"role":"user","content":"hi"}}',
    ])
    empty_title = _write(tmp, "empty.jsonl", [
        '{"type":"ai-title","aiTitle":"  ","sessionId":"s"}',
    ])

    files = {
        "good": good,
        "none": none_title,
        "empty": empty_title,
    }
    def _lookup(sid):
        if sid == "boom":
            raise OSError("simulated iterdir race")
        return files.get(sid)

    orig = sessions_mod.find_session_file
    sessions_mod.find_session_file = _lookup
    try:
        cases = [
            ("good", "Final Refined Title"),   # picks LAST ai-title
            ("none", None),                    # no ai-title -> fall back
            ("empty", None),                   # blank value rejected
            ("missing", None),                 # unknown session
            ("", None),                        # empty id short-circuits
            ("boom", None),                    # lookup raises -> best-effort None
        ]
        for sid, expected in cases:
            got = read_ai_title(sid)
            status = "ok" if got == expected else "FAIL"
            if got != expected:
                failures.append(f"{sid!r}: expected {expected!r}, got {got!r}")
            print(f"  [{status}] read_ai_title({sid!r}) -> {got!r}")
    finally:
        sessions_mod.find_session_file = orig

    if failures:
        print("\nFAIL - " + "; ".join(failures))
        return 1
    print("\nPASS - all assertions held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
