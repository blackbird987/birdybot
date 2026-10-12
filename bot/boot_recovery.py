"""What a fresh boot does about the previous process: the portability audit,
the reboot note, orphaned progress cards, interrupted chains and stale
pending-prompt embeds.

All of it is called once from ``bot.app.run``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from bot import config, paths
from bot.claude import memory as claude_memory
from bot.claude.types import InstanceStatus
from bot.platform.base import NotificationService
from bot.store.state import StateStore
from bot.textutil import parse_iso_utc

log = logging.getLogger(__name__)


def audit_repo_portability(store: StateStore) -> None:
    """Warn about registered repos that only exist on this machine.

    The state file is shared between machines, so a repo registered outside
    every known root simply will not be there after the next reboot into the
    other OS.  Worse, it can be a *second checkout* of a repo whose history
    all sits under the shared copy — identical contents, different folder, so
    nothing looks wrong except that no conversation in it can ever resume.
    That cost a night of 16-minute retry loops before anyone noticed, and the
    whole diagnosis is one line the bot was in a position to print at boot.

    Log-only, deliberately: which copy is the right one is a judgement call,
    and silently re-pointing a repo is a far worse failure than saying so.
    """
    log.info("%s", paths.describe())
    repos = store.list_repos()
    if not repos:
        return
    stray = {n: p for n, p in repos.items() if not paths.is_portable(p)}
    if not stray:
        return
    for name, path in sorted(stray.items()):
        log.warning(
            "Repo %r is outside every known root (%s) — it will not resolve "
            "on another machine sharing this state file, and CLI history "
            "written elsewhere for the same project will not be found from "
            "here. Re-register it under a shared root, or add its root to "
            "data/%s.",
            name, path, paths.ROOTS_FILENAME,
        )
    log.warning(
        "Repo portability: %d of %d registered repos are machine-local",
        len(stray), len(repos),
    )


async def read_reboot_note(notifier: NotificationService) -> dict | None:
    """If a reboot_message.json was left by a previous process, read and return it.

    The caller handles broadcasting, optional thread resume, and file cleanup.
    """
    import json
    try:
        data = json.loads(config.REBOOT_MSG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    log.info(
        "Read reboot message for %s channel %s (deferred)",
        data.get("platform"), data.get("channel_id"),
    )
    return data


async def restore_pending_prompts(discord_bot) -> None:
    """Reconcile persisted pending-prompt entries after a reboot.

    At reboot all instances are gone — so every persisted pending entry is
    stale (the run it was queued behind is dead).  For each entry: edit the
    Queued embed to say "Lost on restart — please resend", then drop it from
    disk.  If a live replacement is needed later, the user sends again.
    """
    from bot.engine import pending as pending_mod

    if not await discord_bot._wait_for_ready("restore_pending_prompts"):
        return
    entries = pending_mod.load_from_disk()
    if not entries:
        return
    log.info("Reconciling %d pending-prompt entries from prior run", len(entries))
    for p in entries:
        if not p.message_id:
            continue
        try:
            await discord_bot.messenger.edit_text(
                p.channel_id, p.message_id,
                "Lost on restart — please resend your message.", None,
            )
        except Exception:
            log.debug("Could not edit stale pending embed %s", p.message_id)
    pending_mod.clear_persisted_file()


async def cleanup_orphan_messages(
    notifier: NotificationService,
    orphans: list,
    auto_resuming_sessions: set[str] | None = None,
    drain_callback_channel_ids: set[str] | None = None,
    interrupt_reason: str | None = None,
) -> None:
    """Update thinking messages for instances that were interrupted by a restart.

    Finds the last message sent by each orphaned instance and edits it to show
    the interrupted status, so users see a clean resolution instead of a stale
    'processing...' indicator.

    Skips instances that will be auto-resumed (via chain resume or drain queue
    callback) to avoid the confusing "interrupted" → immediate restart sequence.

    ``interrupt_reason`` names the cause when startup managed to establish one
    (currently: the previous run was OOM-killed). It lands in the thread the
    user is actually looking at, which is the difference between "something
    happened at 2 AM" and an answer.
    """
    if not orphans:
        return
    _resuming = auto_resuming_sessions or set()
    _drain_cbs = drain_callback_channel_ids or set()
    from bot.platform.base import MessageHandle
    for inst in orphans:
        # Skip instances that will auto-resume via chain or drain callback
        if inst.session_id and inst.session_id in _resuming:
            continue

        for platform, msg_ids in inst.message_ids.items():
            if not msg_ids or platform not in notifier._messengers:
                continue
            messenger, _ = notifier._messengers[platform]
            # The last message_id is typically the thinking/progress message
            last_msg_id = msg_ids[-1]
            try:
                channel_id = None
                if inst.session_id and hasattr(messenger, 'find_channel_for_session'):
                    channel_id = messenger.find_channel_for_session(inst.session_id)
                # Also skip if channel is in drain callback set
                if channel_id and channel_id in _drain_cbs:
                    continue
                if channel_id:
                    handle = MessageHandle(
                        platform=platform,
                        _data={"message_id": last_msg_id, "channel_id": channel_id},
                    )
                    try:
                        await messenger.edit_thinking(
                            handle,
                            f"{inst.display_id()} interrupted by bot restart"
                            + (f" — {interrupt_reason}" if interrupt_reason else ""),
                        )
                        log.info("Updated orphan thinking msg for %s in %s:%s",
                                 inst.id, platform, channel_id)
                    except Exception:
                        log.debug("Could not edit orphan thinking msg for %s", inst.id)
            except Exception:
                log.debug("Orphan message cleanup failed for %s", inst.id, exc_info=True)


def _find_last_completed_instance(store, session_id: str):
    """Find the most recent COMPLETED instance for a session.

    Returns the last successfully completed step — the one whose output
    the next chain step should build upon.  Skips FAILED instances
    (which include the interrupted step marked by mark_orphans).
    """
    for inst in store.list_instances(all_=True):
        if inst.session_id == session_id and inst.status == InstanceStatus.COMPLETED:
            return inst
    return None


async def resume_interrupted_chains(
    store, discord_bot, drain_callback_channel_ids: set[str],
) -> None:
    """Find autopilot chains that were interrupted by restart and resume them.

    Skips sessions whose thread already has a callback entry in the drain queue
    (that callback will replay the equivalent work).  Text-only drain entries
    do NOT suppress chain resume — they answer a user message but don't
    continue the autopilot chain.

    Brief delay lets drain queue dispatch start first and acquire its locks.
    """
    from datetime import datetime, timedelta, timezone

    from bot.engine import workflows
    from bot.engine.commands import _get_channel_lock

    if not await discord_bot._wait_for_ready("resume_interrupted_chains"):
        return

    # Brief delay to let drain queue dispatch start and acquire channel locks
    await asyncio.sleep(5)

    all_chains = store.get_all_autopilot_chains()
    if not all_chains:
        return

    log.info("Found %d interrupted autopilot chains to resume", len(all_chains))
    # Stale-chain TTL: a queue older than this is presumed abandoned and
    # dropped. Long enough to cover an overnight pause (user steps away with
    # a needs_input pending), short enough to avoid resurrecting weeks-old
    # work after a long downtime.
    stale_chain_ttl = timedelta(hours=48)
    now = datetime.now(timezone.utc)
    for session_id, steps in all_chains.items():
        meta = store.get_autopilot_chain_meta(session_id) or {}
        status = meta.get("status")
        updated_at = meta.get("updated_at")

        # Defensive: any chain without meta got dropped at load. If we still
        # see one here, something added a queue without going through
        # set_autopilot_chain — log and skip rather than guess intent.
        if not status:
            log.warning(
                "Skipping unmetered chain (post-load) for session %s — clearing",
                session_id,
            )
            store.clear_chain_state(session_id)
            continue

        # Paused chains are waiting on the user (needs_input, phantom, phase
        # gate). They MUST NOT resume on reboot — that's the bug this whole
        # field exists to prevent.
        if status == "paused":
            log.info(
                "Skipping paused autopilot chain for session %s (steps: %s)",
                session_id, steps,
            )
            continue

        # Stale running chains: crash happened too long ago to safely resume.
        if updated_at:
            ts = parse_iso_utc(updated_at)
            if ts is None:
                log.warning(
                    "Unparseable chain meta updated_at for session %s: %r — clearing",
                    session_id, updated_at,
                )
                store.clear_chain_state(session_id)
                continue
            if now - ts > stale_chain_ttl:
                log.warning(
                    "Dropping stale running chain for session %s (age %s, steps: %s)",
                    session_id, now - ts, steps,
                )
                store.clear_chain_state(session_id)
                continue

        # Find the thread for this session
        lookup = discord_bot._forums.session_to_thread(session_id)
        if not lookup:
            log.warning(
                "No thread found for interrupted chain session %s — clearing",
                session_id,
            )
            store.clear_chain_state(session_id)
            continue

        thread_id, info = lookup

        # Skip sessions whose thread has a callback in the drain queue
        if thread_id in drain_callback_channel_ids:
            log.info(
                "Skipping chain resume for session %s — thread %s covered by drain queue callback",
                session_id, thread_id,
            )
            continue

        # Find the last COMPLETED instance to use as source
        source = _find_last_completed_instance(store, session_id)
        if not source:
            log.warning(
                "No completed instance for interrupted chain session %s — clearing",
                session_id,
            )
            store.clear_chain_state(session_id)
            continue

        log.info(
            "Resuming autopilot chain for session %s in thread %s (steps: %s)",
            session_id, thread_id, steps,
        )

        ctx = discord_bot._ctx(thread_id, session_id=session_id, thread_info=info)
        discord_bot._forums.attach_session_callbacks(ctx, info, thread_id)

        # Notify user that chain is resuming
        try:
            await ctx.messenger.send_text(
                thread_id, "Resuming interrupted chain...", silent=True,
            )
        except Exception:
            log.debug("Could not send chain resume notice to %s", thread_id)

        # Acquire channel lock to prevent racing with user messages
        lock = _get_channel_lock(thread_id)
        async with lock:
            try:
                # Re-run from chain[0] — the interrupted step that never
                # completed.  This differs from resume_autopilot_chain()
                # which skips [0] (designed for needs_input pauses where
                # [0] DID complete).
                label, silent_close = store.get_chain_kwargs(session_id)
                await workflows._run_autopilot_chain(
                    ctx, source.id, None, steps, session_id,
                    silent_close=silent_close,
                    chain_label=label,
                )
            except Exception:
                log.exception(
                    "Failed to resume autopilot chain for session %s", session_id,
                )
            finally:
                await discord_bot._after_turn(ctx, thread_id, dashboard=False)


def log_orphan_git(store: StateStore, runner) -> None:
    """Warn about branches and worktrees no known instance accounts for."""
    known = store.list_instances(all_=True)
    active_branches = {inst.branch for inst in known if inst.branch}
    active_worktrees = {inst.worktree_path for inst in known if inst.worktree_path}
    for repo_name, repo_path in store.list_repos().items():
        if not Path(repo_path).is_dir():
            continue
        orphan_branches = runner.scan_orphan_branches(repo_path, active_branches)
        if orphan_branches:
            log.warning(
                "Repo '%s' has %d orphaned branches: %s",
                repo_name, len(orphan_branches),
                ", ".join(orphan_branches[:5]) + ("..." if len(orphan_branches) > 5 else ""),
            )
        orphan_wts = runner.scan_orphan_worktrees(repo_path, active_worktrees)
        if orphan_wts:
            log.warning(
                "Repo '%s' has %d orphaned worktrees: %s",
                repo_name, len(orphan_wts),
                ", ".join(orphan_wts[:5]) + ("..." if len(orphan_wts) > 5 else ""),
            )


async def announce_reboot(notifier: NotificationService, discord_bot) -> None:
    """Say the bot is back, and resume the thread the reboot note names.

    No-op on a boot that was not preceded by a reboot note.
    """
    reboot_data = await read_reboot_note(notifier)
    if not reboot_data:
        return

    # Wait for notifier to have at least one messenger registered
    for _ in range(60):
        if notifier._messengers:
            break
        await asyncio.sleep(1)

    # Always broadcast "back online" to The Ark (persistent — no TTL)
    reason = reboot_data.get("message", "")
    back_msg = f"✅ {config.PC_NAME} back online."
    if reason:
        back_msg += f" ({reason})"
    await notifier.broadcast(back_msg)

    # Additionally resume a specific thread if channel_id was set
    _resume_channel = reboot_data.get("channel_id")
    _resume_platform = reboot_data.get("platform")
    _resume_prompt = reboot_data.get("resume_prompt")

    if _resume_channel and _resume_platform == "discord" and discord_bot:
        asyncio.create_task(discord_bot.dispatch_resume(
            _resume_channel, _resume_prompt or "", announce=None,
        ))
        log.info("Dispatched post-reboot resume to discord channel %s", _resume_channel)

    # Clean up reboot file (dispatch_resume also tries via unlink(missing_ok=True))
    try:
        config.REBOOT_MSG_FILE.unlink(missing_ok=True)
    except Exception:
        pass


async def post_oom_notice(discord_bot, orphans: list, oom_reason: str | None) -> None:
    """One Ark notice for the whole outage, not one per dead session: an OOM kill
    takes every running session at once, so per-thread notices would say the
    same thing ten times over.

    The reason is taken from the marker as well as from this boot's own
    journal read, and that fallback is the marker's entire reason to exist.
    The orphans are marked failed by the *first* boot after the kill, so if
    that boot dies anywhere between marking them and getting here, the next
    one finds nothing orphaned, never asks the journal, and the outage is
    never reported at all. Cleared only once the notice has actually landed,
    for the same reason — clearing it first would turn one failed send into
    silence.
    """
    notice_reason = oom_reason or claude_memory.read_oom_marker(config.DATA_DIR)
    if discord_bot and notice_reason:
        lobby_id = str(getattr(discord_bot, "_lobby_channel_id", "") or "")
        if lobby_id:
            # Zero is the normal count on the second-boot path above: the
            # sessions were already marked failed by the boot that died.
            took = (
                f", taking {len(orphans)} running session(s) with it"
                if orphans else ""
            )
            try:
                await discord_bot.messenger.send_text(
                    lobby_id,
                    f"⚠️ **The bot was killed for memory and has restarted.** "
                    # Not .capitalize() — that lowercases everything after the
                    # first letter, so the day this reason carries a figure the
                    # notice would read "13.9g".
                    f"{notice_reason[:1].upper()}{notice_reason[1:]}{took}.\n"
                    f"Any work in progress is still on disk — the branches and "
                    f"worktrees are untouched. Check `/status` and resume "
                    f"anything that matters.\n"
                    f"If this repeats, the machine is genuinely short on RAM: "
                    f"see the memory settings in `scripts/claude-bot.service` "
                    f"and `SESSION_MEM_KILL_MB`.",
                )
                claude_memory.clear_oom_marker(config.DATA_DIR)
            except Exception:
                log.exception("Could not post OOM notice to the Ark")
