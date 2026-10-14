"""systemd service watchdog: a frozen bot is restarted, not waited on.

``Restart=always`` brings the bot back when the process *exits*. It does
nothing for a process that is alive and useless: an event loop wedged on a
blocking call, or a Discord client that crashed or closed and will never
reconnect. Both leave the phone with nothing to talk to and the machine
reporting the unit as healthy.

With ``WatchdogSec=`` set on the unit, systemd expects ``WATCHDOG=1`` on
``$NOTIFY_SOCKET`` at least that often and restarts the unit when it stops
arriving. This module sends it from a task on the event loop, so a frozen
loop stops the pings by itself, and it withholds them when the caller's
health check says the bot can no longer be reached.

Inert unless the unit asks for it: no ``WATCHDOG_USEC`` in the environment
(no ``WatchdogSec=``, not under systemd, Windows) means the loop returns at
once. See docs/design/sd-watchdog.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Callable

from bot import config

log = logging.getLogger(__name__)


def notify(msg: str) -> bool:
    """Send one sd_notify datagram. Never raises; False when nothing was sent.

    Non-blocking on purpose: this runs on the event loop, and a full receive
    queue on systemd's side must cost one missed ping, not a stalled loop.
    """
    path = os.environ.get("NOTIFY_SOCKET", "")
    if not path:
        return False
    if path.startswith("@"):
        addr = "\0" + path[1:]          # abstract namespace
    elif path.startswith("/"):
        addr = path
    else:
        log.debug("sd_notify: unsupported NOTIFY_SOCKET %r", path)
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.sendto(msg.encode(), addr)
        return True
    except OSError as exc:
        log.debug("sd_notify %r failed: %s", msg, exc)
        return False


def watchdog_interval_secs() -> float | None:
    """Half the unit's watchdog timeout, or None when no watchdog applies.

    ``WATCHDOG_PID`` names the process the timeout is meant for. Children
    inherit the environment, so a mismatch means we are not that process.
    """
    raw = os.environ.get("WATCHDOG_USEC", "")
    try:
        usec = int(raw)
    except ValueError:
        return None
    if usec <= 0:
        return None
    pid = os.environ.get("WATCHDOG_PID", "")
    if pid:
        try:
            if int(pid) != os.getpid():
                return None
        except ValueError:
            return None
    return usec / 2 / 1_000_000


class DownGate:
    """Healthy unless ``is_down()`` has been true continuously for ``grace_secs``.

    discord.py reconnects on its own through ordinary outages, so a single
    down reading proves nothing. Only a state that persists past the grace is
    treated as one a restart has to fix.
    """

    def __init__(
        self,
        is_down: Callable[[], bool],
        grace_secs: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._is_down = is_down
        self._grace = grace_secs
        self._clock = clock
        self._down_since: float | None = None

    def __call__(self) -> bool:
        if not self._is_down():
            self._down_since = None
            return True
        now = self._clock()
        if self._down_since is None:
            self._down_since = now
        return now - self._down_since < self._grace


async def watchdog_loop(is_healthy: Callable[[], bool]) -> None:
    """Ping systemd's watchdog while ``is_healthy()`` holds. Runs until cancelled."""
    if not config.SD_WATCHDOG_ENABLED:
        return
    interval = watchdog_interval_secs()
    if interval is None:
        return
    log.info("systemd watchdog armed: pinging every %.0fs", interval)
    notify("READY=1")
    was_healthy = True
    check_broken = False
    while True:
        try:
            healthy = bool(is_healthy())
            check_broken = False
        except Exception:
            # A health check that cannot answer is not evidence the bot is
            # broken. Failing closed here would restart a working bot over a
            # bug in the check. Logged once per streak, not every interval.
            if not check_broken:
                log.exception("systemd watchdog health check raised; pinging anyway")
            check_broken = True
            healthy = True
        if healthy:
            notify("WATCHDOG=1")
            if not was_healthy:
                log.warning("systemd watchdog: bot healthy again, pings resumed")
        elif was_healthy:
            log.warning(
                "systemd watchdog: bot unreachable, withholding pings so "
                "systemd restarts it"
            )
        was_healthy = healthy
        await asyncio.sleep(interval)
