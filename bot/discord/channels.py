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
    """Find or create the lobby channel inside a category (inherits perms)."""
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


# --- Thread name format: {mode_emoji} {topic} ---


def parse_thread_name(name: str) -> tuple[bool, str | None, str]:
    """Parse thread name -> (is_processing, mode_key, base_topic).

    is_processing is always False (legacy compat — processing state moved to tags).
    mode_key is "explore"/"plan"/"build" or None if no mode emoji found.
    """
    # Strip legacy 🔄 prefix if still present in old thread names
    if name.startswith("\U0001f504"):
        name = name[1:].lstrip()
    mode_key = None
    for m, e in MODE_EMOJI.items():
        if name.startswith(e):
            mode_key = m
            name = name[len(e):].lstrip()
            break
    return False, mode_key, name


def build_thread_name(topic: str, mode: str) -> str:
    """Build thread name: {mode_emoji} {topic}, max 100 chars."""
    emoji = mode_emoji(mode)
    return f"{emoji} {topic}"[:100]


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


# --- Per-user forum helpers ---


async def ensure_user_forum(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    bot_member: discord.Member,
    user_id: int,
    display_name: str,
    repo_names: list[str],
    owner_id: int | None = None,
    auto_archive: int = 4320,
) -> discord.ForumChannel:
    """Create or find a personal forum channel for a granted user.

    The forum is named after the user and has repo names as tags.
    Only the bot, the owner, and the granted user can see it.
    """
    forum_name = sanitize_channel_name(display_name)

    # Check if it already exists
    for ch in category.channels:
        if isinstance(ch, discord.ForumChannel) and ch.name == forum_name:
            log.info("Found existing user forum %s (%s)", ch.id, ch.name)
            # Sync tags
            await sync_user_forum_tags(ch, repo_names)
            return ch

    # Build permissions: deny @everyone, allow bot + owner + user
    overwrites = _private_overwrites(guild, bot_member, owner_id)
    member = guild.get_member(user_id)
    if member:
        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            send_messages_in_threads=True,
            create_public_threads=True,
        )

    forum = await guild.create_forum(
        name=forum_name,
        category=category,
        default_auto_archive_duration=auto_archive,
        overwrites=overwrites,
    )
    log.info("Created user forum %s (%s) for user %s", forum.id, forum_name, user_id)

    # Add repo tags
    await sync_user_forum_tags(forum, repo_names)

    return forum


async def sync_user_forum_tags(
    forum: discord.ForumChannel,
    repo_names: list[str],
) -> None:
    """Ensure a user's forum has tags matching their granted repos."""
    existing = {tag.name: tag for tag in forum.available_tags}
    desired = set(repo_names)
    current = set(existing.keys())

    # Also keep standard status tags
    standard_tags = {"active", "completed", "failed"}
    to_add = (desired | standard_tags) - current
    to_remove = current - desired - standard_tags

    if not to_add and not to_remove:
        return

    new_tags = [t for t in forum.available_tags if t.name not in to_remove]
    for name in to_add:
        emoji = None
        if name == "active":
            emoji = "\U0001f504"
        elif name == "completed":
            emoji = "\u2705"
        elif name == "failed":
            emoji = "\u274c"
        new_tags.append(discord.ForumTag(name=name, emoji=emoji))

    try:
        await forum.edit(available_tags=new_tags[:20])
        log.info("Synced user forum tags: %s (added=%s, removed=%s)",
                 forum.name, to_add, to_remove)
    except Exception:
        log.warning("Failed to sync user forum tags", exc_info=True)


