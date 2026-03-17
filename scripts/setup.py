#!/usr/bin/env python3
"""Interactive setup wizard for Claude Code Bot on a new device.

Usage:
    python scripts/setup.py

Only requires a Discord bot token — everything else is auto-detected or uses defaults.
"""

from __future__ import annotations

import platform
import subprocess
import sys
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Required bot permissions (bitfield)
BOT_PERMISSIONS = (
    (1 << 4)   |  # Manage Channels
    (1 << 10)  |  # View Channels
    (1 << 11)  |  # Send Messages
    (1 << 13)  |  # Manage Messages
    (1 << 14)  |  # Embed Links
    (1 << 15)  |  # Attach Files
    (1 << 16)  |  # Read Message History
    (1 << 28)  |  # Manage Roles
    (1 << 34)  |  # Manage Threads
    (1 << 35)  |  # Create Public Threads
    (1 << 36)  |  # Create Private Threads
    (1 << 38)     # Send Messages in Threads
)


def _check_python() -> None:
    if sys.version_info < (3, 11):
        print(f"  x Python 3.11+ required (you have {sys.version})")
        sys.exit(1)
    print(f"  + Python {sys.version_info.major}.{sys.version_info.minor}")


def _check_dependencies() -> None:
    missing = []
    for mod in ("httpx", "discord", "dotenv"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        print(f"  x Missing dependencies: {', '.join(missing)}")
        print("    Run: pip install -e .")
        sys.exit(1)
    print("  + Dependencies installed")


def _get_token() -> tuple[str, dict]:
    """Prompt for token, validate via Discord API. Returns (token, bot_info)."""
    import httpx

    print("\n  Step 1: Discord Bot Token")
    print("  Create a bot at https://discord.com/developers/applications")
    print("  Bot tab -> Reset Token -> copy it\n")
    token = input("  Paste token: ").strip()
    if not token:
        print("  x No token provided")
        sys.exit(1)

    headers = {"Authorization": f"Bot {token}"}
    try:
        r = httpx.get("https://discord.com/api/v10/users/@me", headers=headers)
        r.raise_for_status()
    except Exception:
        print("  x Invalid token — check that you copied the full token")
        sys.exit(1)

    bot_info = r.json()
    bot_name = bot_info["username"]
    client_id = bot_info["id"]
    print(f"  + Token valid — bot: {bot_name} ({client_id})")
    return token, bot_info


def _remind_intents() -> None:
    """Remind user to enable required intents before inviting."""
    print("\n  Step 2: Enable Required Intents")
    print("  In the Developer Portal -> Bot tab, enable BOTH:")
    print("    1. MESSAGE CONTENT INTENT  — required for reading messages")
    print("    2. SERVER MEMBERS INTENT   — required for private channel setup")
    print("  (Scroll down on the Bot tab -> toggle both ON -> Save)")
    input("\n  Press Enter after enabling intents...")


def _invite_bot(client_id: str) -> None:
    """Generate invite URL and open in browser."""
    print("\n  Step 3: Invite Bot to Server")
    invite_url = (
        f"https://discord.com/oauth2/authorize"
        f"?client_id={client_id}"
        f"&permissions={BOT_PERMISSIONS}"
        f"&scope=bot+applications.commands"
    )
    print(f"  {invite_url}\n")
    try:
        webbrowser.open(invite_url)
        print("  (Opened in browser)")
    except Exception:
        print("  (Copy the URL above and open it manually)")
    input("\n  Press Enter after inviting the bot...")


def _select_guild(token: str) -> dict:
    """Auto-detect guild from bot's guild list."""
    import httpx

    print("\n  Step 4: Detect Server")
    headers = {"Authorization": f"Bot {token}"}
    try:
        r = httpx.get("https://discord.com/api/v10/users/@me/guilds", headers=headers)
        r.raise_for_status()
        guilds = r.json()
    except Exception as e:
        print(f"  x Failed to fetch servers: {e}")
        sys.exit(1)

    if not isinstance(guilds, list) or not guilds:
        print("  x Bot is not in any server. Invite it first.")
        sys.exit(1)
    elif len(guilds) == 1:
        guild = guilds[0]
        print(f"  + Found: {guild['name']} ({guild['id']})")
    else:
        print("  Found multiple servers:")
        for i, g in enumerate(guilds, 1):
            print(f"    {i}. {g['name']} ({g['id']})")
        while True:
            choice = input(f"  Select [1-{len(guilds)}]: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(guilds):
                guild = guilds[int(choice) - 1]
                break
            print("  Invalid choice, try again.")
    return guild


def _get_user_id() -> str:
    """Prompt for Discord user ID."""
    print("\n  Step 5: Your Discord User ID")
    print("  (Settings -> Advanced -> Developer Mode ON)")
    print("  (Right-click yourself -> Copy User ID)")
    while True:
        user_id = input("  Your user ID: ").strip()
        if user_id.isdigit() and len(user_id) > 10:
            return user_id
        print("  Invalid — should be a long number like 152516669082697728")


def _get_device_identity() -> tuple[str, str]:
    """Auto-detect PC name and category name."""
    print("\n  Step 6: Device Identity")
    pc_name = platform.node().split(".")[0] or "my-pc"
    category_name = f"Claude Code - {pc_name}"
    print(f"  PC name: {pc_name}")
    print(f"  Category: {category_name}")
    custom = input("  Change category name? (Enter to keep): ").strip()
    if custom:
        category_name = custom
    return pc_name, category_name


def _ask_auto_update() -> bool:
    """Ask if this is a secondary device that should auto-update."""
    print("\n  Step 7: Auto-Update")
    print("  Secondary devices auto-pull code from origin and reboot.")
    print("  Enable this if another PC is your primary dev machine.")
    choice = input("  Is this a secondary device? [y/N]: ").strip().lower()
    return choice in ("y", "yes")


def _check_claude_cli() -> None:
    """Check if claude CLI is available."""
    print("\n  Step 8: Claude CLI")
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        version = result.stdout.strip() or result.stderr.strip()
        print(f"  + claude CLI found: {version}")
    except FileNotFoundError:
        print("  ! claude CLI not found — install it before starting the bot")
        print("    https://docs.anthropic.com/en/docs/claude-code/overview")
    except Exception as e:
        print(f"  ! Could not check claude CLI: {e}")


def _write_env(
    token: str,
    guild_id: str,
    user_id: str,
    category_name: str,
    pc_name: str,
    auto_update: bool,
) -> bool:
    """Write .env file. Returns True if written, False if skipped."""
    env_path = PROJECT_ROOT / ".env"

    if env_path.exists():
        overwrite = input("\n  .env already exists. Overwrite? [y/N]: ").strip().lower()
        if overwrite not in ("y", "yes"):
            print("  Aborted — .env not modified.")
            return False

    lines = [
        "# Generated by scripts/setup.py",
        f"DISCORD_BOT_TOKEN={token}",
        f"DISCORD_GUILD_ID={guild_id}",
        f"DISCORD_USER_ID={user_id}",
        f"DISCORD_CATEGORY_NAME={category_name}",
        f"PC_NAME={pc_name}",
    ]

    if auto_update:
        lines.append("")
        lines.append("# Auto-update: pulls code and reboots when origin changes")
        lines.append("AUTO_UPDATE=true")
        lines.append("AUTO_UPDATE_INTERVAL_SECS=300")

    env_path.write_text("\n".join(lines) + "\n")
    print(f"\n  + Wrote {env_path}")
    return True


def main() -> None:
    print("\n  Claude Code Bot — Setup")
    print("  " + "=" * 30 + "\n")

    # Preflight
    _check_python()
    _check_dependencies()

    # Interactive steps
    token, bot_info = _get_token()
    client_id = bot_info["id"]

    _remind_intents()
    _invite_bot(client_id)

    guild = _select_guild(token)
    user_id = _get_user_id()
    pc_name, category_name = _get_device_identity()
    auto_update = _ask_auto_update()

    _check_claude_cli()

    written = _write_env(
        token=token,
        guild_id=guild["id"],
        user_id=user_id,
        category_name=category_name,
        pc_name=pc_name,
        auto_update=auto_update,
    )

    if not written:
        print("\n  Setup cancelled — run again to retry.\n")
        return

    # Recap
    print("\n  --- Configuration ---")
    print(f"  Server:      {guild['name']}")
    print(f"  Category:    {category_name}")
    print(f"  PC name:     {pc_name}")
    print(f"  Auto-update: {'ON' if auto_update else 'OFF'}")

    print("\n  Setup complete! Start the bot:")
    print("  python -m bot\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Setup cancelled.\n")
        sys.exit(130)
