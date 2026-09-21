#!/usr/bin/env python3
"""Regression test: a session gets its own cgroup, and the bot survives it.

The 2026-09-21 incident in one sentence: systemd-oomd SIGKILLed
claude-bot.service because the slice it lives in was stalling at 94.76%
against an 80% kill limit, and every process every session had spawned was
inside that one unit, so the smallest thing oomd could choose was "the
supervisor and all twelve live conversations". The same unit had been killed
that way on 14 and 20 September.

The structural answer is to give oomd a session-sized victim to choose. Each
session is spawned through ``systemd-run --user --scope`` into
``app-claudesessions.slice``, the supervisor carries
``ManagedOOMPreference=avoid``, and the sessions slice is armed one rung
tighter than app.slice so the inner limit is reached first.

Everything here is about the properties that make that safe rather than
merely clever, because the machine this runs on is the user's desktop and a
session that cannot start is worse than a session that runs unprotected:

  * the wrapper is a prefix on the command and nothing else, so the process
    asyncio gets back is still the bot's own child -- ``--scope`` execs in the
    caller's context where ``--unit`` would hand the job to the service
    manager and return a pid that is nobody's child, breaking the stream-json
    reader, the kill path and the watchdog at once
  * a machine without systemd, without systemd-run, or with scopes switched
    off runs sessions exactly as it did before, and says so once
  * ``terminate()`` through a scope still produces a returncode that
    ``runner.is_kill_shape`` recognises, or every Kill and Steer renders as a
    red FAILED card and can be restarted on the backup subscription
  * the per-session ceilings are only ever written into a cgroup that really
    is that session's scope, because the failure mode of getting that wrong is
    a 6 GB ``memory.high`` on the supervisor
  * the orphan scan still sees reparented build daemons, which now sit in a
    scope the bot's own ``cgroup.procs`` will never list

Run:  python scripts/test_session_cgroups.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import ast
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude import cgroups, memory
from bot.claude import runner as runner_mod


def _check_wrapper_shape(failures: list[str]) -> None:
    """What the command becomes, and what it must never become."""
    saved = cgroups._scope_supported
    try:
        cgroups._scope_supported = False
        plain = cgroups.wrap_command(["claude", "-p"], "t-1")
        if plain != ["claude", "-p"]:
            failures.append(
                f"with scopes unsupported the command was still rewritten: {plain}"
            )

        cgroups._scope_supported = True
        wrapped = cgroups.wrap_command(["claude", "-p", "--resume", "abc"], "t-42")
        if wrapped[0] != "systemd-run":
            failures.append(f"the wrapper does not start with systemd-run: {wrapped[0]}")
        if "--scope" not in wrapped:
            failures.append(
                "the wrapper does not pass --scope. --unit would hand the "
                "command to the service manager and give back a pid that is "
                "nobody's child, which breaks the stream-json reader, the kill "
                "path and the watchdog at once"
            )
        if f"--slice={config.SESSION_SLICE}" not in wrapped:
            failures.append(f"the wrapper does not name the sessions slice: {wrapped}")
        sep = wrapped.index("--")
        if wrapped[sep + 1:] != ["claude", "-p", "--resume", "abc"]:
            failures.append(
                f"the wrapped payload was altered: {wrapped[sep + 1:]}"
            )
        if "--collect" not in wrapped:
            failures.append(
                "the scope is not --collect, so every finished session leaves "
                "a loaded unit behind and the names accumulate"
            )

        # The unit name has to be derived from the instance id, or two
        # concurrent sessions collide and the second one fails to start.
        a = cgroups.scope_unit_name("t-8658")
        b = cgroups.scope_unit_name("q-18347")
        if a == b:
            failures.append("two instance ids produced the same scope unit name")
        for name in (a, b):
            if not name.endswith(".scope"):
                failures.append(f"scope unit name {name!r} does not end in .scope")
        if "t-8658" not in a:
            failures.append(
                f"the scope name {a!r} does not carry the instance id, so "
                "systemd-cgls cannot be read next to bot.log"
            )
        # A hostile id must not be able to inject arguments or path segments.
        nasty = cgroups.scope_unit_name("../../etc/passwd x --property=Foo")
        if "/" in nasty or " " in nasty:
            failures.append(f"an unsafe instance id produced {nasty!r}")
    finally:
        cgroups._scope_supported = saved


def _check_probe_seam(failures: list[str]) -> None:
    """The probe must not look like a session spawn.

    The runner spawns sessions through ``asyncio.create_subprocess_exec`` and
    every harness that counts spawns patches it. A probe sharing that seam is
    counted as a session start -- which is exactly how a one-shot ``true``
    turned into a phantom third attempt in the memory-guard suite.
    """
    src = (Path(__file__).resolve().parents[1] / "bot" / "claude" / "cgroups.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))
    # The name is searched for in the *syntax*, not in the file's characters.
    # The reason this module avoids the API is written down beside the code
    # that avoids it, naming it, so a substring search over the source reports
    # the explanation as the offence -- it did, twice, once from a comment and
    # once from a docstring, which is what a `#`-stripping pass cannot catch.
    called = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    } | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    if "create_subprocess_exec" in called:
        failures.append(
            "cgroups.py spawns through asyncio.create_subprocess_exec, the "
            "same seam the runner uses for sessions; a diagnostic sharing it "
            "is recorded as a session start"
        )


async def _check_scope_disabled_path(failures: list[str]) -> None:
    """Switching scopes off has to be a complete, quiet fallback."""
    saved_flag = config.SESSION_SCOPES_ENABLED
    saved_state = (cgroups._scope_supported, cgroups._scope_reason)
    try:
        config.SESSION_SCOPES_ENABLED = False
        cgroups._scope_supported = None
        cgroups._scope_reason = ""
        ok = await cgroups.ensure_scope_support()
        if ok:
            failures.append("scopes probed as available while switched off")
        used, why = cgroups.scope_status()
        if used or "disabled" not in why:
            failures.append(f"the disabled reason was not recorded: {why!r}")
        if cgroups.wrap_command(["claude"], "t-1") != ["claude"]:
            failures.append("a disabled probe still rewrote the command")
        if cgroups.session_slice_path() is not None:
            failures.append(
                "a disabled probe still reported a sessions slice, so the "
                "orphan scan would walk a cgroup nothing runs in"
            )
        if cgroups.slice_headroom_mb() is not None:
            failures.append(
                "a disabled probe reported slice headroom; admission would "
                "gate on a number measured from nothing"
            )
    finally:
        config.SESSION_SCOPES_ENABLED = saved_flag
        cgroups._scope_supported, cgroups._scope_reason = saved_state


async def _check_adoption_identity(failures: list[str]) -> None:
    """Ceilings are written into the session's scope, or into nothing at all."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # A cgroup that is NOT the session's scope: the shape of a spawn where
        # systemd-run failed and the child stayed in the bot's own cgroup.
        wrong = root / "claude-bot.service"
        wrong.mkdir()
        saved = cgroups.cgroup_of_pid
        saved_state = (cgroups._scope_supported, cgroups._scope_stale)
        cgroups._scope_supported = True
        try:
            cgroups.cgroup_of_pid = lambda pid: wrong   # type: ignore[assignment]
            got = await cgroups.adopt_session(1234, "t-99", timeout_s=0.0)
            if got is not None:
                failures.append(
                    "a session that did not land in its own scope was adopted "
                    "anyway; this writes a per-session memory ceiling onto the "
                    "supervisor's cgroup"
                )
            if (wrong / "memory.high").exists() or (wrong / "memory.max").exists():
                failures.append(
                    "a ceiling was written into a cgroup that is not the "
                    "session's scope"
                )

            right = root / cgroups.scope_unit_name("t-99")
            right.mkdir()
            (right / "memory.high").write_text("max", encoding="utf-8")
            (right / "memory.max").write_text("max", encoding="utf-8")
            cgroups.cgroup_of_pid = lambda pid: right   # type: ignore[assignment]
            cg = await cgroups.adopt_session(1234, "t-99")
            if cg is None:
                failures.append("a session in its own scope was not adopted")
            else:
                want_high = config.SESSION_MEM_HIGH_MB * 1024 * 1024
                want_max = config.SESSION_MEM_HARD_MB * 1024 * 1024
                got_high = (right / "memory.high").read_text().strip()
                got_max = (right / "memory.max").read_text().strip()
                if got_high != str(want_high):
                    failures.append(
                        f"memory.high was written as {got_high}, expected {want_high}"
                    )
                if got_max != str(want_max):
                    failures.append(
                        f"memory.max was written as {got_max}, expected {want_max}"
                    )
                if not cg.applied:
                    failures.append("the applied ceilings were not recorded for the log")
                # The per-session figure the tree walker cannot produce.
                (right / "memory.current").write_text(
                    str(3 * 1024 * 1024 * 1024), encoding="utf-8",
                )
                if cg.current_mb() != 3072.0:
                    failures.append(
                        f"memory.current read back as {cg.current_mb()!r}MB, "
                        "expected 3072.0"
                    )
                # An unreadable figure is None, never 0: a 0 reads as "this
                # session holds nothing" and would hide a runaway.
                (right / "memory.current").unlink()
                if cg.current_mb() is not None:
                    failures.append(
                        "an unreadable memory.current came back as a number"
                    )
                # No cgroup.kill (pre-5.14 kernel) has to answer False so the
                # caller falls back to walking the tree.
                if cg.kill():
                    failures.append(
                        "kill() claimed success with no cgroup.kill file; the "
                        "caller would skip the tree walk that is the whole "
                        "mechanism on an older kernel"
                    )
                (right / "cgroup.kill").write_text("", encoding="utf-8")
                if not cg.kill():
                    failures.append("kill() failed against a writable cgroup.kill")

            # The scope does not exist yet at the instant the spawn returns.
            # systemd-run registers the transient unit over D-Bus and only
            # then execs, so a single read at t=0 sees the bot's own cgroup
            # and rejects it on identity -- indistinguishable from "no scope",
            # which silently costs every session its ceilings, its cgroup
            # accounting and its atomic kill. Measured on this machine as
            # "arrives by 50ms"; adoption has to wait for it.
            cgroups._scope_stale = False
            seen = {"n": 0}

            def _late(pid: int) -> Path:
                seen["n"] += 1
                return right if seen["n"] > 3 else wrong

            cgroups.cgroup_of_pid = _late               # type: ignore[assignment]
            # A live pid, because adoption stops early for a process that has
            # already exited: one that is gone is finished, not late, and
            # there is nothing to conclude about the machine from it.
            cg = await cgroups.adopt_session(os.getpid(), "t-99", timeout_s=5.0)
            if cg is None:
                failures.append(
                    "a scope that formed a few milliseconds after the spawn "
                    "was never adopted; this is what systemd-run actually "
                    "does, so no session would ever get its ceilings"
                )
            if cgroups._scope_stale:
                failures.append(
                    "a successful late adoption invalidated the scope probe"
                )

            # Giving up has to distrust the cached probe: a wrapped session
            # that never reaches a scope is the shape of a wedged service
            # manager, where systemd-run blocks forever and every later spawn
            # would hang the same way.
            cgroups.cgroup_of_pid = lambda pid: wrong   # type: ignore[assignment]
            cgroups._scope_stale = False
            await cgroups.adopt_session(os.getpid(), "t-99", timeout_s=0.0)
            if not cgroups._scope_stale:
                failures.append(
                    "a session that never reached its scope left the cached "
                    "probe answer trusted; a wedged service manager would "
                    "hang every later spawn in systemd-run"
                )
        finally:
            cgroups.cgroup_of_pid = saved               # type: ignore[assignment]
            cgroups._scope_supported, cgroups._scope_stale = saved_state


