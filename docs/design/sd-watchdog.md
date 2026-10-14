# A frozen bot is restarted, not waited on (`bot/sd_watchdog.py`)

The phone is the only way back into this machine when something goes wrong,
so the bot being reachable is the property everything else rests on. Boot
start was already covered (enabled user unit, linger, `Restart=always`,
`StartLimitIntervalSec=0`). What was not: `Restart=always` acts on a process
that *exits*. A bot whose event loop is wedged on a blocking call, or whose
Discord client gave up and will never reconnect, is alive and useless, and
systemd reports it as healthy indefinitely.

The unit now sets `WatchdogSec=300` and `NotifyAccess=main`. A task started
in `app.run()` as soon as the PID lock is held sends `WATCHDOG=1` on
`$NOTIFY_SOCKET` every half timeout, and systemd restarts the unit when the pings stop (`Result=watchdog`,
SIGABRT). A frozen loop stops them by itself, because the pinger runs on that
loop. The other half is the health check: `_discord_is_down` reads the client
as gone when it `is_closed()` **or** its start task has ended, and
`DownGate` withholds the pings once that has held continuously for
`SD_WATCHDOG_DOWN_GRACE_SECS` (300). The start task matters as much as
`is_closed()`: a login that fails at boot (network not up yet) raises out of
`bot.start()` without ever closing the client, so `is_closed()` alone reads
that bot as healthy forever. The task is kept on the bot as `_start_task`,
which also fixes it being held only by asyncio's weak reference.

Worst case, a client that gave up is restarted about ten minutes after it
did: up to 150s until the next health sample, the 300s grace, then systemd's
300s timeout. A loop frozen for 300s had already lost its gateway heartbeat
(about 41s), so the restart costs nothing that was not already gone.

Verified live with `systemd-run --user -p WatchdogSec=4 -p NotifyAccess=main`:
a healthy loop stayed `active`, an unhealthy one was killed with
`Result=watchdog` within the timeout.

Four things that must not drift:

- **Inert unless the unit asks for it.** No `WATCHDOG_USEC`, a `WATCHDOG_PID`
  naming another process, or `SD_WATCHDOG_ENABLED=0` returns at once. The
  `WATCHDOG_PID` check is not decoration: every session's children inherit
  the environment.
- **Deploy the code before the unit setting.** A unit with `WatchdogSec=`
  running a build that never pings is killed every five minutes. The deployed
  copy in `~/.config/systemd/user/` needs the same two lines and a
  `daemon-reload`; it is not updated by a merge.
- **A health check that raises pings anyway.** It is a check that cannot
  answer, not evidence the bot is broken, and failing closed would restart a
  working bot over a bug in the check. Logged once per streak.
- **Only a client that will not come back counts.** discord.py reconnects
  through ordinary outages on its own and is neither closed nor finished while
  it does. Do not widen `_discord_is_down` to "not ready" or "no heartbeat
  ack": the router dropping for ten minutes would then restart a bot that was
  about to recover, in a loop, for as long as the outage lasts.

Harness: `python scripts/test_sd_watchdog.py`
