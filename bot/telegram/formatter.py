"""Telegram output formatting — MarkdownV2, chunking, inline buttons, status/cost displays."""

from __future__ import annotations

import re
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.claude.types import Instance, InstanceStatus, InstanceType, Schedule


# --- MarkdownV2 Escaping ---

_SPECIAL_CHARS = r'_*[]()~`>#+-=|{}.!'


def escape_md(text: str) -> str:
    """Escape special MarkdownV2 characters outside code blocks."""
    return re.sub(r'([' + re.escape(_SPECIAL_CHARS) + r'])', r'\\\1', text)


def to_telegram_markdown(text: str) -> str:
    """Convert standard markdown to Telegram MarkdownV2 with fallback."""
    try:
        return _convert_markdown(text)
    except Exception:
        # Fallback: escape everything
        return escape_md(text)


def _convert_markdown(text: str) -> str:
    """Best-effort markdown conversion for MarkdownV2."""
    result_parts = []
    i = 0
    lines = text.split('\n')
    in_code_block = False
    code_block_lines: list[str] = []
    code_lang = ""

    for line in lines:
        # Check for code block fences
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code_block:
                # End code block
                code_content = '\n'.join(code_block_lines)
                if code_lang:
                    result_parts.append(f"```{code_lang}\n{code_content}\n```")
                else:
                    result_parts.append(f"```\n{code_content}\n```")
                code_block_lines = []
                code_lang = ""
                in_code_block = False
            else:
                # Start code block
                in_code_block = True
                code_lang = stripped[3:].strip()
            continue

        if in_code_block:
            code_block_lines.append(line)
            continue

        # Outside code blocks — escape and format
        converted = _convert_inline(line)
        result_parts.append(converted)

    # Handle unclosed code block
    if in_code_block and code_block_lines:
        code_content = '\n'.join(code_block_lines)
        result_parts.append(f"```\n{code_content}\n```")

    return '\n'.join(result_parts)


def _convert_inline(line: str) -> str:
    """Convert inline markdown elements in a single line."""
    # Headers -> bold
    header_match = re.match(r'^(#{1,6})\s+(.*)', line)
    if header_match:
        return f"*{escape_md(header_match.group(2))}*"

    # Process inline code first (protect from escaping)
    parts = re.split(r'(`[^`]+`)', line)
    result = []
    for part in parts:
        if part.startswith('`') and part.endswith('`'):
            result.append(part)  # Inline code — no escaping
        else:
            # Bold: **text** -> *text*
            converted = re.sub(
                r'\*\*(.+?)\*\*',
                lambda m: f"*{escape_md(m.group(1))}*",
                part
            )
            # Italic: _text_ (single underscore, not in bold)
            # Already MarkdownV2 compatible after escaping
            converted = escape_md(converted) if converted == part else converted
            if converted == part:
                converted = escape_md(part)
            result.append(converted)

    return ''.join(result)


# --- Message Chunking ---

def chunk_message(text: str, limit: int = 4096) -> list[str]:
    """Split text into Telegram-safe chunks."""
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        # Try to split at newline
        cut = text.rfind('\n', 0, limit)
        if cut <= 0:
            # Try space
            cut = text.rfind(' ', 0, limit)
        if cut <= 0:
            cut = limit

        chunks.append(text[:cut])
        text = text[cut:].lstrip('\n')

    return chunks


# --- Result File ---

def save_result_file(instance_id: str, text: str, results_dir: Path,
                     suffix: str = ".md") -> str:
    """Write result text to data/results/ and return path."""
    path = results_dir / f"{instance_id}{suffix}"
    path.write_text(text, encoding="utf-8")
    return str(path)


# --- Inline Buttons ---

