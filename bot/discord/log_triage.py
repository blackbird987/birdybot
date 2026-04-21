"""Scheduled log triage service.

Periodically tails `data/logs/bot.log`, pipes new content through a
lightweight `claude -p` call, and posts anomalies to a dedicated
"Triage" thread inside The Ark.

Opt-in via LOG_TRIAGE_ENABLED. See bot/config.py for tunables.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from bot import config

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

_TRIAGE_THREAD_NAME = "🔍 Triage"
_MENTION_COOLDOWN_SECS = 6 * 3600  # don't re-ping owner within 6h
_CLEAN_PATTERNS = ("clean", "no issues", "nothing notable", "no anomalies")

_TRIAGE_PROMPT = (
    "You are triaging a Discord bot's log file. Analyse the log lines piped "
    "on stdin and decide whether anything is abnormal.\n\n"
    "If the logs look healthy (routine activity, expected warnings only), "
    "reply with a single line: CLEAN\n\n"
    "Otherwise reply with exactly this shape (one field per line):\n"
    "SEVERITY: [low|med|high]\n"
    "SUMMARY: <one line describing what you saw>\n"
    "HYPOTHESIS: <one line root-cause guess, or 'unclear'>\n\n"
    "Only flag real anomalies — unexpected errors, crashes, stuck state, "
    "repeated failures. Ignore routine INFO/DEBUG noise."
)

_SEVERITY_RE = re.compile(r"^\s*SEVERITY\s*:\s*(low|med|medium|high)\s*$",
                          re.IGNORECASE | re.MULTILINE)


# --- Offset / rotation handling -----------------------------------------------

def _read_since_offset(log_path: Path, last_offset: int, max_lines: int) -> tuple[bytes, int]:
    """Read bytes from last_offset to EOF. Caps output to last max_lines lines.

    Handles rotation: if file size < last_offset, resets to 0.
    Returns (content_bytes, new_offset).
    """
    if not log_path.exists():
        return b"", last_offset

    try:
        size = log_path.stat().st_size
    except OSError:
        return b"", last_offset

    if size < last_offset:
        # Rotation detected — start over
        last_offset = 0

    if size == last_offset:
        return b"", last_offset

    try:
        with open(log_path, "rb") as f:
            f.seek(last_offset)
            content = f.read()
    except OSError:
        log.warning("Failed to read %s from offset %d", log_path, last_offset,
                    exc_info=True)
        return b"", last_offset

    new_offset = last_offset + len(content)

    # Cap to last max_lines
    lines = content.splitlines(keepends=True)
    if len(lines) > max_lines:
        content = b"".join(lines[-max_lines:])

    return content, new_offset


def _seed_initial_offset(log_path: Path, max_lines: int) -> int:
    """Seed the offset on first enable so the first run sees ~max_lines
    of recent history (not the entire log).
    """
    if not log_path.exists():
        return 0
    try:
        size = log_path.stat().st_size
    except OSError:
        return 0
    if size == 0:
        return 0
    # Approximate: read tail and use byte count to figure an offset
    try:
        with open(log_path, "rb") as f:
            # Read last ~120 bytes/line * max_lines as a rough cap
            approx = max_lines * 120
            start = max(0, size - approx)
            f.seek(start)
            tail = f.read()
    except OSError:
        return size

    lines = tail.splitlines(keepends=True)
    if len(lines) <= max_lines:
        return max(0, size - len(tail))
    # Cut from the front so we keep the last max_lines
    drop = b"".join(lines[:-max_lines])
    return max(0, size - (len(tail) - len(drop)))


# --- Redaction ----------------------------------------------------------------

def _collect_env_secrets() -> list[str]:
    """Pull sensitive .env values out of config for post-redaction stripping."""
    names = [
        "DISCORD_BOT_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        "TWITTER_BEARER_TOKEN",
    ]
    values: list[str] = []
    for n in names:
        v = getattr(config, n, None) or os.getenv(n)
        if v and len(v) >= 8:
            values.append(v)
    webhook_url = os.getenv("TEST_WEBHOOK_URL", "")
    lobby_webhook_url = os.getenv("TEST_LOBBY_WEBHOOK_URL", "")
    for v in (webhook_url, lobby_webhook_url):
        if v and len(v) >= 8:
            values.append(v)
    return values


def _redact(text: str, env_secrets: list[str]) -> str:
    """Apply framework redaction + strip any residual .env secret strings."""
    from bot.platform.formatting import redact_secrets
    out = redact_secrets(text)
    for s in env_secrets:
        if s and s in out:
            out = out.replace(s, "[REDACTED]")
    return out


# --- Claude -p call (stdin piping for Windows arg-limit safety) ---------------

async def _invoke_claude(content: bytes) -> str | None:
    """Pipe content to `claude -p`. Returns raw output text, or None on failure."""
    from bot.claude.parser import extract_result, parse_stream_line

    cmd = [
        config.CLAUDE_BINARY, "-p", _TRIAGE_PROMPT,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "plan",
        "--max-turns", "1",
        "--model", config.LOG_TRIAGE_MODEL,
    ]
    env = os.environ.copy()
    env.pop("CLAUDE_CODE", None)
    env.pop("CLAUDECODE", None)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            cwd=tempfile.gettempdir(),
            **config.NOWND,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(input=content),
            timeout=config.LOG_TRIAGE_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        log.warning("Log triage subprocess timed out after %ds",
                    config.LOG_TRIAGE_TIMEOUT_SECS)
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
        return None
    except Exception:
        log.warning("Log triage subprocess failed", exc_info=True)
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
        return None

    events = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        parsed = parse_stream_line(line)
        if parsed:
            events.append(parsed)
    result = extract_result(events)
    return result.result_text.strip() if result.result_text else None


# --- Verdict parsing ----------------------------------------------------------

def _is_clean(verdict: str) -> bool:
    first_line = verdict.strip().splitlines()[0].lower() if verdict.strip() else ""
    return any(p in first_line for p in _CLEAN_PATTERNS)


def _extract_severity(verdict: str) -> str:
    m = _SEVERITY_RE.search(verdict)
    if not m:
        return "low"
    sev = m.group(1).lower()
    return "med" if sev == "medium" else sev


# --- Thread management --------------------------------------------------------

async def _get_or_create_triage_thread(bot: ClaudeBot) -> discord.Thread | None:
    """Return the persisted triage thread, creating one inside The Ark if needed."""
    if not bot._lobby_channel_id:
        log.debug("Log triage: no lobby channel yet, skipping thread lookup")
        return None
    lobby = bot.get_channel(int(bot._lobby_channel_id))
    if not isinstance(lobby, discord.TextChannel):
        log.warning("Log triage: lobby channel %s is not a TextChannel",
                    bot._lobby_channel_id)
        return None

    discord_state = bot._store.get_platform_state("discord") or {}
    stored_id = discord_state.get("log_triage_thread_id")
    if stored_id:
        existing: discord.Thread | None = None
        try:
            cached = bot.get_channel(int(stored_id))
            if isinstance(cached, discord.Thread):
                existing = cached
            else:
                fetched = await bot.fetch_channel(int(stored_id))
                if isinstance(fetched, discord.Thread):
                    existing = fetched
        except discord.NotFound:
            log.info("Stored triage thread %s gone — will create a new one",
                     stored_id)
        except discord.HTTPException:
            log.warning("Triage thread lookup failed transiently — skipping tick",
                        exc_info=True)
            return None
        if existing is not None:
            if existing.archived:
                try:
                    await existing.edit(archived=False)
                except discord.HTTPException:
                    log.debug("Could not unarchive triage thread %s", stored_id)
            return existing

    try:
        thread = await lobby.create_thread(
            name=_TRIAGE_THREAD_NAME,
            auto_archive_duration=10080,  # 7 days — max
            type=discord.ChannelType.public_thread,
        )
    except discord.HTTPException:
        log.exception("Log triage: failed to create triage thread")
        return None

    # Re-read state after the await so we don't clobber writes made by
    # other subsystems while create_thread was in flight.
    discord_state = bot._store.get_platform_state("discord") or {}
    discord_state["log_triage_thread_id"] = str(thread.id)
    bot._store.set_platform_state("discord", discord_state)
    log.info("Created triage thread %s in The Ark", thread.id)
    return thread


# --- Mention rate limiting ----------------------------------------------------

def _should_mention(discord_state: dict) -> bool:
    last = discord_state.get("log_triage_last_mention_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except (ValueError, TypeError):
        return True
    return datetime.now(timezone.utc) - last_dt >= timedelta(seconds=_MENTION_COOLDOWN_SECS)


# --- Main loop ----------------------------------------------------------------

async def run_triage_service(bot: ClaudeBot, stop_event: asyncio.Event) -> None:
    """Background task: poll on interval, triage new log bytes, post findings."""
    log.info(
        "Log triage service starting (interval=%ds, max_lines=%d, model=%s)",
        config.LOG_TRIAGE_INTERVAL_SECS, config.LOG_TRIAGE_MAX_LINES,
        config.LOG_TRIAGE_MODEL,
    )

    log_path = config.LOG_FILE

    # Seed offset on first enable so we triage recent history, not the whole log.
    discord_state = bot._store.get_platform_state("discord") or {}
    if "log_triage_last_offset" not in discord_state:
        seeded = _seed_initial_offset(log_path, config.LOG_TRIAGE_MAX_LINES)
        discord_state["log_triage_last_offset"] = seeded
        bot._store.set_platform_state("discord", discord_state)
        log.info("Log triage: seeded initial offset to %d bytes", seeded)

    env_secrets = _collect_env_secrets()

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=config.LOG_TRIAGE_INTERVAL_SECS,
            )
            break  # stop_event was set
        except asyncio.TimeoutError:
            pass  # interval elapsed — proceed with a triage tick

        try:
            await _triage_tick(bot, log_path, env_secrets)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Log triage tick failed — continuing")

    log.info("Log triage service stopped")


async def _triage_tick(bot: ClaudeBot, log_path: Path, env_secrets: list[str]) -> None:
    discord_state = bot._store.get_platform_state("discord") or {}
    last_offset = int(discord_state.get("log_triage_last_offset", 0) or 0)

    content, new_offset = _read_since_offset(
        log_path, last_offset, config.LOG_TRIAGE_MAX_LINES,
    )
    if new_offset != last_offset:
        discord_state["log_triage_last_offset"] = new_offset
        bot._store.set_platform_state("discord", discord_state)

    if not content:
        return

    text = content.decode("utf-8", errors="replace")
    redacted = _redact(text, env_secrets)

    verdict = await _invoke_claude(redacted.encode("utf-8"))
    if not verdict:
        return  # subprocess failure already logged

    if _is_clean(verdict):
        log.debug("Log triage: CLEAN (%d bytes scanned)", len(content))
        return

    severity = _extract_severity(verdict)
    await _post_finding(bot, verdict, severity)


async def _post_finding(bot: ClaudeBot, verdict: str, severity: str) -> None:
    thread = await _get_or_create_triage_thread(bot)
    if not thread:
        log.warning("Log triage: no thread available — skipping post")
        return

    mention = ""
    will_mention = False
    if severity == "high":
        discord_state = bot._store.get_platform_state("discord") or {}
        if _should_mention(discord_state):
            user_id = config.DISCORD_USER_ID
            if user_id:
                mention = f"<@{user_id}> "
                will_mention = True

    icon = {"high": "🚨", "med": "⚠️", "low": "🔎"}.get(severity, "🔎")
    safe_verdict = verdict.strip()[:1500].replace("```", "'''")
    body = f"{mention}{icon} **Log triage** ({severity})\n```\n{safe_verdict}\n```"

    try:
        await thread.send(body, silent=(severity != "high"))
    except discord.HTTPException:
        log.exception("Log triage: failed to post finding to thread %s", thread.id)
        return

    if will_mention:
        discord_state = bot._store.get_platform_state("discord") or {}
        discord_state["log_triage_last_mention_at"] = (
            datetime.now(timezone.utc).isoformat()
        )
        bot._store.set_platform_state("discord", discord_state)
