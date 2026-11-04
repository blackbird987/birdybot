#!/usr/bin/env python3
"""Harness for the tap before auto-update applies (docs/design/peer-link.md).

Real git against a throwaway origin and clone, with the Discord side and pip
faked. Nothing here touches the network or the real checkout.
"""

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord  # noqa: E402

from bot import auto_update, config  # noqa: E402
from bot.discord import interactions  # noqa: E402
from bot.discord.bot import ClaudeBot  # noqa: E402
from bot.platform.base import NotificationService  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def git(cwd, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


class Store:
    """The two calls the code makes, over a dict a 'restart' can keep."""
    def __init__(self):
        self.data: dict = {}

    def get_platform_state(self, key):
        return self.data.get(key, {})

    def set_platform_state(self, key, value, *, persist=True):
        self.data[key] = value


class Runner:
    def __init__(self, store):
        self._store = store
        self.reboots: list = []
        self._lock = asyncio.Lock()

    def _get_repo_lock(self, _path):
        return self._lock

    def request_reboot(self, data):
        self.reboots.append(data)


class Notifier:
    def __init__(self, delivered=1):
        self.sent: list = []
        self.delivered = delivered

    async def broadcast(self, text, buttons=None, silent=False, ttl=None):
        self.sent.append(SimpleNamespace(text=text, buttons=buttons))
        return self.delivered


class Repos:
    """A bare origin, a clone playing the bot's checkout, a clone playing the pusher."""
    def __init__(self, tmp: Path):
        self.origin, self.bot, self.pusher = tmp / "origin.git", tmp / "bot", tmp / "pusher"
        git(tmp, "init", "--bare", "-b", "master", str(self.origin))
        git(tmp, "clone", str(self.origin), str(self.pusher))
        git(self.pusher, "checkout", "-b", "master")
        self.push("first")
        git(tmp, "clone", str(self.origin), str(self.bot))

    def push(self, subject: str) -> str:
        path = self.pusher / "file.txt"
        path.write_text(subject, encoding="utf-8")
        git(self.pusher, "add", ".")
        git(self.pusher, "commit", "-m", subject)
        git(self.pusher, "push", "origin", "master")
        return git(self.pusher, "rev-parse", "HEAD")

    def head(self) -> str:
        return git(self.bot, "rev-parse", "HEAD")


async def polls(runner, notifier, count: int = 3) -> None:
    """Run the real loop for a few polls."""
    seen = {"n": 0}
    real = auto_update._git

    async def counting(*args, timeout):
        if args[0] == "fetch":
            seen["n"] += 1
        return await real(*args, timeout=timeout)

    auto_update._git = counting
    stop = asyncio.Event()
    task = asyncio.create_task(auto_update.auto_update_loop(stop, runner, notifier))
    try:
        while seen["n"] < count and not task.done():
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
        auto_update._git = real


def setup_config(root: Path, approval: bool = True) -> None:
    config._PROJECT_ROOT = root
    config.AUTO_UPDATE = True
    config.AUTO_UPDATE_APPROVAL = approval
    config.AUTO_UPDATE_BRANCH = "master"
    config.AUTO_UPDATE_INTERVAL_SECS = 0.01


pip_calls: list = []
_real_run_capture = auto_update.run_capture


def _no_pip(cmd, **kwargs):
    if cmd[0] == sys.executable:
        pip_calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return _real_run_capture(cmd, **kwargs)


auto_update.run_capture = _no_pip


async def scenario_no_pull_and_dedupe(repos: Repos) -> None:
    before = repos.head()
    sha = repos.push("Fix a thing @everyone `tick` <@1> ‮")
    store = Store()
    runner, notifier = Runner(store), Notifier()
    await polls(runner, notifier)
    check("no pull before the tap", repos.head() == before)
    check("no reboot before the tap", not runner.reboots)
    check("one card over several polls", len(notifier.sent) == 1, len(notifier.sent))
    card = notifier.sent[0]
    check("the card names the exact sha on its buttons",
          [b.callback_data for b in card.buttons[0]]
          == [f"auto_update_apply:{sha}", f"auto_update_later:{sha}"])
    check("the card has the count and files", "1 commit(s), 1 file(s) changed" in card.text, card.text)
    check("a subject cannot ping or close the code block",
          "@everyone" not in card.text and "<@1>" not in card.text
          and "`tick`" not in card.text and "‮" not in card.text, card.text)

    # A restart: new runner and notifier, the same persisted state.
    again = Notifier()
    await polls(Runner(store), again)
    check("a restart does not post it again", not again.sent)

    newer = repos.push("Second thing")
    third = Notifier()
    await polls(Runner(store), third)
    check("a newer sha gets a new card",
          len(third.sent) == 1 and newer in third.sent[0].buttons[0][0].callback_data)

    old = await auto_update.apply_update(runner, notifier, sha)
    check("tapping the older card explains", not old.applied and "newer" in old.message, old)
    check("and applies nothing", repos.head() == before)


async def scenario_undelivered(repos: Repos) -> None:
    repos.push("Undelivered")
    store = Store()
    await polls(Runner(store), Notifier(delivered=0), count=2)
    check("a card that did not go out is not on record", not auto_update.carded_sha(store))
    retry = Notifier()
    await polls(Runner(store), retry, count=2)
    check("and goes out on a later poll", len(retry.sent) == 1, len(retry.sent))


async def scenario_exact_apply(repos: Repos) -> None:
    sha = repos.push("Apply me")
    store = Store()
    runner, notifier = Runner(store), Notifier()
    await polls(runner, notifier)

    other = repos.push("Landed after the card")
    outcome = await auto_update.apply_update(runner, notifier, sha)
    check("a moved origin applies nothing", not outcome.applied and "moved" in outcome.message, outcome)
    check("and posts a fresh card",
          len(notifier.sent) == 2 and other in notifier.sent[1].buttons[0][0].callback_data)
    check("the checkout did not move", repos.head() != other and repos.head() != sha)

    outcome = await auto_update.apply_update(runner, notifier, other)
    check("the fresh card applies", outcome.applied, outcome)
    check("HEAD is exactly the carded sha", repos.head() == other)
    check("the outcome counts the commits", outcome.n_commits >= 2, outcome.n_commits)
    check("no reboot until the card is updated", not runner.reboots)
    pip_calls.clear()
    await auto_update.finish_update(runner, notifier, outcome.n_commits, outcome.latest_msg)
    check("then the same install, reboot and notice",
          len(pip_calls) == 1 and len(runner.reboots) == 1 and "Rebooting" in notifier.sent[-1].text)

    outcome = await auto_update.apply_update(runner, notifier, other)
    check("a second tap does nothing", not outcome.applied and "already" in outcome.message, outcome)


async def scenario_refusals(repos: Repos) -> None:
    store = Store()
    runner, notifier = Runner(store), Notifier()
    sha = repos.push("Diverge from me")
    await polls(runner, notifier, count=1)
    (repos.bot / "local.txt").write_text("local", encoding="utf-8")
    git(repos.bot, "add", ".")
    git(repos.bot, "commit", "-m", "local only")
    local = repos.head()
    outcome = await auto_update.apply_update(runner, notifier, sha)
    check("a non-fast-forward is refused",
          not outcome.applied and outcome.keep_buttons and "fast-forward" in outcome.message, outcome)
    check("and the checkout stays", repos.head() == local)
    outcome = await auto_update.apply_update(runner, notifier, "not-a-sha")
    check("a malformed sha is refused", not outcome.applied)
    git(repos.bot, "reset", "--hard", "HEAD~1")


async def scenario_off_switch(repos: Repos) -> None:
    setup_config(repos.bot, approval=False)
    sha = repos.push("No tap needed")
    runner, notifier = Runner(Store()), Notifier()
    pip_calls.clear()
    await polls(runner, notifier, count=1)
    check("with approval off it pulls as before", repos.head() == sha)
    check("and installs and reboots", len(pip_calls) == 1 and len(runner.reboots) == 1)
    check("with no card", not any(n.buttons for n in notifier.sent))
    setup_config(repos.bot)


async def scenario_tap_gates(repos: Repos) -> None:
    sha = repos.push("Tap me")
    store = Store()
    runner, notifier = Runner(store), Notifier()
    await polls(runner, notifier)
    before = repos.head()

    def tap(action: str, is_owner: bool):
        bot = MagicMock()
        bot._check_access.return_value = SimpleNamespace(allowed=True, is_owner=is_owner)
        bot._runner, bot._notifier = runner, notifier
        interaction = MagicMock()
        interaction.data = {"custom_id": f"{action}:{sha}"}
        interaction.response.is_done.return_value = True
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        interaction.message.content = "the card"
        return bot, interaction

    for action in (auto_update.APPLY_ACTION, auto_update.LATER_ACTION):
        bot, interaction = tap(action, is_owner=False)
        await interactions.handle(bot, interaction)
        check(f"a non-owner {action} is refused",
              interaction.followup.send.await_args.args[0] == "Owner only."
              and not interaction.edit_original_response.await_count)
    check("and nothing moved", repos.head() == before)

    bot, interaction = tap(auto_update.LATER_ACTION, is_owner=True)
    await interactions.handle(bot, interaction)
    kept = interaction.edit_original_response.await_args.kwargs["view"]
    check("Later leaves Apply on the card",
          [c.custom_id for c in kept.children] == [f"auto_update_apply:{sha}"])
    check("and moves nothing", repos.head() == before)

    pip_calls.clear()
    bot, interaction = tap(auto_update.APPLY_ACTION, is_owner=True)
    await interactions.handle(bot, interaction)
    check("the owner's Apply lands the commit", repos.head() == sha)
    check("the card is updated and its buttons go",
          interaction.edit_original_response.await_args.kwargs["view"] is None)
    check("and the reboot is asked for", len(runner.reboots) == 1 and len(pip_calls) == 1)


async def scenario_purge_spares_card() -> None:
    sha = "a" * 40
    store = Store()
    store.set_platform_state("auto_update", {"carded_sha": sha})

    def message(mid, custom_id=None):
        button = SimpleNamespace(custom_id=custom_id)
        row = SimpleNamespace(children=[button])
        return SimpleNamespace(id=mid, components=[row] if custom_id else [], delete=AsyncMock())

    live = message(1, f"auto_update_apply:{sha}")
    stale = message(2, f"auto_update_apply:{'b' * 40}")
    plain = message(3)
    checks: list = []

    async def purge(limit, check):
        checks.append(check)
        return []

    async def history(limit):
        for m in (live, stale, plain):
            yield m

    lobby = MagicMock(spec=discord.TextChannel)
    lobby.pins = AsyncMock(return_value=[])
    lobby.purge = purge
    lobby.history = history
    bot = SimpleNamespace(_lobby_channel_id=1, _store=store, get_channel=lambda _i: lobby)
    await ClaudeBot._cleanup_lobby(bot)
    keep = checks[0]
    check("the purge spares the live card", not keep(live) and not live.delete.await_count)
    check("and takes the superseded one", keep(stale) and stale.delete.await_count == 1)
    check("and everything else", keep(plain) and plain.delete.await_count == 1)


async def scenario_broadcast_count() -> None:
    service = NotificationService()
    check("no messenger means nothing delivered", await service.broadcast("x") == 0)
    messenger = SimpleNamespace(platform_name="p", send_text=AsyncMock(return_value="1"))
    service.register(messenger, "c")
    check("a sent message counts", await service.broadcast("x") == 1)
    messenger.send_text = AsyncMock(return_value="")
    check("an unresolved channel does not", await service.broadcast("x") == 0)
    messenger.send_text = AsyncMock(side_effect=RuntimeError("down"))
    check("nor does a failed send", await service.broadcast("x") == 0)


def test_default_on() -> None:
    src = (Path(__file__).resolve().parent.parent / "bot" / "config.py").read_text(encoding="utf-8")
    check("approval defaults on with no .env entry",
          'os.getenv("AUTO_UPDATE_APPROVAL", "1")' in src and "not in (" in src)
    check("both buttons are owner only",
          {auto_update.APPLY_ACTION, auto_update.LATER_ACTION} <= interactions._OWNER_ONLY_ACTIONS)


async def main() -> None:
    test_default_on()
    await scenario_purge_spares_card()
    await scenario_broadcast_count()
    for scenario in (scenario_no_pull_and_dedupe, scenario_undelivered, scenario_exact_apply,
                     scenario_refusals, scenario_off_switch, scenario_tap_gates):
        with tempfile.TemporaryDirectory() as tmp:
            repos = Repos(Path(tmp))
            setup_config(repos.bot)
            await scenario(repos)


if __name__ == "__main__":
    asyncio.run(main())
    print("\n" + ("All checks passed" if not failures else f"FAILED: {failures}"))
    sys.exit(1 if failures else 0)
