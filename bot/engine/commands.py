"""Command handlers — auth, alias resolution, settings, scheduling, repo management.

Each method takes a RequestContext and operates via ctx.messenger.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import config
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType
from bot.engine import lifecycle, sessions as sessions_mod, workflows
from bot.platform.base import ButtonSpec, RequestContext
from bot.platform.formatting import (
    action_button_specs,
    expanded_button_specs,
    format_cost_md,
    format_expanded_result_md,
    format_instance_list_md,
    format_result_md,
    format_schedule_list_md,
    format_status_md,
    redact_secrets,
    strip_markdown,
)

log = logging.getLogger(__name__)


# --- Shared state for uptime / cli_version / shutdown ---

_start_time: float = 0.0
_cli_version: str = "unknown"
_shutdown_fn = None


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


# --- Query ---

async def on_text(ctx: RequestContext, text: str) -> None:
    """Handle a plain text message — run as query."""
    if not text.strip():
        return
    await _run_query(ctx, text)


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
    if not check_budget(ctx):
        await ctx.messenger.send_text(
            ctx.channel_id, "Daily budget exceeded. Use /budget reset to override.",
        )
        return

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
            # Fall through to active repo instead of blocking
            fallback_name, fallback_path = ctx.store.get_active_repo()
            if fallback_path:
                repo_name, repo_path = fallback_name, fallback_path
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"Repo '{ctx.repo_name}' not found — using active repo '{repo_name}'.\n"
                    f"To add it: `/repo add {ctx.repo_name} <path>`",
                    silent=True,
                )
            else:
                await ctx.messenger.send_text(
                    ctx.channel_id, f"Repo '{ctx.repo_name}' not found. Use /repo add.",
                )
                return
    else:
        repo_name, repo_path = ctx.store.get_active_repo()
    if not repo_path:
        await ctx.messenger.send_text(
            ctx.channel_id, "No repo set. Use /repo add <name> <path> first.",
        )
        return

    # Per-channel session (Discord) is authoritative; global fallback only for Telegram
    if ctx.session_id:
        resume_session = ctx.session_id
    elif ctx.platform == "discord":
        resume_session = None  # Discord channels are isolated — never use global session
    else:
        resume_session = ctx.store.active_session_id

    inst = ctx.store.create_instance(
        instance_type=InstanceType.QUERY,
        prompt=prompt,
    )
    inst.origin_platform = ctx.platform
    inst.repo_name = repo_name or ""
    inst.repo_path = repo_path or ""
    if resume_session:
        inst.session_id = resume_session
    inst.status = InstanceStatus.RUNNING
    ctx.store.update_instance(inst)

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
    )
    if handle.get("message_id"):
        inst.message_ids.setdefault(ctx.platform, []).append(handle.get("message_id"))
        ctx.store.update_instance(inst)

    on_progress, on_stall, heartbeat = lifecycle.make_progress_callbacks(
        ctx, inst, handle, ctx.store.verbose_level,
    )

    heartbeat_task = asyncio.create_task(heartbeat())
    start_time = asyncio.get_event_loop().time()
    try:
        result = await ctx.runner.run(
            inst, on_progress=on_progress, on_stall=on_stall,
            context=ctx.store.context,
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
        # For Telegram/global, update the store's global active_session_id
        if not ctx.session_id:
            ctx.store.active_session_id = result.session_id

    await lifecycle.send_result(ctx, inst, result.result_text)
    await budget_warning(ctx)

    # Check if the instance requested a bot reboot
    await lifecycle.check_reboot_request(ctx)


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
    inst.branch = f"claude-bot/{inst.id}"
    inst.status = InstanceStatus.QUEUED
    ctx.store.update_instance(inst)

    escaped = ctx.messenger.escape(inst.display_id())
    escaped_branch = ctx.messenger.escape(inst.branch)
    buttons = action_button_specs(inst)
    msg_id = await ctx.messenger.send_text(
        ctx.channel_id,
        f"🚀 {escaped} queued (build mode, branch `{escaped_branch}`)",
        buttons=buttons,
    )
    inst.message_ids.setdefault(ctx.platform, []).append(msg_id)
    ctx.store.update_instance(inst)

    asyncio.create_task(_run_bg_task(ctx, inst))


async def _run_bg_task(ctx: RequestContext, inst: Instance) -> None:
    try:
        inst.status = InstanceStatus.RUNNING
        ctx.store.update_instance(inst)

        result = await ctx.runner.run(inst, context=ctx.store.context)
        lifecycle.finalize_run(ctx, inst, result)

        await lifecycle.send_result(
            ctx, inst, result.result_text,
            silent=inst.status == InstanceStatus.COMPLETED,
        )
    except Exception:
        log.exception("Background task %s crashed", inst.id)
        inst.status = InstanceStatus.FAILED
        inst.error = "Background task crashed unexpectedly"
        ctx.store.update_instance(inst)
        try:
            await ctx.messenger.send_text(
                ctx.channel_id, f"❌ {inst.display_id()} crashed unexpectedly.",
            )
        except Exception:
            pass


# --- /list ---

async def on_list(ctx: RequestContext, text: str) -> None:
    show_all = "all" in text
    instances = ctx.store.list_instances(all_=show_all)
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
        ctx.store.update_instance(inst)
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
    new_inst.parent_id = inst.id
    new_inst.repo_name = inst.repo_name
    new_inst.repo_path = inst.repo_path
    if inst.session_id:
        new_inst.session_id = inst.session_id
    if inst.branch:
        new_inst.branch = inst.branch
        new_inst.original_branch = inst.original_branch
    ctx.store.update_instance(new_inst)

    escaped = ctx.messenger.escape(new_inst.display_id())
    handle = await ctx.messenger.send_thinking(
        ctx.channel_id, f"⏳ {escaped} retrying...",
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


# --- /cost ---

async def on_cost(ctx: RequestContext) -> None:
    daily = ctx.store.get_daily_cost()
    total = ctx.store.get_total_cost()
    top = ctx.store.get_top_spenders()
    text = format_cost_md(daily, total, top)
    markup = ctx.messenger.markdown_to_markup(text)
    await ctx.messenger.send_text(ctx.channel_id, markup)


# --- /status ---

async def on_status(ctx: RequestContext) -> None:
    uptime = _time.time() - _start_time
    active_repo, _ = ctx.store.get_active_repo()
    recent = ctx.store.list_instances()[:5]

    # Determine active platforms
    platforms = []
    if config.TELEGRAM_ENABLED:
        platforms.append("Telegram")
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
        context=ctx.store.context,
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
    if text in ("explore", "build"):
        ctx.store.mode = text
        await ctx.messenger.send_text(ctx.channel_id, f"Mode set to: {text}")
    elif text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /mode explore|build")
    else:
        await ctx.messenger.send_text(ctx.channel_id, f"Current mode: {ctx.store.mode}")


# --- /verbose ---

async def on_verbose(ctx: RequestContext, text: str) -> None:
    _VERBOSE_LABELS = {0: "silent", 1: "normal", 2: "detailed"}
    text = text.strip()
    if text in ("0", "1", "2"):
        ctx.store.verbose_level = int(text)
        await ctx.messenger.send_text(
            ctx.channel_id, f"Verbose level: {text} ({_VERBOSE_LABELS[int(text)]})",
        )
    elif text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /verbose 0|1|2")
    else:
        level = ctx.store.verbose_level
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Verbose: {level} ({_VERBOSE_LABELS.get(level, '?')})\n0 = silent, 1 = normal, 2 = detailed",
        )


# --- /context ---

async def on_context(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if text.startswith("set "):
        ctx_text = text[4:].strip()
        ctx.store.context = ctx_text
        await ctx.messenger.send_text(ctx.channel_id, f"Context set: {ctx_text[:100]}")
    elif text == "clear":
        ctx.store.context = None
        await ctx.messenger.send_text(ctx.channel_id, "Context cleared.")
    else:
        current = ctx.store.context
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

async def on_repo(ctx: RequestContext, text: str) -> None:
    text = text.strip()

    if text.startswith("add "):
        parts = text[4:].strip().split(None, 1)
        if len(parts) < 2:
            await ctx.messenger.send_text(ctx.channel_id, "Usage: /repo add <name> <path>")
            return
        name, path = parts
        path = path.strip('"\'')
        if not Path(path).is_dir():
            await ctx.messenger.send_text(ctx.channel_id, f"Directory not found: {path}")
            return
        ctx.store.add_repo(name, path)
        await ctx.messenger.send_text(ctx.channel_id, f"Repo '{name}' added: {path}")

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

    elif not text:
        name, path = ctx.store.get_active_repo()
        if name:
            await ctx.messenger.send_text(ctx.channel_id, f"Active repo: {name} ({path})")
        else:
            await ctx.messenger.send_text(ctx.channel_id, "No repo set. Use /repo add <name> <path>")

    else:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /repo add|switch|list")


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
    """Shut down and relaunch the bot process."""
    if not _shutdown_fn:
        await ctx.messenger.send_text(ctx.channel_id, "Reboot not available.")
        return

    import json
    import subprocess
    import sys

    # --- Wait for active instances to finish ---
    active_ids = ctx.runner.active_ids
    if active_ids:
        ids = ", ".join(active_ids)
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"⏳ Waiting for {len(active_ids)} active instance(s) to finish: {ids}",
        )
        idle = await ctx.runner.wait_until_idle(timeout=300)
        if not idle:
            remaining = ", ".join(ctx.runner.active_ids)
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"⚠️ Timed out waiting. Force-rebooting with {len(ctx.runner.active_ids)} still running: {remaining}",
            )

    await ctx.messenger.send_text(ctx.channel_id, f"🔄 Rebooting {config.PC_NAME}...")

    # Save reboot context so the new process can finish the turn
    reboot_data = {
        "channel_id": ctx.channel_id,
        "platform": ctx.platform,
    }
    try:
        config.REBOOT_MSG_FILE.write_text(
            json.dumps(reboot_data), encoding="utf-8",
        )
    except Exception:
        log.warning("Failed to write reboot message file", exc_info=True)

    # Use the dedicated relaunch script (no temp files, no path interpolation)
    launcher = config._PROJECT_ROOT / "scripts" / "relaunch.py"
    subprocess.Popen(
        [sys.executable, str(launcher), str(config._PROJECT_ROOT)],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    _shutdown_fn()


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
        # Discord allows max 5 button rows; Telegram is fine with more
        scan_limit = 5 if ctx.platform == "discord" else 8
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
        "`/diff` — git diff\n"
        "`/merge` — merge branch\n"
        "`/discard` — delete branch\n"
        "`/cost` — spending breakdown\n"
        "`/status` — health dashboard\n"
        "`/logs` — bot log\n"
        "`/mode` — explore|build\n"
        "`/verbose` — progress detail (0|1|2)\n"
        "`/context` — pinned context\n"
        "`/alias` — command shortcuts\n"
        "`/schedule` — recurring tasks\n"
        "`/repo` — repo management\n"
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
            ctx.store.update_instance(inst)
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
        new_inst.parent_id = inst.id
        new_inst.repo_name = inst.repo_name
        new_inst.repo_path = inst.repo_path
        if inst.session_id:
            new_inst.session_id = inst.session_id
        if inst.branch:
            new_inst.branch = inst.branch
            new_inst.original_branch = inst.original_branch
        ctx.store.update_instance(new_inst)

        if source_msg_id:
            try:
                await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, None)
            except Exception:
                pass

        escaped = ctx.messenger.escape(new_inst.display_id())
        handle = await ctx.messenger.send_thinking(
            ctx.channel_id, f"⏳ {escaped} retrying...",
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
        msg = await ctx.runner.merge_branch(inst)
        ctx.store.update_instance(inst)
        escaped = ctx.messenger.escape(msg)
        if source_msg_id:
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, escaped)
        else:
            await ctx.messenger.send_text(ctx.channel_id, escaped)

    elif action == "discard":
        inst = ctx.store.get_instance(instance_id)
        if not inst:
            await ctx.messenger.send_text(ctx.channel_id, "Instance not found.")
            return
        msg = await ctx.runner.discard_branch(inst)
        ctx.store.update_instance(inst)
        escaped = ctx.messenger.escape(msg)
        if source_msg_id:
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, escaped)
        else:
            await ctx.messenger.send_text(ctx.channel_id, escaped)

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
    elif action == "review_code":
        await workflows.on_review_code(ctx, instance_id, source_msg_id)
    elif action == "commit":
        await workflows.on_commit(ctx, instance_id, source_msg_id)
    elif action == "done":
        await workflows.on_done(ctx, instance_id, source_msg_id)
    elif action == "sess_resume":
        await workflows.on_sess_resume(ctx, instance_id, source_msg_id)
