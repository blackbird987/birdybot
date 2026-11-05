"""The two things the machine link does in a Discord thread or channel: card and delivery.

The owner taps Run once on a card here, a confined session answers, and the answer
goes back. On the asking side, an answer that came back is told to the thread's own
session in a turn with no tools. A laptop session could post an answer with nobody
having tapped anything, so an answer must never resume a turn with a shell.

Delivery has no schedule row and no wake: the state in ``bot/store/peer.py`` is the
queue, so nothing here can disturb a wake or watch the session set for itself. See
docs/design/peer-link.md.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from bot import config, peer_confine
from bot.claude.types import InstanceStatus
from bot.engine import lifecycle, peer_questions
from bot.engine.cmd_state import _get_channel_lock
from bot.platform.buttons import running_button_specs
from bot.platform.formatting import collapse_bot_directives
from bot.textutil import match_name

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

_CARD_QUESTION_MAX = 1500
_kept_tasks: set[asyncio.Task] = set()
_delivering: set[str] = set()
_answering = False


def _task_done(task: asyncio.Task) -> None:
    _kept_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.error("Peer link: a background step failed", exc_info=task.exception())


def _keep(task: asyncio.Task) -> asyncio.Task:
    _kept_tasks.add(task)
    task.add_done_callback(_task_done)
    return task


# --- The card ----------------------------------------------------------------


def card_text(record: dict, owner_id: int | str | None) -> str:
    """What the owner is asked. The question is the other machine's words, made inert."""
    shown = record["text"].replace("`", "'").replace("@", "@​")
    if len(shown) > _CARD_QUESTION_MAX:
        shown = shown[:_CARD_QUESTION_MAX] + "\n[shown cut short, the whole question runs]"
    mention = f"<@{owner_id}> " if owner_id else ""
    return (f"{mention}**{record['from']}** asks about **{record['repo']}**:\n"
            f"```\n{shown}\n```\n"
            "Run once lets a read-only session read this repo's committed files and its recent "
            "session reports, and what it writes goes through Discord to the other machine.")


def is_peer_tap(interaction: discord.Interaction) -> bool:
    """A Run once or Decline, on a card in this machine's link channel thread. Only that."""
    if not config.PEER_ENABLED:
        return False
    custom_id = (interaction.data or {}).get("custom_id", "") or ""
    action = custom_id.split(":", 1)[0]
    if action not in (peer_questions.RUN_ACTION, peer_questions.DECLINE_ACTION):
        return False
    return getattr(interaction.channel, "parent_id", None) == config.PEER_CHANNEL_ID


async def _say(interaction: discord.Interaction, text: str) -> None:
    await interaction.followup.send(text, ephemeral=True)


async def handle_tap(bot: ClaudeBot, interaction: discord.Interaction, action: str, qid: str) -> None:
    """A button on a card. The owner's real user id is the only thing that opens it."""
    global _answering
    if not is_peer_tap(interaction) or not bot._is_owner(interaction.user.id):
        await _say(interaction, "Only the owner can do that, and only on a card in the link channel.")
        return
    store = bot._store
    refusal = peer_questions.tap_refusal(store, qid, peer_questions.now_secs())
    if refusal:
        await interaction.edit_original_response(content=refusal, view=None)
        return
    transport = peer_questions.get_transport()
    if transport is None:
        await _say(interaction, "The machine link has not started yet. Tap again in a minute.")
        return
    if action == peer_questions.DECLINE_ACTION:
        peer_questions.tap_decline(store, qid)
        await interaction.edit_original_response(content="Declined. Nothing was read.", view=None)
        _keep(asyncio.create_task(_pump(store, transport)))
        return
    if _answering:
        await _say(interaction, "Another question is being answered. Tap again when it has finished.")
        return
    blocked = bot._runner.check_spawn_allowed()
    if blocked:
        await _say(interaction, "The bot is not starting sessions right now. Tap again in a few minutes.")
        return
    _answering = True  # No await between the check and this line.
    record = peer_questions.mark_running(store, qid, peer_questions.now_secs())
    _keep(asyncio.create_task(_answer(bot, record, transport, str(interaction.channel.id))))
    await interaction.edit_original_response(
        content=f"Running: checking the session is confined, then reading **{record['repo']}**.",
        view=None,
    )


async def _pump(store, transport) -> None:
    try:
        await peer_questions.pump_posting(store, transport)
    except Exception:
        log.warning("Peer link: posting failed, the reader will retry", exc_info=True)


async def _answer(bot: ClaudeBot, record: dict, transport, thread_id: str) -> None:
    """Run the question, then send what came of it. The tap is spent either way.

    The context is the card's thread, never the channel: a stray message in the channel
    itself would become this machine's newest, which is how its directory is found.
    """
    global _answering
    try:
        try:
            ctx = bot._ctx(thread_id, source="peer")
            ctx.maybe_prime_briefing = None
            status, text, note = await peer_questions.run_answer(ctx, record)
        except Exception:
            log.exception("Peer link: could not start the answering run")
            status, text, note = peer_questions.COULD_NOT_RUN
        peer_questions.queue_answer(bot._store, record, status, text, peer_questions.DONE, note)
    finally:
        _answering = False
    await _pump(bot._store, transport)


# --- Delivery ----------------------------------------------------------------


async def _note(bot: ClaudeBot, thread_id: str, text: str) -> None:
    try:
        await bot.messenger.send_text(thread_id, text, silent=True)
    except Exception:
        log.warning("Peer link: could not post a note to %s", thread_id, exc_info=True)


async def notify_closed(bot: ClaudeBot, notes: list[tuple[dict, str]]) -> None:
    for record, text in notes:
        await _note(bot, record["thread_id"], text)


