"""Recover the verify output of the frozen cases from the session transcripts.

The bot's own result files for those runs have been pruned, so the first development
round saw no verify output at all. The transcript of the run still has the final answer.
Only text written inside the run's own start and finish times counts. What is recovered
goes in data/judge_replay/verify/<run id>.txt, and the frozen cases are not touched.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "replay_ab"))

from common import LIVE_DATA  # noqa: E402
from mine import session_of_runs, transcript_index  # noqa: E402

RAW = LIVE_DATA / "judge_replay"
OUT = RAW / "verify"
BLOCK = re.compile(r"```verify\s*\n.*?\n```", re.IGNORECASE | re.DOTALL)


def _when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def history_rows(ids: set[str]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with (LIVE_DATA / "history.jsonl").open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("id") in ids:
                rows[row["id"]] = row
    return rows


def verify_text(transcript: Path, start: datetime, end: datetime) -> str:
    """The last answer with a verify block, written while this run was running."""
    found = ""
    margin = timedelta(seconds=60)
    with transcript.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if '```verify' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            stamp = row.get("timestamp")
            if not stamp or not start - margin <= _when(stamp) <= end + margin:
                continue
            content = (row.get("message") or {}).get("content")
            if row.get("type") != "assistant" or not isinstance(content, list):
                continue
            for part in content:
                if part.get("type") == "text" and BLOCK.search(part.get("text", "")):
                    found = part["text"]
    return found


def main() -> int:
    cases = [json.loads(ln) for ln in (RAW / "cases.jsonl").open(encoding="utf-8")]
    wanted = {c["verify_id"]: c for c in cases if c.get("verify_id")}
    rows = history_rows(set(wanted))
    sessions = session_of_runs()
    transcripts = transcript_index()
    OUT.mkdir(exist_ok=True)
    got = 0
    for vid, case in sorted(wanted.items()):
        row, sid = rows.get(vid), sessions.get(vid)
        path = transcripts.get(sid) if sid else None
        text = ""
        if row and path:
            text = verify_text(path, _when(row["started"]), _when(row["finished"]))
        if text:
            (OUT / f"{vid}.txt").write_text(text, encoding="utf-8")
            got += 1
        print(f"{case['id']} {case['split']} {vid}: "
              f"{'recovered ' + str(len(text)) if text else 'none'} (session {'yes' if sid else 'no'}, "
              f"transcript {'yes' if path else 'no'})")
    print(f"recovered {got} of {len(wanted)} runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
