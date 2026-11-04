"""Create the channel the two machines share. Run once; safe to run again.

    python scripts/peer_setup.py [--peer-bot-id ID]

If no channel named machine-link exists, it creates a top-level text channel with
@everyone denied view, and view, send and read history for this bot, the peer bot
and the owner (DISCORD_USER_ID). Then it prints the id. Nothing else. It never
grants Manage Messages, and it does not change a channel that already exists:
the channel is the audit trail. See docs/design/peer-link.md.
"""

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

_SCRIPT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_ROOT))

from bot.procutil import install_root  # noqa: E402

API = "https://discord.com/api/v10"
CHANNEL_NAME = "machine-link"
OFFICE_BOT_ID = "1484548443878719620"

VIEW, SEND, READ_HISTORY = 1 << 10, 1 << 11, 1 << 16
TEXT_CHANNEL = 0
ROLE, MEMBER = 0, 1


def read_env(root: Path) -> dict:
    env = {}
    for line in (root / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def overwrites(guild_id: str, member_ids: list) -> list:
    allow = str(VIEW | SEND | READ_HISTORY)
    out = [{"id": guild_id, "type": ROLE, "allow": "0", "deny": str(VIEW)}]
    out.extend({"id": str(m), "type": MEMBER, "allow": allow, "deny": "0"} for m in member_ids)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--peer-bot-id", default=OFFICE_BOT_ID)
    args = parser.parse_args()
    env = read_env(install_root(_SCRIPT_ROOT))
    token, guild, owner = (env.get(k) for k in
                           ("DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "DISCORD_USER_ID"))
    if not (token and guild and owner):
        print("Needs DISCORD_BOT_TOKEN, DISCORD_GUILD_ID and DISCORD_USER_ID in .env.")
        return 2

    def call(method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"{API}{path}", data=data, method=method, headers={
            "Authorization": f"Bot {token}", "Content-Type": "application/json",
            "User-Agent": "DiscordBot (peer-setup, 1.0)",
        })
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        for channel in call("GET", f"/guilds/{guild}/channels"):
            if channel.get("name") == CHANNEL_NAME and channel.get("type") == TEXT_CHANNEL:
                print(f"exists {channel['id']}")
                return 0
        me = call("GET", "/users/@me")["id"]
        created = call("POST", f"/guilds/{guild}/channels", {
            "name": CHANNEL_NAME, "type": TEXT_CHANNEL,
            "permission_overwrites": overwrites(guild, [me, args.peer_bot_id, owner]),
        })
    except urllib.error.HTTPError as exc:
        print(f"Discord refused: HTTP {exc.code}")
        return 5
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"Could not reach Discord: {exc}")
        return 5
    print(f"created {created['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
