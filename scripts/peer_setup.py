"""Create the channel the two machines share, or bring its permissions up to date.

    python scripts/peer_setup.py [--peer-bot-id ID]

If no channel named machine-link exists, it creates a top-level text channel with
@everyone denied view, and view, send and read history for this bot, the peer bot
and the owner (DISCORD_USER_ID). Then it prints the id.

If it exists, it compares the two bots' overwrites and changes only those, and only
if they differ: both bots may make and write in threads there, and neither may
Manage Messages or Manage Threads. It prints each change, and says so plainly if
Discord refuses one. It never touches a message or the owner's overwrite, because the
channel is the audit trail. The denies are a default, not a boundary: a bot that
holds Manage Roles could take them back. See docs/design/peer-link.md.
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

from dotenv import dotenv_values  # noqa: E402

from bot.procutil import install_root  # noqa: E402

API = "https://discord.com/api/v10"
CHANNEL_NAME = "machine-link"
OFFICE_BOT_ID = "1484548443878719620"

VIEW, SEND, READ_HISTORY, MANAGE_MESSAGES = 1 << 10, 1 << 11, 1 << 16, 1 << 13
MANAGE_THREADS, CREATE_PUBLIC_THREADS, SEND_IN_THREADS = 1 << 34, 1 << 35, 1 << 38
PERSON_ALLOW = VIEW | SEND | READ_HISTORY
BOT_ALLOW = PERSON_ALLOW | CREATE_PUBLIC_THREADS | SEND_IN_THREADS
BOT_DENY = MANAGE_MESSAGES | MANAGE_THREADS
TEXT_CHANNEL = 0
ROLE, MEMBER = 0, 1


def overwrites(guild_id: str, bot_ids: list, owner_id: str) -> list:
    """Both bots hold Manage Messages and Manage Threads at server level, so the channel denies them."""
    out = [{"id": guild_id, "type": ROLE, "allow": "0", "deny": str(VIEW)}]
    out.extend({"id": str(b), "type": MEMBER, "allow": str(BOT_ALLOW), "deny": str(BOT_DENY)}
               for b in bot_ids)
    out.append({"id": str(owner_id), "type": MEMBER, "allow": str(PERSON_ALLOW), "deny": "0"})
    return out


def overwrite_changes(channel: dict, bot_ids: list) -> list:
    """Each bot's overwrite that is not what it should be, as (id, allow, deny).

    Any other bit someone set by hand stays as it is. Nothing to do gives nothing,
    which is what makes running this twice harmless.
    """
    current = {o["id"]: o for o in channel.get("permission_overwrites", [])}
    changes = []
    for bot_id in dict.fromkeys(str(b) for b in bot_ids):
        over = current.get(bot_id)
        allow, deny = (int(over["allow"]), int(over["deny"])) if over else (0, 0)
        want_allow = (allow | BOT_ALLOW) & ~BOT_DENY
        want_deny = (deny | BOT_DENY) & ~BOT_ALLOW
        if over is None or (want_allow, want_deny) != (allow, deny):
            changes.append((bot_id, want_allow, want_deny))
    return changes


def update_existing(call, channel: dict, bot_ids: list) -> int:
    """Bring an existing channel's bot overwrites up to date. 0 if it all landed."""
    print(f"exists {channel['id']}")
    code = 0
    changes = overwrite_changes(channel, bot_ids)
    if not changes:
        print("bot permissions already correct, nothing changed")
    for bot_id, allow, deny in changes:
        try:
            call("PUT", f"/channels/{channel['id']}/permissions/{bot_id}",
                 {"type": MEMBER, "allow": str(allow), "deny": str(deny)})
            print(f"updated the overwrite for bot {bot_id}")
        except urllib.error.HTTPError as exc:
            print(f"Discord refused the change for bot {bot_id}: HTTP {exc.code}. Give it Create "
                  "Public Threads and Send Messages in Threads, and deny Manage Messages and "
                  "Manage Threads, in the channel's settings by hand.")
            code = 5
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--peer-bot-id", default=OFFICE_BOT_ID)
    args = parser.parse_args()
    env = dotenv_values(install_root(_SCRIPT_ROOT) / ".env")
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
            raw = resp.read()  # Setting an overwrite answers 204, with no body.
            return json.loads(raw.decode("utf-8")) if raw else None

    try:
        me = call("GET", "/users/@me")["id"]
        for channel in call("GET", f"/guilds/{guild}/channels"):
            if channel.get("name") == CHANNEL_NAME and channel.get("type") == TEXT_CHANNEL:
                return update_existing(call, channel, [me, args.peer_bot_id])
        created = call("POST", f"/guilds/{guild}/channels", {
            "name": CHANNEL_NAME, "type": TEXT_CHANNEL,
            "permission_overwrites": overwrites(guild, [me, args.peer_bot_id], owner),
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
