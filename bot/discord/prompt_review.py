"""Discord delivery for the weekly prompt review.

The engine half (`bot/engine/prompt_review.py`) turns the eval record into a
bounded table and a brief, and parses what comes back. This half owns the parts
that touch Discord: where the review runs, how the proposal is posted, and what
Approve and Reject do.

Approve deliberately does NOT apply the diff. It opens a normal session thread
and hands the proposal over as a build brief, so a change to the bot's own
instructions goes through the same review/verify/merge path as any other work
and master stays clean. The reviewing agent could not have written those files
itself (it runs behind the read-only floor), and the approving human should not
be the only gate either.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from bot import config
from bot.claude.types import InstanceOrigin, InstanceStatus, InstanceType
from bot.engine import prompt_review as pr

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

_THREAD_NAME = "Prompt Review"
_STATE_LAST_RUN = "prompt_review_last_run_at"
_STATE_THREAD = "prompt_review_thread_id"
_STATE_PENDING = "prompt_review_pending"
_STATE_REJECTED = "prompt_review_rejected"
# Rejections are kept so a turned-down idea is not re-proposed every week.
# Bounded: the oldest fall off, because a rejection from a year of prompt
# churn ago is about text that no longer exists.
_MAX_REJECTED = 50


# --- State helpers ------------------------------------------------------------

def _state(bot: ClaudeBot) -> dict:
    return bot._store.get_platform_state("discord") or {}


def _save_state(bot: ClaudeBot, mutate) -> None:
    """Re-read, mutate, write. Never hold a copy across an await.

    Other subsystems write the same platform-state dict (log triage, forum
    bookkeeping), so a dict captured before an await and written after it
    silently discards whatever they did in between.
    """
    st = bot._store.get_platform_state("discord") or {}
    mutate(st)
    bot._store.set_platform_state("discord", st)


def rejected_targets(bot: ClaudeBot) -> list[str]:
    """Human-readable targets of every proposal turned down so far."""
    out: list[str] = []
    for entry in _state(bot).get(_STATE_REJECTED, []) or []:
        if isinstance(entry, dict):
            out.extend(str(t) for t in entry.get("targets", []))
    return out


def rejected_fingerprints(bot: ClaudeBot) -> set[str]:
    return {
        str(e.get("fingerprint"))
        for e in (_state(bot).get(_STATE_REJECTED, []) or [])
        if isinstance(e, dict) and e.get("fingerprint")
    }


# --- Repo resolution ----------------------------------------------------------

def own_repo(bot: ClaudeBot) -> tuple[str, str] | None:
    """The registered repo whose path IS this bot's checkout, or None.

    The proposal edits this bot's own prompt files, so guessing the repo is not
    an option: a wrong guess opens a build against somebody else's project and
    asks it to edit files it does not have. `list_repos()` (not the active-only
    view) is right here: the bot's own repo may perfectly well be hidden.
    """
    try:
        here = Path(config._PROJECT_ROOT).resolve()
    except OSError:
        return None
    for name, path in bot._store.list_repos().items():
        try:
            if Path(path).resolve() == here:
                return name, path
        except OSError:
            continue
    return None


# --- Thread management --------------------------------------------------------

async def _get_or_create_thread(bot: ClaudeBot) -> discord.Thread | None:
    """The persisted review thread inside The Ark, created on demand."""
    if not bot._lobby_channel_id:
        return None
    lobby = bot.get_channel(int(bot._lobby_channel_id))
    if not isinstance(lobby, discord.TextChannel):
        log.warning("Prompt review: lobby channel %s is not a TextChannel",
                    bot._lobby_channel_id)
        return None

    stored_id = _state(bot).get(_STATE_THREAD)
    if stored_id:
        existing: discord.Thread | None = None
        try:
            cached = bot.get_channel(int(stored_id))
            if isinstance(cached, discord.Thread):
                existing = cached
            else:
                fetched = await bot.fetch_channel(int(stored_id))
                if isinstance(fetched, discord.Thread):
                    existing = fetched
        except discord.NotFound:
            log.info("Prompt review thread %s gone, creating a new one", stored_id)
        except discord.HTTPException:
            log.warning("Prompt review thread lookup failed transiently",
                        exc_info=True)
            return None
        if existing is not None:
            if existing.archived:
                try:
                    await existing.edit(archived=False)
                except discord.HTTPException:
                    log.debug("Could not unarchive prompt review thread")
            return existing

    try:
        thread = await lobby.create_thread(
            name=_THREAD_NAME,
            auto_archive_duration=10080,  # 7 days (max)
            type=discord.ChannelType.public_thread,
        )
    except discord.HTTPException:
        log.exception("Prompt review: failed to create thread")
        return None

    _save_state(bot, lambda st: st.__setitem__(_STATE_THREAD, str(thread.id)))
    log.info("Created prompt review thread %s in The Ark", thread.id)
    return thread


# --- Running the review -------------------------------------------------------

# One review at a time. The weekly gate makes an overlap unlikely, but
# `/promptreview` can be typed while one is already running, and two agents
# proposing edits to the same block at once is exactly the confusion this
# feature exists to remove.
_running = {"v": False}


async def run_review(bot: ClaudeBot, days: int | None = None) -> str:
    """Run one review end to end. Returns a one-line outcome for the caller."""
    if _running["v"]:
        return "A prompt review is already running."
    if not config.EVAL_ENABLED:
        return "Session evaluation is disabled (`EVAL_ENABLED=0`), so there is nothing to review."

    repo = own_repo(bot)
    if repo is None:
        return (
            "Prompt review needs this bot's own checkout registered as a repo "
            f"(`{config._PROJECT_ROOT}`). Register it with `/repo add`."
        )
    repo_name, repo_path = repo

    thread = await _get_or_create_thread(bot)
    if thread is None:
        return "Prompt review: The Ark is not available yet."

    days = days or config.PROMPT_REVIEW_WINDOW_DAYS
    _running["v"] = True
    try:
        return await _run_review_locked(bot, days, repo_name, repo_path, thread)
    finally:
        _running["v"] = False


async def _run_review_locked(bot, days, repo_name, repo_path, thread) -> str:
    channel_id = str(thread.id)
    review_input = await asyncio.to_thread(pr.build_review_input, days)
    prompt = pr.build_review_prompt(review_input, rejected_targets(bot))

    store = bot._store
    inst = store.create_instance(
        instance_type=InstanceType.QUERY,
        prompt=prompt,
        mode="explore",
    )
    inst.origin = InstanceOrigin.PROMPT_REVIEW
    inst.origin_platform = "discord"
    inst.repo_name = repo_name
    inst.repo_path = repo_path
    # Hard read-only floor, the same one the plan-review step uses: explore
    # mode alone only blocks Edit/Write/NotebookEdit and leaves Bash open as a
    # write backdoor (sed, echo >, tee). An agent proposing edits to its own
    # constraints must not be able to make them.
    inst.bash_policy = "none"
    inst.bash_policy_baseline = "none"
    inst.effort = "high"
    store.update_instance(inst)

    ctx = bot._ctx(channel_id, repo_name=repo_name, source="prompt_review")
    ctx.mode = "explore"
    ctx.bash_policy = "none"
    # No session binding here, deliberately. This thread is not a conversation:
    # each week's review is a fresh context by design (a reviewer that
    # remembers last week's proposals argues for them). Same reasoning as the
    # chain runner. See "A thread must always know its session" in CLAUDE.md
    # for why binding is the caller's job and why this caller declines it.
    ctx.maybe_prime_briefing = None

    from bot.engine import lifecycle
    from bot.platform.formatting import running_button_specs

    handle = await ctx.messenger.send_thinking(
        channel_id,
        f"🔍 {ctx.messenger.escape(inst.display_id())} reviewing the last "
        f"{days}d of evals...",
        buttons=running_button_specs(inst.id),
    )
    if handle.get("message_id"):
        inst.message_ids.setdefault("discord", []).append(handle["message_id"])
        store.update_instance(inst)

    await lifecycle.run_instance(ctx, inst, handle=handle)

    if inst.status != InstanceStatus.COMPLETED:
        return f"Prompt review {inst.display_id()} did not complete ({inst.status.value})."

    text = inst.read_result_text() or ""
    report = pr.parse_review(text)
    await _post_outcome(bot, thread, inst, report)
    return f"Prompt review {inst.display_id()}: {len(report.proposals)} proposal(s)."


async def _post_outcome(bot, thread, inst, report: pr.ReviewReport) -> None:
    """Post the proposal (with buttons) or the quiet no-change line."""
    if report.empty:
        note = ""
        if report.ignored:
            note = "\n-# dropped: " + "; ".join(report.ignored[:3])
        try:
            await thread.send(
                f"✅ Prompt review ({inst.display_id()}): no changes proposed."
                + note,
                silent=True,
            )
        except discord.HTTPException:
            log.debug("Prompt review: no-change post failed", exc_info=True)
        return

    fp = pr.fingerprint(report.proposals)
    if fp in rejected_fingerprints(bot):
        # The agent is told not to re-propose a rejection, but being told is
        # not a mechanism. This is the mechanism.
        log.info("Prompt review %s re-proposed a rejected set (%s), not posting",
                 inst.id, fp)
        try:
            await thread.send(
                f"🔁 Prompt review ({inst.display_id()}) re-proposed a change "
                f"you already rejected, so it was suppressed.",
                silent=True,
            )
        except discord.HTTPException:
            pass
        return

    targets = [p.target() for p in report.proposals]
    _save_state(bot, lambda st: st.setdefault(_STATE_PENDING, {}).__setitem__(
        inst.id,
        {
            "fingerprint": fp,
            "targets": targets,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    ))

    embed = discord.Embed(
        title=f"Prompt review: {len(report.proposals)} proposal(s)",
        description=_embed_body(report, inst),
        color=0x5865F2,
    )
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Approve (open a build)",
        style=discord.ButtonStyle.green,
        custom_id=f"ark:promptreview:approve:{inst.id}",
    ))
    view.add_item(discord.ui.Button(
        label="Reject",
        style=discord.ButtonStyle.secondary,
        custom_id=f"ark:promptreview:reject:{inst.id}",
    ))
    try:
        await thread.send(embed=embed, view=view)
    except discord.HTTPException:
        log.exception("Prompt review: failed to post proposal for %s", inst.id)


def _embed_body(report: pr.ReviewReport, inst) -> str:
    """Embed description: summary first, full report always reachable."""
    lines: list[str] = []
    if report.net_lines:
        lines.append(f"**Net lines:** {report.net_lines}")
    for i, p in enumerate(report.proposals, 1):
        lines.append(f"**{i}. `{p.file}`** ({p.classification})")
        lines.append(f"-# {p.flag[:180]}")
    if report.ignored:
        lines.append("")
        lines.append("-# dropped: " + "; ".join(report.ignored[:3]))
    lines.append("")
    body = "\n".join(lines)

    # The diffs are the bulk and the embed limit is 4096. Include as much of
    # the report as fits, and always name the file that holds all of it. A
    # truncated proposal with no way to read the rest is worse than a pointer.
    detail = report.raw.strip()
    budget = 3900 - len(body)
    if len(detail) > budget:
        detail = detail[:max(0, budget - 40)] + "\n…(truncated)"
    body += detail
    if inst.result_file:
        body += f"\n\n-# full report: `{inst.result_file}`"
    return body[:4096]


# --- The weekly gate ----------------------------------------------------------

async def maybe_run_weekly(bot: ClaudeBot) -> None:
    """Fire a review if one is due. Called from the autonomy loop."""
    if not config.PROMPT_REVIEW_ENABLED or not config.EVAL_ENABLED:
        return
    last = _state(bot).get(_STATE_LAST_RUN)
    if not last:
        # Seed on first sight rather than firing: enabling the feature should
        # not immediately spend a run on a window nobody asked about.
        _save_state(bot, lambda st: st.__setitem__(
            _STATE_LAST_RUN, datetime.now(timezone.utc).isoformat()))
        log.info("Prompt review: seeded first-run timestamp")
        return
    if not pr.should_run_now(last):
        return

    # Stamp BEFORE running, not after. A crash or reboot mid-review must cost
    # one week's review, not turn into a run-on-every-tick loop.
    _save_state(bot, lambda st: st.__setitem__(
        _STATE_LAST_RUN, datetime.now(timezone.utc).isoformat()))
    log.info("Prompt review: weekly run starting")
    try:
        outcome = await run_review(bot)
        log.info("Prompt review: %s", outcome)
    except Exception:
        log.exception("Prompt review: weekly run failed")


# --- Buttons ------------------------------------------------------------------

async def handle_button(
    bot: ClaudeBot, interaction: discord.Interaction, custom_id: str,
) -> None:
    """Handle `ark:promptreview:<action>:<instance_id>`."""
    parts = custom_id.split(":")
    if len(parts) < 4:
        await interaction.response.send_message("Malformed button.", ephemeral=True)
        return
    action, instance_id = parts[2], parts[3]

    await interaction.response.defer(ephemeral=True)
    pending = (_state(bot).get(_STATE_PENDING) or {}).get(instance_id)
    if not pending:
        await interaction.followup.send(
            "That proposal is no longer pending. It was already approved or "
            "rejected.", ephemeral=True,
        )
        return

    if action == "reject":
        def _reject(st: dict) -> None:
            lst = st.setdefault(_STATE_REJECTED, [])
            lst.append({
                "fingerprint": pending.get("fingerprint"),
                "targets": pending.get("targets", []),
                "rejected_at": datetime.now(timezone.utc).isoformat(),
            })
            del lst[:-_MAX_REJECTED]
            (st.get(_STATE_PENDING) or {}).pop(instance_id, None)
        _save_state(bot, _reject)
        await interaction.followup.send(
            "Rejected. It will not be proposed again.", ephemeral=True,
        )
        return

    if action != "approve":
        await interaction.followup.send("Unknown action.", ephemeral=True)
        return

    inst = bot._store.get_instance(instance_id)
    report_text = inst.read_result_text() if inst else None
    if not report_text:
        await interaction.followup.send(
            "The review's report is no longer readable, so there is nothing to hand to a "
            "build.", ephemeral=True,
        )
        return

    repo = own_repo(bot)
    if repo is None:
        await interaction.followup.send(
            "This bot's own checkout is not registered as a repo, so there is "
            "nowhere to open the build.", ephemeral=True,
        )
        return

    try:
        link = await _open_build(bot, repo[0], report_text, instance_id)
    except Exception:
        log.exception("Prompt review: approve failed for %s", instance_id)
        await interaction.followup.send(
            "Could not open the build thread. See the log.", ephemeral=True,
        )
        return

    _save_state(bot, lambda st: (st.get(_STATE_PENDING) or {}).pop(instance_id, None))
    await interaction.followup.send(f"Build opened: {link}", ephemeral=True)


_APPROVE_BRIEF = """A weekly prompt review of this bot's own instructions \
produced the proposal below, and the owner approved it. Apply it.

