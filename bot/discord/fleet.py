"""Fleet ship: merge every committed session → deploy per repo → verify-back.

/fleet scans all forum threads for committed-but-unmerged work, shows a
roster with Confirm/Cancel buttons, then on confirm runs three decoupled
phases joined by persisted state:

1. Ship — merge each target via ``_finalize_merge(skip_close=True)``:
   the thread gets the merged tag + ✅ message but stays open so the
   verify report lands in a live thread (no archive→reopen churn).
   Merge failures get the standard merge-failed banner + buttons and
   ``set_pending_merge`` bookkeeping, same as the autopilot chain.
2. Deploy — per-repo gate: deploy only when ALL of that repo's merges
   succeeded. Command-based repos run ``execute_deploy`` inline (gated
   behind the post-deploy healthcheck when one is configured — deploy
   success is the drain trigger, NOT the optional healthcheck). The
   bot's own repo (``method: "self"``) requests a coalesced reboot
   instead; its verify set drains at next boot via
   ``drain_pending_verify`` called from ``bot/app.py`` startup.
3. Verify-back — pop the persisted set and replay a verify prompt into
   each shipped thread. Threads whose verify turn completes cleanly are
   closed silently; failed turns stay open and are flagged in the
   origin channel.

Pending-verify entries persist in
``platform_state["discord"]["fleet_pending_verify"]`` (same seam as
``orch_resume_payloads``) so a reboot or crash between deploy and
verify cannot lose them.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from bot import config
from bot.claude.types import Instance, InstanceStatus
from bot.platform.base import ButtonSpec
from bot.platform.formatting import merge_failed_banner, merge_failed_button_specs

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

_PENDING_KEY = "fleet_pending_verify"
# platform_state["discord"][_PENDING_KEY] = {
#     repo_name: {
#         "origin": channel_id,      # where fleet progress is reported
#         "deploy": "self" | "command" | "none",
#         # instance_id = the SHIPPED instance — fallback baseline for the
#         # close guard when no pre-replay instance can be found; the verify
#         # replay must produce a NEWER instance before auto-close.
#         "entries": [{"thread_id", "session_id", "title", "instance_id"}],
#     }
# }

# Roster tokens are in-memory only: a roster computed before a restart is
# stale by definition, so expiring it (button → "run /fleet again") is the
# correct behaviour, not a bug.
_pending_rosters: dict[str, dict] = {}


@dataclass
class ShipTarget:
    thread_id: str
    session_id: str
    inst: Instance
    repo_name: str
    title: str


# --- Collection ---


def _commits_ahead(repo_path: str, base: str, branch: str) -> int:
    """Count commits on ``branch`` not yet on ``base`` (0 on any error)."""
    try:
        r = subprocess.run(
            ["git", "-C", repo_path, "rev-list", "--count", f"{base}..{branch}"],
            capture_output=True, text=True, timeout=10, **config.NOWND,
        )
        return int(r.stdout.strip()) if r.returncode == 0 else 0
    except Exception:
        return 0


async def collect_ship_targets(bot: ClaudeBot) -> list[ShipTarget]:
    """Scan all forum threads for committed-but-unmerged work.

    A thread qualifies when its session has a completed instance with a
    live branch + worktree (``_find_session_branch_instance``) AND that
    branch has commits ahead of its merge base — branches with zero new
    commits are unfinished/no-op work, not shippable.
    Skipped: sessions with a run in flight, and sessions with persisted
    autopilot-chain state — a paused/interrupted chain owns its thread's
    lifecycle (it will merge on resume); shipping under it would race.
    """
    from bot.engine.workflows import _find_session_branch_instance

    store, runner, forums = bot._store, bot._runner, bot._forums
    chained_sessions = set((store.get_all_autopilot_chains() or {}).keys())
    targets: list[ShipTarget] = []
    seen_branches: set[str] = set()
    for proj in list(forums.forum_projects.values()):
        for tid, info in list(proj.threads.items()):
            sid = info.session_id
            if not sid or runner.is_session_active(sid) or sid in chained_sessions:
                continue
            inst = _find_session_branch_instance(
                store, sid,
                predicate=lambda i: i.status == InstanceStatus.COMPLETED,
            )
            if inst is None or not inst.branch or inst.branch in seen_branches:
                continue
            ahead = await asyncio.to_thread(
                _commits_ahead, inst.repo_path, inst.original_branch, inst.branch,
            )
            if ahead <= 0:
                continue
            seen_branches.add(inst.branch)
            targets.append(ShipTarget(
                thread_id=tid, session_id=sid, inst=inst,
                repo_name=inst.repo_name,
                title=(info.topic or "").strip() or tid,
            ))
    return targets


def _deploy_method(store, repo_name: str) -> str:
    """'self' | 'command' | 'none' — only approved configs count."""
    cfg = store.get_deploy_config(repo_name)
    if not cfg or not cfg.get("approved"):
        return "none"
    return cfg.get("method", "command")


# --- Pending-verify persistence ---


def _get_pending(store) -> dict:
    state = store.get_platform_state("discord")
    return state.get(_PENDING_KEY) or {}

def _set_pending(store, pending: dict) -> None:
    state = store.get_platform_state("discord")
    state[_PENDING_KEY] = pending
    store.set_platform_state("discord", state, persist=True)


def add_pending_verify(
    store, repo_name: str, origin: str | None,
    targets: list[ShipTarget], deploy: str,
) -> None:
    """Persist shipped threads awaiting a post-deploy verify prompt."""
    pending = _get_pending(store)
    entry = pending.get(repo_name) or {"entries": []}
    entry["origin"] = origin
    entry["deploy"] = deploy
    existing = {e["thread_id"] for e in entry["entries"]}
    for t in targets:
        if t.thread_id not in existing:
            entry["entries"].append({
                "thread_id": t.thread_id,
                "session_id": t.session_id,
                "title": t.title,
                "instance_id": t.inst.id if t.inst else None,
            })
    pending[repo_name] = entry
    _set_pending(store, pending)


def pop_pending_verify(store, repo_name: str) -> dict | None:
    """Remove and return a repo's pending-verify entry (None if absent)."""
    pending = _get_pending(store)
    entry = pending.pop(repo_name, None)
    if entry is not None:
        _set_pending(store, pending)
    return entry


