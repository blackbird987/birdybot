"""The /ask_machine directive: an owner's chat session asks the other machine something.

A question costs the owner a tap on the other machine and shares repo content
through Discord, so it is refused at once, with no record, whenever it could not go
anywhere useful: the other side has no outbox yet, or its directory lacks the repo.
Only a chat session the owner is in may ask at all. See docs/design/peer-link.md.
"""

from __future__ import annotations

import logging

from bot import config, peer_record
from bot.claude.sandbox import origin_is_sandboxed
from bot.claude.types import Instance, InstanceOrigin
from bot.engine import peer_questions
from bot.engine.directives.common import pair_directives, parse_kv
from bot.platform.base import RequestContext
from bot.textutil import directive_re

log = logging.getLogger(__name__)

_ASK_RE = directive_re("ask_machine")
_MAX_PER_RESPONSE = 3


def _refusal(ctx: RequestContext, inst: Instance | None) -> str | None:
    """Why this session may not ask, or None."""
    if not config.PEER_ENABLED:
        return "The machine link is not set up on this machine."
    if inst is None or inst.origin is not InstanceOrigin.DIRECT:
        return "Only a chat session can ask the other machine something."
    if (not inst.is_owner_session or inst.guest_confined or ctx.thread_has_guest
            or origin_is_sandboxed(ctx.store, inst, ctx)):
        return "A session with a guest or in a sandboxed repo cannot ask the other machine."
    return None


def _age_words(view: peer_questions.PeerView) -> str:
    if view.age_secs is None:
        return "its directory is of unknown age"
    minutes = max(view.age_secs, 0) // 60
    age = f"{minutes}m" if minutes < 90 else f"{minutes // 60}h"
    state = "looks offline" if view.looks_offline else "looks online"
    return f"it {state}, with a directory {age} old"


async def _ask_one(ctx: RequestContext, repo: str, body: str, view: peer_questions.PeerView,
                   transport: peer_questions.Transport) -> str:
    """Post one question, or say why not. Returns the line for the thread."""
    now = peer_questions.now_secs()
    today = peer_questions.today(now)
    channel = str(ctx.channel_id)
    if peer_questions.open_on_thread(ctx.store, channel) >= peer_questions.MAX_OPEN_PER_THREAD:
        return (f"This thread already has {peer_questions.MAX_OPEN_PER_THREAD} questions "
                "waiting on the other machine, so this one was not sent.")
    if ctx.store.peer_count_today("asked", today) >= peer_questions.MAX_ASKS_PER_DAY:
        return f"The limit of {peer_questions.MAX_ASKS_PER_DAY} questions a day is used up, so this one was not sent."
    machine = view.record["machine"]
    if view.record.get("omitted", 0) == 0 and repo not in {r[0] for r in view.record["repos"]}:
        return f"{machine} has no repo called `{repo}` in its directory, so nothing was sent."
    qid = peer_questions.new_id()
    try:
        message = peer_record.encode_question(
            qid, config.MACHINE_NAME, machine, repo, peer_questions.clean_outgoing(body),
        )
    except ValueError as exc:
        return f"That question was not sent: it is {exc}."
    peer_questions.record_asked(ctx.store, qid, channel, repo, machine, now)
    try:
        await transport.post(message)
    except Exception:
        log.warning("Peer link: could not post a question", exc_info=True)
        ctx.store.peer_drop_asked(qid)
        return "The question could not be posted to the link channel, so nothing was sent."
    return (f"Asked {machine} about `{repo}`: {_age_words(view)}. The owner has to tap Run once "
            "there, and the answer lands in this thread by itself when it comes.")


async def _handle_ask_directives(
    ctx: RequestContext, result_text: str, source_inst: Instance | None,
) -> None:
    if not result_text or "/ask_machine" not in result_text:
        return
    pairs, no_body, over_cap = pair_directives(result_text, _ASK_RE, "ask", _MAX_PER_RESPONSE)
    if not (pairs or no_body or over_cap):
        return

    async def _notice(text: str) -> None:
        try:
            await ctx.messenger.send_text(ctx.channel_id, text, silent=True)
        except Exception:
            log.warning("Peer link: could not post a notice", exc_info=True)

    refusal = _refusal(ctx, source_inst)
    if refusal:
        log.info("BOT_CMD /ask_machine refused: %s", refusal)
        await _notice(refusal)
        return
    if no_body:
        await _notice(f"{no_body} /ask_machine ignored: each needs its own `~~~ask ... ~~~` block right after it.")
    if over_cap:
        await _notice(f"Only {_MAX_PER_RESPONSE} questions per response, {over_cap} ignored.")
    if not pairs:
        return
    transport = peer_questions.get_transport()
    if transport is None:
        await _notice("The machine link has not started yet. Try again in a minute.")
        return
    try:
        view = await transport.peer_view()
    except Exception:
        log.warning("Peer link: could not read the other machine", exc_info=True)
        await _notice("Could not read the link channel, so nothing was sent.")
        return
    if view.error or view.record is None:
        await _notice(f"Nothing was sent: {view.error or 'the other machine has not published yet'}.")
        return
    if not view.has_outbox:
        await _notice(
            f"{view.record['machine']} runs v{view.record['version']}, which cannot answer "
            "questions yet, so nothing was sent. It needs this update: tap Apply on its "
            "update card, or pull there, then reboot it.")
        return
    for args, body in pairs:
        repo = (parse_kv(args) or {}).get("repo", "")
        if not peer_record.NAME_RE.fullmatch(repo):
            await _notice("An /ask_machine named no usable repo, so it was not sent.")
            continue
        await _notice(await _ask_one(ctx, repo, body, view, transport))
