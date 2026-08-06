"""Platform-agnostic formatting: result, status, cost, digest, redaction, etc."""

from __future__ import annotations

import re
from datetime import timedelta

from dataclasses import dataclass, field

from bot import config
from bot.claude.types import CODE_CHANGE_TOOLS, PLAN_ORIGINS, Instance, InstanceOrigin, InstanceStatus, Schedule
from bot.platform.base import ButtonSpec


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
# Line-prefix parity with the dispatchers matters. Their guard skips lines
# starting with `>` / backtick / `#` (commands._QUOTED_LINE_PREFIX), so a
# quoted example never fires — and the `[ \t]*` prefix below likewise refuses
# to match those lines, leaving them verbatim. Display shows exactly the set of
# directives that will actually be acted on.
_BOT_CMD_DIRECTIVE_RE = re.compile(
    r"(?m)^[ \t]*\[BOT_CMD:\s*/(\w+)([^\]\n]*)\][ \t]*$"
)
# The tilde-fenced payload (~~~wake / ~~~spawn / ~~~plan). Matched from the end
# of the directive line, tolerating a blank line or two in between; a body
# further away than that belongs to prose, not this directive.
_BOT_CMD_BODY_RE = re.compile(
    r"\n(?:[ \t]*\n){0,2}[ \t]*~~~[a-zA-Z]*[ \t]*\n.*?\n[ \t]*~~~[ \t]*(?=\n|\Z)",
    re.DOTALL,
)
# kv pair: key=value, bare or quoted — mirrors commands._SPAWN_KV_RE.
_BOT_CMD_KV_RE = re.compile(r'''(\w+)=(?:"([^"]*)"|'([^']*)'|(\S+))''')
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
    kv = {
        k: (d or s or b or "")
        for k, d, s, b in _BOT_CMD_KV_RE.findall(args or "")
    }
    parts: list[str] = []

    if verb == "image":
        return None
    if verb == "wake":
        try:
            parts.append(f"in {format_delay_secs(int(kv.get('delay', '')))}")
        except ValueError:
            pass  # missing/garbage delay — the reason still carries the why
        if kv.get("reason"):
            parts.append(_chip_value(kv["reason"]))
    elif verb == "spawn":
        parts += [
            _chip_value(kv[k]) for k in ("repo", "mode", "effort") if kv.get(k)
        ]
        if kv.get("title"):
            parts.append(f'"{_chip_value(kv["title"])}"')
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
    for m in _BOT_CMD_DIRECTIVE_RE.finditer(text):
        if m.start() < pos:
            continue  # already swallowed as a previous directive's body
        out.append(text[pos:m.start()])
        chip = _render_directive_chip(m.group(1), m.group(2))
        if chip is not None:
            out.append(chip)
        body = _BOT_CMD_BODY_RE.match(text, m.end())
        pos = body.end() if body else m.end()
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
    return f"{round(secs / 3600, 1)} h"


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

# Anything sitting between the colon and the @ of a URL is, by definition of
# the userinfo field, a credential — so there is no length below which it is
# safe to print. This used to require 8+ characters, which let a short
# password through verbatim.
_CONN_STRING_PATTERN = re.compile(r'(://[^:\s]+:)([^@\s]+)(@)')

# A credential can also *be* the userinfo, with no password half at all:
# `https://<token>@github.com/o/r.git` is a form GitHub accepts, and a token
# in an unrecognised vendor format matches none of the patterns above. Length
# is what separates it from a real username, and the floor is set well clear
# of the longest ones actually in use — `git` (3), `oauth2` (6),
# `x-access-token` (14), `gitlab-ci-token` (15) — so every SSH remote in the
# bot's own output stays readable.
_URL_USERINFO_PATTERN = re.compile(r'(://)([^:/\s@]{20,})(@)')

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
    text = _URL_USERINFO_PATTERN.sub(r'\1[REDACTED]\3', text)
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
    InstanceOrigin.VERIFY, InstanceOrigin.VERIFY_RELEASE,
    InstanceOrigin.BUILD_AND_SHIP, InstanceOrigin.SENSOR_FIX,
})


def mode_name(mode: str) -> str:
    """Human-readable mode name."""
    return MODE_DISPLAY.get(mode, mode.capitalize())


def mode_label(mode: str) -> str:
    """Human-readable mode label (alias for mode_name)."""
    return mode_name(mode)


# --- Effort Display ---

EFFORT_DISPLAY: dict[str, str] = {
    "low":    "Low",
    "medium": "Medium",
    "high":   "High",
    "max":    "Max",
}

VALID_EFFORTS = frozenset(EFFORT_DISPLAY)


def effort_name(effort: str) -> str:
    """Human-readable effort name."""
    return EFFORT_DISPLAY.get(effort, effort.capitalize())


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