def build_control_embed(
    repo_name: str,
    repo_path: str,
    branch: str | None = None,
    mode: str = "explore",
    active_count: int = 0,
    recent_completed: int = 0,
    recent_failed: int = 0,
) -> discord.Embed:
    """Build the embed for a repo control room post."""
    embed = discord.Embed(
        title=f"{repo_name} \u2014 Control Room",
        description=repo_path or "",
        color=discord.Color.dark_grey(),
    )
    if branch:
        embed.add_field(name="Branch", value=f"`{branch}`", inline=True)
    embed.add_field(name="Mode", value=mode_name(mode), inline=True)
    if active_count:
        embed.add_field(name="Active", value=str(active_count), inline=True)
    status_parts = []
    if recent_completed:
        status_parts.append(f"\u2705 {recent_completed}")
    if recent_failed:
        status_parts.append(f"\u274c {recent_failed}")
    if status_parts:
        embed.add_field(name="Recent", value=" ".join(status_parts), inline=True)
    return embed


async def create_repo_control_post(
    forum: discord.ForumChannel,
    repo_name: str,
    repo_path: str,
    branch: str | None = None,
    mode: str = "explore",
) -> tuple[discord.Thread, discord.Message]:
    """Create a control room post in a repo forum with action buttons."""
    embed = build_control_embed(repo_name, repo_path, branch, mode)

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="New Session",
        style=discord.ButtonStyle.green,
        custom_id=f"new_repo:{repo_name}",
    ))
    view.add_item(discord.ui.Button(
        label="Sync CLI",
        style=discord.ButtonStyle.secondary,
        custom_id=f"sync_repo:{repo_name}",
    ))

    result = await forum.create_thread(name="Control Room", embed=embed, view=view)
    try:
        await result.thread.edit(pinned=True)
    except Exception:
        log.debug("Could not pin control room thread", exc_info=True)

    log.info("Created control room post %s in forum %s", result.thread.id, forum.name)
    return result.thread, result.message


def build_user_control_embed(
    display_name: str,
    repo_names: list[str],
    mode: str = "explore",
) -> discord.Embed:
    """Build the embed for a user's personal control room post."""
    embed = discord.Embed(
        title=f"{display_name} \u2014 Control Room",
        color=discord.Color.dark_grey(),
    )
    if repo_names:
        embed.add_field(
            name="Repos",
            value="\n".join(f"\u2022 {r}" for r in repo_names),
            inline=True,
        )
    embed.add_field(name="Mode", value=mode_name(mode), inline=True)
    return embed


async def create_user_control_post(
    forum: discord.ForumChannel,
    display_name: str,
    repo_names: list[str],
    mode: str = "explore",
) -> tuple[discord.Thread, discord.Message]:
    """Create a control room post in a user's personal forum."""
    embed = build_user_control_embed(display_name, repo_names, mode)

    view = discord.ui.View(timeout=None)
    for rname in repo_names[:5]:
        view.add_item(discord.ui.Button(
            label=f"New: {rname}" if len(repo_names) > 1 else "New Session",
            style=discord.ButtonStyle.green,
            custom_id=f"new_repo:{rname}",
        ))

    result = await forum.create_thread(name="Control Room", embed=embed, view=view)
    try:
        await result.thread.edit(pinned=True)
    except Exception:
        log.debug("Could not pin user control room", exc_info=True)

    log.info("Created control room post %s in user forum %s", result.thread.id, forum.name)
    return result.thread, result.message


async def create_user_welcome_post(
    forum: discord.ForumChannel,
    display_name: str,
    repo_names: list[str],
) -> tuple[discord.Thread, discord.Message]:
    """Create a welcome post in a user's personal forum with a New Session button."""
    embed = discord.Embed(
        title=f"Welcome, {display_name}!",
        description=(
            "This is your personal workspace.\n\n"
            "**Start a session** using the button below, "
            "or create a post and start chatting directly."
        ),
        color=discord.Color.blurple(),
    )
    if repo_names:
        embed.add_field(
            name="Available Repos",
            value="\n".join(f"\u2022 {r}" for r in repo_names),
            inline=False,
        )

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="New Session",
        style=discord.ButtonStyle.green,
        custom_id="new:welcome",
    ))

    result = await forum.create_thread(name="Welcome", embed=embed, view=view)
    log.info("Created welcome post %s in user forum %s", result.thread.id, forum.name)
    return result.thread, result.message


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


