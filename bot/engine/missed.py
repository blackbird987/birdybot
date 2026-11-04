"""Things that got past everything and were caught by the owner afterwards.

One tap, or `/missed [note]`, adds a line to `data/missed.jsonl`. The count is
the measure the rest of the judge work answers to (docs/design/judge.md): how
often does "done" turn out not to be. Nothing here runs a model or changes a
run, so a broken file costs a count and never a turn.

The file is append-only. Choosing a kind after the tap adds a second line that
amends the first, so nothing already written is ever rewritten and two bot
processes cannot lose each other's lines. `load_events` folds them back into one
event per tap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import config
from bot.platform.base import ButtonSpec, RequestContext
from bot.platform.redact import redact_secrets
from bot.procutil import run_capture
from bot.textutil import parse_iso_utc

log = logging.getLogger(__name__)

KIND_NOT_WHAT_I_MEANT = "not_what_i_meant"
KIND_TOO_NARROW = "too_narrow"
KIND_BUG = "bug"
KIND_UNSPECIFIED = "unspecified"
KINDS = (KIND_NOT_WHAT_I_MEANT, KIND_TOO_NARROW, KIND_BUG, KIND_UNSPECIFIED)

_KIND_LABELS = {
    KIND_NOT_WHAT_I_MEANT: "Not what I meant",
    KIND_TOO_NARROW: "Too narrow",
    KIND_BUG: "Bug",
}
_MAX_NOTE_CHARS = 1000
_GIT_TIMEOUT_SEC = 5


@dataclass
class MissedEvent:
    id: str
    ts: str
    repo: str
    thread: str
    instance_id: str
    head_sha: str
    kind: str = KIND_UNSPECIFIED
    note: str = ""


def _path() -> Path:
    return config.DATA_DIR / "missed.jsonl"


def _append(row: dict) -> None:
    """One write per line, so concurrent appends do not interleave."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def record(
    repo: str, thread: str, instance_id: str, head_sha: str,
    kind: str = KIND_UNSPECIFIED, note: str = "",
) -> MissedEvent:
    """Write one event now. An unknown kind is kept as unspecified."""
    event = MissedEvent(
        id=uuid.uuid4().hex[:12],
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        repo=repo, thread=thread, instance_id=instance_id, head_sha=head_sha,
        kind=kind if kind in KINDS else KIND_UNSPECIFIED,
        note=redact_secrets(note or "")[:_MAX_NOTE_CHARS],
    )
    _append(asdict(event))
    return event


def amend_kind(event_id: str, kind: str) -> bool:
    """Add the kind the owner picked. False if there is no such kind or event."""
    if kind not in KINDS or not any(e.id == event_id for e in load_events()):
        return False
    _append({
        "amends": event_id, "kind": kind,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return True


def load_events(repo: str | None = None, days: int | None = None) -> list[MissedEvent]:
    """Events oldest first, with amendments applied. A bad line is skipped."""
    try:
        text = _path().read_text(encoding="utf-8")
    except OSError:
        return []
    fields = set(MissedEvent.__dataclass_fields__)
    events: dict[str, MissedEvent] = {}
    for line in text.splitlines():
        try:
            row = json.loads(line)
            if "amends" in row:
                target = events.get(row["amends"])
                if target is not None and row.get("kind") in KINDS:
                    target.kind = row["kind"]
            else:
                events[row["id"]] = MissedEvent(**{k: v for k, v in row.items() if k in fields})
        except (ValueError, TypeError, KeyError, AttributeError):
            log.debug("Skipping a bad missed line", exc_info=True)
    result = list(events.values())
    if repo is not None:
        result = [e for e in result if e.repo == repo]
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = [e for e in result if (parse_iso_utc(e.ts) or cutoff) >= cutoff]
    return result


def head_sha(repo_path: str | None, branch: str | None = None) -> str:
    """The commit this work ended on: the build's branch, or the checkout's HEAD."""
    if not repo_path:
        return ""
    for ref in ([branch] if branch else []) + ["HEAD"]:
        try:
            done = run_capture(
                ["git", "-C", repo_path, "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
                timeout=_GIT_TIMEOUT_SEC,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            log.debug("Missed: rev-parse failed", exc_info=True)
            continue
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    return ""


def kind_buttons(event_id: str) -> list[list[ButtonSpec]]:
    return [[
        ButtonSpec(label, f"missed_kind:{event_id}|{kind}")
        for kind, label in _KIND_LABELS.items()
    ]]


def kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, "unspecified")


async def on_missed_tap(ctx: RequestContext, inst, source_msg_id: str | None = None) -> None:
    """The Missed button: record first, then offer the kind.

    The line is written before anything else can fail, because the tap is the
    thing being counted and the kind is only a refinement. A second tap on the
    same card reoffers the kind and does not count the same work twice.
    """
    if not ctx.is_owner:
        await ctx.messenger.send_text(ctx.channel_id, "Only the owner can mark work as missed.", silent=True)
        return
    existing = next((e for e in await asyncio.to_thread(load_events) if e.instance_id == inst.id), None)
    if existing is None:
        sha = await asyncio.to_thread(head_sha, inst.repo_path, inst.branch)
        existing = await asyncio.to_thread(
            record, inst.repo_name or ctx.repo_name or "", ctx.channel_id, inst.id, sha,
        )
        text = "Recorded as missed. What kind? (Optional)"
    else:
        text = "Already recorded as missed. What kind? (Optional)"
    await ctx.messenger.send_text(ctx.channel_id, text, buttons=kind_buttons(existing.id), silent=True)


async def on_missed_kind(ctx: RequestContext, arg: str, source_msg_id: str | None = None) -> None:
    """A kind button under the note above. It settles that note and its buttons."""
    if not ctx.is_owner:
        return
    event_id, _, kind = arg.partition("|")
    done = await asyncio.to_thread(amend_kind, event_id, kind)
    text = f"Recorded as missed: {kind_label(kind).lower()}." if done else "That record is gone, so nothing changed."
    await ctx.messenger.edit_text(ctx.channel_id, source_msg_id, text, None)


async def on_missed_command(ctx: RequestContext, note: str = "") -> None:
    """`/missed [note]`, for work with no card to tap, such as a chat turn long gone."""
    if not ctx.is_owner:
        await ctx.messenger.send_text(ctx.channel_id, "Only the owner can mark work as missed.")
        return
    repo = ctx.repo_name or ""
    if not repo:
        await ctx.messenger.send_text(ctx.channel_id, "Run /missed in a repo's thread, so it knows which repo this is about.")
        return
    session_id = ctx.session_id
    inst = ctx.store.latest_instance_for_session(session_id) if session_id else None
    repo_path = inst.repo_path if inst else ctx.store.list_repos().get(repo)
    sha = await asyncio.to_thread(head_sha, repo_path, inst.branch if inst else None)
    event = await asyncio.to_thread(
        record, repo, ctx.channel_id, inst.id if inst else "", sha, KIND_UNSPECIFIED, note,
    )
    await ctx.messenger.send_text(
        ctx.channel_id, "Recorded as missed. What kind? (Optional)", buttons=kind_buttons(event.id),
    )
