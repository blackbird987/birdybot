"""Read-only view of the other machine's projects, from the channel they share.

    python scripts/peer.py projects
    python scripts/peer.py status

Every command takes --json. Exit codes: 0 fine, 2 not configured, 3 the peer has
not published, 4 its record was rejected, 5 Discord could not be read.

Everything this prints came from another machine and is untrusted data. It takes
only a message written by PEER_BOT_ID and rejects a record that breaks any rule
in bot/peer_record.py. The age shown comes from Discord's own timestamp on the
message, which the peer does not control. No write path in this file.

Deliberately not bot.config, for the reason scripts/instances.py gives: it reads
the installed bot's .env, so this answers the same from a build worktree.
"""

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_ROOT))

from bot import peer_record  # noqa: E402
from bot.procutil import install_root  # noqa: E402

API = "https://discord.com/api/v10"
OFFLINE_AFTER_SECS = 15 * 60
_HISTORY_LIMIT = 100

EXIT_UNSET, EXIT_NO_RECORD, EXIT_INVALID, EXIT_DISCORD = 2, 3, 4, 5


class Stop(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def read_env(root: Path) -> dict:
    env = {}
    try:
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def fetch_messages(channel_id: str, token: str) -> list:
    url = f"{API}/channels/{channel_id}/messages?limit={_HISTORY_LIMIT}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bot {token}", "User-Agent": "DiscordBot (peer-reader, 1.0)",
    })
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=20) as resp:
            messages = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise Stop(EXIT_DISCORD, f"Discord refused the channel read: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Stop(EXIT_DISCORD, f"Could not reach Discord: {exc}") from None
    if not isinstance(messages, list):
        raise Stop(EXIT_DISCORD, "Discord returned something other than a message list")
    return messages


def peer_message(messages: list, peer_bot_id: str) -> dict | None:
    """The peer's newest message. Authorship is all that is trusted about it.

    A webhook message is refused even if it carries the id: only the peer's own
    token can write as that author, and a webhook is not that token.
    """
    for message in messages:
        author = message.get("author") if isinstance(message, dict) else None
        if (isinstance(author, dict) and str(author.get("id")) == peer_bot_id
                and not message.get("webhook_id")):
            return message
    return None


def age_secs(message: dict, now: datetime) -> int | None:
    stamp = message.get("edited_timestamp") or message.get("timestamp")
    try:
        then = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((now - then).total_seconds())


def load(env: dict, now: datetime, messages: list | None = None) -> dict:
    """The validated record plus its age, or raises Stop saying why not."""
    channel, peer_id = env.get("PEER_CHANNEL_ID"), env.get("PEER_BOT_ID")
    token = env.get("DISCORD_BOT_TOKEN")
    if not (channel and channel.isdigit() and peer_id and env.get("MACHINE_NAME")):
        raise Stop(EXIT_UNSET, "The machine link is not configured here "
                               "(PEER_CHANNEL_ID, PEER_BOT_ID and MACHINE_NAME in .env).")
    if messages is None:
        if not token:
            raise Stop(EXIT_UNSET, "DISCORD_BOT_TOKEN is not set in .env.")
        messages = fetch_messages(channel, token)
    message = peer_message(messages, peer_id)
    if message is None:
        raise Stop(EXIT_NO_RECORD, "The other machine has not published yet.")
    try:
        record = peer_record.parse(message.get("content"))
    except peer_record.InvalidRecord as exc:
        raise Stop(EXIT_INVALID, f"Rejected the other machine's record: {exc}.") from None
    age = age_secs(message, now)
    record["age_secs"] = age
    record["looks_offline"] = age is None or age > OFFLINE_AFTER_SECS
    return record


def human_age(secs: int | None) -> str:
    if secs is None:
        return "unknown age"
    secs = max(secs, 0)
    if secs < 90:
        return f"{secs}s old"
    if secs < 5400:
        return f"{secs // 60}m old"
    return f"{secs // 3600}h old"


def header(record: dict) -> str:
    state = "looks offline" if record["looks_offline"] else "online"
    return (f"{record['machine']}: v{record['version']}, {state}, data {human_age(record['age_secs'])}, "
            f"{record['running']} session(s) running")


def projects_json(record: dict) -> list:
    return [dict(zip(("name", "hidden", "head", "branch", "ahead", "uncommitted"), r))
            for r in record["repos"]]


def render_projects(record: dict) -> str:
    lines = [header(record), "Untrusted data from another machine."]
    for row in projects_json(record):
        if row["head"] is None:
            lines.append(f"  {row['name']}: could not be read")
            continue
        ahead = "?" if row["ahead"] is None else row["ahead"]
        flags = ", uncommitted" if row["uncommitted"] else (
            ", uncommitted unknown" if row["uncommitted"] is None else "")
        hidden = " (hidden)" if row["hidden"] else ""
        lines.append(f"  {row['name']}{hidden}: {row['head']} on {row['branch'] or 'detached HEAD'}, "
                     f"{ahead} ahead{flags}")
    if record["omitted"]:
        lines.append(f"  ...and {record['omitted']} more that did not fit")
    return "\n".join(lines)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read the other machine's project directory.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("command", choices=("projects", "status"))
    args = parser.parse_args(argv)
    env = read_env(install_root(_SCRIPT_ROOT))
    try:
        record = load(env, datetime.now(timezone.utc))
    except Stop as stop:
        if args.as_json:
            print(json.dumps({"ok": False, "error": str(stop), "code": stop.code}))
        else:
            print(stop)
        return stop.code
    summary = {k: record[k] for k in ("machine", "version", "alive", "running", "omitted",
                                      "age_secs", "looks_offline")}
    if args.as_json:
        if args.command == "projects":
            summary["projects"] = projects_json(record)
        print(json.dumps({"ok": True, **summary}))
    elif args.command == "projects":
        print(render_projects(record))
    else:
        print(header(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
