"""`/stepback <repo>`: a read-only look at where a repo's recent work is heading.

The bot gathers the facts and a fresh session reports at most three things worth
stepping back for. It proposes and applies nothing. Run by hand for now
(docs/design/judge.md).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections import Counter

from bot.claude.sandbox import GIT_SAFE
from bot.claude.types import InstanceOrigin, InstanceType
from bot.engine import judge, lifecycle, missed
from bot.platform.base import RequestContext
from bot.platform.formatting import running_button_specs
from bot.procutil import run_capture

log = logging.getLogger(__name__)

WINDOW_DAYS = 7
_MAX_SUBJECTS_CHARS = 20_000
_GIT_TIMEOUT_SEC = 30


def _read_log(repo_path: str) -> tuple[list[str], Counter]:
    """(commit subjects newest first, commits per top-level area)."""
    result = run_capture(
        ["git", *GIT_SAFE, "log", f"--since={WINDOW_DAYS}.days", "--no-merges",
         "--format=\x01%h %s", "--name-only"],
        cwd=repo_path, timeout=_GIT_TIMEOUT_SEC, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or "git log failed")
    subjects: list[str] = []
    areas: Counter = Counter()
    for chunk in result.stdout.split("\x01"):
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
    try:
        subjects, areas = await asyncio.to_thread(_read_log, repo_path)
    except (OSError, subprocess.SubprocessError) as e:
        await ctx.messenger.send_text(ctx.channel_id, f"Could not read that repo's history: {e}")
        return
    events = await asyncio.to_thread(missed.load_events, repo=repo_name, days=WINDOW_DAYS)
    prompt = build_input(judge.repo_goal(repo_path), subjects, areas, events)

    inst = ctx.store.create_instance(
        instance_type=InstanceType.QUERY, prompt=prompt, mode="explore",
    )
    inst.origin = InstanceOrigin.STEPBACK
    inst.origin_platform = ctx.platform
    inst.repo_name = repo_name
    inst.repo_path = repo_path
    # The same floor as the other reviewers: explore alone leaves Bash as a write
    # backdoor, and anything it needs from git was fetched above.
    inst.bash_policy = "none"
    inst.bash_policy_baseline = "none"
    ctx.store.update_instance(inst)

    escaped = ctx.messenger.escape(inst.display_id())
    handle = await ctx.messenger.send_thinking(
        ctx.channel_id, f"⏳ {escaped} stepping back from {ctx.messenger.escape(repo_name)}...",
        buttons=running_button_specs(inst.id),
    )
    await lifecycle.run_instance(ctx, inst, handle=handle)
    # A follow-up in a sessionless thread can then pick the report up.
    await lifecycle.backfill_thread_session(ctx, inst)
