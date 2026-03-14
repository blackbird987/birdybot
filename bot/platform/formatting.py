"""Platform-agnostic formatting: result, status, cost, digest, redaction, etc."""

from __future__ import annotations

import re

from bot.claude.types import CODE_CHANGE_TOOLS, Instance, InstanceOrigin, InstanceStatus, Schedule
from bot.platform.base import ButtonSpec


# --- Shared Helpers ---


def format_duration(ms: int | float | None) -> str:
    """Format duration in milliseconds to a human-readable string."""
    if ms is None:
        return ""
    secs = ms / 1000
    if secs >= 60:
        return f"{secs / 60:.1f}m"
    return f"{secs:.0f}s"


# --- Secret Redaction ---

# Well-known token prefixes (match standalone, no key name needed)
_TOKEN_PATTERNS = [
    re.compile(r'sk-ant-[a-zA-Z0-9_-]{20,}'),
    re.compile(r'sk-[a-zA-Z0-9]{20,}'),
    re.compile(r'gh[pos]_[a-zA-Z0-9]{20,}'),
    re.compile(r'github_pat_[a-zA-Z0-9_]{20,}'),
    re.compile(r'AKIA[A-Z0-9]{16}'),
    re.compile(r'eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}'),
    re.compile(r'0x[0-9a-fA-F]{64}\b'),
    re.compile(r'(?i)Bearer\s+[a-zA-Z0-9_./-]{20,}'),
]

_CONN_STRING_PATTERN = re.compile(r'(://[^:\s]+:)([^@\s]{8,})(@)')

_SECRET_KEY_WORDS = (
    r'password|passwd|secret|mnemonic|private[_-]?key|seed[_-]?phrase|'
    r'api[_-]?key|access[_-]?key|auth[_-]?(?:key|token|secret)|'
    r'hmac|jwt|credential|client[_-]?secret|app[_-]?secret|'
    r'signing[_-]?key|encryption[_-]?key|master[_-]?key|'
    r'db[_-]?password|connection[_-]?string|'
    r'pinata|infura|alchemy|token'
)

_KV_PATTERN = re.compile(
    r'(?i)'
    r'(?:^|(?<=[\s"\'`]))'
    r'((?=\w*(?:' + _SECRET_KEY_WORDS + r'))'
    r'[a-zA-Z_]\w*)'
    r'["\']?'
    r'\s*[=:]\s*'
    r'["\']?'
    r'(.+?)'
    r'["\']?'
    r'(?:[,;\s]|$)',
    re.MULTILINE,
)

_MNEMONIC_PATTERN = re.compile(
    r'(?i)(mnemonic|seed[_-]?phrase|recovery[_-]?phrase)\s*[=:"\']*\s*'
    r'([a-z]+(?:\s+[a-z]+){11,})',
)

_HEX_KEY_PATTERN = re.compile(r'(?<![a-zA-Z0-9])[0-9a-fA-F]{64,}(?![a-zA-Z0-9])')


def redact_secrets(text: str) -> str:
    """Scrub API keys, tokens, and secrets from text."""
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    text = _CONN_STRING_PATTERN.sub(r'\1[REDACTED]\3', text)
    text = _MNEMONIC_PATTERN.sub(lambda m: m.group(1) + '=[REDACTED]', text)
    text = _KV_PATTERN.sub(lambda m: f'{m.group(1)}=[REDACTED] ', text)
    text = _HEX_KEY_PATTERN.sub('[REDACTED]', text)
    return text


def strip_markdown(text: str) -> str:
    """Remove markdown formatting and collapse whitespace."""
    text = re.sub(r'#{1,6}\s*', '', text)
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'[-—=]{3,}', '', text)
    return re.sub(r'\s+', ' ', text).strip()


# --- Status Icon ---

def status_icon(status: InstanceStatus) -> str:
    return {
        InstanceStatus.QUEUED: "⏳",
        InstanceStatus.RUNNING: "🔄",
        InstanceStatus.COMPLETED: "✅",
        InstanceStatus.FAILED: "❌",
        InstanceStatus.KILLED: "💀",
    }.get(status, "❓")


# --- Button Specs (platform-agnostic) ---

