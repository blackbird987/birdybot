"""Platform-agnostic formatting: result, status, cost, digest, redaction, etc."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING

from dataclasses import dataclass, field

from bot import config
from bot.claude.types import Instance, InstanceStatus, Schedule
from bot.platform.buttons import (  # noqa: F401  -- re-exported
    action_button_specs, expanded_button_specs, merge_failed_banner, merge_failed_button_specs,
    queued_button_specs, resolver_running_button_specs, running_button_specs, stall_button_specs,
)
from bot.platform.modes import (  # noqa: F401  -- re-exported
    _WORKFLOW_ORIGINS, EFFORT_DISPLAY, MODE_COLOR, MODE_DISPLAY, MODEL_CLEAR_WORDS, VALID_EFFORTS,
    VALID_MODES, effort_name, mode_label, mode_name, model_suggestions, normalize_model,
)
from bot.platform.redact import redact_secrets  # noqa: F401  -- re-exported
from bot.textutil import DIRECTIVE_ARGS, directive_kv, find_tilde_block, is_quoted_example, mask_tilde_bodies, parse_duration

if TYPE_CHECKING:
    pass


# --- Shared Helpers ---


# Legacy ```verify-board``` fences — the Verify Board feature is gone, but a
# resumed session whose context predates the removal can still emit the block.
# Strip it from displayed text so users never see the raw fence.
_VERIFY_BLOCK_RE = re.compile(r"```verify-board\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_EXCESS_BLANK_RE = re.compile(r"\n{3,}")

# `[TURN_COMPLETE]` — the unattended-turn completion marker (see config and
# lifecycle.has_turn_complete_marker). It's an engine control signal, never
# meant for the user, so strip it from displayed text: first the lone line it's
# instructed to sit on, then any residual inline occurrence.
_TURN_COMPLETE_LINE_RE = re.compile(
    r"(?m)^[ \t]*" + re.escape(config.TURN_COMPLETE_SENTINEL) + r"[ \t]*$\n?"
)
_TURN_COMPLETE_INLINE_RE = re.compile(re.escape(config.TURN_COMPLETE_SENTINEL))


def strip_verify_blocks(text: str) -> str:
    """Remove leftover ```verify-board``` fences and the [TURN_COMPLETE] marker.

    Collapses the ≥3 consecutive newlines that surrounding blank lines
    leave behind so the stripped result doesn't show a visible gap.
    """
    if not text:
        return text
    out = _VERIFY_BLOCK_RE.sub("", text)
    out = _TURN_COMPLETE_LINE_RE.sub("", out)
    out = _TURN_COMPLETE_INLINE_RE.sub("", out)
    out = _EXCESS_BLANK_RE.sub("\n\n", out)
    return out.rstrip()


# --- [BOT_CMD: ...] directive collapsing (display only) --------------------
#
# Directives are the machine-readable control channel between a turn's output
# and the dispatchers (commands._execute_bot_commands, lifecycle.check_wake_
# request). They were never meant to be READ — every dispatch path already
# posts its own human-facing outcome ("I'll check back in ~12 min — …",
# "Spawned new session: <link>", or an explicit refusal). Left in the displayed
# text the raw directive plus its multi-KB ~~~ body is pure duplicate noise
# that also blows past the display budget and truncates the real answer.
#
# So at DISPLAY time each directive collapses to one `-#` subtext line naming
# the command, its params, and its why. Dispatch still reads the RAW text —
# these transforms only ever touch the copy being shown.
#
# Deliberately broader than the dispatchers' strict parsers (any /verb, no
# allow-lists, no caps): a malformed directive should still collapse to one
# line, because the refusal notice posted right after is what explains it.
# Deliberately NARROWER in one respect: the directive must own its whole line.
# That's the documented format; a mid-sentence directive is left visible rather
# than mangling the sentence around it.
#
# Parity with the dispatchers matters. Their guard (textutil.is_quoted_example)
# skips lines starting with `>` / backtick / `#` and anything inside a ```
# fence, so a quoted example never fires. The `[ \t]*` prefix below refuses to
# match the first kind and the loop skips the second, leaving both verbatim.
# Display shows exactly the set of directives that will actually be acted on.
_BOT_CMD_DIRECTIVE_RE = re.compile(
    rf"(?m)^[ \t]*\[BOT_CMD:\s*/(\w+)({DIRECTIVE_ARGS}*)\][ \t]*$"
)
# The tilde-fenced payload (~~~wake / ~~~spawn / ~~~plan). Matched from the end
# of the directive line, tolerating a blank line or two in between; a body
# further away than that belongs to prose, not this directive.
# Only the OPENER is matched here; where the block ends is decided by
# textutil.find_tilde_block, so a /spawn brief carrying a nested ~~~wake block
# collapses whole instead of leaking its second half and a stray closer.
_BOT_CMD_BODY_OPEN_RE = re.compile(
    r"\n(?:[ \t]*\n){0,2}([ \t]*)~~~([a-zA-Z][\w-]*)[ \t]*(?=\n)",
)
# What each chain preset actually runs, so the one-liner explains itself
# without the reader having to remember the preset table.
_CHAIN_PRESET_FLOW = {
    "ship": "build → review → verify → release → merge",
    "hold": "build → review → verify → release, then wait on Merge/Discard",
    "verify": "build → review → verify, then stop",
}
# Chip budget — one line on a phone. Reasons/titles are trimmed to fit.
_CHIP_MAX = 180


def _chip_value(raw: str) -> str:
    """Flatten a directive value into inline-safe one-line text."""
    return re.sub(r"\s+", " ", (raw or "").replace("`", "")).strip()


def _render_directive_chip(verb: str, args: str) -> str | None:
    """One-line summary of a directive: which command, its params, and why.

    Returns None when the directive should vanish from the displayed copy
    entirely (``/image`` — the uploaded picture IS the visible outcome, so a
    chip describing it is pure duplicate noise).
    """
    kv = directive_kv(args)
    parts: list[str] = []

    if verb == "image":
        return None
    if verb == "wake":
        # Same duration grammar the directive parser applies ("3d", "90m",
        # bare seconds), so the chip quotes the delay that was actually armed
        # rather than dropping the unit-suffixed spellings on the floor. A
        # missing/garbage delay yields the 0 sentinel and is simply omitted —
        # the reason still carries the why.
        secs = parse_duration(kv.get("delay") or kv.get("delay_secs"), 0)
        if secs > 0:
            parts.append(f"in {format_delay_secs(secs)}")
        if kv.get("reason"):
            parts.append(_chip_value(kv["reason"]))
    elif verb == "watch":
        if kv.get("label"):
            parts.append(_chip_value(kv["label"]))
        if kv.get("pid"):
            parts.append(f"pid {_chip_value(kv['pid'])}")
        elif kv.get("done"):
            parts.append("until done marker")
        if kv.get("log"):
            parts.append(_chip_value(kv["log"]))
    elif verb == "spawn":
        parts += [
            _chip_value(kv[k]) for k in ("repo", "mode", "effort") if kv.get(k)
        ]
        if kv.get("title"):
            parts.append(f'"{_chip_value(kv["title"])}"')
    elif verb == "reply":
        if kv.get("thread"):
            parts.append(f"answering <#{_chip_value(kv['thread'])}>")
    elif verb == "chain":
        # `preset=ship` and a bare `ship` are both accepted by the dispatcher.
        preset = kv.get("preset") or (args or "").strip().split(" ")[0]
        preset = _chip_value(preset)
        if preset in _CHAIN_PRESET_FLOW:
            parts += [preset, _CHAIN_PRESET_FLOW[preset]]
        else:
            parts.append("repo default policy")
    else:
        # /repo and anything added later: show the args as written.
        if _chip_value(args):
            parts.append(_chip_value(args))

    chip = " · ".join([f"`/{verb}`", *parts]) if parts else f"`/{verb}`"
    if len(chip) > _CHIP_MAX:
        chip = chip[: _CHIP_MAX - 1].rstrip() + "…"
    return f"-# {chip}"


def collapse_bot_directives(text: str) -> str:
    """Replace each [BOT_CMD: ...] directive + its ~~~body~~~ with one line.

    Display-only: callers must pass the copy being shown, never the text handed
    to the dispatchers. See the module comment above for why this exists and
    which directives it deliberately leaves untouched.
    """
    if not text or "[BOT_CMD:" not in text:
        return text
    out: list[str] = []
    pos = 0
    # Found on the masked text, like the dispatchers: a directive inside some
    # tilde block is never acted on, so it stays visible rather than becoming
    # a chip claiming it was. Offsets are shared; bodies are read raw.
    scan = mask_tilde_bodies(text)
    for m in _BOT_CMD_DIRECTIVE_RE.finditer(scan):
        if m.start() < pos:
            continue  # already swallowed as a previous directive's body
        if is_quoted_example(scan, m.start()):
            continue  # an example inside a ``` fence: never acted on, so shown
        out.append(text[pos:m.start()])
        chip = _render_directive_chip(m.group(1), m.group(2))
        if chip is not None:
            out.append(chip)
        pos = m.end()
        opener = _BOT_CMD_BODY_OPEN_RE.match(text, m.end())
        if opener:
            span = find_tilde_block(
                text, opener.group(2), opener.start(1), opener.start(1) + 1,
            )
            if span is not None:
                pos = span[2]
        if chip is None:
            # Nothing rendered in its place — swallow the now-orphaned newline
            # so the directive leaves no gap where its line used to be.
            if text[pos:pos + 1] == "\n":
                pos += 1
    if not out:
        return text
    out.append(text[pos:])
    return _EXCESS_BLANK_RE.sub("\n\n", "".join(out)).rstrip()


def format_duration(ms: int | float | None) -> str:
    """Format duration in milliseconds to a human-readable string."""
    if ms is None:
        return ""
    secs = ms / 1000
    if secs >= 60:
        return f"{secs / 60:.1f}m"
    return f"{secs:.0f}s"


def format_elapsed(secs: float) -> str:
    """Clock on a progress card: ``13s``, then ``12m``, then ``1h05m``.

    Whole minutes past the first one, on purpose. The heartbeat edits the
    live card every 10s, and a clock with a decimal ("65.3m") changes on
    every tick, so no edit could ever be skipped as a no-op. Discord rate
    limits edits to hour-old messages hard, and each 429 is retried inline
    in the stream reader. The finished card uses the same clock, so it does
    not change format at the moment the run ends.
    """
    if secs < 60:
        return f"{int(max(secs, 0))}s"
    minutes = int(secs // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def format_delay_secs(secs: int) -> str:
    """Human-readable wait, e.g. ``45s`` / ``12 min`` / ``2.5 h``.

    Lives here (not in lifecycle) so the collapsed ``/wake`` directive chip and
    the "I'll check back in ~X" confirmation notice that follows it render the
    SAME delay wording — two different modules describing one scheduled wake.
    """
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{round(secs / 60)} min"
    # Days above 36h: the wake ceiling is 30d and a watch can run for days, so
    # without this rung a week-long timer renders as "168.0 h" in both the
    # "I'll check back in ~X" notice and the watch heartbeat's elapsed line.
    if secs < 129600:
        return f"{round(secs / 3600, 1)} h"
    return f"{round(secs / 86400, 1)} d"


def format_tokens(count: int) -> str:
    """Format token count to a compact human-readable string (e.g. 48.2k, 1.3M)."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def short_model_label(model: str | None) -> str:
    """Human-readable model label: 'claude-fable-5' -> 'Fable 5', 'opus' -> 'Opus'.

    Handles full API ids ('claude-opus-4-8', 'claude-haiku-4-5-20251001',
    Bedrock-style 'us.anthropic.claude-...'), CLI aliases ('fable', 'opus'),
    and '-latest' suffixes.  Unrecognized shapes fall back to the raw string.
    """
    if not model:
        return ""
    name = model.strip().lower()
    for prefix in ("us.anthropic.", "eu.anthropic.", "anthropic."):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    name = name.removeprefix("claude-")
    parts = [p for p in name.split("-") if p]
    # Drop trailing date stamps (20251001) and '-latest'/'-v1:0' style suffixes.
    while parts and (
        parts[-1] == "latest"
        or (parts[-1].isdigit() and len(parts[-1]) >= 6)
        or ":" in parts[-1]
    ):
        parts.pop()
    words = [p.capitalize() for p in parts if not p.isdigit()]
    version = ".".join(p for p in parts if p.isdigit())
    label = " ".join(filter(None, [" ".join(words), version]))
    return label or model.strip()


