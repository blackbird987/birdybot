"""Post-reboot smoke test for the bot.

Checks that the bot started cleanly and is responsive.
Exit 0 = healthy, exit 1 = problems found.

Usage:
  python scripts/smoke_test.py              # log-only checks
  python scripts/smoke_test.py --respond    # also send a test message and wait for reply
"""

import os
import re
import sys
import time

# ---------------------------------------------------------------------------
# Config — read .env the same way discord_test.py does (no heavy deps)
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
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

def _read_log_lines(max_lines: int = 200) -> list[str]:
    """Read last N lines from bot.log."""
    try:
        with open(_LOG_FILE, encoding="utf-8", errors="replace") as f:
            return f.readlines()[-max_lines:]
    except OSError:
        # FileNotFoundError (no log yet) or PermissionError (locked during rotation)
        return []


def _find_last_startup(lines: list[str]) -> list[str]:
    """Return log lines from the most recent startup onward.

    Scans backward for 'Bot ready' and then further back to find the
    process start (PID lock or first log line of that run).
    """
    # Find the last "Bot ready" line
    ready_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if "Bot ready" in lines[i]:
            ready_idx = i
            break

    if ready_idx is None:
        # No "Bot ready" found — return all lines (startup may be in progress)
        return lines

    # Walk backward from ready to find start of this boot (PID lock or start of log)
    start_idx = ready_idx
    for i in range(ready_idx - 1, -1, -1):
        if "Acquired PID lock" in lines[i] or "Starting bot" in lines[i]:
            start_idx = i
            break
        # Also stop if we hit a PREVIOUS "Bot ready" — that's a different boot
        if "Bot ready" in lines[i]:
            start_idx = i + 1
            break
        start_idx = i

    return lines[start_idx:]


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

    all_lines = _read_log_lines(200)
    startup_lines = _find_last_startup(all_lines)

    results: list[CheckResult] = []

    # Core checks (always run)
    results.append(check_bot_ready(startup_lines))
    results.append(check_startup_errors(startup_lines))
    results.append(check_platforms(startup_lines))

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
