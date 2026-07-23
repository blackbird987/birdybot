"""Provider configuration for coding CLI tools (Claude Code, Cursor, etc.)."""

from __future__ import annotations

from dataclasses import dataclass

from bot.claude.parser import (
    parse_model_limit as _claude_parse_model_limit,
    parse_usage_limit as _claude_parse_usage_limit,
)


@dataclass(frozen=True)
class ProviderConfig:
    """Configuration + behaviour for a specific coding CLI provider."""

    name: str
    binary: str                       # Default binary name (overridden by CLAUDE_BINARY env)
    projects_dir_name: str            # e.g. ".claude" → ~/.claude/projects/
    branch_prefix: str                # e.g. "claude-bot" → claude-bot/t-001
    config_dir_env: str               # Env var for account switching (CLAUDE_CONFIG_DIR)
    nested_env_vars: tuple[str, ...]  # Env vars to strip to prevent nested-session errors
    instruction_file: str             # Repo-level instruction file (".claude/CLAUDE.md")
    config_dir_name: str              # Dir copied into worktrees (".claude")
    code_change_tools: frozenset[str] # Tool names that indicate file edits

    # Feature flags — controls which provider-specific code paths are active
    supports_account_failover: bool = False
    supports_api_fallback: bool = False
    supports_effort: bool = True
    supports_resume: bool = True
    # Steer = kill the in-flight run and re-spawn with --resume + new prompt.
    # Requires supports_resume + ability to kill and re-spawn cleanly.
    supports_steer: bool = False
    # True when the provider streams per-turn `message.usage` in stream-json.
    # Gates live context-window footer rendering (Claude yes, Cursor no).
    supports_live_usage: bool = False
    system_prompt_method: str = "cli_flag"  # "cli_flag" or "rules_dir"

    def build_command(
        self,
        instance: object,  # Instance (avoid circular import)
        *,
        binary: str | None = None,
        system_prompt_file: str | None,
        system_prompt_inline: str | None,
        api_fallback: bool,
        api_key_file: str | None,
        model_override: str | None = None,
    ) -> list[str]:
        """Build the CLI command args. Subclass-style dispatch via provider name.

        *binary* overrides config.CLAUDE_BINARY — use when the caller has
        snapshotted the binary path for in-flight session safety.
        *model_override* takes precedence over instance.model — used by the
        model-limit failover to run on the fallback model while the primary
        (e.g. Fable) is on its own cooldown.
        Returns the command list.  Prompt is always piped via stdin by the caller.
        """
        raise NotImplementedError(f"build_command not implemented for {self.name}")

    def parse_usage_limit(self, error_text: str) -> object | None:
        """Detect subscription usage-limit errors.  Returns datetime or None."""
        return None

    def parse_model_limit(self, error_text: str) -> object | None:
        """Detect MODEL-specific limit errors (e.g. Fable 5 quota).

        Returns (model_label, reset_datetime) or None.  Distinct from
        parse_usage_limit: the account stays usable for other models.
        """
        return None

    def default_model(self, instance: object) -> str | None:
        """The model this provider would run for *instance* — mirrors
        build_command's resolution minus runtime overrides (limit failover,
        api_fallback).  Display-only: labels a session before the CLI
        reports the model it actually ran.
        """
        return getattr(instance, "model", None)


