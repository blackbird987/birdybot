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


def test_marker_walk_detection() -> None:
    print("\n[4b] A mount point no configuration predicted still finds itself")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        os.environ.pop("BOT_PATHS_DISABLED", None)
        try:
            # The volume came up somewhere nobody could have written down: a
            # live session, a relabelled drive. The configured account paths
            # resolve to nothing here and $HOME is a throwaway profile — but
            # the marker is sitting on the volume, above the code.
            root = tmp / "media" / "liveuser" / "8A31-2F0C" / "Users" / "Quincy"
            repo = root / "Desktop" / "Programming" / "bot"
            repo.mkdir(parents=True)
            shared_id = uuid.uuid4().hex
            (root / paths.MARKER_NAME).write_text(shared_id + "\n")

            throwaway = tmp / "home" / "liveuser"
            throwaway.mkdir(parents=True)

            found = paths.detect_local_root(
                ["C:/Users/Quincy/.claude"],  # does not exist here
                home=throwaway,
                here=repo,
            )
            check("marker above the code identifies the root", found, root)

            # And with no marker anywhere, it still falls back rather than
            # inventing something.
            (root / paths.MARKER_NAME).unlink()
            check(
                "no marker falls back to home",
                paths.detect_local_root([], home=throwaway, here=repo),
                throwaway,
            )

            # Restore it and check init() wires the whole thing up, so a path
            # written under the Windows name resolves at this mount point.
            (root / paths.MARKER_NAME).write_text(shared_id + "\n")
            data = tmp / "data"
            (data).mkdir(parents=True, exist_ok=True)
            (data / paths.ROOTS_FILENAME).write_text(json.dumps(
                {"version": 1, "groups": {shared_id: ["C:/Users/Quincy"]}}
            ))
            paths.init(data_dir=data, account_hints=[], here=repo)
            check(
                "a Windows-written path resolves at the live mount",
                paths.translate("C:/Users/Quincy/Desktop/Programming/bot"),
                str(repo).replace("\\", "/"),
            )
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
        # In-place, not a rebind: a rebind would be invisible to any module
        # that from-imports the name, and the sibling harness already does this.
        saved_accounts = list(config.CLAUDE_ACCOUNTS)
        config.CLAUDE_ACCOUNTS[:] = [str(acct)]
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

            # The id goes into a glob pattern, so a wildcard in it would match
            # somebody ELSE's conversation and hydrate that instead. A real
            # jsonl is sitting in `stray` for it to grab if the guard is gone.
            inst4 = _mk("q-test4", "*")
            ok4 = asyncio.run(
                runner._hydrate_session_for_account(
                    str(acct), repo_linux, "*", inst4,
                )
            )
            check("a wildcard id matches nothing", ok4, False)
        finally:
            config.CLAUDE_ACCOUNTS[:] = saved_accounts


# --------------------------------------------------------------------------
# 7. The refusal message has to be true
# --------------------------------------------------------------------------
def test_refusal_honesty() -> None:
    print("\n[7] 'Every account is signed out' must mean every account")
    from bot import config
    from bot.claude.runner import ClaudeRunner

    runner = ClaudeRunner()
    saved = list(config.CLAUDE_ACCOUNTS)
    config.CLAUDE_ACCOUNTS[:] = ["/acct/main", "/acct/backup"]
    try:
        runner._account_cooldowns = {}
        _, reason = runner._refusal_retry_plan({"/acct/backup"})
        check(
            "one of two signed out is NOT a sweep",
            reason, "some_accounts_logged_out",
        )

        _, reason = runner._refusal_retry_plan({"/acct/main", "/acct/backup"})
        check("both signed out IS a sweep", reason, "accounts_logged_out")

        _, reason = runner._refusal_retry_plan(set())
        check("none signed out", reason, "no_account_free")

        # Every reason the refusal can produce must have copy written for it,
        # and the ones a login would fix must offer the auth panel — otherwise
        # narrowing the wording quietly removes the button that fixes it.
        from bot.engine.lifecycle import _AUTH_RETRY_REASONS, _RETRY_HEADLINES

        for r in ("accounts_logged_out", "some_accounts_logged_out",
                  "no_account_free", "backup_logged_out"):
            check_true(f"copy exists for {r}", r in _RETRY_HEADLINES)
        check_true(
            "partial logout still offers the auth panel",
            "some_accounts_logged_out" in _AUTH_RETRY_REASONS,
        )
        check(
            "'nothing free' does not claim a login problem",
            "no_account_free" in _AUTH_RETRY_REASONS, False,
        )

        # A live cooldown is a real wall-clock moment and keeps priority.
        from datetime import datetime, timedelta, timezone

        runner._account_cooldowns = {
            "/acct/main": datetime.now(timezone.utc) + timedelta(hours=2)
        }
        _, reason = runner._refusal_retry_plan({"/acct/backup"})
        check("a live cooldown still wins", reason, "backup_logged_out")
    finally:
        config.CLAUDE_ACCOUNTS[:] = saved


