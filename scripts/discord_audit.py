"""Read-only report of who could command this PC through the bot's Discord server.

Whoever can speak as the owner in the bot's category gets a bypass-mode Claude
session on this machine. That is the owner's account, and (while test mode is
armed) the test webhooks. Around them sit the powers that reach those: server
administrators, anyone who can manage webhooks (they can copy any webhook's
URL), manage the server or manage roles, server-wide or in one channel. This
prints all of it, so a change can show up in a routine check rather than after
the fact.

Usage:
  python scripts/discord_audit.py                  plain-text report
  python scripts/discord_audit.py --json           the same, as JSON
  python scripts/discord_audit.py --diff old.json  only what changed since a
                                                   saved --json report

Read-only by omission: the only request this file can make is a GET, and
scripts/test_discord_audit.py asserts that against the source. The bot token is
read from the installed bot's .env and never printed, and webhook tokens and
URLs are dropped before anything is reported.

Exit 0 on a report (findings are not failures, and a --diff with no earlier
report says so), 2 when the token is missing, Discord cannot be reached, or the
earlier report cannot be parsed.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

_SCRIPT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_ROOT))

# Not bot.config (side effects, worktree-relative paths): stdlib-only helpers.
from bot import test_webhook_gate  # noqa: E402
from bot.procutil import install_data_dir, install_root, read_dotenv  # noqa: E402

API = "https://discord.com/api/v10"

ADMINISTRATOR = 1 << 3
MANAGE_GUILD = 1 << 5
MANAGE_ROLES = 1 << 28
MANAGE_WEBHOOKS = 1 << 29
POWERS = (
    ("admin", ADMINISTRATOR),
    ("manage_webhooks", MANAGE_WEBHOOKS),
    ("manage_server", MANAGE_GUILD),
    ("manage_roles", MANAGE_ROLES),
)
# What a channel's permission overwrite can add on its own. Administrator cannot
# be granted per channel; Manage Roles there is "Manage Permissions".
CHANNEL_POWERS = (
    ("manage_webhooks", MANAGE_WEBHOOKS),
    ("manage_roles", MANAGE_ROLES),
)
POWER_LABELS = {
    "admin": "Administrator",
    "manage_webhooks": "Manage Webhooks",
    "manage_server": "Manage Server",
    "manage_roles": "Manage Roles",
    "owner": "server owner",
}
MFA_LABELS = {0: "not required for moderators", 1: "required for moderators"}


class AuditError(Exception):
    """Token missing or Discord unreachable: exit 2 with this message."""


# --- reading ----------------------------------------------------------------


def _get(token: str, path: str):
    req = urllib.request.Request(
        API + path,
        headers={"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (audit, 1.0)"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and path in ("/applications/@me", "/users/@me/guilds"):
            raise AuditError(f"Discord refused the bot token (HTTP {e.code})") from None
        return {"_error": f"HTTP {e.code} on {path.split('?')[0]}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AuditError(f"Could not reach Discord: {type(e).__name__}") from None


def _members(token: str, guild_id: str) -> list | dict:
    out: list = []
    after = "0"
    for _ in range(50):  # 50,000 members is far past any server this bot is in
        page = _get(token, f"/guilds/{guild_id}/members?limit=1000&after={after}")
        if isinstance(page, dict):
            return page  # even after earlier pages: a short list would hide holders
        out.extend(page)
        if len(page) < 1000:
            break
        after = page[-1]["user"]["id"]
    return out


def fetch(env: dict[str, str]) -> dict:
    token = env.get("DISCORD_BOT_TOKEN")
    guild_id = env.get("DISCORD_GUILD_ID")
    if not token:
        raise AuditError("DISCORD_BOT_TOKEN is not set in the bot's .env")
    if not guild_id:
        raise AuditError("DISCORD_GUILD_ID is not set in the bot's .env")
    app = _get(token, "/applications/@me")
    guilds = _get(token, "/users/@me/guilds")
    return {
        "guild": _get(token, f"/guilds/{guild_id}?with_counts=true"),
        "roles": _get(token, f"/guilds/{guild_id}/roles"),
        "members": _members(token, guild_id),
        "webhooks": _get(token, f"/guilds/{guild_id}/webhooks"),
        "channels": _get(token, f"/guilds/{guild_id}/channels"),
        "app": app,
        "guilds": guilds,
    }


# --- analysis (pure, tested offline) ----------------------------------------


def _powers(perms: int) -> list[str]:
    if perms & ADMINISTRATOR:
        return [name for name, _ in POWERS]  # Administrator implies the rest
    return [name for name, bit in POWERS if perms & bit]


def analyze(raw: dict, *, guild_id: str, owner_id: str | None,
            test_webhook_ids: set[str], armed: bool) -> dict:
    """Turn raw API data into the report. Never copies a token or URL."""
    guild = raw.get("guild") if isinstance(raw.get("guild"), dict) else {}
    errors = [f"{k}: {v['_error']}" for k, v in raw.items()
              if isinstance(v, dict) and v.get("_error")]
    roles = {r["id"]: r for r in raw.get("roles") or [] if isinstance(r, dict)}
    everyone_perms = int(roles.get(guild_id, {}).get("permissions", 0) or 0)
    guild_owner = guild.get("owner_id")

    app = raw.get("app") if isinstance(raw.get("app"), dict) else {}
    # A bot user shares its application's id: this is the bot running here.
    self_id = str(app.get("id") or "")

    holders = []
    members = raw.get("members") if isinstance(raw.get("members"), list) else []
    for m in members:
        user = m.get("user") or {}
        uid = str(user.get("id", ""))
        perms = everyone_perms
        for rid in m.get("roles") or []:
            perms |= int(roles.get(rid, {}).get("permissions", 0) or 0)
        powers = _powers(perms)
        if uid and uid == guild_owner:
            powers = ["owner", *[p for p in powers if p != "owner"]]
        if not powers:
            continue
        if owner_id and uid == owner_id:
            kind = "me"
        elif self_id and uid == self_id:
            kind = "this bot"
        else:
            kind = "bot" if user.get("bot") else "human"
        holders.append({"id": uid, "name": user.get("username", "?"),
                        "kind": kind, "powers": powers})
    holders.sort(key=lambda h: ({"me": 0, "this bot": 1, "human": 2, "bot": 3}[h["kind"]], h["name"].lower()))

    channels = {c["id"]: c.get("name", "?") for c in raw.get("channels") or []
                if isinstance(c, dict)}
    webhooks = []
    for w in raw.get("webhooks") or []:
        if not isinstance(w, dict):
            continue
        wid = str(w.get("id", ""))
        webhooks.append({  # deliberately field by field: no token, no url
            "id": wid,
            "name": w.get("name") or "?",
            "channel": channels.get(str(w.get("channel_id")), str(w.get("channel_id"))),
            "creator": (w.get("user") or {}).get("username", "?"),
            "test_webhook": wid in test_webhook_ids,
        })
    webhooks.sort(key=lambda w: (not w["test_webhook"], w["name"].lower(), w["id"]))

    # A channel's permission overwrites can grant Manage Webhooks (copy that
    # channel's webhook links) or Manage Permissions to someone who has
    # neither server-wide, so the holders list alone would miss them.
    member_names = {str((m.get("user") or {}).get("id", "")): (m.get("user") or {}).get("username", "?")
                    for m in members}
    channel_grants = []
    for c in raw.get("channels") or []:
        if not isinstance(c, dict):
            continue
        for ow in c.get("permission_overwrites") or []:
            powers = [name for name, bit in CHANNEL_POWERS if int(ow.get("allow", 0) or 0) & bit]
            if not powers:
                continue
            tid = str(ow.get("id", ""))
            is_role = int(ow.get("type", 0) or 0) == 0
            channel_grants.append({
                "channel_id": str(c.get("id", "")),
                "channel": c.get("name", "?"),
                "target_id": tid,
                "target": (roles.get(tid, {}).get("name", tid) if is_role else member_names.get(tid, tid)),
                "target_kind": "role" if is_role else "member",
                "powers": powers,
            })
    channel_grants.sort(key=lambda g: (g["channel"].lower(), g["target"].lower()))

    other_guilds = sorted(
        g.get("name", "?") for g in raw.get("guilds") or []
        if isinstance(g, dict) and str(g.get("id")) != guild_id
    )
    return {
        "guild": {
            "name": guild.get("name", "?"),
            "owner_is_me": bool(owner_id) and guild_owner == owner_id,
            "mfa_level": guild.get("mfa_level"),
            "verification_level": guild.get("verification_level"),
            "member_count": guild.get("approximate_member_count"),
        },
        "everyone_powers": _powers(everyone_perms),
        "holders": holders,
        "channel_grants": channel_grants,
        "webhooks": webhooks,
        "bot_public": app.get("bot_public"),
        "bot_require_code_grant": app.get("bot_require_code_grant"),
        "other_guilds": other_guilds,
        "test_mode_armed": armed,
        "errors": errors,
    }


def _powers_text(powers: list[str]) -> str:
    return ", ".join(POWER_LABELS.get(p, p) for p in powers)


def render(report: dict) -> str:
    g = report["guild"]
    lines = [
        f"Server: {g['name']} ({g['member_count']} members), "
        f"{'you own it' if g['owner_is_me'] else 'you do NOT own it'}",
        f"2FA: {MFA_LABELS.get(g['mfa_level'], g['mfa_level'])}; "
        f"verification level {g['verification_level']}",
        f"@everyone powers: {_powers_text(report['everyone_powers']) or 'none'}",
        f"Bot is public (anyone can invite it): {report['bot_public']}",
        f"Other servers the bot is in (it ignores them): "
        f"{', '.join(report['other_guilds']) or 'none'}",
        f"Test mode: {'ARMED, test webhooks act as you' if report['test_mode_armed'] else 'off'}",
        "",
        "Members with powers:",
    ]
    for h in report["holders"]:
        lines.append(f"  {h['kind']:<8} {h['name']}: {_powers_text(h['powers'])}")
    lines += ["", "Channel-level grants (Manage Webhooks there lets them copy that channel's links):"]
    for g in report["channel_grants"]:
        lines.append(f"  #{g['channel']}: {g['target_kind']} {g['target']}: {_powers_text(g['powers'])}")
    if not report["channel_grants"]:
        lines.append("  none")
    lines += ["", "Webhooks (anyone with Manage Webhooks can copy these links):"]
    for w in report["webhooks"]:
        tag = "  [test webhook: acts as you while test mode is armed]" if w["test_webhook"] else ""
        lines.append(f"  {w['name']} in #{w['channel']}, made by {w['creator']}{tag}")
    if report["errors"]:
        lines += ["", "Could not read: " + "; ".join(report["errors"])]
    return "\n".join(lines)


def diff(old: dict, new: dict) -> list[str]:
    """What changed between two reports, one line each. Empty when nothing did."""
    out: list[str] = []
    old_h = {h["id"]: h for h in old.get("holders", [])}
    new_h = {h["id"]: h for h in new.get("holders", [])}
    for uid, h in new_h.items():
        if uid not in old_h:
            out.append(f"New: {h['kind']} {h['name']} has {_powers_text(h['powers'])}")
        elif set(h["powers"]) != set(old_h[uid]["powers"]):
            out.append(f"Changed: {h['name']} now has {_powers_text(h['powers']) or 'no powers'} "
                       f"(was {_powers_text(old_h[uid]['powers'])})")
    for uid, h in old_h.items():
        if uid not in new_h:
            out.append(f"Gone: {h['kind']} {h['name']} no longer has powers")
    old_w = {w["id"]: w for w in old.get("webhooks", [])}
    new_w = {w["id"]: w for w in new.get("webhooks", [])}
    for wid, w in new_w.items():
        if wid not in old_w:
            out.append(f"New webhook: {w['name']} in #{w['channel']}, made by {w['creator']}"
                       + (" (test webhook)" if w["test_webhook"] else ""))
    for wid, w in old_w.items():
        if wid not in new_w:
            out.append(f"Webhook removed: {w['name']} in #{w['channel']}")
    def _grant_key(g):
        return (g["channel_id"], g["target_id"])
    old_g = {_grant_key(g): g for g in old.get("channel_grants", [])}
    new_g = {_grant_key(g): g for g in new.get("channel_grants", [])}
    for key, g in new_g.items():
        if key not in old_g or set(g["powers"]) != set(old_g[key]["powers"]):
            out.append(f"Channel grant: {g['target_kind']} {g['target']} has "
                       f"{_powers_text(g['powers'])} in #{g['channel']}")
    for key, g in old_g.items():
        if key not in new_g:
            out.append(f"Channel grant removed: {g['target_kind']} {g['target']} in #{g['channel']}")
    og, ng = old.get("guild", {}), new.get("guild", {})
    for key, label in (("mfa_level", "2FA level"), ("verification_level", "verification level"),
                       ("owner_is_me", "you own the server")):
        if og.get(key) != ng.get(key):
            out.append(f"Changed: {label} {og.get(key)} -> {ng.get(key)}")
    for key, label in (("bot_public", "bot is public"), ("everyone_powers", "@everyone powers"),
                       ("other_guilds", "other servers the bot is in")):
        if old.get(key) != new.get(key):
            out.append(f"Changed: {label} {old.get(key)} -> {new.get(key)}")
    return out


# --- entry ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    ap.add_argument("--diff", metavar="OLD_JSON", help="print only changes since a saved --json report")
    args = ap.parse_args(argv)

    root = install_root(_SCRIPT_ROOT)
    env = read_dotenv(root)
    try:
        raw = fetch(env)
    except AuditError as e:
        print(f"discord_audit: {e}", file=sys.stderr)
        return 2
    report = analyze(
        raw,
        guild_id=env.get("DISCORD_GUILD_ID", ""),
        owner_id=env.get("DISCORD_USER_ID"),
        # Parsed exactly as bot/config.py does, so the flag matches what the bot trusts.
        test_webhook_ids=set(filter(None, env.get("TEST_WEBHOOK_IDS", "").split(","))),
        armed=test_webhook_gate.is_armed(install_data_dir(root)),
    )
    if args.diff:
        old_path = Path(args.diff)
        if not old_path.exists():
            print(f"No earlier report at {args.diff} to compare with. Save one with --json.")
            return 0
        try:
            old = json.loads(old_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"discord_audit: could not read {args.diff}: {type(e).__name__}", file=sys.stderr)
            return 2
        changes = diff(old, report)
        print("\n".join(changes) if changes else "No changes since the last report.")
    elif args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
