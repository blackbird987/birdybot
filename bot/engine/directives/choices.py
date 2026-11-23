"""The /choices directive: one-tap answer buttons on a session's reply.

A reply that ends on a question with a few fixed answers can carry them as
buttons::

    [BOT_CMD: /choices]
    ~~~choices
    Keep the old name
    Rename it
    ~~~

A tap sends the label into the thread as the tapper's next message, through
the same path a typed message takes (``query.on_text``, so the usage gate,
the budget gate, the channel lock and the Queued card all apply). Typing still
works exactly as before. Whichever answer comes first, typed or tapped, closes
the offer, and any later turn in the thread closes it too.

Sandboxed and guest sessions may offer choices on purpose: a tap does nothing
but send the label as the person who tapped, with that person's own access,
which they could equally have typed. No key, path or command rides on it.

Design note: ``docs/design/choices.md``. Harness: ``scripts/test_choices.py``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bot import config
from bot.platform.redact import redact_secrets
from bot.textutil import find_tilde_block, is_quoted_example, mask_tilde_bodies

log = logging.getLogger(__name__)

CHOICE_PREFIX = "choice:"
MAX_LABELS = 5
MIN_LABELS = 2
MAX_LABEL_LEN = 40
# An offer nobody answered for a week is not going to be answered.
OFFER_TTL_SECONDS = 7 * 86400
STALE_TAP_TEXT = "That choice is out of date: the conversation moved on."

# Group 1 is always present (possibly empty) so stray arguments can be refused.
_CHOICES_RE = re.compile(r"\[BOT_CMD:\s*/choices\b([^\]]*)\]")
# A reply that hands off work leaves the next step to that work, not to a tap.
_HANDOFF_RE = re.compile(r"\[BOT_CMD:\s*/(?:chain|spawn)\b")


@dataclass
class ParsedChoices:
    labels: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    suppressed: str | None = None  # why a directive that was there offers nothing


def _log_path():
    return config.DATA_DIR / "choices.jsonl"


def record(event: str, **fields) -> None:
    """Append one line to data/choices.jsonl. Never raises."""
    row = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    try:
        with _log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        log.warning("Could not append to choices.jsonl", exc_info=True)


def _live_matches(scan: str, pattern: re.Pattern[str]) -> list[re.Match[str]]:
    return [m for m in pattern.finditer(scan) if not is_quoted_example(scan, m.start())]


def has_choices_directive(text: str) -> bool:
    """True when ``text`` carries a live /choices directive (masked, not quoted)."""
    if not text or "/choices" not in text:
        return False
    return bool(_live_matches(mask_tilde_bodies(text), _CHOICES_RE))


def _label_problem(label: str) -> str | None:
    if len(label) > MAX_LABEL_LEN:
        return f"longer than {MAX_LABEL_LEN} characters"
    if label[0] in "/!":
        return "starts like a command"
    if "[BOT_CMD" in label or "~~~" in label:
        return "contains directive syntax"
    return None


def parse_choices(result_text: str, *, chain_active: bool = False) -> ParsedChoices:
    """Read the reply's /choices directive, if any. Pure: no store, no I/O.

    Directives are found on the tilde-masked text, so one inside another
    session's ``~~~`` body never arms, and quoted examples are skipped.
    """
    out = ParsedChoices()
    if not result_text or "/choices" not in result_text:
        return out
    scan = mask_tilde_bodies(result_text)
    matches = _live_matches(scan, _CHOICES_RE)
    if not matches:
        return out
    if chain_active:
        out.suppressed = "chain running"
        return out
    if _live_matches(scan, _HANDOFF_RE):
        out.suppressed = "reply hands off to /chain or /spawn"
        return out
    if len(matches) > 1:
        out.notices.append(
            f"Only the first of {len(matches)} /choices directives was used.")
    m = matches[0]
    if m.group(1).strip():
        out.notices.append(
            "The /choices directive takes no arguments, so no buttons were added.")
        out.suppressed = "arguments"
        return out
    region_end = matches[1].start() if len(matches) > 1 else len(result_text)
    span = find_tilde_block(result_text, "choices", m.end(), region_end)
    if span is None:
        out.notices.append(
            "The /choices directive had no ~~~choices block after it, so no buttons were added.")
        out.suppressed = "no body"
        return out
    seen: set[str] = set()
    for raw in result_text[span[0]:span[1]].splitlines():
        label = redact_secrets(raw.strip())
        if not label:
            continue
        key = label.casefold()
        if key in seen:
            continue
        problem = _label_problem(label)
        if problem:
            out.refused.append(label)
            out.notices.append(f"Choice left out ({problem}): {label[:60]}")
            continue
        seen.add(key)
        out.labels.append(label)
    if len(out.labels) > MAX_LABELS:
        out.notices.append(
            f"Only the first {MAX_LABELS} of {len(out.labels)} choices became buttons.")
        out.labels = out.labels[:MAX_LABELS]
    if len(out.labels) < MIN_LABELS:
        if out.labels or out.refused:
            out.notices.append("Fewer than two usable choices, so no buttons were added.")
        out.labels = []
        out.suppressed = out.suppressed or "fewer than two labels"
    return out


def chain_active(store, session_id: str | None) -> bool:
    try:
        return bool(session_id and store.get_autopilot_chain(session_id))
    except Exception:
        return False


def open_offer(
    ctx, inst, labels: list[str], msg_id: str,
    messages: list[str] | None = None,
) -> None:
    """Record the offer the card ``msg_id`` now carries. Fails open.

    ``messages``, one per label, is what a tap sends instead of the label:
    the end card's Next buttons carry a short label and a full instruction
    (docs/design/session-recap.md). A list that does not line up with the
    labels is dropped, so a tap falls back to the label.
    """
    put = getattr(ctx.store, "put_choice_offer", None)
    if put is None:
        return
    offer = {
        "instance_id": inst.id,
        "msg_id": str(msg_id),
        "labels": list(labels),
        "offered_at": time.time(),
        "repo": ctx.repo_name or getattr(inst, "repo_name", None),
    }
    if messages and len(messages) == len(labels):
        offer["messages"] = [str(m) for m in messages]
    try:
        put(ctx.channel_id, offer)
    except Exception:
        log.warning("Could not store /choices offer for %s", ctx.channel_id, exc_info=True)
        return
    record("offered", channel_id=ctx.channel_id, instance_id=inst.id,
           repo=offer["repo"], n=len(labels))


def offer_labels_for(
    store, channel_id: str, instance_id: str, msg_id: str | None,
) -> list[str] | None:
    """The open offer's labels, only when it belongs to this instance's card.

    Redraws (Mode, Expand, Collapse, a merge or discard) use this so a card
    keeps its row while the offer is open, and no other card ever grows one.
    With ``msg_id`` the redrawn message must be the one carrying the offer,
    since closing it strips that message and no other. ``None`` (no message
    to redraw) never gets the row.
    """
    get = getattr(store, "get_choice_offer", None)
    if get is None:
        return None
    try:
        offer = get(channel_id)
    except Exception:
        return None
    if not offer or offer.get("instance_id") != instance_id:
        return None
    if not msg_id or str(msg_id) != str(offer.get("msg_id")):
        return None
    return list(offer.get("labels") or []) or None


async def _strip(messenger, channel_id: str, msg_id: str | None) -> None:
    strip = getattr(messenger, "strip_button_rows", None)
    if not msg_id or strip is None:
        return
    try:
        await strip(channel_id, msg_id, CHOICE_PREFIX)
    except Exception:
        log.debug("Could not strip choice row from %s", msg_id, exc_info=True)


async def close_offer(ctx, channel_id: str | None, reason: str) -> None:
    """Close the channel's open offer, if any: drop it, strip its row, log it.

    Called before every turn in the thread starts, so a typed answer (reason
    ``typed``) or any other turn (``superseded:<source>``) retires the
    buttons. Fails open: a store without offers is a no-op.
    """
    if not channel_id:
        return
    pop = getattr(ctx.store, "pop_choice_offer", None)
    if pop is None:
        return
    try:
        offer = pop(channel_id)
    except Exception:
        log.debug("Could not close /choices offer for %s", channel_id, exc_info=True)
        return
    if not offer:
        return
    record(reason.split(":", 1)[0], channel_id=channel_id,
           instance_id=offer.get("instance_id"), repo=offer.get("repo"),
           detail=reason.split(":", 1)[1] if ":" in reason else None,
           seconds=_age(offer))
    await _strip(ctx.messenger, channel_id, offer.get("msg_id"))


def close_reason(source: str) -> str:
    return "typed" if source == "user_message" else f"superseded:{source}"


def _age(offer: dict) -> float | None:
    try:
        return round(time.time() - float(offer.get("offered_at") or 0), 1)
    except (TypeError, ValueError):
        return None


def offer_live(offer: dict, now: float | None = None) -> bool:
    """Whether an offer is still inside its week. An undatable one is not.

    The one expiry rule: a tap, the sweep and the waiting list all use it.
    """
    now = time.time() if now is None else now
    try:
        age = now - float(offer.get("offered_at") or 0)
    except (TypeError, ValueError, AttributeError):
        return False
    return age <= OFFER_TTL_SECONDS


def open_offers(store, now: float | None = None) -> dict[str, dict]:
    """Every offer still open, by channel id: copies, never the stored dicts.

    How anything outside this module reads offers (the "Needs you" list).
    Fails open to an empty dict.
    """
    list_all = getattr(store, "list_choice_offers", None)
    if list_all is None:
        return {}
    try:
        offers = list_all()
    except Exception:
        return {}
    return {str(cid): dict(o) for cid, o in offers.items()
            if isinstance(o, dict) and offer_live(o, now)}


def tap_text(offer: dict, idx: int, label: str) -> str:
    """What a tap sends into the thread: the offer's message for that button
    when it carries one, otherwise the label as the tapper's pick."""
    msgs = offer.get("messages") if isinstance(offer, dict) else None
    if isinstance(msgs, list) and 0 <= idx < len(msgs):
        msg = msgs[idx]
        if isinstance(msg, str) and msg.strip():
            return msg
    return f"User picked: {label}"