# --------------------------------------------------------------------------
# 8. Stored paths are rewritten on the way in
#    (abandoning a conversation and freeing the accounts back up is the other
#    half of this branch, and lives in scripts/test_dead_session_recovery.py —
#    it needs the whole cascade, not just the map)
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
            # Opened by /log and /diff, and when a forum thread rebuilds its
            # history — the failure is silent, so it is easy to miss.
            "result_file": "C:/Users/Quincy/Desktop/Programming/bot/data/results/q-1.md",
            "diff_file": "C:/Users/Quincy/Desktop/Programming/bot/data/results/q-1.diff",
            "prompt": "left alone: C:/Users/Quincy/notes.txt",
            "bash_commands": ["ls C:/Users/Quincy/x"],
            "path_poisoning": ["C:/Users/Quincy/Desktop/Programming/bot/CHANGELOG.md"],
        }],
        "schedules": [{"id": "s1", "repo_path": "C:/Users/Quincy/Desktop/Programming/bot"}],
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
    check("result file localised", data["instances"][0]["result_file"],
          lin + "/Desktop/Programming/bot/data/results/q-1.md")
    check("diff file localised", data["instances"][0]["diff_file"],
          lin + "/Desktop/Programming/bot/data/results/q-1.diff")
    check("schedule repo localised", data["schedules"][0]["repo_path"],
          lin + "/Desktop/Programming/bot")
    check("prompt text NOT rewritten", data["instances"][0]["prompt"],
          "left alone: C:/Users/Quincy/notes.txt")
    check("bash history NOT rewritten", data["instances"][0]["bash_commands"],
          ["ls C:/Users/Quincy/x"])
    check("poisoning record NOT rewritten", data["instances"][0]["path_poisoning"],
          ["C:/Users/Quincy/Desktop/Programming/bot/CHANGELOG.md"])
    check("cooldown key localised",
          list(data["account_cooldowns"]), [lin + "/.claude-klerk"])


