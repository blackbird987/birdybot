"""Questions between machines: the states a question goes through, on both sides.

The asker's side: asked, answered_received, delivered; or undeliverable, or expired.
The answerer's side: seen, carded, running, posting, done; or declined, or expired.

Everything arrives from a thread the reader polls, possibly twice and possibly days
late, so every step goes by the question's id and is safe to repeat. The first text
seen for an id is the one that runs, whatever a later edit or duplicate says.

What the other machine writes is data. It never reaches ``Instance.prompt``, and the
only sessions it reaches are the two kinds ``bot/peer_confine.py`` confines. See
docs/design/peer-link.md.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from bot import config, peer_confine, peer_export, peer_record, vault
from bot.claude.parser import parse_usage_limit
from bot.claude.types import PEER_ORIGINS, InstanceOrigin, InstanceStatus
from bot.fsutil import atomic_write_text
from bot.platform.redact import redact_secrets, scrub_known

log = logging.getLogger(__name__)

# Asker
ASKED, ANSWER_RECEIVED, DELIVERED = "asked", "answered_received", "delivered"
UNDELIVERABLE, EXPIRED = "undeliverable", "expired"
# Answerer
SEEN, CARDED, RUNNING, POSTING, DONE, DECLINED = (
    "seen", "carded", "running", "posting", "done", "declined",
)

MAX_AGE_SECS = 7 * 86400
_KEEP_SECS = 30 * 86400
MAX_OPEN_PER_THREAD = 3
MAX_ASKS_PER_DAY = 20
MAX_CARDS_PER_DAY = 20
MAX_DELIVERY_ATTEMPTS = 3
# Long enough that a failure with a passing cause does not spend all three at once.
DELIVERY_RETRY_SECS = 600
USAGE_LIMIT_RETRY_SECS = 1800
ANSWER_TIMEOUT_SECS = 15 * 60
DELIVERY_TIMEOUT_SECS = 10 * 60

RUN_ACTION, DECLINE_ACTION = "peer_run", "peer_decline"

_DISCORD_EPOCH_MS = 1420070400000


def snowflake_time(message_id: str | int) -> float:
    """When Discord made this id, in epoch seconds. The sender does not choose it."""
    return ((int(message_id) >> 22) + _DISCORD_EPOCH_MS) / 1000


def today(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")


def clean(text: str) -> str:
    """Secrets out of anything that crosses the link or stays on disk."""
    return redact_secrets(scrub_known(text))


# --- The seam to the other machine -------------------------------------------


@dataclass
class PeerView:
    """What the other machine's directory message says right now."""
    record: dict | None = None
    age_secs: int | None = None
    looks_offline: bool = True
    has_outbox: bool = False
    error: str = ""


class Transport(Protocol):
    """Discord in production (``bot/discord/peer_link.py``), a fake in the harness."""

    async def peer_view(self) -> PeerView: ...

    async def post(self, text: str) -> None: ...

    async def post_card(self, record: dict) -> str: ...

    async def edit_card(self, message_id: str, text: str) -> None: ...


_transport: Transport | None = None


def set_transport(transport: Transport | None) -> None:
    global _transport
    _transport = transport


def get_transport() -> Transport | None:
    return _transport


# --- Asker -------------------------------------------------------------------


def new_id() -> str:
    return secrets.token_hex(8)


def open_on_thread(store, thread_id: str) -> int:
    return sum(1 for r in store.peer_asked_all()
               if r.get("thread_id") == thread_id and r.get("status") in (ASKED, ANSWER_RECEIVED))


def clean_outgoing(text: str) -> str:
    """A question leaves this machine with no secret in it, literal or known."""
    return clean(vault.strip_literals(text)).strip()


def record_asked(store, qid: str, thread_id: str, repo: str, to: str, now: float) -> dict:
    record = {"id": qid, "thread_id": thread_id, "repo": repo, "to": to, "status": ASKED,
              "created_at": now, "attempts": 0, "next_attempt_at": 0}
    store.peer_put_asked(record, critical=True)
    store.peer_bump_today("asked", today(now))
    return record


