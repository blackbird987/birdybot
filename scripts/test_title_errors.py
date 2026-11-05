"""Regression test: an error answer can never become a thread title.

The naming call was pinned to the first account and took whatever text came back.
With that account at its limit the CLI answers "You've hit your weekly limit ...",
and that sentence became the thread's name. This pins that the answer is judged on
the CLI's error flag and exit code, that the call runs on an account free to answer,
and that the threads already named this way are repaired once.

Self-contained: a fake CLI stands in for the real one and a fake client for Discord.
Run: python scripts/test_title_errors.py
"""

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord

from bot import config
from bot.claude.runner import ClaudeRunner
from bot.discord import bot as bot_mod
from bot.discord import forums as forums_mod
from bot.discord import channels, spawn_colors, titles
from bot.discord.forums import ForumManager, ForumProject, ThreadInfo

FAILS: list[str] = []
LIMIT = "You've hit your weekly limit \u00b7 resets Nov 9, 10am (Europe/Amsterdam)"
SLEEP = "\U0001f4a4"
# What a damaged thread is really called: a name has its punctuation stripped.
LIVE = "Youve hit your limit resets Mar"


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


# --- The naming call, against a fake CLI ------------------------------------


class FakeCli:
    """A script standing in for the CLI: records its call, prints a set answer."""

    def __init__(self, root: Path):
        self.root = root
        self.record = root / "call.json"
        script = root / "fake-claude"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"root = {str(root)!r}\n"
            "with open(os.path.join(root, 'call.json'), 'w') as f:\n"
            "    json.dump({'argv': sys.argv[1:], 'config_dir': os.environ.get('CLAUDE_CONFIG_DIR'), 'has_key': 'ANTHROPIC_API_KEY' in os.environ}, f)\n"
            "print(open(os.path.join(root, 'answer')).read())\n"
            "sys.exit(int(open(os.path.join(root, 'code')).read()))\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.path = str(script)

    def answers(self, text: str, *, is_error: bool = False, code: int = 0) -> None:
        event = {
            "type": "result", "subtype": "success", "is_error": is_error,
            "result": text, "session_id": "s",
        }
        (self.root / "answer").write_text(json.dumps(event), encoding="utf-8")
        (self.root / "code").write_text(str(code), encoding="utf-8")
        self.record.unlink(missing_ok=True)

    def call(self) -> dict | None:
        return json.loads(self.record.read_text()) if self.record.exists() else None


async def test_answers(cli: FakeCli) -> None:
    print("what counts as a title")
    # The exact shape the real CLI gave: subtype "success", error flag up, exit 1.
    cli.answers(LIMIT, is_error=True, code=1)
    check(await titles.generate_title_text("p") is None, "the limit answer is not a title")
    cli.answers("Fix The Login Bug", is_error=True, code=0)
    check(await titles.generate_title_text("p") is None, "the error flag alone is enough, whatever the words")
    cli.answers("Fix The Login Bug", code=1)
    check(await titles.generate_title_text("p") is None, "a failed exit alone is enough")
    cli.answers("Fix The Login Bug.")
    check(await titles.generate_title_text("p") == "Fix The Login Bug", "a good answer is a title")
    check("--no-session-persistence" in cli.call()["argv"], "the call leaves no session behind")


async def test_account(cli: FakeCli) -> None:
    print("which account answers")
    cli.answers("A Title Here")
    saved_key = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "fake-for-this-test"
    try:
        await titles.generate_title_text("p", account="/tmp/acct-b")
    finally:
        if saved_key is None:
            del os.environ["ANTHROPIC_API_KEY"]
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved_key
    check(cli.call()["config_dir"] == "/tmp/acct-b", "the chosen account is the one that runs")
    # With the key in its environment the CLI bills that and ignores the account.
    check(cli.call()["has_key"] is False, "and the paid API key never reaches the call")
    saved = list(config.CLAUDE_ACCOUNTS)
    config.CLAUDE_ACCOUNTS[:] = ["/tmp/acct-a", "/tmp/acct-b"]
    try:
        await titles.generate_title_text("p")
        check(cli.call()["config_dir"] == "/tmp/acct-a", "with none chosen it is still the primary")
    finally:
        config.CLAUDE_ACCOUNTS[:] = saved


class _RunnerStore:
    """Just enough store for a real ClaudeRunner to choose an account."""

    def get_account_cooldowns(self):
        return {}

    def get_account_cooldown_fps(self):
        return {}

    def get_model_cooldowns(self):
        return {}

    def __getattr__(self, name):
        return lambda *a, **k: None


