#!/usr/bin/env python3
"""Diagnostics for cross-machine path portability and session recovery.

Two modes:

    python scripts/verify_paths.py            # run the checks, exit non-zero on failure
    python scripts/verify_paths.py --show     # print the LIVE map, change nothing

Every check builds its own sandbox under a temp dir and never reads or writes
the real root map, the real state file, or the real session history.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

# Keep importing bot.config from seeding a root group off whatever home this
# happens to run under. Every check installs its own map explicitly.
os.environ.setdefault("BOT_PATHS_DISABLED", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import paths  # noqa: E402

_failures: list[str] = []
_checks = 0


def check(label: str, got, want) -> None:
    global _checks
    _checks += 1
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")
        _failures.append(label)


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


# --------------------------------------------------------------------------
# 1. Translation both directions
# --------------------------------------------------------------------------
def test_translation() -> None:
    print("\n[1] Translating a stored path into the local spelling")
    win = "C:/Users/Quincy"
    lin = "/run/media/quincy/SYSTEM/Users/Quincy"

    # Pretend we booted Linux: the Linux spelling is the one that resolves.
    paths.reset_for_test({"g1": [win, lin]}, {"g1": lin})
    check(
        "windows-stored repo path reads as linux",
        paths.translate("C:/Users/Quincy/Desktop/Programming/X"),
        "/run/media/quincy/SYSTEM/Users/Quincy/Desktop/Programming/X",
    )
    check(
        "backslashes are handled",
        paths.translate(r"C:\Users\Quincy\.claude"),
        "/run/media/quincy/SYSTEM/Users/Quincy/.claude",
    )
    check(
        "drive-letter case does not defeat the match",
        paths.translate("c:/users/quincy/.claude-klerk"),
        "/run/media/quincy/SYSTEM/Users/Quincy/.claude-klerk",
    )
    check(
        "a path already local is unchanged",
        paths.translate(lin + "/Desktop"),
        lin + "/Desktop",
    )
    check(
        "an unknown path passes through untouched",
        paths.translate("/home/quincy/Programming/The-Citadel"),
        "/home/quincy/Programming/The-Citadel",
    )
    check("None survives", paths.translate(None), None)

    # Now pretend we booted Windows off the same shared config.
    paths.reset_for_test({"g1": [win, lin]}, {"g1": win})
    check(
        "linux-stored repo path reads as windows",
        paths.translate(lin + "/Desktop/Programming/X"),
        r"C:\Users\Quincy\Desktop\Programming\X",
    )
    check(
        "windows output uses native separators",
        paths.translate(lin + "/.claude"),
        r"C:\Users\Quincy\.claude",
    )


# --------------------------------------------------------------------------
# 2. Alias generation (what makes history findable across machines)
# --------------------------------------------------------------------------
def test_aliases() -> None:
    print("\n[2] Every known spelling of one directory")
    win = "C:/Users/Quincy"
    lin = "/run/media/quincy/SYSTEM/Users/Quincy"
    usb = "/run/media/liveuser/SYSTEM/Users/Quincy"
    paths.reset_for_test({"g1": [win, lin, usb]}, {"g1": lin})

    got = paths.aliases(lin + "/Desktop/Programming/The-Citadel")
    check("local spelling comes first", got[0], lin + "/Desktop/Programming/The-Citadel")
    check("all three spellings are offered", len(got), 3)
    check_true(
        "the windows spelling is among them",
        any(a.lower().startswith("c:") for a in got),
    )
    check(
        "an unmapped path yields just itself",
        paths.aliases("/tmp/whatever"),
        ["/tmp/whatever"],
    )
    check("None yields nothing", paths.aliases(None), [])


def test_is_portable() -> None:
    print("\n[3] Spotting a repo that exists on one machine only")
    paths.reset_for_test(
        {"g1": ["C:/Users/Quincy", "/run/media/quincy/SYSTEM/Users/Quincy"]},
        {"g1": "/run/media/quincy/SYSTEM/Users/Quincy"},
    )
    check_true(
        "a repo on the shared drive is portable",
        paths.is_portable(
            "/run/media/quincy/SYSTEM/Users/Quincy/Desktop/Programming/bot"
        ),
    )
    check(
        "a repo on the linux-only partition is not",
        paths.is_portable("/home/quincy/Programming/The-Citadel"),
        False,
    )


# --------------------------------------------------------------------------
# 4. The map assembles itself from the marker file
# --------------------------------------------------------------------------
def test_self_assembly() -> None:
    print("\n[4] Two machines, one shared volume, no manual configuration")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        os.environ.pop("BOT_PATHS_DISABLED", None)
        try:
            # One physical volume, reachable under two different names. Two
            # real directories here stand in for the two mount points; the
            # marker file is what tells the bot they are the same place.
            as_linux = tmp / "run" / "media" / "q" / "SYSTEM" / "Users" / "Quincy"
            as_windows = tmp / "winmount" / "Users" / "Quincy"
            for root in (as_linux, as_windows):
                (root / ".claude").mkdir(parents=True)
            shared_id = uuid.uuid4().hex
            for root in (as_linux, as_windows):
                (root / paths.MARKER_NAME).write_text(shared_id + "\n")

            data = tmp / "data"

            # Boot 1 — the "Linux" side.
            paths.init(data_dir=data, account_hints=[str(as_linux / ".claude")])
            snap1 = paths.snapshot()
            check("boot 1 learns one group", len(snap1["groups"]), 1)
            check(
                "boot 1 records its own spelling",
                snap1["local"].get(shared_id),
                str(as_linux).replace("\\", "/"),
            )

            # Boot 2 — the "Windows" side, same shared roots.json, different
            # account hint. It must JOIN the existing group, not start a new one.
            paths.init(data_dir=data, account_hints=[str(as_windows / ".claude")])
            snap2 = paths.snapshot()
            check("boot 2 does not create a second group", len(snap2["groups"]), 1)
            check(
                "boot 2 adds its spelling to the same group",
                len(snap2["groups"][shared_id]),
                2,
            )
            check(
                "boot 2 resolves to its own spelling",
                snap2["local"].get(shared_id),
                str(as_windows).replace("\\", "/"),
            )
            # And now a path stored by boot 1 reads correctly on boot 2.
            check(
                "a path stored by the other machine now translates",
                paths.translate(str(as_linux / "Desktop" / "P" / "repo")),
                str(as_windows / "Desktop" / "P" / "repo").replace("\\", "/"),
            )

            # A deleted marker must rejoin its group rather than fork a new one.
            (as_windows / paths.MARKER_NAME).unlink()
            paths.init(data_dir=data, account_hints=[str(as_windows / ".claude")])
            check(
                "a lost marker rejoins its group",
                len(paths.snapshot()["groups"]),
                1,
            )

            saved = json.loads((data / paths.ROOTS_FILENAME).read_text())
            check_true("roots.json is written", "groups" in saved)
        finally:
            os.environ["BOT_PATHS_DISABLED"] = "1"


def test_no_map_is_passthrough() -> None:
    print("\n[5] With no map at all, nothing is rewritten")
    paths.reset_for_test({}, {})
    for p in ("C:/Users/Quincy/x", "/home/q/y", r"D:\z"):
        check(f"passthrough {p}", paths.translate(p), p)
    check("nothing is portable", paths.is_portable("/anything"), False)


# --------------------------------------------------------------------------
# 6. Finding conversation history filed under another machine's name
# --------------------------------------------------------------------------
def test_history_lookup() -> None:
    print("\n[6] Locating a conversation filed under another spelling")
    from bot import config
    from bot.claude.runner import ClaudeRunner
    from bot.claude.types import Instance, InstanceStatus, InstanceType

    def _mk(iid: str, session: str) -> Instance:
        return Instance(
            id=iid, name=None, instance_type=InstanceType.QUERY, prompt="x",
            repo_name="The-Citadel", repo_path=repo_linux,
            status=InstanceStatus.RUNNING, session_id=session,
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        acct = tmp / "acct"
        (acct / "projects").mkdir(parents=True)

        win_root = "C:/Users/Quincy"
        lin_root = str(tmp / "SYSTEM" / "Users" / "Quincy").replace("\\", "/")
        paths.reset_for_test({"g1": [win_root, lin_root]}, {"g1": lin_root})

        repo_linux = f"{lin_root}/Desktop/Programming/The-Citadel"
        repo_windows = f"{win_root}/Desktop/Programming/The-Citadel"
        session_id = str(uuid.uuid4())

        # History written on the OTHER machine: filed under the Windows name.
        from bot.engine.session_fork import encode_project_path

        other = acct / "projects" / encode_project_path(repo_windows)
        other.mkdir(parents=True)
        (other / f"{session_id}.jsonl").write_text(
            json.dumps({"type": "user", "timestamp": "2026-08-06T10:00:00.000Z"}) + "\n"
        )

        runner = ClaudeRunner()
        inst = _mk("q-test", session_id)
        saved_accounts = list(config.CLAUDE_ACCOUNTS)
        config.CLAUDE_ACCOUNTS = [str(acct)]
        try:
            ok = asyncio.run(
                runner._hydrate_session_for_account(
                    str(acct), repo_linux, session_id, inst,
                )
            )
            target = (
                acct / "projects" / encode_project_path(repo_linux)
                / f"{session_id}.jsonl"
            )
            check_true("cross-machine history is located", ok)
            check_true("it is copied where the CLI will look", target.exists())

            # Now a spelling the map has never heard of — the live-USB mount,
            # or a repo that moved. Only the id-based scan can find this.
            session2 = str(uuid.uuid4())
            stray = acct / "projects" / "-run-media-liveuser-SYSTEM-nope-The-Citadel"
            stray.mkdir(parents=True)
            (stray / f"{session2}.jsonl").write_text(
                json.dumps(
                    {"type": "user", "timestamp": "2026-08-06T11:00:00.000Z"}
                ) + "\n"
            )
            inst2 = _mk("q-test2", session2)
            ok2 = asyncio.run(
                runner._hydrate_session_for_account(
                    str(acct), repo_linux, session2, inst2,
                )
            )
            target2 = (
                acct / "projects" / encode_project_path(repo_linux)
                / f"{session2}.jsonl"
            )
            check_true("history under an unmapped spelling is found by id", ok2)
            check_true("and copied into place", target2.exists())

            # A conversation that genuinely does not exist must still report
            # missing — the scan must not invent a hit.
            inst3 = _mk("q-test3", str(uuid.uuid4()))
            ok3 = asyncio.run(
                runner._hydrate_session_for_account(
                    str(acct), repo_linux, inst3.session_id, inst3,
                )
            )
            check("a genuinely absent conversation stays absent", ok3, False)
        finally:
            config.CLAUDE_ACCOUNTS = saved_accounts


# --------------------------------------------------------------------------
# 7. The refusal message has to be true
# --------------------------------------------------------------------------
def test_refusal_honesty() -> None:
    print("\n[7] 'Every account is signed out' must mean every account")
    from bot import config
    from bot.claude.runner import ClaudeRunner

    runner = ClaudeRunner()
    saved = list(config.CLAUDE_ACCOUNTS)
    config.CLAUDE_ACCOUNTS = ["/acct/main", "/acct/backup"]
    try:
        runner._account_cooldowns = {}
        _, reason = runner._refusal_retry_plan({"/acct/backup"})
        check("one of two signed out is NOT a sweep", reason, "no_account_free")

        _, reason = runner._refusal_retry_plan({"/acct/main", "/acct/backup"})
        check("both signed out IS a sweep", reason, "accounts_logged_out")

        _, reason = runner._refusal_retry_plan(set())
        check("none signed out", reason, "no_account_free")

        # A live cooldown is a real wall-clock moment and keeps priority.
        from datetime import datetime, timedelta, timezone

        runner._account_cooldowns = {
            "/acct/main": datetime.now(timezone.utc) + timedelta(hours=2)
        }
        _, reason = runner._refusal_retry_plan({"/acct/backup"})
        check("a live cooldown still wins", reason, "backup_logged_out")
    finally:
        config.CLAUDE_ACCOUNTS = saved


# --------------------------------------------------------------------------
# 8. Abandoning a conversation must free the accounts up again
# --------------------------------------------------------------------------
def test_state_localisation() -> None:
    print("\n[8] Stored state is localised on load")
    from bot.store.state import _localise_paths

    win = "C:/Users/Quincy"
    lin = "/run/media/quincy/SYSTEM/Users/Quincy"
    paths.reset_for_test({"g1": [win, lin]}, {"g1": lin})

    data = {
        "repos": {"bot": "C:/Users/Quincy/Desktop/Programming/bot",
                  "stray": "/home/quincy/Programming/The-Citadel"},
        "instances": [{
            "id": "q-1",
            "repo_path": "C:/Users/Quincy/Desktop/Programming/bot",
            "worktree_path": "C:/Users/Quincy/Desktop/Programming/bot/.worktrees/t-1",
            "session_account": "C:/Users/Quincy/.claude",
            "prompt": "left alone: C:/Users/Quincy/notes.txt",
        }],
        "account_cooldowns": {"C:/Users/Quincy/.claude-klerk": "2026-08-06T00:00:00Z"},
    }
    _localise_paths(data)
    check("repo path localised", data["repos"]["bot"],
          lin + "/Desktop/Programming/bot")
    check("unknown repo untouched", data["repos"]["stray"],
          "/home/quincy/Programming/The-Citadel")
    check("instance repo localised", data["instances"][0]["repo_path"],
          lin + "/Desktop/Programming/bot")
    check("worktree localised", data["instances"][0]["worktree_path"],
          lin + "/Desktop/Programming/bot/.worktrees/t-1")
    check("account pin localised", data["instances"][0]["session_account"],
          lin + "/.claude")
    check("prompt text NOT rewritten", data["instances"][0]["prompt"],
          "left alone: C:/Users/Quincy/notes.txt")
    check("cooldown key localised",
          list(data["account_cooldowns"]), [lin + "/.claude-klerk"])


def show_live_map() -> None:
    """Print the real map without modifying anything."""
    os.environ.pop("BOT_PATHS_DISABLED", None)
    root = Path(__file__).resolve().parent.parent
    file = root / "data" / paths.ROOTS_FILENAME
    print(f"roots file: {file}")
    if not file.exists():
        print("  (not created yet — it is written on first boot)")
    else:
        print(file.read_text())
    from bot import config  # noqa: F401  — triggers a real init

    print(paths.describe())
    print(json.dumps(paths.snapshot(), indent=2))
    print("\nconfigured accounts (after translation):")
    for a in config.CLAUDE_ACCOUNTS:
        print(f"  {a}  {'[exists]' if Path(a).is_dir() else '[MISSING]'}")


def main() -> int:
    if "--show" in sys.argv:
        show_live_map()
        return 0

    test_translation()
    test_aliases()
    test_is_portable()
    test_self_assembly()
    test_no_map_is_passthrough()
    test_history_lookup()
    test_refusal_honesty()
    test_state_localisation()

    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    if _failures:
        print("FAILED:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
