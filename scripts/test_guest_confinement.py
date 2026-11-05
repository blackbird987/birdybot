"""Regression test: every guest session is confined, whatever its repo's setting.

Pins the one answer per run, every route that creates a run, the refusals, /spawn,
/sandbox, the access fallback, TODO.md, and an owner's run staying as it was. Temp
dirs and fakes, no session and no real key. The real-CLI proof, before and after, is
`scripts/attack_guest_confinement.py`. Read docs/design/session-sandbox.md first.

Run: python scripts/test_guest_confinement.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="guest-harness-")).resolve()
# Before anything asks where home is: only repos under it can be sandboxed.
os.environ["HOME"] = str(TMP)
for key, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                   ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
    os.environ[key] = value

from bot import config  # noqa: E402

config.DATA_DIR = TMP / "data"
config.RESULTS_DIR = config.DATA_DIR / "results"
config.PENDING_IMAGES_DIR = config.DATA_DIR / "pending_images"
config.PENDING_IMAGES_DIR.mkdir(parents=True)
config.RESULTS_DIR.mkdir(parents=True)

from bot.claude import parser, sandbox  # noqa: E402
from bot.claude.provider import get_provider  # noqa: E402
from bot.claude.runner import ClaudeRunner  # noqa: E402
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType  # noqa: E402
from bot.discord import bot as bot_mod  # noqa: E402
from bot.discord.access import AccessConfig, RepoAccess, UserAccess  # noqa: E402
from bot.discord.thread_ctx import ThreadContextMixin  # noqa: E402
from bot.engine import instance_cmds, judge, query, sandbox_cmds, step_spawn  # noqa: E402
from bot.engine.directives.dispatch import _execute_bot_commands  # noqa: E402
from bot.platform.base import RequestContext  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: object, detail: object = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        print(f"  FAIL: {name} {detail}")


def git(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def new_repo(name: str) -> Path:
    path = TMP / name
    path.mkdir()
    git(path, "init", "-q", "-b", "master")
    (path / "a.txt").write_text("a\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "first")
    return path


def new_store(name: str, *repos: Path) -> StateStore:
    store = StateStore(TMP / f"{name}-state.json", TMP / f"{name}-results")
    for repo in repos:
        store.add_repo(repo.name, str(repo))
    return store


def new_instance(repo: Path, inst_id: str = "q-1", **kw) -> Instance:
    defaults = dict(
        id=inst_id, name=None, instance_type=InstanceType.QUERY, prompt="hello",
        repo_name=repo.name, repo_path=str(repo), status=InstanceStatus.RUNNING,
        mode="build", effort="low", is_owner_session=True,
    )
    defaults.update(kw)
    return Instance(**defaults)


class FakeMessenger:
    def __init__(self, stop_at_thinking=False):
        self.posts: list[str] = []
        self.stop_at_thinking = stop_at_thinking

    async def send_text(self, _cid, text, **_kw):
        self.posts.append(text)
        return SimpleNamespace(message_id="m", channel_id="c")

    async def edit_text(self, *_a, **_kw):
        return None

    async def send_thinking(self, *_a, **_kw):
        raise ReachedTheRun()

    def escape(self, text):
        return text

    def __getattr__(self, _name):
        async def nothing(*_a, **_kw):
            return SimpleNamespace(message_id="m", channel_id="c")
        return nothing


class ReachedTheRun(Exception):
    """The run was created and stamped, which is all a route test needs."""


def fake_runner(**kw):
    defaults = dict(
        check_spawn_allowed=lambda *a, **k: None,
        is_draining=False,
        active_instance_for_channel=lambda *a, **k: None,
        active_instance_for_session=lambda *a, **k: None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def make_ctx(store, *, owner=True, has_guest=False, repo=None, runner=None, messenger=None, **kw):
    return RequestContext(
        messenger=messenger or FakeMessenger(), channel_id="c-1", platform="test", store=store,
        runner=runner or fake_runner(), is_owner=owner, thread_has_guest=has_guest,
        repo_name=repo, **kw,
    )


def newest(store: StateStore) -> Instance:
    return list(store._instances.values())[-1]


# --- One answer per run -------------------------------------------------------

def test_answer() -> None:
    repo = new_repo("answer")
    other = new_repo("answer-on")
    store = new_store("answer", repo, other)
    store.set_sandbox_entry(str(other), {"hosts": ["github.com"]})

    owner = new_instance(repo)
    guest = new_instance(repo, is_owner_session=False)
    owner_in_guest_thread = new_instance(repo)
    owner_in_guest_thread.guest_confined = True
    owner_on = new_instance(other)

    check("owner, no setting: not confined", not sandbox.instance_confined(store, owner))
    check("guest, no setting: confined", sandbox.instance_confined(store, guest))
    check("owner in a guest thread: confined", sandbox.instance_confined(store, owner_in_guest_thread))
    check("owner, setting on: confined", sandbox.instance_confined(store, owner_on))

    check("owner gets no setting", sandbox.run_setting(store, owner) is None)
    setting = sandbox.run_setting(store, guest)
    check("guest gets the /sandbox on default hosts", setting["hosts"] == list(sandbox.DEFAULT_HOSTS), setting)
    check("the guest default is marked unchecked", setting.get("guest_only") is True)
    check("the default is a copy", setting["hosts"] is not sandbox.DEFAULT_HOSTS)
    check("a repo's own hosts win", sandbox.run_setting(store, new_instance(other, is_owner_session=False))
          == {"hosts": ["github.com"]})

    store.set_sandbox_entry(str(repo), {"hosts": "bad"})
    try:
        sandbox.run_setting(store, guest)
        check("an unreadable setting raises for a guest too", False)
    except Exception:
        check("an unreadable setting raises for a guest too", True)
    check("the yes/no answer still does not raise", sandbox.instance_confined(store, guest))
    store.set_sandbox_entry(str(repo), None)

    guest_ctx = make_ctx(store, owner=False)
    thread_ctx = make_ctx(store, has_guest=True)
    alone_ctx = make_ctx(store)
    check("no instance, guest ctx", sandbox.instance_confined(store, None, ctx=guest_ctx))
    check("no instance, owner in a guest thread", sandbox.instance_confined(store, None, ctx=thread_ctx))
    check("no instance, owner alone", not sandbox.instance_confined(store, None, ctx=alone_ctx, repo_path=str(repo)))
    check("no instance, owner alone in a sandboxed repo",
          sandbox.instance_confined(store, None, ctx=alone_ctx, repo_path=str(other)))
    bare = new_store("answer-bare", repo)
    check("origin with no instance, guest ctx", sandbox.origin_is_sandboxed(bare, None, guest_ctx))
    check("origin with no instance, owner, nothing on", not sandbox.origin_is_sandboxed(bare, None, alone_ctx))


def test_persistence() -> None:
    repo = new_repo("persist")
    inst = new_instance(repo, "q-7")
    inst.guest_confined = True
    data = inst.to_dict()
    check("to_dict carries it", data["guest_confined"] is True)
    check("from_dict restores it", Instance.from_dict(data).guest_confined is True)
    old = {k: v for k, v in data.items() if k != "guest_confined"}
    check("an old owner record reads as not confined", Instance.from_dict(old).guest_confined is False)
    old["is_owner_session"] = False
    check("an old guest record is confined all the same", sandbox.is_guest_confined(Instance.from_dict(old)))

    path = TMP / "persist-state.json"
    store = StateStore(path, TMP / "persist-results")
    store._instances[inst.id] = inst
    store.save()
    reloaded = StateStore(path, TMP / "persist-results").get_instance(inst.id)
    check("a restart keeps it", reloaded is not None and reloaded.guest_confined is True)
    check("a restart keeps the answer", sandbox.instance_confined(store, reloaded))


# --- Every route that makes a run --------------------------------------------

def _query_run(store, repo, **ctx_kw) -> Instance:
    ctx = make_ctx(store, repo=repo.name, **ctx_kw)
    try:
        asyncio.run(query._execute_query(ctx, "hello"))
    except ReachedTheRun:
        pass
    return newest(store)


def test_routes() -> None:
    repo = new_repo("routes")
    store = new_store("routes", repo)

    inst = _query_run(store, repo, owner=False, bash_policy="none")
    check("direct message: guest stamped", inst.guest_confined and not inst.is_owner_session)
    check("direct message: no keys", not inst.vault_eligible)
    inst = _query_run(store, repo, owner=True, has_guest=True)
    check("owner's turn in a guest thread stamped", inst.guest_confined and inst.is_owner_session)
    check("owner's turn in a guest thread: no keys", not inst.vault_eligible)
    inst = _query_run(store, repo, owner=True, has_guest=False)
    check("owner alone not stamped", not inst.guest_confined)

    # A schedule, a replay and a wake all arrive as a turn with the guest's access.
    inst = _query_run(store, repo, owner=False, user_id="1001", bash_policy="none")
    check("replayed or scheduled guest turn stamped", inst.guest_confined)

    guest_src = new_instance(repo, "q-900", is_owner_session=False, session_id="s", bash_policy="none",
                             bash_policy_baseline="none")
    guest_src.guest_confined = True
    store._instances[guest_src.id] = guest_src

    clone = store.clone_instance_for_rerun(guest_src, origin_platform="t", effort="low", model=None)
    check("clone keeps it", clone.guest_confined and not clone.is_owner_session)
    legacy = new_instance(repo, "q-901", is_owner_session=False)
    store._instances[legacy.id] = legacy
    clone = store.clone_instance_for_rerun(legacy, origin_platform="t", effort="low", model=None)
    check("clone of a guest run from before the field is stamped", clone.guest_confined)
    check("a persisted clone keeps it", Instance.from_dict(clone.to_dict()).guest_confined)

    owner_src = new_instance(repo, "q-902", session_id="s2")
    store._instances[owner_src.id] = owner_src
    clone = store.clone_instance_for_rerun(owner_src, origin_platform="t", effort="low", model=None)
    check("clone of an owner's run stays free", not clone.guest_confined)

    # An owner tapping Retry on a guest's run: the tap is not what is being run.
    ctx = make_ctx(store, owner=True, has_guest=False)
    try:
        asyncio.run(instance_cmds._start_retry(ctx, guest_src))
    except ReachedTheRun:
        pass
    check("retry keeps it, whoever tapped", newest(store).guest_confined and newest(store).parent_id == guest_src.id)

    async def spawn(source, ctx, origin=InstanceOrigin.BUILD, mode="build", itype=InstanceType.TASK):
        cfg = step_spawn.SpawnConfig(instance_type=itype, prompt="next step", mode=mode, origin=origin)
        with mock.patch.object(step_spawn.lifecycle, "run_instance", new=mock.AsyncMock(side_effect=ReachedTheRun)):
            try:
                await step_spawn.spawn_from(ctx, source.id, cfg)
            except ReachedTheRun:
                pass
        return newest(store)

    child = asyncio.run(spawn(guest_src, make_ctx(store, owner=True, has_guest=False)))
    check("chain step from a guest's run stays confined, whoever tapped", child.guest_confined and child.id != guest_src.id)
    owner_child = asyncio.run(spawn(owner_src, make_ctx(store, owner=True, has_guest=True)))
    check("chain step from an owner's run in a guest thread confined", owner_child.guest_confined)
    free_child = asyncio.run(spawn(owner_src, make_ctx(store, owner=True, has_guest=False)))
    check("chain step from an owner's run stays free", not free_child.guest_confined)
    grand = asyncio.run(spawn(child, make_ctx(store, owner=True, has_guest=False)))
    check("a later step keeps it", grand.guest_confined)

    judged = judge.readonly_instance(make_ctx(store, owner=False), InstanceOrigin.JUDGE, "p", repo.name, str(repo))
    check("a read-only helper run on a guest's turn stamped", judged.guest_confined)
    judged = judge.readonly_instance(make_ctx(store), InstanceOrigin.JUDGE, "p", repo.name, str(repo))
    check("a read-only helper run on the owner's turn free", not judged.guest_confined)


def test_family_walk() -> None:
    def info(tid, parent=None, guest=False):
        return SimpleNamespace(thread_id=tid, parent_thread_id=parent, _guest=guest)

    threads = {
        "root": info("root", guest=True),
        "child": info("child", "root"),
        "grandchild": info("grandchild", "child"),
        "plain": info("plain"),
        "looped": info("looped", "looped"),
    }
    fake = SimpleNamespace(
        _forums=SimpleNamespace(thread_to_project=lambda tid: ("repo", threads[tid]) if tid in threads else None),
        _thread_guest_id=lambda i: "1001" if i._guest else None,
    )
    walk = lambda i: ThreadContextMixin._family_has_guest(fake, i)  # noqa: E731
    check("a guest's thread", walk(threads["root"]))
    check("a child of it", walk(threads["child"]))
    check("a grandchild of it", walk(threads["grandchild"]))
    check("an owner's thread", not walk(threads["plain"]))
    check("a loop ends", not walk(threads["looped"]))
    check("an owner in an unmapped channel stays free", not walk(None))


def test_source_scan() -> None:
    """Every place that makes a run stamps it, or says why it does not need to."""
    allow = {
        "bot/scheduler.py": "/schedule is owner only",
        "bot/discord/prompt_review.py": "owner only",
    }
    pattern = re.compile(r"\.(create_instance|clone_instance_for_rerun)\(")
    found = 0
    for path in sorted((ROOT / "bot").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if not pattern.search(line):
                continue
            found += 1
            if rel == "bot/store/state.py":
                check("the store's clone copies the flag", any(
                    "guest_confined" in text for text in lines[number:number + 30]))
                continue
            window = "\n".join(lines[number:number + 30])
            if rel in allow:
                nearby = "\n".join(lines[max(0, number - 8):number + 12])
                check(f"{rel}:{number + 1} says why", "owner only" in nearby.lower(), nearby)
            else:
                check(f"{rel}:{number + 1} stamps", "stamp_guest" in window, line.strip())
    check("the scan found the sites", found >= 10, found)

    # Nothing decides on the repo alone except the places that mean the repo.
    allowed = {
        "bot/claude/sandbox.py": "defines it",
        "bot/engine/directives/spawn.py": "asks about the target repo, not this run",
        "bot/discord/deploy_flow.py": "deploys belong to a repo, not a run",
    }
    for path in sorted((ROOT / "bot").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in allowed:
            continue
        check(f"{rel} asks about the run", not re.search(r"(?<![\w])is_sandboxed\(", path.read_text(encoding="utf-8")))
    # Guest text reaches these two with no sandbox around them.
    for rel in ("bot/discord/titles.py", "bot/discord/log_triage.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        check(f"{rel} turns the tools off", '"--tools", ""' in text)
        check(f"{rel} turns MCP off too", '"--strict-mcp-config"' in text)
    check("titles do not hand on the session's keys",
          "SESSION_STRIPPED_ENV_VARS" in (ROOT / "bot/discord/titles.py").read_text(encoding="utf-8"))


# --- Refusals -----------------------------------------------------------------

def _prepare(runner, inst, provider=None, api_fallback=False, wd="same"):
    return asyncio.run(runner._sandbox_prepare(
        inst, provider or runner.provider, api_fallback, inst.repo_path if wd == "same" else wd,
    ))


def _refused(result) -> bool:
    return bool(getattr(result, "is_error", False)) and sandbox.REFUSAL_PHRASE in (result.error_message or "")


def _clean_wording(result) -> bool:
    """Carries the refusal phrase and none of the other load-bearing ones."""
    lower = (result.error_message or "").lower()
    bad = ("lifetime limit", "out-of-memory", "prompt is too long", "compaction failed")
    return parser.is_account_agnostic_error(result.error_message) and not any(b in lower for b in bad)


def test_refusals() -> None:
    repo = new_repo("refuse")
    store = new_store("refuse", repo)
    runner = ClaudeRunner(store=store)
    guest = new_instance(repo, "q-g", is_owner_session=False)
    owner_turn = new_instance(repo, "q-o")
    owner_turn.guest_confined = True
    for inst in (guest, owner_turn):
        store._instances[inst.id] = inst
    tools = mock.patch.object(shutil, "which", lambda name: f"/usr/bin/{name}")

    with tools:
        result = _prepare(runner, guest)
        check("a good repo prepares", isinstance(result, sandbox.SandboxRun), result)
        if isinstance(result, sandbox.SandboxRun):
            check("on the default hosts", list(result.hosts) == list(sandbox.DEFAULT_HOSTS), result.hosts)
            sandbox.finish_run(result)
        check("no setting was stored", str(repo) not in store.sandbox_entries())

        result = _prepare(runner, guest, provider=get_provider("cursor"))
        check("a provider with no sandbox refuses", _refused(result) and "cursor" in result.error_message)
        check("and says it was a guest", "guest sessions always run sandboxed" in result.error_message, result.error_message)
        check("wording", _clean_wording(result), result.error_message)

        result = _prepare(runner, guest, api_fallback=True)
        check("API fallback (--bare) refuses", _refused(result) and "API fallback" in result.error_message)
        check("wording", _clean_wording(result), result.error_message)

        result = _prepare(runner, guest, wd=None)
        check("no working directory refuses", _refused(result))

        no_repo = new_instance(repo, "q-n", is_owner_session=False, repo_path="", repo_name="")
        result = _prepare(runner, no_repo, wd=str(repo))
        check("no repo refuses, and does not judge the bot's cwd", _refused(result) and "no repo" in result.error_message,
              getattr(result, "error_message", result))

        git(repo, "config", "core.hooksPath", "myhooks")
        result = _prepare(runner, guest)
        git(repo, "config", "--unset", "core.hooksPath")
        check("git config /sandbox on refuses, refuses here", _refused(result) and "hooksPath" in result.error_message)
        check("wording", _clean_wording(result), result.error_message)

        bot_root = next(iter(sandbox._bot_roots()))
        bot_inst = new_instance(Path(bot_root), "q-b", is_owner_session=False, repo_name="bot")
        result = _prepare(runner, bot_inst)
        check("the bot repo refuses a guest", _refused(result) and "overlaps the bot" in result.error_message)
        bot_turn = new_instance(Path(bot_root), "q-bo", repo_name="bot")
        bot_turn.guest_confined = True
        result = _prepare(runner, bot_turn)
        check("an owner's turn in a guest thread there refuses", _refused(result))
        check("and says to start a new thread", "Start a new thread" in result.error_message, result.error_message)
        check("wording", _clean_wording(result), result.error_message)

        outside = Path(tempfile.mkdtemp(prefix="guest-outside-", dir="/tmp")).resolve()
        try:
            git(outside, "init", "-q")
            result = _prepare(runner, new_instance(outside, "q-out", is_owner_session=False))
            check("a path outside home refuses", _refused(result) and "home directory" in result.error_message,
                  getattr(result, "error_message", result))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

        store.set_sandbox_entry(str(repo), {"hosts": "bad"})
        result = _prepare(runner, guest)
        check("an unreadable setting refuses a guest", _refused(result))
        store.set_sandbox_entry(str(repo), None)

    with mock.patch.object(shutil, "which", lambda name: None):
        result = _prepare(runner, guest)
        check("a missing tool refuses, not an open fallback", _refused(result) and "not installed" in result.error_message)

    with tools, mock.patch.object(sandbox, "prepare_run", side_effect=RuntimeError("boom")):
        result = _prepare(runner, guest)
        check("a crash while preparing refuses", _refused(result))


def test_owner_unchanged() -> None:
    repo = new_repo("owner")
    store = new_store("owner", repo)
    config_before = (repo / ".git" / "config").read_text()
    runner = ClaudeRunner(store=store)
    inst = new_instance(repo, "q-o", session_id=None)
    store._instances[inst.id] = inst

    check("no sandbox for the owner", _prepare(runner, inst) is None)
    check("nothing refused in a repo a guest could not use", _prepare(
        runner, new_instance(Path(next(iter(sandbox._bot_roots()))), "q-b", repo_name="bot")) is None)
    check("nothing to merge past", runner._sandbox_merge_block(inst) is None)

    # What actually gets started, through the real run path and a fake CLI.
    dump = TMP / "owner-dump"
    binary = TMP / "fake-claude.sh"
    binary.write_text(f"""#!/bin/bash
