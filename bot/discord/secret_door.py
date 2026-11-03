"""The door: catches a pasted secret before anything else in the bot sees it.

Every way text gets in passes through here first: messages and their
attachments, thread titles, edits, slash command options and modals. Whatever
is certain goes straight to the vault (or is burned), and the rest of the
message carries on with a placeholder where the value was. Anything uncertain
is held in memory until the owner answers.

Not a containment boundary, and Discord has already seen whatever was pasted.
The invariants and what this does not protect against are in
``docs/design/secret-intake.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord

from bot import secret_detect, vault
from bot.discord.access import AccessResult
from bot.discord.attachments import (
    ATTACH_AUDIO_EXTS,
    ATTACH_AUDIO_MAX,
    ATTACH_DOC_EXTS,
    ATTACH_DOC_MAX,
    ATTACH_IMAGE_EXTS,
    ATTACH_IMAGE_MAX,
    ATTACH_TEXT_EXTS,
    ATTACH_TEXT_MAX,
    ATTACH_WORD_EXTS,
    _extract_docx_text,
)
from bot.platform.base import ButtonSpec
from bot.secret_detect import ASK, BURN, REMOVED, SURE, Finding

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

HOLD_TIMEOUT_SECONDS = 600
_NOT_SECRET_MAX = 1024
_MAX_ENTRY_ROWS = 4
_CUSTOM_ID_MAX = 100
_RENAME_WAIT_SECONDS = 300

_ENV_FILE = re.compile(r"^\.?env(\.[\w-]+)?$", re.IGNORECASE)
# ``env.py`` is Alembic's and ``env.json`` is a config: code to read, not to vault.
# A leading dot still counts, because ``.env.txt`` is how a .env gets past a filter.
_NOT_ENV_EXTS = ATTACH_TEXT_EXTS | {".py", ".js", ".ts", ".sh", ".rb", ".go", ".rs"}
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

_EXPOSED = "Treat it as exposed, Discord already has it."
# An extension worth putting back on a scrubbed file name. Shorter than any part
# of a JWT, so this never restores a piece of one.
_REAL_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}")


@dataclass
class DoorResult:
    """What is left of a message after screening.

    ``sanitized`` holds the final text of every body the door read, and
    ``prefetched`` the raw bytes of what it fetched because the message was
    about to go. Ingest uses both instead of reading the attachment again, so it
    can never pick up an unscanned body. ``consumed`` is what it must skip.
    """

    proceed: bool = True
    text: str | None = None
    deleted: bool = False
    prefetched: dict[int, bytes] = field(default_factory=dict)
    sanitized: dict[int, str] = field(default_factory=dict)
    consumed: set[int] = field(default_factory=set)


@dataclass
class _Report:
    """What this screening did, turned into one message at the end."""

    repo: str | None = None
    stored: list[vault.AddResult] = field(default_factory=list)
    burned: int = 0
    guest_hits: int = 0
    vault_failed: bool = False
    delete_failed: bool = False
    title_scrubbed: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class Hold:
    """A message waiting on the owner. In memory only: the text is never written."""

    hold_id: str
    message: discord.Message | None
    access: AccessResult
    channel_id: str
    repo: str | None
    door: DoorResult
    asks: list[Finding]
    route: bool
    resume: Callable[[DoorResult], Awaitable[None]] | None = None
    hold_msg_id: str | None = None
    ephemeral_msg: Any = None
    timeout_task: asyncio.Task | None = None


_holds: dict[str, Hold] = {}
_held_message_ids: set[int] = set()
# sha256 of what the owner said was not a secret, so a resend or the edit event
# Discord fires when a link unfurls does not ask again. Bounded, and gone on restart.
_not_secret: OrderedDict[str, None] = OrderedDict()
_background: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _remember_not_secret(value: str) -> None:
    _not_secret[_fingerprint(value)] = None
    while len(_not_secret) > _NOT_SECRET_MAX:
        _not_secret.popitem(last=False)


def _open_asks(findings: list[Finding]) -> list[Finding]:
    """Findings still worth asking about, each value once."""
    seen: set[str] = set()
    out: list[Finding] = []
    for f in findings:
        if f.tier != ASK or f.value in seen or _fingerprint(f.value) in _not_secret:
            continue
        seen.add(f.value)
        out.append(f)
    return out


def _without_dismissed(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.tier != ASK or _fingerprint(f.value) not in _not_secret]


# --- Where a key goes ------------------------------------------------------


def door_repo(bot: ClaudeBot, channel: Any) -> str | None:
    """The repo a channel belongs to, for the grant. Read-only: adopting is routing's job.

    ``_default`` and the lobby give no repo, so the owner decides with /secret grant.
    """
    forums = bot._forums
    lookup = forums.thread_to_project(str(channel.id))
    proj = lookup[0] if lookup else None
    if proj is None:
        parent_id = getattr(channel, "parent_id", None)
        if parent_id:
            proj = forums.forum_by_channel_id(str(parent_id))
    if proj is None or proj.repo_name == "_default":
        return None
    return proj.repo_name


def _placeholder(result: vault.AddResult) -> str:
    if result.tail:
        return f"[secret {result.name} stored, ending {result.tail}]"
    return f"[secret {result.name} stored]"


async def _store_entry(
    value: str, name: str | None, report: _Report, source_thread: str | None,
) -> vault.AddResult | None:
    """Vault a value. None when the vault is broken, which stores nothing."""
    try:
        result = await asyncio.to_thread(
            vault.add, value, name or secret_detect.default_name(value),
            report.repo, source_thread,
        )
    except vault.VaultUnavailable:
        report.vault_failed = True
        return None
    if not any(r.name == result.name for r in report.stored):
        report.stored.append(result)
    return result


async def _store(
    value: str, name: str | None, report: _Report, source_thread: str | None,
) -> str:
    """Vault a value and return what replaces it."""
    result = await _store_entry(value, name, report, source_thread)
    return _placeholder(result) if result else REMOVED


def _splice(text: str, replacements: list[tuple[Finding, str]]) -> str:
    parts: list[str] = []
    pos = 0
    for f, rep in sorted(replacements, key=lambda pair: pair[0].start):
        parts.append(text[pos:f.start])
        parts.append(rep)
        pos = f.end
    parts.append(text[pos:])
    return "".join(parts)


async def _take_certain(
    text: str, findings: list[Finding], is_owner: bool, report: _Report,
    source_thread: str | None, *, as_removed: bool = False,
) -> str:
    """Replace what needs no question: BURN and SURE, and everything for a guest.

    The owner's ASK findings stay in the text, to be settled by value when the
    hold is answered.
    """
    done: dict[str, str] = {}
    replacements: list[tuple[Finding, str]] = []
    for f in findings:
        if not is_owner:
            report.guest_hits += 1
            rep = REMOVED
        elif f.tier == BURN:
            report.burned += 1
            rep = REMOVED
        elif f.tier == SURE:
            rep = done.get(f.value) or await _store(f.value, f.suggested_name, report, source_thread)
            done[f.value] = rep
            if as_removed:
                rep = REMOVED
        else:
            continue
        replacements.append((f, rep))
    return _splice(text, replacements)


def _is_certain(findings: list[Finding], is_owner: bool) -> bool:
    return any(not is_owner or f.tier in (SURE, BURN) for f in findings)


def _replace_values(text: str, mapping: dict[str, str]) -> str:
    for value in sorted(mapping, key=len, reverse=True):
        text = text.replace(value, mapping[value])
    return text


# --- Telling the owner -----------------------------------------------------


def _stored_line(result: vault.AddResult, repo: str | None) -> str:
    ending = f", ending {result.tail}" if result.tail else ""
    if result.status == "conflict":
        return (
            f"`{result.conflict_with}` already holds a different value, so this "
            f"one is stored as `{result.name}`{ending}."
        )
    verb = "Already had" if result.status == "existing" else "Stored"
    if repo:
        return f"{verb} `{result.name}`{ending}. Sessions in {repo} can use it by name."
    return f"{verb} `{result.name}`{ending}. No repo can use it yet: run `/secret grant`."


def _entry_rows(stored: list[vault.AddResult]) -> list[list[ButtonSpec]]:
    rows: list[list[ButtonSpec]] = []
    for result in stored[:_MAX_ENTRY_ROWS]:
        replace_id = f"secret_replace:{result.conflict_with}|{result.name}"
        if result.status == "conflict" and len(replace_id) <= _CUSTOM_ID_MAX:
            rows.append([
                ButtonSpec(f"Replace {result.conflict_with}"[:80], replace_id),
                ButtonSpec("Keep both", f"secret_keep:{result.name}"),
            ])
        else:
            rows.append([
                ButtonSpec(f"Rename {result.name}"[:80], f"secret_rename:{result.name}"),
                ButtonSpec("Forget", f"secret_forget:{result.name}"),
            ])
    return rows


def _report_text(report: _Report) -> str:
    lines: list[str] = []
    for result in report.stored[:_MAX_ENTRY_ROWS]:
        lines.append(_stored_line(result, report.repo))
    if len(report.stored) > _MAX_ENTRY_ROWS:
        lines.append(f"And {len(report.stored) - _MAX_ENTRY_ROWS} more: see `/secret list`.")
    if report.vault_failed:
        lines.append(
            "The secret vault file can't be read, so I stored nothing and left it "
            "untouched. I still kept the value out of the session. Fix the file, "
            "then paste it again."
        )
    if report.burned:
        lines.append(
            "I removed a seed phrase or private key and stored nothing. "
            f"{_EXPOSED} Move the funds."
        )
    if report.guest_hits:
        lines.append(
            "I removed what looked like a secret from your message and stored "
            "nothing, so the session did not get it. Discord already saw it, so "
            "replace that key."
        )
    if report.title_scrubbed:
        lines.append("I also took it out of this thread's title.")
    if report.delete_failed:
        lines.append(
            "**I could not delete that message, so it is still visible: delete it "
            "yourself.** The value is still out of the session."
        )
    lines.extend(report.notes)
    return "\n".join(lines)


async def _post_report(bot: ClaudeBot, channel_id: str, report: _Report) -> None:
    text = _report_text(report)
    if not text:
        return
    try:
        await bot.messenger.send_text(channel_id, text, buttons=_entry_rows(report.stored) or None)
    except Exception:
        log.warning("Could not post the door's report in %s", channel_id, exc_info=True)


async def _delete(message: discord.Message, report: _Report) -> bool:
    try:
        await message.delete()
        return True
    except discord.NotFound:
        return True
    except Exception as exc:
        log.warning("Could not delete a screened message: %s", type(exc).__name__)
        report.delete_failed = True
        return False


# --- Thread titles ---------------------------------------------------------


async def _rename_thread(bot: ClaudeBot, thread: Any, name: str) -> None:
    """Runs in the background: a rename can sleep minutes on a 429 (see on_message)."""
    thread_id = str(thread.id)
    waited = 0
    while thread_id in bot._name_editing and waited < _RENAME_WAIT_SECONDS:
        await asyncio.sleep(1)
        waited += 1
    bot._name_editing.add(thread_id)
    try:
        await thread.edit(name=name)
    except Exception as exc:
        log.warning("Could not scrub thread %s title: %s", thread_id, type(exc).__name__)
    finally:
        bot._name_editing.discard(thread_id)


async def _screen_thread_name(
    bot: ClaudeBot, channel: Any, is_owner: bool, report: _Report,
) -> None:
    """Scrub a title, every message, so a rename is caught too.

    ``channel.name`` changes at once, so nothing downstream reads the old one
    while the real edit waits its turn.
    """
    if not isinstance(channel, discord.Thread):
        return
    name = channel.name or ""
    findings = _without_dismissed(secret_detect.scan(name))
    if not findings:
        return
    await _take_certain(name, findings, is_owner, report, str(channel.id), as_removed=True)
    clean = secret_detect.replace_findings(name, findings)[:100]
    channel.name = clean
    report.title_scrubbed = True
    _spawn(_rename_thread(bot, channel, clean))


# --- Attachments -----------------------------------------------------------


def is_env_file(filename: str) -> bool:
    """A leading dot may not survive Discord, so ``env`` and ``prod.env`` count."""
    lowered = filename.lower()
    if lowered.endswith(".env"):
        return True
    if not _ENV_FILE.match(filename):
        return False
    return lowered.startswith(".") or Path(lowered).suffix not in _NOT_ENV_EXTS


def parse_env(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value[:1] in ('"', "'"):
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        out.append((key, value))
    return out


def _env_value_is_secret(key: str, value: str) -> bool:
    if secret_detect.is_placeholder_value(value):
        return False
    if secret_detect.name_looks_secret(key):
        return not (value.isdigit() and len(value) < 12)
    return bool(secret_detect.scan(value, attachment=True))


async def _screen_env_file(
    att: Any, body: str, is_owner: bool, report: _Report, source_thread: str | None,
) -> str:
    """Store what it can and return a summary with names only."""
    stored: list[str] = []
    burned: list[str] = []
    skipped: list[str] = []
    # Over the whole file and before the lines are split: a quoted PEM runs across
    # lines, and read line by line its header is a value and its body lines are names.
    findings = await asyncio.to_thread(secret_detect.scan, body, attachment=True)
    body = secret_detect.replace_findings(body, [f for f in findings if f.tier == BURN])
    for key, value in parse_env(body):
        if not value:
            skipped.append(key)
            continue
        if secret_detect.name_burns(key) or REMOVED in value:
            burned.append(key)
            if is_owner:
                report.burned += 1
            continue
        if not is_owner or not _env_value_is_secret(key, value):
            skipped.append(key)
            continue
        result = await _store_entry(value, key, report, source_thread)
        if result:
            stored.append(result.name)
        else:
            skipped.append(key)
    if not is_owner and (burned or skipped):
        report.guest_hits += 1
    summary = (
        f"[env file {att.filename}: stored {', '.join(stored) or 'nothing'}; "
        f"burned {', '.join(burned) or 'nothing'}; "
        f"not stored {', '.join(skipped) or 'nothing'}]"
    )
    return secret_detect.scrub(summary)


async def _screen_filename(
    att: Any, is_owner: bool, report: _Report, source_thread: str | None,
) -> bool:
    """Scrub an attachment's name. True when it held something that means a delete.

    Entropy is skipped as in bodies, because plenty of ordinary uploads have
    hash names. A labelled value in a name is scrubbed without a question: a
    file name is not worth holding a whole message for.
    """
    filename = att.filename or ""
    findings = _without_dismissed(secret_detect.scan(filename, attachment=True))
    if not findings:
        return False
    certain = _is_certain(findings, is_owner)
    await _take_certain(filename, findings, is_owner, report, source_thread, as_removed=True)
    clean = secret_detect.replace_findings(filename, findings)
    # Ingest routes on the extension, so a value that swallowed it gives it back.
    suffix = Path(filename).suffix
    if findings[-1].end == len(filename) and _REAL_EXT.fullmatch(suffix):
        clean += suffix
    att.filename = clean
    return certain


async def _read_bodies(
    message: discord.Message, door: DoorResult, report: _Report, is_owner: bool,
    source_thread: str | None,
) -> tuple[dict[int, list[Finding]], list[str]]:
    """Read and scan every body the prompt could receive, before any delete.

    Returns the findings per attachment and the summaries of consumed .env files.
    """
    found: dict[int, list[Finding]] = {}
    env_summaries: list[str] = []
    for att in message.attachments:
        if not att.filename:
            continue
        ext = Path(att.filename).suffix.lower()
        is_env = is_env_file(att.filename) and att.size <= ATTACH_TEXT_MAX
        is_text = ext in ATTACH_TEXT_EXTS and att.size <= ATTACH_TEXT_MAX
        is_word = ext in ATTACH_WORD_EXTS and att.size <= ATTACH_DOC_MAX
        if not (is_env or is_text or is_word):
            continue
        original_name = att.filename
        try:
            raw = await att.read()
        except Exception as exc:
            log.warning("Could not read %s for screening: %s", original_name, type(exc).__name__)
            door.consumed.add(att.id)
            report.notes.append(
                f"I couldn't open **{original_name}**, downloading it failed. Try sending it again."
            )
            continue
        if is_env:
            door.consumed.add(att.id)
            body = raw.decode("utf-8", errors="replace")
            env_summaries.append(await _screen_env_file(att, body, is_owner, report, source_thread))
            continue
        if is_word:
            try:
                body = await asyncio.to_thread(_extract_docx_text, raw)
            except Exception as exc:
                log.warning("Could not extract %s: %s", original_name, type(exc).__name__)
                door.consumed.add(att.id)
                report.notes.append(
                    f"I couldn't open **{original_name}**, it may be damaged, password-protected, "
                    "or an older Word format. Saving it as a PDF and sending that usually works."
                )
                continue
            if len(body) > ATTACH_TEXT_MAX:
                body = body[:ATTACH_TEXT_MAX] + "\n\n[document truncated]"
        else:
            body = raw.decode("utf-8", errors="replace")
        door.sanitized[att.id] = body
        findings = await asyncio.to_thread(secret_detect.scan, body, attachment=True)
        found[att.id] = _without_dismissed(findings)
    return found, env_summaries


async def _prefetch_rest(message: discord.Message, door: DoorResult) -> None:
    """Fetch what ingest still needs: the CDN URL may die with the message."""
    for att in message.attachments:
        if att.id in door.sanitized or att.id in door.consumed or att.id in door.prefetched:
            continue
        ext = Path(att.filename or "").suffix.lower()
        wanted = (
            (ext in ATTACH_IMAGE_EXTS and att.size <= ATTACH_IMAGE_MAX)
            or (ext in ATTACH_DOC_EXTS and att.size <= ATTACH_DOC_MAX)
            or (ext in ATTACH_AUDIO_EXTS and att.size <= ATTACH_AUDIO_MAX)
        )
        if not wanted:
            continue
        try:
            door.prefetched[att.id] = await att.read()
        except Exception as exc:
            log.warning("Could not prefetch %s: %s", att.filename, type(exc).__name__)


# --- The door --------------------------------------------------------------


async def screen_message(
    bot: ClaudeBot, message: discord.Message, access: AccessResult,
) -> DoorResult:
    """Screen one message. ``proceed`` is False when it is now on hold."""
    channel = message.channel
    channel_id = str(channel.id)
    is_owner = access.is_owner
    door = DoorResult(text=message.content or "")
    report = _Report(repo=door_repo(bot, channel))

    await _screen_thread_name(bot, channel, is_owner, report)

    delete = False
    for att in message.attachments:
        if att.filename and await _screen_filename(att, is_owner, report, channel_id):
            delete = True

    body_findings, env_summaries = await _read_bodies(message, door, report, is_owner, channel_id)
    if env_summaries:
        delete = True

    content_findings = _without_dismissed(secret_detect.scan(door.text))
    asks = _open_asks(content_findings)
    if _is_certain(content_findings, is_owner):
        delete = True
    door.text = await _take_certain(door.text, content_findings, is_owner, report, channel_id)
    for att_id, findings in body_findings.items():
        asks.extend(a for a in _open_asks(findings) if a.value not in {x.value for x in asks})
        if _is_certain(findings, is_owner):
            delete = True
        door.sanitized[att_id] = await _take_certain(
            door.sanitized[att_id], findings, is_owner, report, channel_id,
        )
    if env_summaries:
        joined = "\n".join(env_summaries)
        door.text = f"{door.text}\n\n{joined}" if door.text else joined

    if delete:
        await _prefetch_rest(message, door)
        door.deleted = await _delete(message, report)
    await _post_report(bot, channel_id, report)

    if asks and is_owner:
        await _open_hold(
            bot, message=message, access=access, channel_id=channel_id,
            repo=report.repo, door=door, asks=asks, route=True,
        )
        door.proceed = False
    return door


async def screen_edit(bot: ClaudeBot, message: discord.Message, access: AccessResult) -> None:
    """Screen an edited message. Nothing is routed, so the prompt never re-runs."""
    channel_id = str(message.channel.id)
    is_owner = access.is_owner
    door = DoorResult(text=message.content or "")
    report = _Report(repo=door_repo(bot, message.channel))
    findings = _without_dismissed(secret_detect.scan(door.text))
    if not findings:
        return
    asks = _open_asks(findings)
    if _is_certain(findings, is_owner):
        door.text = await _take_certain(door.text, findings, is_owner, report, channel_id)
        door.deleted = await _delete(message, report)
    await _post_report(bot, channel_id, report)
    if asks and is_owner:
        await _open_hold(
            bot, message=message, access=access, channel_id=channel_id,
            repo=report.repo, door=door, asks=asks, route=False,
        )


def is_held(message_id: int) -> bool:
    return message_id in _held_message_ids


def edit_needs_door(content: str) -> bool:
    """Whether an edit is worth fetching the message for. Most are not."""
    return bool(_without_dismissed(secret_detect.scan(content)))


def transcript_screen(
    bot: ClaudeBot, message: discord.Message, door: DoorResult,
) -> Callable[[str], Awaitable[str]]:
    """Ingest runs this on a voice note's words before logging or echoing them.

    Any hit removes the whole transcript and vaults nothing: speech to text gets
    keys wrong, and a wrong key under a right name is worse than none.
    """

    async def screen(cleaned: str) -> str:
        findings = await asyncio.to_thread(secret_detect.scan, cleaned)
        if not findings:
            return cleaned
        report = _Report()
        await _prefetch_rest(message, door)
        door.deleted = await _delete(message, report) or door.deleted
        report.notes.append(
            "That voice note sounded like it held a secret, so I deleted it and the "
            "session got `[secret removed]`. I stored nothing, because speech to text "
            "gets keys wrong. OpenAI already has the audio, so treat it as exposed."
        )
        await _post_report(bot, str(message.channel.id), report)
        return REMOVED

    return screen


# --- Holds -----------------------------------------------------------------


def _ask_summary(asks: list[Finding]) -> str:
    if len(asks) > 1:
        return f"{len(asks)} values"
    tail = vault.tail_of(asks[0].value)
    return f"a value ending {tail}" if tail else "a short value"


def _hold_rows(hold_id: str, asks: list[Finding]) -> list[list[ButtonSpec]]:
    row = [
        ButtonSpec("Secret", f"secret_hold_yes:{hold_id}"),
        ButtonSpec("Not a secret", f"secret_hold_no:{hold_id}"),
    ]
    if any(secret_detect.is_private_key_candidate(a.value) for a in asks):
        row.append(ButtonSpec("Private key", f"secret_hold_pk:{hold_id}"))
    return [row]


async def _open_hold(
    bot: ClaudeBot, *, message: discord.Message | None, access: AccessResult,
    channel_id: str, repo: str | None, door: DoorResult, asks: list[Finding], route: bool,
    resume: Callable[[DoorResult], Awaitable[None]] | None = None,
    interaction: discord.Interaction | None = None,
) -> None:
    hold_id = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    hold = Hold(
        hold_id=hold_id, message=message, access=access, channel_id=channel_id,
        repo=repo, door=door, asks=asks, route=route, resume=resume,
    )
    prompt = (
        f"I'm holding that back: it contains {_ask_summary(asks)} that may be a "
        "secret. Nothing has gone to the session yet."
    )
    rows = _hold_rows(hold_id, asks)
    # Registered before the buttons exist, so a fast tap finds it.
    _holds[hold_id] = hold
    if message is not None:
        _held_message_ids.add(message.id)
    try:
        if interaction is not None:
            from bot.discord.adapter import _buttons_to_view
            hold.ephemeral_msg = await interaction.followup.send(
                prompt, view=_buttons_to_view(rows), ephemeral=True, wait=True,
            )
        else:
            hold.hold_msg_id = await bot.messenger.send_text(channel_id, prompt, buttons=rows)
    except Exception:
        log.warning("Could not post a hold in %s; dropping it", channel_id, exc_info=True)
        _holds.pop(hold_id, None)
        if message is not None:
            _held_message_ids.discard(message.id)
        return
    # A hold answered while the post was returning has nothing left to mark or time.
    if hold_id not in _holds:
        return
    if hold.hold_msg_id:
        try:
            await asyncio.to_thread(vault.add_hold_marker, channel_id, hold.hold_msg_id)
        except vault.VaultUnavailable:
            log.warning("Hold marker not written: the vault file is unreadable")
    hold.timeout_task = _spawn(_expire_later(bot, hold_id))


async def _close_hold(hold: Hold) -> None:
    if hold.message is not None:
        _held_message_ids.discard(hold.message.id)
    if hold.hold_msg_id:
        try:
            await asyncio.to_thread(vault.remove_hold_marker, hold.hold_msg_id)
        except OSError:
            log.warning("Could not clear a hold marker", exc_info=True)


async def _expire_later(bot: ClaudeBot, hold_id: str) -> None:
    await asyncio.sleep(HOLD_TIMEOUT_SECONDS)
    await expire_hold(bot, hold_id)


async def expire_hold(bot: ClaudeBot, hold_id: str) -> None:
    """Drop a hold nobody answered. The message stays, because nobody said to delete it."""
    hold = _holds.pop(hold_id, None)
    if hold is None:
        return
    await _close_hold(hold)
    where = "It is still in the channel, so delete it if it held a secret. " if (
        hold.message is not None and not hold.door.deleted
    ) else ""
    text = f"No answer in 10 minutes, so I dropped that message. {where}Paste it again to send it."
    try:
        if hold.ephemeral_msg is not None:
            await hold.ephemeral_msg.edit(content=text, view=None)
        else:
            await bot.messenger.edit_text(hold.channel_id, hold.hold_msg_id, text)
    except Exception:
        log.warning("Could not settle an expired hold", exc_info=True)


async def resolve_hold(
    bot: ClaudeBot, hold_id: str, choice: str,
) -> tuple[str, list[list[ButtonSpec]]]:
    """Settle a hold. ``choice`` is yes, no or pk. Returns the line and buttons to show."""
    hold = _holds.pop(hold_id, None)
    if hold is None:
        return "That hold has expired. Paste it again.", []
    if hold.timeout_task is not None:
        hold.timeout_task.cancel()
    await _close_hold(hold)

    door = hold.door
    report = _Report(repo=hold.repo)
    if choice == "no":
        for f in hold.asks:
            _remember_not_secret(f.value)
        text = "Understood, not a secret. Carrying on." if hold.route else "Understood, not a secret."
    else:
        mapping: dict[str, str] = {}
        for f in hold.asks:
            if choice == "pk":
                report.burned += 1
                mapping[f.value] = REMOVED
            else:
                mapping[f.value] = await _store(f.value, f.suggested_name, report, hold.channel_id)
        if door.text:
            door.text = _replace_values(door.text, mapping)
        for att_id, body in door.sanitized.items():
            door.sanitized[att_id] = _replace_values(body, mapping)
        if hold.message is not None and not door.deleted:
            await _prefetch_rest(hold.message, door)
            door.deleted = await _delete(hold.message, report)
        text = _report_text(report)
    if hold.route or hold.resume is not None:
        _spawn(_finish_hold(bot, hold))
    return text, _entry_rows(report.stored)


async def _finish_hold(bot: ClaudeBot, hold: Hold) -> None:
    """Runs apart from the button, because the turn it starts takes as long as it takes."""
    try:
        if hold.resume is not None:
            await hold.resume(hold.door)
        else:
            await bot._route_screened(hold.message, hold.access, hold.door)
    except Exception:
        log.exception("A held message failed to route")


async def notify_leftover_holds(bot: ClaudeBot) -> None:
    """Tell the owner about holds a previous process died with. Text was memory only."""
    markers = await asyncio.to_thread(vault.take_hold_markers)
    for marker in markers:
        text = (
            "The bot restarted while holding a message here, so it was dropped. "
            "It may still be in the channel, so delete it if it held a secret, "
            "and paste it again to send it."
        )
        try:
            await bot.messenger.edit_text(marker["channel_id"], marker["hold_msg_id"], text)
        except Exception:
            log.warning("Could not settle a leftover hold", exc_info=True)


# --- Slash commands --------------------------------------------------------


def _string_options(options: list[dict] | None):
    """Every string option, through subcommands (type 1) and groups (type 2)."""
    for option in options or []:
        kind = option.get("type")
        if kind in (1, 2):
            yield from _string_options(option.get("options"))
        elif kind == 3 and isinstance(option.get("value"), str):
            yield option


async def screen_slash(bot: ClaudeBot, interaction: discord.Interaction) -> bool:
    """Rewrites string options in place before the command reads them.

    A handler refuses anyone without access, and nothing is stored for them, so
    they pass through. Returning False cancels the command, which is the
    answer to anything uncertain: a slash command cannot wait on a button
    inside Discord's 3 second window.
    """
    if interaction.type != discord.InteractionType.application_command:
        return True
    data = interaction.data or {}
    options = list(_string_options(data.get("options")))  # type: ignore[arg-type]
    if not options:
        return True
    channel_id = str(interaction.channel_id)
    access = bot._check_access(interaction.user.id, channel_id=channel_id)
    if not access.allowed:
        return True
    try:
        scans = [(opt, _without_dismissed(secret_detect.scan(opt["value"]))) for opt in options]
        if not any(found for _, found in scans):
            return True
        if access.is_owner and any(
            f.tier in (ASK, BURN) for _, found in scans for f in found
        ):
            await interaction.response.send_message(
                "That looks like it holds a secret, so I did not run the command. "
                "Use `/secret add` to store a value without it passing through chat. "
                "If it is a real key, Discord already has it, so treat it as exposed.",
                ephemeral=True,
            )
            return False
        report = _Report(repo=door_repo(bot, interaction.channel) if interaction.channel else None)
        for opt, found in scans:
            if found:
                opt["value"] = await _take_certain(
                    opt["value"], found, access.is_owner, report, channel_id,
                )
        _spawn(_post_report(bot, channel_id, report))
        return True
    except Exception:
        log.exception("Slash screening failed")
        try:
            await interaction.response.send_message(
                "I couldn't screen that command for secrets, so I did not run it.",
                ephemeral=True,
            )
        except Exception:
            pass
        return False


# --- Modals ----------------------------------------------------------------


async def screen_modal_text(
    bot: ClaudeBot, interaction: discord.Interaction, access: AccessResult,
    text: str, repo: str | None, run: Callable[[str], Awaitable[None]],
) -> None:
    """Screen a deferred modal's text, then run it, now or once the owner answers."""
    findings = await asyncio.to_thread(secret_detect.scan, text)
    findings = _without_dismissed(findings)
    if not findings:
        await run(text)
        return
    channel_id = str(interaction.channel_id)
    report = _Report(repo=repo)
    clean = await _take_certain(text, findings, access.is_owner, report, channel_id)
    summary = _report_text(report)
    if summary:
        try:
            from bot.discord.adapter import _buttons_to_view
            view = _buttons_to_view(_entry_rows(report.stored))
            kwargs = {"view": view} if view is not None else {}
            await interaction.followup.send(summary, ephemeral=True, **kwargs)
        except Exception:
            log.warning("Could not report a modal's screening", exc_info=True)
    asks = _open_asks(findings) if access.is_owner else []
    if not asks:
        await run(clean)
        return

    async def resume(door: DoorResult) -> None:
        await run(door.text or "")

    await _open_hold(
        bot, message=None, access=access, channel_id=channel_id, repo=repo,
        door=DoorResult(text=clean), asks=asks, route=False, resume=resume,
        interaction=interaction,
    )
