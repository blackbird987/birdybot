r"""Context window resolver for Claude Code CLI sessions.

Resolves the effective context window by merging the *env* block from
Claude Code settings.json across four precedence layers (highest wins):

    1. managed:  %ProgramData%\ClaudeCode\managed-settings.json   (Windows)
                 /Library/Application Support/ClaudeCode/...       (mac)
                 /etc/claude-code/managed-settings.json           (linux)
    2. project:  <repo>/.claude/settings.json
    3. local:    <repo>/.claude/settings.local.json
    4. user:     ~/.claude/settings.json

Honours CLAUDE_CODE_DISABLE_1M_CONTEXT — when set to "1", Sonnet falls
back to 200k.  Opus and Haiku are always 200k.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

_CACHE_TTL_SECS = 60.0
# cache key: (repo_path or "") -> (expires_at, merged_env_dict)
_env_cache: dict[str, tuple[float, dict]] = {}


def invalidate_cache() -> None:
    """Drop all cached settings — call from tests or after manual edits."""
    _env_cache.clear()


def _read_env_block(path: Path) -> dict:
    """Read the 'env' block from a settings.json file. Missing/invalid -> {}."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return {}
    except Exception:
        log.debug("Unexpected error reading %s", path, exc_info=True)
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        log.debug("Malformed JSON at %s", path, exc_info=True)
        return {}
    env = data.get("env") if isinstance(data, dict) else None
    return env if isinstance(env, dict) else {}


def _managed_settings_path() -> Path:
    """Resolve the platform-specific managed settings path."""
    if sys.platform == "win32":
        base = os.environ.get("ProgramData", r"C:\ProgramData")
        return Path(base) / "ClaudeCode" / "managed-settings.json"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def _merge_claude_settings_env(repo_path: str | None) -> dict:
    """Merge env blocks from all four settings.json layers.

    Precedence (highest wins): managed > project > local > user.
    Cached for 60s per repo_path.
    """
    key = repo_path or ""
    now = time.monotonic()
    cached = _env_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]

    merged: dict = {}

    # Lowest precedence first — later entries overwrite earlier ones.
    user = Path.home() / ".claude" / "settings.json"
    merged.update(_read_env_block(user))

    if repo_path:
        repo = Path(repo_path)
        local = repo / ".claude" / "settings.local.json"
        merged.update(_read_env_block(local))
        project = repo / ".claude" / "settings.json"
        merged.update(_read_env_block(project))

    merged.update(_read_env_block(_managed_settings_path()))

    _env_cache[key] = (now + _CACHE_TTL_SECS, merged)
    return merged


def context_window_for(model: str | None, repo_path: str | None = None) -> int:
    """Return the effective context window (tokens) for a model + repo.

    Sonnet returns 1,000,000 unless CLAUDE_CODE_DISABLE_1M_CONTEXT=1.
    Everything else returns 200,000.
    """
    if not model:
        return 200_000
    merged_env = _merge_claude_settings_env(repo_path)
    disable_1m = str(merged_env.get("CLAUDE_CODE_DISABLE_1M_CONTEXT", "")).strip() == "1"
    lower = model.lower()
    if "sonnet" in lower and not disable_1m:
        return 1_000_000
    return 200_000


def context_tokens_from_usage(usage: dict | None) -> int:
    """Sum the three token fields that make up the active context window.

    Verified against real CLI JSONL output: context grows monotonically with
    input + cache_read + cache_creation.  output_tokens is NOT counted.
    """
    if not isinstance(usage, dict):
        return 0
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )
