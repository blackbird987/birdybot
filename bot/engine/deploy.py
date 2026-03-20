"""Deploy state tracking — detects version drift after merges."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from bot import config

log = logging.getLogger(__name__)
_NOWND: dict = config.NOWND

DEPLOY_CONFIG_PATH = ".claude/deploy.json"

# Version file parsers in priority order
_VERSION_PARSERS: list[tuple[str, str]] = [
    ("pyproject.toml", r'version\s*=\s*"([^"]+)"'),
    ("setup.cfg", r'version\s*=\s*(\S+)'),
    ("package.json", r'"version"\s*:\s*"([^"]+)"'),
    ("Cargo.toml", r'version\s*=\s*"([^"]+)"'),
]

_CSPROJ_VERSION_RE = re.compile(r"<Version>([^<]+)</Version>", re.IGNORECASE)


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
        )


def detect_version(repo_path: str) -> str | None:
    """Detect version from standard version files (pyproject.toml, package.json, etc.)."""
    root = Path(repo_path)
    for filename, pattern in _VERSION_PARSERS:
        fpath = root / filename
        if fpath.exists():
            try:
                text = fpath.read_text(encoding="utf-8")
                m = re.search(pattern, text)
                if m:
                    return m.group(1)
            except Exception:
                log.debug("Failed to read %s in %s", filename, repo_path, exc_info=True)

    # Check *.csproj files
    for csproj in root.glob("*.csproj"):
        try:
            text = csproj.read_text(encoding="utf-8")
            m = _CSPROJ_VERSION_RE.search(text)
            if m:
                return m.group(1)
        except Exception:
            pass

    # Fallback: latest version tag
    return _get_latest_version_tag(repo_path)


def get_head_ref(repo_path: str) -> str:
    """Get short HEAD commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=10, **_NOWND,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def get_latest_version_tag_ref(repo_path: str) -> str:
    """Find the most recent version tag (vX.Y.Z) and return its short commit ref.

    Uses ^{} to dereference annotated tags to their underlying commit,
    so the ref is comparable with HEAD (which is always a commit hash).
    """
    tag = _get_latest_version_tag(repo_path)
    if not tag:
        return ""
    try:
        # ^{} dereferences annotated tags to the commit object
        result = subprocess.run(
            ["git", "rev-parse", "--short", f"{tag}^{{}}"],
            cwd=repo_path, capture_output=True, text=True, timeout=10, **_NOWND,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _get_latest_version_tag(repo_path: str) -> str | None:
    """Get the latest version tag name (vX.Y.Z)."""
    try:
        result = subprocess.run(
            ["git", "tag", "--list", "v*", "--sort=-version:refname"],
            cwd=repo_path, capture_output=True, text=True, timeout=10, **_NOWND,
        )
        if result.returncode == 0:
            tags = result.stdout.strip().splitlines()
            for tag in tags:
                tag = tag.strip()
                if re.match(r"^v\d+\.\d+", tag):
                    return tag
    except Exception:
        pass
    return None


def get_unreleased_changes(repo_path: str) -> list[str]:
    """Parse [Unreleased] section from CHANGELOG.md, return bullet items."""
    changelog = Path(repo_path) / "CHANGELOG.md"
    if not changelog.exists():
        return []
    try:
        text = changelog.read_text(encoding="utf-8")
    except Exception:
        return []

    items: list[str] = []
    in_unreleased = False
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^##\s+\[Unreleased\]", stripped, re.IGNORECASE):
            in_unreleased = True
            continue
        if in_unreleased and stripped.startswith("## "):
            break  # Next version header
        if in_unreleased and stripped.startswith("- "):
            items.append(stripped[2:])
    return items


def get_recent_commits(repo_path: str, since_ref: str, limit: int = 5) -> list[str]:
    """Get recent commit messages since a ref."""
    if not since_ref:
        return []
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"{since_ref}..HEAD", f"-{limit}"],
            cwd=repo_path, capture_output=True, text=True, timeout=10, **_NOWND,
        )
        if result.returncode == 0:
            return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
    except Exception:
        pass
    return []


