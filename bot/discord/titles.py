"""Smart title generation for Discord forum threads.

Spawns a lightweight Claude CLI subprocess to generate concise
4-6 word thread titles. Stateless — no Discord API calls.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

from bot import config

log = logging.getLogger(__name__)

# On Windows, prevent subprocess console windows from popping up
_NOWND: dict = config.NOWND


def _temp_project_dir() -> Path | None:
    """Path to the CLI projects subdir corresponding to tempfile.gettempdir().

    The CLI encodes cwd by replacing ':', '\\', '/' with '-'. We mirror that
    so we can locate any .jsonl files the title-gen subprocess wrote.
    """
    try:
        tmp = tempfile.gettempdir()
        encoded = tmp.replace(":", "-").replace("\\", "-").replace("/", "-")
        return config.CLAUDE_PROJECTS_DIR / encoded
    except Exception:
        return None


def _is_temp_like_project_dir(proj_dir: Path) -> bool:
    """True if a project dir's encoded path resolves to a system temp location.

    Cheap pre-filter for the startup scan: avoids reading first user messages
    of every real session. Matches the OS temp root or trailing 'Temp'/'tmp'
    segments after decoding the dash-encoded path.
    """
    try:
        name = proj_dir.name
        # Full match against tempfile.gettempdir()'s encoding
        tmp_encoded = tempfile.gettempdir().replace(":", "-").replace("\\", "-").replace("/", "-")
        if name == tmp_encoded:
            return True
        # Last segment heuristic for stale dirs left by other temp roots
        decoded = name.replace("-", "/")
        last = decoded.rstrip("/").rsplit("/", 1)[-1].lower()
        return last in ("temp", "tmp")
    except Exception:
        return False


def cleanup_stale_temp_jsonls() -> int:
    """Delete title-gen jsonls left in temp-like project dirs from prior runs.

    Two-condition safety gate before deletion: the project dir must look like
    a system temp location AND the jsonl's first user message must start with
    config.TITLE_PROMPT_MARKER. Both signals required so a real user session
    whose prompt happens to start with that string is never deleted.

    Returns the count of files removed. Best-effort — never raises.
    """
    projects_dir = config.CLAUDE_PROJECTS_DIR
    if not projects_dir.is_dir():
        return 0

    # Lazy import: titles.py is in the discord layer; engine.sessions doesn't
    # import titles.py, so this directional dep is fine. Lazy keeps module
    # load cheap.
    from bot.engine.sessions import _read_session_summary

    removed = 0
    try:
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir() or not _is_temp_like_project_dir(proj_dir):
                continue
            for jsonl in proj_dir.glob("*.jsonl"):
                summary = _read_session_summary(jsonl)
                if not summary or not summary.get("first_user"):
                    continue
                first = summary["first_user"].get("text", "")
                if not first.startswith(config.TITLE_PROMPT_MARKER):
                    continue
                try:
                    jsonl.unlink()
                    removed += 1
                except OSError:
                    log.debug("Failed to delete stale title-gen jsonl: %s", jsonl, exc_info=True)
    except OSError:
        log.debug("Stale title-gen jsonl scan failed", exc_info=True)
    return removed


async def generate_title_text(prompt: str, summary: str = "") -> str | None:
    """Spawn a lightweight Claude CLI call to generate a 4-6 word thread title.

    Bypasses the runner semaphore — this is a standalone, cheap subprocess.
    Returns the title string, or None on failure/timeout.
    """
    from bot.claude.parser import extract_result, parse_stream_line

    title_prompt = (
        f"{config.TITLE_PROMPT_MARKER}. "
        "Maximum 6 words. No articles or filler words like 'the', 'a', 'for'. "
        "Output ONLY the title — no quotes, no explanation.\n\n"
        f"User asked: {prompt[:300]}\n"
    )
    if summary:
        title_prompt += f"\nResult: {summary[:500]}"

    cmd = [
        config.CLAUDE_BINARY, "-p", title_prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "plan",
        "--max-turns", "1",
    ]

    env = os.environ.copy()
    env.pop("CLAUDE_CODE", None)
    env.pop("CLAUDECODE", None)

    # Snapshot pre-existing jsonls in the Temp project dir so we can delete
    # whatever this subprocess writes — the CLI persists every -p call as a
    # session jsonl, which would otherwise pollute /session lists. The dir
    # itself may not exist yet on a first-ever call; the CLI creates it.
    proj_dir = _temp_project_dir()
    pre_existing: set[str] = set()
    if proj_dir and proj_dir.is_dir():
        try:
            pre_existing = {p.name for p in proj_dir.glob("*.jsonl")}
        except OSError:
            pass

    proc = None
    stdout = b""
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                cwd=tempfile.gettempdir(),
                **_NOWND,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=config.TITLE_TIMEOUT_SECS,
            )
        except asyncio.TimeoutError:
            log.debug("Title generation timed out")
            if proc:
                try:
                    proc.kill()
                    await proc.wait()
                except (ProcessLookupError, OSError):
                    pass
            return None
        except Exception:
            log.warning("Title generation CLI call failed", exc_info=True)
            if proc:
                try:
                    proc.kill()
                    await proc.wait()
                except (ProcessLookupError, OSError):
                    pass
            return None
    finally:
        # Delete any new jsonls the title-gen subprocess wrote. Re-check
        # existence here (not the snapshot-time bool) — on a first-ever call
        # the dir is created by the subprocess, so it can flip from missing
        # to present between snapshot and cleanup. Safety gate below in
        # scan_sessions catches anything that slips through.
        if proj_dir and proj_dir.is_dir():
            try:
                for p in proj_dir.glob("*.jsonl"):
                    if p.name not in pre_existing:
                        try:
                            p.unlink()
                        except OSError:
                            pass
            except OSError:
                log.debug("Title-gen jsonl cleanup scan failed", exc_info=True)

    # Parse stream-json to extract result text
    events = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        parsed = parse_stream_line(line)
        if parsed:
            events.append(parsed)

    result = extract_result(events)
    if not result.result_text:
        return None

    # Take first line only (LLM might add explanation on subsequent lines)
    title = result.result_text.strip().split("\n")[0].strip()
    # Strip markdown formatting: leading # (headers), inline *_`
    title = re.sub(r'^#+\s*', '', title)
    title = re.sub(r'[*_`]', '', title)
    title = title.strip('"\'').strip()
    title = re.sub(r'[.!?:]+$', '', title).strip()
    return title if len(title) >= 3 else None