def _check_orphan_roots(failures: list[str]) -> None:
    """The reclaim path still sees what moved out of the bot's own cgroup."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "cgroup.procs").write_text("11\n12\n", encoding="utf-8")
        nested = root / "child.scope"
        nested.mkdir()
        (nested / "cgroup.procs").write_text("13\n", encoding="utf-8")

        saved = memory._own_cgroup_path
        try:
            memory._own_cgroup_path = lambda: root      # type: ignore[assignment]
            base = set(memory.cgroup_pids())
            if base != {11, 12, 13}:
                failures.append(
                    f"the cgroup scan missed nested scopes: {sorted(base)}"
                )
        finally:
            memory._own_cgroup_path = saved             # type: ignore[assignment]

        # An extra root is folded in, which is how a Roslyn server left behind
        # in a finished session's scope is still found.
        other = root.parent / "elsewhere"
        other.mkdir(exist_ok=True)
        (other / "cgroup.procs").write_text("21\n", encoding="utf-8")
        try:
            memory._own_cgroup_path = lambda: root      # type: ignore[assignment]
            with_extra = set(memory.cgroup_pids((other,)))
            if 21 not in with_extra:
                failures.append(
                    "an extra cgroup root was not scanned, so a build daemon "
                    "left behind in a finished session's scope is invisible to "
                    "the reclaim path"
                )
        finally:
            memory._own_cgroup_path = saved             # type: ignore[assignment]
            shutil.rmtree(other, ignore_errors=True)


async def _check_live_scope(failures: list[str]) -> None:
    """Against the real service manager, when there is one.

    Reported rather than asserted when scopes are unavailable: a machine
    without systemd is a supported configuration, and the fallback is the
    thing being relied on there.
    """
    cgroups._scope_supported = None
    cgroups._scope_reason = ""
    ok = await cgroups.ensure_scope_support()
    if not ok:
        print(f"NOTE: session scopes unavailable here ({cgroups.scope_status()[1]}); "
              "the fallback path is what runs, and the checks above cover it")
        return

    cmd = cgroups.wrap_command(
        [sys.executable, "-c",
         "import os,sys,time; sys.stdout.write(str(os.getpid())+'\\n'); "
         "sys.stdout.flush(); time.sleep(30)"],
        "t-livescope",
    )
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    # Adopted here, before a single byte has been read back, because that is
    # where the runner does it. Waiting for the child's first line first is
    # what hid the adoption race: by then the scope has existed for a
    # comfortable margin, and a single read at t=0 passes a test it fails in
    # production every time.
    live_cg = await cgroups.adopt_session(proc.pid, "t-livescope")
    if live_cg is None:
        failures.append(
            "a real scoped session was not adopted at the moment the spawn "
            "returned, which is the moment the runner adopts it: systemd-run "
            "has not finished registering the scope yet, so the session runs "
            "with no ceilings, no cgroup accounting and no atomic kill"
        )
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=20)
    except asyncio.TimeoutError:
        failures.append("a scoped child produced no output within 20s")
        proc.kill()
        await proc.wait()
        return

    child_pid = int(line.decode().strip())
    if child_pid != proc.pid:
        failures.append(
            f"the scoped child reports pid {child_pid} but asyncio holds "
            f"{proc.pid}. --scope must exec in place, or the kill path, the "
            "watchdog and the memory tree walk all address the wrong process"
        )

    path = cgroups.cgroup_of_pid(proc.pid)
    if path is None or path.name != cgroups.scope_unit_name("t-livescope"):
        failures.append(
            f"the child did not land in its own scope: {path}"
        )
    else:
        cg = live_cg if live_cg is not None else await cgroups.adopt_session(
            proc.pid, "t-livescope",
        )
        if cg is None:
            failures.append("a real scoped session was not adopted")
        else:
            if cg.current_mb() is None:
                failures.append("memory.current could not be read from a real scope")
            high = (path / "memory.high").read_text().strip()
            if high != str(config.SESSION_MEM_HIGH_MB * 1024 * 1024):
                failures.append(
                    f"the real scope's memory.high reads {high}, expected "
                    f"{config.SESSION_MEM_HIGH_MB * 1024 * 1024}"
                )
            if not (path / "cgroup.kill").exists():
                print("NOTE: this kernel has no cgroup.kill; reaps fall back "
                      "to walking the process tree")

    # The kill shape, through the scope. Getting this wrong makes every Kill
    # and Steer render as a red FAILED card -- and a failure with no turns is
    # the account-failover branch's signature, so the run can be restarted on
    # the backup subscription.
    proc.terminate()
    rc = await asyncio.wait_for(proc.wait(), timeout=20)
    if not runner_mod.is_kill_shape(rc):
        failures.append(
            f"terminate() through a scope produced returncode {rc}, which "
            "runner.is_kill_shape does not recognise as a kill"
        )

    # The slice really is the one oomd was armed on, and headroom is readable.
    slice_path = cgroups.session_slice_path()
    if slice_path is None:
        failures.append("scopes work but no sessions slice path resolved")
    else:
        if config.SESSION_SLICE not in str(slice_path):
            failures.append(f"the sessions slice resolved to {slice_path}")
        head = cgroups.slice_headroom_mb()
        if head is not None and head < 0:
            failures.append(f"slice headroom reported as {head}MB")
        pol = memory.read_oomd_policy(config.SESSION_SLICE)
        if not pol.active():
            failures.append(
                f"systemd-oomd is not armed on {config.SESSION_SLICE} "
                f"({pol.summary()}). Install "
                f"scripts/{config.SESSION_SLICE}.d/50-oomd.conf"
            )
        else:
            outer = memory.read_oomd_policy("app.slice")
            if outer.active() and pol.limit_pct >= outer.limit_pct:
                failures.append(
                    f"the sessions slice is armed at {pol.limit_pct:.0f}% and "
                    f"app.slice at {outer.limit_pct:.0f}%. The inner limit has "
                    "to be reached first, or app.slice fires and picks the "
                    "whole sessions slice -- every session at once"
                )
            print(f"      oomd: {config.SESSION_SLICE} at "
                  f"{pol.limit_pct:.0f}%, app.slice at "
                  f"{outer.limit_pct:.0f}%" if outer.active() else "")


async def _amain() -> int:
    failures: list[str] = []

    _check_wrapper_shape(failures)
    _check_probe_seam(failures)
    await _check_scope_disabled_path(failures)
    await _check_adoption_identity(failures)
    _check_orphan_roots(failures)
    await _check_live_scope(failures)

    if failures:
        print("FAIL: per-session cgroups")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASS: a session runs in its own scope inside "
          f"{config.SESSION_SLICE}, still as the")
    print("      bot's own child, with a "
          f"{config.SESSION_MEM_HIGH_MB / 1024:.0f} GB soft throttle and a "
          f"{config.SESSION_MEM_HARD_MB / 1024:.0f} GB hard ceiling of its own.")
    print("      A kill through the scope still reads as a kill, a machine "
          "without")
    print("      systemd-run runs exactly as before, and the reclaim path "
          "still")
    print("      finds build daemons left behind in a finished session's scope.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