# --------------------------------------------------------------------------
# 9. Nothing about translation can cost us the state file
# --------------------------------------------------------------------------
def test_translation_cannot_lose_state() -> None:
    """A path rewrite that blows up must degrade, not wipe.

    The state loader's own handler recovers by "starting fresh", and for
    data/state.json that means every repo, instance and forum-thread mapping
    is gone and the next auto-save writes the empty version over the real one.
    Translation is new code running on every boot, so it gets its own net.
    """
    print("\n[9] A failing path rewrite degrades instead of wiping the state")
    from bot.claude.types import Instance, InstanceStatus, InstanceType
    from bot.store import state as state_mod

    tmp = Path(tempfile.mkdtemp(prefix="bot-loseless-"))
    try:
        # Round-tripped through the real dataclass rather than hand-written, so
        # this fixture cannot rot into an unloadable record when the schema
        # gains a field and quietly turn the check into a tautology.
        inst = Instance(
            id="q-1", name="q-1", instance_type=InstanceType.QUERY,
            prompt="x", repo_name="bot", repo_path="/somewhere/bot",
            status=InstanceStatus.COMPLETED,
        )
        state_file = tmp / "state.json"
        state_file.write_text(json.dumps({
            "repos": {"bot": "/somewhere/bot"},
            "instances": [inst.to_dict()],
        }), encoding="utf-8")

        original = state_mod._localise_paths

        def _explode(_data):
            raise RuntimeError("simulated translation failure")

        state_mod._localise_paths = _explode
        try:
            store = state_mod.StateStore(state_file, tmp / "results")
        finally:
            state_mod._localise_paths = original

        check("repos survived a translation crash", store.list_repos(),
              {"bot": "/somewhere/bot"})
        check("instances survived a translation crash",
              len(store.list_instances(all_=True)), 1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 10. Case folding may never change a path's length
# --------------------------------------------------------------------------
def test_fold_is_length_preserving() -> None:
    """The remainder is sliced off the raw path at an offset measured on the
    folded one, so a fold that changes length silently truncates.  Turkish
    dotted capital I is the classic offender: str.lower() turns it into two
    characters.  Folding ASCII only keeps the two lengths locked together.
    """
    print("\n[10] Case folding keeps the path length intact")
    tricky = "C:/Users/İstanbul/Straße/ΣΩ"
    check("fold preserves length", len(paths._cmp(tricky)), len(tricky))
    check("plain lower() would NOT have", len(tricky.lower()) == len(tricky), False)

    # And end to end: a root whose name carries one of those characters must
    # translate to a whole path, not a shortened one.
    win = "C:/Users/İstanbul"
    lin = "/mnt/win/Users/İstanbul"
    paths.reset_for_test({"g1": [win, lin]}, {"g1": lin})
    check("no truncation under a non-ASCII root",
          paths.translate(win + "/Desktop/Programming/bot"),
          lin + "/Desktop/Programming/bot")

    # Drive-letter case still folds, which is the case that actually occurs.
    paths.reset_for_test({"g1": ["C:/Users/Quincy", "/mnt/q"]}, {"g1": "/mnt/q"})
    check("ASCII case still folds", paths.translate("c:/USERS/quincy/x"), "/mnt/q/x")


# --------------------------------------------------------------------------
# 11. A root of "/" must never enter the map
# --------------------------------------------------------------------------
def test_filesystem_root_is_refused() -> None:
    """A root of "/" would mark every path on the machine as portable.

    "/" normalises to the empty string, which prefix-matches every absolute
    path.  Translation survives that (the rewrite equals the input), but
    is_portable then answers True for everything — and its only job is to spot
    a repo living outside the shared roots, which is precisely the condition
    that stranded a repo on a Linux-only partition.  One bad root would
    silence that warning for every repo at once.

    Driven through the reachable route: an account directory sitting at the
    top of the filesystem, whose parent is "/".  With the guard in place no
    marker is ever written, so this never touches the real root.
    """
    print("\n[11] The filesystem root is refused as a map root")
    tmp = Path(tempfile.mkdtemp(prefix="bot-rootguard-"))
    try:
        # Also seeded on disk, so both the stored map and live detection are
        # exercised by the same check.
        (tmp / "data").mkdir()
        (tmp / "data" / paths.ROOTS_FILENAME).write_text(json.dumps({
            "groups": {"g1": ["/", "C:/Users/Quincy"]},
        }), encoding="utf-8")

        env = os.environ.pop("BOT_PATHS_DISABLED", None)
        try:
            # /tmp exists on every machine this runs on, so its parent — "/" —
            # is what detection would otherwise settle on.
            paths.init(data_dir=tmp / "data", account_hints=["/tmp"],
                       home=tmp / "nonexistent-home")
        finally:
            if env is not None:
                os.environ["BOT_PATHS_DISABLED"] = env

        snap = paths.snapshot()
        check("no group records the filesystem root",
              [m for ms in snap["groups"].values() for m in ms if m == ""], [])
        check("'/' did not become the local root",
              [v for v in snap["local"].values() if v == ""], [])
        check("an arbitrary path is not suddenly portable",
              paths.is_portable("/etc/passwd"), False)
        check("nor is one on the other partition",
              paths.is_portable("/home/quincy/Programming/The-Citadel"), False)
        check("translation is still an identity for them",
              paths.translate("/etc/passwd"), "/etc/passwd")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 12. The session picker sees the other machine's conversations too
# --------------------------------------------------------------------------
def test_session_scan_spellings() -> None:
    print("\n[12] /session and the picker look under every spelling")
    from bot import config
    from bot.engine import sessions as sessions_mod
    from bot.engine.session_fork import encoded_spellings

    win = "C:/Users/Quincy"
    lin = "/run/media/quincy/SYSTEM/Users/Quincy"
    repo = lin + "/Desktop/Programming/bot"

    paths.reset_for_test({"g1": [win, lin]}, {"g1": lin})
    got = encoded_spellings(repo)
    check(
        "the local spelling is first (it is the one we WRITE under)",
        got[0],
        "-run-media-quincy-SYSTEM-Users-Quincy-Desktop-Programming-bot",
    )
    check_true(
        "the windows-written project dir is searched too",
        "C--Users-Quincy-Desktop-Programming-bot" in got,
    )
    check("no duplicates", len(got), len(set(got)))

    # With no map this must collapse to exactly the old single-spelling
    # behaviour — that is what makes it safe on a single-machine install.
    paths.reset_for_test({}, {})
    check(
        "no map means one spelling, unchanged from before",
        encoded_spellings(repo),
        ["-run-media-quincy-SYSTEM-Users-Quincy-Desktop-Programming-bot"],
    )

    # The picker's directory -> repo-name lookup must cover the aliases as
    # well, or a conversation written elsewhere shows a mangled name.
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        paths.reset_for_test({"g1": [win, lin]}, {"g1": lin})
        saved_dirs = config.claude_projects_dirs
        (tmp / "projects").mkdir()
        config.claude_projects_dirs = lambda: [tmp / "projects"]  # type: ignore
        try:
            other = tmp / "projects" / "C--Users-Quincy-Desktop-Programming-bot"
            other.mkdir()
            sid = "11111111-2222-3333-4444-555555555555"
            (other / f"{sid}.jsonl").write_text("{}\n")
            found = sessions_mod.find_latest_session_for_repo(repo)
            check(
                "history written by the other machine is found",
                (found or {}).get("id"),
                sid,
            )
        finally:
            config.claude_projects_dirs = saved_dirs  # type: ignore


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
    test_marker_walk_detection()
    test_no_map_is_passthrough()
    test_history_lookup()
    test_refusal_honesty()
    test_state_localisation()
    test_translation_cannot_lose_state()
    test_fold_is_length_preserving()
    test_filesystem_root_is_refused()
    test_session_scan_spellings()

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
