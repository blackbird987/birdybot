"""Publish this machine's project directory to the channel it shares with a peer.

One message owned by this bot, edited in place every five minutes. It only ever
writes to that one message: nothing here reads what the peer posted, and
``on_message`` is untouched, so no peer message can start anything. Inert unless
``config.PEER_ENABLED``. See docs/design/peer-link.md.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import TYPE_CHECKING

import discord

from bot import config, peer_record
from bot.claude.sandbox import GIT_SAFE
from bot.discord.dashboard import _get_version
from bot.procutil import run_capture

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

PUBLISH_INTERVAL_SECS = 300
_GIT_TIMEOUT = 10
_READY_POLL_SECS = 5
# How far back to look for our own message, the same window the reader has.
# Nothing else posts here in this stage, so this is a margin for the owner's notes.
_HISTORY_LIMIT = 100


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
