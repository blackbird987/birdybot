"""Thread/channel/forum management for Discord sessions."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import discord

from bot.engine.deploy import DeployState
from bot.platform.formatting import EFFORT_DISPLAY, MODE_COLOR, MODE_DISPLAY, effort_name, mode_name

log = logging.getLogger(__name__)

CONTROL_ROOM_NAME = "⚙️ Control Room"


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


async def ensure_archive_channel(
    guild: discord.Guild,
    category: discord.CategoryChannel,
    repo_name: str,
) -> discord.TextChannel:
    """Find or create an archive text channel for a repo (inherits perms)."""
    name = f"archive-{sanitize_channel_name(repo_name)}"

    for ch in category.text_channels:
        if ch.name == name:
            log.info("Found existing archive channel %s (%s)", ch.id, ch.name)
            return ch

    channel = await guild.create_text_channel(name, category=category)
    log.info("Created archive channel %s (%s)", channel.id, channel.name)
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


# --- Thread name helpers ---

# Legacy emoji prefixes to strip from old thread names
_LEGACY_PREFIXES = ("\U0001f504", "\u26aa", "\U0001f535", "\U0001f7e2")  # 🔄 ⚪ 🔵 🟢

_SLEEP_EMOJI = "\U0001f4a4"  # 💤


def parse_thread_name(name: str) -> tuple[bool, str]:
    """Parse thread name -> (is_sleeping, base_topic)."""
    # Strip legacy prefixes (🔄, mode circles)
    for legacy in _LEGACY_PREFIXES:
        if name.startswith(legacy):
            name = name[len(legacy):].lstrip()
    # Strip sleep prefix
    is_sleeping = False
    if name.startswith(_SLEEP_EMOJI):
        is_sleeping = True
        name = name[len(_SLEEP_EMOJI):].lstrip()
        if name.startswith("|"):
            name = name[1:].lstrip()
    return is_sleeping, name


def build_thread_name(topic: str) -> str:
    """Build thread name, max 100 chars."""
    return topic[:100]


def build_sleeping_thread_name(topic: str) -> str:
    """Build thread name with sleep indicator: 💤 | {topic}, max 100 chars."""
    return f"{_SLEEP_EMOJI} | {topic}"[:100]


def build_title_name(text: str) -> str:
    """Build a readable forum post name from LLM-generated title.

    Unlike sanitize_channel_name(), preserves casing and allows spaces
    for better readability (Discord forum posts support mixed case).
    """
    name = re.sub(r"[^a-zA-Z0-9\s\-]", "", text)
    name = re.sub(r"\s+", " ", name).strip()
    # Keep at most 6 words for concise forum post names
    words = name.split()
    if len(words) > 6:
        name = " ".join(words[:6])
    return name[:60] or "session"


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


def session_controls_view(
    current_mode: str = "explore",
    current_effort: str = "high",
) -> discord.ui.View:
    """Build a persistent View with mode + effort buttons for session embeds.

    Active mode/effort button is disabled (standard "already selected" UX).
    """
    view = discord.ui.View(timeout=None)
    # Row 0: Mode buttons
    for mode, name in MODE_DISPLAY.items():
        btn = discord.ui.Button(
            label=name,
            style=_MODE_BUTTON_STYLE.get(mode, discord.ButtonStyle.secondary),
            custom_id=f"mode_set:{mode}",
            disabled=(mode == current_mode),
            row=0,
        )
        view.add_item(btn)
    # Row 1: Effort buttons
    for level in EFFORT_DISPLAY:
        btn = discord.ui.Button(
            label=effort_name(level),
            style=discord.ButtonStyle.primary if level == current_effort else discord.ButtonStyle.secondary,
            custom_id=f"effort_set:{level}",
            disabled=(level == current_effort),
            row=1,
        )
        view.add_item(btn)
    return view


async def create_forum_post(
    forum: discord.ForumChannel,
    name: str,
    origin: str = "bot",
    topic_preview: str = "",
    current_mode: str = "explore",
    current_effort: str = "high",
) -> tuple[discord.Thread, discord.Message]:
    """Create a new forum post (thread + starter message).

    Returns (thread, starter_message).
    """
    name = build_thread_name(name)

    embed = discord.Embed(
        title="Session",
        description=topic_preview[:200] or "New session",
        color=discord.Color(MODE_COLOR.get(current_mode, 0x5865F2)),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Origin", value=origin, inline=True)
    embed.add_field(name="Mode", value=mode_name(current_mode), inline=True)
    embed.add_field(name="Effort", value=effort_name(current_effort), inline=True)

    view = session_controls_view(current_mode, current_effort)
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
    # Mode tags (explore/plan/build)
    for mode in MODE_DISPLAY:
        desired[mode] = None
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


def _truncate_field(value: str, limit: int = 1024) -> str:
    """Truncate a field value to Discord's 1024-char limit."""
    if len(value) > limit:
        return value[:limit - 3] + "..."
    return value


