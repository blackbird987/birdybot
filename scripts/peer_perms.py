"""What each machine's bot can do in Discord, computed from the live guild.

    python scripts/peer_perms.py               the live state
    python scripts/peer_perms.py --simulate    the state after the proposed role change
    python scripts/peer_perms.py --activity    also this bot's own recent posts elsewhere

Every command takes --json. Exit 0 means no finding, 1 means a finding, 5 means
Discord could not be read.

Read-only: this file only issues GET requests. A bot cannot edit its own top
role, so the role change is the owner's, and --simulate prints the steps.

It answers four questions for each bot: can it see the other bot's category,
can it give itself more access (Manage Roles with its top role above a role that
has Administrator or can see the other category), can it remove the other bot
from the server, and does it still have what the code uses in its own category. The permissions are computed by Discord's
algorithm from what REST returns, not exercised. See docs/design/peer-link.md.
"""

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

_SCRIPT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_ROOT))

from bot.procutil import install_root  # noqa: E402

API = "https://discord.com/api/v10"

KICK, BAN, ADMINISTRATOR, MANAGE_CHANNELS, MANAGE_GUILD = 1 << 1, 1 << 2, 1 << 3, 1 << 4, 1 << 5
ADD_REACTIONS, VIEW, SEND, MANAGE_MESSAGES, EMBED = 1 << 6, 1 << 10, 1 << 11, 1 << 13, 1 << 14
ATTACH, READ_HISTORY, MENTION_EVERYONE = 1 << 15, 1 << 16, 1 << 17
MANAGE_ROLES, MANAGE_WEBHOOKS, MANAGE_THREADS = 1 << 28, 1 << 29, 1 << 34
CREATE_PUBLIC_THREADS, SEND_IN_THREADS, PIN_MESSAGES = 1 << 35, 1 << 38, 1 << 51
ALL_PERMISSIONS = (1 << 53) - 1

PERM_NAMES = {
    KICK: "Kick Members", BAN: "Ban Members", ADMINISTRATOR: "Administrator",
    MANAGE_CHANNELS: "Manage Channels", MANAGE_GUILD: "Manage Server",
    ADD_REACTIONS: "Add Reactions", VIEW: "View Channels", SEND: "Send Messages",
    MANAGE_MESSAGES: "Manage Messages", EMBED: "Embed Links", ATTACH: "Attach Files",
    READ_HISTORY: "Read Message History", MENTION_EVERYONE: "Mention Everyone",
    MANAGE_ROLES: "Manage Roles", MANAGE_WEBHOOKS: "Manage Webhooks",
    MANAGE_THREADS: "Manage Threads", CREATE_PUBLIC_THREADS: "Create Public Threads",
    SEND_IN_THREADS: "Send Messages in Threads", PIN_MESSAGES: "Pin Messages",
}

# With its top role above the other bot's, either of these removes that bot from
# the server. Nothing under bot/ or scripts/ calls them, so the proposal takes them.
REMOVES_MEMBERS = KICK | BAN
# The running bot uses none of these (only scripts/discord_test.py setup-webhook
# makes a webhook, by hand). The ones left after the proposal are reported.
DANGEROUS = ADMINISTRATOR | KICK | BAN | MANAGE_GUILD | MENTION_EVERYONE | MANAGE_WEBHOOKS

# What the code uses inside its own category, and where. Pin Messages is its own
# bit: Discord's permissions table describes Manage Messages as deletion only,
# and lists pinning under bit 51 (read 2026-11-04).
REQUIRED_IN_OWN = {
    VIEW: "everything",
    SEND: "channel.send everywhere",
    READ_HISTORY: "lobby cleanup, fetch_message, the publisher's history scan",
    EMBED: "dashboard and result embeds",
    ATTACH: "discord.File uploads, the /image directive",
    ADD_REACTIONS: "bot/discord/memes.py",
    MANAGE_CHANNELS: "create_forum, create_text_channel, channel edits",
    MANAGE_THREADS: "thread archive, rename, forum post pins",
    MANAGE_MESSAGES: "lobby.purge in bot/discord/bot.py",
    MANAGE_ROLES: "set_permissions and overwrites= in bot/discord/channels.py",
    CREATE_PUBLIC_THREADS: "create_thread",
    SEND_IN_THREADS: "every session thread",
    PIN_MESSAGES: "msg.pin in bot/discord/dashboard.py and bot/monitor/service.py",
}
# What the code needs from the server as a whole, not from a channel.
REQUIRED_AT_SERVER = {
    MANAGE_CHANNELS: "guild.create_category in bot/discord/channels.py",
    MANAGE_ROLES: "member.add_roles, the guest lockdown in bot/discord/bot.py",
}


