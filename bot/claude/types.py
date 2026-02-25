"""Dataclasses and enums for Claude Code instance management."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


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
    REVIEW_CODE = "review_code" # [Review Code] button
    COMMIT = "commit"           # [Commit] button
    RETRY = "retry"             # [Retry] button


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
    created_at: str = ""
    finished_at: str | None = None
    summary: str | None = None
    result_file: str | None = None          # Path to data/results/q-001.md
    diff_file: str | None = None            # Path to data/results/q-001.diff
    error: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    retry_count: int = 0
    telegram_message_ids: list[int] = field(default_factory=list)
    pid: int | None = None
    schedule_id: str | None = None
    origin: InstanceOrigin = InstanceOrigin.DIRECT
    parent_id: str | None = None       # ID of instance whose button spawned this

    def display_id(self) -> str:
        if self.name:
            return f"[{self.id}:{self.name}]"
        return f"[{self.id}]"

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
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
            "result_file": self.result_file,
            "diff_file": self.diff_file,
            "error": self.error,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "retry_count": self.retry_count,
            "telegram_message_ids": self.telegram_message_ids,
            "pid": self.pid,
            "schedule_id": self.schedule_id,
            "origin": self.origin.value,
            "parent_id": self.parent_id,
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
            created_at=d.get("created_at", ""),
            finished_at=d.get("finished_at"),
            summary=d.get("summary"),
            result_file=d.get("result_file"),
            diff_file=d.get("diff_file"),
            error=d.get("error"),
            cost_usd=d.get("cost_usd"),
            duration_ms=d.get("duration_ms"),
            retry_count=d.get("retry_count", 0),
            telegram_message_ids=d.get("telegram_message_ids", []),
            pid=d.get("pid"),
            schedule_id=d.get("schedule_id"),
            origin=InstanceOrigin(d["origin"]) if "origin" in d else InstanceOrigin.DIRECT,
            parent_id=d.get("parent_id"),
        )


@dataclass
class RunResult:
    session_id: str | None = None
    result_text: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0
    is_error: bool = False
    error_message: str | None = None


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
