"""Orchestrator loop: child→parent callback + Resume button payload store.

When a spawned child thread finalizes (COMPLETED/FAILED), the engine
invokes ctx.notify_parent_on_finalize. The closure built here:

1. Looks up the child's ThreadInfo to find parent_thread_id.
2. Guards against pinging a closed/archived/discarded parent.
3. Stores the synthesized resume prompt under a short token in
   platform_state["discord"]["orch_resume_payloads"] so the button's
   custom_id (capped at 100 chars by Discord) only needs to carry the
   token, not the full prompt.
4. Posts a callback message into the parent thread with a Resume button.

When the user taps Resume, bot/discord/interactions.py looks up the
payload by token and feeds it back into the parent thread via
_replay_to_thread(source="callback_resume") so the orchestrator wave
counter is NOT reset (only genuine user-typed messages reset).
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

import discord

from bot.platform.base import ButtonSpec

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

_PAYLOAD_KEY = "orch_resume_payloads"
_SUMMARY_TRUNC = 400


def _get_payloads(bot: ClaudeBot) -> dict:
    state = bot._store.get_platform_state("discord")
    payloads = state.get(_PAYLOAD_KEY)
    if payloads is None:
        payloads = {}
        state[_PAYLOAD_KEY] = payloads
        bot._store.set_platform_state("discord", state, persist=False)
    return payloads


def store_resume_payload(bot: ClaudeBot, prompt: str) -> str:
    """Persist a resume prompt and return a short opaque token."""
    token = secrets.token_urlsafe(8)
    payloads = _get_payloads(bot)
    payloads[token] = prompt
    bot._store.save()
    return token


def pop_resume_payload(bot: ClaudeBot, token: str) -> str | None:
    """Look up and consume a resume prompt by token."""
    payloads = _get_payloads(bot)
    prompt = payloads.pop(token, None)
    if prompt is not None:
        bot._store.save()
    return prompt


def _parent_is_alive(bot: ClaudeBot, parent_thread_id: str) -> bool:
    """True iff the parent thread is still present and usable.

    Treats archived/locked threads as not-alive — we never want to ping a
    thread the user has wrapped up.
    """
    lookup = bot._forums.thread_to_project(parent_thread_id)
    if lookup is None:
        return False
    try:
        ch = bot.get_channel(int(parent_thread_id))
    except Exception:
        return False
    if ch is None:
        return False
    if isinstance(ch, discord.Thread):
        if ch.archived or ch.locked:
            return False
    return True


async def post_parent_callback(
    bot: ClaudeBot,
    child_thread_id: str,
    status: str,
    summary: str,
) -> None:
    """Post a child→parent finalize callback into the parent thread.

    No-op if:
    - The child has no recorded parent_thread_id.
    - The parent thread is archived/locked/missing (dead-parent guard).
    Errors are logged but never raised — child finalize must complete.
    """
    child_lookup = bot._forums.thread_to_project(child_thread_id)
    if child_lookup is None:
        return
    _, child_info = child_lookup
    parent_id = child_info.parent_thread_id
    if not parent_id:
        return
    if not _parent_is_alive(bot, parent_id):
        log.info(
            "orchestrator callback skipped — parent thread %s archived/missing",
            parent_id,
        )
        return

    title = (child_info.topic or "").strip() or f"<#{child_thread_id}>"
    if len(title) > 60:
        title = title[:60].rstrip() + "…"
    summary_clean = (summary or "").strip()
    if len(summary_clean) > _SUMMARY_TRUNC:
        summary_clean = summary_clean[:_SUMMARY_TRUNC].rstrip() + "…"

    body_lines = [
        f"Child session \"{title}\" → **{status}**",
        f"Thread: <#{child_thread_id}>",
    ]
    if summary_clean:
        body_lines.append("")
        body_lines.append(summary_clean)
    body = "\n".join(body_lines)

    # Build the Resume payload. The Resume prompt fed back into the
    # parent is the same body the human sees — the parent LLM gets the
    # full context of what the child did and can decide what to spawn next.
    synth_prompt = (
        f"Spawned child session \"{title}\" finished with status: {status}.\n"
        f"Child thread: <#{child_thread_id}>\n\n"
        f"Summary:\n{summary_clean}"
    )
    try:
        token = store_resume_payload(bot, synth_prompt)
    except Exception:
        log.exception("failed to store orch resume payload")
        token = None

    buttons = None
    if token:
        buttons = [[ButtonSpec(label="Resume parent", callback_data=f"orch_resume:{token}")]]

    try:
        await bot.messenger.send_text(parent_id, body, buttons=buttons)
    except Exception:
        log.exception("failed to post orchestrator callback to parent %s", parent_id)
