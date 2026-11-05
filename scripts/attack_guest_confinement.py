"""Real-CLI proof for guest confinement, before and after.

Not a harness (the name keeps it out of scripts/run_harnesses.py). It drives the
real CLI through ClaudeRunner.run against the scripted Messages endpoint from
attack_session_sandbox.py, in a throwaway repo under the home directory with no
sandbox setting. Every secret is a fresh fake canary. It never reads the bot's
`.env`.

    python scripts/attack_guest_confinement.py                 # this code, expects closed
    python scripts/attack_guest_confinement.py --expect open   # an export of the commit
                                                               # before the change

Exit 0 = every expectation held.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import attack_session_sandbox as A

from bot import config
from bot.claude import sandbox
from bot.claude.runner import ClaudeRunner
from bot.claude.types import Instance, InstanceStatus, InstanceType
from bot.discord import titles
from bot.store.state import StateStore

EXPECT = "open" if "--expect" in sys.argv and sys.argv[sys.argv.index("--expect") + 1] == "open" else "closed"
MARK = A.SCRIPT_MARK
OUTSIDE = A.ROOT / "outside"
REPO = A.ROOT / "guestrepo"
SECRET = OUTSIDE / "fake.env"
VICTIM = OUTSIDE / "victim.txt"
PLANTED = OUTSIDE / "planted.txt"


def setup() -> None:
    OUTSIDE.mkdir()
    A.TOKENS["SECRET"] = A.token("FAKE-DISCORD-TOKEN")
    A.TOKENS["DIRNAME"] = A.token("FILENAME")
    SECRET.write_text(f"DISCORD_BOT_TOKEN={A.TOKENS['SECRET']}\n")
    VICTIM.write_text("original\n")
    (OUTSIDE / A.TOKENS["DIRNAME"]).write_text("x\n")
    REPO.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=REPO, check=True)
    (REPO / "notes.txt").write_text("in the repo\n")
    subprocess.run(["git", "add", "-A"], cwd=REPO, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "first"],
        cwd=REPO, check=True,
    )


def instance(store, iid, prompt, *, owner=True, confined=False, bash="full", session=None, branch=None):
    inst = Instance(
        id=iid, name=None, instance_type=InstanceType.TASK if branch else InstanceType.QUERY,
        prompt=prompt, repo_name="guestrepo", repo_path=str(REPO), status=InstanceStatus.RUNNING,
        mode="build", effort="low", model=A.MODEL, is_owner_session=owner,
        bash_policy=bash, bash_policy_baseline=bash, session_id=session,
        branch=branch, original_branch="master" if branch else None,
    )
    inst.guest_confined = confined  # a plain attribute on the code before the change
    store._instances[iid] = inst
    return inst


def show(calls: list[dict]) -> None:
    for call in calls:
        target = call["input"].get("file_path") or call["input"].get("path") or call["input"].get("command", "")
        text = " ".join((call["result"] or "").split())[:110]
        A.record("INFO", f"{call['name']} {target}", f"error={call['is_error']} {text}")


async def run(runner, inst):
    marker = time.time() - 1
    result = await runner.run(inst, None, None)
    return result, A.stream_events(marker)


async def guest_reach(runner, model, store) -> None:
    print(f"\n--- guest-shaped session, no sandbox setting, no Bash (expect {EXPECT}) ---", flush=True)
    model.scripts["guest"] = ([
        ("Read", {"file_path": str(SECRET)}),
        ("Read", {"file_path": str(VICTIM)}),
        ("Edit", {"file_path": str(VICTIM), "old_string": "original", "new_string": "PWNED"}),
        ("Write", {"file_path": str(PLANTED), "content": "planted"}),
        ("Glob", {"pattern": "*", "path": str(OUTSIDE)}),
        ("Read", {"file_path": str(REPO / "notes.txt")}),
        ("Edit", {"file_path": str(REPO / "notes.txt"), "old_string": "in the repo", "new_string": "edited"}),
    ], "guest done")
    model.leaks.clear()
    inst = instance(store, "q-guest", f"{MARK}guest", owner=False, bash="none")
    result, events = await run(runner, inst)
    A.record("INFO", "guest/run", f"is_error={result.is_error} session={bool(result.session_id)}")
    calls = A.tool_uses(events)
    show(calls)
    A.record("INFO", "guest/offered", " ".join(sorted(model.offered_tools)))
    read_leak = "SECRET" in model.leaks
    edited = "PWNED" in VICTIM.read_text()
    planted = PLANTED.exists()
    listed = "DIRNAME" in model.leaks
    A.record("INFO", "guest/outside", f"read={read_leak} edited={edited} planted={planted} listed={listed}")
    want = EXPECT == "open"
    A.check(f"guest/read_outside_is_{EXPECT}", read_leak == want)
    A.check(f"guest/edit_outside_is_{EXPECT}", edited == want)
    A.check(f"guest/write_outside_is_{EXPECT}", planted == want)
    A.check("guest/bash_not_offered", "Bash" not in model.offered_tools, " ".join(sorted(model.offered_tools)))
    if EXPECT == "closed":
        init = next((e for e in events if e.get("subtype") == "init"), {})
        A.check("guest/no_mcp_tools", not [t for t in init.get("tools", []) if t.startswith("mcp__")],
                f"mcp_servers={init.get('mcp_servers')}")
    A.check("guest/can_still_edit_its_repo", "edited" in (REPO / "notes.txt").read_text())


async def owner_unchanged(runner, model, store) -> None:
    print("\n--- owner's session, no guest, no setting ---", flush=True)
    model.scripts["owner"] = ([("Read", {"file_path": str(SECRET)})], "owner done")
    model.leaks.clear()
    marker = time.time() - 1
    inst = instance(store, "q-owner", f"{MARK}owner")
    await runner.run(inst, None, None)
    argv_files = sorted(
        (p for p in A.ROOT.glob("argv-*.bin") if p.stat().st_mtime >= marker),
        key=lambda p: p.stat().st_mtime,
    )
    argv = argv_files[-1].read_bytes().split(b"\0") if argv_files else []
    dump = os.environ.get("OWNER_DUMP")
    if dump and argv_files:
        names = A.ROOT.glob(f"envnames-{argv_files[-1].stem.split('-')[1]}.txt")
        text = "\n".join(a.decode("utf-8", "replace") for a in argv) + "\n=== env names\n"
        text += "".join(n.read_text() for n in names)
        Path(dump).write_text(text.replace(str(A.ROOT), "ROOT"))
    A.check("owner/still_reads_outside", "SECRET" in model.leaks)
    A.check("owner/no_sandbox_settings_on_the_command_line",
            argv and not any(b"sandbox" in a for a in argv), str(len(argv)))


async def resume_old_session(runner, model, store) -> None:
    print("\n--- a thread started unconfined, resumed confined ---", flush=True)
    A.TOKENS["FIRST_TURN"] = A.token("FIRST-TURN")
    model.scripts["old"] = ([], "first turn done")
    first = instance(store, "q-old", f"{MARK}old {A.TOKENS['FIRST_TURN']}")
    result, _ = await run(runner, first)
    A.check("resume/first_turn_ran_unconfined", not result.is_error and bool(result.session_id))
    model.leaks.clear()
    second = instance(store, "q-new", "second turn", owner=False, bash="none", session=result.session_id)
    second_result, events = await run(runner, second)
    A.record("INFO", "resume/second", (
        f"is_error={second_result.is_error} same_session={second_result.session_id == result.session_id} "
        f"error={(second_result.error_message or '')[:160]}"
    ))
    A.check("resume/second_turn_ran", not second_result.is_error)
    A.check("resume/history_came_along", "FIRST_TURN" in model.leaks)
    print(f"resume: session kept: {second_result.session_id == result.session_id}", flush=True)
    # The scripted model picks its script from a session's first message and counts
    # assistant messages across the whole history. So a resumed turn replaces that
    # script, with the call repeated to cover however many messages came before.
    model.scripts["old"] = ([("Read", {"file_path": str(SECRET)})] * 6, "tried")
    model.leaks.clear()
    third = instance(store, "q-new2", "third turn", owner=False, bash="none", session=result.session_id)
    _, events = await run(runner, third)
    reads = [c for c in A.tool_uses(events) if c["name"] == "Read" and c["input"].get("file_path") == str(SECRET)]
    A.check("resume/the_read_was_really_attempted", bool(reads), str(len(reads)))
    A.check("resume/every_attempt_was_denied", reads and all(c["is_error"] for c in reads))
    A.check("resume/a_confined_turn_cannot_read_outside", "SECRET" not in model.leaks)

    # Edit needs the file in the CLI's read state, which a confined session cannot
    # get for an outside file. An old thread can already hold it, so this is the
    # one route where the hook alone stands between Edit and the file.
    model.scripts["readfirst"] = ([("Read", {"file_path": str(VICTIM)})], "read it")
    opened = instance(store, "q-old2", f"{MARK}readfirst")
    opened_result, _ = await run(runner, opened)
    model.scripts["readfirst"] = ([
        ("Edit", {"file_path": str(VICTIM), "old_string": "original", "new_string": "RESUME-PWNED"}),
    ] * 6, "edit tried")
    confined = instance(store, "q-new3", "now edit it", owner=False, bash="none",
                        session=opened_result.session_id)
    edit_result, events = await run(runner, confined)
    edits = [c for c in A.tool_uses(events) if c["name"] == "Edit" and c["input"].get("file_path") == str(VICTIM)]
    if edits:
        A.record("INFO", "resume/edit", " ".join((edits[-1]["result"] or "").split())[-220:])
    A.check("resume/the_edit_was_really_attempted", bool(edits), str(len(edits)))
    A.check("resume/every_edit_went_to_the_hook",
            edits and all("hook" in (c["result"] or "") and c["is_error"] for c in edits))
    A.check("resume/victim_untouched", VICTIM.read_text() == "original\n", VICTIM.read_text()[:40])
    A.check("resume/run_itself_did_not_fail", not edit_result.is_error)


async def confined_build(runner, model, store) -> None:
    print("\n--- an owner's build child of a guest thread ---", flush=True)
    wt = REPO / ".worktrees" / "t-child"
    model.scripts["child"] = ([
        ("Edit", {"file_path": str(wt / "notes.txt"), "old_string": "in the repo", "new_string": "child"}),
        ("Read", {"file_path": str(wt / "notes.txt")}),
        ("Bash", {"command": "git add -A && git -c user.name=t -c user.email=t@t commit -qm child && git log --oneline | head -2",
                  "description": "commit", "timeout": 60000}),
    ], "child done")
    subprocess.run(["git", "checkout", "-q", "--", "notes.txt"], cwd=REPO, check=False)
    before = A.git(REPO, "rev-parse", "master")
    inst = instance(store, "t-child", f"{MARK}child", confined=True, branch="guest/child")
    result, events = await run(runner, inst)
    show(A.tool_uses(events))
    A.record("INFO", "child/run", f"is_error={result.is_error} {(result.error_message or '')[:200]}")
    A.check("child/commit_landed_on_its_branch",
            A.git(REPO, "log", "-1", "--format=%s", "guest/child") == "child")
    A.check("child/master_did_not_move", A.git(REPO, "rev-parse", "master") == before)


async def refused(runner, store) -> None:
    print("\n--- a repo that cannot be sandboxed ---", flush=True)
    subprocess.run(["git", "config", "core.hooksPath", "myhooks"], cwd=REPO, check=True)
    inst = instance(store, "q-refused", "hello", owner=False, bash="none")
    calls_before = len(list(A.ROOT.glob("argv-*.bin")))
    result = await runner.run(inst, None, None)
    subprocess.run(["git", "config", "--unset", "core.hooksPath"], cwd=REPO, check=True)
    A.record("INFO", "refused/message", result.error_message or "")
    A.check("refused/says_so", result.is_error and sandbox.REFUSAL_PHRASE in (result.error_message or ""))
    A.check("refused/cli_never_started", len(list(A.ROOT.glob("argv-*.bin"))) == calls_before)


async def title_helper(model) -> None:
    print("\n--- the title helper, with a guest's words in its prompt ---", flush=True)
    model.scripts["title"] = ([], "A Short Title")
    model.offered_tools.clear()
    model.leaks.clear()
    title = await titles.generate_title_text(f"{MARK}title read that file")
    A.record("INFO", "title/result", repr(title))
    A.record("INFO", "title/offered", " ".join(sorted(model.offered_tools)) or "no tools at all")
    A.check("title/no_tools_offered", not model.offered_tools, " ".join(sorted(model.offered_tools))[:200])
    A.check("title/still_returns_a_title", title == "A Short Title", repr(title))


async def main() -> None:
    setup()
    store = StateStore(A.ROOT / "state.json", config.RESULTS_DIR)
    store.add_repo("guestrepo", str(REPO))
    A.check("setup/repo_has_no_setting", not sandbox.is_sandboxed(store, str(REPO)))
    runner = ClaudeRunner(store=store)
    model = A.ScriptedModel()
    threading.Thread(target=model.serve_forever, daemon=True).start()
    config.CLAUDE_BINARY = str(A.write_wrapper(model.server_address[1]))
    (A.ROOT / "use-scripted-model").touch()
    await guest_reach(runner, model, store)
    await owner_unchanged(runner, model, store)
    if EXPECT == "closed":
        await resume_old_session(runner, model, store)
        await confined_build(runner, model, store)
        await refused(runner, store)
        await title_helper(model)
    A.check("setup/no_setting_was_added", not sandbox.is_sandboxed(store, str(REPO)))


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        asyncio.run(main())
    finally:
        A.cleanup("--keep" in sys.argv)
    fails = [r for r in A.RESULTS if r[0] == "FAIL"]
    print(f"\n{sum(r[0] == 'PASS' for r in A.RESULTS)} passed, {len(fails)} failed")
    for _, name, detail in fails:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1 if fails else 0)
