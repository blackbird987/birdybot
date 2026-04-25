"""Coding CLI subprocess management — streaming, kill, semaphore, stall detection, git worktrees."""

from __future__ import annotations

import asyncio
import json as _json_mod
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from bot import config
from bot.claude.parser import (
    RunResult,
    extract_progress,
    extract_result,
    extract_summary,
    extract_usage,
    is_transient_error,
    iter_tool_blocks,
    parse_stream_line,
)
from bot.claude.provider import ProviderConfig, get_provider
from bot.claude.types import (
    Instance, InstanceOrigin, InstanceStatus, InstanceType,
)
from bot.store import history as history_mod

log = logging.getLogger(__name__)

# On Windows, prevent subprocess console windows from popping up
_NOWND: dict = config.NOWND

ProgressCallback = Callable  # async callback(message: str, detail: str)
StallCallback = Callable[[str], None]     # async callback(instance_id)


class ClaudeRunner:
    """Manages coding CLI subprocesses (Claude Code, Cursor, etc.)."""

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENT)
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        # Task-level tracking: covers the full lifecycle of a query/workflow,
        # not just the subprocess.  Prevents reboot from slipping through
        # gaps between autopilot chain steps.
        self._active_tasks: set[str] = set()
        self._active_sessions: dict[str, str] = {}  # session_id -> task_id
        self._idle_event = asyncio.Event()
        self._idle_event.set()  # starts idle

        # Reboot draining: set when a reboot is queued to block new spawns
        self._draining = False

        # Reboot coalescing: multiple instances can queue reboot requests,
        # but only one reboot executes after all tasks finish.
        self._reboot_requests: list[dict] = []
        self._reboot_executing = False
        self._on_idle_callback: Callable[[], Awaitable[None]] | None = None
        self._idle_loop: asyncio.AbstractEventLoop | None = None
        self._drain_timer_task: asyncio.Task | None = None

        # Per-repo lock: serializes git-metadata-mutating operations
        # (worktree add/remove, merge, branch delete) to prevent lock file races
        self._repo_locks: dict[str, asyncio.Lock] = {}

        # Multi-account failover: tracks when each account's usage limit resets
        self._account_cooldowns: dict[str, datetime] = {}

    @property
    def provider(self) -> ProviderConfig:
        """Current provider — re-reads config for runtime switching."""
        return get_provider(config.PROVIDER)

    async def check_cli(self) -> str:
        """Verify CLI is available. Returns version string."""
        try:
            proc = await asyncio.create_subprocess_exec(
                config.CLAUDE_BINARY, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_NOWND,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            version = stdout.decode().strip()
            if not version:
                raise RuntimeError(f"{self.provider.name} CLI returned empty version")
            return version
        except FileNotFoundError:
            raise RuntimeError(
                f"{self.provider.name} CLI not found: {config.CLAUDE_BINARY}. "
                "Ensure it's installed and in PATH."
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"{self.provider.name} CLI --version timed out")

    def _pick_account(self, exclude: set[str] | None = None) -> str | None:
        """Return the first configured account not on cooldown/excluded, or None."""
        if not config.CLAUDE_ACCOUNTS:
            return None
        now = datetime.now(timezone.utc)
        # Purge expired cooldowns
        self._account_cooldowns = {
            k: v for k, v in self._account_cooldowns.items() if v > now
        }
        exclude = exclude or set()
        for acct in config.CLAUDE_ACCOUNTS:
            if acct in exclude:
                continue
            if acct not in self._account_cooldowns:
                return acct
        return None

    async def run(
        self,
        instance: Instance,
        on_progress: ProgressCallback | None = None,
        on_stall: StallCallback | None = None,
        context: str | None = None,
        sibling_context: str | None = None,
    ) -> RunResult:
        """Run CLI for an instance. Blocks until completion or timeout."""
        async with self._semaphore:
            return await self._run_impl(
                instance, on_progress, on_stall, context, sibling_context,
                api_fallback=instance.api_fallback,
            )

    async def _run_impl(
        self,
        instance: Instance,
        on_progress: ProgressCallback | None,
        on_stall: StallCallback | None,
        context: str | None = None,
        sibling_context: str | None = None,
        api_fallback: bool = False,
        _provider: ProviderConfig | None = None,
        _binary: str | None = None,
    ) -> RunResult:
        # Snapshot provider + binary at entry — in-flight sessions keep their
        # provider even if a runtime switch happens mid-run.
        # On recursive retries, the caller passes the original snapshot.
        provider = _provider or self.provider
        binary = _binary or config.CLAUDE_BINARY

        # Git worktree isolation for build tasks
        if instance.branch:
            await self._ensure_worktree(instance, provider=provider)

        # Copy session file to worktree's project dir so --resume works
        if instance.worktree_path and instance.session_id:
            await asyncio.to_thread(
                self._copy_session_to_worktree, instance,
            )

        # Use worktree as cwd if available (file isolation)
        working_dir = instance.worktree_path or instance.repo_path or None

        cmd, prompt_text, system_prompt_file, api_key_file, rules_file = (
            self._build_command(
                instance, context, sibling_context,
                api_fallback=api_fallback, provider=provider, binary=binary,
            )
        )
        log.info("Running %s (prompt: %d chars via stdin): %s",
                 instance.id, len(prompt_text), " ".join(cmd)[:500])

        # Strip provider-specific env vars to prevent nested-session errors.
        # Strip ANTHROPIC_API_KEY on non-PPU runs so the CLI can never
        # silently bill via API; PPU runs keep it as backup for apiKeyHelper.
        env = {**os.environ}
        for var in provider.nested_env_vars:
            env.pop(var, None)
        if not api_fallback:
            env.pop("ANTHROPIC_API_KEY", None)

        # Multi-account failover (Claude-only: other providers use single account)
        account_dir: str | None = None
        if provider.supports_account_failover:
            account_dir = self._pick_account(exclude=instance._accounts_tried)
            if account_dir:
                env[provider.config_dir_env] = account_dir

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
                env=env,
                limit=10 * 1024 * 1024,  # 10MB line buffer (default 64KB too small for stream-json)
                **_NOWND,
            )
            # Register immediately so kill/cleanup works even if stdin write fails
            instance.pid = proc.pid
            self._processes[instance.id] = proc

            # Pipe user prompt via stdin — avoids Windows command-line length
            # limit (WinError 206) for long prompts / system context.
            try:
                proc.stdin.write(prompt_text.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                log.error("stdin pipe failed for %s: %s", instance.id, exc)
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return RunResult(is_error=True, error_message=f"Failed to send prompt: {exc}")

            result = await self._stream_output(
                proc, instance, on_progress, on_stall,
                supports_live_usage=provider.supports_live_usage,
            )

            # Tag result now — before any early returns — so all exit paths carry the flag.
            result.api_fallback_used = api_fallback

            # Dead session: clear session_id and retry without --resume
            error_text = result.error_message or result.result_text or ""
            if result.is_error and "No conversation found" in error_text and instance.session_id:
                log.warning("Session %s not found for %s, retrying without resume", instance.session_id, instance.id)
                instance.session_id = None
                return await self._run_impl(instance, on_progress, on_stall, context, sibling_context, api_fallback=api_fallback, _provider=provider, _binary=binary)

            # Usage limit: try next account before falling through to cooldown/PPU
            if result.is_error:
                reset_at = provider.parse_usage_limit(error_text)
                if reset_at:
                    log.info("Usage limit for %s, resets at %s", instance.id, reset_at)
                    # Mark this account as on cooldown
                    if account_dir:
                        self._account_cooldowns[account_dir] = reset_at
                        instance._accounts_tried.add(account_dir)
                    # Try next account before giving up
                    next_account = self._pick_account(exclude=instance._accounts_tried)
                    if next_account:
                        log.info("Failing over from %s to %s for %s",
                                 account_dir, next_account, instance.id)
                        # Clear session — it belongs to the exhausted account
                        instance.session_id = None
                        if on_progress:
                            try:
                                await on_progress(
                                    "Switching to backup account",
                                    "Primary account hit usage limit",
                                )
                            except Exception:
                                log.exception("Progress callback error during failover")
                        return await self._run_impl(
                            instance, on_progress, on_stall,
                            context, sibling_context,
                            api_fallback=api_fallback,
                            _provider=provider, _binary=binary,
                        )
                    # All accounts exhausted — fall through to cooldown/PPU
                    result.usage_limit_reset = reset_at
                    return result

            # Auto-retry on transient errors (skip if already in API fallback to prevent loops)
            if (result.is_error
                and not api_fallback
                and is_transient_error(result.error_message or result.result_text)
                and instance.retry_count == 0):
                log.info("Transient error for %s, retrying in 30s", instance.id)
                instance.retry_count = 1
                await asyncio.sleep(30)
                return await self._run_impl(instance, on_progress, on_stall, context, sibling_context, api_fallback=False, _provider=provider, _binary=binary)

            return result

        except Exception as e:
            log.exception("Error running %s", instance.id)
            return RunResult(is_error=True, error_message=str(e))
        finally:
            # Clean up temp files on all exit paths
            for tmp in (system_prompt_file, api_key_file):
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            # Clean up Cursor rules file if created
            if rules_file:
                try:
                    os.unlink(rules_file)
                except OSError:
                    pass
            self._processes.pop(instance.id, None)
            # Kill process on cancellation/unexpected error to avoid orphans
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            if not self._active_tasks and not self._processes:
                self._idle_event.set()

    async def _stream_output(
        self,
        proc: asyncio.subprocess.Process,
        instance: Instance,
        on_progress: ProgressCallback | None,
        on_stall: StallCallback | None,
        *,
        supports_live_usage: bool = False,
    ) -> RunResult:
        """Read stdout line-by-line, parse stream-json, detect stalls.

        No inactivity timeout — processes run until they finish or the user
        kills them.  A safety-net lifetime limit (default 4h) catches truly
        orphaned processes.
        """
        events: list[dict] = []
        captured_session_id: str | None = None
        ask_question: str | None = None  # Set when AskUserQuestion detected
        last_output_time = asyncio.get_event_loop().time()
        process_start_time = last_output_time
        stall_warned = False
        lifetime_exceeded = False
        stall_check_task: asyncio.Task | None = None

        async def check_stall():
            nonlocal stall_warned, lifetime_exceeded
            while True:
                await asyncio.sleep(10)
                now = asyncio.get_event_loop().time()
                elapsed_since_output = now - last_output_time
                elapsed_since_start = now - process_start_time

                # Safety-net: kill truly orphaned processes
                if elapsed_since_start > config.MAX_PROCESS_LIFETIME_SECS:
                    lifetime_exceeded = True
                    log.warning(
                        "Lifetime limit for %s — running for %ds",
                        instance.id, int(elapsed_since_start),
                    )
                    proc.terminate()
                    return

                # Stall warning (no auto-kill)
                if elapsed_since_output > config.STALL_TIMEOUT_SECS and not stall_warned:
                    stall_warned = True
                    if on_stall:
                        try:
                            await on_stall(instance.id)
                        except Exception:
                            log.exception("Stall callback error")

        stall_check_task = asyncio.create_task(check_stall())

        try:
            assert proc.stdout is not None
            while True:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=5
                    )
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    continue
                except (ValueError, asyncio.LimitOverrunError):
                    log.warning("Oversized line from %s (>10MB), skipping", instance.id)
                    proc.terminate()
                    break

                if not line:
                    break

                last_output_time = asyncio.get_event_loop().time()
                stall_warned = False

                decoded = line.decode("utf-8", errors="replace")
                event = parse_stream_line(decoded)
                if event:
                    events.append(event)
                    # Eagerly capture session_id so it survives early termination
                    if not captured_session_id and event.get("session_id"):
                        captured_session_id = event["session_id"]
                    # Per-turn usage (Claude only) — fed to progress callback
                    # so the lifecycle layer can keep the context footer live.
                    usage = extract_usage(event) if supports_live_usage else None

                    if on_progress:
                        progress = extract_progress(event)
                        if progress and progress.message:
                            try:
                                if usage is not None:
                                    try:
                                        await on_progress(
                                            progress.message, progress.detail,
                                            usage=usage,
                                        )
                                    except TypeError:
                                        # Older callback signature — fall back.
                                        await on_progress(
                                            progress.message, progress.detail,
                                        )
                                else:
                                    await on_progress(progress.message, progress.detail)
                            except Exception:
                                log.exception("Progress callback error")
                        elif usage is not None:
                            # No visible progress, but refresh the cached usage
                            # so the heartbeat renders the latest context count.
                            try:
                                await on_progress("", "", usage=usage)
                            except TypeError:
                                pass
                            except Exception:
                                log.exception("Progress callback error (usage-only)")

                    # Detect AskUserQuestion — Claude is blocking on stdin
                    if ask_question is None:
                        for tool_name, tool_input in iter_tool_blocks(event):
                            if tool_name == "AskUserQuestion":
                                ask_question = tool_input.get("question", "")
                                log.warning(
                                    "AskUserQuestion detected for %s: %.100s",
                                    instance.id, ask_question,
                                )
                                proc.terminate()
                                break
                        if ask_question is not None:
                            break
        finally:
            if stall_check_task:
                stall_check_task.cancel()
                try:
                    await stall_check_task
                except asyncio.CancelledError:
                    pass

        # AskUserQuestion — wait for process to die, then return question as result
        if ask_question is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            result = extract_result(events)
            result.needs_input = True
            result.is_error = False
            if ask_question:
                result.result_text = ask_question
            elif not result.result_text:
                result.result_text = "Claude is asking a question (text not captured)"
            if not result.session_id:
                result.session_id = captured_session_id
            if result.result_text:
                result_path = config.RESULTS_DIR / f"{instance.id}.md"
                result_path.write_text(result.result_text, encoding="utf-8")
                instance.result_file = str(result_path)
                instance.summary = extract_summary(result.result_text)
            return result

        await proc.wait()

        if lifetime_exceeded:
            return RunResult(
                session_id=captured_session_id,
                is_error=True,
                error_message=f"Process exceeded {config.MAX_PROCESS_LIFETIME_SECS // 3600}h lifetime limit",
            )

        # Capture stderr for error info
        stderr_text = ""
        if proc.stderr:
            stderr_data = await proc.stderr.read()
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip()

        result = extract_result(events)

        if proc.returncode != 0 and not result.is_error:
            result.is_error = True
            if not result.error_message:
                result.error_message = stderr_text or f"Exit code {proc.returncode}"

        if result.is_error:
            # Log raw events for debugging
            import json as _json
            event_dump = _json.dumps(events[-5:], default=str)[:1000] if events else "no events"
            log.warning(
                "CLI error for %s (exit=%s): %s | stderr: %s | last events: %s",
                instance.id, proc.returncode,
                result.error_message or result.result_text or "no message",
                stderr_text[:500] if stderr_text else "empty",
                event_dump,
            )

        # Save result file
        if result.result_text:
            result_path = config.RESULTS_DIR / f"{instance.id}.md"
            result_path.write_text(result.result_text, encoding="utf-8")
            instance.result_file = str(result_path)

        # Save git diff for build tasks
        if instance.branch and not result.is_error:
            await self._save_diff(instance)

        # Copy session files back from worktree to main repo project dir
        if instance.worktree_path:
            await asyncio.to_thread(self._copy_session_from_worktree, instance)

        # Extract summary
        instance.summary = extract_summary(result.result_text)

        # Capture the JSONL uuid of the final assistant message so the
        # "Branch from here" button can fork the session at this point.
        session_id = result.session_id or captured_session_id
        if session_id and instance.repo_path:
            try:
                from bot.engine.session_fork import (
                    encode_project_path, get_last_assistant_uuid,
                )
                proj = config.CLAUDE_PROJECTS_DIR / encode_project_path(instance.repo_path)
                jsonl = proj / f"{session_id}.jsonl"
                last_uuid = await asyncio.to_thread(get_last_assistant_uuid, jsonl)
                if last_uuid:
                    result.last_assistant_uuid = last_uuid
            except Exception:
                log.exception("last_assistant_uuid capture failed for %s", instance.id)

        return result

    def _build_command(
        self, instance: Instance,
        context: str | None = None,
        sibling_context: str | None = None,
        api_fallback: bool = False,
        provider: ProviderConfig | None = None,
        binary: str | None = None,
    ) -> tuple[list[str], str, str | None, str | None, str | None]:
        """Build CLI command and prompt.  Prompt returned separately for stdin piping.

        Returns (cmd, prompt_text, system_prompt_file, api_key_file, rules_file)
        — caller must clean up the temp files via os.unlink when done.
        """
        provider = provider or self.provider

        # Build prompt — never mutated on the instance
        prompt = instance.prompt
        if not prompt:
            prompt = "Continue the previous conversation."

        # API key file (only for providers that support API fallback)
        api_key_file: str | None = None
        if api_fallback and provider.supports_api_fallback and config.ANTHROPIC_API_KEY:
            api_key_file = self._write_api_key_file(config.ANTHROPIC_API_KEY)
            if not api_key_file:
                log.warning("API key file write failed for %s, skipping API fallback", instance.id)

        # Build system prompt: mobile hint + bot context + pinned context + repo instruction file + projects dir
        system_prompt = self._build_system_prompt(instance, context, sibling_context, provider=provider)

        # Dispatch system prompt delivery based on provider method
        sp_file: str | None = None
        system_prompt_inline: str | None = None
        rules_file: str | None = None

        if provider.system_prompt_method == "rules_dir":
            # Write to workspace .cursor/rules/_bot_system.mdc — Cursor loads
            # all files in this dir automatically.
            working_dir = instance.worktree_path or instance.repo_path
            if working_dir and system_prompt:
                rules_dir = Path(working_dir) / provider.config_dir_name / "rules"
                rules_dir.mkdir(parents=True, exist_ok=True)
                rf = rules_dir / "_bot_system.mdc"
                try:
                    rf.write_text(system_prompt, encoding="utf-8")
                    rules_file = str(rf)
                    log.debug("Wrote system prompt to %s", rf)
                except OSError:
                    log.warning("Failed to write rules file %s, system prompt skipped", rf)
            # sp_file and system_prompt_inline stay None — not passed to build_command
        else:
            # Claude path: write to temp file, fall back to inline arg
            sp_file = self._write_system_prompt_file(system_prompt)
            if not sp_file:
                max_inline = 30_000
                if len(system_prompt) > max_inline:
                    log.warning("System prompt file write failed, truncating to %d chars", max_inline)
                    system_prompt = system_prompt[:max_inline]
                system_prompt_inline = system_prompt

        # Delegate CLI arg assembly to the provider
        cmd = provider.build_command(
            instance,
            binary=binary,
            system_prompt_file=sp_file,
            system_prompt_inline=system_prompt_inline,
            api_fallback=api_fallback,
            api_key_file=api_key_file,
        )

        # Prompt piped via stdin (not CLI arg) to avoid Windows command-line
        # length limit (WinError 206).  No "--" separator needed.
        return cmd, prompt, sp_file, api_key_file, rules_file

    @staticmethod
    def _write_system_prompt_file(content: str) -> str | None:
        """Write system prompt to a temp file, return path.  Returns None on failure."""
        path: str | None = None
        try:
            f = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", prefix="claude_sysprompt_",
                delete=False, encoding="utf-8",
            )
            path = f.name
            f.write(content)
            f.close()
            return path
        except OSError:
            log.warning("Failed to write system prompt temp file, falling back to inline")
            # Clean up partial file if it was created
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            return None

    @staticmethod
    def _write_api_key_file(api_key: str) -> str | None:
        """Write API key to a temp file for apiKeyHelper. Returns path or None."""
        path: str | None = None
        try:
            f = tempfile.NamedTemporaryFile(
                mode="w", suffix=".key", prefix="claude_apikey_",
                delete=False, encoding="utf-8",
            )
            path = f.name
            f.write(api_key)
            f.close()
            return path
        except OSError:
            log.warning("Failed to write API key temp file")
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            return None

    @staticmethod
    def _diagnostics_enabled(instance: Instance) -> bool:
        """Check if diagnostic scaffolding is enabled for this repo."""
        if not instance.repo_path:
            return True  # default on
        test_json = Path(instance.repo_path) / ".claude" / "test.json"
        if not test_json.exists():
            return True  # no config = default on
        try:
            cfg = _json_mod.loads(test_json.read_text(encoding="utf-8"))
            return cfg.get("diagnostics", True)
        except Exception:
            return True

    def _build_system_prompt(
        self, instance: Instance,
        context: str | None = None,
        sibling_context: str | None = None,
        provider: ProviderConfig | None = None,
    ) -> str:
        """Build the system prompt with mobile hint, bot context, pinned context, repo CLAUDE.md, and projects dir."""
        provider = provider or self.provider
        parts = [config.MOBILE_HINT]

        # Explain that user can only see text output (critical for good responses)
        parts.append(config.CHAT_APP_CONSTRAINT)

        # Honesty & verification guidance
        parts.append(config.HONESTY_CONSTRAINT)

        # Bot capability context so Claude knows what the user can do
        parts.append(config.BOT_CONTEXT)

        # Universal working context — workflow, Discord UI, branch model, design principles
        parts.append(config.WORKING_CONTEXT)

        # Outlook integration (optional — only when enabled + pywin32 present)
        if config.OUTLOOK_ENABLED:
            try:
                from bot.services.outlook import COM_AVAILABLE

                if COM_AVAILABLE:
                    script = config._PROJECT_ROOT / "bot" / "services" / "outlook.py"
                    parts.append(
                        f"\n\n--- Outlook Integration ---\n"
                        f"You can read the user's Outlook email and calendar. Commands:\n"
                        f'  python "{script}" inbox [count]      — recent emails\n'
                        f'  python "{script}" calendar [days]     — upcoming events\n'
                        f'  python "{script}" search "query" [count] — search by subject/sender\n'
                        f'  python "{script}" unread              — unread count\n'
                        f'  python "{script}" read "subject"      — full email by subject\n'
                        f"Run these via the Bash tool when the user asks about their email or calendar."
                    )
            except ImportError:
                pass

        # Per-step behavioral guidance based on workflow origin
        origin_key = instance.origin.value if instance.origin else "direct"
        guidance = config.WORKFLOW_GUIDANCE.get(origin_key)
        if guidance:
            parts.append(f"\n\n--- Your Role in This Step ---\n{guidance}")

        # Inject diagnostic scaffolding guidance for build steps
        if origin_key == "build" and self._diagnostics_enabled(instance):
            parts.append(config.DIAGNOSTIC_GUIDANCE)

        # Verify Board emission guidance — opt-in per build-origin session.
        # Keeps prompt footprint off plan/explore steps where items never apply.
        if origin_key in ("build", "build_and_ship", "apply_revisions", "done"):
            parts.append(config.VERIFY_BOARD_GUIDANCE)

        # Plan mode: instruct Claude not to modify files, output a plan instead
        if instance.mode == "plan":
            parts.append(config.PLAN_MODE_CONSTRAINT)

        # Pinned user context (passed as parameter to avoid double-prepend on retry)
        if context:
            parts.append(f"\n\nUser context: {context}")

        repo_path = instance.repo_path
        if repo_path:
            # Include repo instruction file if it exists (e.g. .claude/CLAUDE.md)
            # Skip if path is a directory (e.g. .cursor/rules — handled via rules_dir)
            instruction_path = Path(repo_path) / provider.instruction_file
            if instruction_path.is_file():
                try:
                    content = instruction_path.read_text(encoding="utf-8")
                    parts.append(
                        f"\n\n--- Repository Instructions (from {provider.instruction_file}) ---\n"
                        f"{content}"
                    )
                except Exception:
                    log.warning("Failed to read %s from %s", provider.instruction_file, instruction_path)

            # Point to the projects directory for plans/sessions
            projects_dir = self._get_projects_dir(repo_path)
            if projects_dir and projects_dir.exists():
                parts.append(
                    f"\n\nCLI session history and plans for this repo "
                    f"are stored in: {projects_dir}"
                )

        # Non-owner user context: scope awareness + bash policy
        if not instance.is_owner_session:
            user_label = instance.user_name or instance.user_id or "a granted user"
            access_parts = [
                f"\n\n--- Access Control ---",
                f"This session is being used by {user_label} (not the repo owner).",
                f"Mode: {instance.mode}.",
            ]
            if repo_path:
                access_parts.append(
                    f"You are working in {repo_path}. Do not navigate outside "
                    f"this directory or read files outside it."
                )
            if instance.bash_policy == "allowlist" and instance.mode != "build":
                from bot.discord.access import DEFAULT_BASH_ALLOWLIST, DEFAULT_BASH_DENYLIST
                access_parts.append(
                    f"\nBash commands are restricted. Allowed: {', '.join(DEFAULT_BASH_ALLOWLIST)}."
                    f"\nDenied: {', '.join(DEFAULT_BASH_DENYLIST)}."
                    f"\nDo not run commands outside this allowlist."
                )
            elif instance.bash_policy == "none" and instance.mode != "build":
                access_parts.append(
                    "\nBash tool is disabled. Use Read, Grep, Glob for exploration."
                )
            parts.append("\n".join(access_parts))

        # Recent session history — enables smart recall of past work
        if instance.repo_name:
            recent = history_mod.load_recent(
                repo=instance.repo_name, limit=20, dedupe_thread=True,
            )
            if recent:
                lines = []
                for e in recent:
                    eid = e.get("id", "?")
                    topic = e.get("topic", "")[:80]
                    status = e.get("status", "?")
                    finished = e.get("finished", "")
                    branch = e.get("branch")
                    summary = e.get("summary", "")[:120]

                    age = ""
                    if finished:
                        try:
                            dt = datetime.fromisoformat(finished)
                            delta = datetime.now(timezone.utc) - dt
                            if delta.days > 0:
                                age = f"{delta.days}d ago"
                            else:
                                hours = delta.seconds // 3600
                                age = f"{hours}h ago" if hours else f"{delta.seconds // 60}m ago"
                        except Exception:
                            pass

                    line = f'- [{eid}] "{topic}" — {status} {age}'
                    if branch:
                        line += f" (branch: {branch})"
                    if summary:
                        line += f"\n  Summary: {summary}"
                    lines.append(line)

                history_block = "\n".join(lines)
                # Cap to ~4K to keep total command line under Windows limits
                if len(history_block) > 4000:
                    history_block = history_block[:4000] + "\n... (truncated)"
                parts.append(
                    "\n\n--- Recent Sessions (this project) ---\n"
                    + history_block
                )

        # Sibling session awareness — helps Claude avoid file conflicts
        if sibling_context:
            parts.append(
                f"\n\n--- Sibling Sessions ---\n{sibling_context}\n"
                "Avoid editing files these sessions are likely working on."
            )

        return "".join(parts)

    @staticmethod
    def _get_projects_dir(repo_path: str) -> Path | None:
        """Get the CLI projects directory for a repo path.

        Session data is stored in ~/<provider_dir>/projects/<sanitized-path>/
        where the path has : and separators replaced with dashes.
        """
        try:
            # Sanitize: replace : \ / with -
            sanitized = repo_path.replace(":", "-").replace("\\", "-").replace("/", "-")
            return config.CLAUDE_PROJECTS_DIR / sanitized
        except Exception:
            return None

    # --- Task-level tracking ---

    def begin_task(self, instance_id: str, session_id: str | None = None) -> None:
        """Mark a high-level task (query/workflow chain) as active."""
        self._active_tasks.add(instance_id)
        if session_id:
            self._active_sessions[session_id] = instance_id
        self._idle_event.clear()

    def end_task(self, instance_id: str) -> None:
        """Mark a high-level task as completed.

        If all tasks are done and reboot requests are pending, fires the
        idle callback (exactly once) to execute the coalesced reboot.
        """
        self._active_tasks.discard(instance_id)
        # Clean up session tracking
        stale = [s for s, t in self._active_sessions.items() if t == instance_id]
        for s in stale:
            del self._active_sessions[s]
        if not self._active_tasks and not self._processes:
            self._idle_event.set()
            self._maybe_fire_idle_reboot()

    @property
    def is_busy(self) -> bool:
        """Whether any tasks or processes are active."""
        return bool(self._active_tasks) or bool(self._processes)

    @property
    def active_task_count(self) -> int:
        """Number of active high-level tasks."""
        return len(self._active_tasks)

    @property
    def active_count(self) -> int:
        """Number of currently running CLI processes."""
        return len(self._processes)

    @property
    def active_ids(self) -> list[str]:
        """IDs of currently running instances."""
        return list(self._processes.keys())

    @property
    def is_draining(self) -> bool:
        """Whether a reboot is pending and no new work should start."""
        return self._draining

    def is_session_active(self, session_id: str | None) -> bool:
        """Check if a task is currently running for this session."""
        if not session_id:
            return False
        return session_id in self._active_sessions

    def active_instance_for_session(self, session_id: str | None) -> str | None:
        """Return instance_id of the currently-running task for a session, or None."""
        if not session_id:
            return None
        return self._active_sessions.get(session_id)

    def check_spawn_allowed(self, session_id: str | None = None) -> str | None:
        """Return an error message if spawning is blocked, or None if OK.

        Active-session case is no longer rejected here — the per-channel lock
        in ``bot.engine.commands._get_channel_lock`` serializes same-channel
        spawns cleanly and the Queued-embed UX handles it visibly.  We only
        reject during reboot drain.
        """
        if self._draining:
            return "Reboot in progress — try again shortly."
        return None

    async def wait_until_idle(self, timeout: float = 300) -> bool:
        """Wait until no tasks or processes are running."""
        try:
            await asyncio.wait_for(self._idle_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # --- Reboot coalescing ---

    def request_reboot(self, data: dict) -> None:
        """Queue a reboot request and trigger execution if already idle.

        Safe to call from both task context (check_reboot_request — fires
        after end_task) and non-task context (on_reboot — fires immediately).
        Sets draining flag to block new spawns while waiting for idle.
        """
        self._draining = True
        self._reboot_requests.append(data)
        self._maybe_fire_idle_reboot()
        self._start_drain_timer()

    def pending_reboots(self) -> list[dict]:
        """Return a copy of pending reboot requests."""
        return list(self._reboot_requests)

    def clear_reboots(self) -> None:
        """Clear all pending reboot requests and reset draining flag.

        Does NOT delete drain_queue.json — the new process needs it after
        a successful reboot.  Call purge_drain_queue() explicitly when a
        reboot is *aborted* and queued messages should be discarded.
        """
        self._reboot_requests.clear()
        self._draining = False
        if self._drain_timer_task and not self._drain_timer_task.done():
            self._drain_timer_task.cancel()
            self._drain_timer_task = None

    @staticmethod
    def purge_drain_queue() -> None:
        """Delete the drain queue file (e.g. when reboot is aborted)."""
        try:
            config.DRAIN_QUEUE_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    def queue_for_replay(self, entry: dict) -> None:
        """Persist a user request to be replayed after reboot completes."""
        queue: list[dict] = []
        try:
            data = _json_mod.loads(config.DRAIN_QUEUE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                queue = data
        except (FileNotFoundError, _json_mod.JSONDecodeError):
            pass
        queue.append(entry)
        config.DRAIN_QUEUE_FILE.write_text(
            _json_mod.dumps(queue, indent=2), encoding="utf-8",
        )
        log.info("Queued message for post-reboot replay: channel=%s prompt=%s",
                 entry.get("channel_id"), (entry.get("prompt") or "")[:60])

    @staticmethod
    def read_drain_queue() -> list[dict]:
        """Read and delete the drain queue file. Returns [] if absent."""
        try:
            data = _json_mod.loads(config.DRAIN_QUEUE_FILE.read_text(encoding="utf-8"))
            config.DRAIN_QUEUE_FILE.unlink(missing_ok=True)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, _json_mod.JSONDecodeError):
            return []

    def set_on_idle_reboot(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register async callback invoked when last task ends and reboots are pending.

        Captures the running loop for safe task scheduling from sync end_task().
        Must be called from an async context (e.g. during startup).
        """
        self._on_idle_callback = callback
        self._idle_loop = asyncio.get_running_loop()

    def _maybe_fire_idle_reboot(self) -> None:
        """Schedule the reboot callback if idle + reboots pending + not already running."""
        if (self._on_idle_callback
                and self._reboot_requests
                and not self._reboot_executing
                and not self._active_tasks
                and not self._processes
                and self._idle_loop):
            self._reboot_executing = True
            self._idle_loop.call_soon(
                lambda: self._idle_loop.create_task(self._on_idle_callback())
            )

    def _start_drain_timer(self) -> None:
        """Start a background timer that force-kills processes after drain timeout."""
        if self._drain_timer_task and not self._drain_timer_task.done():
            return  # Timer already running
        if not self._idle_loop:
            log.warning("No event loop for drain timer — reboot may hang")
            return
        log.info(
            "Reboot drain started — will force-kill in %ds if not idle",
            config.REBOOT_DRAIN_TIMEOUT_SECS,
        )
        self._drain_timer_task = self._idle_loop.create_task(self._drain_timeout())

    async def _drain_timeout(self) -> None:
        """Wait for drain timeout, then force-kill remaining processes and tasks."""
        try:
            await asyncio.sleep(config.REBOOT_DRAIN_TIMEOUT_SECS)
            if not self._reboot_requests:
                return  # Reboot already executed normally
            if not self._active_tasks and not self._processes:
                return  # Already idle
            log.warning(
                "Reboot drain timed out after %ds — killing %d tasks, %d processes",
                config.REBOOT_DRAIN_TIMEOUT_SECS,
                len(self._active_tasks),
                len(self._processes),
            )
            await self.kill_all()
            self.force_clear_tasks()
            self._maybe_fire_idle_reboot()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Drain timeout error")

    def force_clear_tasks(self) -> int:
        """Clear all active tasks regardless of process state. Returns count cleared.

        Use after kill_all() to unstick orphaned chain tasks.
        """
        count = len(self._active_tasks)
        if count:
            log.warning(
                "Force-clearing %d orphaned tasks: %s",
                count, list(self._active_tasks),
            )
        self._active_tasks.clear()
        self._active_sessions.clear()
        self._idle_event.set()
        return count

    async def kill(self, instance_id: str) -> bool:
        """Terminate a running CLI process."""
        proc = self._processes.get(instance_id)
        if not proc:
            return False
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            return True
        except Exception:
            log.exception("Error killing process %s", instance_id)
            return False
        finally:
            self._processes.pop(instance_id, None)
            if not self._active_tasks and not self._processes:
                self._idle_event.set()

    async def kill_and_wait(
        self, instance_id: str, timeout: float = 10.0,
    ) -> bool:
        """Kill an instance and wait for its lifecycle task to fully finish.

        Used by Steer: the caller needs the channel lock released (finalize
        done, session file copied back, progress edit applied) before spawning
        a replacement run.  ``kill()`` alone only handles the subprocess —
        it doesn't wait for the surrounding ``run_instance`` coroutine.

        Escalation path: after the inner ``kill()`` returns (which already
        does terminate → 5s wait → SIGKILL), poll for ``_active_tasks`` to
        drop the instance_id for up to *timeout* seconds.  On timeout,
        force-clear the task so the channel lock never stays wedged.
        """
        if instance_id not in self._active_tasks and instance_id not in self._processes:
            return False
        await self.kill(instance_id)
        # Wait for the lifecycle coroutine to finish its finally block
        # (end_task fires there and removes the instance from _active_tasks).
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while instance_id in self._active_tasks:
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.warning(
                    "kill_and_wait timed out after %.1fs for %s — force-clearing",
                    timeout, instance_id,
                )
                self._active_tasks.discard(instance_id)
                stale = [
                    s for s, t in self._active_sessions.items() if t == instance_id
                ]
                for s in stale:
                    del self._active_sessions[s]
                if not self._active_tasks and not self._processes:
                    self._idle_event.set()
                return True
            await asyncio.sleep(min(0.1, remaining))
        return True

    async def kill_all(self) -> int:
        """Terminate all running processes. Returns count killed."""
        killed = 0
        for iid in list(self._processes.keys()):
            if await self.kill(iid):
                killed += 1
        return killed

    def queue_position(self, instance_id: str) -> int | None:
        """Approximate queue position (not exact with asyncio.Semaphore)."""
        # Semaphore doesn't expose waiter count directly
        waiters = getattr(self._semaphore, '_waiters', None)
        if waiters is None:
            return None
        return len(waiters)

    # --- Per-repo locking ---

    def _get_repo_lock(self, repo_path: str) -> asyncio.Lock:
        """Get or create a per-repo lock for serializing git admin operations."""
        if repo_path not in self._repo_locks:
            self._repo_locks[repo_path] = asyncio.Lock()
        return self._repo_locks[repo_path]

    # --- Git worktree management ---

    async def _ensure_worktree(self, instance: Instance, provider: ProviderConfig | None = None) -> None:
        """Create a git worktree for build isolation (idempotent)."""
        if not instance.repo_path:
            return
        # If worktree already exists (copy_branch from parent), skip creation
        if instance.worktree_path and Path(instance.worktree_path).is_dir():
            return
        # worktree_path is set but directory is gone (parent was cleaned up) —
        # clear it so _create_worktree_sync creates a fresh one
        if instance.worktree_path:
            log.warning("Worktree %s no longer exists for %s, recreating",
                        instance.worktree_path, instance.id)
            instance.worktree_path = None
        repo_lock = self._get_repo_lock(instance.repo_path)
        async with repo_lock:
            await asyncio.to_thread(self._create_worktree_sync, instance, provider)

    def _create_worktree_sync(self, instance: Instance, provider: ProviderConfig | None = None) -> None:
        provider = provider or self.provider
        repo = instance.repo_path
        wt_dir = str(Path(repo) / ".worktrees" / instance.id)
        branch = instance.branch
        default_branch = self._get_default_branch(repo)

        # Idempotent: skip if worktree already exists
        if Path(wt_dir).is_dir():
            instance.worktree_path = wt_dir
            instance.original_branch = default_branch
            return

        try:
            # Create .worktrees/ parent if needed
            Path(wt_dir).parent.mkdir(parents=True, exist_ok=True)

            # Create worktree with a new branch from current HEAD (master/main)
            result = subprocess.run(
                ["git", "worktree", "add", wt_dir, "-b", branch],
                cwd=repo, capture_output=True, text=True, **_NOWND,
            )
            if result.returncode != 0:
                # Branch might already exist (retry/resume) — try without -b
                subprocess.run(
                    ["git", "worktree", "add", wt_dir, branch],
                    cwd=repo, capture_output=True, text=True, check=True, **_NOWND,
                )

            instance.worktree_path = wt_dir
            instance.original_branch = default_branch

            # Copy provider config dir into worktree so CLI finds
            # instruction files and project settings (worktrees have a .git
            # file, not directory — some tools may not follow it correctly)
            cfg_dir = provider.config_dir_name
            try:
                src_cfg_dir = Path(repo) / cfg_dir
                dst_cfg_dir = Path(wt_dir) / cfg_dir
                if src_cfg_dir.is_dir() and not dst_cfg_dir.exists():
                    shutil.copytree(str(src_cfg_dir), str(dst_cfg_dir),
                                    ignore=shutil.ignore_patterns("*.jsonl"))
                    log.debug("Copied %s/ into worktree %s", cfg_dir, wt_dir)
            except Exception:
                log.warning("Failed to copy %s/ into worktree %s", cfg_dir, wt_dir, exc_info=True)

            log.info("Created worktree %s (branch %s) in %s", wt_dir, branch, repo)
        except subprocess.CalledProcessError as e:
            log.error("Failed to create worktree: %s", e.stderr)
            raise RuntimeError(f"Failed to create worktree: {e.stderr}")

    @staticmethod
    def _get_default_branch(repo_path: str) -> str:
        """Determine the default branch (master or main)."""
        for candidate in ("master", "main"):
            r = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/heads/{candidate}"],
                cwd=repo_path, capture_output=True, text=True, **_NOWND,
            )
            if r.returncode == 0:
                return candidate
        # Fallback: use HEAD if it's not a bot-managed branch
        _prefix = f"{config.BRANCH_PREFIX}/"
        r = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, **_NOWND,
        )
        if r.returncode == 0:
            head = r.stdout.strip()
            if head and not head.startswith(_prefix):
                return head
        # Last resort: find any non-bot-managed branch
        r = subprocess.run(
            ["git", "branch", "--format=%(refname:short)"],
            cwd=repo_path, capture_output=True, text=True, **_NOWND,
        )
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                branch = line.strip()
                if branch and not branch.startswith(_prefix):
                    return branch
        log.warning("No default branch in %s — every branch is bot-managed",
                     repo_path)
        return "master"

    # --- Session file management (worktree ↔ main repo) ---

    @staticmethod
    def _encode_project_path(path: str) -> str:
        """Encode path the same way the CLI does for project dirs.

        Delegates to the shared implementation in ``bot.engine.session_fork``
        so both the runner and the JSONL forker agree on project-dir names.
        """
        from bot.engine.session_fork import encode_project_path
        return encode_project_path(path)

    def _copy_session_to_worktree(self, instance: Instance) -> None:
        """Copy session JSONL from main repo's project dir to worktree's project dir."""
        if not instance.repo_path or not instance.worktree_path or not instance.session_id:
            return
        repo_encoded = self._encode_project_path(instance.repo_path)
        wt_encoded = self._encode_project_path(instance.worktree_path)
        if repo_encoded == wt_encoded:
            return  # Same path, no copy needed

        src_dir = config.CLAUDE_PROJECTS_DIR / repo_encoded
        dst_dir = config.CLAUDE_PROJECTS_DIR / wt_encoded
        src_file = src_dir / f"{instance.session_id}.jsonl"

        if not src_file.exists():
            log.debug("No session file to copy for %s", instance.session_id[:12])
            return

        dst_dir.mkdir(parents=True, exist_ok=True)
        dst_file = dst_dir / f"{instance.session_id}.jsonl"
        shutil.copy2(str(src_file), str(dst_file))
        log.info("Copied session %s to worktree project dir", instance.session_id[:12])

    def _copy_session_from_worktree(self, instance: Instance) -> None:
        """Copy session JSONL back from worktree's project dir to main repo's project dir.

        Also handles the case where the CLI created a NEW session (different ID).
        """
        if not instance.repo_path or not instance.worktree_path:
            return
        wt_encoded = self._encode_project_path(instance.worktree_path)
        repo_encoded = self._encode_project_path(instance.repo_path)
        if wt_encoded == repo_encoded:
            return

        wt_proj_dir = config.CLAUDE_PROJECTS_DIR / wt_encoded
        repo_proj_dir = config.CLAUDE_PROJECTS_DIR / repo_encoded

        if not wt_proj_dir.is_dir():
            return

        # Copy all .jsonl files (handles new session IDs)
        repo_proj_dir.mkdir(parents=True, exist_ok=True)
        for f in wt_proj_dir.glob("*.jsonl"):
            dst = repo_proj_dir / f.name
            shutil.copy2(str(f), str(dst))
            log.debug("Copied session file %s back to main repo project dir", f.name)

    # --- Diff ---

    async def _save_diff(self, instance: Instance) -> None:
        """Save git diff for a build task."""
        if not instance.branch:
            return
        if not instance.worktree_path and not instance.repo_path:
            return
        await asyncio.to_thread(self._save_diff_sync, instance)

    def _save_diff_sync(self, instance: Instance) -> None:
        try:
            # Diff runs in worktree (where changes are) against the merge base
            diff_cwd = instance.worktree_path or instance.repo_path
            base = instance.original_branch or "HEAD~1"
            result = subprocess.run(
                ["git", "diff", base, "--", "."],
                cwd=diff_cwd, capture_output=True, text=True, **_NOWND,
            )
            if (result.stdout or "").strip():
                diff_path = config.RESULTS_DIR / f"{instance.id}.diff"
                diff_path.write_text(result.stdout, encoding="utf-8")
                instance.diff_file = str(diff_path)
        except Exception:
            log.exception("Failed to save diff for %s", instance.id)

    # --- Merge / Discard ---

    async def merge_branch(self, instance: Instance) -> str:
        """Merge worktree branch into master. Returns status message."""
        if not instance.branch or not instance.original_branch:
            if instance.original_branch and not instance.branch:
                return f"Already merged ({config.BRANCH_PREFIX}/{instance.id} → {instance.original_branch})"
            return "No branch to merge"
        if not instance.repo_path:
            return "No repo path"
        repo_lock = self._get_repo_lock(instance.repo_path)
        async with repo_lock:
            return await asyncio.to_thread(self._merge_branch_sync, instance)

    def _merge_branch_sync(self, instance: Instance) -> str:
        stashed = False
        repo = instance.repo_path
        target = instance.original_branch
        try:
            # Copy session files back before cleanup
            self._copy_session_from_worktree(instance)

            # Re-verify original_branch exists; re-detect if stale
            r = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/heads/{target}"],
                cwd=repo, capture_output=True, text=True, **_NOWND,
            )
            if r.returncode != 0:
                target = self._get_default_branch(repo)
                instance.original_branch = target

            # Stash uncommitted changes that would block checkout
            status_r = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo, capture_output=True, text=True, **_NOWND,
            )
            if status_r.stdout.strip():
                log.info("Stashing dirty working tree in %s before merge", repo)
                stash_r = subprocess.run(
                    ["git", "stash", "push", "-m",
                     f"auto-stash for merge {instance.branch}"],
                    cwd=repo, capture_output=True, text=True, **_NOWND,
                )
                if stash_r.returncode == 0:
                    stashed = True

            # Set up union merge driver for changelog (prevents conflict failures)
            try:
                attrs_dir = Path(repo) / ".git" / "info"
                attrs_dir.mkdir(parents=True, exist_ok=True)
                attrs_file = attrs_dir / "attributes"
                attrs_content = attrs_file.read_text() if attrs_file.exists() else ""
                if "CHANGELOG.md" not in attrs_content:
                    with open(attrs_file, "a") as f:
                        f.write("CHANGELOG.md merge=union\n")
            except OSError:
                log.debug("Could not set up merge driver for %s", repo)

            # Ensure main repo is on the correct branch before merging
            subprocess.run(
                ["git", "checkout", target],
                cwd=repo, capture_output=True, text=True, check=True, **_NOWND,
            )

            # Merge the worktree's branch (-X ours auto-resolves config conflicts)
            merge_r = subprocess.run(
                ["git", "merge", instance.branch, "--no-ff", "-X", "ours",
                 "-m", f"Merge {instance.branch} ({instance.display_id()})"],
                cwd=repo, capture_output=True, text=True, **_NOWND,
            )

            auto_resolved = 0
            if merge_r.returncode != 0:
                detail = (merge_r.stderr or merge_r.stdout or "").strip()
                log.warning("Merge conflict for %s into %s: %s — attempting auto-resolve",
                            instance.branch, target, detail)
                try:
                    auto_resolved = self._auto_resolve_merge_conflicts(
                        repo, instance.branch, detail,
                    )
                except Exception:
                    log.warning("Auto-resolve raised unexpected error for %s",
                                instance.branch, exc_info=True)
                    auto_resolved = -1
                if auto_resolved < 0:
                    subprocess.run(
                        ["git", "merge", "--abort"],
                        cwd=repo, capture_output=True, text=True, **_NOWND,
                    )
                    log.error("Merge failed for %s into %s in %s: %s",
                              instance.branch, target, repo, detail)
                    return f"Merge failed: {detail}"

            # --- Cleanup (reached on successful merge) ---

            # Remove worktree (--force handles uncommitted changes)
            if instance.worktree_path and Path(instance.worktree_path).exists():
                r = subprocess.run(
                    ["git", "worktree", "remove", instance.worktree_path, "--force"],
                    cwd=repo, capture_output=True, text=True, **_NOWND,
                )
                if r.returncode != 0:
                    log.warning("git worktree remove failed for %s: %s",
                                instance.worktree_path, r.stderr.strip())
                    # Fallback: manual removal + prune
                    try:
                        shutil.rmtree(instance.worktree_path, ignore_errors=True)
                        subprocess.run(
                            ["git", "worktree", "prune"],
                            cwd=repo, capture_output=True, text=True, **_NOWND,
                        )
                    except Exception:
                        pass

            # Delete branch (-d safe after merge; -D fallback if -d fails)
            r = subprocess.run(
                ["git", "branch", "-d", instance.branch],
                cwd=repo, capture_output=True, text=True, **_NOWND,
            )
            if r.returncode != 0:
                subprocess.run(
                    ["git", "branch", "-D", instance.branch],
                    cwd=repo, capture_output=True, text=True, **_NOWND,
                )

            # Clean up worktree project dir
            self._cleanup_worktree_session_dir(instance)

            # Push merged result to origin
            push_note = ""
            try:
                # Check if remote exists before attempting push
                has_remote = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    cwd=repo, capture_output=True, text=True, **_NOWND,
                ).returncode == 0

                if not has_remote:
                    log.info("No remote 'origin' in %s — skipping push", repo)
                    push_note = "\nℹ️ No remote configured — local merge is fine"
                elif (push_r := subprocess.run(
                    ["git", "push", "origin", target],
                    cwd=repo, capture_output=True, text=True,
                    timeout=30, **_NOWND,
                )).returncode != 0:
                    push_detail = (push_r.stderr or push_r.stdout or "").strip()
                    log.error("Push to origin after merge in %s: %s",
                              repo, push_detail)
                    push_note = f"\n⚠️ Could not push to origin (exit {push_r.returncode})"
                else:
                    log.info("Pushed %s to origin after merge in %s", target, repo)
                    # Push tags if the merged branch's tip had any
                    try:
                        tag_r = subprocess.run(
                            ["git", "tag", "--points-at", "HEAD^2"],
                            cwd=repo, capture_output=True, text=True, **_NOWND,
                        )
                        if tag_r.returncode != 0:
                            log.debug("git tag --points-at HEAD^2 failed in %s (rc=%d), skipping tag push",
                                      repo, tag_r.returncode)
                        else:
                            tags = tag_r.stdout.strip()
                            if tags:
                                tag_names = tags.splitlines()
                                tag_push_r = subprocess.run(
                                    ["git", "push", "origin"] + tag_names,
                                    cwd=repo, capture_output=True, text=True,
                                    timeout=30, **_NOWND,
                                )
                                if tag_push_r.returncode != 0:
                                    tag_detail = (tag_push_r.stderr or tag_push_r.stdout or "").strip()
                                    log.error("Tag push to origin in %s: %s", repo, tag_detail)
                                    push_note += f"\n⚠️ Tags not pushed (exit {tag_push_r.returncode})"
                                else:
                                    tag_list = ", ".join(tag_names)
                                    log.info("Pushed tags [%s] to origin in %s", tag_list, repo)
                                    push_note += f"\nTags pushed: {tag_list}"
                    except subprocess.TimeoutExpired:
                        log.error("Tag push to origin timed out (30s) in %s", repo)
                        push_note += "\n⚠️ Tag push timed out (30s)"
                    except Exception as e:
                        log.error("Tag push error in %s: %s", repo, e)
                        push_note += f"\n⚠️ Tag push error: {type(e).__name__}"
            except subprocess.TimeoutExpired:
                log.error("Push to origin timed out (30s) in %s", repo)
                push_note = "\n⚠️ Push to origin timed out (30s)"
            except Exception as e:
                log.error("Push to origin error in %s: %s", repo, e)
                push_note = f"\n⚠️ Push to origin error: {type(e).__name__}"

            instance.branch = None
            instance.worktree_path = None
            suffix = ""
            if auto_resolved > 0:
                suffix = f" (auto-resolved {auto_resolved} conflict{'s' if auto_resolved != 1 else ''})"
            return f"Merged into {target}{suffix}{push_note}"
        except subprocess.CalledProcessError as e:
            # Abort any in-progress merge to keep main repo clean for other sessions
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=instance.repo_path, capture_output=True, text=True, **_NOWND,
            )
            detail = (e.stderr or e.stdout or "").strip()
            log.error("Merge failed for %s into %s in %s: %s",
                      instance.branch, target, repo, detail)
            return f"Merge failed: {detail}"
        finally:
            if stashed:
                # Only pop if working tree is clean (abort or merge succeeded)
                check_r = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=repo, capture_output=True, text=True, **_NOWND,
                )
                if not check_r.stdout.strip():
                    subprocess.run(
                        ["git", "stash", "pop"],
                        cwd=repo, capture_output=True, text=True, **_NOWND,
                    )
                else:
                    log.warning(
                        "Skipping stash pop — working tree not clean in %s",
                        repo)

    def _auto_resolve_merge_conflicts(
        self, repo: str, branch: str, detail: str,
    ) -> int:
        """Auto-resolve merge conflicts by preferring the feature branch.

        Called when a merge is in-progress with unresolved conflicts.
        For UU (both-modified) files, attempts a three-way merge-file first
        to preserve both sides' changes. Falls back to --theirs for remaining
        conflicts. Returns number of resolved files, or -1 on failure.
        """
        status_r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo, capture_output=True, text=True, **_NOWND,
        )

        conflicts: list[tuple[str, str]] = []
        for line in status_r.stdout.splitlines():
            if len(line) < 3:
                continue
            code = line[:2]
            filepath = line[3:]
            if code in ("UU", "AA", "DU", "UD", "DD", "AU", "UA"):
                conflicts.append((code, filepath))

        if not conflicts:
            log.warning("Merge failed but no conflicted files for %s: %s",
                        branch, detail)
            return -1

        log.info("Auto-resolving %d conflict(s) for %s: %s",
                 len(conflicts), branch,
                 ", ".join(f"{c}:{f}" for c, f in conflicts))

        for code, filepath in conflicts:
            if code in ("UD", "DD"):
                # Feature branch deleted — accept deletion
                r = subprocess.run(
                    ["git", "rm", "--", filepath],
                    cwd=repo, capture_output=True, text=True, **_NOWND,
                )
                if r.returncode != 0:
                    log.warning("git rm failed for %s: %s",
                                filepath, r.stderr.strip())
                    return -1
            elif code == "UU":
                # Both modified — try three-way merge-file to keep both sides
                if not self._try_merge_file(repo, filepath):
                    # Fallback: accept feature branch version
                    if not self._checkout_theirs(repo, filepath):
                        return -1
            else:
                # AA, AU, UA, DU — accept feature branch version
                if not self._checkout_theirs(repo, filepath):
                    return -1

        commit_r = subprocess.run(
            ["git", "commit", "--no-edit"],
            cwd=repo, capture_output=True, text=True, **_NOWND,
        )
        if commit_r.returncode != 0:
            log.warning("Failed to commit auto-resolved merge for %s: %s",
                        branch, commit_r.stderr.strip())
            return -1

        log.info("Auto-resolved merge for %s completed successfully", branch)
        return len(conflicts)

    def _try_merge_file(self, repo: str, filepath: str) -> bool:
        """Attempt three-way merge-file for a UU conflict.

        Extracts base/ours/theirs from index stages, runs git merge-file,
        and writes the result back if successful. Returns True on success.
        """
        tmp_base = tmp_ours = tmp_theirs = None
        try:
            # Extract the three stages into temp files (Windows-safe)
            tmp_base = tempfile.NamedTemporaryFile(
                delete=False, suffix=".base", prefix="merge_")
            tmp_ours = tempfile.NamedTemporaryFile(
                delete=False, suffix=".ours", prefix="merge_")
            tmp_theirs = tempfile.NamedTemporaryFile(
                delete=False, suffix=".theirs", prefix="merge_")
            # Close handles before git writes to them (Windows requirement)
            tmp_base.close()
            tmp_ours.close()
            tmp_theirs.close()

            stages = [
                (":1:" + filepath, tmp_base.name),   # base
                (":2:" + filepath, tmp_ours.name),    # ours
                (":3:" + filepath, tmp_theirs.name),  # theirs
            ]
            for ref, dest in stages:
                r = subprocess.run(
                    ["git", "show", ref],
                    cwd=repo, capture_output=True, **_NOWND,
                )
                if r.returncode != 0:
                    log.debug("Could not extract %s for merge-file", ref)
                    return False
                Path(dest).write_bytes(r.stdout)

            # Run merge-file: exit 0 = clean, 1 = conflicts, <0 = error
            mf = subprocess.run(
                ["git", "merge-file", "-p",
                 tmp_ours.name, tmp_base.name, tmp_theirs.name],
                capture_output=True, **_NOWND,
            )

            if mf.returncode < 0:
                # Signal/error — output is garbage
                log.debug("merge-file crashed for %s (rc=%d)", filepath, mf.returncode)
                return False

            if mf.returncode == 0:
                # Clean merge — write result back
                target = Path(repo) / filepath
                target.write_bytes(mf.stdout)
                subprocess.run(
                    ["git", "add", "--", filepath],
                    cwd=repo, capture_output=True, text=True, **_NOWND,
                )
                log.info("merge-file resolved %s cleanly", filepath)
                return True

            # returncode 1 = still has conflicts, fall back to --theirs
            log.debug("merge-file had remaining conflicts for %s, falling back", filepath)
            return False

        except Exception:
            log.debug("merge-file failed for %s", filepath, exc_info=True)
            return False
        finally:
            for tmp in (tmp_base, tmp_ours, tmp_theirs):
                if tmp is not None:
                    try:
                        os.unlink(tmp.name)
                    except OSError:
                        pass

    def _checkout_theirs(self, repo: str, filepath: str) -> bool:
        """Resolve a conflict by accepting the feature branch version."""
        r1 = subprocess.run(
            ["git", "checkout", "--theirs", "--", filepath],
            cwd=repo, capture_output=True, text=True, **_NOWND,
        )
        if r1.returncode != 0:
            log.warning("checkout --theirs failed for %s: %s",
                        filepath, r1.stderr.strip())
            return False
        r2 = subprocess.run(
            ["git", "add", "--", filepath],
            cwd=repo, capture_output=True, text=True, **_NOWND,
        )
        if r2.returncode != 0:
            log.warning("git add failed for %s: %s",
                        filepath, r2.stderr.strip())
            return False
        return True

    async def discard_branch(self, instance: Instance) -> str:
        """Delete worktree and branch without merging."""
        if not instance.branch or not instance.original_branch:
            if instance.original_branch and not instance.branch:
                return f"Already discarded ({config.BRANCH_PREFIX}/{instance.id})"
            return "No branch to discard"
        if not instance.repo_path:
            return "No repo path"
        repo_lock = self._get_repo_lock(instance.repo_path)
        async with repo_lock:
            return await asyncio.to_thread(self._discard_branch_sync, instance)

    def _discard_branch_sync(self, instance: Instance) -> str:
        repo = instance.repo_path
        errors: list[str] = []

        # Each cleanup step is independent — continue on failure
        if instance.worktree_path and Path(instance.worktree_path).exists():
            r = subprocess.run(
                ["git", "worktree", "remove", instance.worktree_path, "--force"],
                cwd=repo, capture_output=True, text=True, **_NOWND,
            )
            if r.returncode != 0:
                log.warning("Failed to remove worktree %s: %s", instance.worktree_path, r.stderr.strip())
                errors.append(f"worktree remove: {r.stderr.strip()}")

        r = subprocess.run(
            ["git", "branch", "-D", instance.branch],
            cwd=repo, capture_output=True, text=True, **_NOWND,
        )
        if r.returncode != 0:
            log.warning("Failed to delete branch %s: %s", instance.branch, r.stderr.strip())
            errors.append(f"branch delete: {r.stderr.strip()}")

        self._cleanup_worktree_session_dir(instance)

        instance.branch = None
        instance.worktree_path = None
        if errors:
            return f"Discarded (with warnings: {'; '.join(errors)})"
        return f"Discarded branch, back on {instance.original_branch}"

    def _cleanup_worktree_session_dir(self, instance: Instance) -> None:
        """Remove the worktree's CLI project directory (session files already copied back)."""
        if not instance.worktree_path:
            return
        wt_encoded = self._encode_project_path(instance.worktree_path)
        wt_proj_dir = config.CLAUDE_PROJECTS_DIR / wt_encoded
        if wt_proj_dir.is_dir():
            shutil.rmtree(str(wt_proj_dir), ignore_errors=True)

    # --- Divergence safety check (used by startup auto-merge) ---

    @staticmethod
    def _check_branch_divergence(
        repo: str, branch: str, target: str,
    ) -> tuple[str, str]:
        """Decide whether `branch` is safe to auto-merge into `target`.

        Returns (decision, reason) where decision is one of:
          - "ok":   clean fast-forward / pure ahead — safe to merge
          - "noop": branch has no new commits (already merged or empty)
          - "skip": unsafe (diverged, missing branches, git error)
        """
        # Verify target branch exists
        r = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{target}"],
            cwd=repo, capture_output=True, text=True, **_NOWND,
        )
        if r.returncode != 0:
            return ("skip", f"target branch '{target}' missing")

        # Verify source branch exists
        r = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
            cwd=repo, capture_output=True, text=True, **_NOWND,
        )
        if r.returncode != 0:
            return ("skip", f"source branch '{branch}' missing")

        # Compute ahead/behind: left=target-only, right=branch-only
        r = subprocess.run(
            ["git", "rev-list", "--left-right", "--count", f"{target}...{branch}"],
            cwd=repo, capture_output=True, text=True, **_NOWND,
        )
        if r.returncode != 0:
            return ("skip", f"rev-list failed: {(r.stderr or '').strip()}")
        parts = r.stdout.strip().split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return ("skip", f"unexpected rev-list output: {r.stdout!r}")
        behind, ahead = int(parts[0]), int(parts[1])

        if ahead == 0:
            # No new commits on branch — already merged (or never had any)
            return ("noop", f"branch already merged into {target} (ahead=0, behind={behind})")
        if behind > 0:
            return ("skip", f"diverged from {target} (ahead={ahead}, behind={behind})")
        return ("ok", f"safe to merge (ahead={ahead}, behind=0)")

    # --- Orphan scanning ---

    @staticmethod
    def scan_orphan_branches(repo_path: str, active_branches: set[str]) -> list[str]:
        """Find bot-managed branches not associated with active instances."""
        try:
            result = subprocess.run(
                ["git", "branch", "--list", f"{config.BRANCH_PREFIX}/*"],
                cwd=repo_path, capture_output=True, text=True, **_NOWND,
            )
            branches = [b.strip().lstrip("* ") for b in result.stdout.splitlines() if b.strip()]
            return [b for b in branches if b not in active_branches]
        except Exception:
            log.debug("Failed to scan branches in %s", repo_path, exc_info=True)
            return []

    @staticmethod
    def scan_orphan_worktrees(repo_path: str, active_worktrees: set[str]) -> list[str]:
        """Find stale .worktrees/ directories not associated with active instances."""
        wt_parent = Path(repo_path) / ".worktrees"
        if not wt_parent.is_dir():
            return []
        orphans = []
        for d in wt_parent.iterdir():
            if d.is_dir() and str(d) not in active_worktrees:
                orphans.append(d.name)
        return orphans

    # --- Startup cleanup ---

    async def cleanup_stale_worktrees(
        self, store, repos: dict[str, str],
    ) -> list[str]:
        """Merge pending done instances, fix repo HEADs, clean orphans.

        Called on startup to recover from interrupted autopilot chains.
        Returns a list of actions taken (for logging).
        """
        messages: list[str] = []

        # 1. Fix repos stuck on bot-managed branches (under repo lock)
        for name, path in repos.items():
            if not Path(path).is_dir():
                continue
            try:
                repo_lock = self._get_repo_lock(path)
                async with repo_lock:
                    msg = await asyncio.to_thread(self._fix_repo_head, path)
                if msg:
                    messages.append(f"{name}: {msg}")
            except Exception:
                log.warning("Failed to fix HEAD for %s", name, exc_info=True)

        # 2. Merge completed done instances that still have branches
        # (merge_branch already acquires repo lock internally)
        # Age gate: skip instances older than 24h to avoid silently merging
        # stale/diverged branches. User can still /merge manually.
        MAX_STALE_AGE_HOURS = 24
        now = datetime.now(timezone.utc)
        done_insts = [
            inst for inst in store.list_instances(all_=True)
            if inst.origin == InstanceOrigin.DONE
            and inst.status == InstanceStatus.COMPLETED
            and inst.branch and inst.original_branch
            and inst.repo_path and Path(inst.repo_path).is_dir()
        ]
        for inst in done_insts:
            branch_name = inst.branch
            # Check age — use finished_at, fall back to created_at
            age_ref = inst.finished_at or inst.created_at
            try:
                ref_dt = datetime.fromisoformat(age_ref) if age_ref else None
            except (ValueError, TypeError):
                ref_dt = None
            if ref_dt is None:
                messages.append(f"skip {branch_name}: no timestamp — use /merge manually")
                continue
            age_hours = (now - ref_dt).total_seconds() / 3600
            # Guard against future dates (system clock wrong) or excessively old instances
            if age_hours < 0:
                messages.append(
                    f"skip {branch_name}: timestamp in future ({-age_hours:.0f}h) — check system clock"
                )
                continue
            if age_hours > MAX_STALE_AGE_HOURS:
                messages.append(
                    f"skip {branch_name}: stale ({age_hours:.0f}h old) — use /merge manually"
                )
                continue

            # Divergence safety check — never silently merge a branch that has
            # forked from its target since the bot session started.
            try:
                decision, reason = await asyncio.to_thread(
                    self._check_branch_divergence,
                    inst.repo_path, branch_name, inst.original_branch,
                )
            except Exception as e:
                log.warning("startup auto-merge: divergence check raised for %s",
                            branch_name, exc_info=True)
                messages.append(f"skip {branch_name}: divergence check failed ({e})")
                continue
            if decision == "skip":
                messages.append(f"skip {branch_name}: {reason} — use /merge manually")
                continue
            if decision == "noop":
                messages.append(f"no-op {branch_name}: {reason}")
                # Branch already merged; clear stale branch refs on all
                # instances (including source) so this case doesn't recur
                # on every startup.
                self._clear_stale_branches_static(store, branch_name)
                continue

            try:
                msg = await self.merge_branch(inst)
                store.update_instance(inst)
                if "failed" not in msg.lower():
                    self._clear_stale_branches_static(store, branch_name)
                messages.append(f"merge {branch_name}: {msg}")
            except Exception as e:
                log.warning("startup auto-merge: merge %s raised", branch_name, exc_info=True)
                messages.append(f"merge {branch_name}: error ({e})")

        # 3. Clean up remaining orphaned branches and worktrees (under repo lock)
        for name, path in repos.items():
            if not Path(path).is_dir():
                continue
            try:
                repo_lock = self._get_repo_lock(path)
                async with repo_lock:
                    cleaned = await asyncio.to_thread(
                        self._cleanup_orphans_sync, store, path,
                    )
                messages.extend(f"{name}: {c}" for c in cleaned)
            except Exception:
                log.warning("Orphan cleanup failed for %s", name, exc_info=True)

        return messages

    def _fix_repo_head(self, repo_path: str) -> str | None:
        """If repo HEAD is on a bot-managed branch, checkout default branch."""
        r = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, **_NOWND,
        )
        current = r.stdout.strip() if r.returncode == 0 else ""
        if not current.startswith(f"{config.BRANCH_PREFIX}/"):
            return None
        target = self._get_default_branch(repo_path)
        r = subprocess.run(
            ["git", "checkout", target],
            cwd=repo_path, capture_output=True, text=True, **_NOWND,
        )
        if r.returncode != 0:
            log.warning("Failed to checkout %s in %s: %s",
                        target, repo_path, r.stderr.strip())
            return f"stuck on {current} (checkout {target} failed)"
        return f"switched HEAD from {current} to {target}"

    def _cleanup_orphans_sync(self, store, repo_path: str) -> list[str]:
        """Remove orphaned branches and worktrees for a repo."""
        cleaned: list[str] = []

        # Collect branches still referenced by any instance
        active_branches = {
            inst.branch for inst in store.list_instances(all_=True)
            if inst.branch
        }

        orphan_branches = self.scan_orphan_branches(repo_path, active_branches)
        for branch in orphan_branches:
            # Infer worktree dir from branch name (prefix/t-xxx → .worktrees/t-xxx)
            wt_name = branch.split("/")[-1] if "/" in branch else branch
            wt_dir = Path(repo_path) / ".worktrees" / wt_name
            if wt_dir.exists():
                r = subprocess.run(
                    ["git", "worktree", "remove", str(wt_dir), "--force"],
                    cwd=repo_path, capture_output=True, text=True, **_NOWND,
                )
                if r.returncode != 0:
                    shutil.rmtree(str(wt_dir), ignore_errors=True)
            subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=repo_path, capture_output=True, text=True, **_NOWND,
            )
            cleaned.append(f"cleaned orphan {branch}")

        # Prune any worktree registrations pointing to deleted directories
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo_path, capture_output=True, text=True, **_NOWND,
        )

        return cleaned

    @staticmethod
    def _clear_stale_branches_static(store, branch_name: str) -> int:
        """Clear branch/worktree_path on ALL instances sharing a branch name.

        Also nulls the branch field in history.jsonl so resumed sessions don't
        see stale branch refs in their system prompt.
        """
        count = 0
        for inst in store.list_instances(all_=True):
            if inst.branch == branch_name:
                inst.branch = None
                inst.worktree_path = None
                store.update_instance(inst)
                count += 1
        try:
            from bot.store import history as history_mod
            history_mod.clear_branch(branch_name)
        except Exception:
            pass
        return count
