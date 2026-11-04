"""Step three of the judge replay: build and freeze the labelled cases.

Three groups, split by thread so nothing from a held-out thread teaches the one
allowed prompt revision:

  miss        a labelled correction; the judge sees the work as it stood when the
              author's turn ended, and never the complaint
  clean       work with no correction in its thread and no later fix to its files
              for 14 days
  review_fix  work a "Review fixes" commit followed, as it stood before that commit;
              the issues to catch come from that commit's message

Every input is written here, once, through the real `build_input` and `repo_goal`, so
what a judge run reads is fixed before there is a result to tune it against. Raw text
stays under data/judge_replay/. `pick` prints the pool and `freeze` writes the cases.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import subprocess
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "replay_ab"))

from common import LIVE_DATA  # noqa: E402
from fetch_threads import CACHE, RAW, credentials, fetch  # noqa: E402
from mine import NOW, REPOS, is_owner_text, load_rows  # noqa: E402

SEED = 20261104
MISSES, CLEAN, REVIEW = 18, 15, 10
MAX_PER_REPO = 5
MAX_DIFF_CHARS = 80_000
QUIET = timedelta(days=14)
_GIT = ["git", "-c", "core.hooksPath=/dev/null"]

_BORING = re.compile(r"(^|/)(CHANGELOG\.md|TODO\.md|CLAUDE\.md|STATE\.md|pyproject\.toml|package\.json|[^/]+\.csproj)$")
_FIXISH = re.compile(r"\b(fix\w*|bugs?|correct\w*|revert\w*|repair\w*|restor\w*|wrong|broken|missing)\b", re.I)
_REVIEW_FIX = re.compile(r"\breview fix(es)?\b", re.I)
_RELEASE = re.compile(r"^v\d+\.\d+")
_MERGE = re.compile(r"^Merge ")

# Looked at before any run: the owner's next message shows this work was not clean.
# Filled in from `pick`, which prints that message.
CLEAN_REJECT: set[str] = {"7cd6d1d0bfc5"}


def _when(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def git_out(repo: str, *args: str) -> str:
    r = subprocess.run([*_GIT, *args], cwd=repo, capture_output=True, text=True,
                       errors="replace", timeout=180)
    return r.stdout if r.returncode == 0 else ""


def commit_diffs(repo: str, shas: list[str]) -> str:
    """The form the chat judge reads (`judge._commit_diffs`), messages left out."""
    out = []
    for sha in shas:
        shown = git_out(repo, "show", "--format=", "--stat", "--patch", "--no-ext-diff", "--no-textconv", sha)
        out.append(f"--- commit {sha[:12]} ---\n{shown}")
    return "\n".join(out)


def later_changes(repo: str) -> list[tuple[float, str, str, set[str]]]:
    """Every commit of the window: (when, sha, subject, files), oldest first."""
    since = (NOW - timedelta(days=31)).isoformat()
    text = git_out(repo, "log", "--all", "--format=%x01%H%x00%s%x00%ct", "--name-only", f"--since={since}")
    rows = []
    for chunk in text.split("\x01")[1:]:
        head, _, rest = chunk.partition("\n")
        sha, subject, when = head.split("\x00")
        rows.append((float(when), sha, subject, {ln for ln in rest.splitlines() if ln.strip()}))
    rows.sort(key=lambda r: r[0])
    return rows


def spoke_times(rows: list[dict]) -> dict[str, list[datetime]]:
    out: dict[str, list[datetime]] = defaultdict(list)
    for r in rows:
        if r.get("origin") == "direct" and is_owner_text(r.get("topic")):
            out[r["thread_id"]].append(r["_t"])
    for v in out.values():
        v.sort()
    return out


def batches_of(commits: list[dict], spoke: list[datetime]) -> list[list[dict]]:
    """Commits grouped by the owner message they answered: one turn or one chain."""
    groups: dict[int, list[dict]] = defaultdict(list)
    for c in commits:
        t = datetime.fromtimestamp(c["ts"], timezone.utc)
        groups[bisect_right(spoke, t)].append(c)
    return [groups[k] for k in sorted(groups)]


def clean_pool(paths: dict[str, str], timeline: dict, candidates: list[dict], rows: list[dict]) -> list[dict]:
    spoke = spoke_times(rows)
    repo_of = {r["thread_id"]: r["repo"] for r in rows}
    flagged = {sha for c in candidates for sha in c["batch"]}
    changes = {repo: later_changes(path) for repo, path in paths.items()}
    cutoff = (NOW - QUIET).timestamp()
    pool = []
    for tid, commits in timeline.items():
        repo = repo_of.get(tid)
        if repo not in paths or not spoke.get(tid):
            continue
        for batch in batches_of(commits, spoke[tid]):
            shas = {c["sha"] for c in batch}
            end = max(c["ts"] for c in batch)
            if end >= cutoff or shas & flagged:
                continue
            if all(_RELEASE.match(c["subject"]) or _MERGE.match(c["subject"]) for c in batch):
                continue
            files = {f for c in batch for f in c["files"] if not _BORING.search(f)}
            if not files:
                continue
            later = [row for row in changes[repo]
                     if end < row[0] <= end + QUIET.total_seconds()
                     and row[1] not in shas and row[3] & files and _FIXISH.search(row[2])]
            if later:
                continue
            # Nothing that reads as a review fix inside the batch either: those cases are the third group.
            if any(_REVIEW_FIX.search(c["subject"]) for c in batch):
                continue
            pool.append({"repo": repo, "thread": tid, "batch": [c["sha"] for c in sorted(batch, key=lambda c: c["ts"])],
                         "end": end, "subjects": [c["subject"][:80] for c in batch]})
    return pool


def issues_of(message: str) -> list[str]:
    """The issues a review-fix commit names: its bullets, else the clauses of its subject."""
    lines = [ln.strip() for ln in message.splitlines()]
    bullets: list[str] = []
    for ln in message.splitlines():
        if ln.startswith(("- ", "* ")):
            bullets.append(ln[2:].strip())
        elif bullets and ln.startswith(" ") and ln.strip():
            bullets[-1] += " " + ln.strip()  # a hard-wrapped bullet
    if bullets:
        return bullets[:10]
    subject = lines[0] if lines else ""
    body = [ln for ln in lines[1:] if ln and not re.match(r"^[A-Z][\w-]+: ", ln)]
    if ":" in subject:
        tail = subject.split(":", 1)[1]
        parts = [p.strip() for p in tail.split(",") if p.strip()]
        if len(parts) > 1:
            return parts[:10]
    if body:
        text = " ".join(body)
        parts = [p.strip() for p in re.split(r"(?<=[.])\s+", text) if len(p.strip()) > 15]
        if parts:
            return parts[:10]
    return [subject]


def review_pool(paths: dict[str, str], timeline: dict, rows: list[dict]) -> list[dict]:
    spoke = spoke_times(rows)
    repo_of = {r["thread_id"]: r["repo"] for r in rows}
    pool = []
    for tid, commits in timeline.items():
        repo = repo_of.get(tid)
        if repo not in paths or not spoke.get(tid):
            continue
        ordered = sorted(commits, key=lambda c: c["ts"])
        for i, c in enumerate(ordered):
            if not _REVIEW_FIX.search(c["subject"]):
                continue
            t = datetime.fromtimestamp(c["ts"], timezone.utc)
            n = bisect_right(spoke[tid], t)
            since = spoke[tid][n - 1].timestamp() if n else 0.0
            before = [p for p in ordered[:i] if p["ts"] >= since and not _REVIEW_FIX.search(p["subject"])]
            if not before:
                continue
            path = paths[repo]
            parent = git_out(path, "rev-parse", f"{c['sha']}^").strip()
            issues = issues_of(git_out(path, "log", "-1", "--format=%B", c["sha"]))
            pool.append({"repo": repo, "thread": tid, "batch": [p["sha"] for p in before], "head": parent,
                         "end": c["ts"], "fix": c["sha"], "subjects": [c["subject"][:80]], "issues": issues})
    # A second review fix over the same work is the same case. The first one saw it first.
    first: dict[tuple, dict] = {}
    for p in sorted(pool, key=lambda p: p["end"]):
        first.setdefault((p["repo"], tuple(p["batch"])), p)
    return list(first.values())


def round_robin(pool: list[dict], want: int, rng: random.Random) -> list[dict]:
    """A fixed-seed draw that spreads over the repos and does not repeat a thread's batch needlessly."""
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for p in sorted(pool, key=lambda p: (p["repo"], p["end"])):
        by_repo[p["repo"]].append(p)
    for v in by_repo.values():
        rng.shuffle(v)
    picked: list[dict] = []
    repos = sorted(by_repo)
    rng.shuffle(repos)
    while len(picked) < want and any(by_repo[r] for r in repos):
        for r in repos:
            if by_repo[r] and len(picked) < want and sum(p["repo"] == r for p in picked) < MAX_PER_REPO:
                picked.append(by_repo[r].pop())
        if all(sum(p["repo"] == r for p in picked) >= MAX_PER_REPO or not by_repo[r] for r in repos):
            break
    return picked


