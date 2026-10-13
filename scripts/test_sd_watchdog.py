#!/usr/bin/env python3
"""Harness: the systemd watchdog pings when healthy and is inert otherwise.

Driven against a real AF_UNIX datagram socket standing in for systemd's
$NOTIFY_SOCKET, both by path and in the abstract namespace. Pins:

  * no WATCHDOG_USEC, a WATCHDOG_PID naming another process, or
    SD_WATCHDOG_ENABLED off -> the loop returns at once and sends nothing
  * healthy -> READY=1 then WATCHDOG=1 each interval; unhealthy -> no pings
  * a health check that raises still pings (fail open)
  * an unhealthy spell is logged once going down and once on recovery
  * a send that cannot be delivered returns False, never raises
  * DownGate only reports unhealthy after the grace has run out
  * _discord_is_down: closed or a finished start task is down, None is not
  * structural: app.run() starts watchdog_loop, the unit sets WatchdogSec and
    NotifyAccess=main

    python scripts/test_sd_watchdog.py
"""

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import logging
import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import app, config, sd_watchdog  # noqa: E402

_failures: list[str] = []
_ENV_KEYS = ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID")
_TMP = tempfile.TemporaryDirectory(prefix="sdwd-")


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name}" + (f" ({detail})" if detail else ""))
        _failures.append(name)


class _Env:
    """Set the three systemd variables for one case, restore them after."""

    def __init__(self, **values: str | None) -> None:
        self._values = values
        self._saved: dict[str, str | None] = {}

    def __enter__(self):
        for key in _ENV_KEYS:
            self._saved[key] = os.environ.get(key)
            os.environ.pop(key, None)
        for key, val in self._values.items():
            if val is not None:
                os.environ[key] = val
        return self

    def __exit__(self, *exc):
        for key, val in self._saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val


def _receiver(abstract: bool = False) -> tuple[socket.socket, str]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    if abstract:
        name = f"sdwd-test-{os.getpid()}"
        sock.bind("\0" + name)
        env = "@" + name
    else:
        path = os.path.join(_TMP.name, f"notify-{len(os.listdir(_TMP.name))}")
        sock.bind(path)
        env = path
    sock.setblocking(False)
    return sock, env


def _drain(sock: socket.socket) -> list[str]:
    out = []
    while True:
        try:
            out.append(sock.recv(4096).decode())
        except BlockingIOError:
            return out


def _run_loop(is_healthy, secs: float = 0.35) -> None:
    async def go():
        task = asyncio.create_task(sd_watchdog.watchdog_loop(is_healthy))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=secs)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    asyncio.run(go())


def check_notify() -> None:
    sock, env = _receiver()
    with _Env(NOTIFY_SOCKET=env):
        _check("notify by path delivers", sd_watchdog.notify("WATCHDOG=1"))
    _check("receiver got WATCHDOG=1", _drain(sock) == ["WATCHDOG=1"])
    sock.close()

    sock, env = _receiver(abstract=True)
    with _Env(NOTIFY_SOCKET=env):
        _check("notify to @abstract delivers", sd_watchdog.notify("WATCHDOG=1"))
    _check("abstract receiver got WATCHDOG=1", _drain(sock) == ["WATCHDOG=1"])
    sock.close()

    with _Env(NOTIFY_SOCKET="/nonexistent/sdwd/notify"):
        try:
            ok = sd_watchdog.notify("WATCHDOG=1")
            _check("undeliverable send returns False", ok is False)
        except Exception as exc:  # noqa: BLE001
            _check("undeliverable send does not raise", False, repr(exc))
    with _Env():
        _check("no NOTIFY_SOCKET returns False",
               sd_watchdog.notify("WATCHDOG=1") is False)


def check_interval() -> None:
    with _Env():
        _check("no WATCHDOG_USEC -> no interval",
               sd_watchdog.watchdog_interval_secs() is None)
    with _Env(WATCHDOG_USEC="300000000", WATCHDOG_PID=str(os.getpid())):
        _check("interval is half the timeout",
               sd_watchdog.watchdog_interval_secs() == 150.0)
    with _Env(WATCHDOG_USEC="300000000"):
        _check("no WATCHDOG_PID still arms",
               sd_watchdog.watchdog_interval_secs() == 150.0)
    with _Env(WATCHDOG_USEC="300000000", WATCHDOG_PID=str(os.getpid() + 1)):
        _check("WATCHDOG_PID of another process -> inert",
               sd_watchdog.watchdog_interval_secs() is None)
    with _Env(WATCHDOG_USEC="junk"):
        _check("unparseable WATCHDOG_USEC -> inert",
               sd_watchdog.watchdog_interval_secs() is None)