# --- Command + button entry points ---


async def handle_fleet_command(bot: ClaudeBot, interaction: discord.Interaction) -> None:
    """/fleet — build the roster and ask for confirmation."""
    await interaction.response.defer()
    targets = await collect_ship_targets(bot)
    channel_id = str(interaction.channel_id)
    if not targets:
        await interaction.followup.send(
            "Nothing to ship — no sessions with committed, unmerged work.",
        )
        return

    by_repo: dict[str, list[ShipTarget]] = {}
    for t in targets:
        by_repo.setdefault(t.repo_name, []).append(t)

    lines = [f"**Fleet ship roster** — {len(targets)} session(s), {len(by_repo)} repo(s)", ""]
    for repo, ts in by_repo.items():
        method = _deploy_method(bot._store, repo)
        deploy_label = {
            "self": "deploy: bot reboot",
            "command": "deploy: command",
            "none": "no deploy configured",
        }[method]
        lines.append(f"**{repo}** ({deploy_label})")
        for t in ts:
            lines.append(f"- <#{t.thread_id}> `{t.inst.branch}`")
        lines.append("")
    lines.append("Merge all → deploy per repo → verify prompt back into each thread.")

    token = secrets.token_urlsafe(8)
    _pending_rosters[token] = {
        "thread_ids": [t.thread_id for t in targets],
        "origin": channel_id,
    }
    # Un-tapped rosters accumulate one token each; keep only the newest few
    # (dicts preserve insertion order, so the first key is the oldest).
    while len(_pending_rosters) > 8:
        _pending_rosters.pop(next(iter(_pending_rosters)))
    buttons = [[
        ButtonSpec("Confirm Ship", f"fleet_confirm:{token}"),
        ButtonSpec("Cancel", f"fleet_cancel:{token}"),
    ]]
    await bot.messenger.send_text(channel_id, "\n".join(lines), buttons=buttons)
    try:
        await interaction.delete_original_response()
    except Exception:
        pass


