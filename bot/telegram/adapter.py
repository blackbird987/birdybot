"""TelegramMessenger — implements the Messenger protocol for Telegram."""

from __future__ import annotations

import logging
import re

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from bot.platform.base import ButtonSpec, MessageHandle
from bot.telegram.formatter import chunk_message, escape_html, to_telegram_html

log = logging.getLogger(__name__)

PARSE_MODE = "HTML"

_RE_HTML_TAG = re.compile(r'<[^>]+>')


def _strip_html(text: str) -> str:
    """Remove HTML tags for plain-text fallback."""
    return _RE_HTML_TAG.sub('', text)


def _buttons_to_markup(
    buttons: list[list[ButtonSpec]] | None,
) -> InlineKeyboardMarkup | None:
    """Convert ButtonSpec rows to Telegram InlineKeyboardMarkup."""
    if not buttons:
        return None
    rows = []
    for row in buttons:
        rows.append([
            InlineKeyboardButton(b.label, callback_data=b.callback_data)
            for b in row
        ])
    return InlineKeyboardMarkup(rows) if rows else None


class TelegramMessenger:
    """Implements Messenger protocol for Telegram."""

    def __init__(self, bot: Bot, default_chat_id: int) -> None:
        self._bot = bot
        self._default_chat_id = default_chat_id

    @property
    def platform_name(self) -> str:
        return "telegram"

    async def create_conversation(
        self, instance_id: str, summary: str, is_task: bool,
    ) -> str:
        """Telegram has one chat — return the default chat_id."""
        return str(self._default_chat_id)

    async def send_thinking(
        self, channel_id: str, text: str,
        buttons: list[list[ButtonSpec]] | None = None,
    ) -> MessageHandle:
        """Send a thinking message. Returns handle wrapping (chat_id, msg_id)."""
        markup = _buttons_to_markup(buttons)
        msg = await self._send_safe(int(channel_id), text, markup)
        return MessageHandle(
            platform="telegram",
            _data={"chat_id": int(channel_id), "message_id": str(msg.message_id)},
        )

    async def edit_thinking(
        self, handle: MessageHandle, text: str,
        buttons: list[list[ButtonSpec]] | None = None,
    ) -> None:
        """Edit a thinking message."""
        chat_id = handle.get("chat_id")
        message_id = int(handle.get("message_id"))
        markup = _buttons_to_markup(buttons)
        try:
            await self._bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=PARSE_MODE,
                reply_markup=markup,
            )
        except Exception:
            # Try plain text fallback
            try:
                plain = _strip_html(text)
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=plain or "...",
                    reply_markup=markup,
                )
            except Exception:
                pass

    async def send_text(
        self, channel_id: str, text: str,
        buttons: list[list[ButtonSpec]] | None = None,
        silent: bool = False,
    ) -> str:
        """Send a text message. Returns message_id as string."""
        markup = _buttons_to_markup(buttons)
        msg = await self._send_safe(int(channel_id), text, markup, silent)
        return str(msg.message_id)

    async def send_result(
        self, channel_id: str, text: str,
        metadata: dict | None = None,
        buttons: list[list[ButtonSpec]] | None = None,
        silent: bool = False,
    ) -> str:
        """Send a result (same as send_text for Telegram)."""
        return await self.send_text(channel_id, text, buttons, silent)

    async def edit_text(
        self, channel_id: str, msg_id: str | None, text: str | None,
        buttons: list[list[ButtonSpec]] | None = None,
    ) -> None:
        """Edit a regular message. If text is None, only strip buttons."""
        if msg_id is None:
            return
        markup = _buttons_to_markup(buttons)
        try:
            if text is None:
                # Strip buttons only
                await self._bot.edit_message_reply_markup(
                    chat_id=int(channel_id),
                    message_id=int(msg_id),
                    reply_markup=markup,
                )
            else:
                await self._bot.edit_message_text(
                    chat_id=int(channel_id),
                    message_id=int(msg_id),
                    text=text,
                    parse_mode=PARSE_MODE,
                    reply_markup=markup,
                )
        except Exception:
            if text:
                try:
                    plain = _strip_html(text)
                    await self._bot.edit_message_text(
                        chat_id=int(channel_id),
                        message_id=int(msg_id),
                        text=plain[:4096] or "...",
                        reply_markup=markup,
                    )
                except Exception:
                    pass

    async def delete_message(self, channel_id: str, msg_id: str) -> None:
        try:
            await self._bot.delete_message(chat_id=int(channel_id), message_id=int(msg_id))
        except Exception:
            pass

    async def send_file(
        self, channel_id: str, file_path: str, filename: str,
        caption: str | None = None,
    ) -> str:
        with open(file_path, "rb") as f:
            msg = await self._bot.send_document(
                chat_id=int(channel_id),
                document=f,
                filename=filename,
                caption=caption,
            )
        return str(msg.message_id)

    def markdown_to_markup(self, md: str) -> str:
        """Convert markdown to Telegram HTML."""
        return to_telegram_html(md)

    def escape(self, text: str) -> str:
        return escape_html(text)

    def chunk_message(self, text: str) -> list[str]:
        return chunk_message(text)

    # --- Internal ---

    async def _send_safe(
        self, chat_id: int, text: str,
        markup: InlineKeyboardMarkup | None = None,
        silent: bool = False,
    ):
        """Send with HTML parse mode, falling back to plain text."""
        try:
            return await self._bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=PARSE_MODE,
                reply_markup=markup,
                disable_notification=silent,
            )
        except Exception:
            plain = _strip_html(text)
            return await self._bot.send_message(
                chat_id=chat_id,
                text=plain or "...",
                reply_markup=markup,
                disable_notification=silent,
            )