The review agent ran read-only and could not test its own diff, so treat the \
diff as INTENT, not as a patch to apply blindly: read the current text of each \
file first, and if the quoted context no longer matches, make the equivalent \
change to what is actually there and say so.

Two rules the review itself was held to, which apply to you as well:

- Do not weaken a rule just because it is often broken. The review classified \
each finding, and only `contradicted` and `obsolete` findings were allowed to \
become edits. If applying one would delete a rule that is merely being \
disobeyed, stop and say so instead.
- Prefer deletion and consolidation. These blocks are paid for on every run of \
every session.

Update CHANGELOG.md under `## [Unreleased]`, and commit.

---

"""


async def _open_build(
    bot: ClaudeBot, repo_name: str, report_text: str, review_id: str,
) -> str:
    """Open a session thread carrying the proposal as a build brief."""
    from bot.engine import commands

    thread = await bot._forums.get_or_create_session_thread(
        repo_name,
        session_id=None,
        topic=f"Prompt review {review_id}",
        origin="prompt_review",
    )
    if thread is None:
        raise RuntimeError("get_or_create_session_thread returned None")

    channel_id = str(thread.id)
    lookup = bot._forums.thread_to_project(channel_id)
    info = lookup[1] if lookup else None
    if info is not None:
        info.mode = "build"
        bot._store.save()

    ctx = bot._ctx(channel_id, repo_name=repo_name, thread_info=info,
                   source="prompt_review_approve")
    ctx.mode = "build"
    if info is not None:
        bot._forums.attach_session_callbacks(ctx, info, channel_id)
    # A brand-new thread has no prior conversation to reconstruct, and the
    # brief below is self-contained, same reasoning as the spawn dispatch.
    ctx.maybe_prime_briefing = None

    prompt = _APPROVE_BRIEF + report_text

    async def _dispatch() -> None:
        try:
            await commands.on_text(ctx, prompt)
            if info is not None:
                bot._forums.persist_ctx_settings(ctx)
        except Exception:
            log.exception("Prompt review: build dispatch failed in %s", channel_id)

    asyncio.create_task(_dispatch())
    return f"<#{channel_id}>"