async def handle_fleet_button(
    bot: ClaudeBot, interaction: discord.Interaction, action: str, token: str,
) -> None:
    """Confirm/Cancel taps on the roster message (already deferred)."""
    if not bot._is_owner(interaction.user.id):
        await interaction.followup.send("Owner only.", ephemeral=True)
        return
    roster = _pending_rosters.pop(token, None)
    # Strip buttons so the roster can't be double-confirmed.
    try:
        await interaction.message.edit(view=None)
    except Exception:
        pass
    if action == "fleet_cancel":
        await interaction.followup.send("Fleet ship cancelled.", ephemeral=True)
        return
    if roster is None:
        await interaction.followup.send(
            "Roster expired (bot restarted?) — run /fleet again.", ephemeral=True,
        )
        return
    # Long-running (merges + deploys + verify turns) — never block the
    # interaction. Progress is reported to the origin channel.
    asyncio.create_task(
        run_fleet_ship(bot, roster["origin"], roster["thread_ids"]),
    )


# --- Pipeline ---


async def _say(bot: ClaudeBot, channel_id: str | None, text: str) -> None:
    if not channel_id:
        return
    try:
        await bot.messenger.send_text(channel_id, text, silent=True)
    except Exception:
        log.debug("fleet: failed to post to %s", channel_id, exc_info=True)


async def run_fleet_ship(
    bot: ClaudeBot, origin: str, thread_ids: list[str],
) -> None:
    """Phase 1+2: merge everything, then gate + deploy per repo.

    Re-resolves targets fresh (the confirmed roster may be minutes old);
    threads whose work merged or changed in the meantime drop out.
    """
    try:
        wanted = set(thread_ids)
        targets = [
            t for t in await collect_ship_targets(bot) if t.thread_id in wanted
        ]
        if not targets:
            await _say(bot, origin, "Fleet ship: nothing left to ship — roster is stale.")
            return

        by_repo: dict[str, list[ShipTarget]] = {}
        for t in targets:
            by_repo.setdefault(t.repo_name, []).append(t)

        await _say(
            bot, origin,
            f"🚢 Fleet ship: merging {len(targets)} session(s) "
            f"across {len(by_repo)} repo(s)…",
        )

        merged: dict[str, list[ShipTarget]] = {}
        failed: dict[str, list[ShipTarget]] = {}
        for repo, ts in by_repo.items():
            for t in ts:
                ok = await _ship_one(bot, t)
                (merged if ok else failed).setdefault(repo, []).append(t)

        if failed:
            fail_lines = ["⚠ Merge failures — deploy skipped for these repos:"]
            for repo, ts in failed.items():
                for t in ts:
                    fail_lines.append(f"- {repo}: <#{t.thread_id}> (fix in-thread, then /fleet again)")
            await _say(bot, origin, "\n".join(fail_lines))

        # Per-repo gate: never deploy a half-shipped repo. Deploy the bot's
        # own repo LAST — a self deploy reboots the process and would kill
        # the remaining command deploys mid-flight.
        deployable = [r for r in by_repo if r not in failed and merged.get(r)]
        deployable.sort(key=lambda r: _deploy_method(bot._store, r) == "self")
        for repo in deployable:
            await _deploy_and_verify(bot, origin, repo, merged[repo])
    except Exception:
        log.exception("fleet ship pipeline failed")
        await _say(bot, origin, "❌ Fleet ship hit an internal error — check logs.")


async def _ship_one(bot: ClaudeBot, t: ShipTarget) -> bool:
    """Merge one thread's branch; on failure post the standard merge-failed UI."""
    from bot.engine.workflows import _finalize_merge

    lookup = bot._forums.thread_to_project(t.thread_id)
    info = lookup[1] if lookup else None
    ctx = bot._ctx(
        t.thread_id, session_id=t.session_id, repo_name=t.repo_name,
        thread_info=info, source="fleet",
    )
    try:
        ok = await _finalize_merge(ctx, t.inst, close_silent=True, skip_close=True)
    except Exception:
        log.exception("fleet: merge crashed for %s", t.thread_id)
        return False
    if ok:
        # Clear any stale failed-merge record from an earlier attempt —
        # while one exists, every prompt dispatched into the thread gets
        # the "[system note: auto-merge still unresolved]" prefix, which
        # would wrap the verify prompt in a false warning. Cleared by all
        # three keyings: the prompt-prefix reads by CHANNEL (survives
        # session-id rotation), resume flows read by session/instance.
        pm_ch = ctx.store.get_pending_merge_by_channel(t.thread_id)
        if pm_ch:
            ctx.store.clear_pending_merge(pm_ch[0])
        pm = ctx.store.get_pending_merge_by_session(t.session_id)
        if pm:
            ctx.store.clear_pending_merge(pm[0])
        ctx.store.clear_pending_merge(t.inst.id)
    else:
        failure_kind = ctx.runner._last_merge_failure_kind.get(t.inst.id)
        msg = merge_failed_banner(failure_kind)
        try:
            await ctx.messenger.send_text(
                t.thread_id, msg,
                buttons=merge_failed_button_specs(t.inst.id), silent=True,
            )
        except Exception:
            log.debug("fleet: failed to post merge-failed banner", exc_info=True)
        ctx.store.set_pending_merge(
            t.inst.id,
            session_id=t.session_id,
            channel_id=t.thread_id,
            repo_name=t.repo_name,
            message=msg,
            failure_kind=failure_kind,
        )
    return ok


