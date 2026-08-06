"""Post-reboot smoke test for the bot.

Checks that the bot started cleanly and is responsive.
Exit 0 = healthy, exit 1 = problems found.

Usage:
  python scripts/smoke_test.py              # log-only checks
  python scripts/smoke_test.py --respond    # also send a test message and wait for reply
"""

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import re
import sys
import time

# ---------------------------------------------------------------------------
# Config — read .env the same way discord_test.py does (no heavy deps)
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPT_ROOT = os.path.dirname(_SCRIPT_DIR)

# Health is a property of the *installed* bot. Run from a build worktree this
# used to read the worktree's own data/logs/bot.log — which is empty, and has
# no .env beside it either — and report UNHEALTHY for a bot that is running
# perfectly well. Fall back to the old behaviour if the helper is unavailable
# for any reason; a health check must never fail to run.
try:
    sys.path.insert(0, _SCRIPT_ROOT)
    from bot.procutil import install_root
    from pathlib import Path

    _PROJECT_ROOT = str(install_root(Path(_SCRIPT_ROOT)))
except Exception:
    _PROJECT_ROOT = _SCRIPT_ROOT

_LOG_FILE = os.path.join(_PROJECT_ROOT, "data", "logs", "bot.log")

ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")
_env: dict[str, str] = {}
try:
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                _env[k.strip()] = v.strip()
except FileNotFoundError:
    pass

LOBBY_WEBHOOK_URL = _env.get("TEST_LOBBY_WEBHOOK_URL")
DISCORD_BOT_TOKEN = _env.get("DISCORD_BOT_TOKEN")
LOBBY_ID = _env.get("DISCORD_LOBBY_CHANNEL_ID")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_log_lines(max_bytes: int = 12 * 1024 * 1024) -> list[str]:
    """Read the log, from the whole file if it fits.

    This used to take the last 200 lines, which made the check *less* reliable
    the better the bot was doing: after eleven hours of healthy uptime the
    "Bot ready" line had scrolled 2,500 lines out of reach, so a bot that was
    talking to Discord that same second was reported as never having started.
    Since `.claude/test.json` tells the verify step to run `start` when the
    log says the bot is down, a false negative here points at booting a second
    instance of a singleton.

    The cap is a backstop, not a window: the log rotates at 10 MB, so this
    reads the whole of a normal one.
    """
    try:
        size = os.path.getsize(_LOG_FILE)
        with open(_LOG_FILE, encoding="utf-8", errors="replace") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard the partial line landed in
            return f.readlines()
    except OSError:
        # FileNotFoundError (no log yet) or PermissionError (locked during rotation)
        return []


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")

# How far back of "Bot ready" a boot can plausibly reach when the log holds no
# explicit start marker. Real boots here take about thirty seconds; five
# minutes is slack, not a window anyone should be relying on.
_BOOT_WINDOW_S = 300.0


def _line_time(line: str) -> float | None:
    """Epoch seconds for a log line, or None for continuation lines."""
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def _split_log(lines: list[str]) -> tuple[list[str], list[str]]:
    """Split the log into (this boot's startup, everything logged after it).

    Bounded at *both* ends on purpose. Everything after the bot finished
    coming up is ordinary runtime, and folding that in meant any later error —
    a git push timing out in some unrelated repo nine hours in — was reported
    as a startup failure.

    The backward walk is also bounded by *time*. It used to rely solely on
    finding a start marker, and neither marker it looked for had ever been
    written to a log, so it ran back to the previous boot's "Bot ready" and
    called ten hours of runtime a startup sequence. The marker is now really
    emitted (`bot/app.py`, on taking the PID lock) but historic logs and any
    bot that predates it still need the clock as a backstop.
    """
    ready_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if "Bot ready" in lines[i]:
            ready_idx = i
            break

    if ready_idx is None:
        # Never came up, or is still coming up. Recent lines are the evidence.
        return lines[-200:], []

    ready_at = _line_time(lines[ready_idx])
    marker_idx = None   # an explicit start marker, if this log has one
    clock_idx = None    # first line too old to belong to this boot
    floor_idx = ready_idx

    for i in range(ready_idx - 1, -1, -1):
        if "Acquired PID lock" in lines[i] or "Starting bot" in lines[i]:
            marker_idx = i
            floor_idx = i
            break
        # A PREVIOUS "Bot ready" is a different boot entirely.
        if "Bot ready" in lines[i]:
            floor_idx = i + 1
            break
        ts = _line_time(lines[i])
        if clock_idx is None and ready_at is not None and ts is not None \
                and ready_at - ts > _BOOT_WINDOW_S:
            clock_idx = i + 1
        floor_idx = i

    # The marker is authoritative wherever it appears: a boot that took a
    # quarter of an hour is unusual, not evidence that the log is lying. The
    # clock only stands in for a marker that was never written.
    if marker_idx is not None:
        start_idx = marker_idx
    elif clock_idx is not None:
        start_idx = clock_idx
    else:
        start_idx = floor_idx

    return lines[start_idx:ready_idx + 1], lines[ready_idx + 1:]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

