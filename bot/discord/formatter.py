"""Discord-specific formatting: embeds, escape, chunking."""

from __future__ import annotations

import re

import discord

from bot.claude.types import Instance, InstanceStatus
from bot.platform.formatting import (
    format_context_footer, format_duration, redact_secrets, status_icon,
)


def escape_discord(text: str) -> str:
    """Escape Discord markdown special characters."""
    for char in ('\\', '*', '_', '~', '`', '|', '>', '#'):
        text = text.replace(char, '\\' + char)
    return text


def chunk_message(text: str, limit: int = 4096) -> list[str]:
    """Split text into Discord-safe chunks (4096 for embed description)."""
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

        # Preserve code block continuity
        if chunk.count('```') % 2 != 0:
            chunk += '\n```'
            text = '```\n' + text

        chunks.append(chunk)

    return chunks


def result_color(instance: Instance) -> discord.Color:
    """Color for result embed based on status."""
    return {
        InstanceStatus.COMPLETED: discord.Color.green(),
        InstanceStatus.FAILED: discord.Color.red(),
        InstanceStatus.KILLED: discord.Color.orange(),
        InstanceStatus.RUNNING: discord.Color.blue(),
        InstanceStatus.QUEUED: discord.Color.greyple(),
    }.get(instance.status, discord.Color.default())


def _flag_summary(instance: Instance) -> str:
    """One-line summary of session-eval flags by category, or "" if none.

    Reads the persisted SessionEval written by finalize_run; returns ""
    if eval is disabled, missing, or has no flags. Surfaces flag counts
    inline on the result footer so warnings aren't hidden in data/evals/.
    """
    try:
        from bot.engine.eval import load_session_eval
    except Exception:
        return ""
    ev = load_session_eval(instance.id)
    if not ev or not ev.flags:
        return ""
    counts: dict[str, int] = {}
    for f in ev.flags:
        counts[f.category] = counts.get(f.category, 0) + 1
    # Stable order: severity-aware-ish — issues/warnings tend to read first.
    parts = [f"{n} {cat}" for cat, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    icon = "⚠" if any(f.severity in ("warning", "issue") for f in ev.flags) else "ℹ"
    return f"{icon} flags: {', '.join(parts)}"


def build_result_embed(
    instance: Instance,
    description: str,
    metadata: dict | None = None,
) -> discord.Embed:
    """Build a result embed for Discord."""
    embed = discord.Embed(
        title=f"{status_icon(instance.status)} {instance.display_id()}",
        description=description[:4096],
        color=result_color(instance),
    )

    # Footer with metadata
    footer_parts = []
    dur = format_duration(instance.duration_ms)
    if dur:
        footer_parts.append(dur)
    if instance.cost_usd:
        footer_parts.append(f"${instance.cost_usd:.4f}")
    if instance.mode == "build":
        footer_parts.append("build")
    if instance.branch:
        footer_parts.append(f"branch: {instance.branch}")
    # Context usage snapshot from the last assistant event
    if instance.context_tokens > 0:
        ctx_text, _ = format_context_footer(
            instance.context_tokens, instance.context_model, instance.repo_path,
        )
        if ctx_text:
            footer_parts.append(ctx_text)
    flags_line = _flag_summary(instance)
    if flags_line:
        footer_parts.append(flags_line)

    if footer_parts:
        embed.set_footer(text=" | ".join(footer_parts))

    return embed