async def _deploy_and_verify(
    bot: ClaudeBot, origin: str, repo_name: str, targets: list[ShipTarget],
) -> None:
    """Phase 2+3 for one repo: deploy, then drain verify-backs.

    The pending-verify set is persisted BEFORE the deploy so a self-deploy
    (which reboots the process) or a crash mid-deploy can't lose it.
    """
    store = bot._store
    method = _deploy_method(store, repo_name)
    add_pending_verify(store, repo_name, origin, targets, deploy=method)

    if method == "self":
        # Say it BEFORE requesting: a queued reboot on an idle runner can
        # shut the process down before a follow-up message would send.
        await _say(
            bot, origin,
            f"♻️ **{repo_name}**: self-managed — reboot requested; verify "
            f"prompts for {len(targets)} thread(s) fire after it's back online.",
        )
        from bot.claude.runner import RebootResult

        # Coalesced reboot: executes when the runner goes idle, so any
        # verify turns already running for other repos finish first.
        result = bot._runner.request_reboot({
            "message": f"Fleet ship: deploy {repo_name}",
            "channel_id": origin,
            "platform": "discord",
        })
        if result is RebootResult.DEFERRED:
            # Deferred = other work active; safe to send a follow-up. The
            # deferred file auto-promotes at the next idle session-end but
            # is dropped as stale after REBOOT_DEFERRED_TTL_SECS — tell the
            # user the fallback, since the verify set survives either way.
            await _say(
                bot, origin,
                f"⏳ **{repo_name}**: reboot deferred until current work "
                f"finishes (auto-retries for ~1 h). If it gets dropped as "
                f"stale, tap Reboot in the control room — the verify "
                f"prompts are saved and fire at the next boot.",
            )
        return  # boot-time drain_pending_verify handles the rest

    if method == "command":
        from bot.discord.interactions import (
            _post_deploy_healthcheck, _spawn_deploy_fix, execute_deploy,
        )
        cfg = store.get_deploy_config(repo_name) or {}
        success, output, err = await execute_deploy(bot, repo_name, cfg)
        healthcheck = cfg.get("healthcheck")
        hc_failed = False
        if success and healthcheck:
            hc_ok = await _post_deploy_healthcheck(
                bot, repo_name, cfg, healthcheck,
            )
            if not hc_ok:
                success = False
                hc_failed = True
                err = "post-deploy healthcheck failed"
        if not success:
            # Prod wasn't (cleanly) updated — verifying against it would
            # report on stale code. Drop the set and say so.
            pop_pending_verify(store, repo_name)
            await _say(
                bot, origin,
                f"❌ **{repo_name}**: deploy failed ({err}) — verify skipped "
                f"for {len(targets)} thread(s). Work IS merged to master.",
            )
            if cfg.get("auto_fix") and not hc_failed:
                # Healthcheck failures already spawn their own fix session
                # inside _post_deploy_healthcheck; only command failures
                # need one triggered here.
                asyncio.create_task(
                    _spawn_deploy_fix(bot, repo_name, cfg, output, err),
                )
            return
        await _say(
            bot, origin,
            f"✅ **{repo_name}**: deployed. Verifying {len(targets)} thread(s)…",
        )
    else:
        await _say(
            bot, origin,
            f"ℹ️ **{repo_name}**: no approved deploy config — merged to "
            f"master; sending verify prompts anyway.",
        )

    await drain_pending_verify(bot, repo_name)