def format_context_footer(
    context_tokens: int,
    model: str | None,
    repo_path: str | None = None,
) -> tuple[str, float]:
    """Render a `"Fable 5 · 72k / 200k · 36%"` style footer string + percent (0..1).

    Returns ("", 0.0) when there is nothing to show.  Model and repo are
    used to resolve the effective window (Sonnet can be 1M or 200k).
    """
    if not context_tokens or context_tokens <= 0:
        return "", 0.0
    # Lazy import — avoids a cycle with bot.claude.models at module load.
    from bot.claude.models import context_window_for
    window = context_window_for(model, repo_path)
    if window <= 0:
        return "", 0.0
    percent = min(context_tokens / window, 1.0)
    text = f"{format_tokens(context_tokens)} / {format_tokens(window)} · {int(round(percent * 100))}%"
    label = short_model_label(model)
    if label:
        text = f"{label} · {text}"
    return text, percent


def format_age(delta: timedelta) -> str:
    """Format a timedelta as a human-readable age string (e.g. '3h ago')."""
    if delta.days > 0:
        return f"{delta.days}d ago"
    if delta.seconds >= 3600:
        return f"{delta.seconds // 3600}h ago"
    if delta.seconds >= 60:
        return f"{delta.seconds // 60}m ago"
    return "just now"


