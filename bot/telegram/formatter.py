"""Telegram-specific formatting — HTML conversion, escaping, chunking.

Platform-agnostic formatting (result, status, cost, digest, redaction, buttons)
lives in bot.platform.formatting.
"""

from __future__ import annotations

import re

# Re-export shared functions for backward compat
from bot.platform.formatting import redact_secrets  # noqa: F401


# --- HTML Escaping ---

def escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- Markdown to Telegram HTML ---

def to_telegram_html(text: str) -> str:
    """Convert standard markdown to Telegram HTML with fallback to plain escaped text."""
    try:
        return _convert_markdown_to_html(text)
    except Exception:
        return escape_html(text)


def _convert_markdown_to_html(text: str) -> str:
    """Convert markdown to Telegram-compatible HTML."""
    result_parts = []
    lines = text.split('\n')
    in_code_block = False
    code_block_lines: list[str] = []
    code_lang = ""

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code_block:
                code_content = escape_html('\n'.join(code_block_lines))
                if code_lang:
                    result_parts.append(
                        f'<pre><code class="language-{escape_html(code_lang)}">'
                        f'{code_content}</code></pre>'
                    )
                else:
                    result_parts.append(f"<pre>{code_content}</pre>")
                code_block_lines = []
                code_lang = ""
                in_code_block = False
            else:
                in_code_block = True
                code_lang = stripped[3:].strip()
            continue

        if in_code_block:
            code_block_lines.append(line)
            continue

        result_parts.append(_convert_line_to_html(line))

    if in_code_block and code_block_lines:
        code_content = escape_html('\n'.join(code_block_lines))
        result_parts.append(f"<pre>{code_content}</pre>")

    return '\n'.join(result_parts)


def _convert_line_to_html(line: str) -> str:
    """Convert a single line of markdown to HTML."""
    header_match = re.match(r'^(#{1,6})\s+(.*)', line)
    if header_match:
        return f"<b>{escape_html(header_match.group(2))}</b>"

    parts = re.split(r'(`[^`]+`)', line)
    result = []
    for part in parts:
        if part.startswith('`') and part.endswith('`') and len(part) > 2:
            inner = part[1:-1]
            result.append(f"<code>{escape_html(inner)}</code>")
        elif part == '``':
            result.append(escape_html(part))
        else:
            result.append(_convert_text_to_html(part))

    return ''.join(result)


def _convert_text_to_html(text: str) -> str:
    """Convert bold/italic markers in a text segment to HTML."""
    output = []
    i = 0
    n = len(text)

    while i < n:
        if i < n - 1 and text[i:i+2] == '**':
            end = text.find('**', i + 2)
            if end != -1:
                inner = text[i+2:end]
                output.append(f"<b>{escape_html(inner)}</b>")
                i = end + 2
                continue

        if text[i] == '*':
            end = text.find('*', i + 1)
            if end != -1 and end > i + 1:
                inner = text[i+1:end]
                output.append(f"<i>{escape_html(inner)}</i>")
                i = end + 1
                continue

        if i < n - 1 and text[i:i+2] == '~~':
            end = text.find('~~', i + 2)
            if end != -1:
                inner = text[i+2:end]
                output.append(f"<s>{escape_html(inner)}</s>")
                i = end + 2
                continue

        next_special = n
        for marker in ('**', '*', '~~'):
            pos = text.find(marker, i + 1)
            if pos != -1 and pos < next_special:
                next_special = pos

        if next_special > i:
            output.append(escape_html(text[i:next_special]))
            i = next_special
        else:
            output.append(escape_html(text[i]))
            i += 1

    return ''.join(output)


# --- Message Chunking ---

def chunk_message(text: str, limit: int = 4096) -> list[str]:
    """Split text into Telegram-safe chunks, preserving code block continuity."""
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        effective = limit - 10
        cut = text.rfind('\n', 0, effective)
        if cut <= 0:
            cut = text.rfind(' ', 0, effective)
        if cut <= 0:
            cut = effective

        chunk = text[:cut]
        text = text[cut:].lstrip('\n')

        if chunk.count('<pre>') > chunk.count('</pre>'):
            chunk += '</pre>'
            text = '<pre>' + text

        chunks.append(chunk)

    return chunks
