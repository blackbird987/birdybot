"""Messenger protocol, ButtonSpec, MessageHandle, RequestContext, NotificationService."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from bot.claude.runner import ClaudeRunner
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


@dataclass
class ButtonSpec:
    """Platform-agnostic button definition."""
    label: str
    callback_data: str


@dataclass
class MessageHandle:
    """Opaque handle for a thinking/progress message.

    Each platform stores whatever it needs to edit the message later.
    The engine never inspects internals — just passes it to edit_thinking.
    """
    platform: str
    _data: dict = field(default_factory=dict)

    def get(self, key: str, default=None):
        return self._data.get(key, default)


@runtime_checkable
class Messenger(Protocol):
    """Protocol for platform-specific messaging."""

    @property
    def platform_name(self) -> str:
        """Return 'telegram' or 'discord'."""
        ...

    async def create_conversation(
        self, instance_id: str, summary: str, is_task: bool,
    ) -> str:
        """Create a conversation space. Returns channel_id.

        Discord: thread (query) or channel (task).
        Telegram: returns existing chat_id.
        """
        ...

    async def send_thinking(
        self, channel_id: str, text: str,
        buttons: list[list[ButtonSpec]] | None = None,
    ) -> MessageHandle:
        """Send initial thinking/progress message. Returns handle for editing."""
        ...

    async def edit_thinking(
        self, handle: MessageHandle, text: str,
        buttons: list[list[ButtonSpec]] | None = None,
    ) -> None:
        """Edit a thinking message identified by handle."""
        ...

    async def send_text(
        self, channel_id: str, text: str,
        buttons: list[list[ButtonSpec]] | None = None,
        silent: bool = False,
    ) -> str:
        """Send a regular text message. Returns message_id as string."""
        ...

    async def send_result(
        self, channel_id: str, text: str,
        metadata: dict | None = None,
        buttons: list[list[ButtonSpec]] | None = None,
        silent: bool = False,
    ) -> str:
        """Send a result message. Discord uses embed; Telegram uses HTML.
        Returns message_id as string.
        """
        ...

    async def edit_text(
        self, channel_id: str, msg_id: str | None, text: str | None,
        buttons: list[list[ButtonSpec]] | None = None,
    ) -> None:
        """Edit a regular message. If text is None, only update buttons."""
        ...

    async def delete_message(self, channel_id: str, msg_id: str) -> None:
        """Delete a message."""
        ...

    async def send_file(
        self, channel_id: str, file_path: str, filename: str,
        caption: str | None = None,
    ) -> str:
        """Send a file. Returns message_id as string."""
        ...

    def markdown_to_markup(self, md: str) -> str:
        """Convert markdown to platform markup.
        Telegram: converts to HTML. Discord: passes through.
        """
        ...

    def escape(self, text: str) -> str:
        """Escape text for the platform."""
        ...

    def chunk_message(self, text: str) -> list[str]:
        """Split text into platform-safe chunks."""
        ...

    async def close_conversation(self, channel_id: str) -> None:
        """Close/archive a conversation. Discord: archive+lock thread. Telegram: no-op."""
        ...


@dataclass
class RequestContext:
    """Everything an engine function needs to operate on a request."""
    messenger: Messenger
    channel_id: str
    platform: str           # "telegram" or "discord"
    store: StateStore
    runner: ClaudeRunner
    session_id: str | None = None  # per-request override (Discord channels)
    repo_name: str | None = None   # per-request repo override (Discord channels)
    # Per-thread settings overrides (None = inherit from global store)
    mode: str | None = None
    context: str | None = None        # None=inherit, ""=cleared, str=set
    verbose_level: int | None = None
    # Session resolution callbacks (Discord race-condition fix)
    resolve_session_id: Callable[[], str | None] | None = None
    on_session_resolved: Callable[[str], None] | None = None

    @property
    def effective_mode(self) -> str:
        return self.mode if self.mode is not None else self.store.mode

    @property
    def effective_context(self) -> str | None:
        if self.context is None:
            return self.store.context      # inherit global
        return self.context or None        # "" sentinel -> None (cleared)

    @property
    def effective_verbose(self) -> int:
        return self.verbose_level if self.verbose_level is not None else self.store.verbose_level

    def update_mode(self, value: str) -> None:
        self.mode = value
        if self.platform != "discord":
            self.store.mode = value

    def update_context(self, value: str | None) -> None:
        self.context = value if value is not None else ""  # "" = explicitly cleared
        if self.platform != "discord":
            self.store.context = value

    def update_verbose(self, value: int) -> None:
        self.verbose_level = value
        if self.platform != "discord":
            self.store.verbose_level = value


class NotificationService:
    """Broadcasts notifications to all registered Messengers (best-effort)."""

    def __init__(self) -> None:
        self._messengers: dict[str, tuple[Messenger, str]] = {}

    def register(self, messenger: Messenger, default_channel_id: str) -> None:
        self._messengers[messenger.platform_name] = (messenger, default_channel_id)

    def unregister(self, platform: str) -> None:
        self._messengers.pop(platform, None)

    async def broadcast(
        self, text: str,
        buttons: list[list[ButtonSpec]] | None = None,
        silent: bool = False,
    ) -> None:
        """Send to all registered platforms. Best-effort — one failure doesn't block others."""
        for platform, (messenger, channel_id) in self._messengers.items():
            try:
                await messenger.send_text(channel_id, text, buttons, silent)
            except Exception:
                log.exception("Failed to broadcast to %s", platform)

    async def broadcast_result(
        self, text: str,
        metadata: dict | None = None,
        buttons: list[list[ButtonSpec]] | None = None,
        silent: bool = False,
    ) -> None:
        """Send result to all registered platforms."""
        for platform, (messenger, channel_id) in self._messengers.items():
            try:
                await messenger.send_result(channel_id, text, metadata, buttons, silent)
            except Exception:
                log.exception("Failed to broadcast result to %s", platform)
