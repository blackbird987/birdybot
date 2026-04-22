"""Fork a Claude CLI session JSONL truncated to a chosen assistant message.

The CLI's ``--resume`` replays from the start; it has no native truncation.
We work around this by snapshot-copying the source JSONL, walking the
``parentUuid`` chain backward from the chosen ``uuid`` to keep only ancestor
records on the conversation chain, rewriting the ``sessionId`` on every
kept line, and writing the result into the destination project directory
under a fresh session id.

Records that have no ``uuid`` (CLI bookkeeping like ``queue-operation``,
``last-prompt``, ``ai-title``, ``system``) are preserved up to the position
of the target — these belong to the CLI's own state machine and are kept
defensively so ``--resume`` sees the same shape it would in a non-forked file.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import uuid as uuid_lib
from pathlib import Path

log = logging.getLogger(__name__)


def encode_project_path(path: str) -> str:
    """Encode a filesystem path the same way the Claude CLI does for project dirs.

    Mirrors ``ClaudeRunner._encode_project_path``: replaces ``\\``, ``/``, ``:``,
    and ``.`` with ``-``.
    """
    p = path.replace("\\", "/").rstrip("/")
    return p.replace("/", "-").replace(":", "-").replace(".", "-")


def get_last_assistant_uuid(jsonl_path: Path) -> str | None:
    """Return the uuid of the most recent assistant record in the JSONL, or None."""
    if not jsonl_path.exists():
        return None
    last_uuid: str | None = None
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "assistant" and rec.get("uuid"):
                    last_uuid = rec["uuid"]
    except OSError:
        return None
    return last_uuid


def fork_session(
    source_path: Path,
    target_uuid: str,
    dest_project_dir: Path,
) -> tuple[str, Path] | None:
    """Fork ``source_path`` truncated through ``target_uuid`` (inclusive).

    Walks the ``parentUuid`` chain backward from ``target_uuid`` to determine
    which uuid records belong to the kept conversation.  In the output:
      - records with a uuid in the parent chain are kept,
      - records with no uuid (bookkeeping) are kept up to and including the
        original line position of the target,
      - records with a uuid NOT in the parent chain (siblings of pruned
        branches) are dropped.

    Every kept record has its ``sessionId`` rewritten to a fresh UUID.

    Returns ``(new_session_id, new_jsonl_path)`` on success, ``None`` on failure.
    """
    if not source_path.exists():
        log.warning("fork_session: source not found: %s", source_path)
        return None

    # Snapshot first — the source may still be actively streamed by a live run.
    fd, tmp_name = tempfile.mkstemp(suffix=".jsonl", prefix="fork_src_")
    os.close(fd)
    snapshot_path = Path(tmp_name)
    try:
        try:
            shutil.copyfile(source_path, snapshot_path)
        except OSError as e:
            log.warning("fork_session: snapshot copy failed: %s", e)
            return None

        try:
            with open(snapshot_path, "r", encoding="utf-8", errors="replace") as f:
                raw_lines = f.readlines()
        except OSError as e:
            log.warning("fork_session: cannot read snapshot: %s", e)
            return None

        # Parse all records preserving original order.
        # records[i] = (rec_dict, uuid_or_None) — None for bookkeeping records.
        records: list[tuple[dict, str | None]] = []
        records_by_uuid: dict[str, dict] = {}
        target_idx: int | None = None
        total = len(raw_lines)

        for i, raw in enumerate(raw_lines):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except json.JSONDecodeError:
                # Tolerate ONLY a trailing partial line (active-write race).
                if i == total - 1:
                    log.info("fork_session: dropping trailing partial line")
                    continue
                log.warning(
                    "fork_session: JSON parse error at line %d/%d, aborting",
                    i + 1, total,
                )
                return None
            u = rec.get("uuid") if isinstance(rec.get("uuid"), str) else None
            records.append((rec, u))
            if u:
                records_by_uuid[u] = rec
                if u == target_uuid:
                    target_idx = len(records) - 1

        if target_idx is None:
            log.warning(
                "fork_session: target uuid %s not in source (%d uuid records)",
                target_uuid[:8], len(records_by_uuid),
            )
            return None

        # Walk parentUuid chain backward to determine kept uuids.
        keep: set[str] = set()
        cursor: str | None = target_uuid
        guard = 0
        while cursor and cursor in records_by_uuid:
            if cursor in keep:
                break  # cycle protection
            keep.add(cursor)
            cursor = records_by_uuid[cursor].get("parentUuid")
            guard += 1
            if guard > 100_000:
                log.warning("fork_session: parentUuid walk exceeded guard limit")
                return None

        new_session_id = str(uuid_lib.uuid4())
        dest_project_dir.mkdir(parents=True, exist_ok=True)
        new_path = dest_project_dir / f"{new_session_id}.jsonl"

        wrote = 0
        try:
            with open(new_path, "w", encoding="utf-8") as out:
                for i, (rec, u) in enumerate(records):
                    if u is None:
                        # Bookkeeping record — keep only if positioned at or
                        # before the target (drop entries that describe later
                        # turns we are truncating away).
                        if i > target_idx:
                            continue
                    else:
                        if u not in keep:
                            continue  # sibling of a pruned branch
                    if "sessionId" in rec:
                        rec["sessionId"] = new_session_id
                    out.write(json.dumps(rec) + "\n")
                    wrote += 1
        except OSError as e:
            log.warning("fork_session: write failed: %s", e)
            try:
                new_path.unlink()
            except OSError:
                pass
            return None

        if wrote == 0:
            log.warning("fork_session: no records written, removing empty file")
            try:
                new_path.unlink()
            except OSError:
                pass
            return None

        log.info(
            "fork_session: wrote %d/%d records to %s (session %s)",
            wrote, len(records), new_path.name, new_session_id[:8],
        )
        return new_session_id, new_path

    finally:
        try:
            snapshot_path.unlink()
        except OSError:
            pass
