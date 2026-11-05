"""This machine's side of the channel it shares with a peer.

The publisher keeps one message owned by this bot current, edited in place every five
minutes. Questions and answers do not go in the channel, where they would become the
newest message by this author: each machine has an outbox, a public thread on its
directory message. A reader polls the peer's. Polling is the whole of how anything
arrives, so catching up after sleep is the same path as a normal tick, and
``on_message`` stays untouched: no peer message can start anything by being posted.
What an accepted record may then do is in ``bot/engine/peer_questions.py``. Inert
unless ``config.PEER_ENABLED``. See docs/design/peer-link.md.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from bot import config, peer_record
from bot.claude.sandbox import GIT_SAFE
from bot.discord.adapter import _buttons_to_view
from bot.discord.dashboard import _get_version
from bot.discord import peer_turns
from bot.engine import peer_questions
from bot.platform.base import ButtonSpec
from bot.procutil import run_capture

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

PUBLISH_INTERVAL_SECS = 300
_GIT_TIMEOUT = 10
_READY_POLL_SECS = 5
# How far back to look for our own message, the same window the reader has.
# Nothing else posts in the channel itself, so this is a margin for the owner's notes.
_HISTORY_LIMIT = 100
READ_INTERVAL_SECS = 30
OUTBOX_NAME = "outbox"
_OUTBOX_ARCHIVE_MINUTES = 10080  # 7 days, the longest Discord has
_THREAD_ALREADY_EXISTS = 160004
_THREAD_READ_LIMIT = 100
# A directory message replaced by hand leaves its old thread behind. Keep reading it
# until it has been quiet for a good while, so nothing already in it is lost.
_OLD_THREADS_TO_SCAN = 3
_DRAINED_AFTER_SECS = 14 * 86400


def _git_read(path: str, *args: str) -> str | None:
    """One bounded read-only git command. None if it failed, for any reason."""
    try:
        r = run_capture(
            ["git", *GIT_SAFE, "--no-optional-locks", *args],
            cwd=path, timeout=_GIT_TIMEOUT, encoding="utf-8", errors="replace",
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _read_repo(name: str, path: str, hidden: bool) -> list:
    """One repo's entry. No fetch: this reports what this machine already knows."""
    sha = _git_read(path, "rev-parse", "--short=7", "HEAD")
    if sha is None:
        return peer_record.repo_entry(name, hidden, None, None, None, None)
    ahead = _git_read(path, "rev-list", "--count", "@{upstream}..HEAD")
    status = _git_read(path, "status", "--porcelain")
    return peer_record.repo_entry(
        name, hidden, sha,
        # Detached HEAD has no branch, and this fails. None says so.
        _git_read(path, "symbolic-ref", "--short", "-q", "HEAD"),
        int(ahead) if ahead and ahead.isdigit() else None,
        None if status is None else bool(status),
    )


def snapshot_repos(store) -> list[tuple[str, str, bool]]:
    """(name, path, hidden) per repo. Reads the store, so call it on the event loop."""
    # Hidden repos last: if the message has to lose some, they go first.
    return sorted(
        ((name, path, store.is_repo_dormant(name)) for name, path in store.list_repos().items()),
        key=lambda repo: (repo[2], repo[0].lower()),
    )


def collect_message(repos: list[tuple[str, str, bool]], running: int) -> str:
    """The record as message text. Runs git, so call it off the event loop."""
    return peer_record.encode(
        config.MACHINE_NAME, _get_version(), running,
        [_read_repo(*repo) for repo in repos],
    )


async def _find_own_message(channel, bot_id: int) -> discord.Message | None:
    """Our newest message in the channel. Raises if the channel cannot be read."""
    async for message in channel.history(limit=_HISTORY_LIMIT):
        if message.author.id == bot_id:
            return message
    return None