def _save_answer(record: dict, status: str, text: str) -> Path:
    path = peer_confine.answer_file(record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    body = peer_confine.neutralise(clean(text))
    atomic_write_text(path, f"Outcome on the other machine: {status}\n\n{body}\n")
    return path


def on_answer_part(store, part: dict, now: float) -> bool:
    """One part of an answer. True once the answer is whole and saved.

    Only for ids this machine asked. The first copy of each part wins, and the first
    part fixes how many there are and what the outcome was.
    """
    record = store.peer_asked(part["id"])
    if record is None:
        log.info("Peer link: an answer for a question this machine did not ask, ignored")
        return False
    if record.get("status") != ASKED:
        return False
    if record.get("m") is None:
        record["m"] = part["m"]
        record["answer_status"] = part["status"]
        record["parts"] = {}
    if part["m"] != record["m"]:
        log.warning("Peer link: answer %s changed its part count, part ignored", part["id"])
        return False
    record["parts"].setdefault(str(part["n"]), part["text"])
    if len(record["parts"]) < record["m"]:
        store.peer_put_asked(record)
        return False
    text = "".join(record["parts"][str(i)] for i in range(1, record["m"] + 1))
    record["answer_path"] = str(_save_answer(record, record["answer_status"], text))
    record.pop("parts")
    record["status"] = ANSWER_RECEIVED
    record["answered_at"] = now
    store.peer_put_asked(record, critical=True)
    return True


def due_deliveries(store, now: float) -> list[dict]:
    """The oldest waiting answer per thread, and only if its next try has come round."""
    picked: dict[str, dict] = {}
    for record in store.peer_asked_all():
        if record.get("status") == ANSWER_RECEIVED:
            picked.setdefault(record["thread_id"], record)
    return [r for r in picked.values() if r.get("next_attempt_at", 0) <= now]


def close_delivery(store, record: dict, status: str, reason: str) -> None:
    record["status"] = status
    record["closed_reason"] = reason
    store.peer_put_asked(record, critical=True)


def delivery_failed(store, record: dict, error: str | None, now: float) -> bool:
    """Note a turn that did not land. True when that was the last try.

    A usage limit says nothing about the delivery, so it waits without using one up.
    """
    if error and parse_usage_limit(error):
        record["next_attempt_at"] = now + USAGE_LIMIT_RETRY_SECS
        store.peer_put_asked(record)
        return False
    record["attempts"] = record.get("attempts", 0) + 1
    record["next_attempt_at"] = now + DELIVERY_RETRY_SECS
    store.peer_put_asked(record)
    return record["attempts"] >= MAX_DELIVERY_ATTEMPTS


def expire_asked(store, now: float) -> list[tuple[dict, str]]:
    """Questions that have waited too long, with the one line each thread is owed."""
    notes = []
    for record in store.peer_asked_all():
        if record.get("status") == ASKED and now - record["created_at"] > MAX_AGE_SECS:
            record["status"] = EXPIRED
            record.pop("parts", None)
            store.peer_put_asked(record)
            notes.append((record, f"No answer from {record.get('to', 'the other machine')} about "
                                  f"`{record['repo']}` after 7 days, so that question is closed."))
        elif (record.get("status") == ANSWER_RECEIVED
              and now - record.get("answered_at", now) > MAX_AGE_SECS):
            close_delivery(store, record, UNDELIVERABLE, "never found a free moment")
            notes.append((record, f"An answer about `{record['repo']}` could not be brought into "
                                  f"this thread for a week. It is saved at `{record['answer_path']}`."))
    return notes


# --- Answerer ----------------------------------------------------------------


def on_question(store, question: dict, message_id: str, now: float) -> bool:
    """A question from the other machine. True if it is new."""
    if question["to"] != config.MACHINE_NAME:
        log.info("Peer link: a question addressed to %s, not this machine", question["to"])
        return False
    if store.peer_received(question["id"]) is not None:
        return False
    created = snowflake_time(message_id)
    status = EXPIRED if now - created > MAX_AGE_SECS else SEEN
    store.peer_put_received({
        "id": question["id"], "from": question["from"], "repo": question["repo"],
        "text": clean(question["text"]), "status": status, "created_at": created,
    }, critical=True)
    return status == SEEN


def queue_answer(store, record: dict, status: str, text: str, final: str, card_note: str) -> None:
    """Hand an outcome to the posting step. It survives a restart from here on."""
    record["parts"] = peer_record.encode_answer(record["id"], status, text)
    record["parts_done"] = 0
    record["answer_status"] = status
    record["final"] = final
    record["card_note"] = card_note
    record["status"] = POSTING
    store.peer_put_received(record, critical=True)


async def card_new_questions(store, transport: Transport, now: float) -> None:
    """Every question not yet shown to the owner: card it, or answer it automatically."""
    for record in store.peer_received_all():
        if record.get("status") != SEEN:
            continue
        if now - record["created_at"] > MAX_AGE_SECS:
            record["status"] = EXPIRED
            store.peer_put_received(record)
            continue
        if record["repo"] not in store.list_repos():
            queue_answer(store, record, peer_record.UNKNOWN_REPO,
                         f"This machine has no repo called {record['repo']}.", DONE, "")
            continue
        if store.peer_count_today("carded", today(now)) >= MAX_CARDS_PER_DAY:
            queue_answer(store, record, peer_record.FAILED,
                         "This machine has already shown its owner the day's limit of "
                         "questions. Ask again tomorrow.", DONE, "")
            continue
        # A failed send stays `seen` and comes round again. A crash between the send
        # and the save can show a second card, and the tap is single use by status.
        record["card_message_id"] = await transport.post_card(record)
        record["status"] = CARDED
        store.peer_put_received(record, critical=True)
        store.peer_bump_today("carded", today(now))


async def expire_cards(store, transport: Transport, now: float) -> None:
    """A card nobody tapped for a week. The tap refuses by now anyway: this is for the eye."""
    for record in store.peer_received_all():
        if record.get("status") == CARDED and now - record["created_at"] > MAX_AGE_SECS:
            record["status"] = EXPIRED
            store.peer_put_received(record)
            try:
                await transport.edit_card(record["card_message_id"], "Expired: nobody tapped this for 7 days.")
            except Exception:
                log.warning("Peer link: could not update an expired card", exc_info=True)


async def pump_posting(store, transport: Transport) -> None:
    """Post what is waiting to go out, from the next unposted part. Raises to retry."""
    for record in store.peer_received_all():
        if record.get("status") != POSTING:
            continue
        parts = record.get("parts") or []
        while record.get("parts_done", 0) < len(parts):
            await transport.post(parts[record.get("parts_done", 0)])
            record["parts_done"] = record.get("parts_done", 0) + 1
            store.peer_put_received(record, critical=True)
        record["status"] = record.get("final", DONE)
        record.pop("parts", None)
        store.peer_put_received(record, critical=True)
        if record.get("card_message_id") and record.get("card_note"):
            try:
                await transport.edit_card(record["card_message_id"], record["card_note"])
            except Exception:
                log.warning("Peer link: could not update the card", exc_info=True)


def tap_refusal(store, qid: str, now: float) -> str | None:
    """Why a tap on this card does nothing, or None if it is good."""
    record = store.peer_received(qid)
    if record is None:
        return "This question is no longer on record."
    if record.get("status") != CARDED:
        return "This question has already been handled."
    if now - record["created_at"] > MAX_AGE_SECS:
        record["status"] = EXPIRED
        store.peer_put_received(record)
        return "This question is more than 7 days old, so it can no longer be run."
    return None


def tap_decline(store, qid: str) -> None:
    queue_answer(store, store.peer_received(qid), peer_record.DECLINED,
                 "The owner declined to run this.", DECLINED, "Declined. Nothing was read.")


def mark_running(store, qid: str, now: float) -> dict:
    """The tap is spent from here on: a restart answers `failed`, it does not run twice."""
    record = store.peer_received(qid)
    record["status"] = RUNNING
    record["started_at"] = now
    store.peer_put_received(record, critical=True)
    return record


def recover_after_restart(store) -> int:
    """A process died mid-answer. Its tap is spent, so the other side hears `failed`.

    Questions already posting just carry on at the next part when the reader starts.
    """
    recovered = 0
    for record in store.peer_received_all():
        if record.get("status") == RUNNING:
            queue_answer(store, record, peer_record.FAILED,
                         "This machine restarted while answering. Ask again if it still matters.",
                         DONE, "Failed: this machine restarted while answering.")
            recovered += 1
    return recovered


def prune(store, now: float) -> None:
    """Forget closed questions after a while, and the answers saved for them."""
    for record in store.peer_asked_all():
        if (record.get("status") in (DELIVERED, UNDELIVERABLE, EXPIRED)
                and now - record["created_at"] > _KEEP_SECS):
            if record.get("answer_path"):
                Path(record["answer_path"]).unlink(missing_ok=True)
            store.peer_drop_asked(record["id"])
    for record in store.peer_received_all():
        if (record.get("status") in (DONE, DECLINED, EXPIRED)
                and now - record["created_at"] > _KEEP_SECS):
            store.peer_drop_received(record["id"])


# --- The answering run -------------------------------------------------------


def _reports_for(store, repo: str) -> list[tuple[str, str]]:
    """That repo's newest session reports, never a guest's and never an earlier answer."""
    runs = [
        inst for inst in store.list_instances(all_=True)
        if inst.repo_name == repo and inst.result_file and inst.origin not in PEER_ORIGINS
        and inst.is_owner_session and not inst.guest_confined
    ]
    runs.sort(key=lambda inst: inst.created_at or "", reverse=True)
    reports = []
    for inst in runs:
        text = inst.read_result_text()
        if text:
            reports.append((inst.id, text))
        if len(reports) >= peer_export.MAX_REPORTS:
            break
    return reports


def _plain_failure(inst) -> str:
    """Why a run failed, in words safe to send to the other machine."""
    error = inst.error or ""
    if parse_usage_limit(error):
        return "The answering session hit this machine's usage limit. Ask again once it resets."
    if "max turns" in error.lower() or "max_turns" in error.lower():
        return "The answering session used all its steps before it had an answer."
    return "The answering session failed before it had an answer."


def _make_answer_instance(ctx, export: peer_export.Export):
    # Local import: judge reaches the engine's commands, which import the directives.
    from bot.engine.judge import readonly_instance

    inst = readonly_instance(ctx, InstanceOrigin.PEER_ANSWER, peer_confine.ANSWER_PROMPT,
                             "", str(export.repo_dir))
    inst.vault_eligible = False
    ctx.store.update_instance(inst)
    return inst


async def run_answer(ctx, record: dict) -> tuple[str, str, str]:
    """Run one tapped question: (status, text for the asker, line for the card).

    Refuses without a measured check. There is no path from here to an unchecked run.
    Never raises, because a tap that is spent has to answer something.
    """
    from bot.engine import lifecycle

    export = None
    try:
        check = await asyncio.to_thread(peer_confine.self_check, config.CLAUDE_BINARY)
        if not check.ok:
            reason = clean(check.reason)
            log.warning("Peer link: self-check refused a run: %s", reason)
            return (peer_record.FAILED,
                    "This machine would not run it: its check that the answering session "
                    "is confined did not pass.",
                    f"Refused, nothing was read. The confinement check said: {reason}")
        repo_path = ctx.store.list_repos().get(record["repo"])
        if not repo_path:
            return (peer_record.UNKNOWN_REPO, f"This machine has no repo called {record['repo']}.",
                    "That repo is no longer registered here.")
        reports = await asyncio.to_thread(_reports_for, ctx.store, record["repo"])
        export = await asyncio.to_thread(
            peer_export.build_export, repo_path,
            peer_confine.question_block(record["from"], record["text"]), reports,
        )
        peer_confine.write_answer_settings(export.root, export.repo_dir)
        inst = _make_answer_instance(ctx, export)
        task = asyncio.create_task(lifecycle.run_instance(ctx, inst, silent=True))
        done, _ = await asyncio.wait({task}, timeout=ANSWER_TIMEOUT_SECS)
        if not done:
            await ctx.runner.kill_and_wait(inst.id)
            await asyncio.gather(task, return_exceptions=True)
            return (peer_record.FAILED, "The answering session took too long and was stopped.",
                    "Failed: it took too long.")
        await task
        answer = inst.read_result_text()
        if inst.error:
            inst.error = clean(inst.error)
            ctx.store.update_instance(inst)
        if inst.status is not InstanceStatus.COMPLETED or not answer.strip():
            text = _plain_failure(inst)
            return peer_record.FAILED, text, f"Failed: {text}"
        return (peer_record.ANSWERED, peer_confine.neutralise(clean(answer)),
                "Answered, and the answer has gone to the other machine.")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Peer link: the answering run failed")
        return (peer_record.FAILED, "This machine could not run the answering session.",
                "Failed: this machine could not run it. The bot log has why.")
    finally:
        if export is not None:
            await asyncio.to_thread(peer_export.remove_export, export.root)


def delivery_instance(ctx, record: dict, session_id: str, repo_name: str, repo_path: str):
    """A no-tool turn on the thread's own session. Its prompt only names the answer."""
    from bot.engine.judge import readonly_instance

    inst = readonly_instance(ctx, InstanceOrigin.PEER_DELIVERY,
                             f"{peer_confine.DELIVERY_PREFIX}{record['id']}", repo_name, repo_path)
    inst.session_id = session_id
    inst.vault_eligible = False
    ctx.store.update_instance(inst)
    return inst


def now_secs() -> float:
    return time.time()
