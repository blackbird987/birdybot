"""Dataclasses and enums for coding CLI instance management."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


# Tools that indicate code was modified (used for button context detection).
# Claude and Cursor share these names.  Override via provider.code_change_tools
# when a provider with different tool names is added (e.g. Codex).
CODE_CHANGE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})


class InstanceType(str, Enum):
    TASK = "task"
    QUERY = "query"
    SCHEDULED = "scheduled"


class InstanceStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


class InstanceOrigin(str, Enum):
    DIRECT = "direct"           # User typed a message or command
    PLAN = "plan"               # [Plan] button
    BUILD = "build"             # [Build It] button
    REVIEW_PLAN = "review_plan" # [Review Plan] button
    APPLY_REVISIONS = "apply_revisions"  # [Apply Revisions] button
    REVIEW_CODE = "review_code" # [Review Code] button
    COMMIT = "commit"           # [Commit] button
    DONE = "done"               # [Done] button — commit + close thread
    RELEASE = "release"         # /release command
    RETRY = "retry"             # [Retry] button
    VERIFY = "verify"           # [Verify] step — test/validate changes
    VERIFY_RELEASE = "verify_release"  # Autopilot gate: cross-check changelog/commit claims vs diff
    BUILD_AND_SHIP = "build_and_ship"  # [Build & Ship] button
    BG = "bg"                       # /bg command — background task


# Origins that belong to the plan workflow (used in lifecycle + button selection)
PLAN_ORIGINS = frozenset({InstanceOrigin.PLAN, InstanceOrigin.REVIEW_PLAN, InstanceOrigin.APPLY_REVISIONS})


def _migrate_message_ids(d: dict) -> dict[str, list[str]]:
    """Backward compat: convert old telegram_message_ids list[int] to new dict format.

    Deprecated: kept to avoid breaking state.json files from older versions.
    """
    if "message_ids" in d:
        return d["message_ids"]
    # Migrate from old format
    old_ids = d.get("telegram_message_ids", [])
    if old_ids:
        return {"telegram": [str(mid) for mid in old_ids]}
    return {}


@dataclass
class Instance:
    id: str                                 # "t-001", "q-004"
    name: str | None                        # Optional human name
    instance_type: InstanceType
    prompt: str
    repo_name: str
    repo_path: str                          # Absolute path (frozen at creation)
    status: InstanceStatus
    session_id: str | None = None           # Claude Code session for --resume
    mode: str = "explore"                   # "explore" or "build"
    branch: str | None = None               # Auto-created branch for build bg tasks
    original_branch: str | None = None      # Branch to merge back into
    worktree_path: str | None = None        # Isolated worktree directory for builds
    created_at: str = ""
    finished_at: str | None = None
    summary: str | None = None
    result_file: str | None = None          # Path to data/results/q-001.md
    diff_file: str | None = None            # Path to data/results/q-001.diff
    error: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    retry_count: int = 0
    message_ids: dict[str, list[str]] = field(default_factory=dict)  # platform -> [msg_id]
    pid: int | None = None
    schedule_id: str | None = None
    origin: InstanceOrigin = InstanceOrigin.DIRECT
    parent_id: str | None = None       # ID of instance whose button spawned this
    origin_platform: str = "discord"  # Platform that created this instance
    user_id: str = ""                  # User who started this instance
    user_name: str = ""                # Display name of the user
    tools_used: list[str] = field(default_factory=list)  # Tool names used (Edit, Write, TodoWrite...)
    bash_commands: list[str] = field(default_factory=list)  # Bash commands run (for eval)
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Live-context footer — populated from the latest assistant `message.usage`.
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    context_tokens: int = 0       # input + cache_read + cache_creation
    context_model: str | None = None  # Model name reported in usage (for window lookup)
    warning_pinned: bool = False      # 95% context warning already fired (idempotent)
    plan_active: bool = False  # Session has an active plan (for button context)
    code_active: bool = False  # Session has uncommitted code changes (for button context)
    needs_input: bool = False  # AskUserQuestion detected — waiting for user reply
    needs_manual_verification: bool = False  # Verify outcome was `manual` or unrecoverable `fail` under warn — handed off to Verify Board
    manual_verify_reason: str | None = None  # WHY: line (or chain summary) for the Verify Board entry
    deferred_revisions: list[str] = field(default_factory=list)  # Medium/Low revisions from plan review
    jsonl_uuid_by_msg_id: dict[str, str] = field(default_factory=dict)  # Discord msg_id -> JSONL assistant uuid (for "Branch from here")
    # Access control fields (non-owner sessions)
    is_owner_session: bool = True     # False for granted user sessions
    bash_policy: str = "full"         # "full"=Bash unrestricted; "allowlist"=non-owner allowlist guard; "none"=Bash disabled for ANY session
    effort: str = "high"             # reasoning effort: low/medium/high/max
    model: str | None = None         # CLI model override (e.g. "sonnet" for plan steps)
    cooldown_retry_at: str | None = None   # ISO datetime — auto-retry after usage limit
    cooldown_retries: int = 0              # Count of cooldown retries attempted (capped at 3)
    cooldown_channel_id: str | None = None # Channel to retry in (for cooldown loop)
    api_fallback: bool = False            # Force API billing fallback on next run
    session_account: str | None = None   # Account dir whose ~/.claude/projects/ owns session_id (for prefer-on-resume)
    chained_from: str | None = None       # Instance ID of the prior build whose branch+worktree this build was stacked on
    _accounts_tried: set[str] = field(default_factory=set)  # Ephemeral: tracks accounts tried this run (not persisted)

    def display_id(self) -> str:
        if self.name:
            return f"[{self.id}:{self.name}]"
        return f"[{self.id}]"

    def read_result_text(self) -> str:
        """Return result-file text, or "" if missing/unreadable.

        Best-effort read used by heuristic eval and workflow parsers —
        any read failure (missing path, IO error, decode error) collapses
        to "" so callers don't need to wrap every read in try/except.
        """
        if not self.result_file:
            return ""
        try:
            return Path(self.result_file).read_text(encoding="utf-8")
        except Exception:
            return ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "instance_type": self.instance_type.value,
            "prompt": self.prompt,
            "repo_name": self.repo_name,
            "repo_path": self.repo_path,
            "status": self.status.value,
            "session_id": self.session_id,
            "mode": self.mode,
            "branch": self.branch,
            "original_branch": self.original_branch,
            "worktree_path": self.worktree_path,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
            "result_file": self.result_file,
            "diff_file": self.diff_file,
            "error": self.error,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "retry_count": self.retry_count,
            "message_ids": self.message_ids,
            "pid": self.pid,
            "schedule_id": self.schedule_id,
            "origin": self.origin.value,
            "parent_id": self.parent_id,
            "origin_platform": self.origin_platform,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "tools_used": self.tools_used,
            "bash_commands": self.bash_commands,
            "num_turns": self.num_turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "context_tokens": self.context_tokens,
            "context_model": self.context_model,
            "warning_pinned": self.warning_pinned,
            "plan_active": self.plan_active,
            "code_active": self.code_active,
            "needs_input": self.needs_input,
            "needs_manual_verification": self.needs_manual_verification,
            "manual_verify_reason": self.manual_verify_reason,
            "deferred_revisions": self.deferred_revisions,
            "jsonl_uuid_by_msg_id": self.jsonl_uuid_by_msg_id,
            "is_owner_session": self.is_owner_session,
            "bash_policy": self.bash_policy,
            "effort": self.effort,
            "model": self.model,
            "cooldown_retry_at": self.cooldown_retry_at,
            "cooldown_retries": self.cooldown_retries,
            "cooldown_channel_id": self.cooldown_channel_id,
            "api_fallback": self.api_fallback,
            "session_account": self.session_account,
            "chained_from": self.chained_from,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Instance:
        return cls(
            id=d["id"],
            name=d.get("name"),
            instance_type=InstanceType(d["instance_type"]),
            prompt=d["prompt"],
            repo_name=d["repo_name"],
            repo_path=d["repo_path"],
            status=InstanceStatus(d["status"]),
            session_id=d.get("session_id"),
            mode=d.get("mode", "explore"),
            branch=d.get("branch"),
            original_branch=d.get("original_branch"),
            worktree_path=d.get("worktree_path"),
            created_at=d.get("created_at", ""),
            finished_at=d.get("finished_at"),
            summary=d.get("summary"),
            result_file=d.get("result_file"),
            diff_file=d.get("diff_file"),
            error=d.get("error"),
            cost_usd=d.get("cost_usd"),
            duration_ms=d.get("duration_ms"),
            retry_count=d.get("retry_count", 0),
            message_ids=_migrate_message_ids(d),
            pid=d.get("pid"),
            schedule_id=d.get("schedule_id"),
            origin=InstanceOrigin(d["origin"]) if "origin" in d else InstanceOrigin.DIRECT,
            parent_id=d.get("parent_id"),
            origin_platform=d.get("origin_platform", "discord"),
            user_id=d.get("user_id", ""),
            user_name=d.get("user_name", ""),
            tools_used=d.get("tools_used", []),
            bash_commands=d.get("bash_commands", []),
            num_turns=d.get("num_turns", 0),
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            cache_read_tokens=d.get("cache_read_tokens", 0),
            cache_creation_tokens=d.get("cache_creation_tokens", 0),
            context_tokens=d.get("context_tokens", 0),
            context_model=d.get("context_model"),
            warning_pinned=d.get("warning_pinned", False),
            plan_active=d.get("plan_active", False),
            code_active=d.get("code_active", False),
            needs_input=d.get("needs_input", False),
            needs_manual_verification=d.get("needs_manual_verification", False),
            manual_verify_reason=d.get("manual_verify_reason"),
            deferred_revisions=d.get("deferred_revisions", []),
            jsonl_uuid_by_msg_id=d.get("jsonl_uuid_by_msg_id", {}),
            is_owner_session=d.get("is_owner_session", True),
            bash_policy=d.get("bash_policy", "full"),
            effort=d.get("effort", "high"),
            model=d.get("model"),
            cooldown_retry_at=d.get("cooldown_retry_at"),
            cooldown_retries=d.get("cooldown_retries", 0),
            cooldown_channel_id=d.get("cooldown_channel_id"),
            api_fallback=d.get("api_fallback", False),
            session_account=d.get("session_account"),
            chained_from=d.get("chained_from"),
        )


@dataclass
class RunResult:
    session_id: str | None = None
    result_text: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0
    duration_api_ms: int = 0
    is_error: bool = False
    error_message: str | None = None
    tools_used: list[str] = field(default_factory=list)  # Unique tool names used
    bash_commands: list[str] = field(default_factory=list)  # Bash commands run (for eval)
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Usage snapshot from the last assistant event — used by the context footer
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    context_tokens: int = 0          # input + cache_read + cache_creation
    model: str | None = None         # Model name reported in usage
    needs_input: bool = False  # AskUserQuestion detected — waiting for user reply
    usage_limit_reset: object = None  # datetime | None — when usage limit resets (set by parser)
    api_fallback_used: bool = False   # True if result came from API billing fallback
    last_assistant_uuid: str | None = None  # JSONL uuid of final assistant message (for "Branch from here")
    # Recovery exhausted: layer-3 path fired (rebuild + cross-account both failed,
    # session_id was dropped and a fresh session was created).  The thread's prior
    # context is unrecoverable from this run's perspective.  commands.py surfaces
    # this to the user so the dementia is visible instead of silent.
    session_recovery_exhausted: bool = False
    # True iff the runner's on_recovery callback fired successfully for this run
    # (Layer 4 of t-3541).  Used by commands.py to suppress the older terse
    # post-completion notice when the richer mid-run callback warning already
    # posted — avoids posting two near-identical warnings for the same event.
    recovery_warning_posted: bool = False


# Valid gate types for autopilot phase boundaries.
# - mechanical: pure refactor (rename, move, format) — autopilot flies through
# - design:     human input wanted on approach before starting — pause pre-phase
# - risk:       production-behavior change — pause for human review post-phase
PHASE_GATES = frozenset({"mechanical", "design", "risk"})


@dataclass
class Phase:
    """One declared phase from a multi-phase plan."""
    id: str                    # short slug, e.g. "p1"
    title: str                 # human-readable label
    gate: str                  # one of PHASE_GATES
    reason: str = ""           # optional one-line rationale from the plan

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "gate": self.gate, "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict) -> Phase:
        return cls(
            id=d["id"],
            title=d.get("title", ""),
            gate=d.get("gate", "mechanical"),
            reason=d.get("reason", ""),
        )


@dataclass
class ChainPhaseState:
    """Tracks position in a multi-phase build chain.

    Persists across reboots so a chain can be resumed mid-phase. The build
    step in the autopilot chain consults this to know which phase to spawn,
    whether to pause for a gate, and where to pick up after a restart.
    """
    phases: list[Phase] = field(default_factory=list)
    cursor: int = 0                          # index of the phase currently in flight (0-based)
    paused_at: str | None = None             # None | "pre" | "post" — gate the chain is waiting at
    pre_phase_head: str | None = None        # git SHA captured BEFORE the phase spawned (for empty-diff guard + reboot recovery)
    worktree_path: str | None = None         # shared worktree across all phases of the chain
    first_build_id: str | None = None        # instance id of the first phase build (so later phases can copy_branch)

    def current(self) -> Phase | None:
        if 0 <= self.cursor < len(self.phases):
            return self.phases[self.cursor]
        return None

    def is_done(self) -> bool:
        return self.cursor >= len(self.phases)

    def to_dict(self) -> dict:
        return {
            "phases": [p.to_dict() for p in self.phases],
            "cursor": self.cursor,
            "paused_at": self.paused_at,
            "pre_phase_head": self.pre_phase_head,
            "worktree_path": self.worktree_path,
            "first_build_id": self.first_build_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ChainPhaseState:
        return cls(
            phases=[Phase.from_dict(p) for p in d.get("phases", [])],
            cursor=d.get("cursor", 0),
            paused_at=d.get("paused_at"),
            pre_phase_head=d.get("pre_phase_head"),
            worktree_path=d.get("worktree_path"),
            first_build_id=d.get("first_build_id"),
        )


@dataclass
class Schedule:
    id: str                     # "s-001"
    prompt: str
    repo_name: str
    repo_path: str
    mode: str = "explore"       # "explore" or "build"
    interval_secs: int | None = None    # For recurring
    run_at: str | None = None           # ISO time for one-shot
    is_recurring: bool = True
    last_run_at: str | None = None
    next_run_at: str | None = None
    last_summary: str | None = None     # For smart diffing
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "repo_name": self.repo_name,
            "repo_path": self.repo_path,
            "mode": self.mode,
            "interval_secs": self.interval_secs,
            "run_at": self.run_at,
            "is_recurring": self.is_recurring,
            "last_run_at": self.last_run_at,
            "next_run_at": self.next_run_at,
            "last_summary": self.last_summary,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Schedule:
        return cls(
            id=d["id"],
            prompt=d["prompt"],
            repo_name=d["repo_name"],
            repo_path=d["repo_path"],
            mode=d.get("mode", "explore"),
            interval_secs=d.get("interval_secs"),
            run_at=d.get("run_at"),
            is_recurring=d.get("is_recurring", True),
            last_run_at=d.get("last_run_at"),
            next_run_at=d.get("next_run_at"),
            last_summary=d.get("last_summary"),
            enabled=d.get("enabled", True),
        )
