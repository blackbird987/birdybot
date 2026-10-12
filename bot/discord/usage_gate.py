"""The usage-limit gate and its persistent queue, as a ClaudeBot mixin.

When a subscription is near its limit a turn is offered Run now / Queue /
Cancel instead of starting. A queued prompt is written to a lock-serialized
JSON file and replayed when its slot comes due. Moved out of bot.py
unchanged; the methods still read ClaudeBot's own attributes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import discord

from bot.textutil import parse_iso_utc
from bot import config
from bot.fsutil import atomic_write_json
from bot.discord import interactions as interactions_mod
from bot.discord.attachments import prepare_replayed_prompt, refresh_image_retention
from bot.platform.base import RequestContext

log = logging.getLogger(__name__)


class UsageGateMixin:
    """ClaudeBot methods; see the module docstring."""

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
        atomic_write_json(config.USAGE_QUEUE_FILE, entries, ensure_ascii=False)

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
                run_at = parse_iso_utc(e.get("run_at"))
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
                    run_at = parse_iso_utc(e.get("run_at"))
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

    async def _usage_queue_replay_loop(self) -> None:
        """Periodic tick (60s). Runs AFTER startup drain completes."""
        while True:
            await asyncio.sleep(60)
            try:
                await self._fire_due_entries(datetime.now(timezone.utc))
            except Exception:
                log.exception("usage_queue_replay_loop iteration failed")
