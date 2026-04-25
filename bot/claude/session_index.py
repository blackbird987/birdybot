"""Rebuild Claude CLI sessions-index.json from on-disk JSONL files.

Claude CLI 2.1.x requires sessions to be registered in
`~/.claude/projects/<project>/sessions-index.json` for `--resume <id>` to
succeed. The CLI sometimes stops updating this index, leaving JSONL files
orphaned — `--resume` returns "No conversation found" even though the file
exists. This module scans the JSONLs and rebuilds the index from ground
truth.

Concurrency:
- Per-project asyncio.Lock serializes rebuild within one process.
- Atomic write: sessions-index.json.new + os.replace().
- Active-project guard: skip projects with live instances (other than an
  optional caller exemption) to avoid racing with the CLI's own writes.
- Local post-write verification — never spawn `claude.exe --resume <id>` to
  probe (would permanently append "ok" turns to a real user session).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Per-project asyncio locks, lazily created.
_locks: dict[str, asyncio.Lock] = {}


def _get_lock(project_dir: str) -> asyncio.Lock:
    lock = _locks.get(project_dir)
    if lock is None:
        lock = asyncio.Lock()
        _locks[project_dir] = lock
    return lock


@dataclass(frozen=True)
class RebuildResult:
    project_dir: str
    status: str               # "rebuilt" | "skipped_active" | "skipped_no_jsonls"
                              # | "verification_failed" | "error"
    sessions_indexed: int = 0
    detail: str = ""


def claude_projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def cwd_to_project_dir_name(cwd: str) -> str:
    """Convert a working dir to Claude's project-dir naming convention.

    `C:\\Users\\foo\\bar`           → `C--Users-foo-bar`
    `C:\\foo\\.worktrees\\t-1`      → `C--foo--worktrees-t-1`
    """
    return (cwd
            .replace(":", "-")
            .replace("\\", "-")
            .replace("/", "-")
            .replace(".", "-"))


def extract_session_metadata(jsonl_path: Path) -> dict | None:
    """Parse a session JSONL and extract fields needed for an index entry.

    Skips non-user records (queue-operation, summary, system, tool_result)
    when looking for `firstPrompt` — JSONLs commonly open with queue-op
    events before any real user turn. Returns None if the file is unreadable
    or contains no usable session id.
    """
    session_id: str | None = None
    first_prompt = ""
    last_summary = ""
    message_count = 0
    git_branch = ""
    created = ""
    is_sidechain = False
    first_user_found = False
    # Fallback: queue-operation enqueue often carries the original prompt
    queue_first = ""

    try:
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate truncated last line / corruption
                if not isinstance(rec, dict):
                    continue

                if not session_id:
                    sid = rec.get("sessionId") or rec.get("session_id")
                    if sid:
                        session_id = sid

                if not created:
                    ts = rec.get("timestamp")
                    if ts:
                        created = ts

                if rec.get("isSidechain"):
                    is_sidechain = True

                rtype = rec.get("type")

                if rtype == "queue-operation" and rec.get("operation") == "enqueue" and not queue_first:
                    c = rec.get("content")
                    if isinstance(c, str) and c.strip():
                        queue_first = c.strip()[:500]

                if rtype == "summary":
                    s = rec.get("summary") or rec.get("content") or ""
                    if isinstance(s, str) and s.strip():
                        last_summary = s.strip()[:200]

                if rtype == "user" and not first_user_found:
                    msg = rec.get("message") or {}
                    content = msg.get("content")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text = block.get("text", "")
                                break
                    if text.strip():
                        first_prompt = text.strip()[:500]
                        first_user_found = True

                if rtype in ("user", "assistant"):
                    message_count += 1

                gb = rec.get("gitBranch") or rec.get("git_branch")
                if gb and not git_branch:
                    git_branch = gb

    except (OSError, PermissionError) as exc:
        log.debug("Could not read %s: %s", jsonl_path, exc)
        return None

    if not session_id:
        return None

    return {
        "sessionId": session_id,
        "firstPrompt": first_prompt or queue_first,
        "summary": last_summary,
        "messageCount": message_count,
        "gitBranch": git_branch,
        "created": created,
        "isSidechain": is_sidechain,
    }


def _project_dir_to_original_path(dir_name: str) -> str:
    """Best-effort reverse of `C--Users-Quincy-...` → `C:\\Users\\Quincy\\...`.

    Cannot recover dots inside path components (e.g. `.worktrees`) — Claude
    CLI seems to tolerate this as long as `fullPath` on each entry is right.
    """
    if len(dir_name) >= 3 and dir_name[1:3] == "--" and dir_name[0].isalpha():
        return f"{dir_name[0]}:\\" + dir_name[3:].replace("-", "\\")
    return dir_name.replace("-", "\\")


def _existing_original_path(project_dir: Path) -> str | None:
    idx = project_dir / "sessions-index.json"
    if not idx.exists():
        return None
    try:
        with open(idx, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("originalPath") or None
    except (OSError, json.JSONDecodeError):
        return None


def _verify_index_locally(index_path: Path, sample_entry: dict | None) -> tuple[bool, str]:
    try:
        with open(index_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"reload failed: {exc}"
    if not isinstance(data, dict):
        return False, "top-level not an object"
    for key in ("version", "entries", "originalPath"):
        if key not in data:
            return False, f"missing top-level key: {key}"
    if not isinstance(data["entries"], list):
        return False, "entries is not a list"
    if sample_entry is None:
        return True, ""
    full_path = sample_entry.get("fullPath")
    if not full_path or not Path(full_path).exists():
        return False, f"sample fullPath does not exist: {full_path}"
    meta = extract_session_metadata(Path(full_path))
    if meta is None:
        return False, "sample JSONL could not be re-parsed"
    if meta.get("sessionId") != sample_entry.get("sessionId"):
        return False, "sample sessionId mismatch"
    return True, ""


def _collect_active_session_ids(state, exempt_instance_id: str | None) -> set[str]:
    """Session IDs of instances currently RUNNING/QUEUED/STALLED, minus exempt."""
    if state is None:
        return set()
    try:
        from bot.claude.types import InstanceStatus
    except Exception:
        return set()
    active_states = {InstanceStatus.RUNNING, InstanceStatus.QUEUED, InstanceStatus.STALLED}
    out: set[str] = set()
    try:
        for inst in state.list_instances(all_=True):
            if inst.id == exempt_instance_id:
                continue
            if inst.status in active_states and inst.session_id:
                out.add(inst.session_id)
    except Exception:
        log.exception("Could not list active instances")
    return out


async def rebuild_project_index(
    project_dir: Path,
    *,
    state=None,
    exempt_instance_id: str | None = None,
    backup: bool = False,
) -> RebuildResult:
    """Rebuild sessions-index.json for a single Claude project directory."""
    pdir = Path(project_dir)
    lock = _get_lock(str(pdir))
    async with lock:
        return await asyncio.to_thread(
            _rebuild_project_index_sync,
            pdir, state, exempt_instance_id, backup,
        )


def _rebuild_project_index_sync(
    project_dir: Path,
    state,
    exempt_instance_id: str | None,
    backup: bool,
) -> RebuildResult:
    if not project_dir.is_dir():
        return RebuildResult(str(project_dir), "error", detail="not a directory")

    jsonls = sorted(project_dir.glob("*.jsonl"))
    if not jsonls:
        return RebuildResult(str(project_dir), "skipped_no_jsonls")

    active_sids = _collect_active_session_ids(state, exempt_instance_id)
    if active_sids:
        for jp in jsonls:
            if jp.stem in active_sids:
                return RebuildResult(
                    str(project_dir), "skipped_active",
                    detail=f"active session {jp.stem}",
                )

    index_path = project_dir / "sessions-index.json"
    original_path = (
        _existing_original_path(project_dir)
        or _project_dir_to_original_path(project_dir.name)
    )

    entries: list[dict] = []
    for jp in jsonls:
        meta = extract_session_metadata(jp)
        if meta is None:
            continue
        try:
            stat = jp.stat()
            mtime_ms = int(stat.st_mtime * 1000)
            # Match Claude CLI's exact format: ISO with milliseconds + "Z"
            dt = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            modified_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
        except OSError:
            mtime_ms = 0
            modified_iso = ""
        entries.append({
            "sessionId": meta["sessionId"],
            "fullPath": str(jp),
            "fileMtime": mtime_ms,
            "firstPrompt": meta["firstPrompt"],
            "summary": meta["summary"],
            "messageCount": meta["messageCount"],
            "created": meta["created"] or modified_iso,
            "modified": modified_iso,
            "gitBranch": meta["gitBranch"],
            "projectPath": original_path,
            "isSidechain": meta["isSidechain"],
        })

    if not entries:
        return RebuildResult(str(project_dir), "skipped_no_jsonls", detail="no parseable JSONLs")

    new_data = {"version": 1, "entries": entries, "originalPath": original_path}

    if backup and index_path.exists():
        bak = index_path.with_name(f"sessions-index.json.bak-{int(time.time())}")
        try:
            shutil.copy2(index_path, bak)
        except OSError:
            log.warning("Backup failed for %s", index_path)

    tmp_path = index_path.with_suffix(index_path.suffix + ".new")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(new_data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        return RebuildResult(str(project_dir), "error", detail=f"write failed: {exc}")

    ok, why = _verify_index_locally(tmp_path, entries[0])
    if not ok:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        log.error("Rebuild verification failed for %s: %s", project_dir, why)
        return RebuildResult(str(project_dir), "verification_failed", detail=why)

    try:
        os.replace(tmp_path, index_path)
    except OSError as exc:
        return RebuildResult(str(project_dir), "error", detail=f"rename failed: {exc}")

    return RebuildResult(str(project_dir), "rebuilt", sessions_indexed=len(entries))


async def rebuild_all(
    *,
    state=None,
    exempt_instance_id: str | None = None,
    backup: bool = False,
) -> dict:
    """Rebuild every project's sessions-index.json under ~/.claude/projects.

    Per-project failures are isolated and logged; other projects continue.
    """
    base = claude_projects_dir()
    if not base.is_dir():
        log.info("No %s — nothing to rebuild", base)
        return {"projects": 0, "sessions": 0, "failed": 0, "skipped_active": 0, "no_jsonls": 0}

    counts = {"projects": 0, "sessions": 0, "failed": 0, "skipped_active": 0, "no_jsonls": 0}
    for pdir in sorted(base.iterdir()):
        if not pdir.is_dir():
            continue
        try:
            result = await rebuild_project_index(
                pdir, state=state,
                exempt_instance_id=exempt_instance_id,
                backup=backup,
            )
        except Exception:
            log.exception("rebuild_project_index crashed for %s", pdir)
            counts["failed"] += 1
            continue

        if result.status == "rebuilt":
            counts["projects"] += 1
            counts["sessions"] += result.sessions_indexed
        elif result.status == "skipped_active":
            counts["skipped_active"] += 1
        elif result.status == "skipped_no_jsonls":
            counts["no_jsonls"] += 1
        else:  # verification_failed | error
            counts["failed"] += 1
            log.warning("Rebuild %s for %s: %s", result.status, pdir, result.detail)

    log.info(
        "Sessions-index rebuild: %d projects, %d sessions, %d failed, %d skipped (active), %d empty",
        counts["projects"], counts["sessions"], counts["failed"],
        counts["skipped_active"], counts["no_jsonls"],
    )
    return counts
