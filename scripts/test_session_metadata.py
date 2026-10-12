#!/usr/bin/env python3
"""Regression test: ``session_index.extract_session_metadata``.

The index entry for a session is read out of its JSONL transcript in one
pass. Pinned, with a hand-built transcript per rule:

  * the first session id and the first timestamp win, in either spelling;
  * ``firstPrompt`` is the first user turn with text (string or text block),
    skipping queue operations, tool results and blank turns, and falls back
    to the first queue ``enqueue`` when no user turn has text;
  * the summary is the *last* summary record, the branch the *first* one;
  * only user and assistant records count as messages;
  * a sidechain flag anywhere marks the session;
  * blank lines, corrupt JSON and non-object records are skipped;
  * an unreadable file or one with no session id yields None;
  * prompts are capped at 500 chars, summaries at 200.

Run:  python scripts/test_session_metadata.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.claude.session_index import extract_session_metadata  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


def _write(tmp: Path, name: str, lines: list) -> Path:
    path = tmp / f"{name}.jsonl"
    path.write_text("\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n",
                    encoding="utf-8")
    return path


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        print("a typical transcript")
        meta = extract_session_metadata(_write(tmp, "typical", [
            {"type": "queue-operation", "operation": "enqueue", "content": "  queued prompt  ",
             "sessionId": "s-1", "timestamp": "2026-10-01T00:00:00Z"},
            "",
            "{not json",
            ["a", "list"],
            {"type": "user", "message": {"content": [{"type": "tool_result", "text": "no"}]},
             "gitBranch": "feature/x", "session_id": "s-other"},
            {"type": "user", "message": {"content": "   "}},
            {"type": "user", "message": {"content": [{"type": "image"},
                                                     {"type": "text", "text": "  real prompt "},
                                                     {"type": "text", "text": "second"}]},
             "git_branch": "other", "timestamp": "2026-10-02T00:00:00Z"},
            {"type": "assistant", "message": {"content": []}},
            {"type": "summary", "summary": "first summary"},
            {"type": "user", "message": {"content": "later prompt"}},
            {"type": "summary", "content": " last summary "},
            {"type": "system", "isSidechain": True},
        ]))
        check(meta is not None and meta["sessionId"] == "s-1", "the first session id wins")
        check(meta["created"] == "2026-10-01T00:00:00Z", "the first timestamp wins")
        check(meta["firstPrompt"] == "real prompt", f"first user text, stripped ({meta['firstPrompt']!r})")
        check(meta["summary"] == "last summary", f"the last summary wins ({meta['summary']!r})")
        check(meta["gitBranch"] == "feature/x", "the first branch wins")
        check(meta["messageCount"] == 5, f"user and assistant records counted ({meta['messageCount']})")
        check(meta["isSidechain"] is True, "a sidechain flag anywhere marks the session")
        check(set(meta) == {"sessionId", "firstPrompt", "summary", "messageCount",
                            "gitBranch", "created", "isSidechain"}, "the entry's keys")

        print("fallbacks and caps")
        meta = extract_session_metadata(_write(tmp, "queue", [
            {"type": "queue-operation", "operation": "dequeue", "content": "not this",
             "session_id": "s-2"},
            {"type": "queue-operation", "operation": "enqueue", "content": "x" * 600},
            {"type": "queue-operation", "operation": "enqueue", "content": "second enqueue"},
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            {"type": "summary", "summary": "y" * 300},
        ]))
        check(meta["sessionId"] == "s-2", "the snake_case session id is read")
        check(meta["firstPrompt"] == "x" * 500, "no user text falls back to the first enqueue, capped")
        check(meta["summary"] == "y" * 200, "a summary is capped at 200")
        check(meta["created"] == "" and meta["gitBranch"] == "" and meta["isSidechain"] is False,
              "absent fields read empty")
        meta = extract_session_metadata(_write(tmp, "long", [
            {"type": "user", "sessionId": "s-3", "message": {"content": "z" * 700}},
            {"type": "user", "message": None},
        ]))
        check(meta["firstPrompt"] == "z" * 500 and meta["messageCount"] == 2,
              "a user prompt is capped at 500, and a null message still counts")

        print("nothing usable")
        check(extract_session_metadata(_write(tmp, "noid", [{"type": "user"}])) is None,
              "no session id yields None")
        check(extract_session_metadata(tmp / "missing.jsonl") is None,
              "an unreadable file yields None")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
