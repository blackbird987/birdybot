"""A named guest is locked down on join; nobody else is touched.

The role denies View Channel on every channel, so the seconds between
accepting an invite and being given the role are the only exposure there is.
This pins that the assignment happens on join, that it is idempotent, that a
failure is loud rather than silent, and -- the one that matters on a live
community server -- that an ordinary joiner is never given it.
"""
import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio, os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config
from bot.discord.bot import ClaudeBot

fails = []
def check(label, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + f"{label}  got={got!r} want={want!r}")
    if not ok: fails.append(label)

class Role:
    def __init__(self, name): self.name = name
    def __eq__(self, o): return isinstance(o, Role) and o.name == self.name
    def __repr__(self): return f"<Role {self.name}>"

class Guild:
    def __init__(self, roles, gid=1): self.roles = roles; self.id = gid

class Member:
    def __init__(self, name, guild, global_name=None, roles=None, boom=False):
        self.name = name; self.global_name = global_name; self.id = 4242
        self.guild = guild; self.roles = roles or []; self.boom = boom
        self.added = []
    async def add_roles(self, role, reason=None):
        if self.boom: raise RuntimeError("missing permissions")
        self.added.append(role.name); self.roles.append(role)

class Chan:
    def __init__(self): self.sent = []
    async def send(self, t): self.sent.append(t)

class FakeBot:
    _guild_id = 1
    def __init__(self, chan): self._lobby_channel_id = 99; self._chan = chan
    def get_channel(self, cid): return self._chan
    _alert_owner = ClaudeBot._alert_owner

GUEST = "Guest (no access)"
config.GUEST_AUTO_ROLE = GUEST
config.GUEST_AUTO_ROLE_USERS = ("vinski11",)

def run(member, roles=(GUEST,)):
    chan = Chan(); bot = FakeBot(chan)
    member.guild.roles = [Role(r) for r in roles]
    asyncio.run(ClaudeBot._apply_guest_role(bot, member, source="join"))
    return member, chan

g = Guild([])
m, c = run(Member("vinski11", g))
check("named guest gets the role", m.added, [GUEST])
check("owner is told", len(c.sent), 1)

m, c = run(Member("SomeGuy", g))
check("ordinary community joiner untouched", m.added, [])
check("...and no alert spam", c.sent, [])

m, c = run(Member("Vinski11", g))
check("username match is case-insensitive", m.added, [GUEST])

m, c = run(Member("other_handle", g, global_name="vinski11"))
check("display-name match also works", m.added, [GUEST])

m, c = run(Member("vinski11", g, roles=[Role(GUEST)]))
check("idempotent when already held", m.added, [])

m, c = run(Member("vinski11", g), roles=("Analyst",))
check("missing role -> not silent", (m.added, len(c.sent)), ([], 1))
check("...and the alert says they are exposed", "manually" in c.sent[0], True)

m, c = run(Member("vinski11", g, boom=True))
check("assignment failure -> owner warned", (m.added, len(c.sent)), ([], 1))

config.GUEST_AUTO_ROLE_USERS = ()
m, c = run(Member("vinski11", g))
check("feature off -> nobody touched", m.added, [])

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
