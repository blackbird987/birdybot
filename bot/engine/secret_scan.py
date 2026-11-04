"""After a run, look for stored values in its transcript.

A session that prints a key has already sent it to Anthropic, so this cannot
prevent anything: it tells the owner which key to rotate. Names only.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections import OrderedDict
from pathlib import Path

from bot import vault
from bot.engine import sessions

log = logging.getLogger(__name__)

SCAN_CAP_BYTES = 64 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_OFFSETS_MAX = 2048

# Read position per file, so a long session is not re-read on every turn. Memory
# only: after a restart it re-reads, and ``vault.flag_once`` keeps it from re-posting.
_offsets: OrderedDict[str, int] = OrderedDict()
_background: set[asyncio.Task] = set()


def needles_for(value: str) -> list[bytes]:
    """How a value can look in a JSONL transcript."""
    raw = value.encode("utf-8", "replace")
    encoded = [base64.b64encode(raw), base64.urlsafe_b64encode(raw)]
    out = {raw, json.dumps(value)[1:-1].encode("utf-8")}
    for e in encoded:
        out.add(e)
        out.add(e.rstrip(b"="))
    return sorted(out, key=len, reverse=True)


def transcript_files(session_id: str) -> list[Path]:
    """The session's transcript plus its subagents', which sit in a sibling directory."""
    path = sessions.find_session_file(session_id)
    if path is None:
        return []
    files = [path]
    subdir = path.with_suffix("")
    if subdir.is_dir():
        files.extend(sorted(subdir.rglob("*.jsonl")))
    return files


def _scan_file(
    path: Path, needles: dict[str, list[bytes]], budget: int, overlap: int,
) -> tuple[set[str], int]:
    """Search one file from its saved offset. Returns the names hit and bytes read."""
    key = str(path)
    start = _offsets.get(key, 0)
    hits: set[str] = set()
    read = 0
    try:
        size = path.stat().st_size
        if size < start:
            start = 0
        with path.open("rb") as f:
            f.seek(max(0, start - overlap))
            tail = b""
            pos = start
            while read < budget:
                chunk = f.read(min(_CHUNK_BYTES, budget - read))
                if not chunk:
                    break
                read += len(chunk)
                window = tail + chunk
                for name, forms in needles.items():
                    if name not in hits and any(n in window for n in forms):
                        hits.add(name)
                tail = window[-overlap:] if overlap else b""
            pos = f.tell()
    except OSError as exc:
        log.debug("Transcript scan skipped a file: %s", type(exc).__name__)
        return hits, read
    _offsets[key] = pos
    _offsets.move_to_end(key)
    while len(_offsets) > _OFFSETS_MAX:
        _offsets.popitem(last=False)
    return hits, read


def scan_session(session_id: str) -> list[str]:
    """Names newly found in this session's transcripts, each reported once ever."""
    entries = vault.literal_entries()
    if not entries:
        return []
    needles = {name: needles_for(value) for name, value in entries}
    overlap = max(len(n) for forms in needles.values() for n in forms)
    found: set[str] = set()
    budget = SCAN_CAP_BYTES
    for path in transcript_files(session_id):
        if budget <= 0:
            break
        hits, read = _scan_file(path, needles, budget, overlap)
        found |= hits
        budget -= read
    return [name for name in sorted(found) if vault.flag_once(session_id, name)]


def flag_text(names: list[str]) -> str:
    listed = ", ".join(f"`{n}`" for n in names)
    verb = "appeared" if len(names) == 1 else "each appeared"
    return (
        f"{listed} {verb} in this session's transcript, so it reached Anthropic "
        "and sits on disk. Rotate it if that matters."
    )


async def _scan_and_post(session_id: str, channel_id: str | None, messenger) -> None:
    try:
        names = await asyncio.to_thread(scan_session, session_id)
        if not names:
            return
        text = flag_text(names)
        if channel_id is None:
            await messenger.broadcast(text)
        else:
            await messenger.send_text(channel_id, text)
    except Exception:
        log.warning("Transcript scan failed", exc_info=True)


def schedule(session_id: str | None, channel_id: str | None, messenger) -> None:
    """Scan in the background. The run has already finished, so nothing waits on it."""
    if not session_id or not vault.has_entries():
        return
    task = asyncio.create_task(_scan_and_post(session_id, channel_id, messenger))
    _background.add(task)
    task.add_done_callback(_background.discard)
