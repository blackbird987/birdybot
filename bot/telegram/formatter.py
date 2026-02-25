"""Telegram output formatting — HTML mode, chunking, inline buttons, status/cost displays."""

from __future__ import annotations

import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, Schedule


# --- HTML Escaping ---

def escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# --- Secret Redaction ---

# Well-known token prefixes (match standalone, no key name needed)
_TOKEN_PATTERNS = [
    # Anthropic API keys
    re.compile(r'sk-ant-[a-zA-Z0-9_-]{20,}'),
    # OpenAI / generic sk- keys
    re.compile(r'sk-[a-zA-Z0-9]{20,}'),
    # GitHub tokens
    re.compile(r'gh[pos]_[a-zA-Z0-9]{20,}'),
    re.compile(r'github_pat_[a-zA-Z0-9_]{20,}'),
    # AWS access keys
    re.compile(r'AKIA[A-Z0-9]{16}'),
    # JWT tokens (eyJ header.payload.signature)
    re.compile(r'eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}'),
    # Ethereum / hex private keys (0x + 64 hex chars)
    re.compile(r'0x[0-9a-fA-F]{64}\b'),
    # Bearer tokens
    re.compile(r'(?i)Bearer\s+[a-zA-Z0-9_./-]{20,}'),
]

# Connection strings: preserve URL structure (mongodb://user:[REDACTED]@host)
_CONN_STRING_PATTERN = re.compile(r'(://[^:\s]+:)([^@\s]{8,})(@)')

# Keywords that indicate a secret value when used as a key/variable name.
# Matches any identifier *containing* these substrings (case-insensitive),
# so SETTINGS_PASSWORD, HmacSecret, AdminPassword, PINATA_JWT all match.
_SECRET_KEY_WORDS = (
    r'password|passwd|secret|mnemonic|private[_-]?key|seed[_-]?phrase|'
    r'api[_-]?key|access[_-]?key|auth[_-]?(?:key|token|secret)|'
    r'hmac|jwt|credential|client[_-]?secret|app[_-]?secret|'
    r'signing[_-]?key|encryption[_-]?key|master[_-]?key|'
    r'db[_-]?password|connection[_-]?string|'
    r'pinata|infura|alchemy|token'
)

# key=value / key: value patterns (env files, JSON, YAML, code assignments)
# Matches: KEY=value, KEY = "value", "Key": "value", string Key = "value"
_KV_PATTERN = re.compile(
    r'(?i)'
    r'(?:^|(?<=[\s"\'`]))'                      # boundary (zero-width)
    r'((?=\w*(?:' + _SECRET_KEY_WORDS + r'))'  # lookahead: key must contain keyword
    r'[a-zA-Z_]\w*)'                           # full key name
    r'["\']?'                                   # optional closing quote on key
    r'\s*[=:]\s*'                               # delimiter
    r'["\']?'                                   # optional opening quote on value
    r'(.+?)'                                    # value (non-greedy)
    r'["\']?'                                   # optional closing quote on value
    r'(?:[,;\s]|$)',                            # boundary
    re.MULTILINE,
)

# Mnemonic / seed phrases: 12+ lowercase words (BIP-39 style)
_MNEMONIC_PATTERN = re.compile(
    r'(?i)(mnemonic|seed[_-]?phrase|recovery[_-]?phrase)\s*[=:"\']*\s*'
    r'([a-z]+(?:\s+[a-z]+){11,})',
)

# Standalone long hex strings (64+ chars) that look like private keys
_HEX_KEY_PATTERN = re.compile(r'(?<![a-zA-Z0-9])[0-9a-fA-F]{64,}(?![a-zA-Z0-9])')


def redact_secrets(text: str) -> str:
    """Scrub API keys, tokens, and secrets from text before sending to Telegram."""
    # 1. Well-known token formats
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub('[REDACTED]', text)

    # 1b. Connection strings (preserve URL structure)
    text = _CONN_STRING_PATTERN.sub(r'\1[REDACTED]\3', text)

    # 2. Mnemonic phrases (before KV so we catch the full phrase)
    text = _MNEMONIC_PATTERN.sub(lambda m: m.group(1) + '=[REDACTED]', text)

    # 3. Key-value pairs where key contains a secret keyword
    text = _KV_PATTERN.sub(lambda m: f'{m.group(1)}=[REDACTED] ', text)

    # 4. Standalone long hex strings (likely private keys)
    text = _HEX_KEY_PATTERN.sub('[REDACTED]', text)

    return text


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

    # Handle unclosed code block
    if in_code_block and code_block_lines:
        code_content = escape_html('\n'.join(code_block_lines))
        result_parts.append(f"<pre>{code_content}</pre>")

    return '\n'.join(result_parts)


