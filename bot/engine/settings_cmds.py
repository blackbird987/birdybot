"""Per-thread settings: mode, verbosity, effort, model, provider, pinned context, aliases, schedules."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from bot import config
from bot.platform.base import RequestContext
from bot.platform.formatting import (
    MODEL_CLEAR_WORDS,
    VALID_MODES,
    format_schedule_list_md,
    mode_label,
    model_suggestions,
    normalize_model,
    short_model_label,
)

log = logging.getLogger(__name__)


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


# --- /model ---

async def on_model(ctx: RequestContext, text: str) -> None:
    """View, pin, or clear the model this thread's sessions run on.

    Takes effect on the NEXT turn: the session itself is unchanged (--resume
    still carries the transcript), only the --model flag differs. There is
    deliberately no list of accepted model names -- see normalize_model.
    """
    raw = text.strip()
    suggestions = model_suggestions(ctx.store)
    # Named as examples, never as the accepted set — any well-formed name works.
    seen = f"\nSeen here: {', '.join(suggestions[:6])}" if suggestions else ""

    if not raw:
        pinned = ctx.effective_model
        if pinned:
            body = (
                f"Model: **{short_model_label(pinned)}** (pinned to this thread)\n"
                f"Change: `/model <name>` — clear: `/model default`{seen}"
            )
        else:
            body = (
                "Model: **default** — this thread follows normal routing.\n"
                f"Pin one with: `/model <name>`{seen}"
            )
        await ctx.messenger.send_text(ctx.channel_id, body)
        return

    if raw.lower() in MODEL_CLEAR_WORDS:
        ctx.update_model("")
        await ctx.messenger.send_text(
            ctx.channel_id,
            "Model: **default** — back to normal routing from the next message.",
        )
        return

    resolved = normalize_model(raw)
    if not resolved:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"`{raw}` doesn't look like a model name — "
            f"letters, digits, `.` `-` `_` `:` only, no spaces.{seen}",
        )
        return

    ctx.update_model(resolved)
    lines = [f"Model: **{short_model_label(resolved)}** — from your next message in this thread."]
    if suggestions and resolved not in suggestions:
        # Soft warning, never a block: a name this deployment hasn't run yet is
        # exactly what a newly released model looks like.
        lines.append(
            "Haven't seen this one run here before — if the CLI rejects it, "
            "`/model default` puts it back."
        )
    if ctx.runner.active_instance_for_channel(ctx.channel_id):
        lines.append("A run is in flight — it finishes on the old model.")
    await ctx.messenger.send_text(ctx.channel_id, "\n".join(lines))


# --- /provider ---

async def on_provider(ctx: RequestContext, text: str) -> None:
    """View or switch the active CLI provider."""
    from bot.claude.provider import PROVIDERS

    text = text.strip().lower()
    available = [k for k, v in PROVIDERS.items() if v is not None]

    if not text:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Provider: **{config.PROVIDER}**\n"
            f"Binary: `{config.CLAUDE_BINARY}`\n"
            f"Available: {', '.join(available)}\n"
            f"Switch: `/provider claude` or `/provider cursor`",
        )
        return

    if text not in available:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Unknown provider `{text}`. Available: {', '.join(available)}",
        )
        return

    if text == config.PROVIDER:
        await ctx.messenger.send_text(
            ctx.channel_id, f"Already using **{text}**.",
        )
        return

    old = config.PROVIDER
    try:
        config.set_provider(text)
    except RuntimeError as exc:
        await ctx.messenger.send_text(ctx.channel_id, f"Switch failed: {exc}")
        return

    # Persist so it survives reboots
    ctx.store.active_provider = text

    busy_note = ""
    if ctx.runner.is_busy:
        busy_note = (
            f"\n{ctx.runner.active_task_count} session(s) still running — "
            f"they will finish with **{old}**."
        )

    await ctx.messenger.send_text(
        ctx.channel_id,
        f"Switched: **{old}** → **{text}**\n"
        f"Binary: `{config.CLAUDE_BINARY}`\n"
        f"Prefix: `{config.BRANCH_PREFIX}`"
        + busy_note,
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
# TODO: deferred — positional var substitution ($1 $2 $N, $$ for literal $).
# Expansion happens in on_bg, which does `f"{alias_prompt} {extra}".strip()`;
# swap that for an _expand_alias(template, extra) helper. Missing args = error; extra
# args append (keeps current non-placeholder aliases working unchanged).
# See TODO.md → Features for full design. Not built because user doesn't use /alias.

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