def build_action_buttons(instance: Instance) -> InlineKeyboardMarkup | None:
    """Build inline keyboard based on instance status and type."""
    buttons: list[list[InlineKeyboardButton]] = []
    iid = instance.id

    if instance.status == InstanceStatus.COMPLETED:
        row1 = [
            InlineKeyboardButton("Continue", callback_data=f"continue:{iid}"),
            InlineKeyboardButton("Retry", callback_data=f"retry:{iid}"),
            InlineKeyboardButton("Log", callback_data=f"log:{iid}"),
        ]
        buttons.append(row1)
        # Build bg tasks get merge/discard buttons
        if instance.branch:
            buttons.append([
                InlineKeyboardButton("Diff", callback_data=f"diff:{iid}"),
                InlineKeyboardButton("Merge", callback_data=f"merge:{iid}"),
                InlineKeyboardButton("Discard", callback_data=f"discard:{iid}"),
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
    """Format a completed/failed instance result for Telegram."""
    parts = [f"*{escape_md(instance.display_id())}*"]

    if instance.status == InstanceStatus.FAILED:
        parts.append(f"FAILED: {escape_md(instance.error or 'Unknown error')}")
    elif instance.summary:
        parts.append(escape_md(instance.summary))

    # Cost and duration
    meta = []
    if instance.cost_usd is not None:
        meta.append(f"${instance.cost_usd:.4f}")
    if instance.duration_ms is not None:
        secs = instance.duration_ms / 1000
        if secs >= 60:
            meta.append(f"{secs / 60:.1f}m")
        else:
            meta.append(f"{secs:.0f}s")
    if instance.mode == "build":
        meta.append("build")
    if meta:
        parts.append(escape_md(" | ".join(meta)))

    return "\n".join(parts)


def format_instance_list(instances: list[Instance]) -> str:
    """Format instance list with status indicators."""
    if not instances:
        return escape_md("No instances found.")

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
        cost_part = f" ${inst.cost_usd:.4f}" if inst.cost_usd else ""
        prompt_preview = inst.prompt[:40] + "..." if len(inst.prompt) > 40 else inst.prompt

        lines.append(
            f"{status_icon} `{inst.id}{name_part}`{cost_part} "
            f"{escape_md(prompt_preview)}"
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
    """Format /status health dashboard."""
    uptime_h = uptime_secs / 3600
    parts = [
        f"*Status*",
        f"Uptime: {escape_md(f'{uptime_h:.1f}h')}",
        f"Running: {running}",
        f"Today: {escape_md(f'${daily_cost:.4f}')}",
        f"Total: {escape_md(f'${total_cost:.4f}')}",
        f"Mode: `{mode}`",
    ]
    if repo_name:
        parts.append(f"Repo: `{escape_md(repo_name)}` \\({escape_md(repo_path or '')}\\)")
    if context:
        parts.append(f"Context: {escape_md(context[:100])}")
    if schedule_count:
        parts.append(f"Schedules: {schedule_count}")
    parts.append(f"CLI: {escape_md(cli_version)}")
    return "\n".join(parts)


def format_cost(daily: float, total: float, top_spenders: list[Instance]) -> str:
    """Format /cost breakdown."""
    lines = [
        f"*Cost*",
        f"Today: {escape_md(f'${daily:.4f}')}",
        f"Total: {escape_md(f'${total:.4f}')}",
    ]
    if top_spenders:
        lines.append(f"\n*Top spenders today:*")
        for inst in top_spenders:
            cost = f"${inst.cost_usd:.4f}" if inst.cost_usd else "$0"
            lines.append(f"  `{inst.id}` {escape_md(cost)} — {escape_md(inst.prompt[:30])}")
    return "\n".join(lines)


def format_digest(
    instance_count: int,
    daily_cost: float,
    failures: int,
    repo_name: str | None,
    mode: str,
) -> str:
    """Format daily digest."""
    lines = [
        f"*Daily Digest*",
        f"Instances: {instance_count}",
        f"Cost: {escape_md(f'${daily_cost:.4f}')}",
        f"Failures: {failures}",
    ]
    if repo_name:
        lines.append(f"Repo: `{escape_md(repo_name)}`")
    lines.append(f"Mode: `{mode}`")
    return "\n".join(lines)


def format_schedule_list(schedules: list[Schedule]) -> str:
    """Format active schedules."""
    if not schedules:
        return escape_md("No active schedules.")

    lines = [f"*Schedules*"]
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
            next_run = f" next: {escape_md(s.next_run_at[:16])}"

        prompt_preview = s.prompt[:40] + "..." if len(s.prompt) > 40 else s.prompt
        lines.append(
            f"  `{s.id}` {escape_md(interval)}{next_run}\n"
            f"    {escape_md(prompt_preview)}"
        )

    return "\n".join(lines)
