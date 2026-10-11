#!/usr/bin/env python3
"""Regression test: an autocompact thrash caused by an oversized auto-attached
instruction file is named, both to the resumed attempt and in the final error.

The incident (t-9142, AIAgent, 2026-10-11): a build died with "Autocompact is
thrashing" and the user retried it by hand. The bot already auto-resumes a
thrash, because the CLI's thrash counter lives in the process. This one did
not clear on resume, because the CLI re-attaches a
nested ``CLAUDE.md`` (a ``nested_memory`` transcript row) every time the
session touches a file in its folder, and ``Services/Automation/CLAUDE.md``
was 317K chars. Each compaction was undone by the next Read.

Asserted here:

  * the scanner finds a large nested file loaded several times, through both
    the main repo and a build worktree, as ONE file with the loads summed,
    at the size of its LAST load
  * a file that was large but slimmed before its last load is not blamed
  * it finds a large file in an ``instructions`` row, and ignores small files
  * garbage lines, non-object JSON and the spaced JSON spelling are handled
  * a missing transcript is no finding, never an exception
  * the resumed attempt's prompt carries the note only when there is a finding
  * the final error keeps the CLI's own text FIRST (the matchers read it) and
    gains a cause line only when resumes are exhausted and there is a finding
  * with no finding, the resumed prompt and the final error are byte-identical
    to a run with no transcript at all
  * every thrash scans afresh, so a second thrash is judged on the rows the
    resumed attempt appended
  * against the real t-9142 transcript (the bytes it held when that build
    died, so a later resume of the session cannot move the answer),
    ``Services/Automation/CLAUDE.md`` is found at over 300K chars (skipped
    when the file is not on this machine)

The existing thrash, overflow and failover suites cover that retry behaviour
itself is unchanged; run them alongside this one.

Run: ``python scripts/test_context_bloat.py``  (exit 0 on pass).
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("BOT_PATHS_DISABLED", "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bot import config
from bot.claude import context_bloat
from bot.engine.session_fork import encode_project_path
from bot.claude.types import RunResult

# The thrash harness drives the real _run_impl cascade against a stubbed
# subprocess; reusing it keeps both suites on one definition of "a thrash".
from test_context_thrash import (  # noqa: E402
    BORN_SESSION, THRASH, _Harness, _thrash_result,
)

BIG = 120_000
SMALL = 2_000

# Real transcript of t-9142, the build that thrashed on the 317K file.
REAL_TRANSCRIPT = Path(
    "/home/quincy/.claude-main/projects/"
    "-home-quincy-Programming-DegenAI-AIAgent-AIAgent--worktrees-t-9142/"
    "daab9204-55ed-4bcf-98a9-5b9bb5be7658.jsonl"
)
# The transcript's length when t-9142 died. The session has been resumed
# since, so the replay reads only this prefix (it ends on a line break).
REAL_TRANSCRIPT_BYTES = 34_541_276
REAL_REPO = "/home/quincy/Programming/DegenAI/AIAgent/AIAgent"
REAL_WORKTREE = REAL_REPO + "/.worktrees/t-9142"


def _nested(path: str, chars: int, display: str = "x") -> str:
    # The real shape: content is an object carrying the text, and displayPath
    # is relative to wherever the shell stood (so it is deliberately unused).
    return json.dumps({
        "type": "attachment",
        "attachment": {
            "type": "nested_memory",
            "path": path,
            "content": {"path": path, "type": "Project", "content": "a" * chars},
            "displayPath": display,
        },
    }, separators=(",", ":"))


def _instructions(files: list[tuple[str, int]], spaced: bool = False) -> str:
    row = {
        "type": "attachment",
        "attachment": {
            "type": "instructions",
            "files": [
                {"path": p, "type": "Project", "content": "b" * n}
                for p, n in files
            ],
        },
    }
    return json.dumps(row) if spaced else json.dumps(row, separators=(",", ":"))


def _write_transcript(path: Path, repo: str, worktree: str | None) -> None:
    wt = worktree or repo
    lines = [
        '{"type":"user","message":{"content":"hello"}}',
        "this line is not json at all",
        '"a bare string mentioning nested_memory"',
        '{"type":"attachment","attachment":"nested_memory"}',
        # Big nested file, through the worktree twice and the main repo once.
        _nested(f"{wt}/Services/Automation/CLAUDE.md", BIG, "../CLAUDE.md"),
        _nested(f"{wt}/Services/Automation/CLAUDE.md", BIG, "Services/Automation/CLAUDE.md"),
        _nested(f"{repo}/Services/Automation/CLAUDE.md", BIG - 10),
        # Small nested file: below the threshold, never reported.
        _nested(f"{wt}/Services/Small/CLAUDE.md", SMALL),
        # Instructions row: one big project file, one small user file.
        _instructions([(f"{repo}/.claude/CLAUDE.md", BIG // 2 + 30_000),
                       ("/home/u/.claude/CLAUDE.md", SMALL)]),
        # Same file again in the spaced JSON spelling.
        _instructions([(f"{repo}/.claude/CLAUDE.md", BIG // 2 + 30_000)],
                      spaced=True),
        '{"type":"attachment","attachment":{"type":"nested_memory"',  # truncated
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _check_scanner(tmp: str, failures: list[str]) -> None:
    repo = os.path.join(tmp, "scan_repo")
    worktree = os.path.join(repo, ".worktrees", "t-1")
    transcript = Path(tmp) / "scan.jsonl"
    _write_transcript(transcript, repo, worktree)

    found = context_bloat.find_oversized_autoloads(
        transcript, 60_000, [repo, worktree],
    )
    names = [a.display_path for a in found]
    if names != ["Services/Automation/CLAUDE.md", ".claude/CLAUDE.md"]:
        failures.append(f"scanner found {names!r}, expected the two big files, "
                        "largest first, each named once")
        return
    nested, instr = found
    if nested.loads != 3:
        failures.append(
            f"the nested file was loaded 3 times (twice via the worktree, once "
            f"via the main repo) but counted {nested.loads}"
        )
    # The main repo copy was loaded last, at BIG - 10: the latest size wins.
    if nested.chars != BIG - 10 or nested.kind != context_bloat.KIND_NESTED:
        failures.append(f"nested finding should carry its last size: {nested!r}")
    if instr.loads != 2 or instr.kind != context_bloat.KIND_INSTRUCTIONS:
        failures.append(
            f"instructions file should count both spellings as 2 loads: {instr!r}"
        )

    if context_bloat.find_oversized_autoloads(
        Path(tmp) / "does-not-exist.jsonl", 60_000, [repo],
    ) != []:
        failures.append("a missing transcript must be no finding")
    if context_bloat.find_oversized_autoloads(transcript, 10_000_000, [repo]):
        failures.append("nothing is over 10M chars, yet something was reported")
    if context_bloat.scan_session("", repo, None) != []:
        failures.append("an empty session id must be no finding")

    # Big on its first two loads, slimmed before the third: no longer a cause.
    slimmed = Path(tmp) / "slimmed.jsonl"
    slimmed.write_text("\n".join([
        _nested(f"{repo}/Big/CLAUDE.md", BIG),
        _nested(f"{repo}/Big/CLAUDE.md", BIG),
        _nested(f"{repo}/Big/CLAUDE.md", SMALL),
    ]) + "\n", encoding="utf-8")
    if context_bloat.find_oversized_autoloads(slimmed, 60_000, [repo]):
        failures.append("a file slimmed before its last load was still blamed")

    note = context_bloat.render_note(found)
    if "`Services/Automation/CLAUDE.md`" not in note or "Agent tool" not in note:
        failures.append(f"note does not name the file and the delegation fix: {note!r}")
    if context_bloat.render_note([]) or context_bloat.cause_line([]):
        failures.append("no finding must render no note and no cause line")
    for text in (note, context_bloat.cause_line(found), config.CONTEXT_THRASH_BLOAT_NOTE):
        if "\u2014" in text:
            failures.append("em dash in user-facing text")


async def _check_runner(tmp: str, failures: list[str]) -> None:
    def place_transcript(h: _Harness, *, big: bool) -> None:
        path = (Path(h.account) / "projects" / encode_project_path(h.repo)
                / f"{BORN_SESSION}.jsonl")
        if big:
            _write_transcript(path, h.repo, None)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_nested(f"{h.repo}/x/CLAUDE.md", SMALL) + "\n")

    def ok() -> RunResult:
        return RunResult(is_error=False, session_id=BORN_SESSION,
                         result_text="done", num_turns=3)

    scans = {"n": 0}
    real_scan = context_bloat.scan_session

    def counting_scan(*a, **kw):
        scans["n"] += 1
        return real_scan(*a, **kw)

    context_bloat.scan_session = counting_scan  # type: ignore[assignment]
    try:
        # --- A: finding, thrash once then succeed ------------------------
        ha = _Harness(os.path.join(tmp, "a"), [_thrash_result(), ok()])
        place_transcript(ha, big=True)
        result, _ = await ha.run(session_id=None)
        if len(ha.prompts) < 2:
            failures.append("A: the thrash was not resumed at all")
        else:
            if "Services/Automation/CLAUDE.md" not in ha.prompts[1]:
                failures.append("A: the resumed prompt does not name the bloated file")
            if config.CONTEXT_THRASH_NUDGE not in ha.prompts[1]:
                failures.append("A: the general thrash nudge went missing")
            if "Services/Automation/CLAUDE.md" in ha.prompts[0]:
                failures.append("A: the first attempt already carried the note")
        if result.is_error:
            failures.append(f"A: the resumed success was lost: {result.error_message!r}")

        # --- B: finding, thrash forever -> cause line after the CLI text --
        scans["n"] = 0
        hb = _Harness(os.path.join(tmp, "b"), [_thrash_result()])
        place_transcript(hb, big=True)
        result_b, inst_b = await hb.run(session_id=None)
        err = result_b.error_message or ""
        if not err.startswith(THRASH):
            failures.append(f"B: the CLI's own text is no longer first: {err[:120]!r}")
        if "Cause: `Services/Automation/CLAUDE.md`" not in err:
            failures.append(f"B: exhausted thrash does not name the cause: {err!r}")
        if err.count("Cause:") != 1:
            failures.append(f"B: cause line appended {err.count('Cause:')} times")
        if scans["n"] != len(hb.prompts) or scans["n"] < 2:
            failures.append(
                f"B: {len(hb.prompts)} thrashed attempts scanned the transcript "
                f"{scans['n']} times; each thrash should scan once"
            )
        if getattr(inst_b, "_context_bloat_note", None):
            failures.append("B: the bloat note was left set after the run")

        # --- C/D: no finding is byte-identical to no transcript -----------
        hc = _Harness(os.path.join(tmp, "c"), [_thrash_result()])
        place_transcript(hc, big=False)  # only a small file
        result_c, _ = await hc.run(session_id=None)
        hd = _Harness(os.path.join(tmp, "d"), [_thrash_result()])
        result_d, _ = await hd.run(session_id=None)  # no transcript at all
        if result_c.error_message != THRASH or result_d.error_message != THRASH:
            failures.append(
                "C/D: with no finding the error must be the CLI text unchanged: "
                f"{result_c.error_message!r} / {result_d.error_message!r}"
            )
        if hc.prompts != hd.prompts:
            failures.append("C/D: a below-threshold transcript changed the prompts")
        if any("Work around them" in p for p in hc.prompts + hd.prompts):
            failures.append("C/D: the bloat note appeared with no finding")
        expected = config.CONTEXT_THRASH_NUDGE + "\n\n"
        if len(hd.prompts) > 1 and expected not in hd.prompts[1]:
            failures.append("C/D: the resumed prompt lost the plain nudge layout")
    finally:
        context_bloat.scan_session = real_scan  # type: ignore[assignment]


def _check_real_transcript(tmp: str, failures: list[str]) -> str:
    if not REAL_TRANSCRIPT.is_file() \
            or REAL_TRANSCRIPT.stat().st_size < REAL_TRANSCRIPT_BYTES:
        return "skipped (t-9142 transcript not on this machine)"
    prefix = Path(tmp) / "t-9142.jsonl"
    with open(REAL_TRANSCRIPT, "rb") as src, open(prefix, "wb") as dst:
        remaining = REAL_TRANSCRIPT_BYTES
        while remaining:
            chunk = src.read(min(remaining, 1 << 20))
            if not chunk:
                break
            dst.write(chunk)
            remaining -= len(chunk)
    roots = context_bloat.display_roots(REAL_REPO, REAL_WORKTREE)
    found = context_bloat.find_oversized_autoloads(
        prefix, config.CONTEXT_BLOAT_MIN_CHARS, roots,
    )
    top = found[0] if found else None
    if top is None or top.display_path != "Services/Automation/CLAUDE.md" \
            or top.chars <= 300_000:
        failures.append(f"real t-9142 transcript: expected the 317K "
                        f"Services/Automation/CLAUDE.md first, got {top!r}")
        return "failed"
    names = [a.display_path for a in found]
    if len(names) != len(set(names)):
        failures.append(f"real t-9142 transcript: a file was named twice: {names!r}")
    return f"{top.display_path} {top.chars} chars, {top.loads} loads"


async def _amain() -> int:
    failures: list[str] = []
    tmp = tempfile.mkdtemp(prefix="bloat_test_")
    try:
        _check_scanner(tmp, failures)
        await _check_runner(tmp, failures)
        real = _check_real_transcript(tmp, failures)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL: context bloat diagnosis")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: an oversized auto-attached file is named to the resumed attempt")
    print("      and in the exhausted error, after the CLI's own text; no finding")
    print(f"      changes nothing. Real replay: {real}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