def claim_for_tap(
    store, channel_id: str, instance_id: str, idx: int,
    msg_id: str | None = None,
) -> tuple[dict | None, str | None]:
    """Claim the offer for one tap. Returns ``(offer, label)`` or ``(None, None)``.

    No await between the check and the removal, so a second tap on the same
    button (or another button on the card) finds nothing and is a no-op.
    ``msg_id``, when given, must be the card the offer was recorded on.
    """
    get = getattr(store, "get_choice_offer", None)
    pop = getattr(store, "pop_choice_offer", None)
    if get is None or pop is None:
        return None, None
    offer = get(channel_id)
    if not offer or offer.get("instance_id") != instance_id:
        return None, None
    if msg_id is not None and str(msg_id) != str(offer.get("msg_id")):
        return None, None
    labels = offer.get("labels") or []
    if not 0 <= idx < len(labels):
        return None, None
    if not offer_live(offer):
        return None, None
    pop(channel_id)
    return offer, labels[idx]


async def expire_stale(store, messenger, *, now: float | None = None) -> int:
    """Retire offers older than a week. Returns how many were closed."""
    list_all = getattr(store, "list_choice_offers", None)
    pop = getattr(store, "pop_choice_offer", None)
    if list_all is None or pop is None:
        return 0
    now = time.time() if now is None else now
    n = 0
    for channel_id, offer in list_all().items():
        if offer_live(offer, now):
            continue
        if pop(channel_id) is None:
            continue
        n += 1
        record("expired", channel_id=channel_id,
               instance_id=offer.get("instance_id"), repo=offer.get("repo"))
        await _strip(messenger, channel_id, offer.get("msg_id"))
    return n


