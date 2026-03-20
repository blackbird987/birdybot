"""Command handlers — auth, alias resolution, settings, scheduling, repo management.

Each method takes a RequestContext and operates via ctx.messenger.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import config
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType
from bot.engine import lifecycle, sessions as sessions_mod, workflows
from bot.platform.base import ButtonSpec, RequestContext
from bot.platform.formatting import (
    VALID_MODES,
    action_button_specs,
    expanded_button_specs,
    format_cost_md,
    format_expanded_result_md,
    format_instance_list_md,
    format_result_md,
    format_schedule_list_md,
    format_status_md,
    mode_label,
    redact_secrets,
    running_button_specs,
    strip_markdown,
)

from bot.claude.runner import ClaudeRunner, _NOWND

log = logging.getLogger(__name__)

# --- Shared state for uptime / cli_version / shutdown ---

_start_time: float = 0.0
_cli_version: str = "unknown"
_shutdown_fn = None

# --- Per-channel query queue (prevents concurrent queries in same session) ---

_channel_locks: dict[str, asyncio.Lock] = {}


def _get_channel_lock(channel_id: str) -> asyncio.Lock:
    """Get or create a per-channel lock for serializing queries."""
    if channel_id not in _channel_locks:
        _channel_locks[channel_id] = asyncio.Lock()
    return _channel_locks[channel_id]


def init(start_time: float, cli_version: str, shutdown_fn=None) -> None:
    """Initialize module-level state."""
    global _start_time, _cli_version, _shutdown_fn
    _start_time = start_time
    _cli_version = cli_version
    _shutdown_fn = shutdown_fn


def check_budget(ctx: RequestContext) -> bool:
    daily = ctx.store.get_daily_cost()
    return daily < config.DAILY_BUDGET_USD


async def budget_warning(ctx: RequestContext) -> None:
    daily = ctx.store.get_daily_cost()
    if daily >= config.DAILY_BUDGET_USD * 0.8:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"⚠️ Budget warning: ${daily:.4f} / ${config.DAILY_BUDGET_USD:.2f} "
            f"({daily / config.DAILY_BUDGET_USD * 100:.0f}%)",
        )


# --- Natural language repo detection (Tier 1 — fast regex) ---

_FAST_REPO_PATTERNS = [
    # "add repo <name> <path>" / "register repo <name> <path>"
    re.compile(
        r"(?:add|register)\s+(?:a\s+)?(?:repo|repository|project)\s+"
        r"['\"]?(\w[\w-]{2,31})['\"]?\s+(?:at\s+)?['\"]?"
        r"([A-Za-z]:\\[^\s'\"]+|/[^\s'\"]+)['\"]?",
        re.IGNORECASE,
    ),
    # "create repo <name>" / "create a new repo <name>" [+ optional path]
    re.compile(
        r"(?:create|init)\s+(?:a\s+)?(?:new\s+)?(?:repo|repository|project)\s+"
        r"(?:called\s+|named\s+)?['\"]?(\w[\w-]{2,31})['\"]?"
        r"(?:\s+(?:at\s+)?['\"]?([A-Za-z]:\\[^\s'\"]+|/[^\s'\"]+)['\"]?)?$",
        re.IGNORECASE,
    ),
]


async def _try_fast_repo_command(ctx: RequestContext, text: str) -> bool:
    """Catch very explicit natural-language repo commands. Returns True if handled."""
    stripped = text.strip()
    for i, pat in enumerate(_FAST_REPO_PATTERNS):
        m = pat.search(stripped)
        if not m:
            continue
        if i == 0:  # add/register pattern
            name, path = m.group(1), m.group(2)
            await ctx.messenger.send_text(
                ctx.channel_id, f"📂 Registering repo **{name}** at `{path}`…",
            )
            await on_repo(ctx, f"add {name} {path}")
            return True
        elif i == 1:  # create pattern
            name = m.group(1)
            path = m.group(2) or ""
            await ctx.messenger.send_text(
                ctx.channel_id, f"📂 Creating repo **{name}**…",
            )
            await on_repo(ctx, f"create {name} {path}".strip())
            return True
    return False


# --- Natural language repo detection (Tier 2 — Claude-assisted BOT_CMD) ---

_BOT_CMD_RE = re.compile(r'\[BOT_CMD:\s*/repo\s+(.+?)\]')
_ALLOWED_BOT_CMD_ACTIONS = {"add", "create", "switch"}
_DANGEROUS_PATH_CHARS = re.compile(r'[;&|`$(){}!<>]')
_QUOTED_LINE_PREFIX = re.compile(r'^\s*(?:>|`|```|#{1,3}\s)')


async def _execute_bot_commands(ctx: RequestContext, result_text: str) -> None:
    """Scan final assistant output for [BOT_CMD: /repo ...] directives."""
    if not result_text:
        return
    for m in _BOT_CMD_RE.finditer(result_text):
        # Skip matches inside quoted/code content
        line_start = result_text.rfind('\n', 0, m.start()) + 1
        line_prefix = result_text[line_start:m.start()]
        if _QUOTED_LINE_PREFIX.match(line_prefix):
            log.debug("BOT_CMD skipped — inside quoted content")
            continue

        repo_args = m.group(1).strip()
        action = repo_args.split()[0] if repo_args else ""
        if action not in _ALLOWED_BOT_CMD_ACTIONS:
            log.warning("BOT_CMD blocked — disallowed action: %s", action)
            continue
        if _DANGEROUS_PATH_CHARS.search(repo_args):
            log.warning("BOT_CMD blocked — dangerous characters: %s", repo_args)
            continue
        # For add commands, validate the path exists
        if action == "add":
            parts = repo_args.split(None, 2)  # "add <name> <path>"
            if len(parts) >= 3:
                candidate = Path(parts[2].strip("\"'"))
                if not candidate.is_dir():
                    log.warning("BOT_CMD blocked — path not found: %s", parts[2])
                    continue
        try:
            await ctx.messenger.send_text(
                ctx.channel_id, f"⚡ Auto-executing: `/repo {repo_args}`",
            )
            await on_repo(ctx, repo_args)
        except Exception:
            log.warning("Failed to execute bot command: /repo %s", repo_args)


# --- Query ---

async def on_text(ctx: RequestContext, text: str) -> None:
    """Handle a plain text message — run as query."""
    if not text.strip():
        return
    # Check for natural-language repo commands (shares channel lock to prevent races)
    lock = _get_channel_lock(ctx.channel_id)
    queued_msg_id = None
    if lock.locked():
        queued_msg_id = await ctx.messenger.send_text(
            ctx.channel_id,
            "📋 Queued — waiting for current query to finish.",
            silent=True,
        )
    async with lock:
        if queued_msg_id:
            try:
                await ctx.messenger.delete_message(ctx.channel_id, queued_msg_id)
            except Exception:
                pass
        if await _try_fast_repo_command(ctx, text):
            return
        # Double-checked locking: re-read session_id after acquiring lock
        if not ctx.session_id and ctx.resolve_session_id is not None:
            fresh = ctx.resolve_session_id()
            if fresh:
                ctx.session_id = fresh
        await _execute_query(ctx, text)


async def on_unknown_command(ctx: RequestContext, text: str) -> None:
    """Handle unregistered /commands — check aliases first."""
    alias_match = re.match(r'^/(\w+)(?:\s+(.*))?$', text, re.DOTALL)
    if alias_match:
        alias_name = alias_match.group(1)
        alias_prompt = ctx.store.get_alias(alias_name)
        if alias_prompt:
            extra = alias_match.group(2) or ""
            prompt = f"{alias_prompt} {extra}".strip() if extra else alias_prompt
            await _run_query(ctx, prompt)
            return

    escaped = ctx.messenger.escape(text.split()[0])
    await ctx.messenger.send_text(
        ctx.channel_id,
        f"Unknown command: {escaped}\nUse /help for available commands, or /alias list for aliases.",
    )


async def _run_query(ctx: RequestContext, prompt: str) -> None:
    lock = _get_channel_lock(ctx.channel_id)
    queued_msg_id = None
    if lock.locked():
        queued_msg_id = await ctx.messenger.send_text(
            ctx.channel_id,
            "📋 Queued — waiting for current query to finish.",
            silent=True,
        )
    async with lock:
        # Clean up the "queued" notice now that we're running
        if queued_msg_id:
            try:
                await ctx.messenger.delete_message(ctx.channel_id, queued_msg_id)
            except Exception:
                pass
        # Double-checked locking: re-read session_id after acquiring lock
        if not ctx.session_id and ctx.resolve_session_id is not None:
            fresh = ctx.resolve_session_id()
            if fresh:
                ctx.session_id = fresh
        await _execute_query(ctx, prompt)


async def _execute_query(ctx: RequestContext, prompt: str) -> None:
    # Block spawns during reboot drain or if session already has a running task
    spawn_err = ctx.runner.check_spawn_allowed(ctx.session_id)
    if spawn_err:
        await ctx.messenger.send_text(ctx.channel_id, spawn_err)
        return

    if not check_budget(ctx):
        await ctx.messenger.send_text(
            ctx.channel_id, "Daily budget exceeded. Use /budget reset to override.",
        )
        return

    # Rate limit for non-owner users (callbacks populated by platform layer)
    if not ctx.is_owner and ctx.user_id and ctx.check_rate_limit:
        if not ctx.check_rate_limit():
            limit = ctx.max_daily_queries or "?"
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"Daily query limit reached ({limit}/day). "
                "Ask the bot owner to increase your limit.",
            )
            return
        if ctx.increment_query_count:
            ctx.increment_query_count()

    # Log user attribution
    user_label = f"{ctx.user_name} ({ctx.user_id})" if ctx.user_id else "owner"
    log.info("Query by %s (owner=%s) repo=%s: %s", user_label, ctx.is_owner, ctx.repo_name, prompt[:80])

    # Per-channel repo (Discord) takes priority over global active repo
    if ctx.repo_name:
        repos = ctx.store.list_repos()
        # Case-insensitive lookup (session project "AIAgent" vs repo "aiagent")
        repo_path = repos.get(ctx.repo_name)
        repo_name = ctx.repo_name
        if not repo_path:
            lower_map = {k.lower(): (k, v) for k, v in repos.items()}
            match = lower_map.get(ctx.repo_name.lower())
            if match:
                repo_name, repo_path = match
        if not repo_path:
            available = ", ".join(sorted(repos.keys())) if repos else "none"
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"Repo '{ctx.repo_name}' not found (available: {available}).\n"
                f"To add it: `/repo add {ctx.repo_name} <path>`",
            )
            return
    else:
        repo_name, repo_path = ctx.store.get_active_repo()
    if not repo_path:
        await ctx.messenger.send_text(
            ctx.channel_id, "No repo set. Use /repo add <name> <path> first.",
        )
        return

    # Per-channel session (Discord) is authoritative; global fallback for other platforms
    if ctx.session_id:
        resume_session = ctx.session_id
    elif ctx.platform == "discord":
        resume_session = None  # Discord channels are isolated — never use global session
    else:
        resume_session = ctx.store.active_session_id

    inst = ctx.store.create_instance(
        instance_type=InstanceType.QUERY,
        prompt=prompt,
        mode=ctx.effective_mode,
    )
    inst.origin_platform = ctx.platform
    inst.effort = ctx.effective_effort
    inst.repo_name = repo_name or ""
    inst.repo_path = repo_path or ""
    # User identity and access control
    inst.user_id = ctx.user_id or ""
    inst.user_name = ctx.user_name or ""
    inst.is_owner_session = ctx.is_owner
    if not ctx.is_owner and ctx.bash_policy:
        inst.bash_policy = ctx.bash_policy
    if resume_session:
        inst.session_id = resume_session
    inst.status = InstanceStatus.RUNNING
    ctx.store.update_instance(inst, critical=True)

    if resume_session:
        label = "resuming..."
        # Show session context hint so user knows what they're continuing
        if ctx.session_id:
            try:
                fpath = await asyncio.to_thread(sessions_mod.find_session_file, resume_session)
                if fpath:
                    msgs = await asyncio.to_thread(sessions_mod.read_session_messages, fpath, 2)
                    # Find last assistant message for context
                    last_topic = ""
                    for m in reversed(msgs):
                        if m["role"] == "assistant":
                            last_topic = m["text"][:80].replace("\n", " ").strip()
                            break
                    if last_topic:
                        label = f"resuming... (last: {last_topic})"
            except Exception:
                pass
    else:
        label = "processing..."
    escaped = ctx.messenger.escape(inst.display_id())
    handle = await ctx.messenger.send_thinking(
        ctx.channel_id, f"⏳ {escaped} {label}",
        buttons=running_button_specs(inst.id),
    )
    if handle.get("message_id"):
        inst.message_ids.setdefault(ctx.platform, []).append(handle.get("message_id"))
        ctx.store.update_instance(inst)

    on_progress, on_stall, heartbeat = lifecycle.make_progress_callbacks(
        ctx, inst, handle, ctx.effective_verbose,
    )

    heartbeat_task = asyncio.create_task(heartbeat())
    start_time = asyncio.get_event_loop().time()
    ctx.runner.begin_task(inst.id, session_id=inst.session_id)
    try:
        try:
            result = await ctx.runner.run(
                inst, on_progress=on_progress, on_stall=on_stall,
                context=ctx.effective_context,
            )
        finally:
            heartbeat_task.cancel()

        # Update thinking message to show completion
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed >= 60:
            elapsed_str = f"{elapsed / 60:.1f}m"
        else:
            elapsed_str = f"{elapsed:.0f}s"
        icon = "✅" if not result.is_error else "❌"
        try:
            await ctx.messenger.edit_thinking(
                handle, f"{icon} {escaped} done ({elapsed_str})",
            )
        except Exception:
            pass

        lifecycle.finalize_run(ctx, inst, result)

        if not result.is_error and result.session_id:
            # For Discord channels, update the per-request session_id (caller reads inst.session_id)
            # For non-Discord platforms, update the store's global active_session_id
            if not ctx.session_id:
                ctx.store.active_session_id = result.session_id
            # Write session_id back immediately (before lock release)
            if ctx.on_session_resolved:
                ctx.on_session_resolved(result.session_id)

        await lifecycle.send_result(ctx, inst, result.result_text)

        # Tier 2: scan Claude's response for [BOT_CMD: /repo ...] directives
        if result.result_text and not result.is_error:
            await _execute_bot_commands(ctx, result.result_text)

        await budget_warning(ctx)

        # Check reboot request BEFORE end_task so it's queued when end_task
        # checks for pending reboots on idle.  Safe because check_reboot_request
        # just queues (no waiting) — the actual reboot fires from end_task.
        await lifecycle.check_reboot_request(ctx)
    finally:
        ctx.runner.end_task(inst.id)


# --- /new ---

async def on_new(ctx: RequestContext) -> None:
    """Clear chat and reset session."""
    for inst in ctx.store.list_instances(all_=True):
        for msg_id in inst.message_ids.get(ctx.platform, []):
            try:
                await ctx.messenger.delete_message(ctx.channel_id, msg_id)
            except Exception:
                pass
        inst.message_ids.get(ctx.platform, []).clear()
    ctx.store.save()
    ctx.store.active_session_id = None


# --- /bg ---

async def on_bg(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if not text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /bg [--name <name>] <description>")
        return

    if not check_budget(ctx):
        await ctx.messenger.send_text(ctx.channel_id, "Daily budget exceeded.")
        return

    repo_name, repo_path = ctx.store.get_active_repo()
    if not repo_path:
        await ctx.messenger.send_text(ctx.channel_id, "No repo set. Use /repo add <name> <path> first.")
        return

    name = None
    name_match = re.match(r'--name\s+(\S+)\s+(.*)', text, re.DOTALL)
    if name_match:
        name = name_match.group(1)
        text = name_match.group(2).strip()

    alias_match = re.match(r'^/(\w+)(?:\s+(.*))?$', text, re.DOTALL)
    if alias_match:
        alias_prompt = ctx.store.get_alias(alias_match.group(1))
        if alias_prompt:
            extra = alias_match.group(2) or ""
            text = f"{alias_prompt} {extra}".strip() if extra else alias_prompt

    inst = ctx.store.create_instance(
        instance_type=InstanceType.TASK,
        prompt=text,
        name=name,
        mode="build",
    )
    inst.origin_platform = ctx.platform
    inst.effort = ctx.effective_effort
    inst.branch = f"claude-bot/{inst.id}"
    inst.status = InstanceStatus.QUEUED
    ctx.store.update_instance(inst)

    escaped = ctx.messenger.escape(inst.display_id())
    escaped_branch = ctx.messenger.escape(inst.branch)
    buttons = action_button_specs(inst)
    msg_id = await ctx.messenger.send_text(
        ctx.channel_id,
        f"{escaped} queued (build mode, branch `{escaped_branch}`)",
        buttons=buttons,
    )
    inst.message_ids.setdefault(ctx.platform, []).append(msg_id)
    ctx.store.update_instance(inst)

    asyncio.create_task(_run_bg_task(ctx, inst))


async def _run_bg_task(ctx: RequestContext, inst: Instance) -> None:
    try:
        inst.status = InstanceStatus.RUNNING
        ctx.store.update_instance(inst, critical=True)

        result = await ctx.runner.run(inst, context=ctx.effective_context)
        lifecycle.finalize_run(ctx, inst, result)

        await lifecycle.send_result(
            ctx, inst, result.result_text,
            silent=inst.status == InstanceStatus.COMPLETED,
        )
    except Exception:
        log.exception("Background task %s crashed", inst.id)
        inst.status = InstanceStatus.FAILED
        inst.error = "Background task crashed unexpectedly"
        ctx.store.update_instance(inst, critical=True)
        try:
            await ctx.messenger.send_text(
                ctx.channel_id, f"❌ {inst.display_id()} crashed unexpectedly.",
            )
        except Exception:
            pass


# --- /release ---

async def on_release(ctx: RequestContext, text: str) -> None:
    """Handle /release [patch|minor|major|X.Y.Z]."""
    if not check_budget(ctx):
        await ctx.messenger.send_text(ctx.channel_id, "Daily budget exceeded.")
        return

    repo_name, repo_path = ctx.store.get_active_repo()
    if not repo_path:
        await ctx.messenger.send_text(ctx.channel_id, "No repo set. Use /repo add <name> <path> first.")
        return

    version_hint = text.strip() if text.strip() else "patch"
    prompt = config.RELEASE_PROMPT.format(version_hint=version_hint)

    inst = ctx.store.create_instance(
        instance_type=InstanceType.TASK,
        prompt=prompt,
        name=f"release-{version_hint}",
        mode="build",
    )
    inst.origin = InstanceOrigin.RELEASE
    inst.origin_platform = ctx.platform
    inst.effort = ctx.effective_effort
    inst.status = InstanceStatus.QUEUED
    ctx.store.update_instance(inst)

    escaped = ctx.messenger.escape(inst.display_id())
    handle = await ctx.messenger.send_text(
        ctx.channel_id,
        f"{escaped} — releasing ({ctx.messenger.escape(version_hint)})...",
    )
    inst.message_ids.setdefault(ctx.platform, []).append(handle)
    ctx.store.update_instance(inst)

    asyncio.create_task(_run_bg_task(ctx, inst))


# --- /list ---

async def on_list(ctx: RequestContext, text: str) -> None:
    tokens = text.lower().split() if text else []
    show_all = "all" in tokens

    # Filter tokens: running, failed, questions, or repo name
    status_filters: list[InstanceStatus] = []
    repo_filter: str | None = None
    repos = ctx.store.list_repos()

    for tok in tokens:
        if tok == "all":
            continue
        elif tok == "running":
            status_filters.append(InstanceStatus.RUNNING)
        elif tok in ("failed", "errors"):
            status_filters.append(InstanceStatus.FAILED)
        elif tok in ("questions", "asking", "attention"):
            # Will be handled separately
            pass
        elif tok in repos:
            repo_filter = tok

    # Build filtered list
    if "questions" in tokens or "asking" in tokens or "attention" in tokens:
        instances = ctx.store.needs_attention()
    elif status_filters:
        instances = ctx.store.list_by_status(*status_filters)
    else:
        instances = ctx.store.list_instances(all_=show_all)

    if repo_filter:
        instances = [i for i in instances if i.repo_name == repo_filter]

    # Group by repo for display
    if not status_filters and not repo_filter and len(repos) > 1:
        by_repo: dict[str, list[Instance]] = {}
        for inst in instances:
            by_repo.setdefault(inst.repo_name or "unknown", []).append(inst)
        lines: list[str] = []
        for rname, repo_insts in by_repo.items():
            lines.append(f"**{rname}**")
            lines.append(format_instance_list_md(repo_insts))
            lines.append("")
        msg_text = "\n".join(lines).strip() if lines else "No instances found."
    else:
        msg_text = format_instance_list_md(instances)

    markup = ctx.messenger.markdown_to_markup(msg_text)
    chunks = ctx.messenger.chunk_message(markup)

    for i, chunk in enumerate(chunks):
        await ctx.messenger.send_text(ctx.channel_id, chunk)

        if i == len(chunks) - 1:
            for inst in instances[:5]:
                buttons = action_button_specs(inst)
                if buttons:
                    await ctx.messenger.send_text(
                        ctx.channel_id, f"`{inst.id}`", buttons=buttons,
                    )


# --- /kill ---

async def on_kill(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if not text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /kill <id|name>")
        return

    inst = ctx.store.get_instance(text)
    if not inst:
        await ctx.messenger.send_text(ctx.channel_id, f"Instance '{text}' not found.")
        return

    killed = await ctx.runner.kill(inst.id)
    if killed:
        inst.status = InstanceStatus.KILLED
        inst.finished_at = datetime.now(timezone.utc).isoformat()
        ctx.store.update_instance(inst, critical=True)
        await ctx.messenger.send_text(ctx.channel_id, f"Killed {inst.display_id()}")
    else:
        await ctx.messenger.send_text(ctx.channel_id, "Process not found or already stopped.")


# --- /retry ---

async def on_retry(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if not text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /retry <id|name>")
        return

    inst = ctx.store.get_instance(text)
    if not inst:
        await ctx.messenger.send_text(ctx.channel_id, f"Instance '{text}' not found.")
        return

    if not check_budget(ctx):
        await ctx.messenger.send_text(ctx.channel_id, "Daily budget exceeded.")
        return

    if inst.repo_path and not Path(inst.repo_path).is_dir():
        await ctx.messenger.send_text(ctx.channel_id, "Repo path no longer valid.")
        return

    new_inst = ctx.store.create_instance(
        instance_type=inst.instance_type,
        prompt=inst.prompt,
        name=f"{inst.name}-retry" if inst.name else None,
        mode=inst.mode,
    )
    new_inst.origin = inst.origin
    new_inst.origin_platform = ctx.platform
    new_inst.effort = ctx.effective_effort
    new_inst.parent_id = inst.id
    new_inst.repo_name = inst.repo_name
    new_inst.repo_path = inst.repo_path
    if inst.session_id:
        new_inst.session_id = inst.session_id
    if inst.branch:
        new_inst.branch = inst.branch
        new_inst.original_branch = inst.original_branch
        new_inst.worktree_path = inst.worktree_path
    ctx.store.update_instance(new_inst)

    escaped = ctx.messenger.escape(new_inst.display_id())
    handle = await ctx.messenger.send_thinking(
        ctx.channel_id, f"⏳ {escaped} retrying...",
        buttons=running_button_specs(new_inst.id),
    )
    if handle.get("message_id"):
        new_inst.message_ids.setdefault(ctx.platform, []).append(handle.get("message_id"))
        ctx.store.update_instance(new_inst)

    await lifecycle.run_instance(ctx, new_inst, handle=handle)


# --- /log ---

async def on_log(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if not text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /log <id|name>")
        return

    inst = ctx.store.get_instance(text)
    if not inst:
        await ctx.messenger.send_text(ctx.channel_id, f"Instance '{text}' not found.")
        return

    if inst.result_file and Path(inst.result_file).exists():
        await ctx.messenger.send_file(
            ctx.channel_id, inst.result_file, f"{inst.id}.md",
            caption=f"Full output for {inst.display_id()}",
        )
    elif inst.error:
        await ctx.messenger.send_text(ctx.channel_id, f"Prompt: {inst.prompt}\n\nError: {inst.error}")
    elif inst.summary:
        await ctx.messenger.send_text(ctx.channel_id, f"Prompt: {inst.prompt}\n\n{inst.summary}")
    else:
        await ctx.messenger.send_text(ctx.channel_id, f"Prompt: {inst.prompt}\n\nNo output recorded.")


# --- /diff ---

async def on_diff(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if not text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /diff <id|name>")
        return

    inst = ctx.store.get_instance(text)
    if not inst:
        await ctx.messenger.send_text(ctx.channel_id, f"Instance '{text}' not found.")
        return

    if inst.diff_file and Path(inst.diff_file).exists():
        await ctx.messenger.send_file(
            ctx.channel_id, inst.diff_file, f"{inst.id}.diff",
            caption=f"Diff for {inst.display_id()}",
        )
    else:
        await ctx.messenger.send_text(ctx.channel_id, "No diff available for this instance.")


# --- /merge ---

async def on_merge(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if not text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /merge <id|name>")
        return

    inst = ctx.store.get_instance(text)
    if not inst:
        await ctx.messenger.send_text(ctx.channel_id, f"Instance '{text}' not found.")
        return

    msg = await ctx.runner.merge_branch(inst)
    ctx.store.update_instance(inst)
    if "failed" not in msg.lower():
        from bot.engine.deploy import update_after_merge, rescan_deploy_config_after_merge
        update_after_merge(ctx.store, inst)
        rescan_deploy_config_after_merge(ctx.store, inst.repo_name, inst.repo_path)
        await ctx.messenger.on_deploy_state_changed(inst.repo_name)
    await ctx.messenger.send_text(ctx.channel_id, msg)


# --- /discard ---

async def on_discard(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if not text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /discard <id|name>")
        return

    inst = ctx.store.get_instance(text)
    if not inst:
        await ctx.messenger.send_text(ctx.channel_id, f"Instance '{text}' not found.")
        return

    msg = await ctx.runner.discard_branch(inst)
    ctx.store.update_instance(inst)
    await ctx.messenger.send_text(ctx.channel_id, msg)


# --- /branches ---

async def on_branches(ctx: RequestContext) -> None:
    """List unmerged claude-bot/* branches across all repos."""
    repos = ctx.store.list_repos()
    if not repos:
        await ctx.messenger.send_text(ctx.channel_id, "No repos configured.")
        return

    # Collect branches and worktrees tracked by ANY instance
    active_branches: set[str] = set()
    active_worktrees: set[str] = set()
    for inst in ctx.store.list_instances(all_=True):
        if inst.branch:
            active_branches.add(inst.branch)
        if inst.worktree_path:
            active_worktrees.add(inst.worktree_path)

    lines: list[str] = []
    total_orphans = 0
    for repo_name, repo_path in repos.items():
        if not Path(repo_path).is_dir():
            continue
        # Orphan branches
        orphan_branches = ClaudeRunner.scan_orphan_branches(repo_path, active_branches)
        # Orphan worktrees
        orphan_wts = ClaudeRunner.scan_orphan_worktrees(repo_path, active_worktrees)
        repo_orphans = len(orphan_branches) + len(orphan_wts)
        if repo_orphans:
            total_orphans += repo_orphans
            lines.append(f"**{repo_name}** ({repo_orphans} orphaned)")
            for b in orphan_branches[:10]:
                lines.append(f"  `{b}` (branch)")
            for w in orphan_wts[:10]:
                lines.append(f"  `{w}` (worktree)")

    if not lines:
        await ctx.messenger.send_text(ctx.channel_id, "No orphaned branches or worktrees found.")
        return

    header = f"**Orphaned** ({total_orphans} total)\n\n"
    text = header + "\n".join(lines)
    markup = ctx.messenger.markdown_to_markup(text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- /cost ---

async def on_cost(ctx: RequestContext) -> None:
    daily = ctx.store.get_daily_cost()
    total = ctx.store.get_total_cost()
    top = ctx.store.get_top_spenders()
    text = format_cost_md(daily, total, top)
    markup = ctx.messenger.markdown_to_markup(text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- /usage ---

async def on_usage(ctx: RequestContext, *, force: bool = False) -> None:
    from bot.engine.usage import get_usage_details

    text = await get_usage_details(force=force)

    # Append top spenders from bot instances
    top = ctx.store.get_top_spenders()
    if top:
        text += "\n\n**Top spenders today:**"
        for inst in top:
            cost = f"${inst.cost_usd:.4f}" if inst.cost_usd else "$0"
            text += f"\n  `{inst.id}` {cost} \u2014 {inst.prompt[:30]}"

    markup = ctx.messenger.markdown_to_markup(text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- /status ---

async def on_status(ctx: RequestContext) -> None:
    uptime = _time.time() - _start_time
    active_repo, _ = ctx.store.get_active_repo()
    recent = ctx.store.list_instances()[:5]

    # Determine active platforms
    platforms = []
    if config.DISCORD_ENABLED:
        platforms.append("Discord")

    text = format_status_md(
        uptime_secs=uptime,
        running=ctx.store.running_count(),
        instances_today=ctx.store.instance_count_today(),
        failures_today=ctx.store.failure_count_today(),
        total_instances=ctx.store.instance_count(),
        repos=ctx.store.list_repos(),
        active_repo=active_repo,
        context=ctx.effective_context,
        schedule_count=len(ctx.store.list_schedules()),
        cli_version=_cli_version,
        pc_name=config.PC_NAME,
        platforms=platforms,
        recent=recent,
    )
    markup = ctx.messenger.markdown_to_markup(text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- /logs ---

async def on_logs(ctx: RequestContext) -> None:
    log_path = config.LOG_FILE
    if not log_path.exists():
        await ctx.messenger.send_text(ctx.channel_id, "No log file found.")
        return

    # Read only the last ~16KB to avoid loading 10MB into memory
    import os as _os
    size = _os.path.getsize(log_path)
    read_bytes = min(size, 16384)
    with open(log_path, "rb") as f:
        f.seek(max(0, size - read_bytes))
        raw = f.read().decode("utf-8", errors="replace")
    tail = raw.splitlines()[-50:]
    max_len = 4096 - 20
    while tail:
        joined = "\n".join(tail)
        escaped = ctx.messenger.escape(joined)
        if len(escaped) <= max_len:
            break
        tail = tail[1:]

    if not tail:
        joined = "(log too large to display)"

    text = "```\n" + joined + "\n```"
    markup = ctx.messenger.markdown_to_markup(text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- /mode ---

async def on_mode(ctx: RequestContext, text: str) -> None:
    text = text.strip().lower()
    if text in VALID_MODES:
        current = ctx.effective_mode
        ctx.update_mode(text)
        actual = ctx.effective_mode
        if actual == current:
            if actual != text and ctx.mode_ceiling:
                # Capped — always explain why
                msg = f"Mode: {mode_label(actual)} (capped — your ceiling is {mode_label(ctx.mode_ceiling)})"
                await ctx.messenger.send_text(ctx.channel_id, msg)
            return  # same effective mode, skip duplicate
        msg = f"Mode: {mode_label(actual)}"
        if actual != text and ctx.mode_ceiling:
            msg += f" (capped — your ceiling is {mode_label(ctx.mode_ceiling)})"
        await ctx.messenger.send_text(ctx.channel_id, msg)
    elif text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /mode explore|plan|build")
    else:
        await ctx.messenger.send_text(ctx.channel_id, f"Mode: {mode_label(ctx.effective_mode)}")


# --- /verbose ---

async def on_verbose(ctx: RequestContext, text: str) -> None:
    _VERBOSE_LABELS = {0: "silent", 1: "normal", 2: "detailed"}
    text = text.strip()
    if text in ("0", "1", "2"):
        ctx.update_verbose(int(text))
        await ctx.messenger.send_text(
            ctx.channel_id, f"Verbose level: {text} ({_VERBOSE_LABELS[int(text)]})",
        )
    elif text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /verbose 0|1|2")
    else:
        level = ctx.effective_verbose
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Verbose: {level} ({_VERBOSE_LABELS.get(level, '?')})\n0 = silent, 1 = normal, 2 = detailed",
        )


# --- /effort ---

_VALID_EFFORT = ("low", "medium", "high", "max")


async def on_effort(ctx: RequestContext, text: str) -> None:
    text = text.strip().lower()
    if text in _VALID_EFFORT:
        ctx.update_effort(text)
        await ctx.messenger.send_text(ctx.channel_id, f"Effort: {text}")
    elif text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /effort low|medium|high|max")
    else:
        level = ctx.effective_effort
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Effort: {level}\nSet with: /effort low|medium|high|max",
        )


# --- /context ---

async def on_context(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if text.startswith("set "):
        ctx_text = text[4:].strip()
        ctx.update_context(ctx_text)
        await ctx.messenger.send_text(ctx.channel_id, f"Context set: {ctx_text[:100]}")
    elif text == "clear":
        ctx.update_context(None)
        await ctx.messenger.send_text(ctx.channel_id, "Context cleared.")
    else:
        current = ctx.effective_context
        if current:
            await ctx.messenger.send_text(ctx.channel_id, f"Current context: {current}")
        else:
            await ctx.messenger.send_text(ctx.channel_id, "No context set. Use /context set <text>")


# --- /alias ---

async def on_alias(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if text.startswith("set "):
        parts = text[4:].strip().split(None, 1)
        if len(parts) < 2:
            await ctx.messenger.send_text(ctx.channel_id, "Usage: /alias set <name> <prompt>")
            return
        name, prompt = parts
        prompt = prompt.strip('"\'')
        ctx.store.set_alias(name, prompt)
        await ctx.messenger.send_text(ctx.channel_id, f"Alias /{name} saved.")
    elif text.startswith("delete "):
        name = text[7:].strip()
        if ctx.store.delete_alias(name):
            await ctx.messenger.send_text(ctx.channel_id, f"Alias /{name} deleted.")
        else:
            await ctx.messenger.send_text(ctx.channel_id, f"Alias '{name}' not found.")
    elif text == "list" or not text:
        aliases = ctx.store.list_aliases()
        if aliases:
            lines = [f"/{k} → {v[:60]}" for k, v in aliases.items()]
            await ctx.messenger.send_text(ctx.channel_id, "\n".join(lines))
        else:
            await ctx.messenger.send_text(ctx.channel_id, "No aliases set.")
    else:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /alias set|delete|list")


# --- /schedule ---

async def on_schedule(ctx: RequestContext, text: str) -> None:
    text = text.strip()

    if text.startswith("every "):
        match = re.match(r'every\s+(\d+)([mhd])\s+(.*)', text, re.DOTALL)
        if not match:
            await ctx.messenger.send_text(ctx.channel_id, "Usage: /schedule every <N><m|h|d> <prompt>")
            return
        amount = int(match.group(1))
        unit = match.group(2)
        prompt = match.group(3).strip()
        multiplier = {"m": 60, "h": 3600, "d": 86400}
        interval_secs = amount * multiplier[unit]
        mode = "explore"
        if "--build" in prompt:
            mode = "build"
            prompt = prompt.replace("--build", "").strip()
        sched = ctx.store.add_schedule(prompt=prompt, interval_secs=interval_secs, mode=mode)
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Schedule {sched.id} created: every {amount}{unit}\n"
            f"Next run: {sched.next_run_at[:16] if sched.next_run_at else 'soon'}",
        )

    elif text.startswith("at "):
        rel_match = re.match(r'at\s+\+(\d+)([smhd])\s+(.*)', text, re.DOTALL)
        abs_match = re.match(r'at\s+(\d{1,2}:\d{2})\s+(.*)', text, re.DOTALL)

        if rel_match:
            amount = int(rel_match.group(1))
            unit = rel_match.group(2)
            prompt = rel_match.group(3).strip()
            multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}
            delta = timedelta(seconds=amount * multiplier[unit])
            run_at = datetime.now(timezone.utc) + delta
            time_label = f"+{amount}{unit}"
        elif abs_match:
            time_str = abs_match.group(1)
            prompt = abs_match.group(2).strip()
            now = datetime.now(timezone.utc)
            hour, minute = map(int, time_str.split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                await ctx.messenger.send_text(ctx.channel_id, "Invalid time. Use HH:MM (0-23:0-59).")
                return
            run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if run_at <= now:
                run_at += timedelta(days=1)
            time_label = f"{time_str} UTC"
        else:
            await ctx.messenger.send_text(ctx.channel_id, "Usage: /schedule at <HH:MM|+Nm|+Nh> <prompt>")
            return

        mode = "explore"
        if "--build" in prompt:
            mode = "build"
            prompt = prompt.replace("--build", "").strip()

        sched = ctx.store.add_schedule(prompt=prompt, run_at=run_at.isoformat(), mode=mode)
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Schedule {sched.id} created: one-shot at {time_label}\n"
            f"Runs: {sched.next_run_at[:16] if sched.next_run_at else time_label}",
        )

    elif text.startswith("delete "):
        sid = text[7:].strip()
        if ctx.store.delete_schedule(sid):
            await ctx.messenger.send_text(ctx.channel_id, f"Schedule {sid} deleted.")
        else:
            await ctx.messenger.send_text(ctx.channel_id, f"Schedule '{sid}' not found.")

    elif text == "list" or not text:
        schedules = ctx.store.list_schedules()
        sched_text = format_schedule_list_md(schedules)
        markup = ctx.messenger.markdown_to_markup(sched_text)
        await ctx.messenger.send_text(ctx.channel_id, markup)
    else:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /schedule every|at|list|delete")


# --- /repo ---

_RESERVED_REPO_NAMES = {"add", "switch", "list", "create", "remove", "delete"}


def _validate_repo_name(name: str) -> str | None:
    """Return error message if invalid, None if ok."""
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        return "Repo name must be alphanumeric (hyphens/underscores ok)."
    if name.lower() in _RESERVED_REPO_NAMES:
        return f"'{name}' is a reserved word."
    if len(name) > 64:
        return "Repo name too long (max 64 chars)."
    return None


def _resolve_default_path(name: str, store) -> tuple[Path, str] | None:
    """Resolve default path for a new repo. Returns (path, source) or None."""
    from bot.config import REPOS_BASE_DIR
    if REPOS_BASE_DIR:
        return REPOS_BASE_DIR / name, "REPOS_BASE_DIR"
    _, active_path = store.get_active_repo()
    if active_path:
        return Path(active_path).parent / name, "sibling of active repo"
    repos = store.list_repos()
    if repos:
        return Path(next(iter(repos.values()))).parent / name, "sibling of first repo"
    return None


async def _create_repo(ctx: RequestContext, text: str) -> None:
    """Handle /repo create <name> [path] [--github] [--public]."""
    # --- Parse flags and positional args ---
    tokens = text.split()
    flags = {t for t in tokens if t.startswith("--")}
    positional = [t for t in tokens if not t.startswith("--")]
    github = bool(flags & {"--github", "--gh"})
    public = "--public" in flags

    if not positional:
        await ctx.messenger.send_text(
            ctx.channel_id, "Usage: /repo create <name> [path] [--github] [--public]"
        )
        return
    name = positional[0]
    path_str = " ".join(positional[1:]) if len(positional) > 1 else None

    # --- Validate name ---
    if err := _validate_repo_name(name):
        await ctx.messenger.send_text(ctx.channel_id, err)
        return
    if name in ctx.store.list_repos():
        await ctx.messenger.send_text(
            ctx.channel_id, f"Repo '{name}' already exists. Use /repo switch {name}"
        )
        return

    # --- Resolve path ---
    path_source: str | None = None
    if path_str:
        repo_path = Path(path_str.strip("\"'"))
    else:
        resolved = _resolve_default_path(name, ctx.store)
        if resolved is None:
            await ctx.messenger.send_text(
                ctx.channel_id,
                "No repos registered — provide a path: /repo create <name> <path>",
            )
            return
        repo_path, path_source = resolved

    # --- Handle existing directory with .git ---
    if repo_path.exists() and (repo_path / ".git").exists():
        ctx.store.add_repo(name, str(repo_path.resolve()))
        ctx.store.switch_repo(name)
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"'{name}' already a git repo — registered and switched: {repo_path}",
        )
        await ctx.messenger.on_repo_added(name)
        return

    # --- Create directory + git init ---
    created_dir = False
    try:
        def _init():
            nonlocal created_dir
            if not repo_path.exists():
                repo_path.mkdir(parents=True)
                created_dir = True
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=str(repo_path),
                capture_output=True, check=True, **_NOWND,
            )
        await asyncio.to_thread(_init)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if created_dir:
            try:
                repo_path.rmdir()  # best-effort cleanup (empty dir we just created)
            except OSError:
                pass
        await ctx.messenger.send_text(ctx.channel_id, f"git init failed: {e}")
        return
    except OSError as e:
        await ctx.messenger.send_text(ctx.channel_id, f"Failed to create directory: {e}")
        return

    # --- Register ---
    ctx.store.add_repo(name, str(repo_path.resolve()))
    ctx.store.switch_repo(name)
    source_hint = f", default: {path_source}" if path_source else ""
    msg = f"Created '{name}' at {repo_path} (git initialized{source_hint})."

    # --- Optional GitHub remote ---
    if github:
        visibility = "--public" if public else "--private"
        try:
            def _gh_create():
                return subprocess.run(
                    ["gh", "repo", "create", name, visibility,
                     "--source", str(repo_path), "--push"],
                    capture_output=True, text=True, cwd=str(repo_path), **_NOWND,
                )
            result = await asyncio.to_thread(_gh_create)
            if result.returncode == 0:
                msg += f" Pushed to GitHub ({visibility[2:]})."
            else:
                msg += f"\nGitHub create failed: {result.stderr.strip()}"
        except FileNotFoundError:
            msg += "\nGitHub push skipped: `gh` CLI not installed."

    await ctx.messenger.send_text(ctx.channel_id, msg)
    await ctx.messenger.on_repo_added(name)


async def on_repo(ctx: RequestContext, text: str) -> None:
    text = text.strip()

    if text.startswith("add "):
        parts = text[4:].strip().split(None, 1)
        if len(parts) < 2:
            await ctx.messenger.send_text(ctx.channel_id, "Usage: /repo add <name> <path>")
            return
        name, path = parts
        if err := _validate_repo_name(name):
            await ctx.messenger.send_text(ctx.channel_id, err)
            return
        path = str(Path(path.strip("\"'")).resolve())
        if not Path(path).is_dir():
            await ctx.messenger.send_text(ctx.channel_id, f"Directory not found: {path}")
            return
        ctx.store.add_repo(name, path)
        await ctx.messenger.send_text(ctx.channel_id, f"Repo '{name}' added: {path}")
        await ctx.messenger.on_repo_added(name)

    elif text.startswith("create "):
        await _create_repo(ctx, text[7:].strip())

    elif text.startswith("remove "):
        name = text[7:].strip()
        if ctx.store.remove_repo(name):
            await ctx.messenger.send_text(ctx.channel_id, f"Repo '{name}' removed from registry.")
        else:
            await ctx.messenger.send_text(ctx.channel_id, f"Repo '{name}' not found.")

    elif text.startswith("switch "):
        name = text[7:].strip()
        if ctx.store.switch_repo(name):
            _, path = ctx.store.get_active_repo()
            await ctx.messenger.send_text(ctx.channel_id, f"Switched to '{name}': {path}")
        else:
            await ctx.messenger.send_text(ctx.channel_id, f"Repo '{name}' not found.")

    elif text == "list":
        repos = ctx.store.list_repos()
        active, _ = ctx.store.get_active_repo()
        if repos:
            lines = []
            for name, path in repos.items():
                marker = " *" if name == active else ""
                lines.append(f"  {name}{marker} → {path}")
            await ctx.messenger.send_text(ctx.channel_id, "\n".join(lines))
        else:
            await ctx.messenger.send_text(ctx.channel_id, "No repos registered.")

    elif text.startswith("deploy"):
        rest = text[6:].strip()
        if rest.startswith("remove "):
            rname = rest[7:].strip()
            ctx.store.remove_deploy_config(rname)
            await ctx.messenger.send_text(ctx.channel_id, f"Deploy config removed for `{rname}`.")
        elif rest == "set" or rest.startswith("set "):
            parts = rest[4:].strip().split(None, 1) if len(rest) > 3 else []
            if len(parts) < 2:
                await ctx.messenger.send_text(ctx.channel_id, "Usage: /repo deploy set <name> <command>")
                return
            rname, command = parts
            if rname not in ctx.store.list_repos():
                await ctx.messenger.send_text(ctx.channel_id, f"Repo `{rname}` not found.")
                return
            from bot.engine.deploy import make_deploy_config
            ctx.store.set_deploy_config(rname, make_deploy_config(
                "command", command=command, label="Deploy",
                source="manual", approved=True,
            ))
            await ctx.messenger.send_text(ctx.channel_id, f"Deploy set for `{rname}`: `{command}`")
            await ctx.messenger.on_deploy_state_changed(rname)
        else:
            # Show current deploy configs
            configs = {n: ctx.store.get_deploy_config(n)
                       for n in ctx.store.list_repos() if ctx.store.get_deploy_config(n)}
            if not configs:
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    "No deploy configs.\nUsage: `/repo deploy set <name> <command>`",
                )
            else:
                lines = []
                for n, c in configs.items():
                    status = "approved" if c.get("approved") else "pending approval"
                    cmd = c.get("command", "self")
                    lines.append(f"  **{n}**: `{cmd}` ({status})")
                await ctx.messenger.send_text(ctx.channel_id, "\n".join(lines))

    elif not text:
        name, path = ctx.store.get_active_repo()
        if name:
            await ctx.messenger.send_text(ctx.channel_id, f"Active repo: {name} ({path})")
        else:
            await ctx.messenger.send_text(ctx.channel_id, "No repo set. Use /repo add <name> <path>")

    else:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /repo add|remove|create|switch|list|deploy")


# --- /budget ---

async def on_budget(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if text == "reset":
        ctx.store.reset_daily_budget()
        await ctx.messenger.send_text(ctx.channel_id, "Daily budget reset.")
    else:
        daily = ctx.store.get_daily_cost()
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Today: ${daily:.4f} / ${config.DAILY_BUDGET_USD:.2f}",
        )


# --- /clear ---

async def on_clear(ctx: RequestContext) -> None:
    count = ctx.store.archive_old()
    await ctx.messenger.send_text(ctx.channel_id, f"Archived {count} old instances.")


# --- /shutdown ---

async def on_shutdown(ctx: RequestContext) -> None:
    if _shutdown_fn:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Shutting down {config.PC_NAME}. Start the bot on another PC to switch.",
        )
        _shutdown_fn()
    else:
        await ctx.messenger.send_text(ctx.channel_id, "Shutdown not available.")


async def on_reboot(ctx: RequestContext) -> None:
    """Shut down and relaunch the bot process.

    Uses the same coalesced reboot path as autopilot-requested reboots.
    If other instances are still running, waits for them to finish first.
    """
    if not _shutdown_fn:
        await ctx.messenger.send_text(ctx.channel_id, "Reboot not available.")
        return

    # Wait for active instances/tasks to finish before queueing
    if ctx.runner.is_busy:
        ids = ", ".join(ctx.runner.active_ids) or "(between steps)"
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"⏳ Waiting for active work to finish: {ids}",
        )
        idle = await ctx.runner.wait_until_idle(timeout=300)
        if not idle:
            remaining = ", ".join(ctx.runner.active_ids)
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"⚠️ Timed out waiting. Force-rebooting with {len(ctx.runner.active_ids)} still running: {remaining}",
            )

    # Queue reboot and trigger — since we waited for idle, this fires immediately
    ctx.runner.request_reboot({
        "message": f"Manual reboot from {ctx.platform}",
        "channel_id": ctx.channel_id,
        "platform": ctx.platform,
    })


# --- /session ---

async def on_session(ctx: RequestContext, text: str) -> None:
    text = text.strip()

    if text.startswith("resume "):
        sid = text[7:].strip()
        if len(sid) < 36:
            fpath = await asyncio.to_thread(sessions_mod.find_session_file, sid)
            if not fpath:
                escaped = ctx.messenger.escape(sid)
                await ctx.messenger.send_text(
                    ctx.channel_id, f"No session found matching '{escaped}'.",
                )
                return
            sid = fpath.stem
        ctx.store.active_session_id = sid
        short = sid[:12]
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Session set: `{short}…`\nNext message will continue this session.",
        )

    elif text == "drop":
        ctx.store.active_session_id = None
        await ctx.messenger.send_text(ctx.channel_id, "Session cleared. Next message starts fresh.")

    else:
        scan_limit = 5  # Discord allows max 5 button rows
        session_list = await asyncio.to_thread(sessions_mod.scan_sessions, scan_limit, ctx.store.list_repos())
        active = ctx.store.active_session_id

        if not session_list:
            await ctx.messenger.send_text(ctx.channel_id, "No sessions found.")
            return

        buttons = []
        for s in session_list:
            is_active = active and s["id"] == active
            project = s.get("project", "?")
            age = s["age"]
            topic = s["topic"]

            # Discord button labels max 80 chars
            # Format: "✅ [bot] Add Discord Support · 2m ago"
            prefix = "✅ " if is_active else ""
            suffix = f" · {age}"
            tag = f"[{project}] "
            max_topic = 80 - len(prefix) - len(tag) - len(suffix)
            if len(topic) > max_topic:
                topic = topic[:max_topic - 1] + "…"
            btn_label = f"{prefix}{tag}{topic}{suffix}"

            buttons.append([ButtonSpec(btn_label, f"sess_resume:{s['id']}")])

        await ctx.messenger.send_text(
            ctx.channel_id, "**Recent Sessions**", buttons=buttons,
        )


# --- /deferred ---

async def on_deferred(ctx: RequestContext, args: str = "") -> None:
    """Show or clear deferred revision items for a repo."""
    parts = args.strip().split(None, 1)
    subcmd = parts[0].lower() if parts else ""

    if subcmd == "clear":
        repo_name = parts[1] if len(parts) > 1 else None
        if not repo_name:
            repo_name, _ = ctx.store.get_active_repo()
        if not repo_name:
            await ctx.messenger.send_text(ctx.channel_id, "No active repo.")
            return
        count = ctx.store.clear_deferred(repo_name)
        await ctx.messenger.send_text(
            ctx.channel_id, f"Cleared {count} deferred item(s) for `{repo_name}`.",
        )
        return

    # Default: show deferred items
    repo_name = subcmd if subcmd else None
    if not repo_name:
        repo_name, _ = ctx.store.get_active_repo()
    if not repo_name:
        await ctx.messenger.send_text(ctx.channel_id, "No active repo.")
        return

    text = ctx.store.get_deferred(repo_name)
    if not text:
        await ctx.messenger.send_text(
            ctx.channel_id, f"No deferred items for `{repo_name}`.",
        )
        return

    if len(text) > 3800:
        text = text[:3800] + "\n\n*(truncated)*"
    markup = ctx.messenger.markdown_to_markup(text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- /help ---

async def on_help(ctx: RequestContext) -> None:
    help_text = (
        "**Commands**\n"
        "Send text — continues current conversation\n"
        "Send photo — image analysis\n"
        "Send file — document analysis\n"
        "`/new` — start fresh conversation\n"
        "`/bg` — background task (build mode)\n"
        "`/list` — show instances (last 24h)\n"
        "`/kill` — terminate instance\n"
        "`/retry` — re-run instance\n"
        "`/log` — full output\n"
        "`/done` — wrap up (commit, changelog, release)\n"
        "`/diff` — git diff\n"
        "`/merge` — merge branch\n"
        "`/discard` — delete branch\n"
        "`/branches` — list orphaned branches\n"
        "`/cost` — spending breakdown\n"
        "`/status` — health dashboard\n"
        "`/logs` — bot log\n"
        "`/mode` — explore|plan|build\n"
        "`/verbose` — progress detail (0|1|2)\n"
        "`/effort` — reasoning effort (low|medium|high|max)\n"
        "`/context` — pinned context\n"
        "`/alias` — command shortcuts\n"
        "`/schedule` — recurring tasks\n"
        "`/deferred` — view/clear deferred review items\n"
        "`/repo` — repo management (add|remove|create|switch|list)\n"
        "`/session` — list/resume desktop CLI sessions\n"
        "`/budget` — budget info/reset\n"
        "`/clear` — archive old instances\n"
        "`/shutdown` — stop the bot (switch PCs)\n"
    )
    markup = ctx.messenger.markdown_to_markup(help_text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- Callback dispatch ---

async def handle_callback(
    ctx: RequestContext,
    action: str,
    instance_id: str,
    source_msg_id: str | None = None,
) -> None:
    """Dispatch a button callback action."""
    if action == "kill":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        killed = await ctx.runner.kill(instance_id)
        if killed:
            inst.status = InstanceStatus.KILLED
            inst.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.store.update_instance(inst, critical=True)
            buttons = action_button_specs(inst)
            escaped = ctx.messenger.escape(inst.display_id())
            markup = f"Killed {escaped}"
            if source_msg_id:
                await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, markup, buttons)
            else:
                await ctx.messenger.send_text(ctx.channel_id, markup, buttons)
        else:
            await ctx.messenger.send_text(ctx.channel_id, "Process not found or already stopped.")

    elif action == "retry":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        if not check_budget(ctx):
            await ctx.messenger.send_text(ctx.channel_id, "Daily budget exceeded.")
            return
        if inst.repo_path and not Path(inst.repo_path).is_dir():
            await ctx.messenger.send_text(ctx.channel_id, "Repo path no longer valid.")
            return
        new_inst = ctx.store.create_instance(
            instance_type=inst.instance_type,
            prompt=inst.prompt,
            name=f"{inst.name}-retry" if inst.name else None,
            mode=inst.mode,
        )
        new_inst.origin = inst.origin
        new_inst.origin_platform = ctx.platform
        new_inst.effort = ctx.effective_effort
        new_inst.parent_id = inst.id
        new_inst.repo_name = inst.repo_name
        new_inst.repo_path = inst.repo_path
        if inst.session_id:
            new_inst.session_id = inst.session_id
        if inst.branch:
            new_inst.branch = inst.branch
            new_inst.original_branch = inst.original_branch
            new_inst.worktree_path = inst.worktree_path
        ctx.store.update_instance(new_inst)

        if source_msg_id:
            try:
                await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, None)
            except Exception:
                pass

        escaped = ctx.messenger.escape(new_inst.display_id())
        handle = await ctx.messenger.send_thinking(
            ctx.channel_id, f"⏳ {escaped} retrying...",
            buttons=running_button_specs(new_inst.id),
        )
        if handle.get("message_id"):
            new_inst.message_ids.setdefault(ctx.platform, []).append(handle.get("message_id"))
            ctx.store.update_instance(new_inst)

        await lifecycle.run_instance(ctx, new_inst, handle=handle)

    elif action == "log":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        if inst.result_file and Path(inst.result_file).exists():
            await ctx.messenger.send_file(
                ctx.channel_id, inst.result_file, f"{inst.id}.md",
                caption=f"Full output for {inst.display_id()}",
            )
        else:
            text = inst.error or inst.summary or "No output recorded."
            escaped = ctx.messenger.escape(text)
            if source_msg_id:
                await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, escaped)
            else:
                await ctx.messenger.send_text(ctx.channel_id, escaped)

    elif action == "diff":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        if inst.diff_file and Path(inst.diff_file).exists():
            await ctx.messenger.send_file(
                ctx.channel_id, inst.diff_file, f"{inst.id}.diff",
                caption=f"Diff for {inst.display_id()}",
            )
        else:
            await ctx.messenger.send_text(ctx.channel_id, "No diff available.")

    elif action == "merge":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        branch_name = inst.branch  # Save before merge clears it
        msg = await ctx.runner.merge_branch(inst)
        ctx.store.update_instance(inst)
        # Clear stale branch refs on all sibling instances
        if branch_name and "failed" not in msg.lower():
            workflows.clear_stale_branches(ctx.store, branch_name)
            from bot.engine.deploy import update_after_merge, rescan_deploy_config_after_merge
            update_after_merge(ctx.store, inst)
            rescan_deploy_config_after_merge(ctx.store, inst.repo_name, inst.repo_path)
            await ctx.messenger.on_deploy_state_changed(inst.repo_name)
        escaped = ctx.messenger.escape(msg)
        if source_msg_id:
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, escaped)
        else:
            await ctx.messenger.send_text(ctx.channel_id, escaped)
        # Close thread if this was a post-Done merge (branch resolved)
        if inst.origin == InstanceOrigin.DONE and not inst.branch:
            try:
                await ctx.messenger.close_conversation(ctx.channel_id)
            except Exception:
                pass

    elif action == "discard":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        branch_name = inst.branch  # Save before discard clears it
        msg = await ctx.runner.discard_branch(inst)
        ctx.store.update_instance(inst)
        # Clear stale branch refs on all sibling instances
        if branch_name:
            workflows.clear_stale_branches(ctx.store, branch_name)
        escaped = ctx.messenger.escape(msg)
        if source_msg_id:
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, escaped)
        else:
            await ctx.messenger.send_text(ctx.channel_id, escaped)
        # Close thread if this was a post-Done discard (branch resolved)
        if inst.origin == InstanceOrigin.DONE and not inst.branch:
            try:
                await ctx.messenger.close_conversation(ctx.channel_id)
            except Exception:
                pass

    elif action == "wait":
        if source_msg_id:
            await ctx.messenger.edit_text(
                ctx.channel_id, source_msg_id, "Waiting... process is still running.",
            )
        else:
            await ctx.messenger.send_text(ctx.channel_id, "Waiting... process is still running.")

    elif action == "new":
        await on_new(ctx)

    elif action == "expand":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        if not inst.result_file or not Path(inst.result_file).exists():
            await ctx.messenger.send_text(ctx.channel_id, "Result file not available.")
            return
        result_text = Path(inst.result_file).read_text(encoding="utf-8")
        expanded = format_expanded_result_md(inst, result_text)
        buttons = expanded_button_specs(inst)
        markup = ctx.messenger.markdown_to_markup(expanded)
        if source_msg_id:
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, markup, buttons)
        else:
            await ctx.messenger.send_text(ctx.channel_id, markup, buttons)

    elif action == "collapse":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        collapsed = format_result_md(inst)
        buttons = action_button_specs(inst, show_expand=True)
        markup = ctx.messenger.markdown_to_markup(collapsed)
        if source_msg_id:
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, markup, buttons)
        else:
            await ctx.messenger.send_text(ctx.channel_id, markup, buttons)

    elif action == "plan":
        await workflows.on_plan(ctx, instance_id, source_msg_id)
    elif action == "build":
        await workflows.on_build(ctx, instance_id, source_msg_id)
    elif action == "review_plan":
        await workflows.on_review_plan(ctx, instance_id, source_msg_id)
    elif action == "apply_revisions":
        await workflows.on_apply_revisions(ctx, instance_id, source_msg_id)
    elif action == "review_code":
        await workflows.on_review_code(ctx, instance_id, source_msg_id)
    elif action == "commit":
        await workflows.on_commit(ctx, instance_id, source_msg_id)
    elif action == "done":
        await workflows.on_done(ctx, instance_id, source_msg_id)
    elif action == "autopilot":
        await workflows.on_autopilot(ctx, instance_id, source_msg_id)
    elif action == "build_and_ship":
        await workflows.on_build_and_ship(ctx, instance_id, source_msg_id)
    elif action == "continue_autopilot":
        inst = ctx.store.get_instance(instance_id)
        if inst:
            chain = ctx.store.get_autopilot_chain(inst.session_id)
            if chain and len(chain) > 1:
                # Skip the step that triggered the question (it's been answered)
                start = chain[1]
            elif chain:
                start = chain[0]
            else:
                start = "build"
            await workflows.on_autopilot(ctx, instance_id, source_msg_id, start_from=start)
        else:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
    elif action == "sess_resume":
        await workflows.on_sess_resume(ctx, instance_id, source_msg_id)

    elif action in ("mode_explore", "mode_plan", "mode_build"):
        target = action.split("_", 1)[1]  # "explore", "plan", or "build"
        current = ctx.effective_mode
        ctx.update_mode(target)
        actual = ctx.effective_mode  # may be capped by mode_ceiling

        # Update source message buttons to reflect new mode
        inst = ctx.store.get_instance(instance_id)
        if inst and source_msg_id:
            inst.mode = actual
            ctx.store.update_instance(inst)
            buttons = action_button_specs(inst)
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, None, buttons)

        if actual == current:
            if actual != target and ctx.mode_ceiling:
                # Capped — always explain why, even on repeat taps
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"Mode: {mode_label(actual)} (capped — your ceiling is {mode_label(ctx.mode_ceiling)})",
                    silent=True,
                )
            return  # same effective mode, skip duplicate message

        msg = f"Mode: {mode_label(actual)}"
        if actual != target and ctx.mode_ceiling:
            msg += f" (capped — your ceiling is {mode_label(ctx.mode_ceiling)})"
        await ctx.messenger.send_text(ctx.channel_id, msg, silent=True)
