"""Recover everything needed to replay one finished build, read-only.

Given an instance id this finds, without touching the live bot's state:

  session_id   the CLI session the build resumed (``--resume`` on its run line)
  account      the config dir whose ``projects/`` holds that transcript
  transcript   the session JSONL
  prompt       the build's user message, verbatim from the transcript
  cut_uuid     the row the build prompt hangs off (its ``parentUuid``); the
               replay forks the conversation there, so it starts from exactly
               the context the original build started from
  base_sha     merge-base of the merge's two parents: the tree the build saw
  ref_tip      the merge's second parent: the shipped solution, used only for
               scoring and never reachable from the sandbox
  ref_tests    harnesses the solution added or changed (fail-to-pass candidates)
  model/effort the flags the original run used

Sources, in order: ``data/history.jsonl`` (pruned instances live on there),
the ``Running <id>`` line in ``data/logs/bot.log*``, the transcript, and git.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from common import LIVE_DATA, LIVE_ROOT, git

ACCOUNT_GLOB = ".claude*"


@dataclass
class Case:
    instance_id: str
    repo_path: str
    session_id: str | None
    account: str | None
    transcript: str | None
    prompt: str
    cut_uuid: str | None
    base_sha: str
    ref_tip: str
    merge_sha: str
    ref_tests: list[str] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    origin: str | None = None
    mode: str | None = None
    started: str | None = None
    topic: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Case":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


def history_record(instance_id: str, data_dir: Path = LIVE_DATA) -> dict | None:
    path = data_dir / "history.jsonl"
    found = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if f'"{instance_id}"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("id") == instance_id:
                    found = rec
    except OSError:
        return None
    return found


_RUN_RE = re.compile(r"Running (?P<id>[qt]-\d+) \[acct=(?P<acct>[^\]]*)\] \(prompt: (?P<chars>\d+) chars")


def run_line(instance_id: str, logs_dir: Path = LIVE_DATA / "logs") -> dict | None:
    """The first ``Running <id>`` line: the original attempt, not a recovery."""
    files = sorted(logs_dir.glob("bot.log*"), key=lambda p: p.stat().st_mtime)
    needle = f"Running {instance_id} "
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if needle not in line:
                        continue
                    m = _RUN_RE.search(line)
                    if not m or m.group("id") != instance_id:
                        continue
                    flag = lambda name: (re.search(rf"--{name} (\S+)", line) or [None, None])[1]  # noqa: E731
                    return {
                        "acct_hint": m.group("acct"),
                        "prompt_chars": int(m.group("chars")),
                        "model": flag("model"),
                        "effort": flag("effort"),
                        "resume": flag("resume"),
                    }
        except OSError:
            continue
    return None


def find_transcript(session_id: str, home: Path = Path.home(),
                    acct_hint: str | None = None) -> Path | None:
    """The session JSONL, preferring the account the run line names.

    The runner hydrates a session into whichever account it picks, so the
    same file can sit under several accounts. The log line shows the tail of
    the account the build actually ran on; that copy wins.
    """
    found: list[Path] = []
    for acct in sorted(home.glob(ACCOUNT_GLOB)):
        if not acct.is_dir():
            continue
        hits = list((acct / "projects").glob(f"*/{session_id}.jsonl"))
        if hits:
            best = max(hits, key=lambda p: p.stat().st_size)
            if acct_hint and str(acct).endswith(acct_hint.rstrip("/")):
                return best
            found.append(best)
    return found[0] if found else None


def _text_of(content) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts) if parts else None
    return None


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def find_prompt_row(transcript: Path, started: str | None, prompt_chars: int | None) -> dict | None:
    """The build's user message: first human-typed row at or after the run started."""
    t0 = _parse_ts(started)
    if t0:
        t0 = t0 - timedelta(seconds=60)
    fallback = None
    with open(transcript, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("type") != "user" or row.get("isMeta"):
                continue
            text = _text_of((row.get("message") or {}).get("content"))
            if not text:
                continue
            ts = _parse_ts(row.get("timestamp"))
            if t0 and ts and ts < t0:
                continue
            if prompt_chars is None or len(text) == prompt_chars:
                return {"row": row, "text": text}
            if fallback is None:
                fallback = {"row": row, "text": text}
    return fallback


_MERGE_RE_TMPL = r"^Merge (?:branch ')?claude-bot/{id}\b"


def find_merge(instance_id: str, repo: Path = LIVE_ROOT) -> str | None:
    out = git(repo, "log", "--merges", "--format=%H%x09%s", "--all", check=False)
    pat = re.compile(_MERGE_RE_TMPL.format(id=re.escape(instance_id)))
    for line in out.splitlines():
        sha, _, subj = line.partition("\t")
        if pat.search(subj):
            return sha
    return None


def reference_tests(repo: Path, base: str, tip: str) -> list[str]:
    out = git(repo, "diff", "--name-only", "--diff-filter=AM", f"{base}..{tip}", check=False)
    return sorted(
        p for p in out.splitlines()
        if re.match(r"scripts/(test|verify)_[^/]+\.py$", p)
    )


def extract(instance_id: str, *, data_dir: Path = LIVE_DATA, repo: Path = LIVE_ROOT,
            home: Path = Path.home()) -> Case:
    hist = history_record(instance_id, data_dir) or {}
    run = run_line(instance_id, data_dir / "logs") or {}
    sid = run.get("resume")

    transcript = find_transcript(sid, home, run.get("acct_hint")) if sid else None
    prompt_row = None
    if transcript:
        prompt_row = find_prompt_row(transcript, hist.get("started"), run.get("prompt_chars"))
    if not prompt_row:
        raise LookupError(
            f"{instance_id}: could not recover the build prompt "
            f"(session={sid}, transcript={transcript})"
        )

    merge = find_merge(instance_id, repo)
    if not merge:
        raise LookupError(f"{instance_id}: no merge commit for claude-bot/{instance_id}, nothing to score against")
    p1 = git(repo, "rev-parse", f"{merge}^1")
    tip = git(repo, "rev-parse", f"{merge}^2")
    base = git(repo, "merge-base", p1, tip)

    return Case(
        instance_id=instance_id,
        repo_path=str(repo),
        session_id=sid,
        account=str(transcript.parents[2]) if transcript else None,
        transcript=str(transcript) if transcript else None,
        prompt=prompt_row["text"],
        cut_uuid=prompt_row["row"].get("parentUuid") if sid else None,
        base_sha=base,
        ref_tip=tip,
        merge_sha=merge,
        ref_tests=reference_tests(repo, base, tip),
        model=run.get("model"),
        effort=run.get("effort"),
        origin=hist.get("origin"),
        mode=hist.get("mode"),
        started=hist.get("started"),
        topic=hist.get("topic"),
    )


if __name__ == "__main__":
    import sys
    for iid in sys.argv[1:]:
        c = extract(iid)
        d = c.to_dict()
        d["prompt"] = f"<{len(c.prompt)} chars> {c.prompt[:120]!r}"
        print(json.dumps(d, indent=1))