async def publish_once(channel, bot_id: int, text: str, known: discord.Message | None):
    """Edit our one message, creating it only after a read that found none.

    A failed read raises out of here, and that is the guard against a second
    message: not finding it is only believed when the history answered.
    """
    message = known or await _find_own_message(channel, bot_id)
    if message is None:
        return await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    try:
        return await message.edit(content=text)
    except discord.NotFound:
        # Deleted by hand since we last looked. Look again next time.
        return None


async def _stopped_within(stop_event: asyncio.Event, secs: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=secs)
        return True
    except asyncio.TimeoutError:
        return False


async def run_peer_publisher(bot: ClaudeBot, stop_event: asyncio.Event) -> None:
    """Background task: keep our directory message current. Never fails the bot."""
    if not peer_record.NAME_RE.fullmatch(config.MACHINE_NAME):
        # The reader would reject every record, so say why here rather than there.
        log.warning("Peer link: MACHINE_NAME is not a usable name, not publishing")
        return
    # Not _wait_for_ready: that gives up after a minute, and a slow start would
    # then make this machine look offline until the next reboot.
    while not bot._ready_event.is_set():
        if await _stopped_within(stop_event, _READY_POLL_SECS):
            return
    channel = None
    known = None
    while not stop_event.is_set():
        try:
            if channel is None:
                channel = await bot.fetch_channel(config.PEER_CHANNEL_ID)
                if not isinstance(channel, discord.TextChannel):
                    log.warning("Peer link: channel %s is not a text channel, not publishing",
                                config.PEER_CHANNEL_ID)
                    return
                log.info("Peer link: publishing as %s", config.MACHINE_NAME)
            text = await asyncio.to_thread(
                collect_message, snapshot_repos(bot._store), bot._store.running_count(),
            )
            known = await publish_once(channel, bot.user.id, text, known)
            log.info("Peer link: published %d chars", len(text))
            # Every time, not just once: publish_once replaces a deleted message, and
            # with no thread on it the other side would report this machine as not
            # updated yet.
            if known is not None:
                await _ensure_outbox_quietly(bot, channel, known)
        except asyncio.CancelledError:
            raise
        except (discord.NotFound, discord.Forbidden):
            if channel is None:
                log.warning("Peer link: channel %s is unreachable, not publishing",
                            config.PEER_CHANNEL_ID)
                return
            known = None
            log.warning("Peer link: publish refused, will try again", exc_info=True)
        except Exception:
            known = None
            log.warning("Peer link: publish failed, will try again", exc_info=True)
        if await _stopped_within(stop_event, PUBLISH_INTERVAL_SECS):
            return


# --- The outbox: a thread on the directory message --------------------------


def _from_peer(message) -> bool:
    """The peer's own token wrote this. A webhook carrying the same id is not that."""
    return message.author.id == config.PEER_BOT_ID and message.webhook_id is None


async def _thread_on(bot, channel, message_id: int):
    """The thread on a message in this channel, or None.

    A thread started on a message shares its id. Fetched by id because
    ``Message.thread`` leaves out archived threads, and these sleep after 7 days.
    """
    try:
        thread = await bot.fetch_channel(message_id)
    except discord.NotFound:
        return None
    if isinstance(thread, discord.Thread) and thread.parent_id == channel.id:
        return thread
    return None


async def ensure_outbox(bot, channel, message):
    """This machine's outbox, made if it is missing. Its existence tells the peer we can answer."""
    thread = await _thread_on(bot, channel, message.id)
    if thread is not None:
        return thread
    try:
        return await message.create_thread(
            name=OUTBOX_NAME, auto_archive_duration=_OUTBOX_ARCHIVE_MINUTES,
        )
    except discord.HTTPException as exc:
        if exc.code != _THREAD_ALREADY_EXISTS:
            raise
        return await _thread_on(bot, channel, message.id)


_outbox_warned = False


async def _ensure_outbox_quietly(bot, channel, message) -> None:
    """A missing permission must not stop the directory being published."""
    global _outbox_warned
    try:
        await ensure_outbox(bot, channel, message)
        _outbox_warned = False
    except asyncio.CancelledError:
        raise
    except Exception:
        if not _outbox_warned:
            log.warning("Peer link: could not make the outbox thread. Questions will be "
                        "refused until it exists: re-run scripts/peer_setup.py", exc_info=True)
        _outbox_warned = True