def _smart_title_bot(runner, info: ThreadInfo) -> SimpleNamespace:
    proj = ForumProject(repo_name="r", forum_channel_id="1", threads={"7": info})
    return SimpleNamespace(
        _runner=runner,
        _name_editing=set(),
        _store=_ColorStore(),
        _forums=SimpleNamespace(
            thread_to_project=lambda tid: (proj, info),
            save_forum_map=lambda: None,
        ),
    )


class _SmartThread:
    id = 7

    def __init__(self):
        self.renamed: list[str] = []

    async def edit(self, *, name):
        self.renamed.append(name)


async def test_smart_title(cli: FakeCli, tmp: Path) -> None:
    print("the naming path, with a real account picker")
    acct_a, acct_b = str(tmp / "acct-a"), str(tmp / "acct-b")
    os.makedirs(acct_a), os.makedirs(acct_b)
    saved = list(config.CLAUDE_ACCOUNTS)
    saved_provider = config.PROVIDER
    saved_read = bot_mod.read_ai_title
    config.CLAUDE_ACCOUNTS[:] = [acct_a, acct_b]
    config.PROVIDER = "claude"
    bot_mod.read_ai_title = lambda sid: None
    try:
        runner = ClaudeRunner(store=_RunnerStore())
        runner._unusable_accounts = lambda: set()  # the fake dirs hold no login
        soon = datetime.now(timezone.utc) + timedelta(hours=2)

        info, thread = ThreadInfo(thread_id="7", session_id="s"), _SmartThread()
        cli.answers("Named By The Free Account")
        runner._account_cooldowns[acct_a] = soon
        await bot_mod.ClaudeBot._generate_smart_title(_smart_title_bot(runner, info), thread, "p")
        check(cli.call()["config_dir"] == acct_b, "an account at its limit is passed over")
        check(info.topic == "Named By The Free Account" and thread.renamed, "and the thread gets named")

        runner._account_cooldowns.clear()
        runner._model_cooldowns[acct_a] = soon
        info, thread = ThreadInfo(thread_id="7", session_id="s"), _SmartThread()
        cli.answers("Named By The Free Account")
        await bot_mod.ClaudeBot._generate_smart_title(_smart_title_bot(runner, info), thread, "p")
        check(cli.call()["config_dir"] == acct_b, "so is one out of only its primary model")

        runner._model_cooldowns.clear()
        runner._account_cooldowns[acct_a] = runner._account_cooldowns[acct_b] = soon
        info, thread = ThreadInfo(thread_id="7", session_id="s"), _SmartThread()
        cli.answers("Should Never Run")
        await bot_mod.ClaudeBot._generate_smart_title(_smart_title_bot(runner, info), thread, "p")
        check(cli.call() is None, "with every account cooling nothing is spawned")
        check(info._title_generated is False and not thread.renamed, "and a later turn may try again")

        runner._account_cooldowns.clear()
        info, thread = ThreadInfo(thread_id="7", session_id="s"), _SmartThread()
        cli.answers(LIMIT, is_error=True, code=1)
        await bot_mod.ClaudeBot._generate_smart_title(_smart_title_bot(runner, info), thread, "p")
        check(not thread.renamed and info.topic == "", "an error answer renames nothing")
        check(info._title_generated is False, "and a later turn may try again")
    finally:
        config.CLAUDE_ACCOUNTS[:] = saved
        config.PROVIDER = saved_provider
        bot_mod.read_ai_title = saved_read


def test_matcher() -> None:
    print("spotting old damage")
    match = titles.is_limit_message_title
    check(match(LIMIT), "the weekly wording")
    check(match("You've hit your limit \u00b7 resets 3pm (UTC)"), "the plain wording")
    check(match("You\u2019ve hit your weekly limit \u00b7 resets Nov 9"), "a curly apostrophe")
    check(match(LIVE), "a thread's real name, punctuation stripped")
    check(match(channels.build_title_name(LIMIT)), "which is what the name builder makes of it")
    for real in ("Weekly Usage Limit Check", "Usage limit retry message date fix",
                 "You have hit a limit in the parser", "", None):
        check(not match(real), f"leaves {real!r} alone")


# --- The repair, against a fake Discord ------------------------------------