class _ClaudeProvider(ProviderConfig):
    """Claude Code CLI provider."""

    def build_command(
        self,
        instance: object,
        *,
        binary: str | None = None,
        system_prompt_file: str | None,
        system_prompt_inline: str | None,
        api_fallback: bool,
        api_key_file: str | None,
        model_override: str | None = None,
    ) -> list[str]:
        import json as _json
        import sys

        from bot import config

        cmd = [binary or config.CLAUDE_BINARY, "-p"]
        cmd.extend(["--output-format", "stream-json", "--verbose"])
        # Stream hook events so the runner can log/observe hook decisions
        # (e.g. the worktree path-poisoning guard). Harmless when no hooks
        # are installed.
        cmd.extend(["--include-hook-events"])
        # Project-scoped settings.local.json doesn't load by default; the
        # bot writes the worktree-guard hook there, so opt all three scopes
        # in. Not applied to --bare runs (api_fallback) since --bare
        # explicitly disables the user settings cascade.
        if not api_fallback:
            cmd.extend(["--setting-sources", "user,project,local"])

        if self.supports_effort:
            cmd.extend(["--effort", instance.effort])  # type: ignore[attr-defined]

        # Model resolution: model-limit failover (model_override) beats the
        # per-instance choice (explicit model / MODEL_ROUTING / BUILD_ORIGINS→
        # BUILD_MODEL / EXPLORE_MODEL, all baked into instance.model at spawn by
        # resolve_spawn_model), which beats the deployment-wide default. This is
        # the single choke point for
        # DEFAULT_SESSION_MODEL so DIRECT sessions — which never pass through
        # workflows.spawn_from — get it too. model_override first means the
        # fallback can never undo an explicit limit-failover downgrade.
        model = (
            model_override
            or instance.model  # type: ignore[attr-defined]
            or config.DEFAULT_SESSION_MODEL
        )
        # Sticky-resume guard: the CLI restores a resumed session's last model,
        # so a session downgraded to MODEL_FALLBACK during a primary-model limit
        # window would stay on the fallback forever once no --model flag is
        # passed again. Pin the primary explicitly on resume so expired
        # cooldowns actually switch back.
        if not model and instance.session_id and not api_fallback:  # type: ignore[attr-defined]
            model = config.PRIMARY_MODEL
        if model and not api_fallback:
            cmd.extend(["--model", model])

        # API billing fallback: --bare + apiKeyHelper for secure key passing
        if api_fallback and api_key_file and config.ANTHROPIC_API_KEY:
            helper_cmd = (
                f'{sys.executable} -c '
                f'"print(open({repr(api_key_file)}).read().strip())"'
            )
            cmd.extend(["--bare"])
            cmd.extend(["--settings", _json.dumps({"apiKeyHelper": helper_cmd})])
            cmd.extend(["--model", config.API_FALLBACK_MODEL])
            cmd.extend(["--max-budget-usd", str(config.API_FALLBACK_MAX_USD)])

        # --- Claude-specific CLI args (inlined from former _build_common_args) ---

        # System prompt
        if system_prompt_file:
            cmd.extend(["--append-system-prompt-file", system_prompt_file])
        elif system_prompt_inline:
            cmd.extend(["--append-system-prompt", system_prompt_inline])

        # Resume session
        if instance.session_id:  # type: ignore[attr-defined]
            cmd.extend(["--resume", instance.session_id])  # type: ignore[attr-defined]

        # Permissions: always bypass (non-interactive bot can't approve prompts)
        cmd.extend(["--permission-mode", "bypassPermissions"])

        # Disallowed tools (uses per-provider code_change_tools field)
        disallowed: set[str] = set()
        if instance.mode != "build":  # type: ignore[attr-defined]
            disallowed.update(self.code_change_tools)
        # bash_policy="none" is an explicit opt-out of Bash for ANY session
        # (owner or non-owner). Used by the triage subagent's read-only floor
        # to prevent file writes via shell commands in explore mode.
        if instance.bash_policy == "none":  # type: ignore[attr-defined]
            disallowed.add("Bash")
        if disallowed:
            cmd.extend(["--disallowed-tools", ",".join(sorted(disallowed))])

        return cmd

    def parse_usage_limit(self, error_text: str) -> object | None:
        return _claude_parse_usage_limit(error_text)

    def parse_model_limit(self, error_text: str) -> object | None:
        return _claude_parse_model_limit(error_text)

    def default_model(self, instance: object) -> str | None:
        from bot import config

        # Fresh sessions get no --model flag, so the account's CLI default
        # runs — which PRIMARY_MODEL names by convention. Resumed sessions DO
        # get PRIMARY_MODEL pinned explicitly (see build_command's
        # sticky-resume guard), so both paths resolve to PRIMARY_MODEL here.
        # Runtime overrides (active limit-failover downgrade, api_fallback)
        # are intentionally not mirrored — see the base-class docstring.
        return (
            getattr(instance, "model", None)
            or config.DEFAULT_SESSION_MODEL
            or config.PRIMARY_MODEL
        )


