"""End-to-end simulation of the fleet-ship pipeline with faked seams.

Drives the REAL orchestration code (run_fleet_ship, _deploy_and_verify,
drain_pending_verify, _verify_one) while faking the leaf operations
(_finalize_merge, execute_deploy, _replay_to_thread, Discord sends) so
the sequencing and state behavior can be asserted without a live bot:

1. happy path (command deploy): merge -> persist BEFORE deploy -> deploy
   -> verify replay per thread -> new completed instance -> thread closed
2. merge failure gates the repo: no deploy, no verify, pending_merge set
3. deploy failure: verify set popped, no replay fired
4. self deploy: reboot requested, set persists; boot drain fires replays
5. no-op replay guard: replay that runs no turn -> thread NOT closed

Run: python scripts/verify_fleet_sim.py   (exit 0 = all pass)
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import asyncio
from types import SimpleNamespace

from bot.claude.runner import RebootResult
from bot.claude.types import InstanceStatus
from bot.discord import fleet
import bot.engine.workflows as workflows
import bot.discord.interactions as interactions

_failures: list[str] = []


def _check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


class FakeStore:
    def __init__(self) -> None:
        self._ps: dict = {}
        self.deploy_configs: dict = {}
        self.pending_merges: dict = {}
        self.instances: list = []       # newest-first
        self.events: list[str] = []

    # platform_state seam (pending-verify persistence)
    def get_platform_state(self, platform):
        return self._ps.get(platform, {})

    def set_platform_state(self, platform, data, *, persist=True):
        self._ps[platform] = data
        self.events.append("persist_state")

    # deploy
    def get_deploy_config(self, repo):
        return self.deploy_configs.get(repo)

    # pending merges
    def set_pending_merge(self, iid, **kw):
        self.pending_merges[iid] = kw
        self.events.append(f"set_pending_merge:{iid}")

    def get_pending_merge_by_session(self, sid):
        for iid, meta in self.pending_merges.items():
            if meta.get("session_id") == sid:
                return iid, meta
        return None

    def get_pending_merge_by_channel(self, cid):
        for iid, meta in self.pending_merges.items():
            if meta.get("channel_id") == cid:
                return iid, meta
        return None

    def clear_pending_merge(self, iid):
        self.pending_merges.pop(iid, None)

    def list_instances(self, all_=False):
        return list(self.instances)


class FakeMessenger:
    def __init__(self, store):
        self.store = store
        self.sent: list[tuple[str, str]] = []
        self.closed: list[str] = []

    async def send_text(self, channel_id, text, **kw):
        self.sent.append((channel_id, text))
        self.store.events.append(f"say:{channel_id}")

    async def close_conversation(self, channel_id, *, skip_mention=False):
        self.closed.append(channel_id)
        self.store.events.append(f"close:{channel_id}")


class FakeRunner:
    def __init__(self, store):
        self.store = store
        self._last_merge_failure_kind: dict = {}
        self.reboots: list[dict] = []

    def request_reboot(self, data, **kw):
        self.reboots.append(data)
        self.store.events.append("request_reboot")
        return RebootResult.QUEUED

    def is_session_active(self, sid):
        return False


def make_bot():
    store = FakeStore()
    messenger = FakeMessenger(store)
    runner = FakeRunner(store)
    bot = SimpleNamespace()
    bot._store = store
    bot.messenger = messenger
    bot._runner = runner
    bot._forums = SimpleNamespace(
        thread_to_project=lambda tid: (None, SimpleNamespace(session_id=f"sess-{tid}")),
    )
    bot._ctx = lambda cid, **kw: SimpleNamespace(
        channel_id=cid, store=store, runner=runner, messenger=messenger,
    )

    # replay fake: records the call; if bot.replay_runs_turn, appends a NEW
    # completed instance for the thread's session (turn actually ran)
    bot.replay_calls = []
    bot.replay_runs_turn = True

    async def _replay(tid, prompt, source="replay", **kw):
        bot.replay_calls.append((tid, prompt, source))
        store.events.append(f"replay:{tid}")
        if bot.replay_runs_turn:
            store.instances.insert(0, SimpleNamespace(
                id=f"verify-{tid}", session_id=f"sess-{tid}",
                status=InstanceStatus.COMPLETED, needs_input=False,
            ))
        return True

    bot._replay_to_thread = _replay
    return bot


def make_target(tid, repo):
    return fleet.ShipTarget(
        thread_id=tid, session_id=f"sess-{tid}",
        inst=SimpleNamespace(id=f"ship-{tid}", branch=f"branch-{tid}"),
        repo_name=repo, title=f"Thread {tid}",
    )


def patch_collect(targets):
    async def _collect(bot):
        return list(targets)
    fleet.collect_ship_targets = _collect


async def scenario_happy_command_deploy():
    print("scenario 1: happy path, command deploy")
    bot = make_bot()
    bot._store.deploy_configs["web"] = {
        "method": "command", "approved": True, "command": "echo deploy",
    }
    # shipped instance is newest pre-replay (realistic baseline)
    for tid in ("101", "102"):
        bot._store.instances.append(SimpleNamespace(
            id=f"ship-{tid}", session_id=f"sess-{tid}",
            status=InstanceStatus.COMPLETED, needs_input=False,
        ))
    targets = [make_target("101", "web"), make_target("102", "web")]
    patch_collect(targets)

    merged: list[str] = []

    async def fake_finalize(ctx, inst, *, close_silent=False, skip_close=False):
        merged.append(inst.id)
        ctx.store.events.append(f"merge:{inst.id}")
        assert skip_close, "fleet must merge with skip_close=True"
        return True

    deploys: list[str] = []

    async def fake_deploy(bot_, repo, cfg, **kw):
        deploys.append(repo)
        bot_._store.events.append(f"deploy:{repo}")
        return True, "ok", ""

    workflows._finalize_merge = fake_finalize
    interactions.execute_deploy = fake_deploy

    await fleet.run_fleet_ship(bot, "origin-1", ["101", "102"])
    await asyncio.sleep(0.05)  # let verify tasks finish

    ev = bot._store.events
    _check(merged == ["ship-101", "ship-102"], "both targets merged in order")
    _check(deploys == ["web"], "repo deployed exactly once")
    persist_i = ev.index("persist_state")
    deploy_i = ev.index("deploy:web")
    _check(persist_i < deploy_i, "verify set persisted BEFORE deploy")
    _check(len(bot.replay_calls) == 2, "verify replay fired for both threads")
    _check("deploy command" in bot.replay_calls[0][1],
           "verify prompt says command deploy ran")
    _check(bot.replay_calls[0][2] == "fleet_verify", "replay tagged fleet_verify")
    _check(sorted(bot.messenger.closed) == ["101", "102"],
           "both threads closed after clean verify turns")
    _check(fleet._get_pending(bot._store) == {}, "pending set drained")


async def scenario_merge_failure_gates_deploy():
    print("scenario 2: merge failure gates the repo")
    bot = make_bot()
    bot._store.deploy_configs["web"] = {
        "method": "command", "approved": True, "command": "echo deploy",
    }
    targets = [make_target("201", "web"), make_target("202", "web")]
    patch_collect(targets)

    async def fake_finalize(ctx, inst, **kw):
        return inst.id != "ship-202"  # second merge fails

    deploys: list[str] = []

    async def fake_deploy(bot_, repo, cfg, **kw):
        deploys.append(repo)
        return True, "ok", ""

    workflows._finalize_merge = fake_finalize
    interactions.execute_deploy = fake_deploy

    await fleet.run_fleet_ship(bot, "origin-2", ["201", "202"])
    await asyncio.sleep(0.05)

    _check(deploys == [], "deploy skipped for repo with a failed merge")
    _check(bot.replay_calls == [], "no verify replay for a gated repo")
    _check("ship-202" in bot._store.pending_merges,
           "failed merge recorded in pending_merges")
    banner_posts = [m for m in bot.messenger.sent if m[0] == "202"]
    _check(len(banner_posts) == 1, "merge-failed banner posted in the thread")


async def scenario_deploy_failure_skips_verify():
    print("scenario 3: deploy failure skips verify")
    bot = make_bot()
    bot._store.deploy_configs["web"] = {
        "method": "command", "approved": True, "command": "echo deploy",
    }
    targets = [make_target("301", "web")]
    patch_collect(targets)

    async def fake_finalize(ctx, inst, **kw):
        return True

    async def fake_deploy(bot_, repo, cfg, **kw):
        return False, "boom", "exit 1"

    workflows._finalize_merge = fake_finalize
    interactions.execute_deploy = fake_deploy

    await fleet.run_fleet_ship(bot, "origin-3", ["301"])
    await asyncio.sleep(0.05)

    _check(bot.replay_calls == [], "no verify replay after failed deploy")
    _check(fleet._get_pending(bot._store) == {},
           "pending set popped on deploy failure (won't drain at next boot)")
    fail_msgs = [t for _, t in bot.messenger.sent if "deploy failed" in t]
    _check(len(fail_msgs) == 1, "origin told the deploy failed")


async def scenario_self_deploy_boot_drain():
    print("scenario 4: self deploy persists set; boot drain fires it")
    bot = make_bot()
    bot._store.deploy_configs["bot"] = {"method": "self", "approved": True}
    targets = [make_target("401", "bot")]
    patch_collect(targets)

    async def fake_finalize(ctx, inst, **kw):
        return True

    workflows._finalize_merge = fake_finalize

    await fleet.run_fleet_ship(bot, "origin-4", ["401"])
    await asyncio.sleep(0.05)

    _check(len(bot._runner.reboots) == 1, "reboot requested for self repo")
    _check(bot.replay_calls == [], "verify NOT fired inline for self deploy")
    pending = fleet._get_pending(bot._store)
    _check("bot" in pending and len(pending["bot"]["entries"]) == 1,
           "verify set persisted across the (simulated) reboot")

    # --- simulated next boot: app.py calls drain_pending_verify(bot) ---
    n = await fleet.drain_pending_verify(bot)
    await asyncio.sleep(0.05)
    _check(n == 1, "boot drain dispatched 1 verify prompt")
    _check(len(bot.replay_calls) == 1, "verify replay fired after boot")
    _check("rebooted" in bot.replay_calls[0][1],
           "verify prompt says the bot rebooted")
    _check(fleet._get_pending(bot._store) == {}, "pending set empty after drain")


async def scenario_noop_replay_not_closed():
    print("scenario 5: replay that runs no turn does NOT close the thread")
    bot = make_bot()
    bot.replay_runs_turn = False  # dispatched, but no new instance appears
    bot._store.deploy_configs["web"] = {
        "method": "command", "approved": True, "command": "echo deploy",
    }
    # baseline: shipped instance is the newest COMPLETED one
    bot._store.instances.append(SimpleNamespace(
        id="ship-501", session_id="sess-501",
        status=InstanceStatus.COMPLETED, needs_input=False,
    ))
    targets = [make_target("501", "web")]
    patch_collect(targets)

    async def fake_finalize(ctx, inst, **kw):
        return True

    async def fake_deploy(bot_, repo, cfg, **kw):
        return True, "ok", ""

    workflows._finalize_merge = fake_finalize
    interactions.execute_deploy = fake_deploy

    await fleet.run_fleet_ship(bot, "origin-5", ["501"])
    await asyncio.sleep(0.05)

    _check(len(bot.replay_calls) == 1, "replay dispatched")
    _check(bot.messenger.closed == [], "thread NOT closed (no new turn ran)")
    attention = [t for _, t in bot.messenger.sent if "needs attention" in t]
    _check(len(attention) == 1, "origin flagged the thread for attention")


async def main() -> int:
    await scenario_happy_command_deploy()
    await scenario_merge_failure_gates_deploy()
    await scenario_deploy_failure_skips_verify()
    await scenario_self_deploy_boot_drain()
    await scenario_noop_replay_not_closed()

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("All fleet pipeline simulation scenarios passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
