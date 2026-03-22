"""Parse Claude CLI stream-json output."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .types import RunResult

log = logging.getLogger(__name__)


@dataclass
class ProgressEvent:
    """A progress update extracted from stream-json."""
    turn: int = 0
    tool_name: str | None = None
    message: str = ""
    detail: str = ""  # Verbose info (tool input snippet)


def parse_stream_line(line: str) -> dict | None:
    """Parse a single line of stream-json output. Returns dict or None."""
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        log.debug("Non-JSON line from Claude CLI: %s", line[:200])
        return None


def iter_tool_blocks(event: dict):
    """Yield (tool_name, tool_input) for each tool_use block in an event."""
    etype = event.get("type", "")
    if etype == "assistant":
        content = event.get("content", [])
        if not content:
            msg = event.get("message", {})
            if isinstance(msg, dict):
                content = msg.get("content", [])
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block.get("name", ""), block.get("input", {})
    elif etype == "content_block_start":
        cb = event.get("content_block", {})
        if cb.get("type") == "tool_use":
            yield cb.get("name", ""), cb.get("input", {})


def extract_progress(event: dict) -> ProgressEvent | None:
    """Extract a user-friendly progress event from a stream-json message."""
    etype = event.get("type", "")

    # Assistant turn with tool use
    if etype == "assistant":
        message = event.get("message", {})
        turn = message.get("turn", 0) if isinstance(message, dict) else 0
        # Look for tool_use in content blocks
        content = event.get("content", [])
        if not content and isinstance(message, dict):
            content = message.get("content", [])
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool = block.get("name", "thinking")
                tool_input = block.get("input", {})
                detail = _tool_detail(tool, tool_input)
                return ProgressEvent(
                    turn=turn, tool_name=tool,
                    message=f"Turn {turn} — {_friendly_tool(tool)}",
                    detail=f"Turn {turn} — {detail}" if detail else "",
                )
        if turn:
            return ProgressEvent(turn=turn, message=f"Turn {turn} — thinking...")
        return None

    # Content block delta with tool info
    if etype == "content_block_start":
        cb = event.get("content_block", {})
        if cb.get("type") == "tool_use":
            tool = cb.get("name", "")
            tool_input = cb.get("input", {})
            detail = _tool_detail(tool, tool_input)
            return ProgressEvent(
                tool_name=tool,
                message=f"Using {_friendly_tool(tool)}...",
                detail=f"{detail}..." if detail else "",
            )

    # System message about turn
    if etype == "system" and "turn" in str(event):
        return ProgressEvent(message="Processing...")

    return None


def extract_result(events: list[dict]) -> RunResult:
    """Extract the final result from accumulated stream-json events.

    Tracks text per assistant turn.  The CLI ``result`` event only carries
    the *last* turn's text — earlier turns are prepended only when the
    final result is suspiciously short (suggesting the real answer was
    delivered in an earlier turn).
    """
    result = RunResult()

    # Single-pass: collect tools, per-turn text, and the result event
    tools_seen: set[str] = set()
    result_event: dict | None = None
    # Per-turn text tracking: each assistant event starts a new turn
    assistant_turns: list[list[str]] = []
    current_turn: list[str] = []

    for event in events:
        etype = event.get("type", "")

        if etype == "assistant":
            # Each assistant event = new turn — save previous if it had text
            if current_turn:
                assistant_turns.append(current_turn)
            current_turn = []

            content = event.get("content", [])
            if not content:
                message = event.get("message", {})
                if isinstance(message, dict):
                    content = message.get("content", [])
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        name = block.get("name", "")
                        if name and name not in tools_seen:
                            tools_seen.add(name)
                            result.tools_used.append(name)
                        if name == "Bash":
                            cmd = block.get("input", {}).get("command", "")
                            if cmd:
                                result.bash_commands.append(cmd[:200])
                    elif block.get("type") == "text":
                        current_turn.append(block.get("text", ""))
        # content_block_start: tool/bash tracking only — no turn text
        elif etype == "content_block_start":
            cb = event.get("content_block", {})
            if cb.get("type") == "tool_use":
                name = cb.get("name", "")
                if name and name not in tools_seen:
                    tools_seen.add(name)
                    result.tools_used.append(name)
                if name == "Bash":
                    cmd = cb.get("input", {}).get("command", "")
                    if cmd:
                        result.bash_commands.append(cmd[:200])
        elif etype == "result":
            result_event = event

    # Save final turn
    if current_turn:
        assistant_turns.append(current_turn)

    # Extract result data
    if result_event:
        result.session_id = result_event.get("session_id")
        result.cost_usd = result_event.get("cost_usd", 0.0)
        result.duration_ms = result_event.get("duration_ms", 0)
        result.duration_api_ms = result_event.get("duration_api_ms", 0)
        result.is_error = result_event.get("is_error", False)
        result.num_turns = result_event.get("num_turns", 0)
        # Tokens may be top-level or nested under "usage"
        usage = result_event.get("usage") or {}
        result.input_tokens = (
            result_event.get("input_tokens")
            or usage.get("input_tokens")
            or 0
        )
        result.output_tokens = (
            result_event.get("output_tokens")
            or usage.get("output_tokens")
            or 0
        )

        result_data = result_event.get("result", "")
        if isinstance(result_data, str):
            result.result_text = result_data
        elif isinstance(result_data, list):
            parts = []
            for block in result_data:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            result.result_text = "\n".join(parts)
        elif isinstance(result_data, dict):
            result.result_text = result_data.get("text", str(result_data))

        # Prepend intermediate turn text only when the final result is
        # suspiciously short — suggesting the real answer was in an earlier
        # turn.  Uses a proportional gate so it scales with session length.
        if result.result_text and len(assistant_turns) > 1:
            intermediate_parts: list[str] = []
            for turn in assistant_turns[:-1]:
                text = "\n".join(t for t in turn if t.strip())
                if text.strip():
                    intermediate_parts.append(text)
            if intermediate_parts:
                intermediate = "\n\n---\n\n".join(intermediate_parts)
                final_len = len(result.result_text)
                inter_len = len(intermediate)
                if final_len < inter_len * 0.15:
                    result.result_text = (
                        intermediate + "\n\n---\n\n" + result.result_text
                    )

        if result.is_error and not result.result_text:
            errors_list = result_event.get("errors", [])
            if errors_list:
                result.error_message = "; ".join(str(e) for e in errors_list)
            else:
                result.error_message = result_event.get("error", "Unknown error")

    # Fallback: no result event — prefer last substantial turn over
    # joining everything (avoids wall-of-text from narration turns).
    if not result.result_text and assistant_turns:
        for turn in reversed(assistant_turns):
            text = "\n".join(t for t in turn if t.strip())
            if len(text) >= 100:
                result.result_text = text
                break
        if not result.result_text:
            all_parts = [t for turn in assistant_turns for t in turn if t.strip()]
            result.result_text = "\n".join(all_parts)

    return result


def extract_summary(text: str, max_len: int = 500) -> str:
    """Extract first paragraph as summary, max max_len chars."""
    if not text:
        return ""

    # Strip leading whitespace and markdown headers
    text = text.strip()
    text = re.sub(r'^#+\s+', '', text)

    # First paragraph: split on double newline
    paragraphs = re.split(r'\n\s*\n', text, maxsplit=1)
    summary = paragraphs[0].strip()

    # Collapse newlines within the paragraph
    summary = re.sub(r'\s*\n\s*', ' ', summary)

    if len(summary) > max_len:
        # Cut at last sentence boundary or word boundary
        truncated = summary[:max_len]
        last_period = truncated.rfind('. ')
        if last_period > max_len // 2:
            return truncated[:last_period + 1]
        last_space = truncated.rfind(' ')
        if last_space > 0:
            return truncated[:last_space] + "..."
        return truncated + "..."

    return summary


def parse_usage_limit(error_text: str):
    """Detect Claude subscription usage-limit errors and extract reset time.

    Returns a datetime (UTC) when the limit resets, or None if this isn't a
    usage-limit error.  Handles patterns like:
      - "resets 12pm" / "resets 3:00 PM"
      - "resets Mar 20, 12pm"
      - "resets in 2 hours"
    Falls back to 4 hours from now if the limit is detected but the reset
    time can't be parsed (conservative to avoid retry storms).
    """
    from datetime import datetime, timedelta, timezone

    if not error_text:
        return None
    lower = error_text.lower()
    # Must look like a subscription usage cap (NOT a transient 429 rate limit)
    limit_phrases = ["hit your limit", "usage limit", "plan limit"]
    if not any(p in lower for p in limit_phrases):
        return None

    now = datetime.now(timezone.utc)

    # Pattern: "resets in X hours" / "resets in Xh"
    m = re.search(r'resets?\s+in\s+(\d+)\s*h', lower)
    if m:
        return now + timedelta(hours=int(m.group(1)))

    # Pattern: "resets 12pm" / "resets 3:00 PM" / "resets Mar 20, 12pm"
    m = re.search(
        r'resets?\s+(?:(\w+)\s+(\d{1,2}),?\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)',
        lower,
    )
    if m:
        month_str, day_str, hour_str, min_str, ampm = m.groups()
        hour = int(hour_str)
        minute = int(min_str) if min_str else 0
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0

        # Try to use the user's timezone (Europe/Amsterdam) for parsing
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo("Europe/Amsterdam")
        except Exception:
            tz = timezone.utc

        local_now = datetime.now(tz)

        if month_str and day_str:
            # Explicit date given (e.g., "Mar 20")
            month_map = {
                "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
            }
            month = month_map.get(month_str[:3], local_now.month)
            day = int(day_str)
            reset_local = local_now.replace(
                month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0,
            )
        else:
            # Time only — assume today, or tomorrow if already past
            reset_local = local_now.replace(
                hour=hour, minute=minute, second=0, microsecond=0,
            )
            if reset_local <= local_now:
                reset_local += timedelta(days=1)

        # Convert to UTC
        return reset_local.astimezone(timezone.utc)

    # Detected limit but couldn't parse reset time — conservative fallback
    log.warning("Usage limit detected but couldn't parse reset time: %s", error_text[:200])
    return now + timedelta(hours=4)


def is_transient_error(error_text: str) -> bool:
    """Check if an error is transient and worth retrying."""
    if not error_text:
        return False
    lower = error_text.lower()
    transient_patterns = [
        "rate limit",
        "overloaded",
        "connection refused",
        "network",
        "timeout",
        "502",
        "503",
        "529",
        "econnreset",
        "econnrefused",
        "socket hang up",
    ]
    return any(p in lower for p in transient_patterns)


def _friendly_tool(tool: str) -> str:
    """Convert tool names to friendly descriptions."""
    mapping = {
        "Read": "reading files",
        "Glob": "searching files",
        "Grep": "searching code",
        "Edit": "editing code",
        "Write": "writing files",
        "Bash": "running command",
        "WebSearch": "searching web",
        "WebFetch": "fetching page",
        "Task": "delegating task",
        "AskUserQuestion": "asking a question",
    }
    return mapping.get(tool, tool)


def _tool_detail(tool: str, tool_input: dict) -> str:
    """Extract verbose detail from tool input. Returns '' if no specific detail."""
    if not isinstance(tool_input, dict) or not tool_input:
        return ""
    if tool == "Read":
        path = tool_input.get("file_path", "")
        return f"reading {_short_path(path)}" if path else ""
    if tool == "Glob":
        pattern = tool_input.get("pattern", "")
        return f"glob {pattern}" if pattern else ""
    if tool == "Grep":
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        if pattern and path:
            return f"grep '{pattern[:30]}' in {_short_path(path)}"
        return f"grep '{pattern[:30]}'" if pattern else ""
    if tool == "Edit":
        path = tool_input.get("file_path", "")
        return f"editing {_short_path(path)}" if path else ""
    if tool == "Write":
        path = tool_input.get("file_path", "")
        return f"writing {_short_path(path)}" if path else ""
    if tool == "Bash":
        desc = tool_input.get("description", "")
        if desc:
            return desc[:60]
        # Don't show raw commands — they're confusing for users
        cmd = tool_input.get("command", "")
        if not cmd:
            return ""
        # Show first line only, truncated, no multiline scripts
        first_line = cmd.split("\n")[0].strip()
        if len(first_line) > 40 or not first_line:
            return "running command"
        return f"$ {first_line}"
    if tool == "WebSearch":
        query = tool_input.get("query", "")
        return f"searching '{query[:40]}'" if query else ""
    if tool == "WebFetch":
        url = tool_input.get("url", "")
        return f"fetching {url[:50]}" if url else ""
    if tool == "Task":
        desc = tool_input.get("description", "")
        return f"task: {desc[:40]}" if desc else ""
    if tool == "AskUserQuestion":
        q = tool_input.get("question", "")
        return f"question: {q[:60]}" if q else ""
    return ""


def _short_path(path: str) -> str:
    """Shorten a file path for progress display."""
    if not path:
        return ""
    parts = path.replace("\\", "/").split("/")
    if len(parts) <= 2:
        return path
    return "/".join(parts[-2:])
