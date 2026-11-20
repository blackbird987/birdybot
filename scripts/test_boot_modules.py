#!/usr/bin/env python3
"""Harness for the pieces that moved out of bot/app.py's run().

- ``deploy.auto_register_deploy_configs``: a self-managed repo gets an
  approved ``self`` config, a repo with .claude/deploy.json gets an unapproved
  command config that keeps the file's ``timeout`` (the boot copy of this loop
  used to drop it, while the post-merge rescan kept it), and a repo that
  already has a config is left alone.
- ``auto_update.auto_update_loop``: a failure that repeats on every poll is
  announced once, not once per poll.
- ``boot_recovery.announce_reboot``: a boot with no reboot note says nothing.
- ``boot_recovery.resume_interrupted_chains``: chains in different threads
  resume side by side, so one long chain does not hold up the rest.

Run: python scripts/test_boot_modules.py
Exit 0 = all pass.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import auto_update, boot_recovery, config  # noqa: E402
from bot.engine import deploy  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


class FakeStore:
    def __init__(self, repos, states=None, configs=None):
        self._repos = repos
        self._states = states or {}
        self.configs = dict(configs or {})

    def list_repos(self):
        return dict(self._repos)

    def get_deploy_state(self, name):
        return self._states.get(name)

    def get_deploy_config(self, name):
        return self.configs.get(name)

    def set_deploy_config(self, name, cfg):
        self.configs[name] = cfg


def test_deploy_registration() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in ("filed", "kept", "selfm", "bare"):
            (root / name / ".claude").mkdir(parents=True)
        cfg = {"command": "make deploy", "label": "Ship", "timeout": 900, "auto_fix": True}
        for name in ("filed", "kept"):
            (root / name / ".claude" / "deploy.json").write_text(json.dumps(cfg), encoding="utf-8")
        manual = {"method": "command", "command": "x", "source": "manual"}
        store = FakeStore(
            {n: str(root / n) for n in ("filed", "kept", "selfm", "bare")},
            states={"selfm": SimpleNamespace(self_managed=True)},
            configs={"kept": manual},
        )
        deploy.auto_register_deploy_configs(store)
        filed = store.configs.get("filed") or {}
        check("a deploy.json repo is registered unapproved from the file",
              filed.get("command") == "make deploy" and filed.get("approved") is False
              and filed.get("source") == "file" and filed.get("auto_fix") is True, str(filed))
        check("the file's timeout survives boot registration", filed.get("timeout") == 900, str(filed))
        check("an existing config is left alone", store.configs["kept"] is manual)
        selfm = store.configs.get("selfm") or {}
        check("a self-managed repo gets an approved self config",
              selfm.get("method") == "self" and selfm.get("approved") is True, str(selfm))
        check("a repo with nothing to register gets nothing", "bare" not in store.configs)


def test_auto_update_dedup() -> None:
    sent: list[str] = []

    class Notifier:
        async def broadcast(self, text, **_):
            sent.append(text)

    class Runner:
        def _get_repo_lock(self, _path):
            return asyncio.Lock()

    calls = {"n": 0}

    async def failing_git(*args, timeout):
        calls["n"] += 1
        return SimpleNamespace(returncode=1, stderr="no network", stdout="")

    async def go() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(auto_update.auto_update_loop(stop, Runner(), Notifier()))
        while calls["n"] < 4:
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 2)

    saved = (auto_update._git, auto_update._detect_update_branch, config.AUTO_UPDATE_INTERVAL_SECS)
    auto_update._git = failing_git
    auto_update._detect_update_branch = lambda: "master"
    config.AUTO_UPDATE_INTERVAL_SECS = 0.01
    try:
        asyncio.run(go())
    finally:
        auto_update._git, auto_update._detect_update_branch, config.AUTO_UPDATE_INTERVAL_SECS = saved
    check("a fetch failing on every poll is announced once",
          calls["n"] >= 4 and len(sent) == 1 and "fetch failed" in sent[0], f"{calls['n']} polls, {sent}")


def test_announce_without_note() -> None:
    sent: list[str] = []

    class Notifier:
        _messengers = [object()]

        async def broadcast(self, text, **_):
            sent.append(text)

    saved = config.REBOOT_MSG_FILE
    with tempfile.TemporaryDirectory() as tmp:
        config.REBOOT_MSG_FILE = Path(tmp) / "reboot_message.json"
        try:
            asyncio.run(boot_recovery.announce_reboot(Notifier(), None))
            check("no reboot note, no announcement", sent == [], str(sent))
            config.REBOOT_MSG_FILE.write_text(json.dumps({"message": "applied a fix"}), encoding="utf-8")
            asyncio.run(boot_recovery.announce_reboot(Notifier(), None))
            check("a reboot note is announced with its reason and then removed",
                  len(sent) == 1 and "applied a fix" in sent[0] and not config.REBOOT_MSG_FILE.exists(),
                  str(sent))
        finally:
            config.REBOOT_MSG_FILE = saved


def test_chains_resume_in_parallel() -> None:
    from unittest import mock

    from bot.claude.types import Instance, InstanceStatus, InstanceType
    from bot.engine import chain_runner
    from bot.store.state import StateStore

    started: list[str] = []
    finished: list[str] = []
    after_turn: list[str] = []

    class FakeMessenger:
        async def send_text(self, *a, **k):
            return "m"

    class FakeBot:
        def __init__(self) -> None:
            self._forums = SimpleNamespace(
                session_to_thread=lambda sid: (f"thread-{sid}", object()),
                attach_session_callbacks=lambda *a, **k: None,
            )

        async def _wait_for_ready(self, _why):
            return True

        def _ctx(self, thread_id, session_id=None, thread_info=None):
            return SimpleNamespace(messenger=FakeMessenger(), channel_id=thread_id)

        async def _after_turn(self, ctx, thread_id, dashboard=True):
            after_turn.append(thread_id)

    async def scenario() -> None:
        b_started = asyncio.Event()

        async def fake_chain(ctx, source_id, msg, steps, session_id, **kw):
            started.append(session_id)
            if session_id == "sess-a":
                # A only finishes once B has started: run one after another,
                # this would wait forever.
                await b_started.wait()
            else:
                b_started.set()
            finished.append(session_id)

        real_sleep = asyncio.sleep

        async def no_sleep(delay, *a, **k):
            await real_sleep(0)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(chain_runner, "_run_autopilot_chain", fake_chain), \
                mock.patch.object(boot_recovery.asyncio, "sleep", no_sleep):
            store = StateStore(Path(tmp) / "state.json", Path(tmp) / "results")
            for sid in ("sess-a", "sess-b"):
                store.set_autopilot_chain(sid, ["build", "verify"])
                store.update_instance(Instance(
                    id=f"q-{sid}", name=None, instance_type=InstanceType.QUERY,
                    prompt="x", repo_name="r", repo_path=tmp,
                    status=InstanceStatus.COMPLETED, session_id=sid,
                ))
            await asyncio.wait_for(
                boot_recovery.resume_interrupted_chains(store, FakeBot(), set()), 5,
            )

    try:
        asyncio.run(scenario())
        timed_out = False
    except asyncio.TimeoutError:
        timed_out = True
    check("a chain blocked on another thread's chain does not hold it up",
          not timed_out and sorted(finished) == ["sess-a", "sess-b"],
          f"timed out={timed_out}, started={started}, finished={finished}")
    check("each resumed chain gets its after-turn",
          sorted(after_turn) == ["thread-sess-a", "thread-sess-b"], str(after_turn))


def main() -> int:
    test_deploy_registration()
    test_auto_update_dedup()
    test_announce_without_note()
    test_chains_resume_in_parallel()
    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