def start_deliveries(bot: ClaudeBot) -> None:
    """Called from each reader tick. At most one delivery per thread, oldest first."""
    for record in peer_questions.due_deliveries(bot._store, peer_questions.now_secs()):
        thread_id = record["thread_id"]
        if thread_id in _delivering:
            continue
        _delivering.add(thread_id)
        task = _keep(asyncio.create_task(deliver_one(bot, record)))
        task.add_done_callback(lambda _t, t=thread_id: _delivering.discard(t))


async def deliver_one(bot: ClaudeBot, record: dict) -> None:
    """Tell the thread's session what came back, or close the question saying why not."""
    store = bot._store
    thread_id, saved = record["thread_id"], record.get("answer_path", "")
    lookup = bot._forums.thread_to_project(thread_id)
    if lookup is None:
        log.info("Peer link: thread %s is gone, answer %s stays at %s", thread_id, record["id"], saved)
        peer_questions.close_delivery(store, record, peer_questions.UNDELIVERABLE, "thread gone")
        return
    project, info = lookup
    if bot._family_has_guest(info):
        peer_questions.close_delivery(store, record, peer_questions.UNDELIVERABLE, "guest in thread")
        await _note(bot, thread_id, f"An answer about `{record['repo']}` came back. This thread has "
                                    f"a guest now, so it was not shown here. It is saved at `{saved}`.")
        return
    session_id = info.session_id
    if not session_id:
        peer_questions.close_delivery(store, record, peer_questions.UNDELIVERABLE, "no session")
        await _note(bot, thread_id, f"An answer about `{record['repo']}` came back, but this thread "
                                    f"has no session to tell. It is saved at `{saved}`.")
        return
    # Not now if anything else has this thread. The next tick looks again, and this
    # cannot race a wake or a typed message because they hold the same lock.
    lock = _get_channel_lock(thread_id)
    if (lock.locked() or bot._runner.active_instance_for_channel(thread_id)
            or bot._runner.check_spawn_allowed()):
        return
    # The same lookup a typed message makes, so the resume finds the same session.
    repos = store.list_repos()
    repo_name = match_name(project.repo_name, repos) or project.repo_name
    repo_path = repos.get(repo_name)
    if not repo_path:
        peer_questions.close_delivery(store, record, peer_questions.UNDELIVERABLE, "no repo")
        await _note(bot, thread_id, f"An answer about `{record['repo']}` came back, but this thread's "
                                    f"repo is no longer registered. It is saved at `{saved}`.")
        return
    async with lock:
        await _run_delivery(bot, record, thread_id, info, session_id, repo_name, repo_path)


async def _run_delivery(bot, record, thread_id, info, session_id, repo_name, repo_path) -> None:
    store = bot._store
    ctx = bot._ctx(thread_id, session_id=session_id, repo_name=repo_name, thread_info=info,
                   source="peer")
    ctx.maybe_prime_briefing = None
    ctx.offer_usage_limit_choice = None
    bot._cancel_sleep(thread_id)
    failure: str | None = "could not run"
    stopped = False
    try:
        try:
            await bot._forums.wake_repo_if_dormant(repo_name, notify_channel_id=thread_id)
        except Exception:
            log.debug("Wake-on-work check failed for %s", repo_name, exc_info=True)
        inst = peer_questions.delivery_instance(ctx, record, session_id, repo_name or "", repo_path)
        handle = await ctx.messenger.send_thinking(
            thread_id, f"⏳ An answer came back from the other machine about {record['repo']}",
            buttons=running_button_specs(inst.id),
        )
        task = asyncio.create_task(lifecycle.run_instance(ctx, inst, handle=handle))
        done, _ = await asyncio.wait({task}, timeout=peer_questions.DELIVERY_TIMEOUT_SECS)
        if not done:
            await ctx.runner.kill_and_wait(inst.id)
            await asyncio.gather(task, return_exceptions=True)
        else:
            await task
        # Finished by itself, as killed: the owner tapped Kill or Steer, which is a no.
        stopped = bool(done) and inst.status is InstanceStatus.KILLED
        text = inst.read_result_text()
        failure = inst.error or "no reply"
        if inst.status is InstanceStatus.COMPLETED and text.strip():
            for chunk in ctx.messenger.chunk_message(peer_confine.quiet_mentions(collapse_bot_directives(text))):
                await ctx.messenger.send_text(thread_id, chunk)
            failure = None
    except asyncio.CancelledError:
        raise
    except discord.NotFound:
        # The thread went away under us. An archived one would have come back.
        log.info("Peer link: thread %s is gone, answer %s stays saved", thread_id, record["id"])
        peer_questions.close_delivery(store, record, peer_questions.UNDELIVERABLE, "thread gone")
        return
    except Exception:
        log.exception("Peer link: delivery of %s failed", record["id"])
    finally:
        await bot._after_turn(ctx, thread_id, dashboard=False)
    if stopped:
        peer_questions.close_delivery(store, record, peer_questions.UNDELIVERABLE, "stopped by the owner")
        await _note(bot, thread_id, f"That answer about `{record['repo']}` was stopped before it came "
                                    f"through, and will not be tried again. It is saved at "
                                    f"`{record.get('answer_path', '')}`.")
        return
    if failure is None:
        record["status"] = peer_questions.DELIVERED
        record["delivered_at"] = peer_questions.now_secs()
        store.peer_put_asked(record, critical=True)
        return
    if peer_questions.delivery_failed(store, record, failure, peer_questions.now_secs()):
        peer_questions.close_delivery(store, record, peer_questions.UNDELIVERABLE, "three failed tries")
        await _note(bot, thread_id, f"An answer about `{record['repo']}` came back, but three tries to "
                                    f"bring it into this thread failed. It is saved at "
                                    f"`{record.get('answer_path', '')}`.")