# --- Verify-back ---


def _verify_prompt(repo_name: str, deploy: str) -> str:
    if deploy == "self":
        deployed = "and the bot has been rebooted onto the new code"
    elif deploy == "command":
        deployed = "and the deploy command for this repo ran successfully"
    else:
        deployed = (
            "(no deploy is configured for this repo, so it is live "
            "wherever master is consumed)"
        )
    return (
        f"[Fleet ship] The work in this thread was just merged to master {deployed}.\n\n"
        "Verify the shipped change actually works in production: exercise the "
        "feature or check the logs/diagnostics relevant to what this session "
        "changed. Then report either:\n"
        "- ✅ verified working, with brief evidence, or\n"
        "- what's broken and the exact next step to fix it.\n"
        "Keep it short — this is a post-deploy verification pass."
    )


async def drain_pending_verify(bot: ClaudeBot, repo_name: str | None = None) -> int:
    """Fire verify prompts for pending fleet-shipped threads.

    ``repo_name=None`` drains every repo — the boot path uses this to pick
    up sets left by a self-deploy reboot (or a crash mid-pipeline).
    Returns the number of verify prompts dispatched.
    """
    store = bot._store
    repos = [repo_name] if repo_name else list(_get_pending(store).keys())
    count = 0
    for repo in repos:
        entry = pop_pending_verify(store, repo)
        if not entry:
            continue
        deploy = entry.get("deploy", "none")
        origin = entry.get("origin")
        for e in entry.get("entries", []):
            # Parallel tasks, like real user messages in parallel threads —
            # each verify is a full Claude turn and they're independent.
            asyncio.create_task(_verify_one(bot, repo, deploy, origin, e))
            count += 1
    return count


def _newest_instance(bot: ClaudeBot, thread_id: str, fallback_sid: str | None):
    """Newest instance for a thread's CURRENT session (replays rotate ids)."""
    lookup = bot._forums.thread_to_project(thread_id)
    sid = (lookup[1].session_id if lookup else None) or fallback_sid
    return next(
        (i for i in bot._store.list_instances(all_=True) if sid and i.session_id == sid),
        None,
    )


async def _verify_one(
    bot: ClaudeBot, repo_name: str, deploy: str, origin: str | None, e: dict,
) -> None:
    """One thread's verify turn: replay prompt, then close on clean completion."""
    thread_id = e.get("thread_id") or ""
    title = (e.get("title") or "").strip() or f"<#{thread_id}>"

    # Snapshot the newest run BEFORE the replay: past turns are COMPLETED
    # by definition (including the shipped one), so "newest run completed"
    # alone would false-positive when the replay dispatches but no turn
    # actually runs (drain gate, usage limit, spawn refusal). The shipped
    # instance id is the persisted fallback when nothing is found (e.g.
    # instances pruned across a reboot).
    pre = _newest_instance(bot, thread_id, e.get("session_id"))
    pre_id = pre.id if pre is not None else e.get("instance_id")

    try:
        ok = await bot._replay_to_thread(
            thread_id, _verify_prompt(repo_name, deploy), source="fleet_verify",
        )
    except Exception:
        log.exception("fleet: verify replay crashed for %s", thread_id)
        ok = False
    if not ok:
        await _say(
            bot, origin,
            f"⚠ Fleet verify: couldn't resume <#{thread_id}> ({title}) — check it manually.",
        )
        return

    # Only close when the replay produced a NEW run that finished cleanly
    # and didn't end on a question for the user.
    inst = _newest_instance(bot, thread_id, e.get("session_id"))
    verified = (
        inst is not None
        and inst.id != pre_id
        and inst.status == InstanceStatus.COMPLETED
        and not inst.needs_input
    )
    if verified:
        # Verify turn finished cleanly — close the thread. Silent: the
        # verify report embed in-thread + the origin summary carry the signal.
        try:
            await bot.messenger.close_conversation(thread_id, skip_mention=True)
        except Exception:
            log.debug("fleet: close after verify failed for %s", thread_id, exc_info=True)
        await _say(bot, origin, f"✅ Fleet verify done: <#{thread_id}> — thread closed.")
    else:
        await _say(
            bot, origin,
            f"⚠ Fleet verify: <#{thread_id}> needs attention — thread left open.",
        )
