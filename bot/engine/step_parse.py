"""Reading a finished step's output: plans, review status, deferred items, phase plans, verify verdicts."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal

from bot.textutil import parse_iso_utc
from bot import config
from bot.claude.types import (
    Instance, InstanceOrigin,
    InstanceStatus, PHASE_GATES, Phase,
)
from bot.engine import prior_art
from bot.platform.base import RequestContext

log = logging.getLogger(__name__)


# --- Helpers ---

_FENCE_LINE_RE = re.compile(r"^\s*```.*$", re.MULTILINE)


def _stop_reason_snippet(summary: str | None, max_chars: int = 280) -> str | None:
    """First paragraph of an agent's final message, trimmed for chat embeds.

    Fence lines (```...) are stripped before paragraph splitting so the
    `> `-prefix quoting in `_build_halted_text` can't break Discord's
    code-fence rendering.
    """
    if not summary or not summary.strip():
        return None
    cleaned = _FENCE_LINE_RE.sub("", summary).strip()
    if not cleaned:
        return None
    para = cleaned.split("\n\n", 1)[0].strip()
    if len(para) > max_chars:
        para = para[:max_chars].rsplit(" ", 1)[0] + "…"
    return para or None


_REVIEW_STATUS_RE = re.compile(r'```review-status\s*\n(.*?)```', re.DOTALL)
_HIGH_PRIO_RE = re.compile(r'(?:Critical|High)\s*[·|]', re.IGNORECASE)
_TRIAGE_RESULT_RE = re.compile(r'```triage-result\s*\n(.*?)```', re.DOTALL)

# Module-level so the halt message in the autopilot chain can reference the
# same value the loop guard uses — kills drift if the cap ever changes.
_REVIEW_LOOP_MAX_ROUNDS = 5

# Cap injected plan text so a runaway plan can't blow the prompt budget.
# 8000 chars ≈ 2000 tokens — comfortably within budget, large enough to
# carry the full plans we've seen in practice (largest observed ~5500).
_BUILD_PLAN_INJECT_MAX = 8000

# Room for the prior-history block a /chain plan carries, on top of the plan's
# own cap: the block's own cap plus its closing instruction and some slack.
_PRIOR_ART_INJECT_MAX = config.PRIOR_ART_MAX_CHARS + 600

# Headings that mark trailing review-metadata sections in APPLY_REVISIONS /
# TRIAGE result text. Stripped before injection so the build agent doesn't
# treat "applied/skipped" log lines as plan items to implement.
_PLAN_METADATA_MARKERS = ("\n### Applied", "\n### Triaged")


def _extract_latest_plan_text(
    ctx: RequestContext,
    chain_instances: list[Instance],
    source_id: str,
) -> str:
    """Return the most recent revised plan text, falling back to the original.

    Walks chain_instances backward looking for an APPLY_REVISIONS instance
    (whose result_text is the revised plan — both the apply and triage
    spawns share that origin and both output a full revised plan + a
    trailing metadata block). REVIEW_PLAN instances are intentionally
    skipped — their result_text is the revisions list, not the plan.

    Relies on `_review_plan_loop` appending intermediate APPLY_REVISIONS
    to chain_instances; without that, the loop's final return (often a
    REVIEW_PLAN on the converged-no-deferred path) would be the only
    revision-related instance visible and the helper would miss real
    revisions from earlier rounds.

    Falls back to the chain's source instance when its origin is PLAN
    (the original plan instance whose result_text IS a plan).

    When chain_instances is empty AND source isn't PLAN-origin, falls
    back further to a store-backed lookup: scans `list_instances(all_=False)`
    (24h window) for the most recent APPLY_REVISIONS or PLAN instance
    matching `source.session_id` with `created_at <= source.created_at`.
    This catches the broken-chain case (user posted a non-button message
    between Plan and Build, or a prior Build halted with no commits and
    cleared the chain) where chain_instances is empty but a real plan
    still lives in the store. The 24h cutoff in `list_instances` is the
    soft recency cap that keeps unrelated old plans from same-session
    threads from being resurrected; chains paused longer than 24h won't
    get the fallback (degrading to today's no-injection behavior, not to
    wrong injection). created_at is parsed via `parse_iso_utc`
    rather than lexicographic string compare, since the codebase isn't
    audited to guarantee uniform ISO format across all writers; naive
    timestamps are coerced to UTC before comparison so a stray naive
    writer can't cause a tz-aware/naive TypeError mid-Build.

    Strips trailing "### Applied" / "### Triaged" review-metadata blocks
    before returning, so the injected text is plan-only.

    Returns "" if no usable plan text is available — call sites should
    skip the inject prefix in that case (`if plan_text:`).

    Highest priority: a conversational-chain plan override. When the user
    approves scoped work mid-conversation, the agent writes the plan and
    hands it to a `/chain` directive; that text is stored per session and
    read here so a chain launched outside the formal Plan button still
    injects a real plan. Cleared when the chain finishes.
    """
    override_source = ctx.store.get_instance(source_id)
    if override_source and override_source.session_id:
        override = ctx.store.get_chain_plan_override(override_source.session_id)
        if override:
            # The /chain handler may have attached the plan's git history
            # after it. Split it off first: the plan half is the only part
            # the metadata strip and the cap are about, and a long plan must
            # not truncate the history (and its "ask before re-adding"
            # instruction) off the end of the brief.
            override, attached = prior_art.split_attached(override)
            for marker in _PLAN_METADATA_MARKERS:
                idx = override.rfind(marker)
                if idx != -1:
                    override = override[:idx].rstrip()
            override = override[:_BUILD_PLAN_INJECT_MAX]
            if attached:
                override += "\n\n" + attached[:_PRIOR_ART_INJECT_MAX]
            return override
    raw = ""
    for inst in reversed(chain_instances):
        if inst.origin == InstanceOrigin.APPLY_REVISIONS:
            text = inst.read_result_text()
            if text:
                raw = text
                break
    source = ctx.store.get_instance(source_id) if not raw else None
    if not raw:
        if source and source.origin == InstanceOrigin.PLAN:
            raw = source.read_result_text() or ""
    if not raw and source and source.session_id and source.created_at:
        # parse_iso_utc reads a naive stamp as UTC, so a stray naive writer
        # cannot raise an aware/naive TypeError here and halt Build mid-flight.
        source_dt = parse_iso_utc(source.created_at)
        if source_dt is not None:
            recent = ctx.store.list_instances(all_=False)
            candidates: list[Instance] = []
            for inst in recent:
                if inst.session_id != source.session_id:
                    continue
                inst_dt = parse_iso_utc(inst.created_at)
                if inst_dt is None:
                    continue
                if inst_dt > source_dt:
                    continue
                candidates.append(inst)
            for inst in candidates:
                if inst.origin == InstanceOrigin.APPLY_REVISIONS:
                    text = inst.read_result_text()
                    if text:
                        raw = text
                        break
            if not raw:
                for inst in candidates:
                    if inst.origin == InstanceOrigin.PLAN:
                        text = inst.read_result_text()
                        if text:
                            raw = text
                            break
    if not raw:
        return ""
    # rfind, not find: a plan body can legitimately use "### Applied" as
    # a section header (e.g. "### Applied migrations"). The metadata block
    # is always at the END of the agent's output, so the LAST occurrence
    # is the right cut point. find() would strip at the body header and
    # silently drop the rest of the plan + the actual metadata.
    for marker in _PLAN_METADATA_MARKERS:
        idx = raw.rfind(marker)
        if idx != -1:
            raw = raw[:idx].rstrip()
    return raw[:_BUILD_PLAN_INJECT_MAX]


def _needs_revision(inst: Instance) -> bool:
    """Check if a plan review found Critical/High revisions.

    Parses the review-status block first; falls back to regex on prose.
    """
    if not inst.result_file:
        return False
    try:
        text = Path(inst.result_file).read_text(encoding="utf-8")
    except Exception:
        return False
    m = _REVIEW_STATUS_RE.search(text)
    if m:
        return "needs_revision: yes" in m.group(1).lower()
    # Fallback: scan prose for Critical/High markers
    return bool(_HIGH_PRIO_RE.search(text))


def _parse_deferred_block(inst: Instance, pattern: re.Pattern[str]) -> list[str]:
    """Parse DEFERRED items from a structured block matching *pattern*."""
    if not inst.result_file:
        return []
    try:
        text = Path(inst.result_file).read_text(encoding="utf-8")
    except Exception:
        return []
    m = pattern.search(text)
    if not m:
        return []
    lines: list[str] = []
    in_deferred = False
    for line in m.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("DEFERRED:"):
            in_deferred = True
        elif stripped.startswith("- ") and in_deferred:
            lines.append(stripped[2:])
        elif stripped and not stripped.startswith("-"):
            in_deferred = False
    return lines


def _extract_deferred(inst: Instance) -> list[str]:
    """Extract deferred revision items from review-status block."""
    return _parse_deferred_block(inst, _REVIEW_STATUS_RE)


def _extract_triage_deferred(inst: Instance) -> list[str]:
    """Extract still-deferred items after LLM triage."""
    return _parse_deferred_block(inst, _TRIAGE_RESULT_RE)


# --- Phase plan parsing & gate helpers ---

_PHASE_PLAN_RE = re.compile(r'```phase-plan\s*\n(.*?)```', re.DOTALL)


def _extract_phase_plan(inst: Instance | None) -> list[Phase]:
    """Parse a `phase-plan` fenced block from an instance's result file.

    Each line inside the block looks like:
      `- id: <slug> | title: <title> | gate: mechanical|design|risk [| reason: <text>]`
    Lines with missing/invalid id or gate are silently skipped.
    Returns [] if the block is absent or malformed.
    """
    if not inst or not inst.result_file:
        return []
    try:
        text = Path(inst.result_file).read_text(encoding="utf-8")
    except OSError:
        return []
    # Take the LAST block — if the plan was revised, the most recent
    # phase-plan supersedes earlier drafts in the same result file.
    blocks = _PHASE_PLAN_RE.findall(text)
    if not blocks:
        return []
    phases: list[Phase] = []
    for raw in blocks[-1].splitlines():
        line = raw.strip()
        if not line.startswith("-"):
            continue
        line = line.lstrip("-").strip()
        fields: dict[str, str] = {}
        for part in line.split("|"):
            if ":" not in part:
                continue
            k, _, v = part.partition(":")
            fields[k.strip().lower()] = v.strip()
        pid = fields.get("id", "")
        title = fields.get("title", "")
        gate = fields.get("gate", "").lower()
        reason = fields.get("reason", "")
        if not pid or gate not in PHASE_GATES:
            continue
        phases.append(Phase(id=pid, title=title, gate=gate, reason=reason))
    return phases


def _find_phase_plan(store, inst: Instance | None, max_depth: int = 24) -> list[Phase]:
    """Walk parent_id chain to find the most recent phase-plan block.

    The plan-review loop (review → apply → triage) often produces follow-up
    instances whose result files don't echo the original `phase-plan` block
    (e.g. TRIAGE_DEFERRED_PROMPT only asks for the triage section). Without
    this walk, autopilot would silently fall back to single-shot for any
    plan that went through revisions. Depth must accommodate the worst-case
    review loop: up to MAX_ROUNDS=5 review/apply pairs (10 instances) plus
    plan + triage + the originating question, so we budget 24 to leave
    headroom for nested chain entry points. Bounded to protect against any
    future parent-id cycle.
    """
    visited: set[str] = set()
    current = inst
    while current and current.id not in visited and len(visited) < max_depth:
        visited.add(current.id)
        phases = _extract_phase_plan(current)
        if phases:
            return phases
        if not current.parent_id:
            return []
        current = store.get_instance(current.parent_id)
    return []


def _phase_gate_suffix(phase: Phase, where: str) -> str:
    """Build the user-facing notification text for a phase gate pause."""
    if where == "pre":
        head = f"Phase `{phase.id}` ({phase.title}) needs your input before starting"
    else:
        head = f"Phase `{phase.id}` ({phase.title}) shipped — review before next phase"
    if phase.reason:
        head += f": {phase.reason}"
    return head + "."


# --- Verify ---

_VERIFY_BLOCK_RE = re.compile(
    r'```verify\s*\n(.*?)\n```', re.IGNORECASE | re.DOTALL
)
_VERIFY_RESULT_RE = re.compile(
    r'^\s*RESULT:\s*(pass|fail|skip|manual)\s*$', re.IGNORECASE | re.MULTILINE
)
_VERIFY_ACTIONS_RE = re.compile(
    r'^\s*ACTIONS_TESTED:\s*(.+)$', re.IGNORECASE | re.MULTILINE
)
_VERIFY_ENDPOINTS_RE = re.compile(
    r'^\s*ENDPOINTS_USED:\s*(.+)$', re.IGNORECASE | re.MULTILINE
)
_VERIFY_SUMMARY_RE = re.compile(
    r'^\s*SUMMARY:\s*(.+)$', re.IGNORECASE | re.MULTILINE
)
_VERIFY_WHY_RE = re.compile(
    r'^\s*WHY:\s*(.+)$', re.IGNORECASE | re.MULTILINE
)


VerifyOutcome = Literal["pass", "fail", "manual", "skip", "crashed"]


def _verify_outcome(inst: Instance) -> VerifyOutcome:
    """Classify a verify-instance run into one of five outcomes.

    Status check runs FIRST: a non-COMPLETED instance is `crashed` regardless
    of whatever block text the runner happened to capture. This prevents a
    stale `RESULT:` line from a prior fix-loop round (or partial stdout) from
    masquerading as a real verdict when the verify environment actually died.

    On healthy instances we parse the LAST ```verify``` block — if the model
    narrates/pre-quotes the template before the real output, the final block
    is authoritative. Missing block / unparseable RESULT collapses to `fail`
    so the fix-loop runs (Claude forgot to emit it ≠ environment crashed).
    """
    if inst.status != InstanceStatus.COMPLETED:
        return "crashed"
    if not inst.result_file:
        return "fail"
    try:
        text = Path(inst.result_file).read_text(encoding="utf-8")
    except OSError:
        return "fail"
    blocks = _VERIFY_BLOCK_RE.findall(text)
    if not blocks:
        return "fail"
    block = blocks[-1]
    result_m = _VERIFY_RESULT_RE.search(block)
    if not result_m:
        return "fail"
    result = result_m.group(1).lower()
    if result == "skip":
        summary = _VERIFY_SUMMARY_RE.search(block)
        log.info(
            "Verify skipped: %s",
            summary.group(1).strip() if summary else "no reason given",
        )
        return "skip"
    if result == "manual":
        why = _VERIFY_WHY_RE.search(block)
        log.info(
            "Verify manual: %s",
            why.group(1).strip() if why else "no WHY given",
        )
        return "manual"
    # pass/fail path — log what was actually tested for observability
    actions = _VERIFY_ACTIONS_RE.search(block)
    endpoints = _VERIFY_ENDPOINTS_RE.search(block)
    if actions:
        log.info("Verify actions: %s", actions.group(1).strip())
    if endpoints:
        log.info("Verify endpoints: %s", endpoints.group(1).strip())
    return "pass" if result == "pass" else "fail"


def _verify_why(inst: Instance) -> str | None:
    """Extract WHY: line from the LAST verify block, or None if absent."""
    if not inst.result_file:
        return None
    try:
        text = Path(inst.result_file).read_text(encoding="utf-8")
    except OSError:
        return None
    blocks = _VERIFY_BLOCK_RE.findall(text)
    if not blocks:
        return None
    m = _VERIFY_WHY_RE.search(blocks[-1])
    if not m:
        return None
    why = m.group(1).strip()
    return why or None

# Pull a fenced ```json``` block out of verifier output.
_JSON_BLOCK_RE = re.compile(
    r'```json\s*\n(.*?)```', re.DOTALL,
)


def _parse_verifier_json(text: str) -> dict | None:
    """Extract the verifier's JSON verdict from result text. None on failure.

    Takes the LAST ```json``` block in the output — if Claude narrates or
    quotes the prompt template before emitting the real answer, the final
    block is the authoritative verdict. Matches the convention used by
    `_verify_outcome`.
    """
    if not text:
        return None
    blocks = _JSON_BLOCK_RE.findall(text)
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1].strip())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("verdict") not in ("ok", "mismatch"):
        return None
    return data
