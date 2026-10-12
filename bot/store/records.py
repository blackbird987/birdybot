"""Per-repo records the store persists for the deploy and auto-fix flows.

They live under bot.store, not next to the engine code that fills them,
so the store does not import the engine to load its own state file.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DeployState:
    """Per-repo deploy state tracking."""

    boot_version: str | None = None
    boot_ref: str = ""
    current_version: str | None = None
    current_ref: str = ""
    self_managed: bool = False
    pending_sessions: list[str] = field(default_factory=list)
    pending_changes: list[str] = field(default_factory=list)
    last_deploy_error: str | None = None
    auto_fix_attempt: int = 0
    auto_fix_thread_id: str | None = None

    @property
    def needs_reboot(self) -> bool:
        return self.boot_ref != self.current_ref and self.boot_ref != ""

    def to_dict(self) -> dict:
        return {
            "boot_version": self.boot_version,
            "boot_ref": self.boot_ref,
            "current_version": self.current_version,
            "current_ref": self.current_ref,
            "self_managed": self.self_managed,
            "pending_sessions": self.pending_sessions,
            "pending_changes": self.pending_changes,
            "last_deploy_error": self.last_deploy_error,
            "auto_fix_attempt": self.auto_fix_attempt,
            "auto_fix_thread_id": self.auto_fix_thread_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DeployState:
        return cls(
            boot_version=data.get("boot_version"),
            boot_ref=data.get("boot_ref", ""),
            current_version=data.get("current_version"),
            current_ref=data.get("current_ref", ""),
            self_managed=data.get("self_managed", False),
            pending_sessions=data.get("pending_sessions", []),
            pending_changes=data.get("pending_changes", []),
            last_deploy_error=data.get("last_deploy_error"),
            auto_fix_attempt=data.get("auto_fix_attempt", 0),
            auto_fix_thread_id=data.get("auto_fix_thread_id"),
        )


@dataclass
class AutoFixState:
    """Per-repo, per-trigger auto-fix tracking."""

    attempt: int = 0
    thread_id: str | None = None

    def to_dict(self) -> dict:
        return {"attempt": self.attempt, "thread_id": self.thread_id}

    @classmethod
    def from_dict(cls, d: dict) -> AutoFixState:
        return cls(attempt=d.get("attempt", 0), thread_id=d.get("thread_id"))