@dataclass(frozen=True)
class BotSpec:
    label: str
    user_id: str
    own_role_id: str            # the role it keeps
    drop_role_ids: tuple        # the roles the proposal takes off it
    category_id: str
    own_name_part: str          # also covers its archive category, by name


BOTS = (
    BotSpec("TT-GPT", "696352579894116392", "1085310033736249396", (),
            "1481317112957108226", "foundry"),
    BotSpec("office bot", "1484548443878719620", "1484549635526889474",
            ("825720550198083596", "1085310033736249396"),
            "1484551301445452009", "office"),
)


def names(mask: int) -> list:
    return [name for bit, name in sorted(PERM_NAMES.items()) if mask & bit]


def base_permissions(guild_id: str, roles: dict, role_ids) -> int:
    """Discord's base: @everyone plus every role held. Administrator is everything."""
    perms = roles[guild_id]["perms"]
    for role_id in role_ids:
        if role_id in roles:
            perms |= roles[role_id]["perms"]
    return ALL_PERMISSIONS if perms & ADMINISTRATOR else perms


def channel_permissions(base: int, guild_id: str, overwrites: list, role_ids, user_id: str) -> int:
    """Discord's overwrite order: @everyone, then roles as one, then the member."""
    if base & ADMINISTRATOR:
        return ALL_PERMISSIONS
    perms = base
    by_id = {o["id"]: o for o in overwrites}
    everyone = by_id.get(guild_id)
    if everyone:
        perms = (perms & ~int(everyone["deny"])) | int(everyone["allow"])
    allow = deny = 0
    for role_id in role_ids:
        over = by_id.get(role_id)
        if over and int(over.get("type", 0)) == 0:
            allow |= int(over["allow"])
            deny |= int(over["deny"])
    perms = (perms & ~deny) | allow
    member = by_id.get(user_id)
    if member and int(member.get("type", 1)) == 1:
        perms = (perms & ~int(member["deny"])) | int(member["allow"])
    return perms if perms & VIEW else 0


def top_position(roles: dict, role_ids) -> float:
    return max((roles[r]["position"] for r in role_ids if r in roles), default=0)


def escalations(spec: BotSpec, peer: BotSpec, guild_id: str, roles: dict, member_roles: list,
                channels: list) -> list:
    """Ways this bot could give itself more with the roles it holds."""
    base = base_permissions(guild_id, roles, member_roles)
    if not base & (MANAGE_ROLES | ADMINISTRATOR):
        return []
    top = top_position(roles, member_roles)
    found = []
    for role_id, role in roles.items():
        # A managed role belongs to an integration and cannot be assigned at all.
        if (role_id == guild_id or role_id in member_roles or role["position"] >= top
                or role.get("managed")):
            continue
        if role["perms"] & ADMINISTRATOR:
            found.append(f"can assign itself {role['name']} ({role['position']}), which has Administrator")
    peer_ids = {c["id"] for c in channels if in_category(c, peer)}
    seen = set()
    for channel in channels:
        if channel["id"] not in peer_ids:
            continue
        for over in channel.get("permission_overwrites", []):
            role = roles.get(over["id"])
            if (int(over.get("type", 0)) == 0 and role and over["id"] != guild_id
                    and over["id"] not in member_roles and role["position"] < top
                    and not role.get("managed")
                    and int(over["allow"]) & VIEW and over["id"] not in seen):
                seen.add(over["id"])
                found.append(f"can assign itself {role['name']} ({role['position']}), "
                             f"which can see {peer.label}'s category")
    return found


def in_category(channel: dict, spec: BotSpec) -> bool:
    return channel["_category_id"] in spec_categories(channel["_all"], spec)


def spec_categories(categories: dict, spec: BotSpec) -> set:
    return {cid for cid, name in categories.items()
            if cid == spec.category_id or spec.own_name_part in name.lower()}


