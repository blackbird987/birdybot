#!/usr/bin/env python3
"""Regression test: the archive entry's summary choice and its wording.

``ForumManager.post_archive_entry`` used to choose the summary, build the line
and post it in one function. The first two are now pure functions,
``pick_archive_summary`` and ``format_archive_entry``, pinned here:

  * the waterfall: a DONE/COMMIT changelog beats a BUILD summary, which beats
    the newest other summary, which beats the thread topic;
  * a result file that cannot be read falls through instead of raising;
  * the line carries status, mode, duration, cost, commit and version, cuts a
    long summary at 200 chars, and falls back to "Session closed".

Run:  python scripts/test_archive_entry.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType  # noqa: E402
from bot.discord.forums import format_archive_entry, pick_archive_summary  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


def _inst(origin: InstanceOrigin, summary: str, finished: str, **kw) -> Instance:  # noqa: ANN003
    inst = Instance(
        id=f"t-{finished}", prompt="p", instance_type=InstanceType.QUERY, name="n",
        repo_name="bot", repo_path="/tmp", status=InstanceStatus.COMPLETED,
    )
    inst.origin = origin
    inst.summary = summary
    inst.finished_at = finished
    for k, v in kw.items():
        setattr(inst, k, v)
    return inst


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    result = tmp / "done.md"
    result.write_text(
        "Done.\n```summary\nCOMMIT: abc1234 Ship it\nCHANGELOG:\n- One\n- Two\n"
        "VERSION: v1.2.3\n```\n", encoding="utf-8",
    )

    print("waterfall")
    build = _inst(InstanceOrigin.BUILD, "built it", "2")
    query = _inst(InstanceOrigin.DIRECT, "asked", "3")
    done = _inst(InstanceOrigin.DONE, "done", "1", result_file=str(result))
    s, inst, fi = pick_archive_summary([build, query, done], "topic")
    check(s == "- One\n- Two" and inst is done and fi and fi.version == "v1.2.3",
          f"changelog wins (got {s!r})")

    missing = _inst(InstanceOrigin.DONE, "done", "4", result_file=str(tmp / "nope.md"))
    s, inst, fi = pick_archive_summary([missing, build, query], "topic")
    check(s == "built it" and inst is build and fi is None,
          f"unreadable result file falls through to BUILD (got {s!r})")

    s, inst, _ = pick_archive_summary([missing, query], "topic")
    check(s == "asked" and inst is query, f"newest non-DONE next (got {s!r})")

    s, inst, _ = pick_archive_summary([missing], "topic")
    check(s == "done" and inst is missing, f"a lone DONE still counts (got {s!r})")

    s, inst, _ = pick_archive_summary([], "topic")
    check((s, inst) == ("topic", None), "topic when nothing else")
    s, _, _ = pick_archive_summary([], "")
    check(s == "No summary", "placeholder when there is no topic either")

    print("wording")
    line = format_archive_entry("9", "- One\n- Two", done, fi_for(result))
    check(line.startswith("**Session completed ✅** · Explore"), f"status and mode ({line.splitlines()[0]!r})")
    check("`abc1234`" in line and "v1.2.3" in line, "commit and version")
    check(line.endswith("<#9>"), "links the thread")

    timed = _inst(InstanceOrigin.BUILD, "x", "5", duration_ms=125_000, cost_usd=1.5,
                  branch="claude-bot/t-1", mode="build")
    line = format_archive_entry("9", "y" * 300, timed, None)
    first = line.splitlines()[0]
    check("2m" in first and "$1.50" in first and "`claude-bot/t-1`" in first and "Build" in first,
          f"duration, cost, branch, mode ({first!r})")
    check(line.splitlines()[1] == "y" * 197 + "...", "long summary cut at 200 chars")

    line = format_archive_entry("9", "topic", None, None)
    check(line.startswith("**Session closed**\ntopic"), "no instance reads as closed")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


def fi_for(path: Path):  # noqa: ANN201
    from bot.platform.formatting import parse_finalize_output
    return parse_finalize_output(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    sys.exit(main())
