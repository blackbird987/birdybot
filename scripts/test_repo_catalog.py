#!/usr/bin/env python3
"""Regression test: a session can see which repos already exist.

The user keeps two dozen repos and hides the ones they are not working in.
Asked to build something new, a session that has never seen the repo list
reaches for `/repo create` and duplicates a project that already exists,
most likely one that is hidden and so out of the user's sight too.

Asserted here:

  * the system prompt carries a "Your Repos" block: every registered repo,
    its cached Control Room description, hidden ones marked, under a size
    bound; empty with no store or no repos; a broken store fails open.
  * `/repo create` refuses a near-duplicate name unless --force is given,
    and --force never leaks into the path.
  * a session may emit `[BOT_CMD: /repo hide ...]` / `unhide`, while
    actions outside the allow-list (remove) stay blocked.
"""

from __future__ import annotations

import asyncio

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.claude.runner import ClaudeRunner  # noqa: E402
from bot.claude.types import Instance, InstanceStatus, InstanceType  # noqa: E402
from bot.engine import repo_cmds  # noqa: E402
from bot.engine.directives import dispatch  # noqa: E402
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
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, channel_id, text, **kw) -> None:
        self.sent.append(text)

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


def make_instance() -> Instance:
    return Instance(
        id="t-test", name=None, instance_type=InstanceType.TASK, prompt="x",
        repo_name="", repo_path="", status=InstanceStatus.RUNNING,
        mode="build", branch="claude-bot/t-test",
    )


def make_runner(store) -> ClaudeRunner:
    runner = ClaudeRunner()
    runner._store = store
    runner._build_location_block = lambda inst: ""  # type: ignore[method-assign]
    runner._build_master_context_block = lambda inst: ""  # type: ignore[method-assign]
    return runner


def describe(store: StateStore, name: str, text: str) -> None:
    store.set_repo_description(name, text, "CLAUDE.md", "sig", 0.0, "/x")


tmpdir = tempfile.TemporaryDirectory()
tmp = Path(tmpdir.name)


def new_store(sub: str) -> StateStore:
    d = tmp / sub
    d.mkdir()
    return StateStore(d / "state.json", d / "results")


# ---- The prompt block --------------------------------------------------------
print("Repo catalog block")

store = new_store("catalog")
store.add_repo("smarthome", str(tmp / "smarthome"))
store.add_repo("deskforge", str(tmp / "deskforge"))
store.add_repo("aiagent", str(tmp / "aiagent"))
describe(store, "smarthome", "An index of every smart device in the house.")
describe(store, "deskforge", "Desktop workshop for this machine.")
store.set_repo_dormant("deskforge", True)

runner = make_runner(store)
block = runner._build_repo_catalog_block(make_instance())
check("block has its heading", "--- Your Repos ---" in block)
check("a described repo shows its description",
      "- smarthome: An index of every smart device in the house." in block)
check("a hidden repo is listed and marked",
      "- deskforge (hidden): Desktop workshop for this machine." in block)
check("a repo with no cached description shows its name alone",
      "\n- aiagent\n" in block)
check("the check-before-create rule is present", "/repo create" in block)
check("no em dash in the block", "\u2014" not in block)

prompt = runner._build_system_prompt(make_instance())
check("the block lands in the system prompt", block in prompt)
check("it sits after BOT_CONTEXT",
      prompt.index(config.BOT_CONTEXT.strip()) < prompt.index("--- Your Repos ---"))

check("no store, no block", make_runner(None)._build_repo_catalog_block(make_instance()) == "")
check("no repos, no block",
      make_runner(new_store("empty"))._build_repo_catalog_block(make_instance()) == "")


class BrokenStore:
    def list_repos(self):
        raise RuntimeError("state.json is on fire")


broken = make_runner(BrokenStore())
check("a broken store fails open", broken._build_repo_catalog_block(make_instance()) == "")
check("and the prompt still builds",
      "--- Your Repos ---" not in broken._build_system_prompt(make_instance()))

big = new_store("big")
for i in range(25):
    big.add_repo(f"some-project-name-{i:02d}", str(tmp / f"p{i}"))
    describe(big, f"some-project-name-{i:02d}", "word " * 60)
