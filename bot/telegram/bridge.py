"""Thin Telegram Update -> RequestContext -> engine translation layer."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from bot import config
from bot.engine import commands
from bot.platform.base import RequestContext

if TYPE_CHECKING:
    from bot.telegram.adapter import TelegramMessenger
    from bot.claude.runner import ClaudeRunner
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


class TelegramBridge:
    """Translates Telegram Updates into RequestContext + engine calls."""

    def __init__(
        self,
        messenger: TelegramMessenger,
        store: StateStore,
        runner: ClaudeRunner,
    ) -> None:
        self._messenger = messenger
        self._store = store
        self._runner = runner

    def _auth(self, update: Update) -> bool:
        user = update.effective_user
        return user is not None and user.id == config.TELEGRAM_USER_ID

    def _ctx(self, update: Update) -> RequestContext:
        chat_id = str(update.effective_chat.id) if update.effective_chat else str(config.TELEGRAM_USER_ID)
        return RequestContext(
            messenger=self._messenger,
            channel_id=chat_id,
            platform="telegram",
            store=self._store,
            runner=self._runner,
        )

    # --- Message handlers ---

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message or not update.message.text:
            return
        text = update.message.text.strip()
        if not text:
            return
        await commands.on_text(self._ctx(update), text)

    async def on_unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message or not update.message.text:
            return
        text = update.message.text.strip()
        await commands.on_unknown_command(self._ctx(update), text)

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        ctx = self._ctx(update)
        if not commands.check_budget(ctx):
            await ctx.messenger.send_text(ctx.channel_id, "Daily budget exceeded.")
            return

        if not update.message.photo:
            return
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        await file.download_to_drive(tmp_path)

        caption = update.message.caption or ""
        if caption:
            prompt = f"Analyze the image at {tmp_path}. {caption}"
        else:
            prompt = f"Analyze this screenshot at {tmp_path} and describe what you see."

        try:
            await commands.on_text(ctx, prompt)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        ctx = self._ctx(update)
        if not commands.check_budget(ctx):
            await ctx.messenger.send_text(ctx.channel_id, "Daily budget exceeded.")
            return

        doc = update.message.document
        if not doc:
            return
        if doc.file_size and doc.file_size > 10 * 1024 * 1024:
            await ctx.messenger.send_text(ctx.channel_id, "File too large (max 10MB).")
            return

        file = await context.bot.get_file(doc.file_id)
        suffix = Path(doc.file_name).suffix if doc.file_name else ".txt"
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        await file.download_to_drive(tmp_path)

        caption = update.message.caption or ""
        filename = doc.file_name or "uploaded file"
        if caption:
            prompt = f"I've uploaded a file '{filename}' at {tmp_path}. {caption}"
        else:
            prompt = f"I've uploaded a file '{filename}' at {tmp_path}. Analyze its contents."

        try:
            await commands.on_text(ctx, prompt)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # --- Command handlers (thin wrappers: strip command prefix, delegate) ---

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        await commands.on_help(self._ctx(update))

    async def on_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        await commands.on_new(self._ctx(update))
        try:
            await update.message.delete()
        except Exception:
            pass

    async def on_bg(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/bg", "", 1).strip()
        await commands.on_bg(self._ctx(update), text)

    async def on_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").strip()
        await commands.on_list(self._ctx(update), text)

    async def on_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/kill", "", 1).strip()
        await commands.on_kill(self._ctx(update), text)

    async def on_retry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/retry", "", 1).strip()
        await commands.on_retry(self._ctx(update), text)

    async def on_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/log", "", 1).strip()
        await commands.on_log(self._ctx(update), text)

    async def on_diff(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/diff", "", 1).strip()
        await commands.on_diff(self._ctx(update), text)

    async def on_merge(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/merge", "", 1).strip()
        await commands.on_merge(self._ctx(update), text)

    async def on_discard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/discard", "", 1).strip()
        await commands.on_discard(self._ctx(update), text)

    async def on_cost(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        await commands.on_cost(self._ctx(update))

    async def on_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        await commands.on_status(self._ctx(update))

    async def on_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        await commands.on_logs(self._ctx(update))

    async def on_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/mode", "", 1).strip()
        await commands.on_mode(self._ctx(update), text)

    async def on_verbose(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/verbose", "", 1).strip()
        await commands.on_verbose(self._ctx(update), text)

    async def on_context(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/context", "", 1).strip()
        await commands.on_context(self._ctx(update), text)

    async def on_alias(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/alias", "", 1).strip()
        await commands.on_alias(self._ctx(update), text)

    async def on_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/schedule", "", 1).strip()
        await commands.on_schedule(self._ctx(update), text)

    async def on_repo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/repo", "", 1).strip()
        await commands.on_repo(self._ctx(update), text)

    async def on_budget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/budget", "", 1).strip()
        await commands.on_budget(self._ctx(update), text)

    async def on_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        await commands.on_clear(self._ctx(update))

    async def on_shutdown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        await commands.on_shutdown(self._ctx(update))

    async def on_reboot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        await commands.on_reboot(self._ctx(update))

    async def on_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").replace("/session", "", 1).strip()
        await commands.on_session(self._ctx(update), text)

    # --- Callback query handler ---

    async def on_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return

        if query.from_user and query.from_user.id != config.TELEGRAM_USER_ID:
            await query.answer("Unauthorized", show_alert=True)
            return

        await query.answer()

        parts = query.data.split(":", 1)
        if len(parts) != 2:
            return

        action, instance_id = parts
        chat_id = str(query.message.chat_id) if query.message else str(config.TELEGRAM_USER_ID)
        source_msg_id = str(query.message.message_id) if query.message else None

        ctx = RequestContext(
            messenger=self._messenger,
            channel_id=chat_id,
            platform="telegram",
            store=self._store,
            runner=self._runner,
        )

        await commands.handle_callback(ctx, action, instance_id, source_msg_id)
