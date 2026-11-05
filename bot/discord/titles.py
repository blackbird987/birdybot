"""Smart title generation for Discord forum threads.

Spawns a lightweight Claude CLI subprocess to generate concise
4-6 word thread titles. Stateless — no Discord API calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path

from bot import config

log = logging.getLogger(__name__)

# On Windows, prevent subprocess console windows from popping up
_NOWND: dict = config.NOWND

# A limit answer that got used as a title, as saved ("You've hit your limit \u00b7
# resets 4pm") or as the thread's name, which has its punctuation stripped ("Youve
# hit your limit resets 4pm"). Only for finding old damage: a new answer is judged
# on the CLI's error flag, not on this.
_LIMIT_TITLE = re.compile(r"^You['\u2019]?ve hit your(?: \w+)? limit\W+resets\b", re.IGNORECASE)


def is_limit_message_title(topic: str | None) -> bool:
    """True when a title or thread name is really the CLI's usage limit message."""
    return bool(topic and _LIMIT_TITLE.match(topic))


def _encoded_temp_dir() -> str:
    """``tempfile.gettempdir()`` under the CLI's project-dir encoding.

    Delegates to the one shared encoder. Both call sites used to carry their
    own copy that replaced ':', '\\' and '/' but not '.', which is harmless
    while the temp root has no dot in it (``/tmp``, ``…\\AppData\\Local\\Temp``)
    and silently wrong the moment ``TMPDIR`` points somewhere that does — the
    title-gen JSONLs would then never be found, and every one of them would
    accumulate in the session picker.
    """
    from bot.claude.session_fork import encode_project_path
    return encode_project_path(tempfile.gettempdir())


def _is_temp_like_project_dir(proj_dir: Path) -> bool:
    """True if a project dir's encoded path resolves to a system temp location.

    Cheap pre-filter for the startup scan: avoids reading first user messages
    of every real session. Matches the OS temp root or trailing 'Temp'/'tmp'
    segments after decoding the dash-encoded path.
    """
    try:
        name = proj_dir.name
        # Full match against tempfile.gettempdir()'s encoding
        if name == _encoded_temp_dir():
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
    # Lazy import: titles.py is in the discord layer; engine.sessions doesn't
    # import titles.py, so this directional dep is fine. Lazy keeps module
    # load cheap.
    from bot.engine.sessions import _read_session_summary

    removed = 0
    try:
        # Sweep every account root, not just the primary — older builds ran
        # title-gen against $HOME/.claude, so leftovers can sit in either.
        for projects_dir in config.claude_projects_dirs():
            try:
                entries = list(projects_dir.iterdir())
            except OSError:
                # One unreadable root must not abandon the others.
                log.debug("Cannot list %s", projects_dir, exc_info=True)
                continue
            for proj_dir in entries:
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
                        log.debug(
                            "Failed to delete stale title-gen jsonl: %s",
                            jsonl, exc_info=True,
                        )
    except OSError:
        log.debug("Stale title-gen jsonl scan failed", exc_info=True)
    return removed


def read_ai_title(session_id: str) -> str | None:
    """Return the CLI's native session title from the session jsonl, or None.

    Claude Code writes ``{"type":"ai-title","aiTitle":"…"}`` records to each
    session's jsonl, refining the value as the conversation grows. We read the
    *last* one — it's clean, structured, and computed for free by the CLI,
    which is why this is preferred over spawning our own title-gen subprocess
    (that path occasionally emits Docker-style codename prefixes like
    "Glimmering Church …"). Best-effort: returns None on any miss so the
    caller can fall back to generate_title_text().
    """
    if not session_id:
        return None
    # Lazy import: engine.sessions is the file-locating layer; importing it at
    # module load would pull the engine into the discord layer eagerly.
    from bot.engine.sessions import find_session_file

    # Synchronous file I/O — callers on the event loop must dispatch this via
    # asyncio.to_thread so a large jsonl can't stall the loop.
    title: str | None = None
    try:
        fpath = find_session_file(session_id)
        if not fpath:
            return None
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or '"ai-title"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "ai-title":
                    val = (record.get("aiTitle") or "").strip()
                    if val:
                        title = val
    except Exception:
        # Best-effort: caller falls back to generate_title_text. Log at debug
        # so a real bug here (which would silently disable the ai-title path)
        # leaves a breadcrumb instead of vanishing.
        log.debug("read_ai_title failed for %s", session_id, exc_info=True)
        return None
    return title if title and len(title) >= 3 else None


async def generate_title_text(
    prompt: str, summary: str = "", *, account: str | None = None,
) -> str | None:
    """Spawn a lightweight Claude CLI call to generate a 4-6 word thread title.

    Bypasses the runner semaphore — this is a standalone, cheap subprocess.
    ``account`` is the config dir to run on; without one it is the primary.
    Returns the title string, or None on failure/timeout. An error answer is a
    failure: the CLI prints "You've hit your weekly limit" as its result text.
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
        # Nothing to clean up afterwards, in whichever account this runs on.
        "--no-session-persistence",
        # The prompt quotes a guest's words, and this runs outside any sandbox.
        # --tools does not reach MCP servers: the attack run saw them still offered.
        "--tools", "",
        "--strict-mcp-config",
    ]

    env = os.environ.copy()
    env.pop("CLAUDE_CODE", None)
    env.pop("CLAUDECODE", None)
    from bot.claude.runner import SESSION_STRIPPED_ENV_VARS
    for var in SESSION_STRIPPED_ENV_VARS:
        env.pop(var, None)
    # config loads .env into this process, so the paid key is in os.environ. Left
    # in, the CLI bills it and ignores the account below, as a normal session's
    # strip in the runner already knows.
    env.pop("ANTHROPIC_API_KEY", None)
    # Pin to an account, the chosen one or else the primary. Without this the
    # subprocess falls back to $HOME/.claude, which on Linux can be a different,
    # possibly unauthenticated, config dir, silently failing every title generation.
    pinned = account or config.primary_account_dir()
    if pinned is not None:
        env["CLAUDE_CONFIG_DIR"] = str(pinned)

    proc = None
    stdout = b""
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

    # Parse stream-json to extract result text
    events = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        parsed = parse_stream_line(line)
        if parsed:
            events.append(parsed)

    result = extract_result(events)
    # The flag and the exit code, never the wording. The subtype is no use: a limit
    # answer comes back as subtype "success" with is_error true and exit 1.
    if result.is_error or proc.returncode != 0:
        log.info(
            "Title generation got an error answer (exit %s): %s",
            proc.returncode, (result.result_text or result.error_message or "")[:120],
        )
        return None
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