class CheckResult:
    def __init__(self, name: str, passed: bool, detail: str = "", partial: bool = False):
        self.name = name
        self.passed = passed
        self.detail = detail
        self.partial = partial  # True = degraded but not a failure

    def __str__(self):
        if self.partial:
            tag = "[PARTIAL]"
        elif self.passed:
            tag = "[PASS]"
        else:
            tag = "[FAIL]"
        s = f"{tag} {self.name}"
        if self.detail:
            s += f"\n       {self.detail}"
        return s


def check_process_alive() -> CheckResult:
    """Is there actually a bot process? The most direct evidence there is.

    The log tells you what happened; this tells you what is true now. Nothing
    here checked it before, so every verdict rested on reading old text —
    which is how a bot that was live on Discord that same second could be
    reported as down.
    """
    pid_file = os.path.join(_PROJECT_ROOT, "data", "bot.pid")
    try:
        with open(pid_file, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError) as exc:
        return CheckResult("Bot process", False, f"No usable PID file ({exc})")

    try:
        from bot.procutil import is_bot_process, is_process_alive
    except Exception as exc:  # pragma: no cover — helper must never break the check
        return CheckResult("Bot process", True,
                           f"Skipped — cannot import procutil: {exc}", partial=True)

    if not is_process_alive(pid):
        return CheckResult("Bot process", False, f"PID {pid} is not running")
    if not is_bot_process(pid, _PROJECT_ROOT):
        return CheckResult("Bot process", False,
                           f"PID {pid} was recycled onto another process")
    return CheckResult("Bot process", True, f"Running as PID {pid}")


def check_runtime_errors(runtime_lines: list[str]) -> CheckResult:
    """Errors logged *after* the bot came up.

    Reported, but never fatal: these are the bot doing its job and hitting
    something — a repo whose push timed out, an API hiccup — not evidence that
    this build broke it. Rolling them into the startup verdict is what made a
    healthy bot read UNHEALTHY.

    Only the last 200 runtime lines are scanned. A long-lived bot accumulates
    thousands, and listing every one it ever hit tells you nothing about the
    build you just shipped.
    """
    tail = runtime_lines[-200:]

    log_level_re = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (ERROR|CRITICAL)\s")
    errors = [ln.strip()[:150] for ln in tail if log_level_re.match(ln)]
    if not errors:
        return CheckResult("No recent runtime errors", True,
                           f"Scanned {len(tail)} of {len(runtime_lines)} lines since startup")
    detail = f"{len(errors)} since startup (not a startup failure):\n" + "\n".join(
        f"       - {e}" for e in errors[:5]
    )
    return CheckResult("No recent runtime errors", True, detail, partial=True)


def check_bot_ready(startup_lines: list[str]) -> CheckResult:
    """Check that 'Bot ready' appears in the current startup."""
    for line in startup_lines:
        if "Bot ready" in line:
            # Extract the interesting bits
            clean = line.strip()
            return CheckResult("Bot ready", True, clean[-120:])
    return CheckResult("Bot ready", False, "No 'Bot ready' line found in recent logs")


def check_startup_errors(startup_lines: list[str]) -> CheckResult:
    """Check for ERROR/CRITICAL/Traceback in startup logs."""
    errors: list[str] = []
    # Match log-level field in standard format: "2026-03-16 14:30:00 ERROR   bot.app: msg"
    log_level_re = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (ERROR|CRITICAL)\s")
    for line in startup_lines:
        if log_level_re.match(line):
            errors.append(line.strip()[:150])
        elif "Traceback (most recent call last)" in line:
            errors.append(line.strip()[:150])

    if not errors:
        return CheckResult("No startup errors", True, f"Scanned {len(startup_lines)} lines")
    detail = f"{len(errors)} error(s) found:\n" + "\n".join(f"       - {e}" for e in errors[:5])
    if len(errors) > 5:
        detail += f"\n       ... and {len(errors) - 5} more"
    return CheckResult("No startup errors", False, detail)