big_block = make_runner(big)._build_repo_catalog_block(make_instance())
check("25 repos with long descriptions stay under 4 KB", len(big_block) < 4096,
      f"{len(big_block)} bytes")
check("long descriptions are cut with an ellipsis",
      all(len(line) < 140 for line in big_block.splitlines() if line.startswith("- ")))


# ---- /repo create near-duplicates -------------------------------------------
print("/repo create near-duplicates")

check("separator-only difference matches",
      repo_cmds._similar_repo_names("smart-home", {"smarthome": "/x"}) == ["smarthome"])
check("case difference matches",
      repo_cmds._similar_repo_names("SmartMoney", {"smartmoney": "/x"}) == ["smartmoney"])
check("containment matches", repo_cmds._similar_repo_names(
    "citadel", {"The-Citadel": "/x"}) == ["The-Citadel"])
check("a short name is not matched by containment alone",
      repo_cmds._similar_repo_names("ai", {"aiagent": "/x"}) == [])
check("an unrelated name does not match",
      repo_cmds._similar_repo_names("video-cutter", {"smarthome": "/x", "aiagent": "/y"}) == [])

created: list[tuple] = []


async def run_create(store, text: str) -> FakeMessenger:
    messenger = FakeMessenger()
    await repo_cmds._create_repo(Ctx(store, messenger), text)
    return messenger


dup = new_store("dup")
dup.add_repo("smarthome", str(tmp / "smarthome"))
dup.set_repo_dormant("smarthome", True)
target = tmp / "ws" / "smart-home"
m = asyncio.run(run_create(dup, f"smart-home {target}"))
refusal = m.sent[-1] if m.sent else ""
check("a near-duplicate is refused", "Similar repo(s) already registered" in refusal, refusal)
check("the refusal marks the hidden match", "smarthome (hidden)" in refusal)
check("the refusal points at --force", "--force" in refusal)
check("nothing was registered", "smart-home" not in dup.list_repos())
check("nothing was created on disk", not target.exists())

m = asyncio.run(run_create(dup, f"smart-home {target} --force"))
check("--force creates it anyway", "smart-home" in dup.list_repos(), " | ".join(m.sent))
check("--force does not leak into the path",
      dup.list_repos().get("smart-home") == str(target))

m = asyncio.run(run_create(dup, f"brand-new-thing {tmp / 'ws' / 'brand-new-thing'}"))
check("an unrelated name is created without --force", "brand-new-thing" in dup.list_repos(),
      " | ".join(m.sent))


# ---- BOT_CMD hide / unhide ----------------------------------------------------
print("BOT_CMD hide/unhide")

ran: list[str] = []


async def fake_on_repo(ctx, args: str) -> None:
    ran.append(args)


dispatch.on_repo = fake_on_repo  # patched where it is read


async def run_dispatch(text: str) -> None:
    await dispatch._execute_bot_commands(Ctx(new_store(f"d{len(ran)}{text[:3]}x{checks}"),
                                             FakeMessenger()), text)


asyncio.run(run_dispatch("Done.\n[BOT_CMD: /repo hide deadcut fundops]"))
check("hide with several names runs", ran == ["hide deadcut fundops"], repr(ran))
ran.clear()
asyncio.run(run_dispatch("[BOT_CMD: /repo unhide deadcut]"))
check("unhide runs", ran == ["unhide deadcut"], repr(ran))
ran.clear()
asyncio.run(run_dispatch("[BOT_CMD: /repo remove deadcut]"))
check("remove stays blocked", ran == [], repr(ran))
asyncio.run(run_dispatch("[BOT_CMD: /repo hide deadcut; rm -rf ~]"))
check("dangerous characters stay blocked", ran == [], repr(ran))
check("BOT_CONTEXT tells sessions they may hide",
      "[BOT_CMD: /repo hide" in config.BOT_CONTEXT)


tmpdir.cleanup()
print()
if failures:
    print(f"FAILED {len(failures)}/{checks}:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"All {checks} checks passed.")