def merge_failed_button_specs(instance_id: str) -> list[list[ButtonSpec]]:
    """Buttons posted after an auto-merge failure — retry or discard.

    Posting actual buttons (instead of plain text) means the user's tap is
    routed through the existing merge/discard handlers rather than typing
    text into the thread, which Claude would otherwise reinterpret as a
    fresh review/build prompt.
    """
    return [[
        ButtonSpec("Resolve with Claude", f"resolve_merge:{instance_id}"),
        ButtonSpec("Try Merge Again", f"merge:{instance_id}"),
        ButtonSpec("Discard", f"discard:{instance_id}"),
    ]]


def merge_failed_banner(failure_kind: str | None) -> str:
    """Compose the user-facing banner for a failed auto-merge.

    Branches on the ``MERGE_FAIL_*`` taxonomy from ``bot/claude/runner.py``
    (kept as string constants so this module can stay platform-agnostic
    without importing runner). Empty / unknown kinds fall back to the
    generic conflict copy — same as before the t-4114 work landed.
    """
    if failure_kind == "orphaned_index":
        return (
            "Auto-merge failed because the main repo has an orphaned "
            "merge in its index. Tap **Try Merge Again** — the next "
            "attempt will detect and auto-recover the leftover state "
            "(any local changes are preserved in a stash). **Resolve "
            "with Claude** also works. **Discard** drops the branch."
        )
    if failure_kind == "recovery_failed":
        return (
            "Auto-merge failed and automatic recovery couldn't unstick "
            "the main repo. **Manual intervention needed** — open a "
            "terminal in the repo and run `git reset --merge` or "
            "`git status` to inspect. Any work the bot preserved is "
            "recorded in a labeled stash (see the failure detail above). "
            "Once unstuck, tap **Try Merge Again**."
        )
    if failure_kind == "diverged":
        # Deliberately does NOT suggest Resolve with Claude — the resolver
        # fixes branch-vs-master conflict markers inside the worktree and
        # never touches local-vs-origin divergence, so it can't help here.
        return (
            "Auto-merge stopped before merging: local master and "
            "origin have diverged (commits exist on both sides — see the "
            "counts above). Nothing was changed. Run `git pull --rebase` "
            "in the main repo to reconcile, then tap **Try Merge Again**."
        )
    return (
        "Auto-merge failed. Tap **Try Merge Again** to retry "
        "(useful if a parallel build just completed) or **Discard** "
        "to drop the branch. Or describe the situation here to talk "
        "it through with Claude."
    )


