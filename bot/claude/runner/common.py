"""Small helpers and types shared by more than one runner module."""

from __future__ import annotations

import logging
from typing import Callable

from bot import config

log = logging.getLogger(__name__)

# On Windows, prevent subprocess console windows from popping up
_NOWND: dict = config.NOWND


# Environment variables removed before spawning any Claude CLI subprocess.
# These four are the bot's own Discord identity -- the token it logs in with,
# and the test webhooks that on_message historically treated as the owner.
# Nothing a session does needs them, so inheriting them only creates a way to
# leak them by accident.
#
# The Anthropic key is popped separately (it depends on the API-fallback path)
# and OPENAI_API_KEY / TWITTER_BEARER_TOKEN are deliberately NOT here: other
# repos may legitimately read those from the inherited environment.
#
# Public because the log-triage subprocess (bot/discord/log_triage.py) spawns
# a CLI too and has exactly the same reason to strip these. Kept separate from
# `_ENV_SECRET_NAMES` in bot/platform/formatting.py on purpose -- that list
# answers "what must never be PRINTED", this one answers "what must never be
# INHERITED", and they genuinely differ at both ends.
SESSION_STRIPPED_ENV_VARS: tuple[str, ...] = (
    "DISCORD_BOT_TOKEN",
    "TEST_WEBHOOK_URL",
    "TEST_LOBBY_WEBHOOK_URL",
    "TEST_WEBHOOK_IDS",
)


ProgressCallback = Callable  # async callback(message: str, detail: str)
# Stall callback: legacy single-arg form is still accepted via TypeError
# fallback at the call site, so platform layers that haven't been updated
# don't break. New form: async callback(instance_id, snapshot).
StallCallback = Callable     # async callback(instance_id: str, snapshot: StallSnapshot | None = None)
# Recovery callback: fires once per Layer 3 recovery event so the platform
# layer can post a visible thread warning (the silent session-clear was half
# of the t-3541 confusion — user couldn't see why context vanished).
# Signature: async callback(reason: str, lost_session_id: str | None,
#                            worktree_path: str | None)
RecoveryCallback = Callable
# Context-reset callback: asked for a briefing to hand the FRESH session that
# replaces one whose transcript could not be compacted (see
# parser.is_context_overflow_error). Returns a ready-to-prepend BLOCK -- the
# quoted recent thread history already wrapped in the preamble that frames it
# as data and the "---" separator that closes it -- or None when the platform
# has no history to offer. The runner cannot build any of that itself: the
# history and the wrapper both live in the platform layer, which owns the
# thread, so what comes back is glued in front of the prompt verbatim.
# Signature: async callback() -> str | None
ContextResetCallback = Callable


async def _notify(on_progress, headline: str, detail: str, what: str) -> None:
    """Send one progress update. A failing callback is logged, never raised.

    The update is a courtesy to the user; the recovery or kill it describes
    must go ahead whether or not the card could be edited.
    """
    if not on_progress:
        return
    try:
        await on_progress(headline, detail)
    except Exception:
        log.exception("Progress callback error %s", what)
