"""Harness: the health check must judge *this* boot, and only this boot.

The check reports on a window of the log, and every bug it has had was a bug
about that window:

- it read a fixed 200 lines from the end, so the better the bot's uptime the
  further "Bot ready" scrolled out of reach, and an eleven-hour-old healthy bot
  was reported as never having started;
- it hunted for start markers ("Acquired PID lock", "Starting bot") that no
  version of the bot had ever written, so it ran back to the *previous* boot's
  "Bot ready" and judged the current startup by ten hours of unrelated runtime.

Both failures point the same way: `.claude/test.json` tells the verify step to
run `start` when the log says the bot is down, and this project is a singleton.
A false "down" is a second bot on one Discord token.

Usage: python scripts/test_smoke_window.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_test import (  # noqa: E402  (needs the path above)
    _split_log,
    check_bot_ready,
    check_runtime_errors,
    check_startup_errors,
)


def _log(*rows: str) -> list[str]:
    """Build log lines from 'HH:MM:SS LEVEL message' shorthand, all one day."""
    return [f"2026-08-06 {r}\n" for r in rows]


PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------


def case_previous_boot_excluded() -> None:
    """The real shape of the bug: two boots, errors belonging to the first."""
    lines = _log(
        "15:08:59 INFO    bot.app: Bot ready — platforms: discord",
        "21:55:08 ERROR   bot.engine.deploy: push timed out",
        "00:35:34 ERROR   bot.claude.runner: transient API failure",
        "01:04:08 WARNING bot.app: CLAUDE_ACCOUNTS entry sidelined: /x",
        "01:04:39 INFO    bot.app: Bot ready — platforms: discord",
        "01:05:00 INFO    bot.discord.bot: Synced slash commands",
        "10:40:12 ERROR   bot.engine.deploy: push timed out again",
    )
    startup, runtime = _split_log(lines)

    check("startup window stops at the previous boot",
          all("push timed out" not in ln and "transient API" not in ln for ln in startup),
          f"{len(startup)} lines")
    check("startup window ends at 'Bot ready'", "Bot ready" in startup[-1])
    check("this boot's own lines are inside it",
          any("sidelined" in ln for ln in startup))
    check("later runtime is split out, not dropped",
          any("push timed out again" in ln for ln in runtime) and len(runtime) == 2)

    check("clean boot passes despite older errors", check_startup_errors(startup).passed)
    check("bot-ready found", check_bot_ready(startup).passed)

    rt = check_runtime_errors(runtime)
    check("runtime errors are reported but never fatal", rt.passed and rt.partial,
          rt.detail.splitlines()[0])


def case_no_start_marker_uses_the_clock() -> None:
    """No marker at all — the boot is still bounded, by time."""
    lines = _log(
        "01:00:00 ERROR   bot.engine.deploy: hours before this boot",
        "01:04:08 INFO    bot.app: reconciling accounts",
        "01:04:39 INFO    bot.app: Bot ready — platforms: discord",
    )
    # 01:00:00 is 279s before ready, inside the 300s slack; push it out.
    lines[0] = lines[0].replace("01:00:00", "00:50:00")
    startup, _ = _split_log(lines)
    check("a distant error is outside the boot window",
          all("hours before" not in ln for ln in startup), f"{len(startup)} lines")
    check("nearby boot lines are kept",
          any("reconciling accounts" in ln for ln in startup))


def case_marker_wins_over_the_clock() -> None:
    """When the marker is present it is authoritative, slow boot or not."""
    lines = _log(
        "00:00:00 ERROR   bot.app: from the previous run",
        "00:50:00 INFO    bot.app: Acquired PID lock (PID 1234)",
        "00:51:00 INFO    bot.app: loading state",
        "01:04:39 INFO    bot.app: Bot ready — platforms: discord",
    )
    startup, _ = _split_log(lines)
    check("window opens exactly at the PID-lock marker",
          "Acquired PID lock" in startup[0], startup[0].strip()[-40:])
    check("a 15-minute boot is not truncated by the clock", len(startup) == 3)


def case_ready_far_from_the_end() -> None:
    """The original bug: 'Bot ready' thousands of lines back."""
    lines = (
        _log("01:04:39 INFO    bot.app: Bot ready — platforms: discord")
        + _log(*[f"01:0{i % 6}:00 INFO    bot.x: chatter {i}" for i in range(3000)])
    )
    startup, runtime = _split_log(lines)
    check("'Bot ready' still found under 3000 lines of chatter",
          check_bot_ready(startup).passed)
    check("all the chatter lands in runtime, not startup",
          len(startup) == 1 and len(runtime) == 3000, f"{len(startup)}/{len(runtime)}")
    check("runtime scan is capped at 200 lines",
          "200 of 3000" in check_runtime_errors(runtime).detail)


def case_never_came_up() -> None:
    """No 'Bot ready' anywhere — must fail loudly, not silently pass."""
    lines = _log(
        "01:04:08 INFO    bot.app: starting",
        "01:04:10 CRITICAL bot.app: could not connect to Discord",
    )
    startup, runtime = _split_log(lines)
    check("no-ready is reported as not ready", not check_bot_ready(startup).passed)
    check("the crash is caught as a startup error", not check_startup_errors(startup).passed)
    check("nothing is misfiled as runtime", runtime == [])

    empty_startup, empty_runtime = _split_log([])
    check("an empty log does not crash the check",
          not check_bot_ready(empty_startup).passed and empty_runtime == [])


def case_sidelined_account_is_not_a_failure() -> None:
    """A signed-out backup account is handled, so it must not fail health.

    It is logged at WARNING (`bot/app.py`) precisely so this stays true; at
    ERROR it made every startup on this machine read as a failed startup while
    the second Claude account was logged out.
    """
    startup, _ = _split_log(_log(
        "01:04:08 WARNING bot.app: CLAUDE_ACCOUNTS entry sidelined: /x (no refresh token)",
        "01:04:08 WARNING bot.app: Multi-account failover degraded: 1 of 2 accounts usable",
        "01:04:39 INFO    bot.app: Bot ready — platforms: discord",
    ))
    check("degraded failover does not fail the health check",
          check_startup_errors(startup).passed)

    startup, _ = _split_log(_log(
        "01:04:08 ERROR   bot.app: CLAUDE_ACCOUNTS entry dropped: /x (no such directory)",
        "01:04:39 INFO    bot.app: Bot ready — platforms: discord",
    ))
    check("a genuinely broken account entry still fails it",
          not check_startup_errors(startup).passed)


def main() -> int:
    print("Health-check log window\n")
    for case in (
        case_previous_boot_excluded,
        case_no_start_marker_uses_the_clock,
        case_marker_wins_over_the_clock,
        case_ready_far_from_the_end,
        case_never_came_up,
        case_sidelined_account_is_not_a_failure,
    ):
        print(case.__name__)
        case()
        print()

    print("-" * 50)
    if FAILED:
        print(f"FAIL — {len(FAILED)} of {len(PASSED) + len(FAILED)} checks failed")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print(f"PASS — {len(PASSED)} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