class FakeThread:
    def __init__(self, tid: int, name: str, *, archived: bool = False, fail: Exception | None = None):
        self.id = tid
        self.name = name
        self.archived = archived
        self.fail = fail
        self.calls: list[dict] = []

    async def edit(self, **fields):
        # Mirrors Discord error 50083: an archived thread accepts only `archived`.
        if self.archived and (set(fields) != {"archived"}):
            raise RuntimeError("Thread is archived (50083)")
        if self.fail is not None and "name" in fields:
            raise self.fail
        self.calls.append(fields)
        self.name = fields.get("name", self.name)
        self.archived = fields.get("archived", self.archived)
        return self


class FakeClient:
    def __init__(self, threads: dict[int, FakeThread], *, cached: tuple = (), errors: dict | None = None):
        self.threads = threads
        self.cached = cached
        self.errors = errors or {}
        self.fetches = 0

    def get_channel(self, cid):
        return self.threads.get(cid) if cid in self.cached else None

    async def fetch_channel(self, cid):
        self.fetches += 1
        if cid in self.errors:
            raise self.errors[cid]
        if cid not in self.threads:
            raise _not_found()
        return self.threads[cid]


def _not_found() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")


def _forbidden() -> discord.Forbidden:
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")


class _ColorStore:
    def __init__(self):
        self.state: dict = {}

    def get_platform_state(self, name):
        return self.state

    def set_platform_state(self, name, state, persist=True):
        self.state = state


def _manager(client: FakeClient, infos: list[ThreadInfo]) -> ForumManager:
    mgr = object.__new__(ForumManager)
    mgr._client = client
    mgr._store = _ColorStore()
    mgr._forum_projects = {"r": ForumProject(
        repo_name="r", forum_channel_id="1", threads={i.thread_id: i for i in infos},
    )}
    mgr.saves = 0

    def save():
        mgr.saves += 1

    mgr.save_forum_map = save
    return mgr


