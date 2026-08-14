"""Discord bot with slash commands, message handler, and persistent views.

Forum-based architecture: one ForumChannel per project, one thread per session.
Delegates forum/thread management to ForumManager (bot.discord.forums).

Extracted modules:
- slash_commands.py — slash command registration (~720 lines)
- interactions.py — button/select/modal dispatch (~500 lines)
- idle.py — thread sleep/wake timer management
- tags.py — forum tag management
- monitoring.py — monitor service lifecycle
- modals.py — QuickTaskModal
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time as _time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from bot import config
from bot.discord import channels
from bot.discord import access as access_mod
from bot.discord.access import AccessResult, load_access_config, check_user_access, has_any_access, get_most_restrictive_ceiling, effective_mode as access_effective_mode
from bot.discord.adapter import DiscordMessenger
from bot.discord import dashboard as dashboard_mod
from bot.discord import idle as idle_mod
from bot.discord import interactions as interactions_mod
from bot.discord import monitoring as monitoring_mod
from bot.discord import slash_commands as slash_commands_mod
from bot.discord import spawn_colors
from bot.discord import tags as tags_mod
from bot.discord.forums import ForumManager, ThreadInfo
from bot.discord.titles import generate_title_text, read_ai_title
from bot.engine import commands
from bot.platform.base import RequestContext, SpawnArgs, SpawnResult
from bot.services.twitter import enrich_with_tweets

if TYPE_CHECKING:
    from bot.claude.runner import ClaudeRunner
    from bot.monitor.service import MonitorService
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


def _parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string, returning None on any failure."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def unlink_image_paths(paths: list[str], *, site: str) -> int:
    """Best-effort delete each path; log a single line with the count.

    Only counts files that actually existed and got removed — already-missing
    paths return cleanly without inflating the count.

    One caller left: a cancelled queue entry.  That prompt never ran, so no
    session transcript can be holding the path and deleting now is safe.
    Every other site that used to call this deleted files a live run still
    needed — see ``reap_pending_images``.
    """
    deleted = 0
    for p in paths or []:
        try:
            Path(p).unlink()
            deleted += 1
        except FileNotFoundError:
            pass
        except Exception:
            pass
    if deleted:
        log.info("Image cleanup [%s]: deleted %d files", site, deleted)
    return deleted


def reap_pending_images(
    referenced: set[str],
    now: float,
    *,
    ttl_hours: float | None = None,
    max_bytes: int | None = None,
    min_age_secs: int | None = None,
) -> tuple[int, int]:
    """Delete stale uploads from PENDING_IMAGES_DIR. Returns (aged, evicted).

    Two exemptions, then two rules:

    - A file a live usage-queue entry still points at is never touched — that
      prompt hasn't run yet and the path is the only copy it has.
    - Nothing younger than ``min_age_secs`` is ever deleted, by any rule.
      That floor is the in-flight guarantee: a run reading a picture it was
      just handed cannot lose it to a burst of uploads in another thread, and
      making it outrank both rules means a mistyped TTL can't quietly revoke
      it either.

    1. Past the retention window it goes.  The window is generous on purpose:
       the path we handed the session is in its transcript forever, so a steer,
       a retry, or a later "look at that screenshot again" turn can still read
       it.  Deleting on the receiving turn's exit is exactly what made uploads
       vanish out from under a steered run one second before it started.
    2. If the folder is still over the size cap, evict oldest-first until it
       fits.  The cap governs reapable files only: a queued prompt's uploads
       are exempt above and don't count toward it, since evicting other
       people's files wouldn't reclaim them anyway.

    Blocking (stat/unlink); the caller runs it off the event loop.
    """
    ttl = config.PENDING_IMAGES_TTL_HOURS if ttl_hours is None else ttl_hours
    cap = config.PENDING_IMAGES_MAX_BYTES if max_bytes is None else max_bytes
    floor = (
        config.PENDING_IMAGES_MIN_AGE_SECS if min_age_secs is None else min_age_secs
    )

    survivors: list[tuple[float, int, Path]] = []  # (mtime, size, path)
    aged = 0
    try:
        entries = list(config.PENDING_IMAGES_DIR.iterdir())
    except OSError:
        return 0, 0

    for f in entries:
        try:
            if not f.is_file():
                continue
            if str(f.resolve()) in referenced:
                continue
            st = f.stat()
            age = now - st.st_mtime
            # Too young to touch, but its bytes are still on the disk, so it
            # counts toward the cap below (which skips it for the same reason).
            if age >= floor and age > ttl * 3600:
                f.unlink(missing_ok=True)
                aged += 1
            else:
                survivors.append((st.st_mtime, st.st_size, f))
        except OSError:
            continue

    evicted = 0
    total = sum(size for _, size, _ in survivors)
    if total > cap:
        survivors.sort(key=lambda t: t[0])  # oldest first
        for mtime, size, f in survivors:
            if total <= cap:
                break
            if now - mtime < floor:
                continue  # too young to reap, cap or no cap
            try:
                f.unlink(missing_ok=True)
            except OSError:
                continue
            total -= size
            evicted += 1
        if total > cap:
            log.warning(
                "Image cleanup [sweep]: still %d bytes over cap after evicting "
                "%d — the rest is inside the %ds floor or wouldn't delete",
                total - cap, evicted, floor,
            )

    if aged or evicted:
        log.info(
            "Image cleanup [sweep]: deleted %d past retention, evicted %d for size",
            aged, evicted,
        )
    return aged, evicted


def _strip_missing_image_refs(prompt: str, image_paths: list[str]) -> str:
    """Drop "saved at `<path>`" / "screenshot at `<path>`" clauses for paths
    that no longer exist on disk.  Falls back to noop if every file is intact.

    A queue entry shouldn't be able to outlive its image files any more — the
    reaper exempts anything the queue still references, and the retention
    clock restarts at the pop — so this is the belt to that pair of braces,
    covering what the bot doesn't control: a file deleted by hand, a wiped
    data dir, a restore from a backup taken before the upload.  Without the
    scrub the replay hands Claude a path whose Read fails silently; better to
    drop the path and let it answer whatever text remains.
    """
    if not image_paths:
        return prompt
    missing = [p for p in image_paths if not Path(p).exists()]
    if not missing:
        return prompt
    cleaned = prompt
    for p in missing:
        esc = re.escape(p)
        cleaned = re.sub(r"\s*saved at `" + esc + r"`", "", cleaned)
        cleaned = re.sub(
            r"Analyze this screenshot at `" + esc + r"`\.\s*",
            "[image no longer available] ",
            cleaned,
        )
    log.warning(
        "Replay: %d image file(s) missing for queued prompt — stripped path refs: %s",
        len(missing), missing,
    )
    return cleaned


def refresh_image_retention(paths: list[str]) -> None:
    """Restart the retention clock on uploads that are about to be needed.

    The reaper dates a file by its mtime, which for an upload means "when it
    arrived".  That is the wrong clock for anything that sat in the usage
    queue: a weekly limit can hold a prompt for days, and the queue exemption
    ends the instant the entry is popped — so its images can be past the
    retention window before the run they were queued for has read a byte, and
    the next sweep takes them out from under it.  Touching them makes mtime
    mean "last needed by a run" instead of "uploaded at", which is the
    lifetime that matters, and re-arms the young-file floor for the read.

    Called both where the exemption ends (the pop) and at the handoff itself,
    because a batch of due entries replays one at a time and the last one's
    files would otherwise be unprotected for the length of every run ahead
    of it.
    """
    for p in paths or []:
        try:
            os.utime(p, None)
        except OSError:
            pass  # gone already, or not ours to touch — reads degrade to the
            # path-strip, which is the same outcome as before this existed


def prepare_replayed_prompt(prompt: str, image_paths: list[str]) -> str:
    """Ready a queued prompt and its uploads for the run about to get them.

    Public because interactions.py calls it too: the timer path and the Run
    Now button need an identical handoff, and the cost of a third call site
    getting it wrong is an upload deleted while a run is reading it.
    """
    refresh_image_retention(image_paths)
    return _strip_missing_image_refs(prompt, image_paths)


# --- Attachment handling ---------------------------------------------------
# What the message handler accepts and the size ceiling for each family.
# Module-level so the user-facing rejection notice can quote the same limits
# the loop enforces instead of duplicating them.
ATTACH_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
ATTACH_AUDIO_EXTS = {".ogg", ".mp3", ".wav", ".m4a", ".webm"}
# Inlined straight into the prompt — anything that is plain text underneath
# and small enough to paste.
ATTACH_TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml"}
# Saved to disk and referenced by path: the session's own Read tool handles
# PDFs natively (extracted text *and* a rendered page image, plus page
# ranges), which beats anything we could extract here — and costs no new
# dependency.  Verified against a real PDF before choosing this route.
ATTACH_DOC_EXTS = {".pdf"}
# Read refuses .docx as a binary file (also verified), so Word has to be
# extracted locally — see _extract_docx_text.
ATTACH_WORD_EXTS = {".docx"}

ATTACH_AUDIO_MAX = 25_000_000
ATTACH_IMAGE_MAX = 10_000_000
ATTACH_TEXT_MAX = 500_000
ATTACH_DOC_MAX = 20_000_000

ATTACH_ACCEPTED_SENTENCE = (
    "I can read PDFs, Word documents, images, plain-text files "
    "(including Markdown, CSV, JSON and logs) and voice notes."
)


def _human_size(n: int) -> str:
    """Rough, readable size for a user-facing sentence — not for arithmetic."""
    if n >= 10_000_000:
        return f"{n / 1_000_000:.0f} MB"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    return f"{max(1, round(n / 1000))} KB"


def _attachment_reject_note(
    filename: str, ext: str, size: int, *, voice_enabled: bool = True,
) -> str:
    """Plain-language reason an attachment couldn't be taken.

    Written for a non-technical reader: name the file, say what went wrong,
    and say what would work instead.  Never leaks a traceback or a bare list
    of extensions.
    """
    if ext in ATTACH_AUDIO_EXTS and not voice_enabled:
        return (
            f"I couldn't listen to **{filename}** — voice transcription isn't "
            "switched on for this bot right now."
        )
    families = (
        ("voice note", ATTACH_AUDIO_EXTS, ATTACH_AUDIO_MAX),
        ("image", ATTACH_IMAGE_EXTS, ATTACH_IMAGE_MAX),
        ("text file", ATTACH_TEXT_EXTS, ATTACH_TEXT_MAX),
        ("document", ATTACH_DOC_EXTS | ATTACH_WORD_EXTS, ATTACH_DOC_MAX),
    )
    for label, exts, cap in families:
        if ext in exts:
            return (
                f"**{filename}** is too big for me to open — it's "
                f"{_human_size(size)}, and the most I can take for a "
                f"{label} is {_human_size(cap)}."
            )
    return f"I can't read **{filename}**. {ATTACH_ACCEPTED_SENTENCE}"


# A .docx is a zip; guard against one that unpacks to something enormous.
_DOCX_XML_MAX = 10_000_000
_DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _extract_docx_text(data: bytes) -> str:
    """Pull the visible text out of a .docx.

    A .docx is a zip whose ``word/document.xml`` holds the body; every run of
    visible text is a ``<w:t>`` node and paragraphs (``<w:p>``) are the line
    breaks.  Table cells are built from the same nodes, so a form laid out as
    a table comes through as well.

    Done with the standard library on purpose rather than python-docx: the
    point of this feature is that a non-technical user can drop a Word file
    in and have it work, and a lazy-imported dependency that isn't installed
    would degrade straight back to "I can't read that".  zipfile keeps the
    bot dependency-free so it works on a fresh checkout.  If layout fidelity
    ever matters more than that, python-docx is a drop-in swap here.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        info = z.getinfo("word/document.xml")
        if info.file_size > _DOCX_XML_MAX:
            raise ValueError(f"document.xml too large ({info.file_size} bytes)")
        xml = z.read("word/document.xml")
    # ElementTree will happily expand entity declarations; a hostile file can
    # use that to blow up memory.  We only ever want plain markup here.
    if b"<!ENTITY" in xml or b"<!DOCTYPE" in xml:
        raise ValueError("document.xml declares entities")
    root = ET.fromstring(xml)
    lines: list[str] = []
    for para in root.iter(f"{_DOCX_NS}p"):
        parts: list[str] = []
        for node in para.iter():
            if node.tag == f"{_DOCX_NS}t":
                parts.append(node.text or "")
            elif node.tag == f"{_DOCX_NS}tab":
                parts.append("\t")
            elif node.tag == f"{_DOCX_NS}br":
                parts.append("\n")
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


