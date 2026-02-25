"""Claude Code subprocess management — streaming, kill, semaphore, stall detection, git branching."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from bot import config
from bot.claude.parser import (
    RunResult,
    extract_progress,
    extract_result,
    extract_summary,
    is_transient_error,
    parse_stream_line,
)
from bot.claude.types import Instance, InstanceStatus

log = logging.getLogger(__name__)

ProgressCallback = Callable  # async callback(message: str, detail: str)
StallCallback = Callable[[str], None]     # async callback(instance_id)


class ClaudeRunner:
    """Manages Claude Code CLI subprocesses."""

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENT)
        self._processes: dict[str, asyncio.subprocess.Process] = {}

    async def check_cli(self) -> str:
        """Verify Claude CLI is available. Returns version string."""
        try:
            proc = await asyncio.create_subprocess_exec(
                config.CLAUDE_BINARY, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            version = stdout.decode().strip()
            if not version:
                raise RuntimeError("Claude CLI returned empty version")
            return version
        except FileNotFoundError:
            raise RuntimeError(
                f"Claude CLI not found: {config.CLAUDE_BINARY}. "
                "Ensure it's installed and in PATH."
            )
        except asyncio.TimeoutError:
            raise RuntimeError("Claude CLI --version timed out")

    async def run(
        self,
        instance: Instance,
        on_progress: ProgressCallback | None = None,
        on_stall: StallCallback | None = None,
        context: str | None = None,
    ) -> RunResult:
        """Run Claude CLI for an instance. Blocks until completion or timeout."""
        async with self._semaphore:
            return await self._run_impl(instance, on_progress, on_stall, context)

    async def _run_impl(
        self,
        instance: Instance,
        on_progress: ProgressCallback | None,
        on_stall: StallCallback | None,
        context: str | None = None,
    ) -> RunResult:
        timeout = (config.TASK_TIMEOUT_SECS
                   if instance.instance_type.value == "task"
                   else config.QUERY_TIMEOUT_SECS)

        # Git branch safety for build bg tasks
        if instance.branch:
            await self._ensure_branch(instance)

        cmd = self._build_command(instance, context)
        log.info("Running %s: %s", instance.id, " ".join(cmd))

        # Clear CLAUDE_CODE env var to avoid nested-session error
        env = {**os.environ}
        env.pop("CLAUDE_CODE", None)
        env.pop("CLAUDECODE", None)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=instance.repo_path or None,
                env=env,
                limit=1024 * 1024,  # 1MB line buffer (default 64KB too small for stream-json)
            )
            instance.pid = proc.pid
            self._processes[instance.id] = proc

            result = await asyncio.wait_for(
                self._stream_output(proc, instance, on_progress, on_stall),
                timeout=timeout,
            )

            # Auto-retry on transient errors
            if result.is_error and is_transient_error(
                result.error_message or result.result_text
            ) and instance.retry_count == 0:
                log.info("Transient error for %s, retrying in 30s", instance.id)
                instance.retry_count = 1
                await asyncio.sleep(30)
                return await self._run_impl(instance, on_progress, on_stall, context)

            return result

        except asyncio.TimeoutError:
            log.warning("Timeout for %s after %ds", instance.id, timeout)
            await self.kill(instance.id)
            return RunResult(
                is_error=True,
                error_message=f"Timed out after {timeout}s",
            )
        except Exception as e:
            log.exception("Error running %s", instance.id)
            return RunResult(is_error=True, error_message=str(e))
        finally:
            self._processes.pop(instance.id, None)

    async def _stream_output(
        self,
        proc: asyncio.subprocess.Process,
        instance: Instance,
        on_progress: ProgressCallback | None,
        on_stall: StallCallback | None,
    ) -> RunResult:
        """Read stdout line-by-line, parse stream-json, detect stalls."""
        events: list[dict] = []
        last_output_time = asyncio.get_event_loop().time()
        stall_warned = False
        stall_check_task: asyncio.Task | None = None

        async def check_stall():
            nonlocal stall_warned
            while True:
                await asyncio.sleep(10)
                elapsed = asyncio.get_event_loop().time() - last_output_time
                if elapsed > config.STALL_TIMEOUT_SECS and not stall_warned:
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

                if not line:
                    break

                last_output_time = asyncio.get_event_loop().time()
                stall_warned = False

                decoded = line.decode("utf-8", errors="replace")
                event = parse_stream_line(decoded)
                if event:
                    events.append(event)
                    if on_progress:
                        progress = extract_progress(event)
                        if progress and progress.message:
                            try:
                                await on_progress(progress.message, progress.detail)
                            except Exception:
                                log.exception("Progress callback error")
        finally:
            if stall_check_task:
                stall_check_task.cancel()
                try:
                    await stall_check_task
                except asyncio.CancelledError:
                    pass

        await proc.wait()

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

        # Save result file
        if result.result_text:
            result_path = config.RESULTS_DIR / f"{instance.id}.md"
            result_path.write_text(result.result_text, encoding="utf-8")
            instance.result_file = str(result_path)

        # Save git diff for build bg tasks
        if instance.branch and not result.is_error:
            await self._save_diff(instance)

        # Extract summary
        instance.summary = extract_summary(result.result_text)

        return result

    def _build_command(self, instance: Instance, context: str | None = None) -> list[str]:
        cmd = [config.CLAUDE_BINARY, "-p"]

        # Build prompt — never mutated on the instance
        prompt = instance.prompt
        if not prompt:
            prompt = "Continue the previous conversation."

        cmd.append(prompt)
        cmd.extend(["--output-format", "stream-json", "--verbose"])

        # Build system prompt: mobile hint + bot context + pinned context + repo CLAUDE.md + projects dir
        system_prompt = self._build_system_prompt(instance, context)
        cmd.extend(["--append-system-prompt", system_prompt])

        # Resume session
        if instance.session_id:
            cmd.extend(["--resume", instance.session_id])

        # Permission mode
        if instance.mode == "build":
            cmd.extend(["--permission-mode", "bypassPermissions"])
        else:
            cmd.extend(["--allowedTools", config.EXPLORE_TOOLS])

        return cmd

    def _build_system_prompt(self, instance: Instance, context: str | None = None) -> str:
        """Build the system prompt with mobile hint, bot context, pinned context, repo CLAUDE.md, and projects dir."""
        parts = [config.MOBILE_HINT]

        # Bot capability context so Claude knows what the user can do
        parts.append(config.BOT_CONTEXT)

        # Pinned user context (passed as parameter to avoid double-prepend on retry)
        if context:
            parts.append(f"\n\nUser context: {context}")

        repo_path = instance.repo_path
        if repo_path:
            # Include CLAUDE.md from the repo if it exists
            claude_md = Path(repo_path) / ".claude" / "CLAUDE.md"
            if claude_md.exists():
                try:
                    content = claude_md.read_text(encoding="utf-8")
                    parts.append(
                        f"\n\n--- Repository Instructions (from .claude/CLAUDE.md) ---\n"
                        f"{content}"
                    )
                except Exception:
                    log.warning("Failed to read CLAUDE.md from %s", claude_md)

            # Point Claude to the projects directory for plans/sessions
            projects_dir = self._get_projects_dir(repo_path)
            if projects_dir and projects_dir.exists():
                parts.append(
                    f"\n\nClaude Code session history and plans for this repo "
                    f"are stored in: {projects_dir}"
                )

        return "".join(parts)

    @staticmethod
    def _get_projects_dir(repo_path: str) -> Path | None:
        """Get the Claude projects directory for a repo path.

        Claude Code stores session data in ~/.claude/projects/<sanitized-path>/
        where the path has : and separators replaced with dashes.
        """
        try:
            # Sanitize: replace : \ / with -
            sanitized = repo_path.replace(":", "-").replace("\\", "-").replace("/", "-")
            return config.CLAUDE_PROJECTS_DIR / sanitized
        except Exception:
            return None

    async def kill(self, instance_id: str) -> bool:
        """Terminate a running Claude process."""
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

    def queue_position(self, instance_id: str) -> int | None:
        """Approximate queue position (not exact with asyncio.Semaphore)."""
        # Semaphore doesn't expose waiter count directly
        waiters = getattr(self._semaphore, '_waiters', None)
        if waiters is None:
            return None
        return len(waiters)

    async def _ensure_branch(self, instance: Instance) -> None:
        """Create or checkout a git branch for build tasks (idempotent)."""
        if not instance.repo_path:
            return
        try:
            # Get current branch name
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=instance.repo_path, capture_output=True, text=True,
            )
            current = result.stdout.strip()

            # Already on the right branch
            if current == instance.branch:
                return

            if not instance.original_branch:
                instance.original_branch = current

            # Try to create new branch; if it already exists, just checkout
            create = subprocess.run(
                ["git", "checkout", "-b", instance.branch],
                cwd=instance.repo_path, capture_output=True, text=True,
            )
            if create.returncode != 0:
                subprocess.run(
                    ["git", "checkout", instance.branch],
                    cwd=instance.repo_path, capture_output=True, text=True, check=True,
                )
            log.info("On branch %s in %s", instance.branch, instance.repo_path)
        except subprocess.CalledProcessError as e:
            log.error("Failed to ensure branch: %s", e.stderr)
            raise RuntimeError(f"Failed to ensure branch: {e.stderr}")

    async def _save_diff(self, instance: Instance) -> None:
        """Save git diff for a build bg task."""
        if not instance.repo_path or not instance.branch:
            return
        try:
            base = instance.original_branch or "HEAD~1"
            result = subprocess.run(
                ["git", "diff", base, "--", "."],
                cwd=instance.repo_path, capture_output=True, text=True
            )
            if result.stdout.strip():
                diff_path = config.RESULTS_DIR / f"{instance.id}.diff"
                diff_path.write_text(result.stdout, encoding="utf-8")
                instance.diff_file = str(diff_path)
        except Exception:
            log.exception("Failed to save diff for %s", instance.id)

    async def merge_branch(self, instance: Instance) -> str:
        """Merge task branch into original branch. Returns status message."""
        if not instance.branch or not instance.original_branch:
            return "No branch to merge"
        try:
            repo = instance.repo_path
            # Checkout original branch
            subprocess.run(
                ["git", "checkout", instance.original_branch],
                cwd=repo, capture_output=True, text=True, check=True
            )
            # Merge task branch
            result = subprocess.run(
                ["git", "merge", instance.branch, "--no-ff",
                 "-m", f"Merge {instance.branch} ({instance.display_id()})"],
                cwd=repo, capture_output=True, text=True, check=True
            )
            # Delete task branch
            subprocess.run(
                ["git", "branch", "-d", instance.branch],
                cwd=repo, capture_output=True, text=True
            )
            instance.branch = None
            return f"Merged into {instance.original_branch}"
        except subprocess.CalledProcessError as e:
            return f"Merge failed: {e.stderr.strip()}"

    async def discard_branch(self, instance: Instance) -> str:
        """Delete task branch without merging."""
        if not instance.branch or not instance.original_branch:
            return "No branch to discard"
        try:
            repo = instance.repo_path
            # Checkout original branch first
            subprocess.run(
                ["git", "checkout", instance.original_branch],
                cwd=repo, capture_output=True, text=True, check=True
            )
            # Force-delete task branch
            subprocess.run(
                ["git", "branch", "-D", instance.branch],
                cwd=repo, capture_output=True, text=True, check=True
            )
            instance.branch = None
            return f"Discarded branch, back on {instance.original_branch}"
        except subprocess.CalledProcessError as e:
            return f"Discard failed: {e.stderr.strip()}"
