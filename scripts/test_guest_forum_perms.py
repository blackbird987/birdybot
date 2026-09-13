"""A granted user's forum lets them read history and send screenshots.

Without read_message_history Discord shows a guest only the messages that
arrive while their client is open, so the thread looks empty after every app
restart. This pins the allow set, that existing forums are topped up (the
bug lived in forums created before the allow existed), that unrelated
settings on the overwrite survive, that a cache miss falls back to the API,
and that an already-correct forum costs no API call.
"""
import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
from bot.discord import channels

fails = []
def check(label, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + f"{label}  got={got!r} want={want!r}")
    if not ok: fails.append(label)

class Member:
    def __init__(self, mid): self.id = mid
    def __hash__(self): return hash(self.id)
    def __eq__(self, o): return getattr(o, "id", None) == self.id

class Guild:
    def __init__(self, cached=(), remote=()):
        self.cached = {m.id: m for m in cached}
        self.remote = {m.id: m for m in remote}
        self.fetches = 0
    def get_member(self, mid): return self.cached.get(mid)
    async def fetch_member(self, mid):
        self.fetches += 1
        if mid in self.remote: return self.remote[mid]
        raise discord.NotFound(type("R", (), {"status": 404, "reason": "nf"})(), "gone")

class Forum:
    id = 1
    def __init__(self, ows=None): self.ows = ows or {}; self.calls = []
    def overwrites_for(self, m):
        return self.ows.get(m, discord.PermissionOverwrite())
    async def set_permissions(self, m, overwrite=None, reason=None):
        self.calls.append(reason); self.ows[m] = overwrite

def run(c): return asyncio.run(c)

check("allow set has read_message_history",
      channels.GUEST_FORUM_ALLOWS.get("read_message_history"), True)
check("allow set has attach_files",
      channels.GUEST_FORUM_ALLOWS.get("attach_files"), True)

# The live shape of the bug: a forum created with the old four allows.
vin = Member(7)
old = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                  send_messages_in_threads=True,
                                  create_public_threads=True,
                                  mention_everyone=False)
f = Forum({vin: old})
check("old forum is changed", run(channels.reconcile_guest_overwrite(f, Guild([vin]), 7)), True)
ow = f.ows[vin]
for k in channels.GUEST_FORUM_ALLOWS:
    check(f"old forum now allows {k}", getattr(ow, k), True)
check("unrelated deny survives", ow.mention_everyone, False)
check("one API call", len(f.calls), 1)

check("second pass is a no-op", run(channels.reconcile_guest_overwrite(f, Guild([vin]), 7)), False)
check("no extra API call", len(f.calls), 1)

# Cache miss: get_member returns None on a cold cache; must fetch.
f2 = Forum(); g2 = Guild(remote=[vin])
check("cache miss still reconciles", run(channels.reconcile_guest_overwrite(f2, g2, 7)), True)
check("fetched once", g2.fetches, 1)
check("history granted after fetch", f2.ows[vin].read_message_history, True)

# User left the guild: log and move on, never raise.
f3 = Forum()
check("departed user returns False", run(channels.reconcile_guest_overwrite(f3, Guild(), 7)), False)
check("departed user makes no call", f3.calls, [])

print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
