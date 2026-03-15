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
    """Extract the final result from accumulated stream-json events."""
    result = RunResult()

    # Single-pass: collect tools and find result event
    tools_seen: set[str] = set()
    result_event: dict | None = None
    fallback_parts: list[str] = []

    for event in events:
        etype = event.get("type", "")

        # Collect tool names
        if etype == "assistant":
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
                    elif block.get("type") == "text":
                        fallback_parts.append(block.get("text", ""))
        elif etype == "content_block_start":
            cb = event.get("content_block", {})
            if cb.get("type") == "tool_use":
                name = cb.get("name", "")
                if name and name not in tools_seen:
                    tools_seen.add(name)
                    result.tools_used.append(name)
        elif etype == "result":
            result_event = event

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

        if result.is_error and not result.result_text:
            errors_list = result_event.get("errors", [])
            if errors_list:
                result.error_message = "; ".join(str(e) for e in errors_list)
            else:
                result.error_message = result_event.get("error", "Unknown error")

    # Fallback: use text collected during the single pass
    if not result.result_text and fallback_parts:
        result.result_text = "\n".join(fallback_parts)

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
