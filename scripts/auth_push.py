#!/usr/bin/env python3
"""Standalone script to push Claude CLI credentials to Discord.

Zero bot dependencies — uses only urllib + stdlib.  Run immediately after
``git push`` to post credentials before auto-update reboots the bot.

Usage:
    python scripts/auth_push.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env manually (no dotenv dependency)
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


def _load_env(path: Path) -> None:
    """Minimal .env parser — sets os.environ for KEY=VALUE lines."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env(_ENV_FILE)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
LOBBY_CHANNEL_ID = os.environ.get("DISCORD_LOBBY_CHANNEL_ID", "")
PC_NAME = os.environ.get("PC_NAME", "") or __import__("platform").node()

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
API = "https://discord.com/api/v10"

_ssl_ctx = ssl.create_default_context()


def _http(method: str, url: str, body: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bot {TOKEN}",
        "User-Agent": "DiscordBot (auth-push, 1.0)",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = {"error": str(e)}
        print(f"HTTP {e.code}: {err_body}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Encryption (matches bot/services/auth_sync.py)
# ---------------------------------------------------------------------------

def _derive_key(shared_secret: str) -> bytes:
    digest = hashlib.sha256(shared_secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _encrypt(data: dict, shared_secret: str) -> str:
    raw = json.dumps(data).encode()
    try:
        from cryptography.fernet import Fernet
        return Fernet(_derive_key(shared_secret)).encrypt(raw).decode()
    except ImportError:
        print("WARNING: cryptography not installed — using base64 (not encrypted)")
        return base64.urlsafe_b64encode(raw).decode()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    if not GUILD_ID:
        print("ERROR: DISCORD_GUILD_ID not set", file=sys.stderr)
        sys.exit(1)
    if not LOBBY_CHANNEL_ID:
        print("ERROR: DISCORD_LOBBY_CHANNEL_ID not set", file=sys.stderr)
        sys.exit(1)

    if not CREDENTIALS_PATH.exists():
        print(f"ERROR: No credentials at {CREDENTIALS_PATH}", file=sys.stderr)
        sys.exit(1)

    creds = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    oauth = creds.get("claudeAiOauth", {})
    if not oauth.get("refreshToken"):
        print("ERROR: Credentials file has no refreshToken", file=sys.stderr)
        sys.exit(1)

    encrypted = _encrypt(creds, GUILD_ID)
    message = f"[AUTH_SYNC:from={PC_NAME}] {encrypted}"

    print(f"Pushing credentials from {PC_NAME} to channel {LOBBY_CHANNEL_ID}...")
    result = _http("POST", f"{API}/channels/{LOBBY_CHANNEL_ID}/messages",
                    {"content": message})

    msg_id = result.get("id", "?")
    print(f"OK — message {msg_id} posted to The Ark")
    print(f"Other bot instances can now pull via Claude Login button or reboot.")


if __name__ == "__main__":
    main()
