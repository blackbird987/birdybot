"""Claude Code subprocess management — streaming, kill, semaphore, stall detection, git branching."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Awaitable, Callable

from bot import config
from bot.claude.parser import (
    RunResult,
    extract_progress,
    extract_result,
    extract_summary,
    is_transient_error,
    iter_tool_blocks,
    parse_stream_line,
)
from bot.claude.types import CODE_CHANGE_TOOLS, Instance, InstanceType

log = logging.getLogger(__name__)

# On Windows, prevent subprocess console windows from popping up
_NOWND: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
)

ProgressCallback = Callable  # async callback(message: str, detail: str)
StallCallback = Callable[[str], None]     # async callback(instance_id)


class ClaudeRunner:
    """Manages Claude Code CLI subprocesses."""

    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENT)
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        # Task-level tracking: covers the full lifecycle of a query/workflow,
        # not just the subprocess.  Prevents reboot from slipping through
        # gaps between autopilot chain steps.
        self._active_tasks: set[str] = set()
        self._idle_event = asyncio.Event()
        self._idle_event.set()  # starts idle

        # Reboot coalescing: multiple instances can queue reboot requests,
        # but only one reboot executes after all tasks finish.
        self._reboot_requests: list[dict] = []
        self._reboot_executing = False
        self._on_idle_callback: Callable[[], Awaitable[None]] | None = None
        self._idle_loop: asyncio.AbstractEventLoop | None = None

    async def check_cli(self) -> str:
        """Verify Claude CLI is available. Returns version string."""
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
        sibling_context: str | None = None,
    ) -> RunResult:
        """Run Claude CLI for an instance. Blocks until completion or timeout."""
        async with self._semaphore:
            return await self._run_impl(instance, on_progress, on_stall, context, sibling_context)

    async def _run_impl(
        self,
        instance: Instance,
        on_progress: ProgressCallback | None,
        on_stall: StallCallback | None,
        context: str | None = None,
        sibling_context: str | None = None,
    ) -> RunResult:
        inactivity_timeout = (config.TASK_TIMEOUT_SECS
                              if instance.instance_type == InstanceType.TASK
                              else config.QUERY_TIMEOUT_SECS)

        # Git branch safety for build bg tasks
        if instance.branch:
            await self._ensure_branch(instance)

        cmd = self._build_command(instance, context, sibling_context)
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
                **_NOWND,
            )
            instance.pid = proc.pid
            self._processes[instance.id] = proc

            # Activity-based timeout: only times out if no output for
            # inactivity_timeout seconds (not wall-clock total runtime)
            result = await self._stream_output(
                proc, instance, on_progress, on_stall, inactivity_timeout,
            )

            # Dead session: clear session_id and retry without --resume
            error_text = result.error_message or result.result_text or ""
            if result.is_error and "No conversation found" in error_text and instance.session_id:
                log.warning("Session %s not found for %s, retrying without resume", instance.session_id, instance.id)
                instance.session_id = None
                return await self._run_impl(instance, on_progress, on_stall, context)

            # Auto-retry on transient errors
            if result.is_error and is_transient_error(
                result.error_message or result.result_text
            ) and instance.retry_count == 0:
                log.info("Transient error for %s, retrying in 30s", instance.id)
                instance.retry_count = 1
                await asyncio.sleep(30)
                return await self._run_impl(instance, on_progress, on_stall, context)

            return result

        except Exception as e:
            log.exception("Error running %s", instance.id)
            return RunResult(is_error=True, error_message=str(e))
        finally:
            self._processes.pop(instance.id, None)
            if not self._active_tasks and not self._processes:
                self._idle_event.set()

    async def _stream_output(
        self,
        proc: asyncio.subprocess.Process,
        instance: Instance,
        on_progress: ProgressCallback | None,
        on_stall: StallCallback | None,
        inactivity_timeout: int = 300,
    ) -> RunResult:
        """Read stdout line-by-line, parse stream-json, detect stalls.

        Uses an activity-based timeout: the process is only killed if no
        output is received for ``inactivity_timeout`` seconds.  This lets
        long-running but actively-streaming sessions continue indefinitely.
        """
        events: list[dict] = []
        captured_session_id: str | None = None
        ask_question: str | None = None  # Set when AskUserQuestion detected
        last_output_time = asyncio.get_event_loop().time()
        stall_warned = False
        timed_out = False
        stall_check_task: asyncio.Task | None = None

        async def check_stall():
            nonlocal stall_warned, timed_out
            while True:
                await asyncio.sleep(10)
                elapsed = asyncio.get_event_loop().time() - last_output_time

                # Hard inactivity timeout — kill the process
                if elapsed > inactivity_timeout:
                    timed_out = True
                    log.warning(
                        "Inactivity timeout for %s — no output for %ds",
                        instance.id, inactivity_timeout,
                    )
                    proc.terminate()
                    return

                # Early stall warning
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
                    # Eagerly capture session_id so it survives a timeout kill
                    if not captured_session_id and event.get("session_id"):
                        captured_session_id = event["session_id"]
                    if on_progress:
                        progress = extract_progress(event)
                        if progress and progress.message:
                            try:
                                await on_progress(progress.message, progress.detail)
                            except Exception:
                                log.exception("Progress callback error")

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

        if timed_out:
            return RunResult(
                session_id=captured_session_id,
                is_error=True,
                error_message=f"No output for {inactivity_timeout}s — timed out",
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

        # Save git diff for build bg tasks
        if instance.branch and not result.is_error:
            await self._save_diff(instance)

        # Extract summary
        instance.summary = extract_summary(result.result_text)

        return result

    def _build_command(
        self, instance: Instance,
        context: str | None = None,
        sibling_context: str | None = None,
    ) -> list[str]:
        cmd = [config.CLAUDE_BINARY, "-p"]

        # Build prompt — never mutated on the instance
        prompt = instance.prompt
        if not prompt:
            prompt = "Continue the previous conversation."

        cmd.extend(["--output-format", "stream-json", "--verbose"])

        # Build system prompt: mobile hint + bot context + pinned context + repo CLAUDE.md + projects dir
        system_prompt = self._build_system_prompt(instance, context, sibling_context)
        cmd.extend(["--append-system-prompt", system_prompt])

        # Resume session
        if instance.session_id:
            cmd.extend(["--resume", instance.session_id])

        # Permissions: always bypass (non-interactive bot can't approve prompts).
        # In explore/plan, block file-modification tools to enforce read-only.
        cmd.extend(["--permission-mode", "bypassPermissions"])

        disallowed = set()
        if instance.mode != "build":
            disallowed.update(CODE_CHANGE_TOOLS)

        # Defense-in-depth: non-owner sessions enforce code change tools
        # even if mode somehow got set to "build" incorrectly
        if not instance.is_owner_session:
            if instance.mode != "build":
                disallowed.update(CODE_CHANGE_TOOLS)  # redundant with above, but safe
                if instance.bash_policy == "none":
                    disallowed.add("Bash")

        if disallowed:
            cmd.extend(["--disallowed-tools", ",".join(sorted(disallowed))])

        # End-of-options: prevents prompt text starting with dashes (e.g. /ref
        # context injection) from being parsed as CLI flags by Commander.js.
        cmd.extend(["--", prompt])
        return cmd

    def _build_system_prompt(
        self, instance: Instance,
        context: str | None = None,
        sibling_context: str | None = None,
    ) -> str:
        """Build the system prompt with mobile hint, bot context, pinned context, repo CLAUDE.md, and projects dir."""
        parts = [config.MOBILE_HINT]

        # Explain that user can only see text output (critical for good responses)
        parts.append(config.CHAT_APP_CONSTRAINT)

        # Bot capability context so Claude knows what the user can do
        parts.append(config.BOT_CONTEXT)

        # Plan mode: instruct Claude not to modify files, output a plan instead
        if instance.mode == "plan":
            parts.append(config.PLAN_MODE_CONSTRAINT)

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

        # Sibling session awareness — helps Claude avoid file conflicts
        if sibling_context:
            parts.append(
                f"\n\n--- Sibling Sessions ---\n{sibling_context}\n"
                "Avoid editing files these sessions are likely working on."
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

    # --- Task-level tracking ---

    def begin_task(self, instance_id: str) -> None:
        """Mark a high-level task (query/workflow chain) as active."""
        self._active_tasks.add(instance_id)
        self._idle_event.clear()

    def end_task(self, instance_id: str) -> None:
        """Mark a high-level task as completed.

        If all tasks are done and reboot requests are pending, fires the
        idle callback (exactly once) to execute the coalesced reboot.
        """
        self._active_tasks.discard(instance_id)
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
        """Number of currently running Claude processes."""
        return len(self._processes)

    @property
    def active_ids(self) -> list[str]:
        """IDs of currently running instances."""
        return list(self._processes.keys())

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
        """
        self._reboot_requests.append(data)
        self._maybe_fire_idle_reboot()

    def pending_reboots(self) -> list[dict]:
        """Return a copy of pending reboot requests."""
        return list(self._reboot_requests)

    def clear_reboots(self) -> None:
        """Clear all pending reboot requests."""
        self._reboot_requests.clear()

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
            if not self._active_tasks and not self._processes:
                self._idle_event.set()

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
        await asyncio.to_thread(self._ensure_branch_sync, instance)

    def _ensure_branch_sync(self, instance: Instance) -> None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=instance.repo_path, capture_output=True, text=True, **_NOWND,
            )
            current = result.stdout.strip()
            if current == instance.branch:
                return
            if not instance.original_branch:
                instance.original_branch = current
            create = subprocess.run(
                ["git", "checkout", "-b", instance.branch],
                cwd=instance.repo_path, capture_output=True, text=True, **_NOWND,
            )
            if create.returncode != 0:
                subprocess.run(
                    ["git", "checkout", instance.branch],
                    cwd=instance.repo_path, capture_output=True, text=True, check=True, **_NOWND,
                )
            log.info("On branch %s in %s", instance.branch, instance.repo_path)
        except subprocess.CalledProcessError as e:
            log.error("Failed to ensure branch: %s", e.stderr)
            raise RuntimeError(f"Failed to ensure branch: {e.stderr}")

    async def _save_diff(self, instance: Instance) -> None:
        """Save git diff for a build bg task."""
        if not instance.repo_path or not instance.branch:
            return
        await asyncio.to_thread(self._save_diff_sync, instance)

    def _save_diff_sync(self, instance: Instance) -> None:
        try:
            base = instance.original_branch or "HEAD~1"
            result = subprocess.run(
                ["git", "diff", base, "--", "."],
                cwd=instance.repo_path, capture_output=True, text=True, **_NOWND,
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
        return await asyncio.to_thread(self._merge_branch_sync, instance)

    def _merge_branch_sync(self, instance: Instance) -> str:
        try:
            repo = instance.repo_path
            subprocess.run(
                ["git", "checkout", instance.original_branch],
                cwd=repo, capture_output=True, text=True, check=True, **_NOWND,
            )
            subprocess.run(
                ["git", "merge", instance.branch, "--no-ff",
                 "-m", f"Merge {instance.branch} ({instance.display_id()})"],
                cwd=repo, capture_output=True, text=True, check=True, **_NOWND,
            )
            subprocess.run(
                ["git", "branch", "-d", instance.branch],
                cwd=repo, capture_output=True, text=True, **_NOWND,
            )
            instance.branch = None
            return f"Merged into {instance.original_branch}"
        except subprocess.CalledProcessError as e:
            return f"Merge failed: {e.stderr.strip()}"

    async def discard_branch(self, instance: Instance) -> str:
        """Delete task branch without merging."""
        if not instance.branch or not instance.original_branch:
            return "No branch to discard"
        return await asyncio.to_thread(self._discard_branch_sync, instance)

    def _discard_branch_sync(self, instance: Instance) -> str:
        try:
            repo = instance.repo_path
            subprocess.run(
                ["git", "checkout", instance.original_branch],
                cwd=repo, capture_output=True, text=True, check=True, **_NOWND,
            )
            subprocess.run(
                ["git", "branch", "-D", instance.branch],
                cwd=repo, capture_output=True, text=True, check=True, **_NOWND,
            )
            instance.branch = None
            return f"Discarded branch, back on {instance.original_branch}"
        except subprocess.CalledProcessError as e:
            return f"Discard failed: {e.stderr.strip()}"