async def prepare(ctx, inst, raw_text: str | None) -> list[str] | None:
    """The labels to put on this reply's card, or None. Fails open to None.

    Logs refusals and suppressions, and posts any notice before the card is
    sent, so the card (and its buttons) stays the last thing in the thread.
    """
    try:
        parsed = parse_choices(
            raw_text or "",
            chain_active=chain_active(ctx.store, inst.session_id),
        )
    except Exception:
        log.exception("Could not parse /choices for %s", inst.id)
        return None
    repo = ctx.repo_name or getattr(inst, "repo_name", None)
    for label in parsed.refused:
        record("refused", channel_id=ctx.channel_id, instance_id=inst.id,
               repo=repo, label=label[:80])
    labels = parsed.labels or None
    if labels:
        shows = getattr(ctx.messenger, "shows_buttons", None)
        try:
            visible = bool(shows(ctx.channel_id)) if callable(shows) else False
        except Exception:
            visible = True  # same fail-open as the adapter's own button check
        if not visible:
            parsed.suppressed = "buttons hidden"
            labels = None
    if parsed.suppressed:
        record("suppressed", channel_id=ctx.channel_id, instance_id=inst.id,
               repo=repo, reason=parsed.suppressed)
    for note in parsed.notices:
        try:
            await ctx.messenger.send_text(ctx.channel_id, note, silent=True)
        except Exception:
            log.debug("Could not post /choices notice", exc_info=True)
    return labels