class _CursorProvider(ProviderConfig):
    """Cursor CLI provider (agent binary)."""

    def build_command(
        self,
        instance: object,
        *,
        binary: str | None = None,
        system_prompt_file: str | None,
        system_prompt_inline: str | None,
        api_fallback: bool,
        api_key_file: str | None,
        model_override: str | None = None,  # unused — Claude-only feature
    ) -> list[str]:
        from bot import config

        cmd = [binary or config.CLAUDE_BINARY, "-p"]
        cmd.extend(["--output-format", "stream-json"])

        # Permissions: Cursor uses --force --trust (no --permission-mode)
        cmd.extend(["--force", "--trust"])

        # Model: env-configurable default (free tier = "auto", paid = specific model)
        model = instance.model if not api_fallback else None  # type: ignore[attr-defined]
        if not model:
            model = config.CURSOR_MODEL
        cmd.extend(["--model", model])

        # Mode-based access control (no --disallowed-tools):
        # build → full agent (no flag), plan origin → --mode plan, explore → --mode ask
        if instance.mode == "plan":  # type: ignore[attr-defined]
            cmd.extend(["--mode", "plan"])
        elif instance.mode != "build":  # type: ignore[attr-defined]
            cmd.extend(["--mode", "ask"])

        # Resume
        if self.supports_resume and instance.session_id:  # type: ignore[attr-defined]
            cmd.extend(["--resume", instance.session_id])  # type: ignore[attr-defined]

        # System prompt handled by runner via rules dir — not passed as CLI args
        return cmd

    def default_model(self, instance: object) -> str | None:
        from bot import config

        return getattr(instance, "model", None) or config.CURSOR_MODEL


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_CLAUDE = _ClaudeProvider(
    name="claude",
    binary="claude",
    projects_dir_name=".claude",
    branch_prefix="claude-bot",
    config_dir_env="CLAUDE_CONFIG_DIR",
    nested_env_vars=("CLAUDE_CODE", "CLAUDECODE"),
    instruction_file=".claude/CLAUDE.md",
    config_dir_name=".claude",
    code_change_tools=frozenset({"Edit", "Write", "NotebookEdit"}),
    supports_account_failover=True,
    supports_api_fallback=True,
    supports_effort=True,
    supports_resume=True,
    supports_steer=True,
    supports_live_usage=True,
    system_prompt_method="cli_flag",
)

_CURSOR = _CursorProvider(
    name="cursor",
    binary="agent",
    projects_dir_name=".cursor",
    branch_prefix="cursor-bot",
    config_dir_env="CURSOR_CONFIG_DIR",
    nested_env_vars=(),
    instruction_file=".cursor/rules",
    config_dir_name=".cursor",
    code_change_tools=frozenset({"Edit", "Write", "NotebookEdit"}),
    supports_account_failover=False,
    supports_api_fallback=False,
    supports_effort=False,
    supports_resume=False,  # disabled until confirmed
    supports_steer=False,   # requires supports_resume
    system_prompt_method="rules_dir",
)

PROVIDERS: dict[str, ProviderConfig | None] = {
    "claude": _CLAUDE,
    "cursor": _CURSOR,
    "codex": None,  # Not yet supported — event schema unverified
}


def get_provider(name: str) -> ProviderConfig:
    """Look up a provider by name.  Raises RuntimeError for unknown/unsupported."""
    if name not in PROVIDERS:
        raise RuntimeError(
            f"Unknown provider '{name}'. "
            f"Supported: {', '.join(k for k, v in PROVIDERS.items() if v)}"
        )
    provider = PROVIDERS[name]
    if provider is None:
        raise RuntimeError(
            f"Provider '{name}' is not yet supported. "
            f"Use: {', '.join(k for k, v in PROVIDERS.items() if v)}"
        )
    return provider
