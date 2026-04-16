"""Provider configuration for coding CLI tools (Claude Code, Cursor, etc.)."""

from __future__ import annotations

from dataclasses import dataclass

from bot.claude.parser import parse_usage_limit as _claude_parse_usage_limit


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

    # Feature flags — controls which Claude-specific code paths are active
    supports_account_failover: bool = False
    supports_api_fallback: bool = False
    supports_effort: bool = True
    supports_resume: bool = True

    def build_command(
        self,
        instance: object,  # Instance (avoid circular import)
        *,
        system_prompt_file: str | None,
        system_prompt_inline: str | None,
        api_fallback: bool,
        api_key_file: str | None,
    ) -> list[str]:
        """Build the CLI command args. Subclass-style dispatch via provider name.

        Returns the command list.  Prompt is always piped via stdin by the caller.
        """
        raise NotImplementedError(f"build_command not implemented for {self.name}")

    def _build_common_args(
        self,
        instance: object,
        *,
        system_prompt_file: str | None,
        system_prompt_inline: str | None,
    ) -> list[str]:
        """Shared CLI args: system prompt, resume, permissions, disallowed tools."""
        args: list[str] = []

        # System prompt
        if system_prompt_file:
            args.extend(["--append-system-prompt-file", system_prompt_file])
        elif system_prompt_inline:
            args.extend(["--append-system-prompt", system_prompt_inline])

        # Resume session
        if instance.session_id:  # type: ignore[attr-defined]
            args.extend(["--resume", instance.session_id])  # type: ignore[attr-defined]

        # Permissions: always bypass (non-interactive bot can't approve prompts)
        args.extend(["--permission-mode", "bypassPermissions"])

        # Disallowed tools (uses per-provider code_change_tools field)
        disallowed: set[str] = set()
        if instance.mode != "build":  # type: ignore[attr-defined]
            disallowed.update(self.code_change_tools)
        if not instance.is_owner_session:  # type: ignore[attr-defined]
            if instance.bash_policy == "none":  # type: ignore[attr-defined]
                disallowed.add("Bash")
        if disallowed:
            args.extend(["--disallowed-tools", ",".join(sorted(disallowed))])

        return args

    def parse_usage_limit(self, error_text: str) -> object | None:
        """Detect subscription usage-limit errors.  Returns datetime or None."""
        return None


class _ClaudeProvider(ProviderConfig):
    """Claude Code CLI provider."""

    def build_command(
        self,
        instance: object,
        *,
        system_prompt_file: str | None,
        system_prompt_inline: str | None,
        api_fallback: bool,
        api_key_file: str | None,
    ) -> list[str]:
        import json as _json
        import sys

        from bot import config

        cmd = [config.CLAUDE_BINARY, "-p"]
        cmd.extend(["--output-format", "stream-json", "--verbose"])

        if self.supports_effort:
            cmd.extend(["--effort", instance.effort])  # type: ignore[attr-defined]

        # Model override (e.g. Sonnet for explore/plan steps)
        if instance.model and not api_fallback:  # type: ignore[attr-defined]
            cmd.extend(["--model", instance.model])  # type: ignore[attr-defined]

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

        cmd.extend(self._build_common_args(
            instance,
            system_prompt_file=system_prompt_file,
            system_prompt_inline=system_prompt_inline,
        ))
        return cmd

    def parse_usage_limit(self, error_text: str) -> object | None:
        return _claude_parse_usage_limit(error_text)


class _CursorProvider(ProviderConfig):
    """Cursor CLI provider — nearly identical to Claude Code."""

    def build_command(
        self,
        instance: object,
        *,
        system_prompt_file: str | None,
        system_prompt_inline: str | None,
        api_fallback: bool,
        api_key_file: str | None,
    ) -> list[str]:
        from bot import config

        cmd = [config.CLAUDE_BINARY, "-p"]
        cmd.extend(["--output-format", "stream-json", "--verbose"])

        # Cursor --effort support unverified — skip if not confirmed
        if self.supports_effort:
            cmd.extend(["--effort", instance.effort])  # type: ignore[attr-defined]

        if instance.model and not api_fallback:  # type: ignore[attr-defined]
            cmd.extend(["--model", instance.model])  # type: ignore[attr-defined]

        # Cursor has no --bare API fallback mode — skip

        cmd.extend(self._build_common_args(
            instance,
            system_prompt_file=system_prompt_file,
            system_prompt_inline=system_prompt_inline,
        ))
        return cmd


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
)

_CURSOR = _CursorProvider(
    name="cursor",
    binary="cursor",
    projects_dir_name=".cursor",
    branch_prefix="cursor-bot",
    config_dir_env="CURSOR_CONFIG_DIR",
    nested_env_vars=(),
    instruction_file=".cursor/rules",     # TBD — may be .cursorrules
    config_dir_name=".cursor",
    code_change_tools=frozenset({"Edit", "Write", "NotebookEdit"}),
    supports_account_failover=False,
    supports_api_fallback=False,
    supports_effort=False,                # Unverified — disabled until confirmed
    supports_resume=True,
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
