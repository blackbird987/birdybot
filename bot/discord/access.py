"""Per-repo user access control for Discord multi-user support.

Manages access grants (who can use which repos), mode ceilings,
bash policies, and daily rate limits. Stored in data/access.json.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from bot import config
from bot.platform.modes import MODE_RANK, cap_mode
from bot.textutil import match_name

log = logging.getLogger(__name__)

ACCESS_FILE: Path = config.DATA_DIR / "access.json"


@dataclass
class RepoAccess:
    """Access configuration for a single repo grant."""
    mode: str = "explore"               # mode ceiling: explore, plan, build
    max_daily_queries: int = 30
    bash_policy: str = "allowlist"      # "allowlist", "full", "none"

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "max_daily_queries": self.max_daily_queries,
            "bash_policy": self.bash_policy,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RepoAccess:
        return cls(
            mode=d.get("mode", "explore"),
            max_daily_queries=d.get("max_daily_queries", 30),
            bash_policy=d.get("bash_policy", "allowlist"),
        )


@dataclass
class UserAccess:
    """Access grants for a single Discord user."""
    user_id: str
    display_name: str
    repos: dict[str, RepoAccess] = field(default_factory=dict)  # repo_name -> access
    global_access: bool = False
    forum_channel_id: str | None = None  # their personal forum
    welcome_posted: bool = False         # welcome post created in personal forum
    control_thread_id: str | None = None   # control room thread in personal forum
    control_message_id: str | None = None  # control room embed message
    archive_thread_id: str | None = None   # archive thread in personal forum

    def to_dict(self) -> dict:
        d = {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "repos": {k: v.to_dict() for k, v in self.repos.items()},
            "global_access": self.global_access,
            "forum_channel_id": self.forum_channel_id,
            "welcome_posted": self.welcome_posted,
        }
        if self.control_thread_id:
            d["control_thread_id"] = self.control_thread_id
        if self.control_message_id:
            d["control_message_id"] = self.control_message_id
        if self.archive_thread_id:
            d["archive_thread_id"] = self.archive_thread_id
        return d

    @classmethod
    def from_dict(cls, d: dict) -> UserAccess:
        repos = {k: RepoAccess.from_dict(v) for k, v in d.get("repos", {}).items()}
        return cls(
            user_id=d["user_id"],
            display_name=d.get("display_name", ""),
            repos=repos,
            global_access=d.get("global_access", False),
            forum_channel_id=d.get("forum_channel_id"),
            welcome_posted=d.get("welcome_posted", False),
            control_thread_id=d.get("control_thread_id"),
            control_message_id=d.get("control_message_id"),
            archive_thread_id=d.get("archive_thread_id"),
        )


@dataclass
class AccessConfig:
    """Top-level access configuration."""
    users: dict[str, UserAccess] = field(default_factory=dict)  # user_id -> access
    default_mode: str = "explore"
    default_max_daily_queries: int = 30
    daily_counts: dict[str, dict[str, int]] = field(default_factory=dict)  # date -> {user_id: count}

    def to_dict(self) -> dict:
        return {
            "users": {k: v.to_dict() for k, v in self.users.items()},
            "default_mode": self.default_mode,
            "default_max_daily_queries": self.default_max_daily_queries,
            "daily_counts": self.daily_counts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> AccessConfig:
        users = {k: UserAccess.from_dict(v) for k, v in d.get("users", {}).items()}
        return cls(
            users=users,
            default_mode=d.get("default_mode", "explore"),
            default_max_daily_queries=d.get("default_max_daily_queries", 30),
            daily_counts=d.get("daily_counts", {}),
        )


@dataclass
class AccessResult:
    """Result of an access check."""
    allowed: bool
    is_owner: bool
    mode_ceiling: str | None = None   # None = no ceiling (owner)
    bash_policy: str | None = None    # None = unrestricted (owner)
    max_daily_queries: int = 0
    reason: str = ""


# --- Cache ---

_cached_config: AccessConfig | None = None
_cache_mtime: float = 0.0
_CACHE_TTL: float = 30.0  # seconds


def _invalidate_cache() -> None:
    global _cached_config, _cache_mtime
    _cached_config = None
    _cache_mtime = 0.0


def load_access_config() -> AccessConfig:
    """Load access config from data/access.json. Cached with 30s TTL."""
    global _cached_config, _cache_mtime

    now = time.monotonic()
    if _cached_config is not None and (now - _cache_mtime) < _CACHE_TTL:
        return _cached_config

    if not ACCESS_FILE.exists():
        _cached_config = AccessConfig()
        _cache_mtime = now
        return _cached_config

    try:
        data = json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
        cfg = AccessConfig.from_dict(data)
        # Prune daily_counts older than 7 days
        if len(cfg.daily_counts) > 7:
            sorted_dates = sorted(cfg.daily_counts.keys(), reverse=True)
            for old_date in sorted_dates[7:]:
                del cfg.daily_counts[old_date]

        _cached_config = cfg
        _cache_mtime = now
        return cfg
    except Exception:
        log.exception("Failed to load access config")
        _cached_config = AccessConfig()
        _cache_mtime = now
        return _cached_config


def save_access_config(cfg: AccessConfig) -> None:
    """Save access config to data/access.json. Invalidates cache."""
    try:
        ACCESS_FILE.write_text(
            json.dumps(cfg.to_dict(), indent=2),
            encoding="utf-8",
        )
        _invalidate_cache()
    except Exception:
        log.exception("Failed to save access config")


def check_user_access(
    cfg: AccessConfig, user_id: str, repo_name: str | None,
) -> RepoAccess | None:
    """Check if a user has access to a repo. Returns RepoAccess or None.

    Case-insensitive repo name matching.
    """
    ua = cfg.users.get(user_id)
    if not ua:
        return None
    if ua.global_access:
        # Return default access settings
        return RepoAccess(
            mode=cfg.default_mode,
            max_daily_queries=cfg.default_max_daily_queries,
        )
    if not repo_name:
        return None
    canonical = match_name(repo_name, ua.repos)
    return ua.repos[canonical] if canonical else None


def has_any_access(cfg: AccessConfig, user_id: str) -> bool:
    """Check if a user has any access grant at all."""
    ua = cfg.users.get(user_id)
    if not ua:
        return False
    return bool(ua.global_access or ua.repos)


def get_most_restrictive_bash(cfg: AccessConfig, user_id: str) -> str:
    """Tightest bash policy across all of a user's grants.

    The twin of get_most_restrictive_ceiling, and used by the same caller: a
    guest in a channel whose repo cannot be resolved gets the strictest thing
    they hold anywhere, never the permissive default. Granting one repo
    bash="none" must not be undone by a channel the resolver did not
    recognise.
    """
    ua = cfg.users.get(user_id)
    if not ua:
        return "none"
    if ua.global_access:
        return "allowlist"
    _RANK = {"none": 0, "allowlist": 1, "full": 2}
    tightest = "none"
    tightest_rank = 99
    for grant in ua.repos.values():
        rank = _RANK.get(grant.bash_policy, 1)
        if rank < tightest_rank:
            tightest_rank, tightest = rank, grant.bash_policy
    return tightest if tightest_rank < 99 else "none"


def get_most_restrictive_ceiling(cfg: AccessConfig, user_id: str) -> str:
    """Get the most restrictive mode ceiling across all of a user's grants."""
    ua = cfg.users.get(user_id)
    if not ua:
        return "explore"
    if ua.global_access:
        return cfg.default_mode
    min_rank = 2
    for grant in ua.repos.values():
        rank = MODE_RANK.get(grant.mode, 0)
        if rank < min_rank:
            min_rank = rank
    rank_to_mode = {0: "explore", 1: "plan", 2: "build"}
    return rank_to_mode.get(min_rank, "explore")


def effective_mode(grant: RepoAccess, requested_mode: str) -> str:
    """Enforce mode ceiling. Returns the effective mode."""
    # A grant with no mode recorded is the most restrictive one, not none.
    return cap_mode(requested_mode, grant.mode or "explore")


def check_rate_limit(cfg: AccessConfig, user_id: str, max_queries: int) -> bool:
    """Check if user is within daily rate limit. Returns True if allowed."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_counts = cfg.daily_counts.get(today, {})
    current = day_counts.get(user_id, 0)
    return current < max_queries


def increment_query_count(cfg: AccessConfig, user_id: str) -> None:
    """Increment daily query count for a user and save."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today not in cfg.daily_counts:
        cfg.daily_counts[today] = {}
    cfg.daily_counts[today][user_id] = cfg.daily_counts[today].get(user_id, 0) + 1
    save_access_config(cfg)


