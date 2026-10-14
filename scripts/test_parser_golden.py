#!/usr/bin/env python3
"""Golden-output test for bot/claude/parser.py.

A refactor of the parser (or of anything it leans on, such as
``models.context_tokens_from_usage`` or the ``RunResult`` dataclass) must not
change what the runner gets back for the same stream-json. This feeds a fixed
set of stream-json inputs through the parser and compares every output with a
checked-in golden file, so drift shows up as a named case with a field diff
instead of as a Discord card that reads slightly wrong a week later.

What is covered, per ``kind`` in the fixture:

  stream        a whole run: ``extract_result`` (every RunResult field that is
                not its default), ``last_assistant_text``, and for each event
                ``extract_progress``, ``extract_usage`` and ``iter_tool_blocks``
                with the ``_tool_detail`` of each block.
  raw_line      ``parse_stream_line`` on blank, malformed and valid lines.
  tool_detail   ``_tool_detail`` on edge cases for every branch (missing keys,
                truncation limits, non-dict input, unknown tools).
  ask_question  ``format_ask_question`` on well-formed and junk input.

Only fields that differ from a bare ``RunResult()`` are stored, so a new
RunResult field that the parser never sets does not churn the golden file,
while a parser that starts (or stops) setting one does.

Files:
  scripts/fixtures/parser/cases.jsonl   inputs, one case per line
  scripts/fixtures/parser/golden.json   expected outputs, keyed by case id

The fixtures are sanitised: home directories are rewritten to /opt/fixture,
emails and token-shaped strings are scrubbed, long strings are truncated and
assistant text is filler of the original length. Each case's ``source`` field
says whether its events are real (taken from local transcripts or the bot's
own logs) or derived (hand-built to the real shape to reach a branch).

Run:    python scripts/test_parser_golden.py
Regen:  python scripts/test_parser_golden.py --regen
``--regen`` rewrites golden.json from the CURRENT parser output and exits 0.
Only do that when the change in output is intended, and read the diff of
golden.json before committing it: that diff is the whole point of the file.
Exit 0 = every case matches, 1 = drift (or fixture and golden out of step).
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.claude import parser  # noqa: E402
from bot.claude.types import RunResult  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "parser"
CASES = FIXTURE_DIR / "cases.jsonl"
GOLDEN = FIXTURE_DIR / "golden.json"

_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}" + (f"  ({detail})" if detail else ""))
        _failures.append(name)


def _jsonable(obj):
    """Round-trip through json so tuples, datetimes and the like compare as stored."""
    return json.loads(json.dumps(obj, default=str, sort_keys=True))


def _call(fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        return {"raises": type(e).__name__}


def _result_fields(result: RunResult) -> dict:
    base = dataclasses.asdict(RunResult())
    got = dataclasses.asdict(result)
    return {k: v for k, v in got.items() if k not in base or base[k] != v}


def _run_stream(events: list) -> dict:
    per_event = []
    for i, ev in enumerate(events):
        progress = _call(parser.extract_progress, ev)
        if dataclasses.is_dataclass(progress):
            progress = dataclasses.asdict(progress)
        usage = _call(parser.extract_usage, ev)
        blocks = _call(lambda e: list(parser.iter_tool_blocks(e)), ev)
        if isinstance(blocks, list):
            blocks = [
                {"name": n, "input": inp, "detail": _call(parser._tool_detail, n, inp)}
                for n, inp in blocks
            ]
        entry = {"i": i, "type": ev.get("type") if isinstance(ev, dict) else None}
        if progress is not None:
            entry["progress"] = progress
        if usage is not None:
            entry["usage"] = usage
        if blocks:
            entry["tool_blocks"] = blocks
        if len(entry) > 2:
            per_event.append(entry)
    result = _call(parser.extract_result, events)
    return {
        "result": _result_fields(result) if isinstance(result, RunResult) else result,
        "last_assistant_text": _call(parser.last_assistant_text, events),
        "per_event": per_event,
    }


def run_case(case: dict):
    kind = case["kind"]
    if kind == "stream":
        return _run_stream(case["events"])
    if kind == "raw_line":
        return _call(parser.parse_stream_line, case["line"])
    if kind == "tool_detail":
        return _call(parser._tool_detail, case["tool"], case["tool_input"])
    if kind == "ask_question":
        return _call(parser.format_ask_question, case["tool_input"])
    raise ValueError(f"unknown fixture kind {kind!r} in case {case.get('id')!r}")


def load_cases() -> list[dict]:
    cases = []
    for n, line in enumerate(CASES.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"{CASES.name}:{n} is not valid JSON: {e}")
    return cases


def _diff(expected, actual, path="") -> list[str]:
    """Short human-readable list of where two json values differ."""
    out: list[str] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k in sorted(set(expected) | set(actual)):
            if k not in actual:
                out.append(f"{path}.{k}: missing (expected {_short(expected[k])})")
            elif k not in expected:
                out.append(f"{path}.{k}: unexpected {_short(actual[k])}")
            else:
                out.extend(_diff(expected[k], actual[k], f"{path}.{k}"))
    elif isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        for i, (e, a) in enumerate(zip(expected, actual)):
            out.extend(_diff(e, a, f"{path}[{i}]"))
    elif expected != actual:
        out.append(f"{path or '.'}: expected {_short(expected)} got {_short(actual)}")
    return out


def _short(v, limit: int = 90) -> str:
    s = json.dumps(v, sort_keys=True, default=str)
    return s if len(s) <= limit else s[:limit] + "..."


def main() -> int:
    regen = "--regen" in sys.argv[1:]
    cases = load_cases()
    ids = [c["id"] for c in cases]
    _check("fixture case ids are unique", len(ids) == len(set(ids)))

    # Fixtures must stay portable (scripts/check_portability.py only reads .py).
    needle = "/ho" + "me/"
    raw = CASES.read_text(encoding="utf-8")
    _check("fixtures contain no home-directory paths", needle not in raw)

    actual = {c["id"]: _jsonable(run_case(c)) for c in cases}

    # The run cost, asserted by name and not only through the golden file: a
    # --regen would bless a parser that went back to reading only cost_usd,
    # which recorded every run at $0 for months.
    by_id = {c["id"]: c for c in cases}
    if "stream_cost_total_only" in by_id:
        got = parser.extract_result(by_id["stream_cost_total_only"]["events"]).cost_usd
        _check("total_cost_usd alone is read as the run cost", got == 1.0731, str(got))
    if "stream_tokens_top_level" in by_id:
        got = parser.extract_result(by_id["stream_tokens_top_level"]["events"]).cost_usd
        _check("total_cost_usd wins over the older cost_usd", got == 0.4213, str(got))
    legacy = parser.extract_result([{"type": "result", "cost_usd": 0.25}]).cost_usd
    _check("cost_usd alone is still read", legacy == 0.25, str(legacy))

    if regen:
        GOLDEN.write_text(
            json.dumps(actual, indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"regenerated {GOLDEN.relative_to(GOLDEN.parents[2].parent)}: {len(actual)} cases")
        return 0 if not _failures else 1

    if not GOLDEN.exists():
        _check("golden file exists (run with --regen to create it)", False, str(GOLDEN))
        return 1
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    _check(
        "every fixture case has a golden entry and vice versa",
        set(golden) == set(actual),
        f"only in fixtures: {sorted(set(actual) - set(golden))[:5]}, "
        f"only in golden: {sorted(set(golden) - set(actual))[:5]}",
    )
    for case in cases:
        cid = case["id"]
        if cid not in golden:
            continue
        diffs = _diff(golden[cid], actual[cid])
        _check(
            f"{cid}",
            not diffs,
            "; ".join(diffs[:4]) + (f"; +{len(diffs) - 4} more" if len(diffs) > 4 else ""),
        )

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} parser golden check(s) drifted (use --regen only if intended)")
        return 1
    print(f"PASS: parser output matches golden for all {len(cases)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
