"""Instance execution, progress callbacks, result delivery, finalization."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

from bot import config
from bot.claude.models import context_tokens_from_usage
from bot.claude.types import (
    CODE_CHANGE_TOOLS, PLAN_ORIGINS, Instance, InstanceOrigin, InstanceStatus,
    RunResult,
)
from bot.platform.base import ButtonSpec, MessageHandle, RequestContext
from bot.engine.verify import (
    add_item as verify_add_item,
    parse_verify_blocks,
    strip_verify_blocks,
)
from bot.platform.formatting import (
    action_button_specs,
    format_context_footer,
    format_duration,
    format_tokens,
    format_result_md,
    mode_name,
    parse_finalize_output,
    redact_secrets,
    running_button_specs,
    stall_button_specs,
    strip_summary_block,
)
from bot.store import history as history_mod

log = logging.getLogger(__name__)
_NOWND: dict = config.NOWND

MAX_COOLDOWN_RETRIES = 3


def _with_fallback_footer(text: str, result: RunResult) -> str:
    """Append API billing fallback notice to result text if applicable."""
    if not result.api_fallback_used:
        return text
    cost_note = f" · ~${result.cost_usd:.2f}" if result.cost_usd else ""
    return text + f"\n\n`⚡ Responded via API billing ({config.API_FALLBACK_MODEL}){cost_note}`"


def _collect_verify_items(ctx: RequestContext, inst: Instance, result_text: str) -> None:
    """Parse ```verify-board``` blocks from result and push to the repo's board.

    Fire-and-forget via asyncio.create_task — never blocks send_result.
    Only runs on Discord (where the board lives) and when a forum for
    this repo exists. Pure no-op on telegram / missing forum.
    """
    if ctx.platform != "discord":
        return
    if not result_text or not inst.repo_name:
        return
    items = parse_verify_blocks(result_text)
    if not items:
        return
    # Defense-in-depth — a hallucinated secret in a verify item would
    # otherwise persist to state.json in the clear. Items are short and
    # redaction is cheap.
    items = [redact_secrets(t) for t in items]
    bot = getattr(ctx.messenger, "_bot", None)
    forums = getattr(bot, "_forums", None) if bot else None
    if not forums:
        return
    proj = forums.forum_projects.get(inst.repo_name)
    if not proj:
        return

    # Backlink metadata — channel_id is the thread id for forum threads
    thread_id = str(ctx.channel_id) if ctx.channel_id else None
    short_name = inst.id  # e.g. "t-2842"

    async def _push() -> None:
        lock = forums.verify_lock(inst.repo_name)
        async with lock:
            added = 0
            for text in items:
                if verify_add_item(
                    proj.verify_items, text,
                    origin_thread_id=thread_id,
                    origin_thread_name=short_name,
                    origin_instance_id=inst.id,
                ):
                    added += 1
            if added:
                forums.save_forum_map()
        if added:
            forums.schedule_verify_refresh(inst.repo_name)
            log.info(
                "Added %d verify-board item(s) from %s for repo %s",
                added, inst.id, inst.repo_name,
            )

    asyncio.create_task(_push())


def _format_reset_time(reset_utc: datetime) -> str:
    """Format a UTC reset time for display (Europe/Amsterdam local time)."""
    # Guard against naive datetimes — assume UTC if no tzinfo
    if reset_utc.tzinfo is None:
        reset_utc = reset_utc.replace(tzinfo=timezone.utc)
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Amsterdam")
        local = reset_utc.astimezone(tz)
        now_local = datetime.now(tz)
        hour = local.hour % 12 or 12
        minute = local.strftime("%M")
        ampm = "AM" if local.hour < 12 else "PM"
        time_str = f"{hour}:{minute} {ampm}"
        if local.date() != now_local.date():
            time_str = f"{local.strftime('%b %d')}, {time_str}"
        return time_str
    except Exception:
        now_utc = datetime.now(timezone.utc)
        time_str = reset_utc.strftime("%H:%M UTC")
        if reset_utc.date() != now_utc.date():
            time_str = f"{reset_utc.strftime('%b %d')}, {time_str}"
        return time_str


async def schedule_cooldown_retry(
    ctx: RequestContext,
    inst: Instance,
    result: RunResult,
    silent: bool = False,
) -> bool:
    """Schedule auto-retry if usage limit detected. Returns True if scheduled."""
    if not result.usage_limit_reset or inst.cooldown_retries >= MAX_COOLDOWN_RETRIES:
        return False

    # Record limit observation for smart usage bar.
    # Use both ccusage block cost and the instance's own tracked cost —
    # ccusage may be stale (60s cache) or the block may have rolled over.
    try:
        from bot.engine.usage import get_current_block, record_block_limit_hit
        block = await get_current_block()
        block_cost = block.cost_usd if block else 0
        instance_cost = inst.cost_usd or 0
        if block_cost > 0 or instance_cost > 0:
            record_block_limit_hit(block_cost, instance_cost)
    except Exception:
        log.debug("Failed to record limit observation", exc_info=True)

    # Clamp retry time to at least 60s from now so the cooldown loop
    # picks it up on the next tick (avoids silent skip when parsed time
    # is already in the past due to timezone edge cases or slow runs).
    now = datetime.now(timezone.utc)
    retry_at = max(result.usage_limit_reset, now + timedelta(seconds=60))

    inst.cooldown_retry_at = retry_at.isoformat()
    inst.cooldown_retries += 1
    inst.cooldown_channel_id = ctx.channel_id
    ctx.store.update_instance(inst, critical=True)

    # Display the original reset time (not the clamped time) so the
    # user sees "retrying at 4:00 AM" matching the limit message.
    reset_str = _format_reset_time(result.usage_limit_reset)
    msg = (
        f"⏳ Usage limit hit — auto-retrying at {reset_str}"
        f" (attempt {inst.cooldown_retries}/{MAX_COOLDOWN_RETRIES})"
    )
    buttons = []
    # Offer pay-per-use opt-in if API key configured, budget not exhausted,
    # and this isn't an unattended autopilot chain.
    has_chain = bool(ctx.store.get_autopilot_chain(inst.session_id))
    if config.API_FALLBACK_ENABLED and not has_chain:
        daily_spend = ctx.store.get_fallback_spend_today()
        if daily_spend < config.API_FALLBACK_DAILY_MAX_USD:
            cap = config.API_FALLBACK_MAX_USD
            buttons.append([ButtonSpec(
                f"Continue with {config.API_FALLBACK_MODEL} (≤${cap:.2f})",
                f"continue_ppu:{inst.id}",
            )])
    buttons.append([ButtonSpec("Cancel Auto-Retry", f"cancel_cooldown:{inst.id}")])
    try:
        await ctx.messenger.send_text(
            ctx.channel_id, msg, buttons=buttons, silent=silent,
        )
    except Exception:
        log.exception("Failed to send cooldown message for %s", inst.id)
    return True


# Labels that don't auto-derive well from InstanceOrigin.value
_ORIGIN_LABEL_OVERRIDES: dict[InstanceOrigin, str] = {
    InstanceOrigin.DIRECT: "",
    InstanceOrigin.DONE: "wrap-up ",
}


def _origin_label(origin: InstanceOrigin) -> str:
    """Human-readable prefix for completion messages, e.g. 'review-code '."""
    if origin in _ORIGIN_LABEL_OVERRIDES:
        return _ORIGIN_LABEL_OVERRIDES[origin]
    return origin.value.replace("_", "-") + " "


# Siblings older than this are treated as zombie RUNNING instances (crashed
# runner, skipped finalize) and hidden. Sub-5s entries are filtered too —
# short-lived RUNNING blips from other threads' turn boundaries.
_SIBLING_MAX_AGE_SEC = 2 * 3600
_SIBLING_MIN_AGE_SEC = 5


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def get_sibling_summary(store, inst: Instance) -> str | None:
    """Scan running instances in same repo, return summary for system prompt."""
    if not inst.repo_name:
        return None
    now = datetime.now(timezone.utc)
    siblings: list[tuple[Instance, float]] = []
    for i in store.list_by_status(InstanceStatus.RUNNING):
        if i.repo_name != inst.repo_name:
            continue
        if i.id == inst.id:
            continue
        try:
            started = datetime.fromisoformat(i.created_at)
        except (ValueError, TypeError):
            continue
        # Legacy state.json entries may be tz-naive; assume UTC so the
        # subtraction below doesn't crash (aware - naive = TypeError).
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age_sec = (now - started).total_seconds()
        if age_sec < _SIBLING_MIN_AGE_SEC or age_sec > _SIBLING_MAX_AGE_SEC:
            continue
        siblings.append((i, age_sec))

    if not siblings:
        return None
    siblings.sort(key=lambda t: t[1], reverse=True)  # oldest first

    lines = []
    for s, age_sec in siblings[:8]:
        label = s.origin.value.replace("_", "-")
        age = _format_age(age_sec)
        # Collapse whitespace so multi-line prompts don't break the blurb
        # into phantom sibling lines when rendered in the system prompt.
        snippet = " ".join(s.prompt.split())[:60]
        lines.append(f"{s.display_id()} {label} {age} — {snippet}")
    return "Other active sessions in this repo:\n" + "\n".join(lines)


async def run_instance(
    ctx: RequestContext,
    inst: Instance,
    handle: MessageHandle | None = None,
    silent: bool = False,
) -> None:
    """Run an instance with optional live progress via handle."""
    inst.status = InstanceStatus.RUNNING
    # Reset per-run context warning flag + clear any leftover near-limit tag
    # from a previous run in the same thread.  Fire-and-forget on tag clear —
    # the thread may not be a forum thread.
    inst.warning_pinned = False
    asyncio.create_task(_try_apply_near_limit(ctx, inst, False))
    ctx.store.update_instance(inst, critical=True)

    on_progress = None
    on_stall = None
    heartbeat_task = None
    if handle:
        on_progress, on_stall, heartbeat = make_progress_callbacks(
            ctx, inst, handle, ctx.effective_verbose,
        )
        heartbeat_task = asyncio.create_task(heartbeat())

    sibling_ctx = get_sibling_summary(ctx.store, inst)

    start_time = asyncio.get_event_loop().time()
    result = None
    finalized = False
    ctx.runner.begin_task(inst.id, session_id=inst.session_id)
    try:
        try:
            result = await ctx.runner.run(
                inst, on_progress=on_progress, on_stall=on_stall,
                context=ctx.effective_context,
                sibling_context=sibling_ctx,
            )
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()

        # Update thinking message to show completion
        if handle:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= 60:
                elapsed_str = f"{elapsed / 60:.1f}m"
            else:
                elapsed_str = f"{elapsed:.0f}s"
            escaped = ctx.messenger.escape(inst.display_id())
            icon = "❓" if result.needs_input else ("✅" if not result.is_error else "❌")
            origin_label = _origin_label(inst.origin)
            status = "asking a question" if result.needs_input else ("done" if not result.is_error else "failed")
            try:
                await ctx.messenger.edit_thinking(
                    handle, f"{icon} {escaped} {origin_label}{status} ({elapsed_str})",
                )
            except Exception:
                pass

        finalize_run(ctx, inst, result)
        finalized = True

        # Usage limit: schedule auto-retry instead of showing normal failure
        if await schedule_cooldown_retry(ctx, inst, result, silent=silent):
            return  # Timer loop in app.py picks this up

        # Extract verify-board items from the original result_text BEFORE
        # stripping the fences for display. Pushes to the repo's board
        # asynchronously (fire-and-forget — never blocks delivery).
        _collect_verify_items(ctx, inst, result.result_text)

        display_text = strip_verify_blocks(result.result_text)
        await send_result(
            ctx, inst, _with_fallback_footer(display_text, result),
            silent=silent, result=result,
        )

        # Track API fallback spending for daily budget cap
        if result.api_fallback_used and result.cost_usd:
            ctx.store.add_fallback_cost(result.cost_usd)

        # Check reboot request BEFORE end_task so it's queued when end_task
        # checks for pending reboots on idle.  Safe because check_reboot_request
        # just queues (no waiting) — the actual reboot fires from end_task.
        await check_reboot_request(ctx)

    except asyncio.CancelledError:
        # Shutdown cancelled this task. The 30s drain in app.py keeps the
        # event loop alive long enough for us to attempt delivery.
        delivered = False
        if result is not None:
            # Result was computed — try to finalize + deliver it.
            # Guard against double-finalize if cancellation hit after
            # finalize_run() but before send_result().
            try:
                if not finalized:
                    finalize_run(ctx, inst, result)
                _collect_verify_items(ctx, inst, result.result_text)
                display_text = strip_verify_blocks(result.result_text)
                await send_result(
                    ctx, inst, _with_fallback_footer(display_text, result),
                    silent=silent, result=result,
                )
                delivered = True
            except (asyncio.CancelledError, Exception):
                pass
        else:
            # Killed mid-execution — mark as failed
            inst.status = InstanceStatus.FAILED
            inst.error = "Bot restarted — instance interrupted"
            inst.finished_at = datetime.now(timezone.utc).isoformat()
            ctx.store.update_instance(inst, critical=True)
        # Only overwrite thinking message if result wasn't delivered —
        # otherwise it already shows the correct completion status.
        if not delivered and handle:
            try:
                await ctx.messenger.edit_thinking(
                    handle, "⚠️ Interrupted by bot restart",
                )
            except (asyncio.CancelledError, Exception):
                pass
        raise

    finally:
        ctx.runner.end_task(inst.id)

def _repo_has_changes(repo_path: str) -> bool:
    """Check if a repo has uncommitted changes (staged or unstaged)."""
    try:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_path, capture_output=True, timeout=5, text=True, **_NOWND,
        )
        return bool(r.stdout.strip())
    except Exception:
        log.warning("Failed to check repo changes in %s", repo_path, exc_info=True)
        return False


def finalize_run(ctx: RequestContext, inst: Instance, result: RunResult) -> None:
    """Apply RunResult to Instance and persist."""
    inst.session_id = result.session_id
    inst.cost_usd = result.cost_usd
    inst.duration_ms = result.duration_ms
    inst.tools_used = result.tools_used
    inst.bash_commands = result.bash_commands
    inst.num_turns = result.num_turns
    inst.input_tokens = result.input_tokens
    inst.output_tokens = result.output_tokens
    # Prefer the final streamed usage — falls back to anything the live
    # callbacks already wrote when the result event omits usage.
    if result.context_tokens:
        inst.context_tokens = result.context_tokens
        inst.cache_read_tokens = result.cache_read_tokens
        inst.cache_creation_tokens = result.cache_creation_tokens
    if result.model:
        inst.context_model = result.model
    inst.needs_input = result.needs_input
    inst.finished_at = datetime.now(timezone.utc).isoformat()

    # Detect session context flags (plan/code) from this instance or siblings
    tools = set(result.tools_used)
    plan_tools = {"EnterPlanMode"}

    if (plan_tools & tools) or inst.origin in PLAN_ORIGINS or inst.mode == "plan":
        inst.plan_active = True
        log.debug("%s plan_active=True (tools=%s, origin=%s, mode=%s)",
                  inst.id, plan_tools & tools, inst.origin.value, inst.mode)
    if CODE_CHANGE_TOOLS & tools:
        inst.code_active = True
    elif "Agent" in tools and inst.repo_path:
        # Subagents (Agent tool) can make edits that don't appear in parent's
        # tools_used. Check git for actual uncommitted changes in the repo.
        # Use worktree path when available (worktree builds isolate changes there).
        inst.code_active = _repo_has_changes(inst.worktree_path or inst.repo_path)

    # Inherit flags from session siblings if not already set
    if inst.session_id and not (inst.plan_active and inst.code_active):
        for sibling in ctx.store.list_instances():
            if sibling.session_id == inst.session_id:
                if sibling.plan_active:
                    inst.plan_active = True
                if sibling.code_active:
                    inst.code_active = True
                if inst.plan_active and inst.code_active:
                    break

    if result.is_error and not result.needs_input:
        inst.status = InstanceStatus.FAILED
        inst.error = result.error_message or result.result_text
    else:
        inst.status = InstanceStatus.COMPLETED

    ctx.store.update_instance(inst, critical=True)

    if result.cost_usd:
        ctx.store.add_cost(result.cost_usd)

    # Append to persistent session history log (best-effort)
    _log_history(ctx, inst, result.result_text)

    # Run session evaluation (best-effort, never blocks).
    # evaluate_instance reads result text from inst.result_file so we don't
    # need to plumb result_text through.
    try:
        from bot.engine.eval import evaluate_instance
        evaluate_instance(inst)
    except Exception:
        log.debug("Eval failed for %s", inst.id, exc_info=True)


# Origins that are internal auto-loop iterations — not useful in history
_SKIP_HISTORY_ORIGINS = frozenset({
    InstanceOrigin.REVIEW_PLAN,
    InstanceOrigin.APPLY_REVISIONS,
})


def _log_history(
    ctx: RequestContext, inst: Instance, result_text: str | None = None,
) -> None:
    """Append a session history entry for completed/failed instances.

    Skips internal auto-loop origins (review_plan, apply_revisions) unless
    they failed — failures are always logged.
    """
    if inst.status not in (InstanceStatus.COMPLETED, InstanceStatus.FAILED):
        return
    # Skip noisy intermediate steps (unless they failed)
    if (inst.origin in _SKIP_HISTORY_ORIGINS
            and inst.status != InstanceStatus.FAILED):
        return

    # Build summary: prefer error for failures, result text for successes
    summary = ""
    if inst.status == InstanceStatus.FAILED:
        summary = (inst.error or "")[:300]
    elif result_text:
        # Redact secrets before persisting to history file
        summary = redact_secrets(result_text.strip())[:300]

    # For workflow steps (build, done, etc.), the prompt is a canned string.
    # Traverse parent chain to find the root instance's prompt (the user's question).
    topic = inst.prompt[:200] if inst.prompt else ""
    if inst.origin != InstanceOrigin.DIRECT and inst.parent_id:
        parent = ctx.store.get_instance(inst.parent_id)
        for _ in range(10):  # max chain depth
            if not parent or not parent.parent_id:
                break
            parent = ctx.store.get_instance(parent.parent_id)
        if parent and parent.prompt:
            topic = parent.prompt[:200]

    entry = {
        "id": inst.id,
        "thread_id": ctx.channel_id,
        "repo": inst.repo_name or "",
        "topic": topic,
        "status": inst.status.value,
        "started": inst.created_at,
        "finished": inst.finished_at,
        "summary": summary,
        "cost": f"${inst.cost_usd:.4f}" if inst.cost_usd else None,
        "branch": inst.branch,
        "mode": inst.mode,
        "origin": inst.origin.value if inst.origin else "direct",
    }
    history_mod.append_entry(entry)


def make_progress_callbacks(
    ctx: RequestContext,
    inst: Instance,
    handle: MessageHandle,
    verbose: int = 1,
):
    """Create on_progress, on_stall, and heartbeat closures.

    The latest ``message.usage`` is cached at closure scope so the context
    footer persists across the 5s throttle on_progress and the 10s heartbeat
    without flickering.
    """
    last_update = [0.0]
    start_time = asyncio.get_event_loop().time()
    last_text = [None]
    last_footer = [None]
    last_severity = [None]
    is_stalled = [False]
    last_activity = ["processing..."]  # tracks last known tool activity
    latest_usage: list[dict | None] = [None]
    near_limit_applied = [False]  # tracks current near-limit tag state
    mode_tag = f"[{inst.mode}] " if inst.mode and inst.mode != "explore" else ""

    def _elapsed() -> str:
        elapsed = asyncio.get_event_loop().time() - start_time
        if elapsed >= 60:
            return f"{elapsed / 60:.1f}m"
        return f"{elapsed:.0f}s"

    def _compute_footer() -> tuple[str | None, str | None]:
        """Render footer text + severity from cached usage. (None, None) if empty.

        Side effect: mirrors the latest token counts onto ``inst`` so the result
        embed and persisted state reflect the last live value even if no result
        event arrives (e.g. killed run).
        """
        usage = latest_usage[0]
        if not usage:
            return None, None
        tokens = context_tokens_from_usage(usage)
        if tokens <= 0:
            return None, None
        model = usage.get("model") if isinstance(usage, dict) else None
        text, pct = format_context_footer(tokens, model, inst.repo_path)
        if not text:
            return None, None
        severity = None
        if pct >= 0.95:
            severity = "crit"
        elif pct >= 0.85:
            severity = "warn"
        # Persist into the instance so result embed + session state reflect it.
        inst.context_tokens = tokens
        inst.cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
        inst.cache_creation_tokens = int(usage.get("cache_creation_input_tokens") or 0)
        if isinstance(model, str):
            inst.context_model = model
        return text, severity

    stop_buttons = running_button_specs(inst.id)

    async def _edit(text: str, buttons=None, *, footer=None, severity=None):
        # Skip no-op edits only when text/footer/severity/buttons all match.
        if (
            text == last_text[0]
            and footer == last_footer[0]
            and severity == last_severity[0]
            and not buttons
        ):
            return
        last_text[0] = text
        last_footer[0] = footer
        last_severity[0] = severity
        try:
            await ctx.messenger.edit_thinking(
                handle, text, buttons, footer=footer, severity=severity,
            )
        except Exception:
            pass

    async def _maybe_pin_warning() -> None:
        """Fire once per session when context first crosses 95%."""
        if inst.warning_pinned:
            return
        inst.warning_pinned = True
        try:
            ctx.store.update_instance(inst)
        except Exception:
            log.debug("Failed to persist warning_pinned for %s", inst.id, exc_info=True)
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                (
                    f"⚠️ `{inst.display_id()}` context is ≥95% — consider wrapping up "
                    "or starting a fresh session before the model degrades."
                ),
            )
        except Exception:
            log.debug("Failed to send 95%% warning for %s", inst.id, exc_info=True)

    async def _dispatch_severity(severity: str | None) -> None:
        """Fire time-critical severity signals — runs outside the 5s edit throttle
        so tag + pinned-warning never lag behind a rapid context spike, and the
        tag clears immediately if context drops (e.g. after compaction)."""
        want = severity in ("warn", "crit")
        if want != near_limit_applied[0]:
            near_limit_applied[0] = want
            asyncio.create_task(_try_apply_near_limit(ctx, inst, want))
        if severity == "crit":
            await _maybe_pin_warning()

    async def on_progress(message: str, detail: str = "", *, usage: dict | None = None):
        is_stalled[0] = False
        if usage is not None:
            latest_usage[0] = usage
        # Always track latest activity (even if throttled); empty message is a
        # usage-only refresh — keep prior activity.
        if message:
            display = detail if verbose >= 2 and detail else message
            last_activity[0] = display
        # Compute footer + dispatch severity BEFORE both the verbose gate and
        # the visible-edit throttle — safety signals (near-limit tag, 95%
        # warning) must fire regardless of display preference and must not
        # lag by up to 5s behind a rapid context spike.
        footer, severity = _compute_footer()
        await _dispatch_severity(severity)
        if verbose == 0:
            return
        now = asyncio.get_event_loop().time()
        if now - last_update[0] < 5:
            return
        last_update[0] = now
        escaped = ctx.messenger.escape(inst.display_id())
        escaped_display = ctx.messenger.escape(last_activity[0])
        await _edit(
            f"🔄 {mode_tag}{escaped} {escaped_display} ({_elapsed()})",
            buttons=stop_buttons,
            footer=footer,
            severity=severity,
        )

    async def on_stall(instance_id: str):
        is_stalled[0] = True
        escaped = ctx.messenger.escape(inst.display_id())
        footer, severity = _compute_footer()
        await _dispatch_severity(severity)
        await _edit(
            f"⚠️ {escaped} quiet for {config.STALL_TIMEOUT_SECS}s — /kill if stuck ({_elapsed()})",
            buttons=stall_button_specs(instance_id),
            footer=footer,
            severity=severity,
        )

    async def heartbeat():
        await asyncio.sleep(3)
        while True:
            if not is_stalled[0]:
                escaped = ctx.messenger.escape(inst.display_id())
                activity = ctx.messenger.escape(last_activity[0])
                footer, severity = _compute_footer()
                await _dispatch_severity(severity)
                await _edit(
                    f"🔄 {mode_tag}{escaped} {activity} ({_elapsed()})",
                    buttons=stop_buttons,
                    footer=footer,
                    severity=severity,
                )
            await asyncio.sleep(10)

    return on_progress, on_stall, heartbeat


async def _try_apply_near_limit(
    ctx: RequestContext, inst: Instance, apply: bool,
) -> None:
    """Fire-and-forget: apply or clear the `near-limit` Discord forum tag."""
    if ctx.platform != "discord":
        return
    bot = getattr(ctx.messenger, "_bot", None)
    if bot is None:
        return
    try:
        from bot.discord.tags import set_thread_near_limit_tag
        await set_thread_near_limit_tag(bot, ctx.channel_id, apply)
    except Exception:
        log.debug("near-limit tag update failed for %s", inst.id, exc_info=True)


async def send_result(
    ctx: RequestContext,
    inst: Instance,
    result_text: str,
    silent: bool = False,
    result: RunResult | None = None,
) -> None:
    """Send result to channel — short inline, long as summary + file."""
    has_chain = bool(ctx.store.get_autopilot_chain(inst.session_id))
    # Stamp the JSONL uuid on every chunk we send so the Branch button can
    # locate the fork point.  Only the last chunk renders the button (buttons
    # are gated to is_last below), but stamping all chunks is harmless.
    last_uuid = result.last_assistant_uuid if result else None

    def _record_msg(msg_id: str) -> None:
        inst.message_ids.setdefault(ctx.platform, []).append(msg_id)
        if last_uuid and msg_id:
            inst.jsonl_uuid_by_msg_id[str(msg_id)] = last_uuid

    buttons = action_button_specs(inst, has_autopilot_chain=has_chain)

    # Mention user on final result embed (no pending chain steps).
    mention_uid = ctx.user_id if not has_chain else None

    if result_text:
        result_text = redact_secrets(result_text)

    # Pass structured metadata for Discord embeds
    meta = {"_status": inst.status.value, "_mode": inst.mode} if inst.status else {}
    dur = format_duration(inst.duration_ms) if inst.duration_ms else None
    if dur:
        meta["Duration"] = dur
    if inst.num_turns:
        meta["Turns"] = str(inst.num_turns)
    total_tokens = inst.input_tokens + inst.output_tokens
    if total_tokens:
        meta["Tokens"] = format_tokens(total_tokens)
    if inst.cost_usd:
        meta["Cost"] = f"${inst.cost_usd:.4f}"
    if inst.branch:
        meta["Branch"] = inst.branch
    meta["Mode"] = mode_name(inst.mode)
    if inst.deferred_revisions:
        meta["_deferred_revisions"] = inst.deferred_revisions

    # Parse structured finalize output for commit/done/release origins
    is_finalize = inst.origin in (
        InstanceOrigin.COMMIT, InstanceOrigin.DONE, InstanceOrigin.RELEASE,
    )
    finalize_info = None
    if is_finalize and result_text and inst.status == InstanceStatus.COMPLETED:
        finalize_info = parse_finalize_output(result_text)
        if finalize_info:
            # Strip the raw summary block from result_text
            result_text = strip_summary_block(result_text)
            meta["_finalize"] = finalize_info

    try:
        if inst.status == InstanceStatus.FAILED or not result_text or finalize_info:
            # Failed/empty results use summary embed; finalize results use rich embed
            formatted = format_result_md(inst)
            markup = ctx.messenger.markdown_to_markup(formatted)
            msg_id = await ctx.messenger.send_result(
                ctx.channel_id, markup, metadata=meta,
                buttons=buttons, silent=silent,
                mention_user_id=mention_uid,
            )
            _record_msg(msg_id)

        elif len(result_text) < 2000:
            markup = ctx.messenger.markdown_to_markup(result_text)
            chunks = ctx.messenger.chunk_message(markup)
            # Prepend mention to first chunk so user gets pinged.
            # Force non-silent only for the chunk carrying the mention.
            mention = ctx.messenger.format_mention(mention_uid) if mention_uid else None
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                text = chunk
                chunk_silent = silent
                if mention and i == 0:
                    combined = f"{mention}\n{chunk}"
                    if len(combined) <= 2000:
                        text = combined
                        mention = None  # consumed
                        chunk_silent = False
                msg_id = await ctx.messenger.send_text(
                    ctx.channel_id, text,
                    buttons if is_last else None, chunk_silent,
                )
                _record_msg(msg_id)
            # Mention didn't fit in chunk — send separately
            if mention:
                await ctx.messenger.send_text(
                    ctx.channel_id, mention, silent=False,
                )

        else:
            expand_buttons = action_button_specs(inst, show_expand=True)
            formatted = format_result_md(inst)
            markup = ctx.messenger.markdown_to_markup(formatted)
            msg_id = await ctx.messenger.send_result(
                ctx.channel_id, markup, metadata=meta,
                buttons=expand_buttons, silent=silent,
                mention_user_id=mention_uid,
            )
            _record_msg(msg_id)

    except Exception:
        log.exception("Failed to send result for %s", inst.id)
        try:
            error_text = inst.error or inst.summary or "Result delivery failed"
            msg_id = await ctx.messenger.send_text(
                ctx.channel_id,
                f"{inst.display_id()}: {error_text[:500]}",
                silent=silent,
            )
            _record_msg(msg_id)
        except Exception:
            log.exception("Last-resort notification also failed for %s", inst.id)

    ctx.store.update_instance(inst)


async def check_reboot_request(ctx: RequestContext) -> None:
    """Check if a Claude Code instance wrote a reboot request file.

    If found, queue the reboot on the runner. The actual reboot executes
    after all active tasks finish (coalesced — multiple autopilots requesting
    reboots produce a single reboot).
    """
    try:
        raw = config.REBOOT_REQUEST_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except Exception:
        log.warning("Failed to read reboot request file", exc_info=True)
        return
    try:
        data = json.loads(raw)
    except Exception:
        log.warning("Malformed reboot request file, removing", exc_info=True)
        config.REBOOT_REQUEST_FILE.unlink(missing_ok=True)
        return

    # Delete file immediately to prevent re-reads by concurrent instances
    try:
        config.REBOOT_REQUEST_FILE.unlink()
    except OSError:
        pass

    # Attach channel context so the reboot executor knows where to notify
    data["channel_id"] = ctx.channel_id
    data["platform"] = ctx.platform

    ctx.runner.request_reboot(data)
    log.info(
        "Queued reboot request (reason: %s, total pending: %d)",
        data.get("message", "reboot requested"),
        len(ctx.runner.pending_reboots()),
    )
