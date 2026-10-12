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


def main() -> int:
    test_deploy_registration()
    test_auto_update_dedup()
    test_announce_without_note()
    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