def check_platforms(startup_lines: list[str]) -> CheckResult:
    """Check which platforms connected successfully."""
    platforms: list[str] = []
    for line in startup_lines:
        if "Bot ready" in line:
            # Extract platforms from "platforms: discord"
            m = re.search(r"platforms:\s*(.+)", line)
            if m:
                platforms = [p.strip() for p in m.group(1).split(",")]
    if platforms:
        return CheckResult("Platforms connected", True, ", ".join(platforms))
    return CheckResult("Platforms connected", False, "Could not determine connected platforms")


def check_bot_responding() -> CheckResult:
    """Send a test message via webhook and check for bot response.

    Requires TEST_LOBBY_WEBHOOK_URL and DISCORD_BOT_TOKEN in .env.
    """
    if not LOBBY_WEBHOOK_URL:
        return CheckResult(
            "Bot responding", True,
            "Skipped — TEST_LOBBY_WEBHOOK_URL not configured (log-only mode)",
            partial=True,
        )
    if not DISCORD_BOT_TOKEN or not LOBBY_ID:
        return CheckResult(
            "Bot responding", True,
            "Skipped — DISCORD_BOT_TOKEN or DISCORD_LOBBY_CHANNEL_ID not set",
            partial=True,
        )

    # Import discord_test helpers (same directory).
    # Catch broadly: discord_test calls sys.exit(1) at module level if .env/TOKEN missing.
    sys.path.insert(0, _SCRIPT_DIR)
    try:
        import discord_test  # noqa: F811
    except (ImportError, SystemExit, Exception) as exc:
        return CheckResult("Bot responding", True,
                           f"Skipped — could not import discord_test: {exc}", partial=True)

    # Wrap all API interaction — network errors shouldn't crash the smoke test.
    try:
        return _probe_bot_response(discord_test)
    except Exception as exc:
        return CheckResult("Bot responding", False,
                           f"API error during response check: {exc}")


def _probe_bot_response(discord_test) -> CheckResult:
    """Send a test message and poll for a bot reply. May raise on network errors."""
    # Record last message ID before sending
    msgs = discord_test.api_call("GET", f"/channels/{LOBBY_ID}/messages?limit=1")
    last_id = msgs[0]["id"] if isinstance(msgs, list) and msgs else "0"

    # Send test message
    test_msg = f"[smoke-test] ping {int(time.time())}"
    result = discord_test.webhook_send(LOBBY_WEBHOOK_URL, test_msg)
    if "id" not in result:
        return CheckResult("Bot responding", False,
                           f"Failed to send test message: {result}")

    # Poll for bot response (up to 20s)
    deadline = time.time() + 20
    while time.time() < deadline:
        time.sleep(3)
        msgs = discord_test.api_call("GET", f"/channels/{LOBBY_ID}/messages?limit=5")
        if not isinstance(msgs, list):
            continue
        for m in msgs:
            if int(m["id"]) > int(last_id) and m["author"].get("bot"):
                content = m.get("content", "")[:100]
                return CheckResult("Bot responding", True,
                                   f"Got response: {content}")

    return CheckResult("Bot responding", False,
                       "No bot response within 20s after sending test message")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(respond: bool = False) -> int:
    """Run all checks. Returns 0 if healthy, 1 if problems found."""
    print("=" * 50)
    print("Bot Smoke Test")
    print("=" * 50)

    startup_lines, runtime_lines = _split_log(_read_log_lines())

    results: list[CheckResult] = []

    # Core checks (always run)
    results.append(check_process_alive())
    results.append(check_bot_ready(startup_lines))
    results.append(check_startup_errors(startup_lines))
    results.append(check_platforms(startup_lines))
    results.append(check_runtime_errors(runtime_lines))

    # Response check (opt-in or when webhooks are available)
    if respond:
        results.append(check_bot_responding())

    # Print results
    print()
    for r in results:
        print(r)
        print()

    # Summary
    failed = [r for r in results if not r.passed]
    partial = [r for r in results if r.partial]
    passed = [r for r in results if r.passed and not r.partial]

    print("-" * 50)
    if failed:
        print(f"UNHEALTHY — {len(failed)} check(s) failed")
        return 1
    elif partial:
        print(f"PARTIAL — {len(passed)} passed, {len(partial)} degraded (no webhook)")
        return 0
    else:
        print(f"HEALTHY — {len(passed)} check(s) passed")
        return 0


def main():
    respond = "--respond" in sys.argv
    sys.exit(run(respond=respond))


if __name__ == "__main__":
    main()