def capture_boot_baselines(store, bot_repo_path: str) -> None:
    """Capture deploy state baselines for all repos at boot time.

    For the bot's own repo: resets baseline (reboot = redeploy).
    For other repos: uses latest version tag as baseline, preserves pending state.
    """
    import os
    bot_norm = os.path.normcase(os.path.normpath(os.path.abspath(bot_repo_path)))

    for repo_name, repo_path in store.list_repos().items():
        head = get_head_ref(repo_path)
        version = detect_version(repo_path)
        is_self = os.path.normcase(os.path.normpath(os.path.abspath(repo_path))) == bot_norm

        if is_self:
            ds = DeployState(
                boot_version=version, boot_ref=head,
                current_version=version, current_ref=head,
                self_managed=True,
                pending_sessions=[], pending_changes=[],
            )
        else:
            tag_ref = get_latest_version_tag_ref(repo_path)
            existing = store.get_deploy_state(repo_name)
            if existing and existing.boot_ref == tag_ref:
                # Tag hasn't changed — preserve accumulated pending state
                existing.current_ref = head
                existing.current_version = version
                ds = existing
            else:
                # New tag appeared (or first boot) — reset pending state
                ds = DeployState(
                    boot_version=version, boot_ref=tag_ref or head,
                    current_version=version, current_ref=head,
                    self_managed=False,
                    pending_sessions=[], pending_changes=[],
                )

        store.set_deploy_state(repo_name, ds)
        log.info(
            "Deploy baseline for %s: version=%s ref=%s self_managed=%s",
            repo_name, ds.boot_version, ds.boot_ref, ds.self_managed,
        )


def update_after_merge(store, inst) -> None:
    """Update deploy state after a successful merge.

    Reads current version and changelog, records the session.
    """
    ds = store.get_deploy_state(inst.repo_name)
    if not ds:
        return

    ds.current_ref = get_head_ref(inst.repo_path)
    ds.current_version = detect_version(inst.repo_path)

    if inst.id not in ds.pending_sessions:
        ds.pending_sessions.append(inst.id)

    # Changelog first, fall back to recent commits
    changes = get_unreleased_changes(inst.repo_path)
    if not changes:
        changes = get_recent_commits(inst.repo_path, ds.boot_ref, limit=5)
    if changes:
        ds.pending_changes = changes

    store.set_deploy_state(inst.repo_name, ds)
    log.info(
        "Deploy state updated for %s: %s -> %s (%d pending sessions)",
        inst.repo_name, ds.boot_version, ds.current_version,
        len(ds.pending_sessions),
    )


# --- Deploy config helpers ---


def make_deploy_config(
    method: str,
    *,
    command: str | None = None,
    label: str = "Reboot",
    cwd: str | None = None,
    source: str = "manual",
    approved: bool = True,
) -> dict:
    """Build a deploy config dict with all required fields."""
    cfg: dict = {
        "method": method,
        "label": label,
        "source": source,
        "approved": approved,
    }
    if command is not None:
        cfg["command"] = command
    if cwd is not None:
        cfg["cwd"] = cwd
    return cfg


def is_deploy_protected(existing_config: dict | None, deploy_state: DeployState | None) -> bool:
    """Check if a repo's deploy config should not be overwritten by file scan.

    Protected when: self-managed, manually configured, or deploy state is self-managed.
    """
    if existing_config and existing_config.get("method") == "self":
        return True
    if existing_config and existing_config.get("source") == "manual":
        return True
    if deploy_state and deploy_state.self_managed:
        return True
    return False


def rescan_deploy_config_after_merge(store, repo_name: str, repo_path: str) -> None:
    """Re-scan .claude/deploy.json after a merge and update config if needed.

    Skips repos with protected configs (self-managed or manually set).
    """
    existing = store.get_deploy_config(repo_name)
    ds = store.get_deploy_state(repo_name)
    if is_deploy_protected(existing, ds):
        return
    file_cfg = scan_deploy_config(repo_path)
    if file_cfg:
        store.set_deploy_config(repo_name, make_deploy_config(
            "command",
            command=file_cfg["command"],
            label=file_cfg.get("label", "Deploy"),
            cwd=file_cfg.get("cwd"),
            source="file", approved=False,
        ))


def scan_deploy_config(repo_path: str) -> dict | None:
    """Read .claude/deploy.json from a repo if it exists.

    Returns raw file data with at least a 'command' key, or None.
    File-based configs are always command-based; 'method: self' is an
    internal concept handled by auto-detection, not by file convention.
    """
    cfg_path = Path(repo_path) / DEPLOY_CONFIG_PATH
    if not cfg_path.exists():
        return None
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        if "command" not in data:
            return None
        return data
    except Exception:
        log.debug("Failed to read deploy config from %s", cfg_path, exc_info=True)
        return None