async def test_repair() -> None:
    print("repairing old titles")
    saved_thread, saved_pace = discord.Thread, forums_mod.REPAIR_TITLE_PACE_SECS
    saved_read = forums_mod.read_ai_title
    discord.Thread = FakeThread
    forums_mod.REPAIR_TITLE_PACE_SECS = 0
    forums_mod.read_ai_title = lambda sid: "Refactor The Parser" if sid == "has-title" else None
    title_base = "Refactor The Parser"
    try:
        info = lambda tid, topic, sid=None, **kw: ThreadInfo(thread_id=str(tid), session_id=sid, topic=topic, **kw)

        # Each of the shapes a damaged thread comes in, in one forum. The ones that
        # cannot be fixed go first, to show they do not stop the rest.
        slot_color = spawn_colors.compose_for_slot(0, "x", is_root=True).removesuffix("x")
        threads = {
            1: FakeThread(1, LIVE),
            2: FakeThread(2, LIVE, archived=True),
            3: FakeThread(3, f"{SLEEP} {LIVE}", archived=True),
            4: FakeThread(4, LIVE),
            5: FakeThread(5, "Weekly Usage Limit Check"),
            6: FakeThread(6, f"{slot_color}{SLEEP} {LIVE}"),
            8: FakeThread(8, f"{SLEEP} | {LIVE}", archived=True),
            11: FakeThread(11, "A Name Someone Chose"),
            12: FakeThread(12, f"{SLEEP} A Name Someone Chose", archived=True),
            13: FakeThread(13, "new-session"),
        }
        infos = [
            info(9, LIMIT, "has-title"),  # deleted
            info(10, LIMIT, "has-title"),  # no access
            info(1, LIMIT, "has-title"),
            info(2, LIMIT, "has-title"),
            info(3, LIMIT, "has-title"),
            info(4, LIMIT, None),
            info(5, "Weekly Usage Limit Check", "has-title"),
            info(6, LIMIT, "has-title", color_slot=0),
            info(8, "You've hit your limit \u00b7 resets 3pm (UTC)", None),
            info(11, LIMIT, "has-title"),
            info(12, LIMIT, None),
            info(13, LIMIT, "has-title"),  # renamed by a run that died before saving
        ]
        client = FakeClient(threads, cached=(1, 4), errors={10: _forbidden()})
        mgr = _manager(client, infos)
        by_id = {i.thread_id: i for i in infos}
        editing: set[str] = set()
        await mgr.repair_limit_titles(editing)

        check(threads[1].name == title_base and by_id["1"].topic == title_base, "a live thread takes its session's own title")
        check(by_id["1"]._title_generated is True, "and is not named again")
        check(threads[2].name == title_base and threads[2].archived, "an archived thread is renamed and put back to sleep")
        check(threads[2].calls == [{"archived": False}, {"name": title_base, "archived": True}],
              "woken first, then renamed and archived in one request")
        check(threads[3].name == f"{SLEEP} {title_base}", "the sleep marker stays")
        check(threads[4].name == "new-session" and by_id["4"].topic == "new-session", "no session title goes back to new-session")
        check(by_id["4"]._title_generated is False, "and a later turn may name it")
        check(threads[5].calls == [] and by_id["5"].topic == "Weekly Usage Limit Check", "a real title about limits is untouched")
        check(threads[6].name.startswith(slot_color) and title_base in threads[6].name and SLEEP in threads[6].name,
              "the family colour stays too")
        check(threads[8].name == f"{SLEEP} new-session" and threads[8].archived, "the old sleep marker with a bar is understood")
        check(threads[11].calls == [] and by_id["11"].topic == "A Name Someone Chose", "a name chosen since is kept, and the saved title follows it")
        check(by_id["11"]._title_generated is True, "and is not named again")
        check(threads[12].calls == [] and by_id["12"].topic == "A Name Someone Chose", "also on a sleeping thread, without its marker")
        check(threads[13].calls == [] and by_id["13"].topic == "new-session", "a repair that died before saving is not redone")
        check(by_id["13"]._title_generated is False, "and a later turn may still name it")
        check(by_id["9"].topic == title_base, "a deleted thread stops matching")
        check(titles.is_limit_message_title(by_id["10"].topic), "one that could not be reached is left for the next boot")
        check(threads[1].name == title_base, "and neither stopped the ones after it")
        check(not editing, "no rename guard left behind")
        check(mgr.saves == 1, "saved once")

        print("  second run")
        before = {t.id: list(t.calls) for t in threads.values()}
        client.errors.clear()  # the unreachable one recovers
        threads[10] = FakeThread(10, LIVE)
        fetches = client.fetches
        await mgr.repair_limit_titles(editing)
        check(all(threads[k].calls == before[k] for k in before), "a second run renames nothing already fixed")
        check(threads[10].name == title_base, "and picks up the one that failed")
        check(client.fetches == fetches + 1, "it only looks at what still matches")
        saves = mgr.saves
        await mgr.repair_limit_titles(editing)
        check(mgr.saves == saves, "with nothing left it does not even save")

        print("  awkward cases")
        busy = FakeThread(20, LIVE)
        mgr = _manager(FakeClient({20: busy}), [info(20, LIMIT, "has-title")])
        guard = {"20"}
        await mgr.repair_limit_titles(guard)
        check(busy.calls == [] and guard == {"20"}, "a thread mid-rename is left, guard and all")
        check(titles.is_limit_message_title(mgr._forum_projects["r"].threads["20"].topic), "and matches again next boot")

        vanished = FakeThread(21, LIVE, fail=_not_found())
        after = FakeThread(22, LIVE)
        mgr = _manager(FakeClient({21: vanished, 22: after}), [info(21, LIMIT, "has-title"), info(22, LIMIT, "has-title")])
        await mgr.repair_limit_titles(set())
        check(mgr._forum_projects["r"].threads["21"].topic == title_base, "a thread deleted during the rename stops matching")
        check(after.name == title_base, "and the next one still gets done")

        broken = FakeThread(23, LIVE, archived=True, fail=RuntimeError("500"))
        mgr = _manager(FakeClient({23: broken}), [info(23, LIMIT, "has-title")])
        await mgr.repair_limit_titles(set())
        check(broken.archived and broken.calls[-1] == {"archived": True}, "a failed rename does not leave an old post awake")
        check(titles.is_limit_message_title(mgr._forum_projects["r"].threads["23"].topic), "and is tried again next boot")
        check(mgr.saves == 0, "with nothing fixed nothing is saved")
    finally:
        discord.Thread = saved_thread
        forums_mod.REPAIR_TITLE_PACE_SECS = saved_pace
        forums_mod.read_ai_title = saved_read


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="test-title-errors-"))
    saved_binary = config.CLAUDE_BINARY
    try:
        cli = FakeCli(tmp)
        config.CLAUDE_BINARY = cli.path
        test_matcher()
        await test_answers(cli)
        await test_account(cli)
        await test_smart_title(cli, tmp)
        await test_repair()
    finally:
        config.CLAUDE_BINARY = saved_binary
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
    if FAILS:
        print(f"\n{len(FAILS)} FAILED:")
        for label in FAILS:
            print(f"  - {label}")
        sys.exit(1)
    print("\nAll title error checks passed.")
