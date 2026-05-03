"""Session scanning, file lookup, and message extraction (pure — no platform deps)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from bot import config
from bot.platform.formatting import format_age, strip_markdown


def _parse_record(line: str) -> dict | None:
    """Parse a single JSONL line into a message dict, or None if not relevant."""
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    rtype = record.get("type")
    if rtype not in ("user", "assistant"):
        return None
    msg = record.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        content = " ".join(parts)
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text:
        return None
    if "<command-name>" in text or "<local-command-" in text:
        return None
    if text.startswith("[Request interrupted"):
        return None
    return {
        "role": rtype,
        "text": text,
        "branch": record.get("gitBranch", ""),
    }


def read_session_messages(fpath: Path, last_n: int = 4) -> list[dict]:
    """Read user+assistant messages from a session JSONL. Returns last N exchanges."""
    messages = []
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parsed = _parse_record(line)
                if parsed:
                    messages.append(parsed)
    except Exception:
        pass
    return messages[-last_n:] if messages else []


def _read_session_summary(fpath: Path) -> dict | None:
    """Fast scan: read first user message + last few lines for topic/branch.

    Avoids reading the entire file for large sessions.
    """
    first_user: dict | None = None
    last_msgs: list[dict] = []
    branch = ""

    try:
        size = fpath.stat().st_size
    except OSError:
        return None

    # For small files (< 512KB), just read the whole thing
    if size < 512 * 1024:
        messages = read_session_messages(fpath, last_n=999)
        if not messages:
            return None
        for m in messages:
            if m["role"] == "user" and not first_user:
                first_user = m
                break
        for m in reversed(messages):
            if m.get("branch"):
                branch = m["branch"]
                break
        return {
            "first_user": first_user,
            "last_msgs": messages[-2:],
            "branch": branch,
        }

    # Large file: read first 64KB for first user msg, tail last 128KB for recent msgs
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            # Read head
            head = f.read(64 * 1024)
            for line in head.split("\n"):
                parsed = _parse_record(line)
                if parsed and parsed["role"] == "user":
                    first_user = parsed
                    break

            # Seek to tail
            f.seek(max(0, size - 128 * 1024))
            f.readline()  # skip partial line
            tail_msgs = []
            for line in f:
                parsed = _parse_record(line)
                if parsed:
                    tail_msgs.append(parsed)

            for m in reversed(tail_msgs):
                if m.get("branch"):
                    branch = m["branch"]
                    break

            last_msgs = tail_msgs[-2:] if tail_msgs else []
    except Exception:
        return None

    if not last_msgs and not first_user:
        return None

    return {
        "first_user": first_user,
        "last_msgs": last_msgs,
        "branch": branch,
    }


def find_session_file(session_id: str) -> Path | None:
    """Find a session JSONL file by full or partial ID."""
    projects_dir = config.CLAUDE_PROJECTS_DIR
    if not projects_dir.is_dir():
        return None
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        exact = proj_dir / f"{session_id}.jsonl"
        if exact.exists():
            return exact
        if len(session_id) < 36:
            for f in proj_dir.glob(f"{session_id}*.jsonl"):
                return f
    return None


def _encode_path(path: str) -> str:
    """Encode a filesystem path the same way Claude Code encodes project dir names.

    Normalizes slashes and strips trailing separator before encoding.
    """
    path = path.replace("\\", "/").rstrip("/")
    return path.replace("/", "-").replace(":", "-")


def find_latest_session_for_repo(repo_path: str) -> dict | None:
    """Find the most recent CLI session matching a repo path.

    Returns {"id": session_id, "mtime": float} or None.
    Lightweight: only checks file mtime, no JSONL parsing.
    Matches by encoding the repo_path and comparing to project dir names.
    """
    projects_dir = config.CLAUDE_PROJECTS_DIR
    if not projects_dir.is_dir():
        return None

    encoded = _encode_path(repo_path)

    best: tuple[float, str] | None = None  # (mtime, session_id)

    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        if proj_dir.name != encoded:
            continue
        for f in proj_dir.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, f.stem)

    if best is None:
        return None
    return {"id": best[1], "mtime": best[0]}


def scan_sessions(
    limit: int = 10,
    repos: dict[str, str] | None = None,
) -> list[dict]:
    """Scan ~/.claude/projects/ for recent sessions.

    Args:
        limit: Max sessions to return.
        repos: Optional {name: path} dict for accurate project name display.
    """
    projects_dir = config.CLAUDE_PROJECTS_DIR
    if not projects_dir.is_dir():
        return []

    # Build reverse lookup: encoded_path -> repo_name for display
    _encoded_to_name: dict[str, str] = {}
    if repos:
        for name, path in repos.items():
            encoded = _encode_path(path)
            _encoded_to_name[encoded] = name

    candidates = []
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.glob("*.jsonl"):
            candidates.append((f.stat().st_mtime, f, proj_dir.name))

    candidates.sort(key=lambda x: x[0], reverse=True)

    sessions = []
    seen_ids = set()
    now = datetime.now(timezone.utc)

    for mtime, fpath, proj_encoded in candidates[:limit * 3]:
        if len(sessions) >= limit:
            break

        session_id = fpath.stem
        if session_id in seen_ids:
            continue
        seen_ids.add(session_id)

        # Use repo name from store if available, otherwise fall back to last dir segment
        project_name = _encoded_to_name.get(proj_encoded)
        if not project_name:
            # Fallback: last segment after replacing - with / (lossy for hyphenated names)
            decoded = proj_encoded.replace("-", "/")
            segments = [s for s in decoded.split("/") if s]
            project_name = segments[-1] if segments else proj_encoded

        summary = _read_session_summary(fpath)
        if not summary:
            continue

        first_user_msg = summary["first_user"]["text"] if summary["first_user"] else ""
        branch = summary["branch"]
        last_msgs = [m["text"] for m in summary["last_msgs"]]

        if not last_msgs:
            continue

        # Skip title-gen subprocess jsonls — they pollute the picker with
        # "[Temp] Generate a 4-6 word title for…" entries. Backstop in case
        # titles.py's per-call cleanup missed one.
        if first_user_msg.startswith(config.TITLE_PROMPT_MARKER):
            continue

        mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
        age = format_age(now - mtime_dt)

        topic = first_user_msg or last_msgs[0]
        for prefix in (
            config.PLAN_PROMPT_PREFIX,
            config.BUILD_FROM_PLAN_PROMPT,
            config.BUILD_FROM_QUERY_PROMPT,
            config.PLAN_REVIEW_PROMPT,
            config.APPLY_REVISIONS_PROMPT,
            config.CODE_REVIEW_PROMPT,
            config.COMMIT_PROMPT,
            "Implement the following plan:",
            "You have full build permissions.",
        ):
            topic = topic.replace(prefix, "")
        topic = strip_markdown(topic)
        topic = re.sub(r'^Plan:\s*', '', topic).strip()
        if len(topic) < 5:
            topic = strip_markdown(last_msgs[-1])

        sessions.append({
            "id": session_id,
            "project": project_name,
            "branch": branch,
            "last_msgs": last_msgs,
            "topic": topic,
            "age": age,
        })

    return sessions
