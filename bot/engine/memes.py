"""#memes — paste an X link in a channel, get the video back as a file.

Sharing an X link to friends on Instagram is bad: they need an account, they
get no sound, half the time it won't play. So the video has to arrive as a
file. This is the Discord end of that; the actual work — download, size
budgeting, re-encode, cache — lives in the ``memepipe`` package, which knows
nothing about Discord and is driven identically by its own standalone bot.

The whole point is that a meme drop NEVER reaches Claude. A link pasted here
is handled inline by the gateway and costs nothing: no session spawns, no
tokens burn, no thread is created. That is why ``handle`` claims the channel
unconditionally (see its docstring) rather than falling through when a message
turns out not to contain a link.

Not wired to the /image directive on purpose: that path is for a turn pushing
a picture out of a session, is capped at four files, and rejects video. This
is inbound-triggered, one video at a time, and has no session behind it.
"""

from __future__ import annotations

import asyncio
import logging
import os

from bot import config

log = logging.getLogger(__name__)

# ffmpeg is the expensive part and this runs inside the gateway process, next
# to every live session. memepipe's own default cap, applied here globally so
# a burst of pasted links can't stack encodes on top of a build.
_ENCODE_SLOTS = asyncio.Semaphore(2)

# Discord rejects a file on the exact ceiling, and its multipart envelope adds
# a little. Leave the budget a hair under what the guild advertises.
_UPLOAD_HEADROOM = 256 * 1024


def _channel_id() -> int | None:
    """Read the channel straight from the environment, not bot.config.

    Deliberate exception to "all env in one place": this setting lived in
    bot/config.py once and was silently erased by an unrelated merge, taking
    the feature with it while the module itself survived untouched. Keeping
    the whole feature inside one file means a merge either has it or doesn't,
    rather than half of it.
    """
    raw = (os.getenv("MEMEPIPE_CHANNEL_ID") or "").strip()
    return int(raw) if raw.isdigit() else None


def enabled() -> bool:
    return _channel_id() is not None


def _budget(message) -> int:
    """Bytes we can actually attach, asked of the real guild.

    A boosted server gets a bigger ceiling and therefore better quality for
    free — hardcoding 10 MiB would silently throw that away.
    """
    limit = getattr(message.guild, "filesize_limit", 0) or 10 * 1024 * 1024
    return max(1024 * 1024, limit - _UPLOAD_HEADROOM)


async def handle(message) -> bool:
    """Deal with a message in the memes channel. True means "handled, stop".

    Returns True for EVERY message in that channel, links or not. The channel
    is not a session channel: chatter in it should be ignored, not answered by
    Claude. False is returned only when the feature is off or the message is
    somewhere else entirely.
    """
    if message.channel.id != _channel_id():
        return False

    # Imported here, not at module scope: a missing memepipe install should
    # cost this one feature, not stop the bot from booting.
    try:
        from memepipe.config import Config
        from memepipe.links import find_status_links
        from memepipe.pipeline import PipelineError, process
    except ImportError:
        log.warning("memes: memepipe is not installed in this venv", exc_info=True)
        return True

    links = find_status_links(message.content or "")
    if not links:
        return True

    cfg = Config.from_env()
    cfg.work_dir = config.DATA_DIR / "memepipe" / "work"
    cfg.cache_dir = config.DATA_DIR / "memepipe" / "cache"
    budget = _budget(message)

    try:
        await message.add_reaction("👀")
    except Exception:
        pass  # a missing reaction must never cost us the video

    delivered = 0
    for link in links:
        try:
            async with _ENCODE_SLOTS:
                delivery = await process(link, budget, cfg)
        except PipelineError as exc:
            # memepipe phrases these for a human already ("post has no video",
            # "too long to fit at watchable quality"), so pass them straight
            # through rather than flattening to "something went wrong".
            await message.reply(f"❌ {exc}", mention_author=False)
            continue
        except Exception:
            log.exception("memes: unexpected failure on %s", link.url)
            await message.reply(
                "❌ that one broke in a way I didn't plan for — check the bot log.",
                mention_author=False,
            )
            continue

        try:
            import discord

            await message.reply(
                delivery.caption,
                file=discord.File(str(delivery.path), filename=delivery.filename),
                mention_author=False,
            )
            delivered += 1
        except Exception:
            log.exception("memes: upload failed for %s", link.url)
            await message.reply(
                f"❌ got the video ({delivery.size // 1024}KB) but Discord "
                "refused the upload.",
                mention_author=False,
            )

    try:
        await message.remove_reaction("👀", message.guild.me)
        if delivered:
            await message.add_reaction("✅")
    except Exception:
        pass

    return True
