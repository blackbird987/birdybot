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
                return ProgressEvent(turn=turn, tool_name=tool,
                                     message=f"Turn {turn} — {_friendly_tool(tool)}")
        if turn:
            return ProgressEvent(turn=turn, message=f"Turn {turn} — thinking...")
        return None

    # Content block delta with tool info
    if etype == "content_block_start":
        cb = event.get("content_block", {})
        if cb.get("type") == "tool_use":
            tool = cb.get("name", "")
            return ProgressEvent(tool_name=tool,
                                 message=f"Using {_friendly_tool(tool)}...")

    # System message about turn
    if etype == "system" and "turn" in str(event):
        return ProgressEvent(message="Processing...")

    return None


def extract_result(events: list[dict]) -> RunResult:
    """Extract the final result from accumulated stream-json events."""
    result = RunResult()

    # Look for the result message (last event usually)
    for event in reversed(events):
        etype = event.get("type", "")

        if etype == "result":
            result.session_id = event.get("session_id")
            result.cost_usd = event.get("cost_usd", 0.0)
            result.duration_ms = event.get("duration_ms", 0)
            result.is_error = event.get("is_error", False)

            # Result text can be in different places
            result_data = event.get("result", "")
            if isinstance(result_data, str):
                result.result_text = result_data
            elif isinstance(result_data, list):
                # Content blocks
                parts = []
                for block in result_data:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                result.result_text = "\n".join(parts)
            elif isinstance(result_data, dict):
                result.result_text = result_data.get("text", str(result_data))

            if result.is_error and not result.result_text:
                result.error_message = event.get("error", "Unknown error")
            break

    # Fallback: collect all text content from assistant messages
    if not result.result_text:
        parts = []
        for event in events:
            if event.get("type") == "assistant":
                content = event.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
                message = event.get("message", {})
                if isinstance(message, dict):
                    for block in message.get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
        if parts:
            result.result_text = "\n".join(parts)

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
    }
    return mapping.get(tool, tool)
