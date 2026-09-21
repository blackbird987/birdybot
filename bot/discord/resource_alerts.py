"""Startup check: are the machine's resource protections actually applied?

scripts/claude-bot.service declares CPUWeight=20 and IOWeight=20 so that the
bot loses a fight with the desktop. On 2026-09-21 the live cgroup read 100 for
both. The 2026-09-08 fix had been installed with `systemctl --user
set-property --runtime`, and systemd resets a --runtime drop-in when the unit
stops, which is exactly what an oomd kill does. The protection uninstalled
itself on the one event it exists for, and for a week the only evidence was a
desktop that kept freezing.

Nothing would have caught that. `systemctl show` reported 20, because by then
the drop-in agreed again; the unit files said 20 throughout, because they
always had. The one source that was wrong is the cgroup itself, which is what
`cgroups.check_weights` reads and what this posts about.

Deliberately a one-shot at startup, not a loop. The weights can only change
when the unit is restarted or someone runs set-property, and the first of
those brings this check with it. A poll would add a recurring notice for a
condition that cannot appear on its own.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bot.claude import cgroups

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)


async def check_and_report(bot: ClaudeBot) -> bool:
    """Run the weight check, log it, and post to The Ark on a mismatch.

    Returns True when everything is as intended, including the case where
    nothing could be measured: an unmeasurable machine is not a broken one,
    and a warning nobody can act on is worse than silence.
    """
    try:
        check = cgroups.check_weights()
    except Exception:
        log.exception("Resource weight check failed")
        return True

    if check.ok():
        log.info("Resource protection check: %s", check.summary())
        return True

    text = check.warning_text()
    log.warning("Resource protection check: %s. %s", check.summary(), text)

    if not bot._lobby_channel_id:
        return False
    channel = bot.get_channel(int(bot._lobby_channel_id))
    if channel is None or not hasattr(channel, "send"):
        return False
    try:
        await channel.send(f"⚠️ {text}")
    except Exception:
        log.exception("Could not post resource protection notice")
    return False


async def run_resource_check(bot: ClaudeBot) -> None:
    """Background one-shot: wait for the gateway, then check once.

    The session-scope probe is done here too, so whether sessions get their own
    cgroup is decided and logged once at boot rather than by whichever session
    happens to start first. It is cached either way; running it here only moves
    the one WARNING that says scopes are unavailable to where it is read.
    """
    if not await bot._wait_for_ready("resource_alerts"):
        return
    try:
        await cgroups.ensure_scope_support()
    except Exception:
        log.exception("Session scope probe failed")
    await check_and_report(bot)