def _format_instance_line(
    inst,
    session_to_thread: dict[str, str] | None = None,
    show_elapsed: bool = False,
) -> str:
    """Format a single instance as a one-line summary for embed fields."""
    line = f"`{inst.display_id()}` \u2014 {inst.prompt[:30]}"
    if inst.user_name and not inst.is_owner_session:
        line += f" [{inst.user_name}]"
    if session_to_thread:
        thread_id = session_to_thread.get(inst.session_id or "")
        if thread_id:
            line += f" \u2022 <#{thread_id}>"
    if show_elapsed and inst.created_at:
        try:
            started = datetime.fromisoformat(inst.created_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = datetime.now(timezone.utc) - started
            mins = int(elapsed.total_seconds() // 60)
            line += f" \u2022 {'<1' if mins < 1 else str(mins)}m"
        except (ValueError, TypeError):
            pass
    return line


def build_control_embed(
    repo_name: str,
    repo_path: str,
    branch: str | None = None,
    *,
    running_instances: list | None = None,
    attention_instances: list | None = None,
    completed_instances: list | None = None,
    session_to_thread: dict[str, str] | None = None,
    today_cost: float = 0.0,
    deploy_state: DeployState | None = None,
    deploy_thread_ids: dict[str, str] | None = None,
    usage_bar: str | None = None,
) -> discord.Embed:
    """Build the embed for a repo control room post.

    Instance lists are optional — when omitted (initial creation),
    the embed shows just branch. When provided (refresh),
    it shows full dashboard data for this repo.
    """
    running_instances = running_instances or []
    attention_instances = attention_instances or []
    completed_instances = completed_instances or []
    active_count = len(running_instances)

    embed = discord.Embed(
        title=f"{repo_name} \u2014 {CONTROL_ROOM_NAME}",
        description=repo_path or "",
        color=discord.Color.dark_grey(),
    )

    # Needs Attention section (top priority)
    if attention_instances:
        attn_lines = []
        for inst in attention_instances[:10]:
            icon = "\u2753" if inst.needs_input else "\u274c"
            line = f"{icon} " + _format_instance_line(inst, session_to_thread)
            attn_lines.append(line)
        embed.add_field(
            name=f"Needs Attention ({len(attention_instances)})",
            value=_truncate_field("\n".join(attn_lines)),
            inline=False,
        )

    # Running instances
    if running_instances:
        run_lines = [
            _format_instance_line(inst, session_to_thread, show_elapsed=True)
            for inst in running_instances
        ]
        embed.add_field(
            name=f"Running ({active_count})",
            value=_truncate_field("\n".join(run_lines)),
            inline=False,
        )

    # Recently Completed
    if completed_instances:
        comp_lines = [
            f"\u2705 " + _format_instance_line(inst, session_to_thread)
            for inst in completed_instances
        ]
        embed.add_field(
            name=f"Recently Completed ({len(completed_instances)})",
            value=_truncate_field("\n".join(comp_lines)),
            inline=False,
        )

    # Inline summary fields
    if branch:
        embed.add_field(name="Branch", value=f"`{branch}`", inline=True)

    # Deploy state section
    if deploy_state and deploy_state.needs_reboot:
        version_line = ""
        if deploy_state.boot_version and deploy_state.current_version:
            version_line = f"`{deploy_state.boot_version}` \u2192 `{deploy_state.current_version}`\n"

        changes = deploy_state.pending_changes[:5]
        change_lines = "\n".join(f"\u2022 {c}" for c in changes)
        if len(deploy_state.pending_changes) > 5:
            change_lines += f"\n\u2026 and {len(deploy_state.pending_changes) - 5} more"

        session_links = ""
        if deploy_state.pending_sessions and deploy_thread_ids:
            links = [f"<#{deploy_thread_ids[s]}>" for s in deploy_state.pending_sessions
                     if s in deploy_thread_ids]
            if links:
                session_links = "\n\U0001f4ce " + " \u00b7 ".join(links)

        value = f"{version_line}{change_lines}{session_links}".strip()
        if not value:
            value = "Changes detected"
        value = _truncate_field(value)

        label = "\U0001f504 Reboot Required" if deploy_state.self_managed else "\U0001f504 Redeploy Required"
        embed.add_field(name=label, value=value, inline=False)
    elif deploy_state and not deploy_state.needs_reboot:
        v = deploy_state.boot_version or "unknown"
        embed.add_field(name="\u2705 Up to date", value=f"`{v}`", inline=True)

    if usage_bar:
        embed.add_field(name="Usage", value=usage_bar, inline=False)
    elif today_cost > 0:
        embed.add_field(name="Today", value=f"${today_cost:.4f}", inline=True)

    return embed


def build_control_view(
    repo_name: str,
    active_count: int = 0,
    deploy_state: DeployState | None = None,
    deploy_config: dict | None = None,
) -> discord.ui.View:
    """Build the button view for a repo control room post."""
    view = discord.ui.View(timeout=None)
    # Row 0: New Session + Resume
    view.add_item(discord.ui.Button(
        label="New Session",
        style=discord.ButtonStyle.green,
        custom_id=f"new_repo:{repo_name}",
        row=0,
    ))
    view.add_item(discord.ui.Button(
        label="Resume",
        style=discord.ButtonStyle.primary,
        custom_id=f"resume_latest:{repo_name}",
        row=0,
    ))
    # Row 1: Quick Task + Sync CLI
    view.add_item(discord.ui.Button(
        label="Quick Task",
        style=discord.ButtonStyle.primary,
        custom_id=f"quick_task:{repo_name}",
        row=1,
    ))
    view.add_item(discord.ui.Button(
        label="Sync CLI",
        style=discord.ButtonStyle.secondary,
        custom_id=f"sync_repo:{repo_name}",
        row=1,
    ))
    # Row 2: Deploy/Reboot button + Stop All + Refresh
    needs_reboot = deploy_state.needs_reboot if deploy_state else False
    if deploy_config:
        if deploy_config.get("approved"):
            # Active Reboot/Deploy button
            label = deploy_config.get("label", "Reboot")
            style = discord.ButtonStyle.danger if needs_reboot else discord.ButtonStyle.secondary
            view.add_item(discord.ui.Button(
                label=label, style=style,
                custom_id=f"reboot_repo:{repo_name}",
                emoji="\U0001f504" if needs_reboot else None,
                row=3,
            ))
        else:
            # Unapproved — show approval button
            cmd = deploy_config.get("command", "?")
            view.add_item(discord.ui.Button(
                label=f"Approve: {cmd[:30]}",
                style=discord.ButtonStyle.primary,
                custom_id=f"approve_deploy:{repo_name}",
                row=2,
            ))
    overflow_row = 2 if not deploy_config else 3
    if active_count > 0:
        view.add_item(discord.ui.Button(
            label=f"Stop All ({active_count})",
            style=discord.ButtonStyle.danger,
            custom_id=f"stop_all:{repo_name}",
            row=overflow_row,
        ))
    view.add_item(discord.ui.Button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        custom_id=f"refresh_control:{repo_name}",
        row=overflow_row,
    ))
    return view


async def create_repo_control_post(
    forum: discord.ForumChannel,
    repo_name: str,
    repo_path: str,
    branch: str | None = None,
    usage_bar: str | None = None,
) -> tuple[discord.Thread, discord.Message]:
    """Create a control room post in a repo forum with action buttons."""
    embed = build_control_embed(repo_name, repo_path, branch, usage_bar=usage_bar)
    view = build_control_view(repo_name, active_count=0)

    result = await forum.create_thread(name=CONTROL_ROOM_NAME, embed=embed, view=view)
    try:
        await result.thread.edit(pinned=True)
    except Exception:
        log.debug("Could not pin control room thread", exc_info=True)

    log.info("Created control room post %s in forum %s", result.thread.id, forum.name)
    return result.thread, result.message


def build_user_control_embed(
    display_name: str,
    repo_names: list[str],
) -> discord.Embed:
    """Build the embed for a user's personal control room post."""
    embed = discord.Embed(
        title=f"{display_name} \u2014 {CONTROL_ROOM_NAME}",
        color=discord.Color.dark_grey(),
    )
    if repo_names:
        embed.add_field(
            name="Repos",
            value="\n".join(f"\u2022 {r}" for r in repo_names),
            inline=True,
        )
    return embed


def build_user_control_view(
    repo_names: list[str],
) -> discord.ui.View:
    """Build the button view for a user's personal control room post."""
    view = discord.ui.View(timeout=None)
    # Row 0: New Session per repo (up to 5)
    for rname in repo_names[:5]:
        view.add_item(discord.ui.Button(
            label=f"New: {rname}" if len(repo_names) > 1 else "New Session",
            style=discord.ButtonStyle.green,
            custom_id=f"new_repo:{rname}",
            row=0,
        ))
    # Row 1: Refresh
    scope = repo_names[0] if repo_names else "_default"
    view.add_item(discord.ui.Button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        custom_id=f"refresh_user_control:{scope}",
        row=1,
    ))
    return view


async def create_user_control_post(
    forum: discord.ForumChannel,
    display_name: str,
    repo_names: list[str],
) -> tuple[discord.Thread, discord.Message]:
    """Create a control room post in a user's personal forum."""
    embed = build_user_control_embed(display_name, repo_names)
    view = build_user_control_view(repo_names)

    result = await forum.create_thread(name=CONTROL_ROOM_NAME, embed=embed, view=view)
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


