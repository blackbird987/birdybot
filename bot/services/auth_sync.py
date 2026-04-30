"""Cross-instance Claude CLI credential sync via Discord messages.

Allows one bot instance to push its Claude CLI OAuth credentials to Discord,
and another instance to pull and apply them — fixing expired auth remotely.

Credentials are encrypted with Fernet using a key derived from DISCORD_GUILD_ID
(shared across all instances).  Messages use the format:

    [AUTH_SYNC:from=<pc_name>] <encrypted_base64_blob>

Security model:
- Encrypted in transit (Fernet symmetric)
- Stored briefly in a private Discord channel
- Deleted immediately after consumption
- Never touches git
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from bot import config

if TYPE_CHECKING:
    import discord

    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

CREDENTIALS_PATH = Path.home() / config.PROVIDER_DIR_NAME / ".credentials.json"
AUTH_PREFIX = "[AUTH_SYNC:from="

# ---------------------------------------------------------------------------
# Encryption helpers (Fernet via cryptography, with stdlib fallback)
# ---------------------------------------------------------------------------

def _derive_key(shared_secret: str) -> bytes:
    """Derive a 32-byte Fernet key from a shared secret (guild ID)."""
    digest = hashlib.sha256(shared_secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def _has_cryptography() -> bool:
    try:
        from cryptography.fernet import Fernet  # noqa: F401
        return True
    except ImportError:
        return False


def encrypt(data: dict, shared_secret: str) -> str:
    """Encrypt credentials dict → base64 string."""
    raw = json.dumps(data).encode()
    if _has_cryptography():
        from cryptography.fernet import Fernet
        return Fernet(_derive_key(shared_secret)).encrypt(raw).decode()
    # Fallback: base64 only (still better than plaintext)
    log.warning("cryptography not installed — using base64 encoding (not encrypted)")
    return base64.urlsafe_b64encode(raw).decode()


def decrypt(payload: str, shared_secret: str) -> dict:
    """Decrypt base64 string → credentials dict."""
    if _has_cryptography():
        from cryptography.fernet import Fernet
        raw = Fernet(_derive_key(shared_secret)).decrypt(payload.encode())
        return json.loads(raw)
    # Fallback: base64 only
    raw = base64.urlsafe_b64decode(payload.encode())
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Credential I/O
# ---------------------------------------------------------------------------

def read_credentials() -> dict | None:
    """Read local Claude CLI credentials. Returns None if missing."""
    if not CREDENTIALS_PATH.exists():
        return None
    try:
        return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to read credentials file")
        return None


def write_credentials(data: dict) -> bool:
    """Write credentials to Claude CLI path. Creates dir if needed."""
    try:
        CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CREDENTIALS_PATH.write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
        log.info("Wrote credentials to %s", CREDENTIALS_PATH)
        return True
    except Exception:
        log.exception("Failed to write credentials")
        return False


def verify_cli() -> bool:
    """Check if Claude CLI can authenticate (runs `claude --version`)."""
    try:
        result = subprocess.run(
            [config.CLAUDE_BINARY, "--version"],
            capture_output=True, text=True, timeout=15,
            **config.NOWND,
        )
        return result.returncode == 0
    except Exception:
        log.debug("CLI verify failed", exc_info=True)
        return False


def credentials_look_valid() -> bool:
    """Quick check: credentials file exists and has a refreshToken."""
    creds = read_credentials()
    if not creds:
        return False
    oauth = creds.get("claudeAiOauth", {})
    return bool(oauth.get("refreshToken"))


# ---------------------------------------------------------------------------
# Message format helpers
# ---------------------------------------------------------------------------

def build_message(source_pc: str, encrypted_payload: str) -> str:
    """Build the Discord message string."""
    return f"{AUTH_PREFIX}{source_pc}] {encrypted_payload}"


def parse_message(content: str) -> tuple[str, str] | None:
    """Parse an AUTH_SYNC message → (source_pc, encrypted_payload) or None."""
    if not content.startswith(AUTH_PREFIX):
        return None
    try:
        rest = content[len(AUTH_PREFIX):]
        bracket_idx = rest.index("]")
        source_pc = rest[:bracket_idx]
        payload = rest[bracket_idx + 2:]  # skip "] "
        return source_pc, payload.strip()
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Discord operations (async, require running bot)
# ---------------------------------------------------------------------------

async def push_credentials(bot: ClaudeBot, channel_id: int) -> str | None:
    """Read local credentials, encrypt, and post to a Discord channel.

    Returns the posted message content on success, None on failure.
    """
    creds = read_credentials()
    if not creds:
        return None

    secret = str(config.DISCORD_GUILD_ID)
    encrypted = encrypt(creds, secret)
    msg_text = build_message(config.PC_NAME, encrypted)

    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        await channel.send(msg_text)
        log.info("Auth credentials pushed to channel %s", channel_id)
        return msg_text
    except Exception:
        log.exception("Failed to push auth credentials")
        return None


async def pull_credentials(
    bot: ClaudeBot, channel_id: int
) -> str | None:
    """Scan channel for AUTH_SYNC messages, consume if found.

    Skips messages from this PC_NAME (don't consume own push).
    Returns source PC name on success, None if nothing found.
    """
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        async for msg in channel.history(limit=50):
            parsed = parse_message(msg.content)
            if not parsed:
                continue
            source_pc, payload = parsed
            if source_pc == config.PC_NAME:
                continue  # skip own push

            # Decrypt and write
            secret = str(config.DISCORD_GUILD_ID)
            try:
                creds = decrypt(payload, secret)
            except Exception:
                log.warning("Failed to decrypt AUTH_SYNC from %s", source_pc)
                continue

            if not write_credentials(creds):
                continue

            # Clean up the message
            try:
                await msg.delete()
                log.info("Deleted consumed AUTH_SYNC message from %s", source_pc)
            except Exception:
                log.warning("Failed to delete AUTH_SYNC message", exc_info=True)

            return source_pc

    except Exception:
        log.exception("Failed to pull auth credentials")

    return None


async def startup_auth_check(bot: ClaudeBot) -> None:
    """Startup hook: if local auth looks broken, try to pull from Discord.

    Called once during bot startup, after Discord is ready.
    Non-fatal — exceptions are caught by the caller.
    Claude-specific — skipped for other providers.
    """
    if config.PROVIDER != "claude":
        log.debug("Auth sync skipped — not using Claude provider")
        return

    if credentials_look_valid():
        log.debug("Local CLI credentials look valid — skipping auth sync")
        return

    log.warning("Local CLI credentials missing or invalid — checking for AUTH_SYNC")

    if not bot._lobby_channel_id:
        log.warning("No lobby channel — cannot check for auth sync")
        return

    source = await pull_credentials(bot, bot._lobby_channel_id)
    if source:
        ok = verify_cli()
        status = "verified" if ok else "written but CLI verify failed"
        msg = f"\U0001f511 Auth restored on **{config.PC_NAME}** from {source} — {status}"
        log.info("Auth sync from %s: %s", source, status)
        # Broadcast to lobby
        if hasattr(bot, "_notifier") and bot._notifier:
            await bot._notifier.broadcast(msg)
    else:
        log.info("No AUTH_SYNC messages found — credentials still missing")


# ---------------------------------------------------------------------------
# Multi-account status (per CLAUDE_CONFIG_DIR)
# ---------------------------------------------------------------------------

@dataclass
class AccountStatus:
    """Snapshot of a single CLAUDE_CONFIG_DIR's auth state."""
    path: str                          # absolute config dir path
    label: str                         # short display label (e.g. ".claude")
    logged_in: bool                    # credentials.json present + has refreshToken
    email: str | None = None           # from .claude.json oauthAccount
    org: str | None = None             # organizationName
    account_uuid: str | None = None    # accountUuid
    cooldown_until: datetime | None = None  # tz-aware UTC
    error: str | None = None           # any read error to surface


def _read_account_identity(account_dir: Path) -> tuple[str | None, str | None, str | None]:
    """Read (email, org, accountUuid) from <dir>/.claude.json. None on miss."""
    cfg_path = account_dir / ".claude.json"
    if not cfg_path.exists():
        return None, None, None
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        log.debug("Failed to read %s", cfg_path, exc_info=True)
        return None, None, None
    oauth = data.get("oauthAccount") or {}
    return (
        oauth.get("emailAddress"),
        oauth.get("organizationName"),
        oauth.get("accountUuid"),
    )


def _check_credentials_file(account_dir: Path) -> bool:
    """True if <dir>/.credentials.json has a refreshToken."""
    cred_path = account_dir / ".credentials.json"
    if not cred_path.exists():
        return False
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    oauth = data.get("claudeAiOauth") or {}
    return bool(oauth.get("refreshToken"))


async def collect_account_statuses(
    account_dirs: list[str],
    cooldowns: dict[str, datetime] | None = None,
) -> list[AccountStatus]:
    """Build AccountStatus list for the given dirs (off the event loop)."""
    cooldowns = cooldowns or {}

    def _build() -> list[AccountStatus]:
        out: list[AccountStatus] = []
        for raw in account_dirs:
            p = Path(raw).expanduser()
            label = p.name or str(p)
            try:
                logged_in = _check_credentials_file(p)
                email, org, uuid_ = _read_account_identity(p)
                out.append(AccountStatus(
                    path=str(p),
                    label=label,
                    logged_in=logged_in,
                    email=email,
                    org=org,
                    account_uuid=uuid_,
                    cooldown_until=cooldowns.get(str(p)) or cooldowns.get(raw),
                ))
            except Exception as exc:
                out.append(AccountStatus(
                    path=str(p), label=label, logged_in=False, error=str(exc),
                ))
        return out

    return await asyncio.to_thread(_build)


# ---------------------------------------------------------------------------
# Console / login terminal helpers
# ---------------------------------------------------------------------------

def host_can_show_console() -> bool:
    """Best-effort: can this host pop up an interactive terminal window?

    On Windows we want all three: an active console session, the bot
    process is attached to an interactive window station (not Services-0x0),
    and SESSIONNAME is set (i.e. running as a logged-in user, not SYSTEM).
    On POSIX a DISPLAY/WAYLAND_DISPLAY is required for a GUI terminal.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            session_id = ctypes.windll.wtsapi32.WTSGetActiveConsoleSessionId()
            if session_id == 0xFFFFFFFF:
                return False
            user32 = ctypes.windll.user32
            hwinsta = user32.GetProcessWindowStation()
            if not hwinsta:
                return False
            # SESSIONNAME unset on Service accounts
            if not os.environ.get("SESSIONNAME"):
                return False
            return True
        except Exception:
            log.debug("host_can_show_console: Windows probe failed", exc_info=True)
            return False
    # POSIX
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def launch_login_terminal(account_dir: str) -> bool:
    """Spawn an interactive terminal window running `claude /login` for *account_dir*.

    Returns True if the spawn was attempted successfully.  The user must
    complete the OAuth flow in that window — we do not block.
    """
    binary = config.CLAUDE_BINARY
    p = Path(account_dir).expanduser()
    try:
        if sys.platform == "win32":
            CREATE_NEW_CONSOLE = 0x00000010
            env = os.environ.copy()
            env["CLAUDE_CONFIG_DIR"] = str(p)
            cmd = f'cmd.exe /k "set CLAUDE_CONFIG_DIR={p} && \"{binary}\""'
            subprocess.Popen(
                cmd, shell=True, env=env, creationflags=CREATE_NEW_CONSOLE,
            )
            log.info("Spawned login terminal for %s", p)
            return True
        # POSIX — try a few common terminal emulators
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(p)
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
            try:
                subprocess.Popen([term, "-e", binary], env=env)
                log.info("Spawned %s for login on %s", term, p)
                return True
            except FileNotFoundError:
                continue
        log.warning("No terminal emulator found to launch login for %s", p)
        return False
    except Exception:
        log.exception("Failed to spawn login terminal for %s", p)
        return False