def _convert_line_to_html(line: str) -> str:
    """Convert a single line of markdown to HTML."""
    # Headers -> bold
    header_match = re.match(r'^(#{1,6})\s+(.*)', line)
    if header_match:
        return f"<b>{escape_html(header_match.group(2))}</b>"

    # Process the line token by token
    # Split by inline code spans first (protect them)
    parts = re.split(r'(`[^`]+`)', line)
    result = []
    for part in parts:
        if part.startswith('`') and part.endswith('`') and len(part) > 2:
            # Inline code
            inner = part[1:-1]
            result.append(f"<code>{escape_html(inner)}</code>")
        elif part == '``':
            result.append(escape_html(part))
        else:
            result.append(_convert_text_to_html(part))

    return ''.join(result)


def _convert_text_to_html(text: str) -> str:
    """Convert bold/italic markers in a text segment (no inline code) to HTML."""
    output = []
    i = 0
    n = len(text)

    while i < n:
        # Bold: **text**
        if i < n - 1 and text[i:i+2] == '**':
            end = text.find('**', i + 2)
            if end != -1:
                inner = text[i+2:end]
                output.append(f"<b>{escape_html(inner)}</b>")
                i = end + 2
                continue

        # Italic: *text* (single, not at start of bold)
        if text[i] == '*':
            end = text.find('*', i + 1)
            if end != -1 and end > i + 1:
                inner = text[i+1:end]
                output.append(f"<i>{escape_html(inner)}</i>")
                i = end + 1
                continue

        # Strikethrough: ~~text~~
        if i < n - 1 and text[i:i+2] == '~~':
            end = text.find('~~', i + 2)
            if end != -1:
                inner = text[i+2:end]
                output.append(f"<s>{escape_html(inner)}</s>")
                i = end + 2
                continue

        # Find next potential marker
        next_special = n
        for marker in ('**', '*', '~~'):
            pos = text.find(marker, i + 1)
            if pos != -1 and pos < next_special:
                next_special = pos

        # Accumulate plain text up to next marker
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

        # Reserve space for potential </pre> closing tag
        effective = limit - 10
        cut = text.rfind('\n', 0, effective)
        if cut <= 0:
            cut = text.rfind(' ', 0, effective)
        if cut <= 0:
            cut = effective

        chunk = text[:cut]
        text = text[cut:].lstrip('\n')

        # Preserve code block continuity across chunks
        if chunk.count('<pre>') > chunk.count('</pre>'):
            chunk += '</pre>'
            text = '<pre>' + text

        chunks.append(chunk)

    return chunks


# --- Inline Buttons ---

def build_action_buttons(instance: Instance) -> InlineKeyboardMarkup | None:
    """Build inline keyboard based on instance status, origin, and branch."""
    buttons: list[list[InlineKeyboardButton]] = []
    iid = instance.id

    if instance.status == InstanceStatus.COMPLETED:
        if instance.origin in (InstanceOrigin.PLAN, InstanceOrigin.REVIEW_PLAN):
            # Plan result -> review or build
            buttons.append([
                InlineKeyboardButton("Review Plan", callback_data=f"review_plan:{iid}"),
                InlineKeyboardButton("Build It", callback_data=f"build:{iid}"),
            ])
        elif instance.branch:
            # Build task -> diff/merge/discard + review/commit
            buttons.append([
                InlineKeyboardButton("Diff", callback_data=f"diff:{iid}"),
                InlineKeyboardButton("Merge", callback_data=f"merge:{iid}"),
                InlineKeyboardButton("Discard", callback_data=f"discard:{iid}"),
            ])
            buttons.append([
                InlineKeyboardButton("Review Code", callback_data=f"review_code:{iid}"),
                InlineKeyboardButton("Commit", callback_data=f"commit:{iid}"),
            ])
        else:
            # Regular query -> new/retry + plan/build
            buttons.append([
                InlineKeyboardButton("New", callback_data=f"new:{iid}"),
                InlineKeyboardButton("Retry", callback_data=f"retry:{iid}"),
            ])
            buttons.append([
                InlineKeyboardButton("Plan", callback_data=f"plan:{iid}"),
                InlineKeyboardButton("Build It", callback_data=f"build:{iid}"),
            ])

    elif instance.status in (InstanceStatus.RUNNING, InstanceStatus.QUEUED):
        buttons.append([
            InlineKeyboardButton("Kill", callback_data=f"kill:{iid}"),
        ])

    elif instance.status == InstanceStatus.FAILED:
        buttons.append([
            InlineKeyboardButton("Retry", callback_data=f"retry:{iid}"),
            InlineKeyboardButton("Log", callback_data=f"log:{iid}"),
        ])

    elif instance.status == InstanceStatus.KILLED:
        buttons.append([
            InlineKeyboardButton("Retry", callback_data=f"retry:{iid}"),
        ])

    return InlineKeyboardMarkup(buttons) if buttons else None


def build_stall_buttons(instance_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Kill", callback_data=f"kill:{instance_id}"),
        InlineKeyboardButton("Wait", callback_data=f"wait:{instance_id}"),
    ]])


# --- Formatting Functions ---