def check_loop() -> None:
    usec = "200000"   # 0.2s timeout -> 0.1s interval
    pid = str(os.getpid())

    sock, env = _receiver()
    with _Env(NOTIFY_SOCKET=env, WATCHDOG_USEC=usec, WATCHDOG_PID=pid):
        _run_loop(lambda: True)
    got = _drain(sock)
    _check("healthy: READY=1 first", got[:1] == ["READY=1"], repr(got))
    _check("healthy: repeated WATCHDOG=1", got.count("WATCHDOG=1") >= 2, repr(got))

    with _Env(NOTIFY_SOCKET=env, WATCHDOG_USEC=usec, WATCHDOG_PID=pid):
        _run_loop(lambda: False)
    got = _drain(sock)
    _check("unhealthy: no WATCHDOG=1", "WATCHDOG=1" not in got, repr(got))

    def boom() -> bool:
        raise RuntimeError("check is broken")
    logged: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            logged.append(record.getMessage())
    handler = _Capture()
    sd_watchdog.log.addHandler(handler)
    sd_watchdog.log.propagate = False
    try:
        with _Env(NOTIFY_SOCKET=env, WATCHDOG_USEC=usec, WATCHDOG_PID=pid):
            _run_loop(boom)
    finally:
        sd_watchdog.log.removeHandler(handler)
        sd_watchdog.log.propagate = True
    got = _drain(sock)
    _check("raising health check still pings", "WATCHDOG=1" in got, repr(got))
    _check("a broken check is logged once, not every interval",
           sum("health check raised" in m for m in logged) == 1, repr(logged))

    states = iter([False, False])
    logged.clear()
    sd_watchdog.log.addHandler(handler)
    sd_watchdog.log.propagate = False
    try:
        with _Env(NOTIFY_SOCKET=env, WATCHDOG_USEC=usec, WATCHDOG_PID=pid):
            _run_loop(lambda: next(states, True))
    finally:
        sd_watchdog.log.removeHandler(handler)
        sd_watchdog.log.propagate = True
    got = _drain(sock)
    _check("recovery: pings resume after an unhealthy spell",
           "WATCHDOG=1" in got, repr(got))
    _check("recovery: withheld and resumed each logged once",
           sum("withholding" in m for m in logged) == 1
           and sum("pings resumed" in m for m in logged) == 1, repr(logged))

    with _Env(NOTIFY_SOCKET=env):
        _run_loop(lambda: True, secs=0.2)
    _check("no WATCHDOG_USEC: loop sends nothing", _drain(sock) == [])

    with _Env(NOTIFY_SOCKET=env, WATCHDOG_USEC=usec, WATCHDOG_PID=str(os.getpid() + 1)):
        _run_loop(lambda: True, secs=0.2)
    _check("PID mismatch: loop sends nothing", _drain(sock) == [])

    saved = config.SD_WATCHDOG_ENABLED
    config.SD_WATCHDOG_ENABLED = False
    try:
        with _Env(NOTIFY_SOCKET=env, WATCHDOG_USEC=usec, WATCHDOG_PID=pid):
            _run_loop(lambda: True, secs=0.2)
        _check("SD_WATCHDOG_ENABLED off: loop sends nothing", _drain(sock) == [])
    finally:
        config.SD_WATCHDOG_ENABLED = saved
    sock.close()


def check_gate() -> None:
    now = [0.0]
    down = [False]
    gate = sd_watchdog.DownGate(lambda: down[0], 300, clock=lambda: now[0])
    _check("gate: up is healthy", gate())
    down[0] = True
    now[0] = 10
    _check("gate: freshly down is still healthy", gate())
    now[0] = 309
    _check("gate: down inside grace is healthy", gate())
    now[0] = 311
    _check("gate: down past grace is unhealthy", not gate())
    down[0] = False
    _check("gate: recovery is healthy", gate())
    down[0] = True
    now[0] = 400
    _check("gate: a new outage restarts the grace", gate())


class _FakeBot:
    def __init__(self, closed: bool, task_done: bool | None) -> None:
        self._closed = closed
        if task_done is not None:
            self._start_task = type("T", (), {"done": lambda s: task_done})()

    def is_closed(self) -> bool:
        return self._closed


def check_discord_down() -> None:
    _check("no client is not down", not app._discord_is_down(None))
    _check("open client, running task is up",
           not app._discord_is_down(_FakeBot(False, False)))
    _check("closed client is down", app._discord_is_down(_FakeBot(True, False)))
    _check("ended start task is down (login failed)",
           app._discord_is_down(_FakeBot(False, True)))
    _check("no start task recorded, open client is up",
           not app._discord_is_down(_FakeBot(False, None)))


def check_structure() -> None:
    src = (ROOT / "bot" / "app.py").read_text(encoding="utf-8")
    _check("app.run() starts watchdog_loop", "sd_watchdog.watchdog_loop(" in src)
    _check("discord start task is kept on the bot",
           "bot._start_task = asyncio.create_task(_run_discord())" in src)
    unit = (ROOT / "scripts" / "claude-bot.service").read_text(encoding="utf-8")
    keys = [ln.strip() for ln in unit.splitlines() if not ln.lstrip().startswith("#")]
    _check("unit sets WatchdogSec", any(k.startswith("WatchdogSec=") for k in keys))
    _check("unit sets NotifyAccess=main", "NotifyAccess=main" in keys)


def main() -> int:
    check_notify()
    check_interval()
    check_loop()
    check_gate()
    check_discord_down()
    check_structure()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll systemd watchdog checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