def strip_markdown(text: str) -> str:
    """Remove markdown formatting and collapse whitespace."""
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'[-—=]{3,}', '', text)
    return re.sub(r'\s+', ' ', text).strip()


# --- Finalize Output Parsing ---


@dataclass
class FinalizeInfo:
    """Structured data parsed from commit/done result text."""
    commit_hash: str = ""
    commit_message: str = ""
    changelog_entries: list[str] = field(default_factory=list)
    version: str = ""  # e.g. "v0.3.6" or empty


_SUMMARY_BLOCK_RE = re.compile(
    r'```summary\s*\n(.*?)```', re.DOTALL,
)


def parse_finalize_output(text: str) -> FinalizeInfo | None:
    """Extract structured commit/changelog/version info from result text.

    Returns None if no summary block found.
    """
    m = _SUMMARY_BLOCK_RE.search(text)
    if not m:
        return None

    block = m.group(1)
    info = FinalizeInfo()
    in_changelog = False

    for line in block.splitlines():
        line = line.strip()
        if line.startswith("COMMIT:"):
            in_changelog = False
            rest = line[len("COMMIT:"):].strip()
            parts = rest.split(None, 1)
            if parts:
                info.commit_hash = parts[0]
                info.commit_message = parts[1] if len(parts) > 1 else ""
        elif line.startswith("CHANGELOG:"):
            in_changelog = True
        elif line.startswith("- ") and in_changelog:
            info.changelog_entries.append(line[2:].strip())
        elif line.startswith("VERSION:"):
            in_changelog = False
            ver = line[len("VERSION:"):].strip().strip('"')
            if ver.lower() != "none":
                info.version = ver

    return info if (info.commit_hash or info.changelog_entries) else None


