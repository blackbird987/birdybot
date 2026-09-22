"""Startup check: are the machine's resource protections actually applied?

The CPU/IO shares that stop a build freezing the desktop are declared in unit
files and enforced by the kernel from the cgroup, and those two have been out
of step before. On 2026-09-21 the live cgroup read 100 where both unit-file
copies said 20: the 2026-09-08 fix had been installed with `systemctl --user
set-property --runtime`, and systemd resets a --runtime drop-in when the unit
stops, which is exactly what an oomd kill does. The protection uninstalled
itself on the one event it exists for, and for a week the only evidence was a
desktop that kept freezing.

Two cgroups are checked, because since v0.101.27 they carry different halves
of the answer. app-claudesessions.slice holds every CLI, shell, dotnet and
Roslyn process and must read 20, which is the number that keeps a compile farm
off the desktop. claude-bot.service holds only the supervisor and must read
the default 100: a ~250 MB asyncio loop that has to answer Discord inside
three seconds gains the desktop nothing by being starved and loses the bot its
gateway connection.

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
    checks = []
    for read in (cgroups.check_weights, cgroups.check_session_slice_weights):
        try:
            checks.append(read())
        except Exception:
            # One unreadable cgroup must not cost the other its check. Same
            # rule as everywhere else here: what cannot be measured is not a
            # finding.
            log.exception("Resource weight check failed")

    bad = [c for c in checks if not c.ok()]
    for check in checks:
        if check.ok():
            log.info("Resource protection check: %s", check.summary())
    if not bad:
        return True

    for check in bad:
        log.warning(
            "Resource protection check: %s. %s",
            check.summary(), check.warning_text(),
        )

    if not bot._lobby_channel_id:
        return False
    channel = bot.get_channel(int(bot._lobby_channel_id))
    if channel is None or not hasattr(channel, "send"):
        return False
    text = "\n\n".join(c.warning_text() for c in bad)
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