def pick_misses(labels: list[dict], rng: random.Random) -> list[dict]:
    """Every too-narrow miss, because they are reported on their own, then a spread of the rest."""
    narrow = [r for r in labels if r["kind"] == "too_narrow"]
    rest = [r for r in labels if r["kind"] != "too_narrow"]
    rng.shuffle(rest)
    picked = list(narrow)
    counts = Counter(r["repo"] for r in picked)
    for r in rest:
        if len(picked) < MISSES and counts[r["repo"]] < MAX_PER_REPO:
            picked.append(r)
            counts[r["repo"]] += 1
    return picked


def split_by_thread(cases: list[dict], rng: random.Random) -> None:
    """A third to develop on, the rest untouched, and never one thread on both sides."""
    totals = Counter(c["group"] for c in cases)
    threads = sorted({c["thread"] for c in cases})
    rng.shuffle(threads)
    dev: Counter = Counter()
    for tid in threads:
        mine = [c for c in cases if c["thread"] == tid]
        here = Counter(c["group"] for c in mine)
        fits = all(dev[g] + n <= totals[g] / 3 for g, n in here.items())
        for c in mine:
            c["split"] = "dev" if fits else "held_out"
        if fits:
            dev.update(here)


def verify_id_of(rows: list[dict], tid: str, start: float, cut: float) -> str | None:
    """A verify run over this work, finished before the cut, if there was one."""
    hits = [r for r in rows if r["thread_id"] == tid and r.get("origin") == "verify"
            and start <= r["_t"].timestamp() <= cut and r.get("status") == "completed"]
    return hits[-1]["id"] if hits else None


