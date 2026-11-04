"""Step one of the judge replay: find what the owner caught after work was called done.

Reads the live bot's history, its logs (to get from a run to its session) and the
session transcripts. Nothing here runs a model or writes outside the raw directory.

A candidate is an owner message in a thread, sent after that thread's session
committed, which reads like a correction and which was followed by a commit in the
same thread touching a file the earlier commit touched. That last part is what
links it: a complaint nobody acted on, or one with no earlier commit to point at,
is dropped. The candidates get a kind by hand afterwards (label.py).

Raw text stays under data/judge_replay/, which git ignores. See docs/design/judge.md.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "replay_ab"))

sys.path.insert(0, str(HERE.parents[1]))

from bot.engine.commit_calls import LOG_FORMAT, match_commits, parse_log, read_commit_calls  # noqa: E402
from common import LIVE_DATA  # noqa: E402

RAW = LIVE_DATA / "judge_replay"
NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)
WINDOW = timedelta(days=30)

# mindroom, vin-coach and every other personal repo stay out by not being here.
REPOS = ("aiagent", "Ev-nova-remake", "halo-apex", "strategy-lab", "smartmoney",
         "funny-rock-polisher", "bot")

_RUN_RE = re.compile(r"Running (?P<id>[a-z]-\d+) .*?--resume (?P<sid>[0-9a-f-]{36})")
_NOT_OWNER = re.compile(
    r"^\s*(\[Background context|\[Self-wake|\[Wake|\[System|\[Watch|Now implement|<|Continue\b|"
    r"Check whether|The /watch|Your watch|A /watch|Spawned children|Your spawned)",
    re.I,
)
CUE = re.compile(
    r"\b(still|again|wrong|broken|broke|breaks|doesn'?t|dont|don'?t|does not|didn'?t|did not|"
    r"not working|isn'?t|aren'?t|wasn'?t|no longer|regress\w*|bugs?|crash\w*|missing|forgot|"
    r"supposed|meant|instead|why|should|fails?|failed|failing|error|weird|off|ugly|looks?|"
    r"feels?|only|just one|other|all of|every|rest|same)\b",
    re.I,
)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def load_rows() -> list[dict]:
    cut = NOW - WINDOW
    rows = []
    with open(LIVE_DATA / "history.jsonl", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            if r.get("repo") not in REPOS or not r.get("thread_id"):
                continue
            started = parse_ts(r.get("started"))
            if started and started >= cut:
                r["_t"] = started
                rows.append(r)
    rows.sort(key=lambda r: r["_t"])
    return rows


def session_of_runs() -> dict[str, str]:
    """instance id -> session id, from every run line in the logs."""
    out: dict[str, str] = {}
    for path in sorted((LIVE_DATA / "logs").glob("bot.log*")):
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "Running " not in line or "--resume" not in line:
                    continue
                m = _RUN_RE.search(line)
                if m:
                    out.setdefault(m.group("id"), m.group("sid"))
    return out


def transcript_index() -> dict[str, Path]:
    """session id -> its largest transcript, across accounts."""
    found: dict[str, Path] = {}
    for acct in sorted(Path.home().glob(".claude*")):
        projects = acct / "projects"
        if not projects.is_dir():
            continue
        for p in projects.glob("*/*.jsonl"):
            cur = found.get(p.stem)
            if cur is None or p.stat().st_size > cur.stat().st_size:
                found[p.stem] = p
    return found


def repo_log(repo_path: str) -> list[tuple[str, str, float]]:
    r = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "log", "--all", LOG_FORMAT,
         f"--since={(NOW - WINDOW - timedelta(days=1)).isoformat()}"],
        cwd=repo_path, capture_output=True, text=True, errors="replace", timeout=120,
    )
    return parse_log(r.stdout)


def files_of(repo_path: str, sha: str) -> list[str]:
    r = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "show", "--no-ext-diff", "--no-textconv",
         "--name-only", "--format=", sha],
        cwd=repo_path, capture_output=True, text=True, errors="replace", timeout=60,
    )
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def is_owner_text(topic: str) -> bool:
    t = (topic or "").strip()
    return bool(t) and not _NOT_OWNER.match(t) and len(t) > 3


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    state = json.loads((LIVE_DATA / "state.json").read_text(encoding="utf-8"))
    paths = {k: state["repos"][k] for k in REPOS}
    rows = load_rows()
    runs = session_of_runs()
    index = transcript_index()
    print(f"rows {len(rows)}, runs with a session {len(runs)}, transcripts {len(index)}", flush=True)

    threads: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        threads[r["thread_id"]].append(r)

    # Every session a thread's runs used, then the commits those sessions made.
    logs = {repo: repo_log(path) for repo, path in paths.items()}
    by_sha = {repo: {sha: (subject, when) for sha, subject, when in log} for repo, log in logs.items()}
    calls_of: dict[str, list] = {}
    timeline: dict[str, list[dict]] = {}
    calls_seen = pinned = unpinned = 0
    for tid, trows in threads.items():
        repo = trows[0]["repo"]
        sessions = {runs[r["id"]] for r in trows if r["id"] in runs}
        commits: dict[str, dict] = {}
        for sid in sessions:
            if sid not in index:
                continue
            if sid not in calls_of:
                calls_of[sid] = read_commit_calls(index[sid])
            calls = [c for c in calls_of[sid] if c.ts is None or c.ts >= (NOW - WINDOW).timestamp()]
            shas, unattributed = match_commits(calls, logs[repo])
            calls_seen += len(calls)
            pinned += len(shas)
            unpinned += len(unattributed)
            for sha in shas:
                subject, when = by_sha[repo][sha]
                commits[sha] = {
                    "sha": sha, "subject": subject, "session": sid, "ts": when,
                    "_t": datetime.fromtimestamp(when, timezone.utc),
                    "files": files_of(paths[repo], sha),
                }
        timeline[tid] = sorted(commits.values(), key=lambda c: c["_t"])
    print(f"threads {len(threads)}, with commits {sum(1 for v in timeline.values() if v)}, "
          f"commit calls {calls_seen}, pinned {pinned}, not pinned {unpinned}", flush=True)

    candidates: list[dict] = []
    for tid, trows in threads.items():
        commits = timeline[tid]
        if not commits:
            continue
        repo = trows[0]["repo"]
        direct = [r for r in trows if r.get("origin") == "direct" and is_owner_text(r.get("topic"))]
        for msg in direct:
            before = [c for c in commits if c["_t"] < msg["_t"]]
            after = [c for c in commits if c["_t"] >= msg["_t"]]
            if not before or not after or not CUE.search(msg["topic"]):
                continue
            # The next commit after this message must touch what was committed before it.
            nxt_time = None
            nxt = [r for r in direct if r["_t"] > msg["_t"]]
            if nxt:
                nxt_time = nxt[0]["_t"]
            answer = [c for c in after if nxt_time is None or c["_t"] < nxt_time]
            if not answer:
                continue
            fixed = {f for c in answer for f in c["files"]}
            corrected = [c for c in before if fixed & set(c["files"])]
            if not corrected:
                continue
            # The work this corrects: everything since the owner last spoke before
            # the corrected commit, which is one turn or one whole chain.
            last = corrected[-1]
            spoke = [r["_t"] for r in direct if r["_t"] < last["_t"]]
            since = spoke[-1] if spoke else datetime.min.replace(tzinfo=timezone.utc)
            batch = [c for c in before if since <= c["_t"] <= last["_t"]]
            gap_h = (msg["_t"] - last["_t"]).total_seconds() / 3600
            candidates.append({
                "repo": repo, "thread": tid, "instance": msg["id"],
                "message_ts": msg["_t"].isoformat(), "message_head": msg["topic"],
                "batch": [c["sha"] for c in batch],
                "batch_subjects": [c["subject"][:90] for c in batch],
                "batch_end_ts": last["_t"].isoformat(),
                "answer": [c["sha"] for c in answer],
                "answer_subjects": [c["subject"][:90] for c in answer],
                "overlap": sorted(fixed & {f for c in batch for f in c["files"]})[:8],
                "gap_hours": round(gap_h, 1),
            })

    out = RAW / "candidates.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for c in candidates:
            f.write(json.dumps(c) + "\n")
    # Timelines too: the clean and review-fix sampling reads them without another pass.
    with (RAW / "timeline.json").open("w", encoding="utf-8") as f:
        json.dump({t: [{k: v for k, v in c.items() if k != "_t"} for c in cs]
                   for t, cs in timeline.items() if cs}, f)
    print(f"{len(candidates)} candidates written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
