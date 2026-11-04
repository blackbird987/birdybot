"""Which commits a session made, from its own `git commit` tool calls.

Master is shared by parallel sessions, so a head-to-head range would take other
people's work. The call itself is the evidence, but mostly not through a hash:
of 7,042 commit calls measured on 2026-11-04, 95% ran `git commit -q`, which prints
nothing. So a call is pinned, in this order, by the hash git printed, by a line the
same command printed afterwards (`git log --oneline -1`) naming a real commit, or by
the exact subject it passed. Those last two have to fall within a few hours of the
call, in the repo's own log. Anything that does not come to exactly one commit stays
unattributed, and so does one call running `git commit` twice. Nothing here guesses.

Standard library only, so the replay can import it without loading the bot's config.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# The lookahead leaves out `git commit-graph` and `git commit-tree`.
_COMMIT_HEAD = r"(?:^|[;&|\n(]\s*)(?:\w+=\S+\s+)*git(?:\s+-[cC]\s+\S+)*\s+commit(?![\w-])"
_COMMIT_HEAD_RE = re.compile(_COMMIT_HEAD)
_GIT_COMMIT_RE = re.compile(_COMMIT_HEAD + r"(?P<rest>.*)", re.DOTALL)
_OUTPUT_HASH_RE = re.compile(r"^\[[^\]\n]*?\b([0-9a-f]{7,40})\]", re.MULTILINE)
_ONELINE_RE = re.compile(r"^([0-9a-f]{7,40}) (.+)$", re.MULTILINE)
_NOT_A_COMMIT_RE = re.compile(
    r"nothing to commit|no changes added to commit|nothing added to commit|^(?:error|fatal):",
    re.MULTILINE,
)
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?\w+['\"]?[^\n]*\n[ \t]*([^\n]*)")
_MESSAGE_RE = re.compile(r"""(?:^|\s)-[a-zA-Z]*m\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)')""", re.DOTALL)
_UNESCAPE_RE = re.compile(r"""\\(["\\$`])""")

# A commit lands within seconds of its call. The slack covers a slow hook and clock
# rounding, and an exact subject that recurs inside it is reported as ambiguous.
_SUBJECT_WINDOW_S = 2 * 3600


@dataclass(frozen=True)
class CommitCall:
    tool_id: str
    ts: float | None
    hash: str | None
    subject: str | None
    failed: bool
    printed: tuple[tuple[str, str], ...] = ()


def commit_subject(command: str) -> str | None:
    """The first line of the message this command passes, if it passes one."""
    m = _GIT_COMMIT_RE.search(command)
    if not m:
        return None
    rest = m.group("rest")
    msg = _MESSAGE_RE.search(rest)
    if msg:
        text = msg.group(1)
        if text is not None:
            text = _UNESCAPE_RE.sub(r"\1", text)
        else:
            text = msg.group(2)
        if not text.lstrip().startswith("$("):
            return text.split("\n", 1)[0].strip() or None
    here = _HEREDOC_RE.search(rest)
    if here:
        return here.group(1).strip() or None
    return None


def _epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _result_text(content) -> str:
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content if isinstance(content, str) else ""


def read_commit_calls(session_file: Path) -> list[CommitCall]:
    """Every `git commit` call in a session, oldest first. Streams: these can be 600 MB."""
    pending: dict[str, tuple[str, float | None]] = {}
    calls: dict[str, CommitCall] = {}
    with open(session_file, encoding="utf-8", errors="replace") as f:
        for line in f:
            # Cheap filter, and it must let results through: the one a quiet commit
            # leaves does not contain the word.
            if "git" not in line and '"tool_result"' not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            # Other row types repeat the same blocks.
            if not isinstance(row, dict) or row.get("type") not in ("assistant", "user"):
                continue
            content = (row.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use" and part.get("name") == "Bash":
                    command = (part.get("input") or {}).get("command") or ""
                    tool_id = part.get("id") or ""
                    if "--dry-run" in command or not _GIT_COMMIT_RE.search(command):
                        continue
                    ts = _epoch(row.get("timestamp"))
                    # One hash or subject cannot speak for two commits, so such a call
                    # never reads its result and stays unattributed.
                    if len(_COMMIT_HEAD_RE.findall(command)) > 1:
                        calls[tool_id] = CommitCall(tool_id, ts, None, None, False)
                        continue
                    pending[tool_id] = (command, ts)
                    # No result yet is a call whose outcome is unknown, not a failure.
                    calls[tool_id] = CommitCall(tool_id, ts, None, commit_subject(command), False)
                elif part.get("type") == "tool_result" and part.get("tool_use_id") in pending:
                    tool_id = part["tool_use_id"]
                    command, ts = pending.pop(tool_id)
                    output = _result_text(part.get("content"))
                    found = _OUTPUT_HASH_RE.search(output)
                    calls[tool_id] = CommitCall(
                        tool_id, ts, found.group(1) if found else None, commit_subject(command),
                        bool(part.get("is_error")) or bool(_NOT_A_COMMIT_RE.search(output)),
                        tuple(_ONELINE_RE.findall(output)),
                    )
    return list(calls.values())


def match_commits(
    calls: list[CommitCall], log: list[tuple[str, str, float]],
) -> tuple[list[str], list[str]]:
    """Full hashes for the calls, plus the tool ids that could not be pinned to one commit.

    log rows are (full hash, subject, commit time) from the repo. A call that failed
    and matches nothing made no commit, so it is neither.
    """
    shas: list[str] = []
    unattributed: list[str] = []
    for call in calls:
        near = [
            row for row in log
            if call.ts is None or abs(row[2] - call.ts) <= _SUBJECT_WINDOW_S
        ]
        hits: list[str] = []
        if call.hash:
            hits = [sha for sha, _, _ in log if sha.startswith(call.hash)]
        else:
            # The newest line comes first, and later lines of `git log -5` are older work.
            for short, text in call.printed:
                hits = [sha for sha, subject, _ in near if sha.startswith(short) and subject == text]
                if hits:
                    break
            if not hits and call.subject:
                hits = [sha for sha, subject, _ in near if subject == call.subject]
        if len(hits) == 1:
            if hits[0] not in shas:
                shas.append(hits[0])
        elif hits or not call.failed:
            unattributed.append(call.tool_id)
    return shas, unattributed


# Rows for parse_log: git log --all with this format.
LOG_FORMAT = "--format=%H%x00%s%x00%ct"


def parse_log(text: str) -> list[tuple[str, str, float]]:
    rows = []
    for line in text.splitlines():
        parts = line.split("\x00")
        if len(parts) == 3 and parts[2].isdigit():
            rows.append((parts[0], parts[1], float(parts[2])))
    return rows
