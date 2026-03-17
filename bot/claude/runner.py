"""Claude Code subprocess management — streaming, kill, semaphore, stall detection, git worktrees."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
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

        # Per-repo lock: serializes git-metadata-mutating operations
        # (worktree add/remove, merge, branch delete) to prevent lock file races
        self._repo_locks: dict[str, asyncio.Lock] = {}

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

        # Git worktree isolation for build tasks
        if instance.branch:
            await self._ensure_worktree(instance)

        # Copy session file to worktree's project dir so --resume works
        if instance.worktree_path and instance.session_id:
            await asyncio.to_thread(
                self._copy_session_to_worktree, instance,
            )

        # Use worktree as cwd if available (file isolation)
        working_dir = instance.worktree_path or instance.repo_path or None

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
                cwd=working_dir,
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
                return await self._run_impl(instance, on_progress, on_stall, context, sibling_context)

            # Auto-retry on transient errors
            if result.is_error and is_transient_error(
                result.error_message or result.result_text
            ) and instance.retry_count == 0:
                log.info("Transient error for %s, retrying in 30s", instance.id)
                instance.retry_count = 1
                await asyncio.sleep(30)
                return await self._run_impl(instance, on_progress, on_stall, context, sibling_context)

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

        # Save git diff for build tasks
        if instance.branch and not result.is_error:
            await self._save_diff(instance)

        # Copy session files back from worktree to main repo project dir
        if instance.worktree_path:
            await asyncio.to_thread(self._copy_session_from_worktree, instance)

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
        cmd.extend(["--effort", instance.effort])

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

        # Honesty & verification guidance
        parts.append(config.HONESTY_CONSTRAINT)

        # Bot capability context so Claude knows what the user can do
        parts.append(config.BOT_CONTEXT)

        # Universal working context — workflow, Discord UI, branch model, design principles
        parts.append(config.WORKING_CONTEXT)

        # Per-step behavioral guidance based on workflow origin
        origin_key = instance.origin.value if instance.origin else "direct"
        guidance = config.WORKFLOW_GUIDANCE.get(origin_key)
        if guidance:
            parts.append(f"\n\n--- Your Role in This Step ---\n{guidance}")

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

    async def _ensure_worktree(self, instance: Instance) -> None:
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
            await asyncio.to_thread(self._create_worktree_sync, instance)

    def _create_worktree_sync(self, instance: Instance) -> None:
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

            # Copy .claude/ directory into worktree so Claude CLI finds
            # CLAUDE.md and project settings (worktrees have a .git file,
            # not directory — some tools may not follow it correctly)
            try:
                src_claude_dir = Path(repo) / ".claude"
                dst_claude_dir = Path(wt_dir) / ".claude"
                if src_claude_dir.is_dir() and not dst_claude_dir.exists():
                    shutil.copytree(str(src_claude_dir), str(dst_claude_dir),
                                    ignore=shutil.ignore_patterns("*.jsonl"))
                    log.debug("Copied .claude/ into worktree %s", wt_dir)
            except Exception:
                log.warning("Failed to copy .claude/ into worktree %s", wt_dir, exc_info=True)

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
        r = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=repo_path, capture_output=True, text=True, **_NOWND,
        )
        if r.returncode == 0:
            head = r.stdout.strip()
            if head and not head.startswith("claude-bot/"):
                return head
        # Last resort: find any non-claude-bot branch
        r = subprocess.run(
            ["git", "branch", "--format=%(refname:short)"],
            cwd=repo_path, capture_output=True, text=True, **_NOWND,
        )
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                branch = line.strip()
                if branch and not branch.startswith("claude-bot/"):
                    return branch
        log.warning("No default branch in %s — every branch is bot-managed",
                     repo_path)
        return "master"

    # --- Session file management (worktree ↔ main repo) ---

    @staticmethod
    def _encode_project_path(path: str) -> str:
        """Encode path the same way Claude Code does for project dirs."""
        path = path.replace("\\", "/").rstrip("/")
        return path.replace("/", "-").replace(":", "-").replace(".", "-")

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

        Also handles the case where Claude created a NEW session (different ID).
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
            subprocess.run(
                ["git", "merge", instance.branch, "--no-ff", "-X", "ours",
                 "-m", f"Merge {instance.branch} ({instance.display_id()})"],
                cwd=repo, capture_output=True, text=True, check=True, **_NOWND,
            )

            # --- Cleanup (reached on successful merge) ---

            # Remove worktree if it exists
            if instance.worktree_path and Path(instance.worktree_path).exists():
                subprocess.run(
                    ["git", "worktree", "remove", instance.worktree_path],
                    cwd=repo, capture_output=True, text=True, **_NOWND,
                )

            # Delete branch
            subprocess.run(
                ["git", "branch", "-d", instance.branch],
                cwd=repo, capture_output=True, text=True, **_NOWND,
            )

            # Clean up worktree project dir
            self._cleanup_worktree_session_dir(instance)

            instance.branch = None
            instance.worktree_path = None
            return f"Merged into {instance.original_branch}"
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

    async def discard_branch(self, instance: Instance) -> str:
        """Delete worktree and branch without merging."""
        if not instance.branch or not instance.original_branch:
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
        """Remove the worktree's Claude project directory (session files already copied back)."""
        if not instance.worktree_path:
            return
        wt_encoded = self._encode_project_path(instance.worktree_path)
        wt_proj_dir = config.CLAUDE_PROJECTS_DIR / wt_encoded
        if wt_proj_dir.is_dir():
            shutil.rmtree(str(wt_proj_dir), ignore_errors=True)

    # --- Orphan scanning ---

    @staticmethod
    def scan_orphan_branches(repo_path: str, active_branches: set[str]) -> list[str]:
        """Find claude-bot/* branches not associated with active instances."""
        try:
            result = subprocess.run(
                ["git", "branch", "--list", "claude-bot/*"],
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