class _AutoDeleteMessenger:
    """Wraps a Messenger to auto-delete messages sent to a specific channel."""

    def __init__(self, inner, lobby_channel_id: str, ttl: float = 10) -> None:
        self._inner = inner
        self._lobby_id = lobby_channel_id
        self._ttl = ttl

    async def send_text(self, channel_id, text, buttons=None, silent=False):
        msg_id = await self._inner.send_text(channel_id, text, buttons, silent)
        if channel_id == self._lobby_id and msg_id:
            asyncio.create_task(self._delete_after(channel_id, msg_id))
        return msg_id

    async def _delete_after(self, channel_id: str, msg_id: str) -> None:
        try:
            await asyncio.sleep(self._ttl)
            await self._inner.delete_message(channel_id, msg_id)
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._inner, name)


class ClaudeBot(discord.Client):
    """Discord bot for Claude Code instance management."""

    def __init__(
        self,
        store: StateStore,
        runner: ClaudeRunner,
        guild_id: int,
        lobby_channel_id: int | None = None,
        category_id: int | None = None,
        category_name: str | None = None,
        discord_user_id: int | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True  # needed to resolve owner member for permissions

        super().__init__(intents=intents)

        self._store = store
        self._runner = runner
        self._guild_id = guild_id
        self._lobby_channel_id = lobby_channel_id
        self._category_id = category_id
        self._category_name = category_name
        self._discord_user_id = discord_user_id
        self._ready_event = asyncio.Event()

        self.tree = app_commands.CommandTree(self)
        self._messenger: DiscordMessenger | None = None

        # Forum manager — owns all forum/thread data and operations
        self._forums = ForumManager(
            client=self,
            store=store,
            guild_id=guild_id,
            category_id=category_id,
            discord_user_id=discord_user_id,
        )

        self._name_editing: set[str] = set()  # thread IDs with a name edit in-flight
        self._dashboard_lock = asyncio.Lock()  # Serializes dashboard refreshes
        self._dashboard_pending_flag = [False]  # Mutable flag for dashboard_mod
        self._idle_timers: dict[str, asyncio.TimerHandle] = {}  # channel_id -> scheduled sleep
        self._sleep_gen: dict[str, int] = {}  # generation counter per channel (stale-callback guard)
        # Pending /ref context: {thread_id: (context_str, unix_epoch_seconds)}
        # Consumed by on_message, expires after 10 minutes. Persisted to
        # platform_state so a reboot mid-window doesn't drop the user's
        # loaded context (10-min TTL would otherwise be lost in seconds).
        self._pending_refs: dict[str, tuple[str, float]] = {}
        self._load_pending_refs()

        self._monitor_service: MonitorService | None = None
        self._monitor_started: bool = False
        self._notifier = None  # set by app.py after notifier is created
        self._voice_enabled: bool = bool(config.OPENAI_API_KEY)
        # Serializes all read/modify/write ops on config.USAGE_QUEUE_FILE.
        self._usage_queue_lock = asyncio.Lock()
        # channel_id -> window_end_utc; bypass the usage-limit gate while now < end.
        # In-memory by design — a reboot mid-window means one more prompt then quiet.
        self._usage_gate_bypass: dict[str, datetime] = {}
        # Debounce for the upload reaper — see _schedule_pending_image_sweep.
        self._last_image_sweep: float = 0.0
        self._setup_commands()

    @property
    def messenger(self) -> DiscordMessenger:
        if self._messenger is None:
            self._messenger = DiscordMessenger(
                bot=self,
                guild_id=self._guild_id,
                lobby_channel_id=self._lobby_channel_id,
                category_id=self._category_id,
            )
        return self._messenger

    def _is_owner(self, user_id: int) -> bool:
        """Check if user is the bot owner."""
        if self._discord_user_id:
            return user_id == self._discord_user_id
        return True

    # --- Pending /ref persistence ---

    _PENDING_REFS_TTL_SECONDS = 600

    def _load_pending_refs(self) -> None:
        """Restore pending /ref context from platform_state (drops expired)."""
        try:
            state = self._store.get_platform_state("discord")
            raw = state.get("pending_refs", {}) or {}
            if not isinstance(raw, dict):
                return
            now = _time.time()
            for tid, entry in raw.items():
                # Tuples become lists in JSON; accept either for forward-compat.
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    continue
                text, ts = entry
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    continue
                if (now - ts) >= self._PENDING_REFS_TTL_SECONDS:
                    continue
                self._pending_refs[str(tid)] = (str(text), ts)
            if self._pending_refs:
                log.info("Restored %d pending /ref entries", len(self._pending_refs))
        except Exception:
            log.debug("Failed to load pending_refs", exc_info=True)

    def _save_pending_refs(self) -> None:
        """Persist pending /ref context to platform_state."""
        try:
            state = self._store.get_platform_state("discord")
            state["pending_refs"] = {
                tid: [text, ts] for tid, (text, ts) in self._pending_refs.items()
            }
            self._store.set_platform_state("discord", state, persist=True)
        except Exception:
            log.debug("Failed to save pending_refs", exc_info=True)

    def _check_access(
        self, user_id: int, *,
        repo_name: str | None = None,
        channel_id: str | None = None,
    ) -> AccessResult:
        """Check if a user has access. Owner always passes."""
        if self._is_owner(user_id):
            return AccessResult(allowed=True, is_owner=True)

        cfg = load_access_config()

        # Resolve repo from channel if not explicit
        if not repo_name and channel_id:
            lookup = self._forums.thread_to_project(channel_id)
            if lookup:
                repo_name = lookup[0].repo_name
            else:
                repo_name = self._resolve_repo_from_user_forum(channel_id, str(user_id))

        grant = check_user_access(cfg, str(user_id), repo_name)
        if grant:
            return AccessResult(
                allowed=True, is_owner=False,
                mode_ceiling=grant.mode,
                bash_policy=grant.bash_policy,
                max_daily_queries=grant.max_daily_queries,
            )

        if has_any_access(cfg, str(user_id)):
            return AccessResult(
                allowed=True, is_owner=False,
                mode_ceiling=get_most_restrictive_ceiling(cfg, str(user_id)),
            )

        return AccessResult(allowed=False, is_owner=False, reason="No access grant")

    def _resolve_repo_from_user_forum(self, channel_id: str, user_id: str) -> str | None:
        """Resolve repo name from a thread in a user's personal forum via tags."""
        try:
            ch = self.get_channel(int(channel_id))
            if not isinstance(ch, discord.Thread):
                return None
            repos = self._store.list_repos()
            for tag in ch.applied_tags:
                if tag.name in repos:
                    return tag.name
        except Exception:
            pass
        return None

    def _ctx(
        self, channel_id: str,
        session_id: str | None = None,
        repo_name: str | None = None,
        thread_info: ThreadInfo | None = None,
        access_result: AccessResult | None = None,
        source: str = "system",
    ) -> RequestContext:
        ctx = RequestContext(
            messenger=self.messenger,
            channel_id=channel_id,
            platform="discord",
            store=self._store,
            runner=self._runner,
            session_id=session_id,
            repo_name=repo_name,
            source=source,
        )
        if thread_info:
            ctx.mode = thread_info.mode
            ctx.context = thread_info.context
            ctx.verbose_level = thread_info.verbose_level
            ctx.effort = thread_info.effort
            ctx.spawn_depth_inherit = thread_info.spawn_depth
        if access_result:
            ctx.is_owner = access_result.is_owner
            if not access_result.is_owner and access_result.mode_ceiling:
                ctx.mode_ceiling = access_result.mode_ceiling
                current = ctx.mode or self._store.mode
                ctx.mode = access_effective_mode(
                    access_mod.RepoAccess(mode=access_result.mode_ceiling),
                    current,
                )
            if not access_result.is_owner and access_result.bash_policy:
                ctx.bash_policy = access_result.bash_policy
            if not access_result.is_owner and access_result.max_daily_queries > 0:
                ctx.max_daily_queries = access_result.max_daily_queries
                def _make_rate_callbacks(uid, rn, max_q):
                    def _check():
                        from bot.discord.access import load_access_config, check_rate_limit
                        return check_rate_limit(load_access_config(), uid, max_q)
                    def _increment():
                        from bot.discord.access import load_access_config, increment_query_count
                        increment_query_count(load_access_config(), uid)
                    return _check, _increment
                ctx.check_rate_limit, ctx.increment_query_count = _make_rate_callbacks(
                    ctx.user_id or "", ctx.repo_name, access_result.max_daily_queries,
                )
        # Wire up merged-tag callback (covers all ctx creation paths)
        _cid = channel_id
        _bot = self

        async def _apply_merged_tag():
            ch = _bot.get_channel(int(_cid))
            if isinstance(ch, discord.Thread):
                from bot.discord.tags import apply_thread_tags
                await apply_thread_tags(ch, "completed", merged=True)

        ctx.on_merged = _apply_merged_tag
        ctx.offer_usage_limit_choice = self._offer_usage_limit_choice

        # [BOT_CMD: /spawn] — engine calls this when a directive in the
        # assistant's final response asks the bot to hand off a generated
        # prompt to a fresh thread. We create the thread, build a child
        # ctx for it, stamp spawn_depth, and dispatch through on_text.
        async def _spawn_session(args: SpawnArgs) -> SpawnResult:
            parent_id = ctx.channel_id
            parent_lookup = _bot._forums.thread_to_project(parent_id)
            parent_forum_project = parent_lookup[0] if parent_lookup else None

            # Spawn-color: assign a slot for this family (reusing the parent's
            # existing slot, the root's stamped slot for root-revival, or the
            # lowest unused). Rename the parent once to prepend the colored
            # square, and pass an explicit child name override carrying the dot.
            assigned_slot: int | None = None
            root_id: str | None = None
            child_name_override: str | None = None
            if parent_forum_project is not None:
                result = await spawn_colors.assign_slot(
                    parent_id,
                    parent_forum_project,
                    _bot._store,
                    _bot._forums,
                )
                if result is not None:
                    assigned_slot, root_id = result
                    sanitized = (
                        channels.build_channel_name(args.title)
                        if args.title and args.title != "new-session"
                        else "new-session"
                    )
                    child_name_override = spawn_colors.compose_for_slot(
                        assigned_slot, sanitized, is_root=False,
                    )

            # Rename the family root to prepend the square emoji. A /spawn
            # event implies activity, so we treat the rename as a wake: any
            # existing 💤 prefix is dropped, and only the color square remains.
            # Reuses the _name_editing guard to not race _generate_smart_title.
            if assigned_slot is not None and root_id is not None:
                square = spawn_colors.prefix_for_root(assigned_slot)
                root_channel = _bot.get_channel(int(root_id))
                if isinstance(root_channel, discord.Thread):
                    _is_sleeping, bare_topic = channels.parse_thread_name(root_channel.name)
                    new_root_name = f"{square} {bare_topic}"[:100]
                    if root_channel.name != new_root_name and root_id not in _bot._name_editing:
                        _bot._name_editing.add(root_id)
                        try:
                            await root_channel.edit(name=new_root_name)
                            # Activity implies awake — cancel any pending sleep
                            # timer so it doesn't immediately reapply 💤.
                            idle_mod.cancel_sleep(_bot, root_id)
                        except Exception:
                            log.warning(
                                "Failed to prepend spawn-color square to root thread %s",
                                root_id, exc_info=True,
                            )
                        finally:
                            _bot._name_editing.discard(root_id)

            thread = await _bot._forums.get_or_create_session_thread(
                args.repo,
                session_id=None,
                topic=args.title,
                origin="spawn",
                user_id=ctx.user_id or None,
                user_name=ctx.user_name or None,
                name_override=child_name_override,
            )
            if thread is None:
                raise RuntimeError("get_or_create_session_thread returned None")
            new_channel_id = str(thread.id)
            lookup = _bot._forums.thread_to_project(new_channel_id)
            new_info = lookup[1] if lookup else None
            if new_info is not None:
                new_info.spawn_depth = args.parent_depth + 1
                new_info.mode = args.mode
                if args.effort:
                    new_info.effort = args.effort
                # Orchestrator loop: record parent linkage on the child so
                # _maybe_callback_parent (in engine.lifecycle) can ping back
                # when this child finalizes.
                new_info.parent_thread_id = ctx.channel_id
                _bot._store.save()

            # Register the new child in its family. Done after parent_thread_id
            # is stamped so find_root sees the link.
            if assigned_slot is not None and root_id is not None and lookup is not None:
                child_forum_project = lookup[0]
                try:
                    await spawn_colors.register_member(
                        root_id, new_channel_id,
                        child_forum_project, _bot._store, _bot._forums,
                    )
                except Exception:
                    log.warning(
                        "Failed to register spawn-color member %s for root %s",
                        new_channel_id, root_id, exc_info=True,
                    )
            # Wave cap accounting: bump the parent's counter so the next
            # /spawn from the same orchestration run sees an incremented
            # value. Counter resets when the user types a fresh message
            # (see commands.on_text reset block). Reuses parent_lookup
            # from the spawn-color block above — same thread, same ref.
            if parent_lookup is not None:
                _, parent_info = parent_lookup
                parent_info.spawn_wave_count += 1
                _bot._store.save()
            new_ctx = _bot._ctx(
                new_channel_id,
                session_id=None,
                repo_name=args.repo,
                thread_info=new_info,
                source="spawn_dispatch",
            )
            # Inherit identity + access from the parent ctx so non-owner spawns
            # carry the same gating into the child thread.
            new_ctx.user_id = ctx.user_id
            new_ctx.user_name = ctx.user_name
            new_ctx.is_owner = ctx.is_owner
            new_ctx.mode_ceiling = ctx.mode_ceiling
            new_ctx.bash_policy = ctx.bash_policy
            new_ctx.max_daily_queries = ctx.max_daily_queries
            new_ctx.check_rate_limit = ctx.check_rate_limit
            new_ctx.increment_query_count = ctx.increment_query_count
            if new_info is not None:
                _bot._forums.attach_session_callbacks(new_ctx, new_info, new_channel_id)
            # Skip the "Reconstructing context…" priming pass on this dispatch:
            # a spawned thread has no prior conversation history that could
            # actually inform the run, and the generated prompt is meant to be
            # self-contained. Leaving it on adds a transient noise message in
            # the brand-new thread for no benefit.
            new_ctx.maybe_prime_briefing = None

            async def _dispatch():
                try:
                    await commands.on_text(new_ctx, args.prompt)
                    if new_info is not None:
                        _bot._forums.persist_ctx_settings(new_ctx)
                    asyncio.create_task(_bot._try_apply_tags_after_run(new_channel_id))
                except Exception:
                    log.exception("spawn dispatch failed in thread %s", new_channel_id)

            asyncio.create_task(_dispatch())
            mention = f"<#{new_channel_id}>"
            url = None
            if thread.guild is not None:
                url = f"https://discord.com/channels/{thread.guild.id}/{new_channel_id}"
            return SpawnResult(
                thread_id=new_channel_id,
                thread_mention=mention,
                thread_url=url,
            )

        ctx.spawn_session = _spawn_session

        # Orchestrator seam: when a spawned child finalizes (COMPLETED/FAILED),
        # post a callback into the recorded parent thread with a Resume button.
        # Closure captures (_bot, channel_id) — the engine just calls it.
        async def _notify_parent(status: str, summary: str) -> None:
            from bot.discord.orchestrator import post_parent_callback
            await post_parent_callback(_bot, channel_id, status, summary)

        ctx.notify_parent_on_finalize = _notify_parent

        # Orchestrator wave-cap accessors. Both look up the thread's ForumProject
        # entry on demand so they reflect the latest state.json contents and
        # never operate on a stale snapshot.
        def _read_wave_count() -> int:
            lookup = _bot._forums.thread_to_project(channel_id)
            if lookup is None:
                return 0
            return lookup[1].spawn_wave_count

        def _reset_wave_count() -> None:
            lookup = _bot._forums.thread_to_project(channel_id)
            if lookup is None:
                return
            info = lookup[1]
            if info.spawn_wave_count != 0:
                info.spawn_wave_count = 0
                _bot._store.save()

        ctx.read_spawn_wave_count = _read_wave_count
        ctx.reset_spawn_wave_count = _reset_wave_count

        # Self-wake runaway-cap accessors — same on-demand lookup so they track
        # the latest state.json. bump increments + persists; reset zeroes it.
        def _bump_wake_count() -> int:
            lookup = _bot._forums.thread_to_project(channel_id)
            if lookup is None:
                return 0
            info = lookup[1]
            info.wake_count += 1
            _bot._store.save()
            return info.wake_count

        def _reset_wake_count() -> None:
            lookup = _bot._forums.thread_to_project(channel_id)
            if lookup is None:
                return
            info = lookup[1]
            if info.wake_count != 0:
                info.wake_count = 0
                _bot._store.save()

        ctx.bump_wake_count = _bump_wake_count
        ctx.reset_wake_count = _reset_wake_count

        # Unattended-turn nudge-cap accessors — same on-demand lookup pattern.
        def _bump_nudge_count() -> int:
            lookup = _bot._forums.thread_to_project(channel_id)
            if lookup is None:
                return 0
            info = lookup[1]
            info.nudge_count += 1
            _bot._store.save()
            return info.nudge_count

        def _reset_nudge_count() -> None:
            lookup = _bot._forums.thread_to_project(channel_id)
            if lookup is None:
                return
            info = lookup[1]
            if info.nudge_count != 0:
                info.nudge_count = 0
                _bot._store.save()

        ctx.bump_nudge_count = _bump_nudge_count
        ctx.reset_nudge_count = _reset_nudge_count
        return ctx

    # --- Delegation to extracted modules ---

    def _schedule_sleep(self, channel_id: str) -> None:
        idle_mod.schedule_sleep(self, channel_id)

    def _cancel_sleep(self, channel_id: str) -> None:
        idle_mod.cancel_sleep(self, channel_id)

    async def _set_thread_sleeping(self, channel) -> None:
        await idle_mod.set_thread_sleeping(self, channel)

    async def _clear_thread_sleeping(self, channel) -> None:
        await idle_mod.clear_thread_sleeping(self, channel)

    async def _apply_thread_tags(self, thread, status, origin="bot", mode=None) -> None:
        await tags_mod.apply_thread_tags(thread, status, origin, mode)

    async def _try_apply_tags_after_run(self, channel_id: str) -> None:
        await tags_mod.try_apply_tags_after_run(self, channel_id)

    async def _set_thread_active_tag(self, channel, active: bool) -> None:
        await tags_mod.set_thread_active_tag(self, channel, active)

    async def _monitor_setup(self, name: str) -> str:
        return await monitoring_mod.monitor_setup(self, name)

    def _init_monitor_service(self) -> None:
        monitoring_mod.init_monitor_service(self)

    # --- Resume after reboot ---

    async def _replay_to_thread(
        self, channel_id: str, prompt: str, repo_name: str | None = None,
        *, source: str = "replay",
    ) -> bool:
        """Look up a thread, build context, and dispatch a prompt through on_text.

        Returns True on success, False if the thread wasn't found.

        *source* tags the RequestContext so the engine can distinguish
        callback-resume replays from normal replays for the orchestrator
        wave-cap reset. Defaults to "replay" (non-resetting).
        """
        lookup = self._forums.thread_to_project(channel_id)
        if not lookup:
            log.warning("replay_to_thread: no thread mapping for %s", channel_id)
            return False
        proj, info = lookup
        session_id = info.session_id or None
        resolved_repo = repo_name or (
            proj.repo_name if proj.repo_name != "_default" else None
        )
        self._cancel_sleep(channel_id)
        ctx = self._ctx(channel_id, session_id=session_id, repo_name=resolved_repo,
                        thread_info=info, source=source)
        # Wire on_session_resolved so the session_id this replay produces
        # gets registered to info.session_id. Without it, the replay's
        # session lives only on disk (commands.py:995 only fires the
        # callback when it's set) — the next user message then finds
        # info.session_id still empty, starts cold, and loses the
        # plan/context this replay just produced. Parity with every
        # other entry point (user-message, lobby, callback, modal,
        # slash, interactions, spawn, app-resume) which all attach.
        self._forums.attach_session_callbacks(ctx, info, channel_id)
        # Replay bypasses the usage-limit gate: the window-end promoter already
        # classified these as queued, and "Run now" clicks have already been
        # consented to.  Re-prompting would loop.
        ctx.offer_usage_limit_choice = None
        await commands.on_text(ctx, prompt)
        self._forums.persist_ctx_settings(ctx)
        asyncio.create_task(self._try_apply_tags_after_run(channel_id))
        self._schedule_sleep(channel_id)
        return True

    async def _wait_for_ready(self, label: str) -> bool:
        """Wait up to 60s for bot + forum map. Returns False on timeout."""
        for _ in range(60):
            if self._ready_event.is_set() and self._forums.forum_projects:
                return True
            await asyncio.sleep(1)
        log.warning("%s: timed out waiting for bot ready + forum map", label)
        return False

    async def dispatch_resume(
        self, channel_id: str, prompt: str, announce: str | None = None,
    ) -> None:
        """Dispatch a query to a forum thread after reboot, resuming the session."""
        if not await self._wait_for_ready("dispatch_resume"):
            return

        if announce:
            asyncio.create_task(self._send_temp_lobby_msg(announce, delay=10))

        try:
            config.REBOOT_MSG_FILE.unlink(missing_ok=True)
        except Exception:
            pass

        if not prompt:
            return

        log.info("Resuming post-reboot in thread %s: %s", channel_id, prompt[:80])
        if await self._replay_to_thread(channel_id, prompt):
            asyncio.create_task(self._refresh_dashboard())

    async def dispatch_drain_queue(self, queue: list[dict]) -> None:
        """Replay messages that were queued during a reboot drain."""
        if not queue:
            return
        if not await self._wait_for_ready("dispatch_drain_queue"):
            return

        log.info("Replaying %d drain-queued messages", len(queue))
        for entry in queue:
            channel_id = entry.get("channel_id")
            if not channel_id:
                continue

            entry_type = entry.get("type", "text")
            if entry_type == "callback":
                await self._replay_callback(entry)
            else:
                prompt = entry.get("prompt")
                if not prompt:
                    continue
                try:
                    log.info("Replaying queued message in thread %s: %s",
                             channel_id, prompt[:60])
                    await self._replay_to_thread(
                        channel_id, prompt, repo_name=entry.get("repo_name"),
                    )
                except Exception:
                    log.exception("Failed to replay queued message in thread %s", channel_id)
        asyncio.create_task(self._refresh_dashboard())

    async def _replay_callback(self, entry: dict) -> None:
        """Replay a button callback action that was interrupted by reboot."""
        channel_id = entry.get("channel_id")
        action = entry.get("action")
        instance_id = entry.get("instance_id")
        if not channel_id or not action or not instance_id:
            log.warning("Incomplete callback replay entry: %s", entry)
            return

        lookup = self._forums.thread_to_project(channel_id)
        if not lookup:
            log.warning("replay_callback: no thread mapping for %s", channel_id)
            return

        proj, info = lookup
        ctx = self._ctx(channel_id, session_id=info.session_id,
                        thread_info=info, source="replay")
        ctx.user_id = entry.get("user_id", "")
        ctx.user_name = entry.get("user_name", "")
        self._forums.attach_session_callbacks(ctx, info, channel_id)

        # Map origin values back to button action names
        _ORIGIN_TO_ACTION = {
            "plan": "plan",
            "build": "build",
            "review_plan": "review_plan",
            "apply_revisions": "apply_revisions",
            "review_code": "review_code",
            "commit": "commit",
            "done": "done",
            "verify": "verify",
        }
        button_action = _ORIGIN_TO_ACTION.get(action, action)

        # source_msg_id was added in 0.87 for gated-button replay; pre-0.87
        # entries (drain-queue) won't have it — .get() keeps that path
        # backward-compatible (handle_callback tolerates None).
        source_msg_id = entry.get("source_msg_id")

        log.info("Replaying callback %s:%s in thread %s",
                 button_action, instance_id[:12], channel_id)

        # Acquire channel lock — same as normal button path in interactions.py
        from bot.engine.commands import _get_channel_lock
        lock = _get_channel_lock(channel_id)
        async with lock:
            try:
                await commands.handle_callback(
                    ctx, button_action, instance_id, source_msg_id=source_msg_id,
                )
            except Exception:
                log.exception("Failed to replay callback %s in thread %s",
                              button_action, channel_id)
            finally:
                self._forums.persist_ctx_settings(ctx)
                asyncio.create_task(self._try_apply_tags_after_run(channel_id))
                self._schedule_sleep(channel_id)

    # --- Usage-limit gate: Run now / Queue for 11am PT / Cancel ---

    async def _offer_gate(
        self,
        ctx: RequestContext,
        *,
        entry_extras: dict,
        label: str,
    ) -> bool:
        """Render the Run/Queue/Cancel UI and persist a queue entry.

        Shared body for both text-prompt and button-callback gating.
        ``entry_extras`` carries type-specific fields (``type``+``prompt``
        +``image_paths`` for text; ``type``+``action``+``instance_id``
        +``source_msg_id`` for callback).  ``label`` is the human-readable
        name shown in the gate UI ("Autopilot", "this prompt", etc.).

        Returns True when the gate handled the interaction.  Returns False
        to fall through to normal dispatch — used when the window isn't
        active, when a per-channel bypass is in effect, when the channel
        isn't a forum thread (no replay path), and as a safety fallback
        if the Discord send fails after the entry was persisted.
        """
        from bot.discord.usage_notifier import (
            is_usage_limit_active, next_window_end_utc,
        )
        if not is_usage_limit_active():
            return False
        # Per-channel bypass set by Run Now — quiets the gate for the rest of
        # the throttle window in this thread.  Checked AFTER is_usage_limit_active
        # so stale entries auto-prune once Anthropic lifts the throttle (the
        # early-return above means we never even read the map).
        bypass_end = self._usage_gate_bypass.get(ctx.channel_id)
        now = datetime.now(timezone.utc)
        if bypass_end and now < bypass_end:
            return False
        if bypass_end:  # expired — prune inline
            self._usage_gate_bypass.pop(ctx.channel_id, None)
        # Only gate forum-thread interactions.  Non-forum channels have no
        # replay path (_replay_to_thread/_replay_callback return early) and
        # queued entries would be orphaned.
        if not self._forums.thread_to_project(ctx.channel_id):
            return False

        # Double-tap dedup for callback gating: if the same channel + action
        # + instance_id is already awaiting_choice, don't create a duplicate.
        # Discord doesn't debounce buttons, so an impatient user tapping
        # Autopilot twice would otherwise produce two queue entries that
        # both auto-fire at window end.
        if entry_extras.get("type") == "callback":
            async with self._usage_queue_lock:
                for e in self._read_usage_queue():
                    if (e.get("status") == "awaiting_choice"
                        and e.get("type") == "callback"
                        and e.get("channel_id") == ctx.channel_id
                        and e.get("action") == entry_extras.get("action")
                        and e.get("instance_id") == entry_extras.get("instance_id")):
                        log.info(
                            "usage gate: dedup duplicate %s in %s (existing qid=%s)",
                            entry_extras.get("action"), ctx.channel_id, e.get("qid"),
                        )
                        return True

        qid = uuid.uuid4().hex[:8]
        end_utc = next_window_end_utc()
        entry = {
            "qid": qid,
            "status": "awaiting_choice",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_at": end_utc.isoformat(),
            "channel_id": ctx.channel_id,
            "message_id": None,
            "repo_name": ctx.repo_name,
            "user_id": ctx.user_id,
            "user_name": ctx.user_name,
        }
        entry.update(entry_extras)
        # Persist before send so a reboot between render and click cannot
        # silently drop the user's intent.
        await self._usage_queue_append(entry)

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Run now", style=discord.ButtonStyle.primary,
            custom_id=f"usage_run:{qid}",
        ))
        view.add_item(discord.ui.Button(
            label="Queue for 11am PT", style=discord.ButtonStyle.secondary,
            custom_id=f"usage_queue:{qid}",
        ))
        view.add_item(discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.danger,
            custom_id=f"usage_cancel:{qid}",
        ))

        unlock_ts = int(end_utc.timestamp())
        content = (
            f"Usage limits active until <t:{unlock_ts}:t>. "
            f"**{label}** — Run now (will be throttled) or queue?"
        )
        try:
            channel = self.get_channel(int(ctx.channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(ctx.channel_id))
            msg = await channel.send(content=content, view=view)
        except Exception:
            log.exception(
                "usage gate: failed to send choice prompt for %s, falling through",
                qid,
            )
            await self._usage_queue_remove(qid)
            return False

        await self._usage_queue_update(qid, message_id=str(msg.id))
        log.info(
            "usage gate: intercepted %s in %s (qid=%s)",
            entry_extras.get("action") or "text", ctx.channel_id, qid,
        )
        return True

    async def _offer_usage_limit_choice(
        self, ctx: RequestContext, text: str,
    ) -> bool:
        """Platform hook invoked from on_text during throttle windows."""
        return await self._offer_gate(
            ctx,
            entry_extras={
                "type": "text",
                "prompt": text,
                "image_paths": list(ctx.pending_image_paths),
            },
            label="this prompt",
        )

    async def _offer_usage_limit_choice_for_callback(
        self,
        ctx: RequestContext,
        action: str,
        instance_id: str,
        source_msg_id: str | None,
    ) -> bool:
        """Gate a Claude-spawning button click during throttle windows.

        Called from interactions.py before button dispatch.  The caller
        must check that no instance is already running on the channel
        (via lock.locked()) so the existing Steer/Cancel pending UI keeps
        owning the mid-run case.
        """
        return await self._offer_gate(
            ctx,
            entry_extras={
                "type": "callback",
                "action": action,
                "instance_id": instance_id,
                "source_msg_id": source_msg_id,
            },
            label=interactions_mod.action_label(action),
        )

    # --- Persistent queue helpers (atomic, lock-serialized) ---

    def _read_usage_queue(self) -> list[dict]:
        try:
            data = json.loads(
                config.USAGE_QUEUE_FILE.read_text(encoding="utf-8"),
            )
            return data if isinstance(data, list) else []
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _write_usage_queue_atomic(self, entries: list[dict]) -> None:
        tmp = config.USAGE_QUEUE_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, config.USAGE_QUEUE_FILE)

    async def _usage_queue_append(self, entry: dict) -> None:
        async with self._usage_queue_lock:
            entries = self._read_usage_queue()
            entries.append(entry)
            self._write_usage_queue_atomic(entries)

    async def _usage_queue_update(self, qid: str, **fields) -> dict | None:
        async with self._usage_queue_lock:
            entries = self._read_usage_queue()
            updated = None
            for e in entries:
                if e.get("qid") == qid:
                    e.update(fields)
                    updated = e
                    break
            if updated is not None:
                self._write_usage_queue_atomic(entries)
            return updated

    async def _usage_queue_remove(self, qid: str) -> dict | None:
        async with self._usage_queue_lock:
            entries = self._read_usage_queue()
            removed = None
            kept: list[dict] = []
            for e in entries:
                if removed is None and e.get("qid") == qid:
                    removed = e
                    continue
                kept.append(e)
            if removed is not None:
                self._write_usage_queue_atomic(kept)
            return removed

    async def _usage_queue_promote_expired(self, now_utc: datetime) -> None:
        """Flip awaiting_choice -> queued for any entries whose run_at has passed.

        This is the "ignoring the prompt = queuing" semantics. Runs atomically
        so the periodic loop never races with live user clicks on the same qid.
        """
        async with self._usage_queue_lock:
            entries = self._read_usage_queue()
            changed = False
            for e in entries:
                if e.get("status") != "awaiting_choice":
                    continue
                run_at = _parse_iso(e.get("run_at"))
                if run_at and run_at <= now_utc:
                    e["status"] = "queued"
                    changed = True
            if changed:
                self._write_usage_queue_atomic(entries)

    async def _usage_queue_pop_due(self, now_utc: datetime) -> list[dict]:
        """Remove and return all status==queued entries whose run_at has passed."""
        async with self._usage_queue_lock:
            entries = self._read_usage_queue()
            due: list[dict] = []
            kept: list[dict] = []
            for e in entries:
                if e.get("status") == "queued":
                    run_at = _parse_iso(e.get("run_at"))
                    if run_at and run_at <= now_utc:
                        due.append(e)
                        continue
                kept.append(e)
            if due:
                self._write_usage_queue_atomic(kept)
            return due

    async def _fire_due_entries(self, now_utc: datetime) -> None:
        """Shared body: promote expired awaiting_choice, then pop + replay queued.

        Dispatches by entry ``type``: text entries replay through on_text
        (``_replay_to_thread``), callback entries re-fire the button action
        (``_replay_callback``).  Entries without a ``type`` field (pre-0.87)
        default to text for backward compatibility.
        """
        await self._usage_queue_promote_expired(now_utc)
        due = await self._usage_queue_pop_due(now_utc)
        if not due:
            return
        log.info("Usage queue: firing %d due entries", len(due))
        # The pop above ended these entries' exemption from the reaper, so
        # re-arm the whole batch's retention now rather than per entry at its
        # own handoff: the replays below run one after another, each awaiting a
        # full query, so entry two's uploads would otherwise sit unreferenced
        # and reapable for however long entry one takes.
        for entry in due:
            refresh_image_retention(entry.get("image_paths", []))
        for entry in due:
            # Clear the stale gate message (best-effort) so its buttons don't
            # linger as "already resolved" traps once the prompt is running.
            await self._retire_gate_message(entry)
            try:
                if entry.get("type", "text") == "callback":
                    await self._replay_callback(entry)
                else:
                    prompt = prepare_replayed_prompt(
                        entry.get("prompt", ""), entry.get("image_paths", []),
                    )
                    await self._replay_to_thread(
                        entry["channel_id"], prompt,
                        repo_name=entry.get("repo_name"),
                    )
            except Exception:
                log.exception(
                    "usage_queue replay failed for %s", entry.get("qid"),
                )
            # No image cleanup here — same reason as the Run Now path in
            # interactions: _replay_to_thread also returns when the run is
            # killed mid-read, and the replacement run still needs the file.
            # The retention reaper collects them once the entry is gone.

    async def _retire_gate_message(self, entry: dict) -> None:
        """Edit the gate-prompt message to reflect that it's been fired."""
        msg_id = entry.get("message_id")
        ch_id = entry.get("channel_id")
        if not msg_id or not ch_id:
            return
        try:
            channel = self.get_channel(int(ch_id))
            if channel is None:
                channel = await self.fetch_channel(int(ch_id))
            msg = await channel.fetch_message(int(msg_id))
            await msg.edit(content="▶ Window ended — running now.", view=None)
        except Exception:
            pass  # message deleted / no access — purely cosmetic

    async def _migrate_relative_image_paths(self) -> None:
        """Rewrite pre-fix relative image paths in queued entries to absolute.

        Pre-fix entries (DATA_DIR=data unresolved) stored relative paths in
        both the ``image_paths`` array and inline in the prompt text
        (``[Image: ... saved at `<path>`]`` / ``Analyze this screenshot at
        `<path>`.``).  Those only resolved when the spawned subprocess shared
        the bot's cwd — broken for every non-bot repo and every worktree.
        Replays send the prompt text verbatim to Claude, so the inline
        substitution is the part that actually fixes the user-visible bug;
        the array is updated in lockstep so cleanup and existence checks
        stay consistent.  Resolves against the bot's cwd (= project root
        at startup).  Idempotent — already-absolute entries are skipped.
        """
        async with self._usage_queue_lock:
            entries = self._read_usage_queue()
            changed = 0
            for e in entries:
                paths = e.get("image_paths") or []
                if not paths:
                    continue
                prompt = e.get("prompt", "")
                new_paths: list[str] = []
                entry_changed = False
                for p in paths:
                    if Path(p).is_absolute():
                        new_paths.append(p)
                        continue
                    resolved = str(Path(p).resolve())
                    new_paths.append(resolved)
                    if prompt and p in prompt:
                        prompt = prompt.replace(p, resolved)
                    entry_changed = True
                if entry_changed:
                    e["image_paths"] = new_paths
                    if prompt != e.get("prompt", ""):
                        e["prompt"] = prompt
                    changed += 1
            if changed:
                self._write_usage_queue_atomic(entries)
                log.info(
                    "Usage queue: migrated %d entries with relative image paths to absolute",
                    changed,
                )

    async def _usage_queue_startup_drain(self) -> None:
        """Fire any entries overdue at boot — runs once before the periodic loop."""
        if not await self._wait_for_ready("usage_queue_startup_drain"):
            return
        await self._migrate_relative_image_paths()
        await self._fire_due_entries(datetime.now(timezone.utc))
        await self._sweep_pending_images()

    async def _sweep_pending_images(self) -> None:
        """Reap old uploads from PENDING_IMAGES_DIR.

        This is now the ONLY thing that deletes an upload (bar an explicitly
        cancelled queue entry, which never ran).  See ``reap_pending_images``
        for the policy and ``PENDING_IMAGES_TTL_HOURS`` for why nothing is
        deleted at the end of the turn that received it.
        """
        try:
            referenced: set[str] = set()
            async with self._usage_queue_lock:
                for e in self._read_usage_queue():
                    for p in e.get("image_paths", []) or []:
                        try:
                            referenced.add(str(Path(p).resolve()))
                        except OSError:
                            pass
            await asyncio.to_thread(
                reap_pending_images, referenced, _time.time(),
            )
        except Exception:
            log.exception("Pending-images sweep failed")

    def _schedule_pending_image_sweep(self) -> None:
        """Debounced nudge for the reaper, fired after an upload lands.

        The hourly loop is the real schedule; this only shortens the window in
        which a burst of large uploads can sit over the size cap.  Rate-limited
        to one sweep a minute so a rapid-fire batch of screenshots doesn't
        spawn one each — a batch that arrives inside that minute waits for the
        next nudge or the next tick, which the cap's headroom can absorb.

        Never raises: on_message calls this from a ``finally``, where an
        exception would mask whatever sent us there.
        """
        now = _time.time()
        if now - self._last_image_sweep < 60:
            return
        self._last_image_sweep = now
        try:
            asyncio.create_task(self._sweep_pending_images())
        except RuntimeError:  # loop already closing (shutdown)
            log.debug("Skipped image sweep — no running loop")

    async def _pending_image_sweep_loop(self) -> None:
        """Periodic reaper tick.

        Independent of the usage-queue startup drain, which sweeps once and
        then never again.  Sleeping first means it can't race that boot sweep.
        """
        while True:
            await asyncio.sleep(config.PENDING_IMAGES_SWEEP_SECS)
            try:
                self._last_image_sweep = _time.time()
                await self._sweep_pending_images()
            except Exception:
                log.exception("pending_image_sweep_loop iteration failed")

    async def _usage_queue_replay_loop(self) -> None:
        """Periodic tick (60s). Runs AFTER startup drain completes."""
        while True:
            await asyncio.sleep(60)
            try:
                await self._fire_due_entries(datetime.now(timezone.utc))
            except Exception:
                log.exception("usage_queue_replay_loop iteration failed")

    def _resolve_user_forum_context(
        self, interaction: discord.Interaction,
    ) -> tuple[str, str, str | None] | None:
        """If interaction is inside a user's personal forum, return (user_id, user_name, repo_name)."""
        parent = getattr(interaction.channel, "parent", None)
        if not parent:
            return None
        uf = self._forums.is_user_forum(str(parent.id))
        if not uf:
            return None
        user_id, user_name = uf
        repo_name = None
        cfg = load_access_config()
        ua = cfg.users.get(user_id)
        if ua and ua.repos:
            granted = [r for r in ua.repos if r in self._forums.forum_projects]
            if granted:
                repo_name = granted[0]
        return user_id, user_name, repo_name

    # --- Slash commands (delegated) ---

    async def _run_slash(
        self, interaction: discord.Interaction, coro,
        *, ephemeral: bool = False,
    ) -> None:
        """Defer, run engine command, then delete the 'thinking' response."""
        cmd_name = interaction.command.name if interaction.command else "?"
        log.info("Discord /%s in #%s by %s", cmd_name, getattr(interaction.channel, "name", "?"), interaction.user)
        await interaction.response.defer(ephemeral=ephemeral)
        channel_id = str(interaction.channel_id)
        lookup = self._forums.thread_to_project(channel_id)
        info = lookup[1] if lookup else None
        ar = self._check_access(interaction.user.id, channel_id=channel_id)
        ctx = self._ctx(channel_id, thread_info=info, access_result=ar)
        ctx.user_id = str(interaction.user.id)
        ctx.user_name = interaction.user.display_name
        # Auto-delete responses sent to the lobby (keep it clean)
        in_lobby = interaction.channel_id == self._lobby_channel_id
        if in_lobby:
            ctx.messenger = _AutoDeleteMessenger(ctx.messenger, channel_id)
        try:
            await coro(ctx)
        except Exception:
            log.exception("Slash command failed: /%s", cmd_name)
            try:
                await interaction.followup.send(
                    f"/{cmd_name} failed \u2014 check logs.", ephemeral=True,
                )
            except Exception:
                pass
            try:
                await interaction.delete_original_response()
            except Exception:
                pass
            return
        self._forums.persist_ctx_settings(ctx)
        try:
            await interaction.delete_original_response()
        except Exception:
            pass

    def _setup_commands(self) -> None:
        """Register slash commands (delegated to slash_commands module)."""
        slash_commands_mod.setup(self)

    # --- Bot lifecycle ---

    async def setup_hook(self) -> None:
        """Called when the bot is ready. Sync commands to guild."""
        guild = discord.Object(id=self._guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Synced slash commands to guild %s", self._guild_id)

        # Note: ArkView is NOT registered via add_view() — this bot uses
        # centralized on_interaction dispatch (interactions.handle) instead of
        # per-view callbacks.  Registering would intercept custom_ids and
        # raise NotImplementedError (no callback on plain Button items).

    async def on_ready(self) -> None:
        log.info("Discord bot ready as %s", self.user)

        if not self._voice_enabled and not getattr(self, "_voice_warning_logged", False):
            log.warning("OPENAI_API_KEY not configured — voice messages will be ignored")
            self._voice_warning_logged = True

        # Auto-provision category + The Ark if not configured
        if self._category_name and not self._lobby_channel_id:
            guild = self.get_guild(self._guild_id)
            if guild and guild.me:
                category = await channels.ensure_category(
                    guild, self._category_name, guild.me,
                    owner_id=self._discord_user_id,
                )
                self._category_id = category.id
                self._forums.category_id = category.id
                lobby = await channels.ensure_lobby(category)
                self._lobby_channel_id = lobby.id
                self._messenger = None
                log.info("Auto-provisioned category=%s lobby=%s", category.id, lobby.id)

                # Ping owner in new Ark so it appears in their sidebar
                if not [m async for m in lobby.history(limit=1)]:
                    owner = guild.get_member(self._discord_user_id) if self._discord_user_id else None
                    if owner:
                        await lobby.send(
                            f"{owner.mention} The Ark is ready. "
                            f"Add a repo with `/repo add` to get started.",
                            delete_after=60,
                        )

        # Load and reconcile forum mapping
        self._forums.load_forum_map()
        await self._forums.reconcile_forums()

        # Clean up orphaned messages in control rooms (one-time, non-blocking)
        if not getattr(self, '_control_rooms_cleaned', False):
            self._control_rooms_cleaned = True
            asyncio.create_task(self._forums.cleanup_all_control_rooms())

        self._ready_event.set()

        # Start monitor service if there are enabled monitors
        if not self._monitor_started:
            state = self._store.get_platform_state("discord")
            monitors = state.get("monitors", {})
            has_enabled = any(m.get("enabled") for m in monitors.values())
            if has_enabled:
                self._init_monitor_service()
                await self._monitor_service.recover_on_startup()
                self._monitor_service.start()
                self._monitor_started = True
                log.info("Monitor service started with %d enabled monitors",
                         sum(1 for m in monitors.values() if m.get("enabled")))

        # Warm up ccusage cache, then refresh dashboard (sequential so
        # the first dashboard render always has cached usage data)
        asyncio.create_task(self._warmup_then_refresh_lobby())

        # Periodic dashboard refresh (keeps usage data current)
        # Guard: on_ready fires on every reconnect — don't create duplicates
        existing = getattr(self, '_periodic_refresh_task', None)
        if not existing or existing.done():
            from bot.discord.dashboard import start_periodic_refresh
            self._periodic_refresh_task = asyncio.create_task(
                start_periodic_refresh(
                    self, self._store, self._forums,
                    self._lobby_channel_id, self._dashboard_lock,
                    self._dashboard_pending_flag,
                )
            )

        # Start usage limit notifier (DMs owner at 5am/11am PT on weekdays)
        if self._discord_user_id:
            existing_notifier = getattr(self, '_usage_notifier_task', None)
            if not existing_notifier or existing_notifier.done():
                from bot.discord import usage_notifier as _usage_notifier
                self._usage_notifier_task = asyncio.create_task(
                    _usage_notifier.usage_limit_notifier_loop(self, self._discord_user_id)
                )
        elif not getattr(self, '_notifier_warning_logged', False):
            log.warning("Usage limit notifier disabled — DISCORD_USER_ID not set in .env")
            self._notifier_warning_logged = True

        # Usage-queue startup drain + periodic replay loop.  Startup drain
        # runs first and awaits completion so the periodic loop cannot race
        # with it on the same entries.
        existing_replay = getattr(self, '_usage_queue_task', None)
        if not existing_replay or existing_replay.done():
            async def _usage_queue_main() -> None:
                try:
                    await self._usage_queue_startup_drain()
                except Exception:
                    log.exception("usage_queue_startup_drain failed")
                await self._usage_queue_replay_loop()
            self._usage_queue_task = asyncio.create_task(_usage_queue_main())

        # Upload reaper.  Separate from the drain above because the drain
        # sweeps once and then never again — and the reaper is the only thing
        # deleting uploads now, so "once at boot" would let a long-lived
        # process accumulate them indefinitely.
        existing_reaper = getattr(self, '_image_sweep_task', None)
        if not existing_reaper or existing_reaper.done():
            self._image_sweep_task = asyncio.create_task(
                self._pending_image_sweep_loop()
            )

        # Clean up stale worktrees/branches from interrupted autopilot chains
        asyncio.create_task(self._startup_worktree_cleanup())

    async def _startup_worktree_cleanup(self) -> None:
        """Recover partial worktrees, merge pending results, clean orphans.

        Order is load-bearing: ``recover_partial_worktrees`` must run BEFORE
        ``cleanup_stale_worktrees`` so an instance whose metadata was lost in
        an earlier buggy cleanup pass gets re-registered before the next
        cleanup pass would otherwise scoop it up as an orphan again. Without
        the recovery-first ordering, a single bad startup could permanently
        cement state.json into a broken state across reboots.
        """
        try:
            repos = self._store.list_repos()
            if not repos:
                return
            try:
                events = await self._runner.recover_partial_worktrees(
                    self._store, repos,
                )
            except Exception:
                events = []
                log.warning("Worktree recovery scan failed", exc_info=True)
            for ev in events:
                log.info(
                    "Worktree recovery: %s %s/%s status=%s detail=%s",
                    ev.instance_id, ev.repo_name, ev.branch,
                    ev.status, ev.detail,
                )
            if events:
                await self._surface_recovery_events(events)
            messages = await self._runner.cleanup_stale_worktrees(
                self._store, repos,
            )
            for msg in messages:
                log.info("Startup cleanup: %s", msg)
            if messages:
                log.info("Startup cleanup complete: %d actions", len(messages))
            try:
                release_events = await self._runner.scan_orphan_release_commits(
                    self._store, repos,
                )
            except Exception:
                release_events = []
                log.warning("Orphan release-commit scan failed", exc_info=True)
            for ev in release_events:
                log.info(
                    "Release recovery: %s %s/%s commit=%s bumped=%s detail=%s",
                    ev.instance_id, ev.repo_name, ev.branch,
                    ev.commit_sha, ev.bumped_version, ev.detail,
                )
            if release_events:
                await self._surface_release_recovery_events(release_events)
        except Exception:
            log.warning("Startup worktree cleanup failed", exc_info=True)

    async def _surface_recovery_events(self, events) -> None:
        """Post a one-line notice into each recovery event's thread.

        Silent recovery is what kept the original t-3700 bug invisible —
        every surfaced event here is a state change the user (managing
        from phone) needs to see. ``skipped`` events with a "branch no
        longer exists" detail are noise (recovered upstream by orphan
        cleanup) and intentionally suppressed.
        """
        for ev in events:
            if ev.status == "skipped" and "no longer exists" in (ev.detail or ""):
                continue
            inst = self._store.get_instance(ev.instance_id)
            if not inst or not inst.session_id:
                continue
            try:
                lookup = self._forums.session_to_thread(inst.session_id)
            except Exception:
                lookup = None
            if not lookup:
                continue
            thread_id, _ = lookup
            if ev.status == "recovered":
                text = (
                    f"Recovered worktree for `{ev.branch}` "
                    f"after metadata loss (content matched branch tip)."
                )
            elif ev.status == "manual_recovery_needed":
                text = (
                    f"Worktree for `{ev.branch}` lost git metadata "
                    f"AND has uncommitted drift ({ev.detail}). "
                    f"Inspect manually before the next Build."
                )
            else:  # "skipped"
                text = (
                    f"Skipped worktree recovery for `{ev.branch}` "
                    f"({ev.detail})."
                )
            try:
                await self.messenger.send_text(thread_id, text, silent=True)
            except Exception:
                log.debug(
                    "Failed to post recovery notice to thread %s", thread_id,
                    exc_info=True,
                )

    async def _surface_release_recovery_events(self, events) -> None:
        """Post a notice when a release session crashed mid-tag.

        Each event represents a non-terminal RELEASE/DONE instance that
        committed a vX.Y.Z bump but has no matching git tag — the runner
        has already marked it FAILED. Surface so the user can decide
        whether to retry the release or clean up the bumped commit.
        """
        for ev in events:
            inst = self._store.get_instance(ev.instance_id)
            if not inst or not inst.session_id:
                continue
            try:
                lookup = self._forums.session_to_thread(inst.session_id)
            except Exception:
                lookup = None
            if not lookup:
                continue
            thread_id, _ = lookup
            text = (
                f"Release crashed mid-tag: commit `{ev.commit_sha[:8]}` "
                f"bumped to `{ev.bumped_version}` on `{ev.branch}` but no "
                f"matching tag was created. Marked as failed — re-run "
                f"/release if you want to retry, or revert the bump."
            )
            try:
                await self.messenger.send_text(thread_id, text, silent=True)
            except Exception:
                log.debug(
                    "Failed to post release-recovery notice to thread %s",
                    thread_id, exc_info=True,
                )

    def _in_scope(self, guild: discord.Guild | None, channel: discord.abc.GuildChannel | None = None) -> bool:
        """Check guild + channel is within our category."""
        if not guild or guild.id != self._guild_id:
            return False
        if channel and self._category_id:
            if isinstance(channel, discord.Thread):
                parent = channel.parent or guild.get_channel(channel.parent_id)
                cat_id = getattr(parent, "category_id", None) if parent else None
            else:
                cat_id = getattr(channel, "category_id", None)
            if cat_id != self._category_id:
                return False
        return True

    async def close(self) -> None:
        if hasattr(self, '_periodic_refresh_task') and self._periodic_refresh_task:
            self._periodic_refresh_task.cancel()
        if self._monitor_service:
            self._monitor_service.stop()
        await super().close()

    # --- Message handling ---

    async def on_message(self, message: discord.Message) -> None:
        """Handle plain text messages in channels."""
        if message.author == self.user:
            return

        _is_test_webhook = (
            config.TEST_WEBHOOK_IDS
            and message.webhook_id
            and str(message.webhook_id) in config.TEST_WEBHOOK_IDS
        )

        if message.author.bot and not _is_test_webhook:
            return

        if not self._in_scope(message.guild, message.channel):
            return

        if not _is_test_webhook:
            msg_access = self._check_access(
                message.author.id, channel_id=str(message.channel.id),
            )
            if not msg_access.allowed:
                return
        else:
            msg_access = AccessResult(allowed=True, is_owner=True)

        text = message.content.strip()
        _image_paths: list[str] = []
        # Anything we couldn't take, phrased for the user.  Sent even when the
        # message carried no text of its own, so an upload can never vanish.
        _attach_problems: list[str] = []
        ctx = None  # set later in forum-thread / unmapped-channel branches

        async def _flush_attach_problems() -> None:
            """Send everything we turned away — the user's only window into it.

            The voice branch's early exits call this too.  They used to return
            straight past the send at the end of the loop, so a notice about an
            earlier attachment in the same message was written and then thrown
            away — the silence this whole notice mechanism exists to prevent.
            Archive channels stay read-only, so stay quiet there.
            """
            if not _attach_problems:
                return
            if message.channel.id in self._forums.archive_channel_ids:
                return
            try:
                await message.channel.send("\n".join(_attach_problems))
            except Exception:
                log.warning("Failed to send attachment notice", exc_info=True)
            _attach_problems.clear()

        def _note_unread_after(idx: int) -> None:
            """A voice note ends the loop — name whatever it left unopened.

            Transcription overwrites the message text, so a second voice note
            would erase the first and the loop has to stop.  Everything queued
            behind it goes unread, which the user has to be told rather than
            left to infer from an answer that ignores their screenshot.
            """
            rest = [
                a.filename for a in message.attachments[idx + 1:] if a.filename
            ]
            if not rest:
                return
            names = ", ".join(f"**{n}**" for n in rest)
            it = "it" if len(rest) == 1 else "them"
            _attach_problems.append(
                f"I stopped at the voice note in that message, so {names} went "
                f"unread — send {it} without a voice note and I'll open {it}."
            )

        # Handle file attachments
        if message.attachments:
            log.info(
                "Message has %d attachment(s): %s",
                len(message.attachments),
                [(a.filename, a.size, a.content_type) for a in message.attachments],
            )
        elif not text:
            log.info(
                "Empty message (flags=%s, type=%s, snapshots=%s, embeds=%d)",
                message.flags.value,
                message.type,
                bool(getattr(message, "message_snapshots", None)),
                len(message.embeds),
            )
        for idx, att in enumerate(message.attachments):
            if not att.filename:
                continue
            ext = Path(att.filename).suffix.lower()

            if (ext in ATTACH_AUDIO_EXTS and self._voice_enabled
                    and att.size <= ATTACH_AUDIO_MAX):
                try:
                    file_bytes = await att.read()
                    from bot.services.audio import transcribe
                    transcription = await transcribe(file_bytes, filename=att.filename)
                    cleaned = transcription.strip() if transcription else ""
                    if cleaned:
                        # Appended, not assigned: a caption typed alongside the
                        # voice note used to be overwritten by the
                        # transcription and never reached the session.  Same
                        # composition every other branch in this loop uses.
                        voice_block = f"[Voice message] {cleaned}"
                        text = f"{text}\n\n{voice_block}" if text else voice_block
                        log.info("Transcribed voice %s: %s", att.filename, cleaned[:80])
                        # Echo is non-critical — don't lose the transcription if send fails
                        try:
                            echo = cleaned[:1900] + "…" if len(cleaned) > 1900 else cleaned
                            echo_embed = discord.Embed(
                                description=echo,
                                color=discord.Color.greyple(),
                            )
                            echo_embed.set_author(name="Voice message")
                            await message.channel.send(embed=echo_embed)
                        except Exception:
                            log.warning("Failed to send voice echo", exc_info=True)
                    else:
                        _attach_problems.append(
                            f"I couldn't hear any speech in **{att.filename}**."
                        )
                        _note_unread_after(idx)
                        await _flush_attach_problems()
                        return
                except Exception:
                    log.warning("Voice transcription failed for %s", att.filename, exc_info=True)
                    _attach_problems.append(
                        f"I couldn't transcribe **{att.filename}** — try sending "
                        "it again."
                    )
                    _note_unread_after(idx)
                    await _flush_attach_problems()
                    return
                _note_unread_after(idx)
                break  # voice consumed, skip remaining attachments

            if ext in ATTACH_TEXT_EXTS and att.size <= ATTACH_TEXT_MAX:
                try:
                    file_bytes = await att.read()
                    # errors="replace" — a mis-encoded file degrades to
                    # readable text with substitutions rather than raising.
                    file_text = file_bytes.decode("utf-8", errors="replace")
                    text = f"{text}\n\n{file_text}" if text else file_text
                    log.info("Read text attachment %s (%d bytes)", att.filename, att.size)
                except Exception:
                    log.warning("Failed to read attachment %s", att.filename, exc_info=True)
                    _attach_problems.append(
                        f"I couldn't open **{att.filename}** — downloading it failed. "
                        "Try sending it again."
                    )

            elif ext in ATTACH_IMAGE_EXTS and att.size <= ATTACH_IMAGE_MAX:
                try:
                    img_path = config.PENDING_IMAGES_DIR / f"{uuid.uuid4().hex}{ext}"
                    file_bytes = await att.read()
                    img_path.write_bytes(file_bytes)
                    _image_paths.append(str(img_path))
                    img_prompt = f"[Image: {att.filename} saved at `{img_path}`]"
                    if text:
                        text = f"{text}\n\n{img_prompt}"
                    else:
                        text = f"Analyze this screenshot at `{img_path}`. Describe what you see."
                    log.info("Saved image attachment %s (%d bytes) to %s", att.filename, att.size, img_path)
                except Exception:
                    log.warning("Failed to save image %s", att.filename, exc_info=True)
                    _attach_problems.append(
                        f"I couldn't open **{att.filename}** — saving it failed. "
                        "Try sending it again."
                    )

            elif ext in ATTACH_DOC_EXTS and att.size <= ATTACH_DOC_MAX:
                # Same shape as the image branch: park the file and hand the
                # session a path, so its Read tool does the parsing.  Reusing
                # _image_paths is deliberate — that list is what a queued prompt
                # holds a reference to and what the retention reaper walks, so
                # documents inherit the identical lifecycle.
                try:
                    doc_path = config.PENDING_IMAGES_DIR / f"{uuid.uuid4().hex}{ext}"
                    file_bytes = await att.read()
                    doc_path.write_bytes(file_bytes)
                    _image_paths.append(str(doc_path))
                    doc_prompt = f"[File: {att.filename} saved at `{doc_path}`]"
                    if text:
                        text = f"{text}\n\n{doc_prompt}"
                    else:
                        text = (
                            f"Read the document saved at `{doc_path}` "
                            f"(the user uploaded it as {att.filename}) and tell "
                            "them what it says."
                        )
                    log.info("Saved document attachment %s (%d bytes) to %s", att.filename, att.size, doc_path)
                except Exception:
                    log.warning("Failed to save document %s", att.filename, exc_info=True)
                    _attach_problems.append(
                        f"I couldn't open **{att.filename}** — saving it failed. "
                        "Try sending it again."
                    )

            elif ext in ATTACH_WORD_EXTS and att.size <= ATTACH_DOC_MAX:
                try:
                    file_bytes = await att.read()
                    doc_text = _extract_docx_text(file_bytes)
                    if len(doc_text) > ATTACH_TEXT_MAX:
                        doc_text = doc_text[:ATTACH_TEXT_MAX] + "\n\n[document truncated]"
                    if doc_text.strip():
                        block = f"[Word document: {att.filename}]\n{doc_text}"
                        text = f"{text}\n\n{block}" if text else block
                        log.info(
                            "Extracted Word attachment %s (%d bytes, %d chars of text)",
                            att.filename, att.size, len(doc_text),
                        )
                    else:
                        log.info("Word attachment %s had no readable text", att.filename)
                        _attach_problems.append(
                            f"I opened **{att.filename}** but there was no text in it. "
                            "If the content is a scan or a picture, sending it as a "
                            "PDF or an image works better."
                        )
                except Exception:
                    log.warning("Failed to read Word attachment %s", att.filename, exc_info=True)
                    _attach_problems.append(
                        f"I couldn't open **{att.filename}** — it may be damaged, "
                        "password-protected, or an older Word format. Saving it as "
                        "a PDF and sending that usually works."
                    )

            else:
                # Nothing above could take it — too big, or a type we don't
                # handle.  Say so; never let an upload disappear in silence.
                log.info(
                    "Unhandled attachment %s (ext=%s, %d bytes)",
                    att.filename, ext, att.size,
                )
                _attach_problems.append(_attachment_reject_note(
                    att.filename, ext, att.size, voice_enabled=self._voice_enabled,
                ))

        # Tell the user about anything we turned away.  This runs before the
        # empty-text bail-out, which is what used to swallow a lone PDF upload.
        await _flush_attach_problems()

        if not text:
            return

        channel_id = str(message.channel.id)
        ch_name = getattr(message.channel, "name", channel_id)
        log.info("Discord msg in #%s: %s", ch_name, text[:80])

        try:
            # --- Archive channels: read-only, never respond ---
            if message.channel.id in self._forums.archive_channel_ids:
                return

            # --- The Ark: informational only, no session routing ---
            if message.channel.id == self._lobby_channel_id:
                if not msg_access.is_owner:
                    return
                if not self._store.list_repos():
                    await message.channel.send(
                        "Add or create a repo to begin \u2014 use `/repo add` or `/repo create`.",
                        delete_after=15,
                    )
                else:
                    await message.channel.send(
                        "Please send prompts inside the repo forum channels, not here.",
                        delete_after=15,
                    )
                try:
                    await message.delete()
                except Exception:
                    pass
                return

            # --- Forum thread: auto-resume session ---
            if isinstance(message.channel, discord.Thread):
                parent = message.channel.parent
                if parent and isinstance(parent, discord.ForumChannel):
                    # Skip control room threads
                    if any(p.control_thread_id == channel_id for p in self._forums.forum_projects.values()):
                        return
                    if channel_id in self._forums.user_control_thread_ids:
                        return
                    lookup = self._forums.thread_to_project(channel_id)
                    if not lookup:
                        # Adopt unmapped thread in a known forum (owner forums)
                        proj = self._forums.forum_by_channel_id(str(parent.id))
                        if proj:
                            log.info("Adopted unmapped thread %s in forum %s", channel_id, parent.name)
                            info = ThreadInfo(thread_id=channel_id, origin="bot")
                            proj.threads[channel_id] = info
                            self._forums.save_forum_map()
                            lookup = (proj, info)

                    # Check if this is a user's personal forum
                    if not lookup:
                        user_forum_info = self._forums.is_user_forum(str(parent.id))
                        if user_forum_info:
                            repo_name = self._forums.user_forum_thread_to_repo(message.channel)
                            if repo_name and repo_name in self._forums.forum_projects:
                                proj = self._forums.forum_projects[repo_name]
                                info = ThreadInfo(
                                    thread_id=channel_id, origin="bot",
                                    user_id=user_forum_info[0],
                                    user_name=user_forum_info[1],
                                )
                                proj.threads[channel_id] = info
                                self._forums.save_forum_map()
                                lookup = (proj, info)
                                log.info("Adopted user forum thread %s repo=%s user=%s",
                                         channel_id, repo_name, user_forum_info[1])
                            elif not repo_name:
                                uid = user_forum_info[0]
                                cfg = load_access_config()
                                ua = cfg.users.get(uid)
                                user_repos = [r for r in (ua.repos if ua else {}) if r in self._forums.forum_projects]
                                if len(user_repos) == 1:
                                    repo_name = user_repos[0]
                                    proj = self._forums.forum_projects[repo_name]
                                    info = ThreadInfo(
                                        thread_id=channel_id, origin="bot",
                                        user_id=user_forum_info[0],
                                        user_name=user_forum_info[1],
                                    )
                                    proj.threads[channel_id] = info
                                    self._forums.save_forum_map()
                                    lookup = (proj, info)
                                    log.info("Auto-selected single repo %s for user %s",
                                             repo_name, user_forum_info[1])
                                else:
                                    await self.messenger.send_text(
                                        channel_id,
                                        "Please select a repo tag on this thread so I know "
                                        "which project to work in.",
                                    )
                                    return

                    if lookup:
                        proj, info = lookup
                        # Track interacting user for close mentions
                        info.user_ids.add(str(message.author.id))
                        session_id = info.session_id or None
                        repo_name = proj.repo_name if proj.repo_name != "_default" else None
                        origin = info.origin

                        if session_id and origin == "cli":
                            log.info("Thread %s resuming CLI session %s — transitioning to bot ownership",
                                     ch_name, session_id[:12])
                            info.origin = "bot"
                            self._forums.save_forum_map()

                        was_pending = not session_id

                        # Inject pending /ref context (wall-clock TTL — survives reboot)
                        ref = self._pending_refs.pop(channel_id, None)
                        if ref:
                            ref_text, ref_time = ref
                            if (_time.time() - ref_time) < self._PENDING_REFS_TTL_SECONDS:
                                text = f"{ref_text}\n\n{text}"
                                log.info("Injected /ref context into prompt in thread %s", ch_name)
                            self._save_pending_refs()

                        self._cancel_sleep(channel_id)
                        await self._clear_thread_sleeping(message.channel)
                        asyncio.create_task(self._set_thread_active_tag(message.channel, True))
                        asyncio.create_task(self._refresh_dashboard())
                        ctx = self._ctx(channel_id, session_id=session_id,
                                        repo_name=repo_name, thread_info=info,
                                        access_result=msg_access,
                                        source="user_message")
                        ctx.user_id = str(message.author.id)
                        ctx.user_name = message.author.display_name
                        ctx.pending_image_paths = list(_image_paths)
                        self._forums.attach_session_callbacks(ctx, info, channel_id)
                        user_text = text  # preserve before tweet enrichment for title/topic
                        try:
                            try:
                                text = await enrich_with_tweets(text)
                            except Exception:
                                log.warning("Tweet enrichment failed, continuing with original text", exc_info=True)
                            await commands.on_text(ctx, text)
                        finally:
                            self._forums.persist_ctx_settings(ctx)
                            if was_pending:
                                await self._forums.finalize_pending_thread(channel_id, message.channel, user_text)
                            if not info._title_generated:
                                summary = self._forums.get_latest_summary(channel_id)
                                asyncio.create_task(self._generate_smart_title(
                                    message.channel, user_text, summary))
                            asyncio.create_task(self._try_apply_tags_after_run(channel_id))
                            self._schedule_sleep(channel_id)
                            asyncio.create_task(self._refresh_dashboard())
                        return

            # --- Other channel (unmapped): no session ---
            ctx = self._ctx(channel_id, access_result=msg_access,
                            source="user_message")
            ctx.user_id = str(message.author.id)
            ctx.user_name = message.author.display_name
            ctx.pending_image_paths = list(_image_paths)
            try:
                text = await enrich_with_tweets(text)
            except Exception:
                log.warning("Tweet enrichment failed, continuing with original text", exc_info=True)
            await commands.on_text(ctx, text)
        finally:
            # Uploads are deliberately NOT deleted here.  This frame returning
            # does not mean the run that reads the file is done with it — the
            # Steer path dispatches its run from a different task, a killed run
            # gets replaced by one resuming the same transcript, and any later
            # turn can be asked to look at the picture again.  Deleting on exit
            # wiped both of those cases (see reap_pending_images).  The reaper
            # owns the lifecycle now; nudge it so a burst can't sit over cap.
            if _image_paths:
                self._schedule_pending_image_sweep()

    async def _route_lobby_message(
        self, message: discord.Message, text: str, repo_name: str | None,
    ) -> None:
        """Route a lobby message to a forum thread."""
        repo_name = repo_name or "_default"
        asyncio.create_task(self._forums.ensure_control_post(repo_name))
        thread = await self._forums.get_or_create_session_thread(
            repo_name, None, text,
            user_id=str(message.author.id),
            user_name=message.author.display_name,
        )
        if thread:
            try:
                await thread.add_user(message.author)
            except Exception:
                log.warning("Failed to auto-follow user %s in thread %s",
                            message.author.id, thread.id)
            try:
                await message.delete()
            except Exception:
                pass
            asyncio.create_task(self._send_redirect(thread))
            tid = str(thread.id)
            self._cancel_sleep(tid)
            await self._clear_thread_sleeping(thread)
            asyncio.create_task(self._set_thread_active_tag(thread, True))
            asyncio.create_task(self._refresh_dashboard())
            lookup = self._forums.thread_to_project(tid)
            t_info = lookup[1] if lookup else None
            ctx = self._ctx(tid, repo_name=repo_name if repo_name != "_default" else None,
                            thread_info=t_info, source="user_message")
            if t_info:
                self._forums.attach_session_callbacks(ctx, t_info, tid)
            try:
                await commands.on_text(ctx, text)
            finally:
                self._forums.persist_ctx_settings(ctx)
                await self._forums.update_pending_thread(tid)
                summary = self._forums.get_latest_summary(tid)
                asyncio.create_task(self._generate_smart_title(thread, text, summary))
                asyncio.create_task(self._try_apply_tags_after_run(tid))
                self._schedule_sleep(tid)
                asyncio.create_task(self._refresh_dashboard())

    # --- Dashboard (delegated to dashboard_mod) ---

    async def _refresh_dashboard(self) -> None:
        """Update or create the pinned dashboard embed in lobby."""
        await dashboard_mod.refresh_dashboard(
            self, self._store, self._forums,
            self._lobby_channel_id, self._dashboard_lock,
            self._dashboard_pending_flag,
        )

    async def _warmup_then_refresh_lobby(self) -> None:
        """Warm ccusage cache, then refresh dashboard + clean lobby.

        Sequential ordering guarantees the first dashboard render has
        cached usage data instead of racing the warmup task.
        """
        from bot.engine.usage import warmup as _usage_warmup
        await _usage_warmup()
        await self._refresh_dashboard()
        await self._cleanup_lobby()

    async def _generate_smart_title(
        self, thread: discord.Thread, prompt: str, summary: str = "",
    ) -> None:
        """Fire-and-forget: generate an LLM title and rename the thread."""
        info = None
        try:
            thread_id = str(thread.id)
            lookup = self._forums.thread_to_project(thread_id)
            if not lookup:
                return
            forum_project, info = lookup
            if info._title_generated:
                return

            info._title_generated = True

            # Prefer the CLI's native, structured session title (clean — no
            # codename prefixes). Only spawn our own title-gen subprocess when
            # the jsonl has no ai-title yet (rare: very short/errored sessions).
            # read_ai_title does blocking file I/O — keep it off the event loop.
            title = (
                await asyncio.to_thread(read_ai_title, info.session_id)
                if info.session_id else None
            )
            if not title:
                title = await generate_title_text(prompt, summary)
            if not title:
                log.warning("Title generation returned empty for thread %s", thread_id)
                info._title_generated = False
                return

            base = channels.build_title_name(title)

            if thread_id in self._name_editing:
                info._title_generated = False
                return
            self._name_editing.add(thread_id)
            try:
                new_name = channels.build_thread_name(base)
                # Preserve any spawn-color prefix (load-bearing — without this,
                # smart-title would strip the dot/square that links this thread
                # to its family).
                new_name = await spawn_colors.compose_name(
                    thread_id, new_name, forum_project, self._store,
                )
                await thread.edit(name=new_name)
            finally:
                self._name_editing.discard(thread_id)

            info.topic = title
            self._forums.save_forum_map()
            log.info("Smart title for thread %s: %s", thread_id, new_name)
        except Exception:
            log.warning("Smart title generation failed for thread %s", thread.id, exc_info=True)
            if info is not None:
                info._title_generated = False

    # --- Session creation helpers ---

    async def _create_new_session(
        self, interaction: discord.Interaction, repo_name: str | None,
        *, redirect: bool = False, mode: str | None = None,
        user_id: str | None = None, user_name: str | None = None,
    ) -> None:
        """Create a new session thread."""
        repo_name = repo_name or "_default"

        forum_channel_id = None
        if user_id:
            cfg = load_access_config()
            ua = cfg.users.get(user_id)
            if ua and ua.forum_channel_id:
                forum_channel_id = ua.forum_channel_id

        thread = await self._forums.get_or_create_session_thread(
            repo_name, None, "new-session",
            forum_channel_id=forum_channel_id,
            user_id=user_id, user_name=user_name,
        )
        if thread:
            try:
                await thread.add_user(interaction.user)
            except Exception:
                log.warning("Failed to auto-follow user %s in thread %s",
                            interaction.user.id, thread.id)
            if mode:
                lookup = self._forums.thread_to_project(str(thread.id))
                if lookup:
                    lookup[1].mode = mode
                    self._forums.save_forum_map()
            if user_id or not redirect:
                await interaction.followup.send(
                    f"Fresh session created: <#{thread.id}>", ephemeral=True,
                )
            else:
                asyncio.create_task(self._send_redirect(thread))
        else:
            msg = "Could not create thread."
            if user_id or not redirect:
                await interaction.followup.send(msg, ephemeral=True)
            else:
                asyncio.create_task(self._send_temp_lobby_msg(msg))

    async def _send_temp_lobby_msg(self, text: str, delay: float = 5) -> None:
        """Send a temporary message in lobby that auto-deletes."""
        try:
            lobby = self.get_channel(self._lobby_channel_id)
            if lobby and isinstance(lobby, discord.TextChannel):
                msg = await lobby.send(text)
                await asyncio.sleep(delay)
                await msg.delete()
        except Exception:
            pass

    async def _send_redirect(self, thread: discord.Thread) -> None:
        """Post a redirect link in lobby, auto-delete after 5s."""
        await self._send_temp_lobby_msg(f"\u2192 <#{thread.id}>")

    async def _cleanup_lobby(self) -> None:
        """Delete all non-pinned messages from lobby on startup."""
        lobby = self.get_channel(self._lobby_channel_id)
        if not lobby or not isinstance(lobby, discord.TextChannel):
            return
        pinned_ids = {m.id for m in await lobby.pins()}
        deleted = 0

        # Pass 1: bulk-delete recent messages (<14 days) — fast
        try:
            purged = await lobby.purge(
                limit=100, check=lambda m: m.id not in pinned_ids,
            )
            deleted += len(purged)
        except Exception:
            log.warning("Lobby purge failed", exc_info=True)

        # Pass 2: individually delete older messages (>14 days)
        batch = 0
        try:
            async for msg in lobby.history(limit=100):
                if msg.id not in pinned_ids:
                    try:
                        await msg.delete()
                        deleted += 1
                        batch += 1
                        if batch >= 5:
                            await asyncio.sleep(1)
                            batch = 0
                    except Exception:
                        pass
        except Exception:
            log.warning("Lobby old-message cleanup failed", exc_info=True)

        if deleted:
            log.info("Lobby cleanup: deleted %d messages", deleted)

    # --- Interaction dispatch (delegated) ---

    async def on_error(self, event_method, *args, **kwargs) -> None:
        """Route event handler exceptions to the log file (not just stderr)."""
        log.exception("Unhandled exception in %s", event_method)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Handle button interactions (persistent views).

        Every component interaction leaves here settled: handled, or answered
        with a reason.  Letting one fall through unacknowledged costs the user
        a 3-second spinner ending in "This interaction failed", which looks
        identical to the bot being down and is just as hard to diagnose.
        """
        if interaction.type != discord.InteractionType.component:
            return
        if not self._in_scope(interaction.guild, interaction.channel):
            await interactions_mod.settle(
                interaction,
                "That control is outside this bot's workspace, so it can't act on it.",
            )
            return
        try:
            await interactions_mod.handle(self, interaction)
        except Exception:
            custom_id = (interaction.data or {}).get("custom_id", "?")
            log.exception("Button interaction failed: %s", custom_id)
            await interactions_mod.settle(
                interaction,
                "Something went wrong handling that button — the error is in the bot log.",
            )