def format_result(instance: Instance) -> str:
    """Format a completed/failed instance result for Telegram (HTML)."""
    parts = [f"<b>{escape_html(instance.display_id())}</b>"]

    if instance.status == InstanceStatus.FAILED:
        error = redact_secrets(instance.error or 'Unknown error')
        parts.append(f"FAILED: {escape_html(error)}")
    elif instance.summary:
        parts.append(to_telegram_html(redact_secrets(instance.summary)))

    meta = []
    if instance.duration_ms is not None:
        secs = instance.duration_ms / 1000
        if secs >= 60:
            meta.append(f"{secs / 60:.1f}m")
        else:
            meta.append(f"{secs:.0f}s")
    if instance.mode == "build":
        meta.append("build")
    if meta:
        parts.append(escape_html(" | ".join(meta)))

    return "\n".join(parts)


def format_instance_list(instances: list[Instance]) -> str:
    """Format instance list with status indicators (HTML)."""
    if not instances:
        return "No instances found."

    lines = []
    for inst in instances:
        status_icon = {
            InstanceStatus.QUEUED: "⏳",
            InstanceStatus.RUNNING: "🔄",
            InstanceStatus.COMPLETED: "✅",
            InstanceStatus.FAILED: "❌",
            InstanceStatus.KILLED: "💀",
        }.get(inst.status, "❓")

        name_part = f":{inst.name}" if inst.name else ""
        parent_part = f" ← {inst.parent_id}" if inst.parent_id else ""
        prompt_preview = inst.prompt[:40] + "..." if len(inst.prompt) > 40 else inst.prompt

        lines.append(
            f"{status_icon} <code>{inst.id}{name_part}{parent_part}</code> "
            f"{escape_html(prompt_preview)}"
        )

    return "\n".join(lines)


def format_status(
    uptime_secs: float,
    running: int,
    daily_cost: float,
    total_cost: float,
    repo_name: str | None,
    repo_path: str | None,
    mode: str,
    context: str | None,
    schedule_count: int,
    cli_version: str,
) -> str:
    """Format /status health dashboard (HTML)."""
    uptime_h = uptime_secs / 3600
    parts = [
        "<b>Status</b>",
        f"Uptime: {uptime_h:.1f}h",
        f"Running: {running}",
        f"Today: ${daily_cost:.4f}",
        f"Total: ${total_cost:.4f}",
        f"Mode: <code>{mode}</code>",
    ]
    if repo_name:
        parts.append(f"Repo: <code>{escape_html(repo_name)}</code> ({escape_html(repo_path or '')})")
    if context:
        parts.append(f"Context: {escape_html(context[:100])}")
    if schedule_count:
        parts.append(f"Schedules: {schedule_count}")
    parts.append(f"CLI: {escape_html(cli_version)}")
    return "\n".join(parts)


def format_cost(daily: float, total: float, top_spenders: list[Instance]) -> str:
    """Format /cost breakdown (HTML)."""
    lines = [
        "<b>Cost</b>",
        f"Today: ${daily:.4f}",
        f"Total: ${total:.4f}",
    ]
    if top_spenders:
        lines.append("\n<b>Top spenders today:</b>")
        for inst in top_spenders:
            cost = f"${inst.cost_usd:.4f}" if inst.cost_usd else "$0"
            lines.append(f"  <code>{inst.id}</code> {cost} — {escape_html(inst.prompt[:30])}")
    return "\n".join(lines)


def format_digest(
    instance_count: int,
    daily_cost: float,
    failures: int,
    repo_name: str | None,
    mode: str,
) -> str:
    """Format daily digest (HTML)."""
    lines = [
        "<b>Daily Digest</b>",
        f"Instances: {instance_count}",
        f"Cost: ${daily_cost:.4f}",
        f"Failures: {failures}",
    ]
    if repo_name:
        lines.append(f"Repo: <code>{escape_html(repo_name)}</code>")
    lines.append(f"Mode: <code>{mode}</code>")
    return "\n".join(lines)


def format_schedule_list(schedules: list[Schedule]) -> str:
    """Format active schedules (HTML)."""
    if not schedules:
        return "No active schedules."

    lines = ["<b>Schedules</b>"]
    for s in schedules:
        interval = ""
        if s.interval_secs:
            if s.interval_secs >= 86400:
                interval = f"every {s.interval_secs // 86400}d"
            elif s.interval_secs >= 3600:
                interval = f"every {s.interval_secs // 3600}h"
            else:
                interval = f"every {s.interval_secs // 60}m"
        elif s.run_at:
            interval = f"at {s.run_at}"

        next_run = ""
        if s.next_run_at:
            next_run = f" next: {s.next_run_at[:16]}"

        prompt_preview = s.prompt[:40] + "..." if len(s.prompt) > 40 else s.prompt
        lines.append(
            f"  <code>{s.id}</code> {escape_html(interval)}{next_run}\n"
            f"    {escape_html(prompt_preview)}"
        )

    return "\n".join(lines)
