"""Thread/channel/forum management for Discord sessions."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import discord

from bot.platform.formatting import MODE_COLOR, MODE_DISPLAY, MODE_EMOJI, mode_emoji, mode_name

log = logging.getLogger(__name__)


def _private_overwrites(
    guild: discord.Guild,
    bot_member: discord.Member,
    owner_id: int | None,
) -> dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
    """Build permission overwrites: deny @everyone, allow bot + owner."""
    overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        bot_member: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            manage_threads=True,
        ),
    }
    if owner_id:
        member = guild.get_member(owner_id)
        if member:
            overwrites[member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
            )
    return overwrites


async def ensure_category(
    guild: discord.Guild,
    name: str,
    bot_member: discord.Member,
    owner_id: int | None = None,
) -> discord.CategoryChannel:
    """Find or create a private category by name."""
    # Check if it already exists
    for cat in guild.categories:
        if cat.name.lower() == name.lower():
            log.info("Found existing category %s (%s)", cat.id, cat.name)
            return cat

    overwrites = _private_overwrites(guild, bot_member, owner_id)
    category = await guild.create_category(name, overwrites=overwrites)
    log.info("Created private category %s (%s)", category.id, category.name)
    return category


async def ensure_lobby(
    category: discord.CategoryChannel,
    name: str = "control-room",
) -> discord.TextChannel:
    """Find or create the control center channel inside a category (inherits perms)."""
    # Also match old name "lobby" for migration
    for ch in category.text_channels:
        if ch.name in (name, "lobby"):
            if ch.name == "lobby":
                try:
                    await ch.edit(name=name)
                    log.info("Renamed lobby -> %s (%s)", name, ch.id)
                except Exception:
                    pass
            log.info("Found existing lobby channel %s (%s)", ch.id, ch.name)
            return ch

    channel = await category.guild.create_text_channel(name, category=category)
    log.info("Created lobby channel %s (%s)", channel.id, channel.name)
    return channel


def sanitize_channel_name(text: str, separator: str = "-") -> str:
    """Convert text to a valid Discord channel name."""
    name = text.lower()
    name = re.sub(r"[^a-z0-9\s\-_]", "", name)
    name = re.sub(r"[\s]+", separator, name)
    name = re.sub(r"[-_]{2,}", separator, name)
    name = name.strip("-_")
    return name[:70] or "session"


def build_channel_name(topic: str) -> str:
    """Build a channel name from topic."""
    return sanitize_channel_name(topic)


# --- Thread name format: [🔄 ]{mode_emoji} {topic} ---

PROCESSING_EMOJI = "\U0001f504"  # 🔄


def parse_thread_name(name: str) -> tuple[bool, str | None, str]:
    """Parse thread name -> (is_processing, mode_key, base_topic).

    mode_key is "explore"/"plan"/"build" or None if no mode emoji found.
    """
    processing = False
    if name.startswith(PROCESSING_EMOJI):
        processing = True
        name = name[len(PROCESSING_EMOJI):].lstrip()
    mode_key = None
    for m, e in MODE_EMOJI.items():
        if name.startswith(e):
            mode_key = m
            name = name[len(e):].lstrip()
            break
    return processing, mode_key, name


def build_thread_name(topic: str, mode: str, processing: bool = False) -> str:
    """Build thread name: [🔄 ]{mode_emoji} {topic}, max 100 chars."""
    emoji = mode_emoji(mode)
    prefix = f"{PROCESSING_EMOJI} {emoji}" if processing else emoji
    return f"{prefix} {topic}"[:100]


def build_title_name(text: str) -> str:
    """Build a readable forum post name from LLM-generated title.

    Unlike sanitize_channel_name(), preserves casing and allows spaces
    for better readability (Discord forum posts support mixed case).
    """
    name = re.sub(r"[^a-zA-Z0-9\s\-]", "", text)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:90] or "session"  # Leave room for emoji prefix


# --- Forum Channel Helpers ---


async def ensure_forum(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    name: str,
    auto_archive: int = 4320,
) -> discord.ForumChannel:
    """Find or create a ForumChannel under the category (inherits private perms).

    Args:
        auto_archive: Default auto-archive duration in minutes (4320 = 3 days).
    """
    sanitized = sanitize_channel_name(name)

    # Check if forum already exists in category
    for ch in category.channels:
        if isinstance(ch, discord.ForumChannel) and ch.name == sanitized:
            log.info("Found existing forum %s (%s)", ch.id, ch.name)
            return ch

    forum = await guild.create_forum(
        name=sanitized,
        category=category,
        default_auto_archive_duration=auto_archive,
    )
    # Sync permissions from category (private to owner + bot)
    try:
        await forum.edit(sync_permissions=True)
    except Exception:
        pass
    log.info("Created forum channel %s (%s) in category %s", forum.id, forum.name, category.name)
    return forum


# Discord button styles per mode (explore=gray, plan=blue, build=green)
_MODE_BUTTON_STYLE: dict[str, discord.ButtonStyle] = {
    "explore": discord.ButtonStyle.secondary,
    "plan":    discord.ButtonStyle.primary,
    "build":   discord.ButtonStyle.success,
}


def mode_select_view(current_mode: str = "explore") -> discord.ui.View:
    """Build a persistent View with mode-selection buttons for new sessions.

    Active mode button is disabled (standard "already selected" UX).
    Labels from MODE_DISPLAY; styles from _MODE_BUTTON_STYLE.
    """
    view = discord.ui.View(timeout=None)
    for mode, name in MODE_DISPLAY.items():
        btn = discord.ui.Button(
            label=name,
            style=_MODE_BUTTON_STYLE.get(mode, discord.ButtonStyle.secondary),
            custom_id=f"mode_set:{mode}",
            disabled=(mode == current_mode),
        )
        view.add_item(btn)
    return view


async def create_forum_post(
    forum: discord.ForumChannel,
    name: str,
    origin: str = "bot",
    topic_preview: str = "",
    current_mode: str = "explore",
) -> tuple[discord.Thread, discord.Message]:
    """Create a new forum post (thread + starter message).

    Returns (thread, starter_message).
    """
    # Prefix thread name with mode emoji
    name = build_thread_name(name, current_mode)

    embed = discord.Embed(
        title="Session",
        description=topic_preview[:200] or "New session",
        color=discord.Color(MODE_COLOR.get(current_mode, 0x5865F2)),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Origin", value=origin, inline=True)
    embed.add_field(name="Mode", value=mode_name(current_mode), inline=True)

    view = mode_select_view(current_mode)
    result = await forum.create_thread(name=name, embed=embed, view=view)
    thread = result.thread
    message = result.message
    log.info("Created forum post %s (%s) in forum %s", thread.id, name, forum.name)
    return thread, message


async def ensure_forum_tags(forum: discord.ForumChannel) -> dict[str, discord.ForumTag]:
    """Create standard tags on a forum channel. Returns {name: tag} dict."""
    desired: dict[str, str | None] = {
        "active": "\U0001f504",      # 🔄  (status)
        "completed": "\u2705",       # ✅  (status)
        "failed": "\u274c",          # ❌  (status)
        "cli": None,
    }
    # Add mode tags from MODE_EMOJI (single source of truth)
    for mode, emoji in MODE_EMOJI.items():
        desired[mode] = emoji
    existing = {tag.name: tag for tag in forum.available_tags}
    missing = []
    for name, emoji in desired.items():
        if name not in existing:
            missing.append(discord.ForumTag(name=name, emoji=emoji))

    if missing:
        new_tags = list(forum.available_tags) + missing
        await forum.edit(available_tags=new_tags[:20])  # Discord limit: 20 tags
        # Re-fetch to get IDs
        existing = {tag.name: tag for tag in forum.available_tags}

    return existing


# --- Channel helpers ---


async def create_thread(
    channel: discord.TextChannel,
    name: str,
    auto_archive_duration: int = 60,
) -> discord.Thread:
    """Create a thread in the lobby channel for a query."""
    # Truncate name to Discord's 100 char limit
    name = name[:100]
    thread = await channel.create_thread(
        name=name,
        auto_archive_duration=auto_archive_duration,
        type=discord.ChannelType.public_thread,
    )
    log.info("Created thread %s (%s)", thread.id, name)
    return thread


async def create_task_channel(
    guild: discord.Guild,
    name: str,
    category: discord.CategoryChannel | None = None,
) -> discord.TextChannel:
    """Create a channel for a background task."""
    name = name[:100].lower().replace(" ", "-")
    channel = await guild.create_text_channel(
        name=name,
        category=category,
    )
    log.info("Created task channel %s (%s)", channel.id, name)
    return channel


