"""Inline keyboard callback query handlers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from bot import config
from bot.claude.types import InstanceStatus, InstanceType
from bot.telegram.formatter import (
    build_action_buttons,
    escape_md,
    format_result,
)

if TYPE_CHECKING:
    from bot.claude.runner import ClaudeRunner
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


class CallbackHandler:
    """Handles inline keyboard button presses."""

    def __init__(self, store: StateStore, runner: ClaudeRunner) -> None:
        self._store = store
        self._runner = runner
        self._handlers_ref = None  # Set by app.py to reference Handlers for run_query

    def set_handlers_ref(self, handlers) -> None:
        self._handlers_ref = handlers

    async def handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return

        # Auth check
        if query.from_user and query.from_user.id != config.TELEGRAM_USER_ID:
            await query.answer("Unauthorized", show_alert=True)
            return

        await query.answer()

        parts = query.data.split(":", 1)
        if len(parts) != 2:
            return

        action, instance_id = parts

        dispatch = {
            "kill": self._on_kill,
            "retry": self._on_retry,
            "log": self._on_log,
            "diff": self._on_diff,
            "merge": self._on_merge,
            "discard": self._on_discard,
            "continue": self._on_continue,
            "wait": self._on_wait,
        }

        handler = dispatch.get(action)
        if handler:
            await handler(query, instance_id, context)

    async def _on_kill(self, query, instance_id: str, context) -> None:
        inst = self._store.get_instance(instance_id)
        if not inst:
            await query.edit_message_text("Instance not found.")
            return

        killed = await self._runner.kill(instance_id)
        if killed:
            inst.status = InstanceStatus.KILLED
            inst.finished_at = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat()
            self._store.update_instance(inst)
            await query.edit_message_text(
                f"Killed {escape_md(inst.display_id())}",
                parse_mode="MarkdownV2",
                reply_markup=build_action_buttons(inst),
            )
        else:
            await query.edit_message_text("Process not found or already stopped.")

    async def _on_retry(self, query, instance_id: str, context) -> None:
        inst = self._store.get_instance(instance_id)
        if not inst:
            await query.edit_message_text("Instance not found.")
            return

        if self._handlers_ref:
            # Create new instance with same prompt
            new_inst = self._store.create_instance(
                instance_type=inst.instance_type,
                prompt=inst.prompt,
                name=f"{inst.name}-retry" if inst.name else None,
                mode=inst.mode,
            )
            new_inst.repo_name = inst.repo_name
            new_inst.repo_path = inst.repo_path
            self._store.update_instance(new_inst)
            await query.edit_message_text(
                f"Retrying as {escape_md(new_inst.display_id())}\\.\\.\\.",
                parse_mode="MarkdownV2",
            )
            # Dispatch to handlers
            await self._handlers_ref.run_instance(new_inst, query.message.chat_id, context)
        else:
            await query.edit_message_text("Retry not available.")

    async def _on_log(self, query, instance_id: str, context) -> None:
        inst = self._store.get_instance(instance_id)
        if not inst:
            await query.edit_message_text("Instance not found.")
            return

        if inst.result_file:
            try:
                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=open(inst.result_file, "rb"),
                    filename=f"{inst.id}.md",
                    caption=f"Full output for {inst.display_id()}",
                )
            except Exception as e:
                await query.edit_message_text(f"Error sending file: {escape_md(str(e))}",
                                              parse_mode="MarkdownV2")
        else:
            text = inst.error or inst.summary or "No output recorded."
            await query.edit_message_text(escape_md(text), parse_mode="MarkdownV2")

    async def _on_diff(self, query, instance_id: str, context) -> None:
        inst = self._store.get_instance(instance_id)
        if not inst:
            await query.edit_message_text("Instance not found.")
            return

        if inst.diff_file:
            try:
                await context.bot.send_document(
                    chat_id=query.message.chat_id,
                    document=open(inst.diff_file, "rb"),
                    filename=f"{inst.id}.diff",
                    caption=f"Diff for {inst.display_id()}",
                )
            except Exception as e:
                await query.edit_message_text(f"Error: {escape_md(str(e))}",
                                              parse_mode="MarkdownV2")
        else:
            await query.edit_message_text("No diff available.")

    async def _on_merge(self, query, instance_id: str, context) -> None:
        inst = self._store.get_instance(instance_id)
        if not inst:
            await query.edit_message_text("Instance not found.")
            return

        msg = await self._runner.merge_branch(inst)
        self._store.update_instance(inst)
        await query.edit_message_text(escape_md(msg), parse_mode="MarkdownV2")

    async def _on_discard(self, query, instance_id: str, context) -> None:
        inst = self._store.get_instance(instance_id)
        if not inst:
            await query.edit_message_text("Instance not found.")
            return

        msg = await self._runner.discard_branch(inst)
        self._store.update_instance(inst)
        await query.edit_message_text(escape_md(msg), parse_mode="MarkdownV2")

    async def _on_continue(self, query, instance_id: str, context) -> None:
        inst = self._store.get_instance(instance_id)
        if not inst or not inst.session_id:
            await query.edit_message_text("Cannot continue — no session found.")
            return

        await query.edit_message_text(
            f"Reply to this message with your follow\\-up for {escape_md(inst.display_id())}",
            parse_mode="MarkdownV2",
        )

    async def _on_wait(self, query, instance_id: str, context) -> None:
        await query.edit_message_text(
            f"Waiting\\.\\.\\. process is still running\\.",
            parse_mode="MarkdownV2",
        )
