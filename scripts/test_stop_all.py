#!/usr/bin/env python3
"""Regression test: the Stop all buttons stop runs as kills, not as crashes.

Two buttons stop every running instance at once: the repo Control Room's
Stop all (``interactions._handle_stop_all``) and The Ark's
(``wizard.handle_ark_button`` with ``ark:stop_all``). Both used to loop over
``runner.kill(inst.id)``, and the bare ``kill`` defaults to a NON-intentional
kill. Every run they stopped therefore rendered as a red FAILED card, and a
killed run's "no output, no turns" shape is exactly what the account-failover
branch reads as an account that fell over, so on a two-account setup the work
the user had just cancelled could be restarted on the backup subscription.
See "Interrupting a session" in CLAUDE.md.

Asserted here:

  * both buttons go through ``commands.stop_instances``, which kills through
    ``kill_and_wait(reason="kill")``, never through the bare ``kill``
  * they do not claim the progress card (``owns_card=False``), so lifecycle
    resolves each card to "stopped" itself
  * an instance whose wait force-cleared is flipped to KILLED by the engine
  * an instance the runner no longer knows is not counted as stopped
  * the Control Room button only stops its own repo
"""

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.claude.types import (  # noqa: E402
    Instance, InstanceStatus, InstanceType, KillOutcome,
)
from bot.discord import interactions, wizard  # noqa: E402

_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name}" + (f" ({detail})" if detail else ""))
        _failures.append(name)


class _Runner:
    def __init__(self, outcomes: dict[str, KillOutcome]):
        self.outcomes = outcomes
        self.waits: list[tuple[str, str | None, bool]] = []
        self.bare_kills: list[str] = []

    async def kill(self, instance_id, **kw):
        self.bare_kills.append(instance_id)
        return True

    async def kill_and_wait(self, instance_id, timeout=10.0, *,
                            intentional=True, reason=None, owns_card=False):
        self.waits.append((instance_id, reason, owns_card))
        return self.outcomes.get(instance_id, KillOutcome.FINALIZED)


class _Store:
    def __init__(self, instances):
        self.instances = {i.id: i for i in instances}
        self.updates: list[str] = []

    def list_instances(self, all_=False):
        return list(self.instances.values())

    def update_instance(self, inst, critical=False):
        self.updates.append(inst.id)

    def get_instance(self, inst_id):
        return self.instances.get(inst_id)


class _Followup:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text, **kw):
        self.sent.append(text)


class _Response:
    async def defer(self, **kw):
        pass


class _User:
    id = 1


class _Interaction:
    def __init__(self):
        self.followup = _Followup()
        self.response = _Response()
        self.user = _User()


class _Forums:
    async def refresh_control_room(self, repo_name):
        pass


class _Access:
    is_owner = True


def _inst(iid: str, repo: str, status=InstanceStatus.RUNNING) -> Instance:
    return Instance(
        id=iid, name=None, instance_type=InstanceType.QUERY, prompt="p",
        repo_name=repo, repo_path="/tmp/x", status=status,
    )


def _bot(store, runner):
    class _Bot:
        pass
    b = _Bot()
    b._store = store
    b._runner = runner
    b._forums = _Forums()
    b._lobby_channel_id = 0
    b._dashboard_lock = asyncio.Lock()
    b._dashboard_pending_flag = [False]
    b._is_owner = lambda uid: True

    async def _refresh_dashboard():
        pass
    b._refresh_dashboard = _refresh_dashboard
    return b


async def _control_room_case() -> None:
    a = _inst("q-1", "alpha")
    gone = _inst("q-2", "alpha")
    wedged = _inst("q-3", "alpha")
    other = _inst("q-4", "beta")
    done = _inst("q-5", "alpha", InstanceStatus.COMPLETED)
    runner = _Runner({"q-2": KillOutcome.NOT_RUNNING, "q-3": KillOutcome.FORCE_CLEARED})
    store = _Store([a, gone, wedged, other, done])
    inter = _Interaction()
    await interactions._handle_stop_all(_bot(store, runner), inter, "alpha", _Access())
    await asyncio.sleep(0)

    _check("control room: bare kill is never called", runner.bare_kills == [],
           str(runner.bare_kills))
    ids = sorted(w[0] for w in runner.waits)
    _check("control room: only this repo's running instances are stopped",
           ids == ["q-1", "q-2", "q-3"], str(ids))
    _check("control room: every stop announces a user kill",
           all(w[1] == "kill" for w in runner.waits), str(runner.waits))
    _check("control room: no stop claims the progress card",
           all(w[2] is False for w in runner.waits), str(runner.waits))
    _check("control room: a force-cleared wait is flipped to KILLED by the engine",
           wedged.status == InstanceStatus.KILLED and wedged.finished_at is not None
           and "q-3" in store.updates)
    _check("control room: a finalized instance is left to lifecycle",
           "q-1" not in store.updates)
    _check("control room: the confirmation counts every running instance",
           any("Stopping 3 instances" in t for t in inter.followup.sent),
           str(inter.followup.sent))


async def _ark_case() -> None:
    a = _inst("q-1", "alpha")
    b = _inst("q-2", "beta")
    gone = _inst("q-3", "gamma")
    runner = _Runner({"q-3": KillOutcome.NOT_RUNNING})
    store = _Store([a, b, gone])
    inter = _Interaction()
    bot = _bot(store, runner)

    async def _no_refresh(*a, **k):
        pass
    import bot.discord.dashboard as dashboard
    saved = dashboard.refresh_dashboard
    dashboard.refresh_dashboard = _no_refresh
    try:
        await wizard.handle_ark_button(bot, inter, "ark:stop_all")
        await asyncio.sleep(0)
    finally:
        dashboard.refresh_dashboard = saved

    _check("ark: bare kill is never called", runner.bare_kills == [],
           str(runner.bare_kills))
    _check("ark: every running instance in every repo is stopped",
           sorted(w[0] for w in runner.waits) == ["q-1", "q-2", "q-3"],
           str(runner.waits))
    _check("ark: every stop announces a user kill without claiming the card",
           all(w[1] == "kill" and w[2] is False for w in runner.waits),
           str(runner.waits))
    _check("ark: an instance the runner no longer knows is not counted",
           any("Stopped 2/3 instances" in t for t in inter.followup.sent),
           str(inter.followup.sent))


async def main() -> int:
    await _control_room_case()
    await _ark_case()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll stop-all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
