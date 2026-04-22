"""Pending-prompt registry for Steer vs Queue mid-run message handling.

When a user sends a message while an instance is running in the same channel,
the per-channel asyncio.Lock makes them wait in order.  Previously the "Queued"
notice was silent and deleted on acquire — no way to cancel or steer.

This module holds interactive pending entries: each has a message handle
pointing at a visible "Queued" embed with [Steer] [Cancel] buttons.

Entries are keyed by a short uuid and also indexed by channel_id.  They are
serialized to ``data/pending_prompts.json`` so they survive reboots (independent
of the drain queue, which uses replay semantics instead of interactive restore).
"""
from __future__ import annotations

import json as _json
import logging
import secrets
import time
from dataclasses import asdict, dataclass

from bot import config

log = logging.getLogger(__name__)

# Steering header: prepended to the user's new prompt when Steer fires.
# Claude sees this; --resume supplies prior context, this disambiguates intent.
STEER_HEADER = (
    "(User interrupted the previous request with a new instruction — "
    "prioritize this over any in-progress work.)"
)


@dataclass
class PendingPrompt:
    """A user message waiting behind the channel lock with a live UI.

    Two mutually-exclusive payload shapes:
      * *Text mode* (``callback_action is None``): ``prompt_text`` holds the
        user's raw message.  On Steer, ``STEER_HEADER`` is prepended and the
        text is re-run as a query.
      * *Callback mode* (``callback_action`` set): the queued work is a button
        callback, not a prompt.  ``callback_instance_id`` and
        ``callback_source_msg_id`` hold the dispatch args; ``prompt_text`` is
        unused (kept empty).
    """
    id: str
    channel_id: str
    session_id: str | None
    prompt_text: str
    message_id: str           # id of the "Queued" embed (platform message id)
    active_instance_id: str | None  # instance to kill on Steer
    created_at: float
    # Runtime-only flags (not persisted meaningfully — reset on restore)
    cancelled: bool = False
    handled_by_steer: bool = False
    # Platform hint (used on restore to route to the right adapter)
    platform: str = "discord"
    # Where to re-run after Steer (optional; repo_name carried through for ctx)
    repo_name: str | None = None
    user_id: str | None = None
    user_name: str | None = None
    is_owner: bool = True
    # Button-callback mode (None ⇒ this is a raw text prompt)
    callback_action: str | None = None
    callback_instance_id: str | None = None
    callback_source_msg_id: str | None = None

    def to_json(self) -> dict:
        d = asdict(self)
        # Drop transient flags from persistence — they're either false at
        # restore time or meaningless once the process that held them died.
        d.pop("cancelled", None)
        d.pop("handled_by_steer", None)
        return d

    @classmethod
    def from_json(cls, data: dict) -> "PendingPrompt":
        return cls(
            id=data["id"],
            channel_id=data["channel_id"],
            session_id=data.get("session_id"),
            prompt_text=data.get("prompt_text", ""),
            message_id=data["message_id"],
            active_instance_id=data.get("active_instance_id"),
            created_at=data.get("created_at", time.time()),
            platform=data.get("platform", "discord"),
            repo_name=data.get("repo_name"),
            user_id=data.get("user_id"),
            user_name=data.get("user_name"),
            is_owner=data.get("is_owner", True),
            callback_action=data.get("callback_action"),
            callback_instance_id=data.get("callback_instance_id"),
            callback_source_msg_id=data.get("callback_source_msg_id"),
        )


# --- In-memory registry ---

_by_id: dict[str, PendingPrompt] = {}
_by_channel: dict[str, list[str]] = {}  # channel_id -> [pending_id, ...]


def _gen_id() -> str:
    return secrets.token_hex(4)


def register(
    channel_id: str,
    session_id: str | None,
    prompt_text: str,
    message_id: str,
    active_instance_id: str | None,
    *,
    pending_id: str | None = None,
    platform: str = "discord",
    repo_name: str | None = None,
    user_id: str | None = None,
    user_name: str | None = None,
    is_owner: bool = True,
    callback_action: str | None = None,
    callback_instance_id: str | None = None,
    callback_source_msg_id: str | None = None,
) -> PendingPrompt:
    """Register a new pending prompt and persist.

    If ``pending_id`` is supplied the registry entry uses it verbatim —
    needed when the id was minted ahead of time to stamp it into button
    custom_ids before the embed was sent.

    Callback mode: when ``callback_action`` is set, this pending represents
    a queued button-callback rather than a raw user prompt.  ``prompt_text``
    should be empty; the callback fields drive dispatch on Steer.
    """
    pending = PendingPrompt(
        id=pending_id or _gen_id(),
        channel_id=channel_id,
        session_id=session_id,
        prompt_text=prompt_text,
        message_id=message_id,
        active_instance_id=active_instance_id,
        created_at=time.time(),
        platform=platform,
        repo_name=repo_name,
        user_id=user_id,
        user_name=user_name,
        is_owner=is_owner,
        callback_action=callback_action,
        callback_instance_id=callback_instance_id,
        callback_source_msg_id=callback_source_msg_id,
    )
    _by_id[pending.id] = pending
    _by_channel.setdefault(channel_id, []).append(pending.id)
    _persist()
    return pending


def get(pending_id: str) -> PendingPrompt | None:
    return _by_id.get(pending_id)


def clear(pending_id: str) -> None:
    p = _by_id.pop(pending_id, None)
    if not p:
        return
    ids = _by_channel.get(p.channel_id)
    if ids:
        try:
            ids.remove(pending_id)
        except ValueError:
            pass
        if not ids:
            _by_channel.pop(p.channel_id, None)
    _persist()


def channel_has_pending(channel_id: str) -> bool:
    """True if any pending prompt exists for the channel (used to suppress idle sleep)."""
    return bool(_by_channel.get(channel_id))


def all_pending() -> list[PendingPrompt]:
    return list(_by_id.values())


# --- Persistence ---

def _persist() -> None:
    try:
        data = [p.to_json() for p in _by_id.values()]
        config.PENDING_PROMPTS_FILE.write_text(
            _json.dumps(data, indent=2), encoding="utf-8",
        )
    except Exception:
        log.exception("Failed to persist pending prompts")


def load_from_disk() -> list[PendingPrompt]:
    """Read persisted entries.  Does NOT populate the registry — caller
    decides per-entry whether to re-register (live instance) or drop (dead).
    """
    try:
        raw = config.PENDING_PROMPTS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except Exception:
        log.exception("Failed to read pending prompts file")
        return []
    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        log.warning("pending_prompts.json is not valid JSON — ignoring")
        return []
    if not isinstance(data, list):
        return []
    out: list[PendingPrompt] = []
    for item in data:
        try:
            out.append(PendingPrompt.from_json(item))
        except Exception:
            log.warning("Skipping malformed pending entry: %r", item)
    return out


def clear_persisted_file() -> None:
    """Delete the on-disk file (e.g. when no entries survived restore)."""
    try:
        config.PENDING_PROMPTS_FILE.unlink(missing_ok=True)
    except Exception:
        pass