def strip_summary_block(text: str) -> str:
    """Remove the ```summary``` block from result text."""
    return _SUMMARY_BLOCK_RE.sub('', text).rstrip()


# --- Relative Time ---


def format_relative_time(seconds: float) -> str:
    """Format seconds into a human-readable relative string like '3m' or '2h 14m'."""
    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m"
    elif seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m" if m else f"{h}h"
    else:
        d = int(seconds // 86400)
        h = int((seconds % 86400) // 3600)
        return f"{d}d {h}h" if h else f"{d}d"


# --- Status Icon ---

def status_icon(status: InstanceStatus) -> str:
    return {
        InstanceStatus.QUEUED: "⏳",
        InstanceStatus.RUNNING: "🔄",
        InstanceStatus.COMPLETED: "✅",
        InstanceStatus.FAILED: "❌",
        InstanceStatus.KILLED: "💀",
    }.get(status, "❓")


# --- Formatting Functions (markdown — platform adapters convert as needed) ---

def format_inline_meta_line(instance: Instance, session_loc: str) -> str:
    """One grey `-#` line of run facts for a result posted as plain messages.

    The embed path renders Duration / Tokens / Cost / Mode as inline fields.
    A plain message has no fields, so raising the inline ceiling would have
    quietly dropped all of them for every result between the old 2000-char
    threshold and the new one. Same facts, one line, no embed.
    """
    bits: list[str] = []
    dur = format_duration(instance.duration_ms)
    if dur:
        bits.append(dur)
    total_tokens = instance.input_tokens + instance.output_tokens
    if total_tokens:
        bits.append(format_tokens(total_tokens))
    if instance.cost_usd:
        bits.append(f"${instance.cost_usd:.4f}")
    bits.append(mode_name(instance.mode))
    if session_loc:
        bits.append(session_loc)
    return "-# " + " \u00b7 ".join(bits)


def _cut_index(text: str, budget: int) -> int:
    """Index to split *text* at, preferring a paragraph/line/word boundary.

    Only the back half of the budget is searched: a boundary near the start
    would technically be "nicer" but yields a near-empty piece and one more
    message than needed. Falls back to a hard cut at *budget* for text with no
    break in that window at all (a long token, a base64 blob).
    """
    for sep in ("\n\n", "\n", " "):
        cut = text.rfind(sep, budget // 2, budget)
        if cut > 0:
            return cut
    return budget


def _close_open_fence(text: str) -> str:
    """Append a closing ``` if *text* ends inside a code block.

    Any cut can land mid-fence, and an odd fence count doesn't just look wrong
    -- everything after it in the message renders as monospace, which for a
    preview card means the "more characters" line and the duration/cost line
    get swallowed into the code block.
    """
    if text.count("```") % 2:
        return text + "\n```"
    return text


def _leading_preview(text: str, budget: int) -> tuple[str, int]:
    """First *budget* chars of *text*, cut on a paragraph/line/word boundary.

    Returns (preview, chars_dropped). Unlike ``extract_summary`` this keeps
    MULTIPLE paragraphs -- the collapse card has to stand on its own, and one
    500-char paragraph out of a 4 KB answer does not.
    """
    text = text.strip()
    if len(text) <= budget:
        return _close_open_fence(text), 0
    cut = _cut_index(text, budget)
    return _close_open_fence(text[:cut].rstrip()), len(text) - cut


def format_result_md(instance: Instance, preview: str | None = None) -> str:
    """Format completed/failed instance result as markdown.

    *preview* is the full result text for a result too long to post inline.
    When given, the card carries its leading paragraphs (up to
    ``RESULT_PREVIEW_MAX``) plus how much is behind the Expand button,
    instead of the <=500-char first paragraph in ``instance.summary``.
    """
    parts = [f"**{instance.display_id()}**"]

    if instance.status == InstanceStatus.FAILED:
        error = redact_secrets(instance.error or 'Unknown error')
        parts.append(f"Failed: {error}")
    elif preview and preview.strip():
        cleaned = collapse_bot_directives(
            strip_verify_blocks(redact_secrets(preview)),
        )
        body, dropped = _leading_preview(cleaned, config.RESULT_PREVIEW_MAX)
        parts.append(body)
        if dropped:
            parts.append(f"-# +{dropped:,} more characters \u2014 Expand \u25bc")
    elif instance.summary:
        parts.append(redact_secrets(instance.summary))

    meta = []
    dur = format_duration(instance.duration_ms)
    if dur:
        meta.append(dur)
    meta.append(mode_name(instance.mode))
    if instance.chained_from:
        meta.append(f"stacked on {instance.chained_from}")
    if meta:
        parts.append(" | ".join(meta))

    return "\n".join(parts)


# Room held back on the final chunk for the "+N more characters" marker.
# Generous on purpose: the count and the instance id both vary in width.
_CUT_MARKER_ROOM = 96


def format_expanded_result_chunks(
    instance: Instance,
    result_text: str,
    first_budget: int = 3900,
    rest_budget: int = 1900,
    max_chunks: int | None = None,
) -> list[str]:
    """Full result text split for Expand: [edited embed, follow-up, ...].

    The old behaviour sliced the result at 3900 chars and appended
    "... truncated" -- so tapping Expand on a 12 KB answer still showed a
    third of it. Now the whole thing is posted: chunk 0 goes back into the
    result embed (4096 cap), the rest go out as plain messages (2000 cap).
    Only a result past ``max_chunks`` is cut, and then the marker says how
    much is missing rather than just "truncated".

    Strips leftover ```verify-board``` fences -- legacy markers a
    stale-context session may still emit, not content the user needs -- and
    collapses [BOT_CMD: ...] directives to one line each. This path reads the
    raw result FILE, so without the collapse a folded ~~~plan body would eat
    the whole budget. (`/log` still ships the file untouched.)
    """
    max_chunks = max(1, config.RESULT_EXPAND_MAX_CHUNKS
                     if max_chunks is None else max_chunks)
    header = f"**{instance.display_id()}**\n\n"
    text = collapse_bot_directives(strip_verify_blocks(redact_secrets(result_text))).strip()

    chunks: list[str] = []
    budget = first_budget - len(header)
    remaining = text
    while remaining and len(chunks) < max_chunks:
        if len(remaining) <= budget:
            chunks.append(remaining)
            remaining = ""
            break
        # On the last chunk we're allowed, hold back room for the marker that
        # says what's missing -- appending it afterwards would push the chunk
        # past the budget the caller sized its message for.
        limit = budget
        if len(chunks) == max_chunks - 1:
            limit = max(1, budget - _CUT_MARKER_ROOM)
        cut = _cut_index(remaining, limit)
        piece = remaining[:cut]
        remaining = remaining[cut:].lstrip('\n')
        # Keep code fences balanced across the split, and reopen on the far
        # side so the continuation still renders as code.
        if piece.count('```') % 2:
            piece += '\n```'
            remaining = '```\n' + remaining
        chunks.append(piece)
        budget = rest_budget

    if not chunks:
        chunks = [""]
    if remaining:
        chunks[-1] += (
            f"\n\n-# +{len(remaining):,} more characters \u2014 "
            f"/log {instance.id} for the full output"
        )

    chunks[0] = header + chunks[0]
    return chunks


def format_instance_list_md(instances: list[Instance]) -> str:
    """Format instance list with status indicators (markdown)."""
    if not instances:
        return "No instances found."

    lines = []
    for inst in instances:
        icon = status_icon(inst.status)
        name_part = f":{inst.name}" if inst.name else ""
        parent_part = f" ← {inst.parent_id}" if inst.parent_id else ""
        prompt_preview = inst.prompt[:40] + "..." if len(inst.prompt) > 40 else inst.prompt
        lines.append(
            f"{icon} `{inst.id}{name_part}{parent_part}` {prompt_preview}"
        )

    return "\n".join(lines)


def format_status_md(
    *,
    uptime_secs: float,
    running: int,
    instances_today: int,
    failures_today: int,
    total_instances: int,
    repos: dict[str, str],
    active_repo: str | None,
    context: str | None,
    schedule_count: int,
    cli_version: str,
    pc_name: str,
    platforms: list[str],
    recent: list[Instance] | None = None,
    accounts_line: str | None = None,
) -> str:
    """Format /status health dashboard (markdown)."""
    # Uptime
    h = int(uptime_secs // 3600)
    m = int((uptime_secs % 3600) // 60)
    uptime_str = f"{h}h {m}m" if h else f"{m}m"

    parts = [
        f"**{pc_name}** | up {uptime_str} | CLI {cli_version}",
        f"Platforms: {', '.join(platforms)}",
        "",
    ]

    # Activity
    fail_str = f" ({failures_today} failed)" if failures_today else ""
    parts.append(f"**Activity** — {instances_today} today{fail_str} | {total_instances} total | {running} running")

    # Accounts — only shown when failover is configured
    if accounts_line:
        parts.append(accounts_line)

    # Repos
    if repos:
        repo_lines = []
        for name, path in repos.items():
            marker = " (active)" if name == active_repo else ""
            repo_lines.append(f"  `{name}`{marker}")
        parts.append(f"**Repos** ({len(repos)})")
        parts.extend(repo_lines)

    # Schedules
    if schedule_count:
        parts.append(f"**Schedules**: {schedule_count} active")

    # Context
    if context:
        parts.append(f"**Context**: {context[:100]}")

    # Recent activity
    if recent:
        parts.append("")
        parts.append("**Recent**")
        for inst in recent[:5]:
            status_icon = {
                "completed": "+",
                "failed": "!",
                "running": ">",
                "killed": "x",
            }.get(inst.status.value, "?")
            dur = format_duration(inst.duration_ms)
            duration = f" {dur}" if dur else ""
            prompt_preview = inst.prompt[:40].replace("\n", " ")
            parts.append(f"  `{status_icon}` `{inst.id}` {prompt_preview}{duration}")

    return "\n".join(parts)


def format_schedule_list_md(schedules: list[Schedule]) -> str:
    """Format active schedules (markdown)."""
    if not schedules:
        return "No active schedules."

    lines = ["**Schedules**"]
    for s in schedules:
        interval = ""
        if s.interval_secs:
            if s.interval_secs >= 86400:
                interval = f"every {s.interval_secs // 86400}d"
            elif s.interval_secs >= 3600:
                interval = f"every {s.interval_secs // 3600}h"
            elif s.interval_secs >= 60:
                interval = f"every {s.interval_secs // 60}m"
            else:
                interval = f"every {s.interval_secs}s"
        elif s.run_at:
            interval = f"at {s.run_at}"

        # Thread-bound self-wake (resume_thread) — distinguish from user crons.
        if s.resume_thread:
            interval = f"wake → <#{s.channel_id}>" if s.channel_id else "wake"

        next_run = ""
        if s.next_run_at:
            next_run = f" next: {s.next_run_at[:16]}"

        prompt_preview = s.prompt[:40] + "..." if len(s.prompt) > 40 else s.prompt
        lines.append(
            f"  `{s.id}` {interval}{next_run}\n"
            f"    {prompt_preview}"
        )

    return "\n".join(lines)
