#!/usr/bin/env python3
"""Regression test: a repo can be hidden from Discord without being deleted.

Twenty repos means twenty forums in one category, and most of them are not
being worked on. Hiding parks a repo's forum in a separate category and drops
it out of every picker. It does **not** unregister the repo, delete a thread
or touch a file.

Three things have to hold or hiding is a way to lose work:

  * ``list_repos()`` stays unfiltered. Dozens of callers resolve a repo path
    through it to resume a session, merge a branch or run a deploy, and a
    hidden repo has to keep working for all of them. Only display surfaces
    read ``list_active_repos()``.
  * hiding is reversible and leaves no orphan state: unregistering a repo
    clears the flag, so a name re-registered later does not come back hidden.
  * work in a hidden repo un-hides it. A self-wake firing or a spawn landing
    inside a parked forum would otherwise post where nobody is looking, so
    every path that can surface something in a forum has to wake the repo
    first.

Asserted here: the store API and its persistence, the command surface
(hide/unhide/list, multiple names at once, reserved words), the wake itself,
and -- structurally -- that all three entry points into a forum call it.
"""

from __future__ import annotations

import asyncio

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.engine import commands  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" · {detail}" if detail else ""))
    if not ok:
        failures.append(label)


class FakeMessenger:
    """Records the visibility hook instead of talking to Discord."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.visibility: list[tuple[str, bool]] = []

    async def send_text(self, channel_id, text, **kw) -> None:
        self.sent.append(text)

    async def on_repo_visibility_changed(self, repo_name: str, hidden: bool) -> None:
        self.visibility.append((repo_name, hidden))

    def __getattr__(self, name):  # any other hook is a no-op
        async def _noop(*a, **kw):
            return None
        return _noop


class Ctx:
    def __init__(self, store, messenger):
        self.store = store
        self.messenger = messenger
        self.channel_id = "chan"
        self.platform = "test"
        self.repo_name = None


def new_store(tmp: Path) -> StateStore:
    store = StateStore(tmp / "state.json", tmp / "results")
    store.add_repo("aiagent", str(tmp / "aiagent"))
    store.add_repo("deskforge", str(tmp / "deskforge"))
    store.add_repo("memepipe", str(tmp / "memepipe"))
    return store


tmpdir = tempfile.TemporaryDirectory()
tmp = Path(tmpdir.name)
run = asyncio.run

# --- 1. The store: hiding is display state, not registration state ---------
print("\nstore")
store = new_store(tmp)
check("a fresh repo is not hidden", not store.is_repo_dormant("deskforge"))
check("hiding an unregistered repo is refused",
      store.set_repo_dormant("nosuchrepo", True) is False)

store.set_repo_dormant("deskforge", True)
check("hidden repo is STILL in list_repos()", "deskforge" in store.list_repos())
check("and its path still resolves",
      store.list_repos().get("deskforge") == str(tmp / "deskforge"))
check("but it is out of list_active_repos()",
      "deskforge" not in store.list_active_repos()
      and "aiagent" in store.list_active_repos())
check("and it is named by list_dormant_repos()",
      store.list_dormant_repos() == ["deskforge"])

# --- 2. Persistence --------------------------------------------------------
print("\npersistence")
store.save()
reloaded = StateStore(tmp / "state.json", tmp / "results")
check("hidden state survives a reload", reloaded.is_repo_dormant("deskforge"))
check("and the visible repos come back visible",
      not reloaded.is_repo_dormant("aiagent"))

# --- 3. Unregistering clears the flag --------------------------------------
print("\nregistry hygiene")
reloaded.remove_repo("deskforge")
check("removing a hidden repo drops it from the dormant set",
      reloaded.list_dormant_repos() == [])
reloaded.add_repo("deskforge", str(tmp / "deskforge"))
check("re-registering the same name does not come back hidden",
      not reloaded.is_repo_dormant("deskforge"))
reloaded.set_repo_dormant("deskforge", True)
reloaded.add_repo("deskforge", str(tmp / "deskforge2"))
check("re-pointing a hidden repo with /repo add un-hides it",
      not reloaded.is_repo_dormant("deskforge"))

# --- 4. Round trip ---------------------------------------------------------
print("\nround trip")
store = new_store(tmp)
before = dict(store.list_repos())
store.set_repo_dormant("memepipe", True)
store.set_repo_dormant("memepipe", False)
check("hide → unhide restores the repo list exactly",
      store.list_repos() == before and store.list_dormant_repos() == [])

# --- 5. The command surface ------------------------------------------------
print("\n/repo hide, /repo unhide, /repo list")
store = new_store(tmp)
msg = FakeMessenger()
ctx = Ctx(store, msg)

run(commands.on_repo(ctx, "hide deskforge memepipe"))
check("several repos hide in one call",
      store.is_repo_dormant("deskforge") and store.is_repo_dormant("memepipe"))
check("the platform is told about each one",
      msg.visibility == [("deskforge", True), ("memepipe", True)],
      str(msg.visibility))
check("the reply says nothing was deleted",
      "deleted" in msg.sent[-1], msg.sent[-1][:80])

msg.sent.clear()
run(commands.on_repo(ctx, "hide nosuchrepo"))
check("an unknown repo is reported, not created",
      "not found" in msg.sent[-1] and "nosuchrepo" not in store.list_repos())

msg.sent.clear()
run(commands.on_repo(ctx, "list"))
listing = msg.sent[-1]
check("/repo list still shows a hidden repo",
      "deskforge" in listing and "aiagent" in listing)
check("under its own Hidden heading", "Hidden" in listing, listing)

msg.sent.clear()
msg.visibility.clear()
run(commands.on_repo(ctx, "unhide deskforge"))
check("unhide clears the flag", not store.is_repo_dormant("deskforge"))
check("and tells the platform to bring the forum back",
      msg.visibility == [("deskforge", False)], str(msg.visibility))
check("a repo hidden alongside it stays hidden",
      store.is_repo_dormant("memepipe"))

msg.sent.clear()
run(commands.on_repo(ctx, "hide"))
check("a bare /repo hide explains itself instead of hiding everything",
      "Usage: /repo hide <name>" in msg.sent[-1]
      and store.list_dormant_repos() == ["memepipe"], msg.sent[-1])

msg.sent.clear()
run(commands.on_repo(ctx, "unhide"))
check("and a bare /repo unhide reaches its own handler, not the fallback",
      "Usage: /repo unhide <name>" in msg.sent[-1]
      and store.list_dormant_repos() == ["memepipe"], msg.sent[-1])

msg.sent.clear()
run(commands.on_repo(ctx, "wat"))
check("the /repo usage line advertises hide and unhide",
      "hide|unhide" in msg.sent[-1], msg.sent[-1])

msg.sent.clear()
store.set_repo_dormant("aiagent", True)
run(commands.on_repo(ctx, ""))
check("bare /repo says how many repos are parked",
      "2 hidden" in msg.sent[-1], msg.sent[-1])
store.set_repo_dormant("aiagent", False)

check("switching to a hidden repo still works",
      store.switch_repo("memepipe") and store.get_active_repo()[0] == "memepipe")

# --- 6. Reserved words -----------------------------------------------------
print("\nreserved words")
for word in ("hide", "unhide"):
    check(f"'{word}' is refused as a repo name",
          commands._validate_repo_name(word) is not None)

# --- 7. Wake on work -------------------------------------------------------
print("\nwake on work")
from bot.discord.forums import ForumManager  # noqa: E402

store = new_store(tmp)
store.set_repo_dormant("memepipe", True)

fm = object.__new__(ForumManager)
fm._store = store
fm._client = None
moved: list[str] = []

async def _fake_unhide(repo_name: str) -> str:
    moved.append(repo_name)
    return "forum moved"

fm.unhide_repo_forum = _fake_unhide

woke = run(fm.wake_repo_if_dormant("memepipe"))
check("starting work in a hidden repo un-hides it",
      woke and not store.is_repo_dormant("memepipe"))
check("and its forum is brought back out of the archive category",
      moved == ["memepipe"], str(moved))

moved.clear()
check("a repo that was never hidden costs nothing",
      run(fm.wake_repo_if_dormant("aiagent")) is False and moved == [])
check("and neither does the placeholder repo",
      run(fm.wake_repo_if_dormant("_default")) is False and moved == [])

store.set_repo_dormant("memepipe", True)
async def _boom(repo_name: str) -> str:
    raise RuntimeError("discord is down")
fm.unhide_repo_forum = _boom
woke = run(fm.wake_repo_if_dormant("memepipe"))
check("a failed channel move still un-hides the repo in state",
      woke and not store.is_repo_dormant("memepipe"))

# --- 8. Every way into a forum goes through the wake -----------------------
# Structural, because the failure is silent: a path added later that posts
# into a forum without waking it parks work where nobody is looking, and no
# unit test of the store would notice.
print("\nwake coverage")
import inspect  # noqa: E402
from bot.discord import bot as discord_bot_mod  # noqa: E402

src = inspect.getsource(ForumManager.get_or_create_session_thread)
check("get_or_create_session_thread wakes the repo",
      "wake_repo_if_dormant" in src)
check("and it wakes BEFORE the already-has-a-thread early return",
      src.index("wake_repo_if_dormant") < src.index("session_to_thread"),
      "a resumed session in a hidden repo would keep posting into it")

replay = inspect.getsource(discord_bot_mod.ClaudeBot._replay_to_thread)
check("every unattended resume (self-wake, watch, --here schedule, spawn "
      "join, reboot replay) wakes the repo",
      "wake_repo_if_dormant" in replay)

on_msg = inspect.getsource(discord_bot_mod)
check("and so does a user message in an existing forum thread",
      on_msg.count("wake_repo_if_dormant") >= 2, str(on_msg.count("wake_repo_if_dormant")))

tmpdir.cleanup()

print()
if failures:
    print("FAIL: repo hide")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print(f"PASS: a repo can be hidden without being deleted ({checks} checks).")
print("      Hiding is display state only: list_repos() stays unfiltered so")
print("      every path, session and merge keeps resolving, the repo is still")
print("      listed under /repo list, unregistering clears the flag, and work")
print("      starting in a hidden repo brings it straight back.")