def resolver_running_button_specs(instance_id: str) -> list[list[ButtonSpec]]:
    """Buttons shown while the merge-conflict resolver is in flight.

    Only Cancel — both Merge and Discard are blocked until the resolver
    finishes or is cancelled, since either action would race the worktree
    state Claude is actively editing.
    """
    return [[
        ButtonSpec("Cancel", f"resolve_cancel:{instance_id}"),
    ]]


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

    # Resolver origin: no buttons on the resolver's own result. The resolve_merge
    # handler posts a follow-up message with the appropriate Merge/Retry/Discard
    # row once verification + post-merge has run.
    if instance.origin == InstanceOrigin.RESOLVE_MERGE:
        return rows

    # Done origin: if branch is pending merge and no autopilot, show Merge/Discard
    if instance.origin == InstanceOrigin.DONE and instance.status == InstanceStatus.COMPLETED:
        if instance.branch and not has_autopilot_chain:
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
            if has_autopilot_chain:
                # Autopilot handles merge — show review/action buttons plus a
                # Discard escape hatch (user may inspect the Diff and decide to
                # bail out of the branch even mid-chain).
                rows.append([
                    ButtonSpec("Diff", f"diff:{iid}"),
                    ButtonSpec("Review Code", f"review_code:{iid}"),
                    ButtonSpec("Commit", f"commit:{iid}"),
                    ButtonSpec("Done", f"done:{iid}"),
                    ButtonSpec("Discard", f"discard:{iid}"),
                ])
            else:
                # Manual build — keep full merge workflow
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
                # Plan created or revisions applied. Hide Autopilot starters
                # when a chain is already paused — Continue Autopilot below
                # is the correct resumption path.
                if not has_autopilot_chain:
                    rows.append([
                        ButtonSpec("Autopilot", f"autopilot:{iid}"),
                        ButtonSpec("Autopilot (Hold)", f"autopilot_hold:{iid}"),
                    ])
                rows.append([
                    ButtonSpec("Review Plan", f"review_plan:{iid}"),
                    ButtonSpec("Build & Ship", f"build_and_ship:{iid}"),
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
            # instance didn't do anything code-related — offer plan actions.
            # Skip Autopilot starters when a chain is already paused.
            if not has_autopilot_chain:
                rows.append([
                    ButtonSpec("Autopilot", f"autopilot:{iid}"),
                    ButtonSpec("Autopilot (Hold)", f"autopilot_hold:{iid}"),
                ])
            rows.append([
                ButtonSpec("Review Plan", f"review_plan:{iid}"),
                ButtonSpec("Build & Ship", f"build_and_ship:{iid}"),
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
                    ButtonSpec("Build & Ship", f"build_and_ship:{iid}"),
                    ButtonSpec("Done", f"done:{iid}"),
                ])

    elif instance.status in (InstanceStatus.RUNNING, InstanceStatus.QUEUED):
        rows.append([ButtonSpec("Kill", f"kill:{iid}")])

    elif instance.status == InstanceStatus.FAILED:
        if instance.cooldown_retry_at:
            row = [ButtonSpec("Cancel Auto-Retry", f"cancel_cooldown:{iid}")]
            if config.API_FALLBACK_ENABLED:
                cap = config.API_FALLBACK_MAX_USD
                row.insert(0, ButtonSpec(
                    f"Continue with {config.API_FALLBACK_MODEL} (≤${cap:.2f})",
                    f"continue_ppu:{iid}",
                ))
            rows.append(row)
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

    # Branch from here — fork the session at the message this button is on.
    # Gated on session_id only; the click handler falls back to the last
    # assistant uuid in the JSONL when the per-message map has no entry
    # (e.g. the first render, before send_result stamps message ids).
    # Reserve a row for Expand when show_expand=True so long-result messages
    # keep their Expand button even with a crowded set of action buttons.
    # Branch+Share row takes priority; when it fires, omit Share from the
    # Expand row below to avoid showing two Share buttons on the same message.
    branch_cap = 4 if show_expand else 5
    share_added = False
    if (instance.status == InstanceStatus.COMPLETED
            and instance.session_id
            and len(rows) < branch_cap):
        rows.append([
            ButtonSpec("Branch", f"branch:{iid}"),
            ButtonSpec("Share", f"share:{iid}"),
        ])
        share_added = True

    if show_expand:
        expand_row = [
            ButtonSpec("Expand \u25bc", f"expand:{iid}"),
            ButtonSpec("Full Log", f"log:{iid}"),
        ]
        if instance.session_id and not share_added:
            expand_row.append(ButtonSpec("Share", f"share:{iid}"))
        rows.append(expand_row)

    return rows


def expanded_button_specs(instance: Instance) -> list[list[ButtonSpec]]:
    """Action buttons + Collapse for expanded view."""
    rows = action_button_specs(instance)
    collapse_row = [
        ButtonSpec("Collapse \u25b2", f"collapse:{instance.id}"),
        ButtonSpec("Full Log", f"log:{instance.id}"),
    ]
    # Share fallback: skip if action_button_specs already placed it (Branch row)
    already_has_share = any(
        b.callback_data.startswith("share:") for row in rows for b in row
    )
    if instance.session_id and not already_has_share:
        collapse_row.append(ButtonSpec("Share", f"share:{instance.id}"))
    rows.append(collapse_row)
    return rows


def running_button_specs(instance_id: str) -> list[list[ButtonSpec]]:
    """Stop button shown on progress messages while an instance is running."""
    return [[ButtonSpec("Stop", f"kill:{instance_id}")]]


def stall_button_specs(instance_id: str) -> list[list[ButtonSpec]]:
    return [[
        ButtonSpec("Kill", f"kill:{instance_id}"),
        ButtonSpec("Wait", f"wait:{instance_id}"),
    ]]


def queued_button_specs(
    pending_id: str, supports_steer: bool,
) -> list[list[ButtonSpec]]:
    """Buttons on a 'Queued' message while a run holds the channel lock.

    Steer button is omitted when the provider can't resume a killed session.
    """
    row: list[ButtonSpec] = []
    if supports_steer:
        row.append(ButtonSpec("Steer Now", f"steer:{pending_id}"))
    row.append(ButtonSpec("Cancel", f"cancel_pending:{pending_id}"))
    return [row]


# --- Formatting Functions (markdown — platform adapters convert as needed) ---

def format_result_md(instance: Instance) -> str:
    """Format completed/failed instance result as markdown."""
    parts = [f"**{instance.display_id()}**"]

    if instance.status == InstanceStatus.FAILED:
        error = redact_secrets(instance.error or 'Unknown error')
        parts.append(f"Failed: {error}")
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


def format_expanded_result_md(instance: Instance, result_text: str, budget: int = 3900) -> str:
    """Format full result text for expanded view, truncated to budget.

    Strips leftover ```verify-board``` fences — legacy markers a
    stale-context session may still emit, not content the user needs — and
    collapses [BOT_CMD: ...] directives to one line each. This path reads the
    raw result FILE, so without the collapse a folded ~~~plan body would eat
    the whole budget and truncate the answer the user tapped Expand to see.
    (`/log` still ships the file untouched — that's the full-fidelity copy.)
    """
    header = f"**{instance.display_id()}**\n\n"
    text = collapse_bot_directives(strip_verify_blocks(redact_secrets(result_text)))

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
