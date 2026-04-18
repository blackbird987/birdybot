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

from bot import config

log = logging.getLogger(__name__)

# On Windows, prevent subprocess console windows from popping up
_NOWND: dict = config.NOWND


async def generate_title_text(prompt: str, summary: str = "") -> str | None:
    """Spawn a lightweight Claude CLI call to generate a 4-6 word thread title.

    Bypasses the runner semaphore — this is a standalone, cheap subprocess.
    Returns the title string, or None on failure/timeout.
    """
    from bot.claude.parser import extract_result, parse_stream_line

    title_prompt = (
        "Generate a 4-6 word title for this coding session. "
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

    proc = None
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