def action_button_specs(
    instance: Instance, show_expand: bool = False,
) -> list[list[ButtonSpec]]:
    """Return button row specs based on instance status and origin."""
    rows: list[list[ButtonSpec]] = []
    iid = instance.id

    # Done origin is terminal on success — no further actions (thread is closing)
    if instance.origin == InstanceOrigin.DONE and instance.status == InstanceStatus.COMPLETED:
        return rows

    if instance.status == InstanceStatus.COMPLETED:
        tools = set(instance.tools_used or [])
        made_code_changes = bool(tools & CODE_CHANGE_TOOLS)
        made_plan = instance.plan_active

        if instance.branch:
            # Build bg task with branch — full merge workflow
            rows.append([
                ButtonSpec("Diff", f"diff:{iid}"),
                ButtonSpec("Merge", f"merge:{iid}"),
                ButtonSpec("Discard", f"discard:{iid}"),
            ])
            rows.append([
                ButtonSpec("Review Code", f"review_code:{iid}"),
                ButtonSpec("Commit", f"commit:{iid}"),
                ButtonSpec("Done", f"done:{iid}"),
            ])
        elif made_code_changes:
            # Edited/wrote files in-place (no branch)
            rows.append([
                ButtonSpec("Review Code", f"review_code:{iid}"),
                ButtonSpec("Retry", f"retry:{iid}"),
                ButtonSpec("Done", f"done:{iid}"),
            ])
        elif instance.code_active:
            # Session has uncommitted code changes — offer commit/review
            rows.append([
                ButtonSpec("Commit", f"commit:{iid}"),
                ButtonSpec("Review Code", f"review_code:{iid}"),
                ButtonSpec("Done", f"done:{iid}"),
            ])
        elif made_plan:
            # Session has an active plan
            rows.append([
                ButtonSpec("Review Plan", f"review_plan:{iid}"),
                ButtonSpec("Build It", f"build:{iid}"),
                ButtonSpec("Done", f"done:{iid}"),
            ])
        else:
            # Default buttons + workflow row when session exists
            rows.append([
                ButtonSpec("New", f"new:{iid}"),
                ButtonSpec("Retry", f"retry:{iid}"),
            ])
            if instance.session_id:
                rows.append([
                    ButtonSpec("Plan", f"plan:{iid}"),
                    ButtonSpec("Build", f"build:{iid}"),
                    ButtonSpec("Done", f"done:{iid}"),
                ])

    elif instance.status in (InstanceStatus.RUNNING, InstanceStatus.QUEUED):
        rows.append([ButtonSpec("Kill", f"kill:{iid}")])

    elif instance.status == InstanceStatus.FAILED:
        rows.append([
            ButtonSpec("Retry", f"retry:{iid}"),
            ButtonSpec("Log", f"log:{iid}"),
        ])

    elif instance.status == InstanceStatus.KILLED:
        rows.append([ButtonSpec("Retry", f"retry:{iid}")])

    if show_expand:
        rows.append([ButtonSpec("Expand \u25bc", f"expand:{iid}")])

    return rows


def expanded_button_specs(instance: Instance) -> list[list[ButtonSpec]]:
    """Action buttons + Collapse for expanded view."""
    rows = action_button_specs(instance)
    rows.append([ButtonSpec("Collapse \u25b2", f"collapse:{instance.id}")])
    return rows


def stall_button_specs(instance_id: str) -> list[list[ButtonSpec]]:
    return [[
        ButtonSpec("Kill", f"kill:{instance_id}"),
        ButtonSpec("Wait", f"wait:{instance_id}"),
    ]]


# --- Formatting Functions (markdown — platform adapters convert as needed) ---

def format_result_md(instance: Instance) -> str:
    """Format completed/failed instance result as markdown."""
    parts = [f"**{instance.display_id()}**"]

    if instance.status == InstanceStatus.FAILED:
        error = redact_secrets(instance.error or 'Unknown error')
        parts.append(f"FAILED: {error}")
    elif instance.summary:
        parts.append(redact_secrets(instance.summary))

    meta = []
    dur = format_duration(instance.duration_ms)
    if dur:
        meta.append(dur)
    if instance.mode == "build":
        meta.append("build")
    if meta:
        parts.append(" | ".join(meta))

    return "\n".join(parts)


def format_expanded_result_md(instance: Instance, result_text: str, budget: int = 3800) -> str:
    """Format full result text for expanded view, truncated to budget."""
    header = f"**{instance.display_id()}**\n\n"
    text = redact_secrets(result_text)

    if len(text) > budget:
        cut = text.rfind('\n', 0, budget)
        if cut <= 0:
            cut = text.rfind(' ', 0, budget)
        if cut <= 0:
            cut = budget
        text = text[:cut]
        text += f"\n\n*... truncated — use /log {instance.id} for full output*"

    return header + text


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


def format_cost_md(daily: float, total: float, top_spenders: list[Instance]) -> str:
    """Format /cost breakdown (markdown)."""
    lines = [
        "**Cost**",
        f"Today: ${daily:.4f}",
        f"Total: ${total:.4f}",
    ]
    if top_spenders:
        lines.append("\n**Top spenders today:**")
        for inst in top_spenders:
            cost = f"${inst.cost_usd:.4f}" if inst.cost_usd else "$0"
            lines.append(f"  `{inst.id}` {cost} — {inst.prompt[:30]}")
    return "\n".join(lines)


def format_digest_md(
    instance_count: int,
    daily_cost: float,
    failures: int,
    repo_name: str | None,
    mode: str,
) -> str:
    """Format daily digest (markdown)."""
    lines = [
        "**Daily Digest**",
        f"Instances: {instance_count}",
        f"Cost: ${daily_cost:.4f}",
        f"Failures: {failures}",
    ]
    if repo_name:
        lines.append(f"Repo: `{repo_name}`")
    lines.append(f"Mode: `{mode}`")
    return "\n".join(lines)


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

        next_run = ""
        if s.next_run_at:
            next_run = f" next: {s.next_run_at[:16]}"

        prompt_preview = s.prompt[:40] + "..." if len(s.prompt) > 40 else s.prompt
        lines.append(
            f"  `{s.id}` {interval}{next_run}\n"
            f"    {prompt_preview}"
        )

    return "\n".join(lines)
