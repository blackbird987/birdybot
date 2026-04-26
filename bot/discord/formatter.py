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


# --- Markdown table -> Discord-safe rendering ---
#
# Discord ignores GFM pipe tables — they render as raw "| col | col |" lines
# with a literal "|---|---|" separator. We rewrite tables into either a padded
# monospace code block (preferred, fits mobile) or a bullet list (fallback for
# wide tables that would wrap awkwardly inside ```).

# Width above which the code-block form wraps badly on Discord mobile. Tuned
# from observed wrapping in the default mobile font.
_MOBILE_CODEBLOCK_WIDTH = 58

_TABLE_RE = re.compile(
    r'(?:^[ \t]*\|.*\|[ \t]*\n)'         # header row
    r'[ \t]*\|[ \t:|\-]+\|[ \t]*\n'       # separator row (---|---|...)
    r'(?:[ \t]*\|.*\|[ \t]*\n?)+',        # one or more body rows
    re.MULTILINE,
)

_FENCE_RE = re.compile(r'```')


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|'):
        s = s[:-1]
    return [c.strip() for c in s.split('|')]


def _render_table(block: str) -> str:
    lines = [l for l in block.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return block
    header = _split_row(lines[0])
    rows = [_split_row(l) for l in lines[2:]]  # skip separator
    if not rows:
        return block

    cols = len(header)
    all_rows = [header] + rows
    widths = [
        max(len(r[i]) if i < len(r) else 0 for r in all_rows)
        for i in range(cols)
    ]
    total_width = sum(widths) + 2 * (cols - 1)

    def fmt(r: list[str]) -> str:
        return "  ".join(
            (r[i] if i < len(r) else "").ljust(widths[i])
            for i in range(cols)
        ).rstrip()

    if total_width <= _MOBILE_CODEBLOCK_WIDTH:
        sep = "  ".join("─" * w for w in widths)
        body = "\n".join([fmt(header), sep] + [fmt(r) for r in rows])
        return f"```\n{body}\n```"

    # Bullet-list fallback for wide tables.
    out = []
    for r in rows:
        if not r:
            continue
        if len(r) < 2:
            out.append(f"- {r[0]}")
            continue
        key = r[0]
        rest = " — ".join(c for c in r[1:] if c)
        out.append(f"- **{key}** — {rest}" if rest else f"- **{key}**")
    return "\n".join(out)


def _mask_code_blocks(text: str) -> tuple[str, list[str]]:
    """Replace ```...``` regions with placeholders so table regex can't
    match pipe-table syntax that appears inside a code example."""
    chunks: list[str] = []
    out: list[str] = []
    i = 0
    in_fence = False
    fence_start = 0
    for m in _FENCE_RE.finditer(text):
        if not in_fence:
            out.append(text[i:m.start()])
            fence_start = m.start()
            in_fence = True
        else:
            chunks.append(text[fence_start:m.end()])
            out.append(f"\x00FENCE{len(chunks) - 1}\x00")
            i = m.end()
            in_fence = False
    if in_fence:
        # Unclosed fence — leave the rest unmasked; the safety pass will
        # balance it later.
        out.append(text[fence_start:])
    else:
        out.append(text[i:])
    return "".join(out), chunks


def _unmask_code_blocks(text: str, chunks: list[str]) -> str:
    for idx, chunk in enumerate(chunks):
        text = text.replace(f"\x00FENCE{idx}\x00", chunk)
    return text


def convert_pipe_tables(text: str) -> str:
    """Convert markdown pipe tables to Discord-renderable form.

    Tables inside ```...``` fences are left untouched.
    """
    if not text or '|' not in text or '---' not in text:
        return text
    masked, chunks = _mask_code_blocks(text)
    converted = _TABLE_RE.sub(lambda m: _render_table(m.group(0)), masked)
    return _unmask_code_blocks(converted, chunks)


def _balance_code_fences(text: str, limit: int) -> str:
    """Bound text to *limit* and ensure ``` fences stay balanced.

    Output is guaranteed to be (a) at most *limit* characters and (b) have
    an even number of ``` markers, so Discord won't reject the embed
    (4096-char hard cap) or render trailing content as monospace.
    """
    out = text[:limit] if len(text) > limit else text
    if out.count("```") % 2 == 0:
        return out
    # Odd fence count: try to make room and append a closer.
    closer = "\n```"
    cut = max(0, limit - len(closer))
    candidate = text[:cut].rstrip() + closer
    if candidate.count("```") % 2 == 0:
        return candidate
    # Pathological — drop the trailing unclosed fence entirely.
    last = out.rfind("```")
    return out[:last].rstrip() if last >= 0 else out


def apply_discord_safety(text: str, limit: int = 4096) -> str:
    """Render Claude-authored text safely for a Discord embed/message.

    - Rewrites pipe tables into code blocks or bullet lists
    - Truncates to *limit* (4096 for embed, 2000 for content)
    - Keeps ``` fences balanced after truncation
    """
    if not text:
        return text
    return _balance_code_fences(convert_pipe_tables(text), limit)


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
        description=apply_discord_safety(description),
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