def owner_words(thread: str, cut: float) -> list[str]:
    """What `adapter.owner_messages` would have returned at that moment."""
    msgs = json.loads((CACHE / f"{thread}.json").read_text(encoding="utf-8"))
    out = []
    for m in msgs:
        if _when(m["ts"]).timestamp() > cut:
            continue
        text = (m["text"] or "").strip()
        names = ", ".join(m.get("attachments") or [])
        if names:
            text = f"{text}\n[attached: {names}]".strip()
        if text:
            out.append(text)
    return out


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "pick"
    state = json.loads((LIVE_DATA / "state.json").read_text(encoding="utf-8"))
    paths = {k: state["repos"][k] for k in REPOS}
    labels = [json.loads(ln) for ln in (RAW / "labels.jsonl").open(encoding="utf-8")]
    candidates = [json.loads(ln) for ln in (RAW / "candidates.jsonl").open(encoding="utf-8")]
    timeline = json.loads((RAW / "timeline.json").read_text(encoding="utf-8"))
    rows = load_rows()
    token, owner = credentials()

    rng = random.Random(SEED)
    misses = pick_misses(labels, rng)
    pool_clean = clean_pool(paths, timeline, candidates, rows)
    pool_review = review_pool(paths, timeline, rows)
    print(f"clean pool {len(pool_clean)}, review-fix pool {len(pool_review)}", flush=True)
    print("clean by repo", dict(Counter(p["repo"] for p in pool_clean)))
    print("review by repo", dict(Counter(p["repo"] for p in pool_review)))

    def usable(p: dict) -> bool:
        if fetch(p["thread"], token, owner) is None or not owner_words(p["thread"], p["end"]):
            return False
        chars = sum(len(s) for s in (commit_diffs(paths[p["repo"]], p["batch"]),))
        p["diff_chars"] = chars
        return 200 <= chars <= MAX_DIFF_CHARS

    clean_rng = random.Random(SEED + 1)
    clean_pool_ok = [p for p in pool_clean if p["batch"][-1][:12] not in CLEAN_REJECT]
    clean_rng.shuffle(clean_pool_ok)
    # Bounded before drawing so a giant diff is not a reason a repo goes missing.
    picked_clean: list[dict] = []
    seen = Counter()
    for p in clean_pool_ok:
        if len(picked_clean) >= CLEAN or seen[p["repo"]] >= MAX_PER_REPO:
            continue
        if usable(p):
            picked_clean.append(p)
            seen[p["repo"]] += 1
    review_rng = random.Random(SEED + 2)
    review_ok = [p for p in pool_review if usable(p)]
    print(f"review-fix usable {len(review_ok)}", flush=True)
    picked_review = round_robin(review_ok, REVIEW, review_rng)

    if mode == "pick":
        for p in picked_clean:
            msgs = json.loads((CACHE / f"{p['thread']}.json").read_text(encoding="utf-8"))
            nxt = [m["text"] for m in msgs if _when(m["ts"]).timestamp() > p["end"]][:2]
            print(f"CLEAN {p['repo']} {p['batch'][-1][:12]} chars={p['diff_chars']} {p['subjects'][-1][:60]!r}\n   NEXT: {[n[:220] for n in nxt]}")
        for p in picked_review:
            print(f"REVIEW {p['repo']} {p['fix'][:12]} chars={p['diff_chars']} {p['subjects'][0][:70]!r} issues={len(p['issues'])}")
            for i in p["issues"][:6]:
                print("    -", i[:110])
        return 0

    cases: list[dict] = []
    for r in misses:
        cases.append({"group": "miss", "repo": r["repo"], "thread": r["thread"], "batch": r["batch"],
                      "head": r["batch"][-1], "cut": _when(r["batch_end_ts"]).timestamp(),
                      "correction_ts": _when(r["message_ts"]).timestamp(),
                      "kind": r["kind"], "looks": r["looks"], "correction": r["correction"],
                      "corrects": r["index"]})
    for p in picked_clean:
        cases.append({"group": "clean", "repo": p["repo"], "thread": p["thread"], "batch": p["batch"],
                      "head": p["batch"][-1], "cut": p["end"]})
    for p in picked_review:
        cases.append({"group": "review_fix", "repo": p["repo"], "thread": p["thread"], "batch": p["batch"],
                      "head": p["head"], "cut": p["end"] - 1, "issues": p["issues"], "fix": p["fix"]})
    split_by_thread(cases, random.Random(SEED + 3))
    for i, c in enumerate(cases):
        c["id"] = f"{c['group']}-{i:02d}"
        ts_of = {x["sha"]: x["ts"] for x in timeline[c["thread"]]}
        start = min(ts_of.get(s, c["cut"]) for s in c["batch"])
        # A verify that ran before a review-fix commit would be out of order, so that group has none.
        latest = min(c["cut"] + 86400, c.get("correction_ts", float("inf")))
        c["verify_id"] = None if c["group"] == "review_fix" else verify_id_of(rows, c["thread"], start, latest)
        c["owner"] = owner_words(c["thread"], c["cut"])
        c["diff"] = commit_diffs(paths[c["repo"]], c["batch"])
        c["repo_path"] = paths[c["repo"]]

    out = RAW / "cases.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (RAW / "cases.sha256").write_text(digest + "\n", encoding="utf-8")
    print("cases", dict(Counter((c["group"], c["split"]) for c in cases)))
    print("too narrow", dict(Counter(c["split"] for c in cases if c.get("kind") == "too_narrow")))
    print("looks", dict(Counter(c["split"] for c in cases if c.get("looks"))))
    print("with verify", sum(1 for c in cases if c["verify_id"]), "of", len(cases))
    print("repos", dict(Counter(c["repo"] for c in cases)))
    print("frozen", digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
