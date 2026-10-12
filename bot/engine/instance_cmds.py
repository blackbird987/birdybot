"""Instance commands: /new, /bg, /release, /list, /kill, /retry, /log, /diff and /share."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from bot import config
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType, KillOutcome
from bot.engine import (
    lifecycle,
    sessions as sessions_mod,
    workflows,
)
from bot.platform.base import RequestContext
from bot.platform.formatting import (
    action_button_specs,
    format_instance_list_md,
    running_button_specs,
)

from bot.claude.runner import ClaudeRunner

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.store.state import StateStore
from bot.engine.cmd_state import check_budget

log = logging.getLogger(__name__)


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

    # TODO: alias $N substitution — see note above on_alias.
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
    inst.origin = InstanceOrigin.BG
    # Manual spawn (bypasses spawn_from) — route through the plan-vs-build
    # split so a background build lands on the strong model, not the default.
    inst.model = workflows.resolve_spawn_model(inst.origin, ctx.effective_model)
    inst.origin_platform = ctx.platform
    inst.effort = ctx.effective_effort
    inst.branch = f"{config.BRANCH_PREFIX}/{inst.id}"
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

        # Usage limit: schedule auto-retry instead of showing normal failure
        if await lifecycle.schedule_cooldown_retry(
            ctx, inst, result,
            silent=inst.status == InstanceStatus.COMPLETED,
        ):
            return  # Timer loop picks this up

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
    # Manual spawn (bypasses spawn_from) — route through the plan-vs-build
    # split so release surgery (changelog/version edits) runs on the strong model.
    inst.model = workflows.resolve_spawn_model(inst.origin, ctx.effective_model)
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

    async def _release_with_lock() -> None:
        async with ctx.runner.release_lock_scope(repo_path, "release-cmd"):
            await _run_bg_task(ctx, inst)
            # /release is the standalone (no-merge) release path: the LLM
            # commits on master directly. We still defer tag creation to the
            # bot so the same idempotent + conflict-safe codepath handles
            # both flows. Pass "HEAD" because there is no merge — the
            # release commit is on the current branch tip. Window is
            # HEAD^1..HEAD (a single commit), which assumes the LLM stops
            # after the release commit per the new RELEASE_PROMPT contract.
            # If the LLM ever lands a cleanup commit on top, this miss is
            # silent — the merge path covers the multi-commit case.
            if inst.status == InstanceStatus.COMPLETED:
                try:
                    tag, _ = await asyncio.to_thread(
                        ctx.runner._tag_release_at, repo_path, "HEAD",
                    )
                except Exception:
                    log.exception(
                        "Tag-release step raised in %s after /release", repo_path,
                    )
                    tag = None
                if tag:
                    await ctx.messenger.send_text(
                        ctx.channel_id,
                        f"Created tag `{tag}` on master. "
                        f"Run `git push origin {tag}` to publish.",
                        silent=True,
                    )

    asyncio.create_task(_release_with_lock())


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

async def kill_instance(
    store: StateStore, runner: ClaudeRunner, inst: Instance,
    *, owns_card: bool = False,
) -> KillOutcome:
    """Stop one instance as a user-requested kill. Every user Kill lands here.

    It announces intent (``reason="kill"``) so the run renders as a quiet
    tombstone rather than a red FAILED card, and so a killed run is never read
    as an account that fell over and restarted on the backup subscription.
    ``kill_and_wait``, not ``kill``, so the channel lock is always released:
    via lifecycle's finally block, or via the 10s force-clear net.
    """
    outcome = await runner.kill_and_wait(inst.id, reason="kill", owns_card=owns_card)
    # On FORCE_CLEARED the lifecycle never reached finalize (genuinely wedged),
    # so the engine layer owns the status flip — runner.py is store-agnostic.
    if outcome == KillOutcome.FORCE_CLEARED:
        inst.status = InstanceStatus.KILLED
        inst.finished_at = datetime.now(timezone.utc).isoformat()
        store.update_instance(inst, critical=True)
    return outcome


async def stop_instances(
    store: StateStore, runner: ClaudeRunner, instances: list[Instance],
) -> int:
    """Kill several instances at once (the Stop all buttons). Returns how many stopped.

    Each goes through ``kill_instance``. The Stop all buttons used to call the
    bare ``runner.kill``, which defaults to a non-intentional kill: every
    instance they stopped rendered as a red FAILED card and was eligible for
    account failover. Killed concurrently, because each wait can take seconds.
    """
    outcomes = await asyncio.gather(
        *(kill_instance(store, runner, inst) for inst in instances),
        return_exceptions=True,
    )
    stopped = 0
    for inst, outcome in zip(instances, outcomes):
        if isinstance(outcome, BaseException):
            log.debug("Failed to kill %s during stop all", inst.id, exc_info=outcome)
        elif outcome != KillOutcome.NOT_RUNNING:
            stopped += 1
    return stopped


async def perform_kill(
    ctx: RequestContext, inst: Instance, source_msg_id: str | None = None,
) -> None:
    """Stop a running instance and render the outcome. One path, two callers.

    The Kill button and typed /kill used to be separate near-copies, and that
    drift is exactly what produced the bug this function exists to prevent:
    the button was taught to announce its intent (reason="kill") and the
    command was not, so /kill fell through to the ordinary failure path — red
    FAILED card, and on a multi-account setup a silent failover that restarted
    the very work the user had just cancelled.

    *source_msg_id* is the message the Kill button was attached to, when there
    was one. Present -> we rewrite that card in place and tell the runner we
    own it, so lifecycle leaves it alone. Absent (typed /kill) -> we post a
    fresh message and lifecycle resolves the live progress card itself.
    """
    outcome = await kill_instance(
        ctx.store, ctx.runner, inst, owns_card=source_msg_id is not None,
    )
    if outcome == KillOutcome.NOT_RUNNING:
        await ctx.messenger.send_text(
            ctx.channel_id, "Process not found or already stopped.",
        )
        return
    # Re-fetch in case finalize_run wrote new fields (status, finished_at,
    # error) — render the buttons off the freshest view.
    inst = ctx.store.get_instance(inst.id) or inst
    text = f"Killed {ctx.messenger.escape(inst.display_id())}"
    buttons = action_button_specs(inst) or None
    if source_msg_id:
        await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, text, buttons)
    else:
        await ctx.messenger.send_text(ctx.channel_id, text, buttons)


async def on_kill(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if not text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /kill <id|name>")
        return

    inst = ctx.store.get_instance(text)
    if not inst:
        await ctx.messenger.send_text(ctx.channel_id, f"Instance '{text}' not found.")
        return

    await perform_kill(ctx, inst)


# --- /retry ---

async def _start_retry(
    ctx: RequestContext, inst: Instance, source_msg_id: str | None = None,
    *, pay_per_use: bool = False,
) -> tuple[Instance, dict] | None:
    """Clone `inst` for a re-run and post its progress card.

    Typed /retry and the Retry button were near-copies of this block — the
    same drift perform_kill exists to end, and the reason the button could be
    fixed while the command kept the old behaviour.  `source_msg_id` is the
    message the button sat on: present means the card it was posted under is
    cleared first, absent (the typed path) means there is no card to clear.

    `pay_per_use` is "continue on pay-per-use" from the usage-limit card: the
    clone runs on the API fallback model with a fresh cooldown budget, and the
    daily budget is not checked here because the caller gates on the
    fallback's own spending cap instead.

    Returns None, after telling the user why, when the retry cannot start.
    The caller runs the instance and backfills the thread session itself:
    run_instance never binds, and that pairing is checked at every call site
    by scripts/test_cooldown_session_bind.py.
    """
    if not pay_per_use and not check_budget(ctx):
        await ctx.messenger.send_text(ctx.channel_id, "Daily budget exceeded.")
        return None

    if inst.repo_path and not Path(inst.repo_path).is_dir():
        await ctx.messenger.send_text(ctx.channel_id, "Repo path no longer valid.")
        return None

    new_inst = ctx.store.clone_instance_for_rerun(
        inst,
        name_suffix="ppu" if pay_per_use else "retry",
        origin_platform=ctx.platform,
        effort=ctx.effective_effort,
        model=None if pay_per_use else inst.model,
    )
    if pay_per_use:
        new_inst.api_fallback = True
        new_inst.cooldown_retries = 0
    ctx.store.update_instance(new_inst)

    if source_msg_id:
        try:
            await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, None)
        except Exception:
            pass

    escaped = ctx.messenger.escape(new_inst.display_id())
    label = (
        f"{escaped} continuing with {config.API_FALLBACK_MODEL} (pay-per-use)..."
        if pay_per_use else f"⏳ {escaped} retrying..."
    )
    handle = await ctx.messenger.send_thinking(
        ctx.channel_id, label, buttons=running_button_specs(new_inst.id),
    )
    if handle.get("message_id"):
        new_inst.message_ids.setdefault(ctx.platform, []).append(handle.get("message_id"))
        ctx.store.update_instance(new_inst)
    return new_inst, handle


async def on_retry(ctx: RequestContext, text: str) -> None:
    text = text.strip()
    if not text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /retry <id|name>")
        return

    inst = ctx.store.get_instance(text)
    if not inst:
        await ctx.messenger.send_text(ctx.channel_id, f"Instance '{text}' not found.")
        return

    started = await _start_retry(ctx, inst)
    if not started:
        return
    new_inst, handle = started

    await lifecycle.run_instance(ctx, new_inst, handle=handle)
    # run_instance doesn't bind; fill a sessionless thread so a wake armed by
    # this retry has something to resume.  Never rebinds — see
    # lifecycle.backfill_thread_session.
    await lifecycle.backfill_thread_session(ctx, new_inst)


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


# --- /export (Share HTML transcript) ---

# Discord non-Nitro upload ceiling. Most guilds are actually 10 MB unless
# boost tier 2+, but we size against the hard ceiling and let Discord reject
# with a cleanly-caught error if a specific server is stricter.
_DISCORD_UPLOAD_MAX = 25 * 1024 * 1024
_SAFE_UPLOAD = 9 * 1024 * 1024


async def on_share(ctx: RequestContext, text: str) -> None:
    """Render the instance's session JSONL as a self-contained HTML transcript
    and post it as a Discord file attachment.
    """
    from bot.engine import transcript as transcript_mod

    text = (text or "").strip()
    if not text:
        await ctx.messenger.send_text(ctx.channel_id, "Usage: /export <id|name>")
        return

    inst = ctx.store.get_instance(text)
    if not inst:
        await ctx.messenger.send_text(ctx.channel_id, f"Instance '{text}' not found.")
        return

    if not inst.session_id:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"No session to share for {inst.display_id()} — instance has no session id yet.",
        )
        return

    session_file = sessions_mod.find_session_file(inst.session_id)
    if not session_file:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Session file missing for {inst.display_id()} ({inst.session_id}).",
        )
        return

    try:
        html_doc = transcript_mod.render_transcript_html(
            session_file,
            title=inst.display_id(),
            instance_summary={
                "session_id": inst.session_id,
                "prompt": inst.prompt,
                "repo": inst.repo_name or inst.repo_path,
                "mode": inst.mode,
                "effort": inst.effort,
                "cost_usd": inst.cost_usd,
                "duration_ms": inst.duration_ms,
                "num_turns": inst.num_turns,
            },
        )
    except Exception as e:
        log.exception("Transcript render failed for %s", inst.id)
        await ctx.messenger.send_text(
            ctx.channel_id, f"Failed to render transcript: {e}",
        )
        return

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".html", delete=False, mode="w", encoding="utf-8",
        ) as f:
            f.write(html_doc)
            tmp_path = f.name

        size = os.path.getsize(tmp_path)
        size_mb = size // (1024 * 1024)
        filename = f"transcript-{inst.id}.html"

        if size > _DISCORD_UPLOAD_MAX:
            # Too big to upload. TODO: gzip/truncation fallback.
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"Transcript too large ({size_mb} MB) — "
                f"exceeds Discord's {_DISCORD_UPLOAD_MAX // (1024 * 1024)} MB limit.",
            )
            return

        caption = f"Transcript for {inst.display_id()}"
        if size > _SAFE_UPLOAD:
            caption += f" ({size_mb} MB — may fail on unboosted servers)"

        await ctx.messenger.send_file(
            ctx.channel_id, tmp_path, filename, caption=caption,
        )
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