@dataclass
class Report:
    label: str
    roles: list
    sees: dict = field(default_factory=dict)      # category name -> visible channels
    sees_peer: bool = False
    escalations: list = field(default_factory=list)
    missing_own: dict = field(default_factory=dict)   # permission -> channels lacking it
    missing_server: list = field(default_factory=list)
    own_channels: int = 0
    dangerous: list = field(default_factory=list)
    removes_peer: list = field(default_factory=list)


def assess(spec: BotSpec, peer: BotSpec, guild_id: str, roles: dict, member_roles: list,
           channels: list, peer_roles: list = ()) -> Report:
    base = base_permissions(guild_id, roles, member_roles)
    report = Report(spec.label, [roles[r]["name"] for r in member_roles if r in roles])
    categories = channels[0]["_all"] if channels else {}
    for channel in channels:
        perms = channel_permissions(base, guild_id, channel.get("permission_overwrites", []),
                                    member_roles, spec.user_id)
        name = categories.get(channel["_category_id"], "(no category)")
        if perms & VIEW:
            report.sees[name] = report.sees.get(name, 0) + 1
            if in_category(channel, peer):
                report.sees_peer = True
        if in_category(channel, spec):
            report.own_channels += 1
            for bit in REQUIRED_IN_OWN:
                if not perms & bit:
                    label = PERM_NAMES[bit]
                    report.missing_own[label] = report.missing_own.get(label, 0) + 1
    report.dangerous = names(base & DANGEROUS)
    report.missing_server = [PERM_NAMES[b] for b in REQUIRED_AT_SERVER if not base & b]
    report.escalations = escalations(spec, peer, guild_id, roles, member_roles, channels)
    if top_position(roles, member_roles) > top_position(roles, peer_roles):
        report.removes_peer = names(base & REMOVES_MEMBERS)
    return report


def simulate(guild_id: str, roles: dict, members: dict, channels: list,
             losers: dict | None = None) -> tuple:
    """Apply the proposal to copies. Returns (roles, members, steps).

    The permissions to tick are derived: remove the Administrator roles, see what
    the code would then lack, and add exactly that.
    """
    roles = {rid: dict(role) for rid, role in roles.items()}
    members = {uid: list(held) for uid, held in members.items()}
    keep = {spec.own_role_id for spec in BOTS}
    for role_id in keep:
        roles[role_id]["_had_removal"] = roles[role_id]["perms"] & REMOVES_MEMBERS
        roles[role_id]["perms"] &= ~(ADMINISTRATOR | REMOVES_MEMBERS)
    lowest_admin = min(r["position"] for rid, r in roles.items()
                       if r["perms"] & ADMINISTRATOR and rid != guild_id and not r.get("managed"))

    steps = []
    for role_id, people in (losers or {}).items():
        if people:
            steps.append(
                f"Decide about {', '.join(sorted(people))} first: it holds role "
                f"{roles[role_id]['name']} and has no other Administrator, so the steps below "
                "take its Administrator away. If it needs that, give it a role of its own placed "
                f"above {roles[role_id]['name']} (placed below, that role is an escalation again).")
    for spec in BOTS:
        held = [r for r in members[spec.user_id]
                if r not in spec.drop_role_ids and r == spec.own_role_id]
        members[spec.user_id] = held
        role = roles[spec.own_role_id]
        peer = BOTS[1] if spec is BOTS[0] else BOTS[0]
        before = assess(spec, peer, guild_id, roles, held, channels)
        needed = 0
        for bit, name in PERM_NAMES.items():
            if name in before.missing_own or name in before.missing_server:
                needed |= bit
        role["perms"] |= needed
        if needed:
            steps.append(f"On role {role['name']}, turn on: {', '.join(names(needed))}.")
        if role["position"] >= lowest_admin:
            lowest = next(r["name"] for r in roles.values()
                          if r["position"] == lowest_admin and not r.get("managed"))
            steps.append(f"Drag role {role['name']} to just below {lowest}, the lowest "
                         "assignable Administrator role.")
            role["position"] = lowest_admin - 0.5
    for spec in BOTS:
        role = roles[spec.own_role_id]
        if role.get("_was_admin"):
            steps.append(f"On role {role['name']}, turn off Administrator.")
        if role.get("_had_removal"):
            steps.append(f"On role {role['name']}, turn off {', '.join(names(role['_had_removal']))}: "
                         "the bot does not use them and they would let it remove the other bot.")
        unused = role["perms"] & DANGEROUS
        if unused:
            steps.append(f"Optional, on role {role['name']}: turn off {', '.join(names(unused))}. "
                         "The running bot uses none of them, but other holders of the role may.")
        for role_id in spec.drop_role_ids:
            steps.append(f"Take role {roles[role_id]['name']} off {spec.label}.")
    return roles, members, steps