class DiscordTransport:
    """The real side of ``peer_questions.Transport``."""

    def __init__(self, bot, channel):
        self._bot = bot
        self._channel = channel

    async def _own_outbox(self):
        """Looked up each time. A kept copy would go on posting to a replaced message's
        old thread, which the other side stops reading once it has gone quiet."""
        message = await _find_own_message(self._channel, self._bot.user.id)
        if message is None:
            raise RuntimeError("this machine has not published its directory yet")
        return await ensure_outbox(self._bot, self._channel, message)

    async def _peer_message(self):
        async for message in self._channel.history(limit=_HISTORY_LIMIT):
            if _from_peer(message):
                return message
        return None

    async def peer_view(self) -> peer_questions.PeerView:
        """Asked fresh each time, because the answer decides whether a question goes."""
        message = await self._peer_message()
        if message is None:
            return peer_questions.PeerView(error="the other machine has not published yet")
        try:
            record = peer_record.parse(message.content)
        except peer_record.InvalidRecord as exc:
            return peer_questions.PeerView(error=f"its directory was rejected ({exc})")
        stamp = message.edited_at or message.created_at
        age = int((datetime.now(timezone.utc) - stamp).total_seconds())
        return peer_questions.PeerView(
            record=record, age_secs=age,
            looks_offline=age > peer_record.OFFLINE_AFTER_SECS,
            has_outbox=await _thread_on(self._bot, self._channel, message.id) is not None,
        )

    async def post(self, text: str) -> None:
        thread = await self._own_outbox()
        await thread.send(text, allowed_mentions=discord.AllowedMentions.none())

    async def post_card(self, record: dict) -> str:
        thread = await self._own_outbox()
        owner = self._bot._discord_user_id
        view = _buttons_to_view([[
            ButtonSpec("Run once", f"{peer_questions.RUN_ACTION}:{record['id']}"),
            ButtonSpec("Decline", f"{peer_questions.DECLINE_ACTION}:{record['id']}"),
        ]])
        mentions = discord.AllowedMentions(
            everyone=False, roles=False, replied_user=False,
            users=[discord.Object(int(owner))] if owner else False,
        )
        message = await thread.send(peer_turns.card_text(record, owner), view=view,
                                    allowed_mentions=mentions)
        return str(message.id)

    async def edit_card(self, message_id: str, text: str) -> None:
        thread = await self._own_outbox()
        # Sending wakes a sleeping thread by itself. Editing is refused (error 50083),
        # and a card a week old may well be in one.
        if thread.archived:
            await thread.edit(archived=False)
        message = await thread.fetch_message(int(message_id))
        await message.edit(content=text, view=None, allowed_mentions=discord.AllowedMentions.none())


# --- The reader ---------------------------------------------------------------


_protocol_warned: set[str] = set()


def _accept(store, message, now: float) -> bool:
    """One message from the peer's outbox. False holds the cursor here."""
    try:
        record = peer_record.parse_link(message.content)
    except peer_record.NotARecord:
        return True  # A card, or a person talking. Not for us.
    except peer_record.UnknownProtocol as exc:
        # It may be perfectly good, and this version cannot tell. An update here reads it.
        if exc.args[0] not in _protocol_warned:
            _protocol_warned.add(exc.args[0])
            log.warning("Peer link: the other machine speaks %s. Update this one to read it.", exc)
        return False
    except peer_record.InvalidRecord as exc:
        log.warning("Peer link: skipped a bad record in message %s: %s", message.id, exc)
        return True
    if record["p"] == "q":
        peer_questions.on_question(store, record, str(message.id), now)
    else:
        peer_questions.on_answer_part(store, record, now)
    return True