printf '%s\\0' "$@" > "{dump}.argv"
env | cut -d= -f1 | sort > "{dump}.env"
pwd > "{dump}.cwd"
echo '{{"type":"result","subtype":"success","is_error":false,"result":"ok","session_id":"s","num_turns":1,"total_cost_usd":0}}'
""")
    binary.chmod(0o755)
    with mock.patch.object(config, "CLAUDE_BINARY", str(binary)):
        result = asyncio.run(runner.run(inst, None, None))
    argv = dump.with_suffix(".argv").read_bytes().split(b"\0") if dump.with_suffix(".argv").exists() else []
    check("the fake CLI ran", bool(argv), getattr(result, "error_message", result))
    check("no settings file on the owner's command line", b"--settings" not in argv, argv[:6])
    check("no sandbox wording in the prompt", not any(b"sandbox" in a.lower() and b"SANDBOX" in a for a in argv))
    check("runs in the repo", dump.with_suffix(".cwd").read_text().strip() == str(repo))
    check("the repo's git config is not touched", (repo / ".git" / "config").read_text() == config_before)
    check("no private run directory", not list((config.DATA_DIR / "sandbox" / "runs").glob("q-o-*"))
          if (config.DATA_DIR / "sandbox" / "runs").exists() else True)
    check("no setting appeared", store.sandbox_entries() == {})


# --- /sandbox -----------------------------------------------------------------

def test_sandbox_command() -> None:
    repo = new_repo("cmd")
    store = new_store("cmd", repo)
    guest = new_instance(repo, is_owner_session=False)

    text = sandbox_cmds.status(store, "claude")
    check("a guest-only repo is not listed", "cmd" not in text.split("Guests are")[0], text)
    check("status names the guest rule", "Guests are always confined, in every repo" in text, text)
    check("status names the hosts", all(h in text for h in sandbox.DEFAULT_HOSTS), text)

    text = sandbox_cmds.turn_off(store, "cmd")
    check("off with no setting says guests stay confined", "Guests are confined there all the same" in text, text)
    check("still confined after off", sandbox.instance_confined(store, guest))

    store.set_sandbox_entry(str(repo), {"hosts": ["example.com"]})
    text = sandbox_cmds.status(store, "claude")
    check("a repo turned on is listed", "**cmd**: example.com" in text, text)
    text = sandbox_cmds.turn_off(store, "cmd")
    check("off with a setting says guests stay confined", "Guests stay confined" in text, text)
    check("the setting is gone", store.sandbox_entries() == {})
    check("/sandbox off does not free a guest", sandbox.instance_confined(store, guest))
    check("nor leave them with no setting", sandbox.run_setting(store, guest) is not None)
    check("the owner is free again", not sandbox.instance_confined(store, new_instance(repo)))


# --- /spawn -------------------------------------------------------------------

def test_spawn() -> None:
    own = new_repo("sp-own")
    other = new_repo("sp-other")
    other_on = new_repo("sp-on")
    store = new_store("sp", own, other, other_on)
    store.set_sandbox_entry(str(other_on), {"hosts": ["github.com"]})
    spawned: list = []

    async def fake_spawn(args):
        spawned.append(args)
        return SimpleNamespace(ok=True, error=None, thread_id="th", thread_url="u", instance_id="q-new")

    async def go(inst, target, **ctx_kw):
        spawned.clear()
        store._instances[inst.id] = inst
        runner = fake_runner(
            active_instance_for_session=lambda *a, **k: inst.id,
            active_instance_for_channel=lambda *a, **k: inst.id,
        )
        messenger = FakeMessenger()
        ctx = make_ctx(store, runner=runner, messenger=messenger, spawn_session=fake_spawn, **ctx_kw)
        text = f'[BOT_CMD: /spawn repo={target} title="T"]\n~~~spawn\nA brief.\n~~~\n'
        await _execute_bot_commands(ctx, text, source_inst=inst)
        return list(spawned), messenger.posts

    guest = new_instance(own, "q-g", is_owner_session=False)
    done, posts = asyncio.run(go(guest, "sp-own", owner=False))
    check("a guest may spawn into its own repo", len(done) == 1, posts)
    done, posts = asyncio.run(go(guest, "sp-other", owner=False))
    check("not into another repo", not done and any("can only spawn into its own repo" in p for p in posts), posts)
    done, posts = asyncio.run(go(guest, "sp-on", owner=False))
    check("not even a sandboxed one", not done and any("can only spawn into its own repo" in p for p in posts), posts)

    owner_turn = new_instance(own, "q-t")
    owner_turn.guest_confined = True
    done, posts = asyncio.run(go(owner_turn, "sp-other", has_guest=True))
    check("an owner's turn in a guest thread keeps to its repo too", not done, posts)

    sandboxed_repo_owner = new_instance(other_on, "q-s")
    done, posts = asyncio.run(go(sandboxed_repo_owner, "sp-other"))
    check("an owner's sandboxed session keeps today's rule", not done and any("/spawn refused" in p for p in posts), posts)
    done, posts = asyncio.run(go(new_instance(own, "q-free"), "sp-other"))
    check("an owner's free session spawns anywhere", len(done) == 1, posts)


# --- The access fallback ------------------------------------------------------

def test_access() -> None:
    forum_thread = SimpleNamespace(parent_id=555)
    cfg = AccessConfig(users={
        "1001": UserAccess(user_id="1001", display_name="v", repos={"own": RepoAccess(mode="build", bash_policy="none")}, forum_channel_id="555"),
    })
    fake = SimpleNamespace(
        _forums=SimpleNamespace(thread_to_project=lambda _cid: None),
        _is_owner=lambda uid: uid == 7,
        _resolve_repo_from_user_forum=lambda _cid, _uid: None,
    )
    fake._in_own_forum = lambda cid, holder: bot_mod.ClaudeBot._in_own_forum(fake, cid, holder)
    fake.get_channel = lambda cid: forum_thread if cid == 777 else None

    with mock.patch.object(bot_mod, "load_access_config", lambda: cfg), \
            mock.patch.object(bot_mod.discord, "Thread", SimpleNamespace):
        chk = lambda uid, cid: bot_mod.ClaudeBot._check_access(fake, uid, channel_id=cid)  # noqa: E731
        check("a grant in an unmapped channel is denied", not chk(1001, "999").allowed)
        check("no channel at all is denied", not chk(1001, None).allowed)
        check("their own forum is allowed", chk(1001, "555").allowed)
        check("an untagged thread in it is allowed", chk(1001, "777").allowed)
        check("at their tightest settings", chk(1001, "777").bash_policy == "none")
        check("the owner is not affected", chk(7, "999").allowed)
        check("no grant is denied", not chk(2002, "555").allowed)

    slash = (ROOT / "bot/discord/slash_commands.py").read_text(encoding="utf-8")
    check("/new asks whether they hold any grant", "has_any_access(load_access_config()" in slash)


def test_query_hard_stop() -> None:
    repo = new_repo("stop")
    store = new_store("stop", repo)
    store.switch_repo("stop")
    before = len(store._instances)
    messenger = FakeMessenger()
    ctx = make_ctx(store, owner=False, messenger=messenger)
    asyncio.run(query._execute_query(ctx, "hello"))
    check("a guest with no repo gets no run", len(store._instances) == before)
    check("and is told", any("no repo you have a grant for" in p for p in messenger.posts), messenger.posts)

    ctx = make_ctx(store, owner=True, messenger=FakeMessenger())
    try:
        asyncio.run(query._execute_query(ctx, "hello"))
    except ReachedTheRun:
        pass
    check("the owner still lands in the active repo", newest(store).repo_name == "stop")


# --- TODO.md in a repo with no setting ----------------------------------------

def test_todo() -> None:
    repo = new_repo("todo")
    store = new_store("todo", repo)
    outside = TMP / "todo-outside.md"
    original = "## Deferred Revisions\n- [ ] secret line\n"
    outside.write_text(original)
    todo = repo / "TODO.md"

    todo.symlink_to(outside)
    check("a link is not followed on read", store.get_deferred_items("todo") == [])
    store.append_deferred("todo", ["new item"])
    check("nor written through", outside.read_text() == original)
    store.clear_deferred("todo")
    check("nor cleared through", outside.read_text() == original)
    todo.unlink()

    os.mkfifo(todo)
    outcome: list = []
    worker = threading.Thread(target=lambda: outcome.append(store.get_deferred_items("todo")), daemon=True)
    worker.start()
    worker.join(5)
    check("a FIFO does not hang the bot", not worker.is_alive() and outcome == [[]], outcome)
    if worker.is_alive():
        os.open(todo, os.O_RDWR | os.O_NONBLOCK)
    todo.unlink()

    store.append_deferred("todo", ["plain item"])
    check("a plain file still works", "plain item" in todo.read_text())
    check("and reads back", any("plain item" in i for i in store.get_deferred_items("todo")))
    check("and clears", store.clear_deferred("todo") >= 1)


# --- Call sites agree ---------------------------------------------------------

def test_sites_agree() -> None:
    repo = new_repo("agree")
    store = new_store("agree", repo)
    runner = ClaudeRunner(store=store)
    git(repo, "checkout", "-qb", "guest/work")
    (repo / ".github").mkdir()
    (repo / ".github" / "x.yml").write_text("x\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "workflow")
    git(repo, "checkout", "-q", "master")

    guest = new_instance(repo, "q-g", is_owner_session=False, branch="guest/work", original_branch="master")
    owner = new_instance(repo, "q-o", branch="guest/work", original_branch="master")
    owner_turn = new_instance(repo, "q-t", branch="guest/work", original_branch="master")
    owner_turn.guest_confined = True

    check("merge: a guest's branch waits for the owner", "changes" in (runner._sandbox_merge_block(guest) or ""))
    check("merge: so does a guest thread's", "changes" in (runner._sandbox_merge_block(owner_turn) or ""))
    check("merge: an owner's does not", runner._sandbox_merge_block(owner) is None)

    with mock.patch.object(shutil, "which", lambda name: f"/usr/bin/{name}"):
        prepared = _prepare(runner, guest)
    check("prepare agrees", isinstance(prepared, sandbox.SandboxRun), prepared)
    if isinstance(prepared, sandbox.SandboxRun):
        sandbox.finish_run(prepared)
    check("prepare agrees for the owner", _prepare(runner, owner) is None)


def main() -> int:
    tests = (test_answer, test_persistence, test_routes, test_family_walk, test_source_scan,
             test_refusals, test_owner_unchanged, test_sandbox_command, test_spawn, test_access,
             test_query_hard_stop, test_todo, test_sites_agree)
    try:
        for test in tests:
            print(test.__name__, flush=True)
            try:
                test()
            except Exception:
                global FAILED
                FAILED += 1
                print(f"  FAIL: {test.__name__} crashed")
                traceback.print_exc()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
