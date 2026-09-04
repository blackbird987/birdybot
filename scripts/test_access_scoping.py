"""A grant names one repo, and reaches only that repo.

_check_access used to fall through: a guest holding any grant at all was
allowed in every repo, at that repo's own settings. This pins the denial, and
pins that an unresolvable channel drops to the tightest policy the user holds
rather than to the permissive defaults.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot.discord.bot as B
from bot.discord.access import AccessConfig, UserAccess, RepoAccess

cfg = AccessConfig()
cfg.users["1001"] = UserAccess(user_id="1001", display_name="Vin",
    repos={"vin-coach": RepoAccess(mode="build", bash_policy="none", max_daily_queries=40)})
cfg.users["1002"] = UserAccess(user_id="1002", display_name="Two",
    repos={"a": RepoAccess(mode="build", bash_policy="full"),
           "b": RepoAccess(mode="explore", bash_policy="none")})
B.load_access_config = lambda: cfg

class FakeForums:
    def thread_to_project(self, cid): return None
class FakeSelf:
    _forums = FakeForums()
    def _is_owner(self, uid): return uid == 7
    def _resolve_repo_from_user_forum(self, cid, uid): return None

f = FakeSelf()
chk = lambda uid, repo=None: B.ClaudeBot._check_access(f, uid, repo_name=repo)

fails = []
def check(label, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + label + f"  got={got} want={want}")
    if not ok: fails.append(label)

r = chk(1001, "vin-coach")
check("Vin in his own repo allowed", (r.allowed, r.mode_ceiling, r.bash_policy, r.max_daily_queries),
      (True, "build", "none", 40))

r = chk(1001, "bot")
check("Vin in the BOT repo denied", (r.allowed, r.reason), (False, "No access grant for `bot`"))

r = chk(1001, "The-Citadel")
check("Vin in Citadel denied", r.allowed, False)

r = chk(1001, None)
check("Vin, unresolvable channel -> tightest", (r.allowed, r.mode_ceiling, r.bash_policy),
      (True, "build", "none"))

r = chk(1002, None)
check("multi-grant unresolvable -> tightest of both", (r.allowed, r.mode_ceiling, r.bash_policy),
      (True, "explore", "none"))

r = chk(1002, "a")
check("multi-grant exact repo keeps its own settings", (r.allowed, r.mode_ceiling, r.bash_policy),
      (True, "build", "full"))

r = chk(7, "bot")
check("owner unaffected", (r.allowed, r.is_owner), (True, True))

r = chk(999, "vin-coach")
check("stranger denied", r.allowed, False)

print()
print("ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
