"""`/stepback <repo>`: a read-only look at where a repo's recent work is heading.

The bot gathers the facts and a fresh session reports at most three things worth
stepping back for. It proposes and applies nothing. Run by hand for now
(docs/design/judge.md).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from bot.claude.types import InstanceOrigin
from bot.engine import cmd_state, judge, lifecycle, missed
from bot.platform.base import RequestContext
from bot.platform.formatting import running_button_specs

log = logging.getLogger(__name__)

WINDOW_DAYS = 7
_MAX_SUBJECTS_CHARS = 20_000


async def _read_log(repo_path: str) -> tuple[list[str], Counter] | None:
    """(commit subjects newest first, commits per top-level area). None if unreadable."""
    text = await judge.git_read(
        repo_path, "log", f"--since={WINDOW_DAYS}.days", "--no-merges",
        "--format=\x01%h %s", "--name-only",
    )
    if text is None:
        return None
    subjects: list[str] = []
    areas: Counter = Counter()
    for chunk in text.split("\x01"):
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        subjects.append(lines[0])
        for area in {ln.split("/", 1)[0] for ln in lines[1:]}:
            areas[area] += 1
    return subjects, areas


def build_input(goal: str, subjects: list[str], areas: Counter, events: list) -> str:
    shown: list[str] = []
    used = 0
    for s in subjects:
        if used + len(s) > _MAX_SUBJECTS_CHARS:
            break
        shown.append(s)
        used += len(s)
    cut = (
        f"\n\nOnly the newest {len(shown)} of {len(subjects)} commit subjects are shown, for size."
        if len(shown) < len(subjects) else ""
    )
    area_text = "\n".join(f"{n:4d}  {a}" for a, n in areas.most_common(30)) or "(none)"
    if events:
        missed_text = "\n".join(
            f"- {e.ts[:10]} {e.kind} {e.head_sha or ''} {e.note or ''}".rstrip() for e in events
        )
    else:
        missed_text = "(none marked)"
    return (
        f"=== THE REPO'S GOAL ===\n\n{goal.strip() or '(none found)'}\n\n"
        f"=== COMMIT SUBJECTS, LAST {WINDOW_DAYS} DAYS, NEWEST FIRST ===\n\n"
        f"{chr(10).join(shown) or '(none)'}{cut}\n\n"
        f"=== COMMITS PER AREA ===\n\n{area_text}\n\n"
        f"=== WORK THE OWNER MARKED AS MISSED ===\n\n{missed_text}\n\n"
        "Report."
    )


async def on_stepback_command(ctx: RequestContext, repo_name: str) -> None:
    repo_path = ctx.store.list_repos().get(repo_name)
    if not repo_path:
        await ctx.messenger.send_text(ctx.channel_id, f"No repo called `{repo_name}`.")
        return
    # A slash command takes no channel lock, and a second run would take over the
    # running one's Kill button.
    spawn_err = ctx.runner.check_spawn_allowed()
    if spawn_err or ctx.runner.active_instance_for_channel(ctx.channel_id):
        await ctx.messenger.send_text(
            ctx.channel_id,
            spawn_err or "Another session is already running on this thread. Wait for it to finish.",
        )
        return
    if await cmd_state.refuse_if_over_budget(ctx):
        return
    history = await _read_log(repo_path)
    if history is None:
        await ctx.messenger.send_text(ctx.channel_id, "Could not read that repo's history.")
        return
    subjects, areas = history
    events = await asyncio.to_thread(missed.load_events, repo=repo_name, days=WINDOW_DAYS)
    if not subjects and not events:
        await ctx.messenger.send_text(ctx.channel_id, f"Nothing to report: no commits or marks in the last {WINDOW_DAYS} days.")
        return
    prompt = build_input(judge.repo_goal(repo_path), subjects, areas, events)

    inst = judge.readonly_instance(ctx, InstanceOrigin.STEPBACK, prompt, repo_name, repo_path)

    escaped = ctx.messenger.escape(inst.display_id())
    handle = await ctx.messenger.send_thinking(
        ctx.channel_id, f"⏳ {escaped} stepping back from {ctx.messenger.escape(repo_name)}...",
        buttons=running_button_specs(inst.id),
    )
    await lifecycle.run_instance(ctx, inst, handle=handle)
    # A follow-up in a sessionless thread can then pick the report up.
    await lifecycle.backfill_thread_session(ctx, inst)
