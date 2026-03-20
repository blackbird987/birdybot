"""Platform-agnostic formatting: result, status, cost, digest, redaction, etc."""

from __future__ import annotations

import re
from datetime import timedelta

from dataclasses import dataclass, field

from bot.claude.types import CODE_CHANGE_TOOLS, PLAN_ORIGINS, Instance, InstanceOrigin, InstanceStatus, Schedule
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


def format_tokens(count: int) -> str:
    """Format token count to a compact human-readable string (e.g. 48.2k, 1.3M)."""
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def format_age(delta: timedelta) -> str:
    """Format a timedelta as a human-readable age string (e.g. '3h ago')."""
    if delta.days > 0:
        return f"{delta.days}d ago"
    if delta.seconds >= 3600:
        return f"{delta.seconds // 3600}h ago"
    if delta.seconds >= 60:
        return f"{delta.seconds // 60}m ago"
    return "just now"


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


# --- Mode Display ---

MODE_DISPLAY: dict[str, str] = {
    "explore": "Explore",
    "plan":    "Plan",
    "build":   "Build",
}

# Embed sidebar colors per mode
MODE_COLOR: dict[str, int] = {
    "explore": 0x95a5a6,  # gray
    "plan":    0x5865F2,  # blurple/blue
    "build":   0x57F287,  # green
}

VALID_MODES = frozenset(MODE_DISPLAY)

# Mode cycle order for the toggle button
_NEXT_MODE: dict[str, str] = {"explore": "plan", "plan": "build", "build": "explore"}

# Origins where mode toggle button should NOT appear (user is in a workflow)
_WORKFLOW_ORIGINS = frozenset({
    InstanceOrigin.PLAN, InstanceOrigin.BUILD,
    InstanceOrigin.REVIEW_PLAN, InstanceOrigin.REVIEW_CODE,
    InstanceOrigin.COMMIT, InstanceOrigin.DONE,
    InstanceOrigin.APPLY_REVISIONS, InstanceOrigin.RELEASE,
    InstanceOrigin.AUTOPILOT, InstanceOrigin.BUILD_AND_SHIP,
})


def mode_name(mode: str) -> str:
    """Human-readable mode name."""
    return MODE_DISPLAY.get(mode, mode.capitalize())


def mode_label(mode: str) -> str:
    """Human-readable mode label (alias for mode_name)."""
    return mode_name(mode)


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
    has_autopilot_chain: bool = False,
) -> list[list[ButtonSpec]]:
    """Return button row specs based on instance status and origin.

    has_autopilot_chain: if True, this session has a paused autopilot chain
    that can be resumed via Continue Autopilot.
    """
    rows: list[list[ButtonSpec]] = []
    iid = instance.id

    # Done origin: if branch is pending merge, show Merge/Discard; otherwise terminal
    if instance.origin == InstanceOrigin.DONE and instance.status == InstanceStatus.COMPLETED:
        if instance.branch:
            rows.append([
                ButtonSpec("Merge", f"merge:{iid}"),
                ButtonSpec("Discard", f"discard:{iid}"),
            ])
        return rows

    if instance.status == InstanceStatus.COMPLETED:
        tools = set(instance.tools_used or [])
        made_code_changes = bool(tools & CODE_CHANGE_TOOLS)
        # this_planned: THIS instance produced/dealt with a plan
        this_planned = bool(
            {"EnterPlanMode", "ExitPlanMode"} & tools
            or instance.origin in PLAN_ORIGINS
            or instance.mode == "plan"
        )
        # session_has_plan: inherited plan_active from any sibling
        session_has_plan = instance.plan_active

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
        elif this_planned:
            # This instance directly produced or reviewed a plan
            if instance.origin == InstanceOrigin.REVIEW_PLAN:
                # Just reviewed — offer to apply or ship
                rows.append([
                    ButtonSpec("Apply Revisions", f"apply_revisions:{iid}"),
                    ButtonSpec("Build & Ship", f"build_and_ship:{iid}"),
                    ButtonSpec("Done", f"done:{iid}"),
                ])
            else:
                # Plan created or revisions applied
                rows.append([
                    ButtonSpec("Autopilot", f"autopilot:{iid}"),
                    ButtonSpec("Review Plan", f"review_plan:{iid}"),
                    ButtonSpec("Build It", f"build:{iid}"),
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
        elif session_has_plan:
            # Fallback: session has a plan from a prior instance, and this
            # instance didn't do anything code-related — offer plan actions
            rows.append([
                ButtonSpec("Autopilot", f"autopilot:{iid}"),
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
        if instance.cooldown_retry_at:
            rows.append([ButtonSpec("Cancel Auto-Retry", f"cancel_cooldown:{iid}")])
        else:
            rows.append([
                ButtonSpec("Retry", f"retry:{iid}"),
                ButtonSpec("Log", f"log:{iid}"),
            ])

    elif instance.status == InstanceStatus.KILLED:
        rows.append([ButtonSpec("Retry", f"retry:{iid}")])

    # Continue Autopilot — shown when session has a paused chain
    if has_autopilot_chain and instance.status == InstanceStatus.COMPLETED:
        rows.append([ButtonSpec("Continue Autopilot", f"continue_autopilot:{iid}")])

    # Mode toggle — only on non-workflow completions
    if (instance.status == InstanceStatus.COMPLETED
            and instance.origin not in _WORKFLOW_ORIGINS):
        target = _NEXT_MODE.get(instance.mode, "explore")
        label = mode_name(target)
        rows.append([ButtonSpec(f"Mode: {label}", f"mode_{target}:{iid}")])

    if show_expand:
        rows.append([ButtonSpec("Expand \u25bc", f"expand:{iid}")])

    return rows


def expanded_button_specs(instance: Instance) -> list[list[ButtonSpec]]:
    """Action buttons + Collapse for expanded view."""
    rows = action_button_specs(instance)
    rows.append([ButtonSpec("Collapse \u25b2", f"collapse:{instance.id}")])
    return rows


def running_button_specs(instance_id: str) -> list[list[ButtonSpec]]:
    """Stop button shown on progress messages while an instance is running."""
    return [[ButtonSpec("Stop", f"kill:{instance_id}")]]


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
    meta.append(mode_name(instance.mode))
    if meta:
        parts.append(" | ".join(meta))

    return "\n".join(parts)


def format_expanded_result_md(instance: Instance, result_text: str, budget: int = 3900) -> str:
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