async def read_thread(thread, store, now: float) -> int:
    """Everything new in one outbox thread, in order. Returns how many messages it saw."""
    key = str(thread.id)
    cursor = store.peer_cursor(key) or key
    seen = 0
    async for message in thread.history(
        after=discord.Object(int(cursor)), limit=_THREAD_READ_LIMIT, oldest_first=True,
    ):
        seen += 1
        if _from_peer(message) and not _accept(store, message, now):
            break
        store.peer_set_cursor(key, str(message.id))
    else:
        if store.peer_cursor(key) is None:
            store.peer_set_cursor(key, key)
    return seen


async def peer_threads(bot, channel, store) -> tuple[list, set[int]]:
    """The peer's outbox threads worth reading: its live one, and any it left behind.

    Also returns the ids still found through the channel. Their cursors must stay,
    since without one such a thread would be read from the start again.
    """
    candidates: list[int] = []
    async for message in channel.history(limit=_HISTORY_LIMIT):
        if _from_peer(message):
            candidates.append(message.id)
            if len(candidates) >= _OLD_THREADS_TO_SCAN:
                break
    visible = set(candidates)
    for known in store.peer_cursor_threads():
        if int(known) not in candidates:
            candidates.append(int(known))
    threads = []
    for candidate in candidates:
        thread = await _thread_on(bot, channel, candidate)
        if thread is None:
            if candidate not in visible:
                store.peer_drop_cursor(str(candidate))
            continue
        threads.append(thread)
    return threads, visible


async def _read_new(bot, channel, store, now: float) -> None:
    threads, visible = await peer_threads(bot, channel, store)
    for thread in threads:
        seen = await read_thread(thread, store, now)
        # The cursor is the last thing read in it, so this is how long it has been quiet.
        last = store.peer_cursor(str(thread.id)) or thread.id
        if (not seen and thread.id not in visible
                and now - peer_questions.snowflake_time(last) > _DRAINED_AFTER_SECS):
            store.peer_drop_cursor(str(thread.id))


async def reader_tick(bot, channel, transport) -> None:
    """One pass of everything the link owes.

    Each step is on its own: Discord refusing a read must not stop an answer already
    here from being delivered, and the next tick repeats whatever failed.
    """
    store = bot._store
    now = peer_questions.now_secs()

    async def _close_old() -> None:
        await peer_turns.notify_closed(bot, peer_questions.expire_asked(store, now))
        peer_questions.prune(store, now)

    async def _deliver() -> None:
        peer_turns.start_deliveries(bot)

    steps = (
        ("read", lambda: _read_new(bot, channel, store, now)),
        ("card", lambda: peer_questions.card_new_questions(store, transport, now)),
        ("expire cards", lambda: peer_questions.expire_cards(store, transport, now)),
        ("post", lambda: peer_questions.pump_posting(store, transport)),
        ("close", _close_old),
        ("deliver", _deliver),
    )
    for name, step in steps:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Peer link: the %s step failed, will retry", name, exc_info=True)


async def run_peer_reader(bot: ClaudeBot, stop_event: asyncio.Event) -> None:
    """Background task: follow the peer's outbox. Never fails the bot.

    Unlike the publisher it keeps trying if the channel cannot be reached: an answer
    may be waiting, and the channel may only have needed its permissions fixed.
    """
    while not bot._ready_event.is_set():
        if await _stopped_within(stop_event, _READY_POLL_SECS):
            return
    transport = None
    channel = None
    warned = False
    while not stop_event.is_set():
        try:
            if transport is None:
                channel = await bot.fetch_channel(config.PEER_CHANNEL_ID)
                if not isinstance(channel, discord.TextChannel):
                    log.warning("Peer link: channel %s is not a text channel, not reading",
                                config.PEER_CHANNEL_ID)
                    return
                transport = DiscordTransport(bot, channel)
                peer_questions.set_transport(transport)
                log.info("Peer link: reading the other machine's outbox")
            await reader_tick(bot, channel, transport)
            warned = False
        except asyncio.CancelledError:
            raise
        except Exception:
            if not warned:
                log.warning("Peer link: read failed, will keep trying", exc_info=True)
            warned = True
        if await _stopped_within(stop_event, READ_INTERVAL_SECS):
            return