class Rest:
    def __init__(self, token: str):
        self.token = token

    def get(self, path: str):
        req = urllib.request.Request(f"{API}{path}", headers={
            "Authorization": f"Bot {self.token}", "User-Agent": "DiscordBot (peer-perms, 1.0)",
        })
        for _ in range(5):
            try:
                with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=20) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise
                time.sleep(float(exc.headers.get("Retry-After") or 2) + 0.2)
        raise OSError("rate limited")


def read_env(root: Path) -> dict:
    env = {}
    for line in (root / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def load_guild(rest: Rest, guild_id: str) -> tuple:
    roles = {r["id"]: {"name": r["name"], "position": r["position"], "perms": int(r["permissions"]),
                       "managed": r.get("managed", False)}
             for r in rest.get(f"/guilds/{guild_id}/roles")}
    for role in roles.values():
        role["_was_admin"] = bool(role["perms"] & ADMINISTRATOR)
    raw = rest.get(f"/guilds/{guild_id}/channels")
    categories = {c["id"]: c["name"] for c in raw if c["type"] == 4}
    channels = []
    for channel in raw:
        channel["_category_id"] = channel["id"] if channel["type"] == 4 else channel.get("parent_id")
        channel["_all"] = categories
        channels.append(channel)
    members = {spec.user_id: rest.get(f"/guilds/{guild_id}/members/{spec.user_id}")["roles"]
               for spec in BOTS}
    return roles, channels, members


def loses_administrator(member_roles: list, role_id: str, roles: dict) -> bool:
    """True if this role is the holder's only source of Administrator."""
    own = {spec.own_role_id for spec in BOTS}
    if role_id not in own or not roles[role_id]["perms"] & ADMINISTRATOR:
        return False
    return not any(roles[r]["perms"] & ADMINISTRATOR for r in member_roles
                   if r in roles and r not in own)


def role_holders(rest: Rest, guild_id: str, role_ids: set, roles: dict) -> tuple:
    """Everyone holding these roles, and who the proposal would take Administrator from.

    Unticking a role changes every holder, including ones this link knows nothing about.
    """
    holders = {r: [] for r in role_ids}
    losers = {r: [] for r in role_ids}
    linked = {spec.user_id for spec in BOTS}
    after = "0"
    for _ in range(50):
        page = rest.get(f"/guilds/{guild_id}/members?limit=1000&after={after}")
        if not page:
            break
        for member in page:
            for role_id in role_ids & set(member["roles"]):
                kind = " (bot)" if member["user"].get("bot") else ""
                holders[role_id].append(member["user"]["username"] + kind)
                if (member["user"]["id"] not in linked
                        and loses_administrator(member["roles"], role_id, roles)):
                    losers[role_id].append(member["user"]["username"] + kind)
        after = page[-1]["user"]["id"]
        if len(page) < 1000:
            break
    return holders, losers


def own_activity(rest: Rest, me: str, channels: list, spec: BotSpec) -> list:
    """This token's newest post in each readable text channel outside its own category."""
    posts = []
    for channel in channels:
        if channel["type"] not in (0, 5) or in_category(channel, spec):
            continue
        try:
            messages = rest.get(f"/channels/{channel['id']}/messages?limit=100")
        except (urllib.error.HTTPError, OSError):
            continue
        mine = [m["timestamp"][:10] for m in messages if m.get("author", {}).get("id") == me]
        if mine:
            posts.append({"channel": channel["name"], "last_post": max(mine)})
    return sorted(posts, key=lambda p: p["last_post"], reverse=True)


def findings(report: Report, peer_label: str) -> list:
    out = []
    if report.sees_peer:
        out.append(f"{report.label} can see {peer_label}'s category")
    out.extend(f"{report.label} {e}" for e in report.escalations)
    if report.missing_own:
        out.append(f"{report.label} lacks in its own category: {', '.join(sorted(report.missing_own))}")
    if report.missing_server:
        out.append(f"{report.label} lacks at server level: {', '.join(report.missing_server)}")
    if report.removes_peer:
        out.append(f"{report.label} can remove {peer_label} from the server "
                   f"({', '.join(report.removes_peer)}, with its top role above)")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute each bot's Discord access.")
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--activity", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    env = read_env(install_root(_SCRIPT_ROOT))
    guild_id, token = env.get("DISCORD_GUILD_ID"), env.get("DISCORD_BOT_TOKEN")
    if not (guild_id and token):
        print("Needs DISCORD_GUILD_ID and DISCORD_BOT_TOKEN in .env.")
        return 5
    rest = Rest(token)
    try:
        roles, channels, members = load_guild(rest, guild_id)
        me = rest.get("/users/@me")["id"]
        watched = {s.own_role_id for s in BOTS} | {r for s in BOTS for r in s.drop_role_ids}
        holders, losers = role_holders(rest, guild_id, watched, roles)
        steps = []
        if args.simulate:
            roles, members, steps = simulate(guild_id, roles, members, channels, losers)
        reports = [assess(s, BOTS[1 - i], guild_id, roles, members[s.user_id], channels,
                          members[BOTS[1 - i].user_id])
                   for i, s in enumerate(BOTS)]
        spec_me = next((s for s in BOTS if s.user_id == me), None)
        activity = own_activity(rest, me, channels, spec_me) if args.activity and spec_me else []
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        print(f"Could not read Discord: {exc}")
        return 5

    problems = [f for i, r in enumerate(reports) for f in findings(r, BOTS[1 - i].label)]
    guest = env.get("GUEST_AUTO_ROLE", "Guest (no access)")
    guest_role = next((r for r in roles.values() if r["name"] == guest), None)
    if guest_role:
        for spec in BOTS:
            if guest_role["position"] >= top_position(roles, members[spec.user_id]):
                problems.append(f"{spec.label}'s top role is not above {guest}, so it cannot assign it")

    if args.as_json:
        print(json.dumps({
            "mode": "simulate" if args.simulate else "live",
            "reports": [vars(r) for r in reports], "findings": problems, "steps": steps,
            "holders": {roles[r]["name"]: h for r, h in holders.items() if r in roles},
            "would_lose_administrator": {roles[r]["name"]: h for r, h in losers.items() if h},
            "activity": activity,
        }, indent=1))
        return 1 if problems else 0

    print("SIMULATED after the proposed change" if args.simulate else "LIVE")
    for report in reports:
        print(f"\n{report.label}: roles {', '.join(report.roles) or 'none'}")
        print("  sees: " + ", ".join(f"{name} ({n})" for name, n in sorted(report.sees.items())))
        print(f"  sees the other bot's category: {'YES' if report.sees_peer else 'no'}")
        print(f"  can escalate: {'; '.join(report.escalations) or 'no'}")
        missing = ", ".join(sorted(report.missing_own)) or "nothing"
        print(f"  missing in its own category ({report.own_channels} channels): {missing}")
        print(f"  missing at server level: {', '.join(report.missing_server) or 'nothing'}")
        print(f"  can remove the other bot: {', '.join(report.removes_peer) or 'no'}")
        print(f"  dangerous permissions left, from its roles: {', '.join(report.dangerous) or 'none'}")
    print("\nWho holds the roles involved (a change to a role reaches all of them):")
    for role_id, people in holders.items():
        if role_id in roles:
            print(f"  {roles[role_id]['name']}: {', '.join(sorted(people)) or 'nobody'}")
    for role_id, people in losers.items():
        if people and role_id in roles:
            print(f"Turning off Administrator on {roles[role_id]['name']} also takes it from: "
                  f"{', '.join(sorted(people))}")
    if args.activity:
        print("\nThis bot's newest posts outside its own category, last 100 messages per channel:")
        for post in activity:
            print(f"  {post['channel']}: {post['last_post']}")
        if not activity:
            print("  none found")
    if steps:
        print("\nSteps for the owner, in order:")
        for number, step in enumerate(steps, 1):
            print(f"  {number}. {step}")
    print("\nKnown risk: this checks what this bot's code uses. If another process shares a token "
          "and uses the Signals channels, this cannot see that and the change could break it.")
    print("\nFindings: " + ("; ".join(problems) if problems else "none"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
