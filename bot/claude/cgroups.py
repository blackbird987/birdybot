"""Per-session cgroups, and proof that the machine's resource knobs are on.

Two jobs, both learned from the same incident.

**The supervisor must outlive the workload.** systemd-oomd picks its victim one
cgroup at a time. With every session running directly inside the bot's service
cgroup, the only victim available was the whole unit, so a single runaway build
took the supervisor and every other session with it: Sep 14 20:34:46, Sep 20
10:39:47, Sep 21 14:42:18, six live sessions each time. Spawning each session
into its own transient scope splits them apart. The scope also comes with the
two controls the tree walker never had: a soft memory ceiling that throttles
instead of killing, and an exact ``memory.current`` that counts a reparented
compiler server the process tree cannot see.

**A protection that is off must say so.** On 2026-09-21 the live cgroup read
``cpu.weight=100`` while both copies of the unit file said 20. The 2026-09-08
fix had been installed with ``systemctl --user set-property --runtime``, and
systemd resets a ``--runtime`` drop-in when the unit stops -- which is exactly
what an oomd kill does. The protection uninstalled itself on the one event it
exists for and nothing noticed for a week. ``check_weights`` reads the live
cgroup files, because that is the only source that would have been wrong.

Everything here is optional at runtime and fails toward today's behaviour. If
``systemd-run`` is missing, the slice will not start, or a limit will not
write, sessions spawn and run exactly as they did before. A resource
refinement that can stop work from starting is a worse bug than the one it
fixes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from bot import config
from bot.claude import memory

log = logging.getLogger(__name__)

_MB = 1024 * 1024


# --- Layer 1: is the CPU/IO protection actually applied right now? ------------


@dataclass
class WeightCheck:
    """Live cgroup weights against the values the unit file is meant to give.

    ``None`` for a live reading means "could not be read", which is neither a
    pass nor a fail: a machine without cgroup v2, or a path that moved, must
    not produce a weekly warning nobody can act on.
    """

    cpu_live: int | None = None
    io_live: int | None = None
    cpu_expected: int = 0
    io_expected: int = 0
    error: str | None = None

    def mismatches(self) -> list[tuple[str, int, int]]:
        """``(knob, live, expected)`` for each weight that is demonstrably wrong."""
        out: list[tuple[str, int, int]] = []
        if self.cpu_expected and self.cpu_live is not None:
            if self.cpu_live != self.cpu_expected:
                out.append(("cpu.weight", self.cpu_live, self.cpu_expected))
        if self.io_expected and self.io_live is not None:
            if self.io_live != self.io_expected:
                out.append(("io.weight", self.io_live, self.io_expected))
        return out

    def ok(self) -> bool:
        return not self.mismatches()

    def summary(self) -> str:
        if self.error:
            return f"weights: {self.error}"
        bits = [
            f"cpu.weight={self.cpu_live if self.cpu_live is not None else '?'}"
            f"/{self.cpu_expected or '-'}",
            f"io.weight={self.io_live if self.io_live is not None else '?'}"
            f"/{self.io_expected or '-'}",
        ]
        return ("weights ok: " if self.ok() else "weights WRONG: ") + ", ".join(bits)

    def warning_text(self) -> str:
        """Plain sentence for a log line and for The Ark. Empty when fine."""
        bad = self.mismatches()
        if not bad:
            return ""
        parts = [
            f"{knob} is {live} but should be {want}" for knob, live, want in bad
        ]
        return (
            "The bot's CPU/IO priority protection is not applied: "
            + ", ".join(parts)
            + ". Until it is fixed the bot competes with the desktop on equal "
            "terms, which is how a build can freeze the machine. This usually "
            "means a stale systemd drop-in is overriding the unit file: check "
            "`systemctl --user show claude-bot.service -p DropInPaths` and "
            "delete anything under /run/user/*/systemd/user.control/."
        )


def _read_weight(path: Path) -> int | None:
    """Parse a cgroup weight file. ``io.weight`` spells it ``default 20``."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    # io.weight is "default N" plus optional per-device lines; cpu.weight is
    # a bare integer. Take the first integer anywhere in the first line.
    first = text.splitlines()[0] if text else ""
    match = re.search(r"\d+", first)
    if not match:
        return None
    try:
        return int(match.group())
    except ValueError:
        return None


def check_weights() -> WeightCheck:
    """Compare the live cgroup weights against the intended ones. Never raises.

    Reads the cgroup, deliberately, and not ``systemctl show`` or the unit
    file. Both of those reported the correct value throughout the week the
    protection was actually off: ``systemctl show`` was reporting the merged
    view at a moment the drop-in agreed, and the unit file is only ever an
    intention. ``cpu.weight`` is what the scheduler uses.
    """
    out = WeightCheck(
        cpu_expected=max(0, config.RESOURCE_CPU_WEIGHT_EXPECTED),
        io_expected=max(0, config.RESOURCE_IO_WEIGHT_EXPECTED),
    )
    if sys.platform != "linux":
        out.error = "not linux"
        return out
    cg = memory._own_cgroup_path()
    if cg is None:
        out.error = "no cgroup"
        return out
    out.cpu_live = _read_weight(cg / "cpu.weight")
    out.io_live = _read_weight(cg / "io.weight")
    if out.cpu_live is None and out.io_live is None:
        out.error = "weights unreadable"
    return out


# --- Layer 3: each session in its own scope ----------------------------------
#
# `systemd-run --user --scope` and not `--unit`. A scope execs the payload in
# the caller's own process context: the pipes asyncio handed it survive, the
# child stays the bot's child, and `proc.pid` is still the pid to renice, to
# signal and to walk a tree from. `--unit` would hand the command to the
# service manager and give back a pid that is nobody's child, which would
# break the stream-json reader, the kill path and the watchdog at once.
#
# Verified on this machine before it was built on: the wrapped child reported
# its own pid as the one asyncio returned, a 19-byte stdin write arrived, the
# scope's cgroup accepted a memory.high write, cgroup.kill was present, and
# terminate() produced -15 -- the shape runner.is_kill_shape already accepts.

# The transient unit name. Must end in .scope and must not collide, so the
# instance id carries it: ids are unique per run and already appear in every
# log line about the session, which makes `systemd-cgls` readable next to
# `bot.log` with no translation step.
_SCOPE_PREFIX = "claude-session-"
# The hyphen is last so it is a literal, not a range. It was once spelled
# `\\-` inside a raw string, which is a literal backslash followed by a
# literal hyphen -- so the class allowed a backslash through into a
# systemd unit name, where it is an escape character.
_SCOPE_SAFE = re.compile(r"[^A-Za-z0-9_.-]")

# Three-state, deliberately: None means "not probed yet". Probing is a real
# subprocess, so it happens once per bot lifetime rather than once per spawn,
# and a machine where scopes do not work pays for the discovery exactly once.
_scope_supported: bool | None = None
_scope_probe_lock: asyncio.Lock | None = None
_scope_reason: str = ""
_scope_probed_at: float = 0.0
_scope_stale: bool = False


def scope_unit_name(instance_id: str) -> str:
    """Transient unit name for a session, e.g. ``claude-session-t-8658.scope``."""
    safe = _SCOPE_SAFE.sub("-", instance_id)[:64] or "unknown"
    return f"{_SCOPE_PREFIX}{safe}.scope"


def scope_status() -> tuple[bool, str]:
    """Whether scopes are in use, and why not when they are not."""
    return bool(_scope_supported), _scope_reason


def _probe_is_stale() -> bool:
    """Whether the cached probe answer has to be re-established before use."""
    if _scope_stale:
        return True
    ttl = config.SESSION_SCOPE_PROBE_TTL_SECS
    if ttl <= 0:
        return False
    return (time.monotonic() - _scope_probed_at) >= ttl


def invalidate_scope_probe() -> None:
    """Force the next ``ensure_scope_support`` to establish the answer again.

    Called when a session that *was* wrapped never reached a scope. That is
    the signature of a user service manager that has stopped running jobs,
    seen twice on 2026-09-21 (every job ``waiting``, none ``running``, behind
    a crash-looping unit), where ``systemd-run`` blocks on its D-Bus call
    forever and the wrapped session never execs at all. Without this, one
    cached "yes" keeps wrapping every later spawn into the same hang; with
    it, the next spawn pays one bounded probe and then runs unwrapped,
    exactly as a machine with no systemd at all does.
    """
    global _scope_stale
    _scope_stale = True


async def ensure_scope_support() -> bool:
    """Can we actually put a process in a scope? Cached, re-checked. Never raises.

    A probe rather than a try/except around the real spawn, because the real
    spawn is the one thing that must not be retried. By the time a session's
    `systemd-run` fails, the runner has already registered the process, told
    the thread it started, and armed the watchdog; unwinding all of that to
    try again is a far larger blast radius than running `true` once at boot.

    Cached with a TTL rather than answered once per bot lifetime, because the
    answer is a property of the *service manager*, which can change under a
    process that lives for weeks: a wedged manager makes a working machine
    stop working, and clearing the wedge makes it work again. A stale cache
    in the first direction hangs every spawn; in the second it leaves the
    protection off until the next reboot. The probe itself is a ``true``, and
    a cache miss costs one of those per TTL.
    """
    global _scope_supported, _scope_probe_lock, _scope_reason
    global _scope_probed_at, _scope_stale
    if _scope_supported is not None and not _probe_is_stale():
        return _scope_supported
    if _scope_probe_lock is None:
        _scope_probe_lock = asyncio.Lock()
    async with _scope_probe_lock:
        if _scope_supported is not None and not _probe_is_stale():
            return _scope_supported
        previous = _scope_supported
        _scope_supported, _scope_reason = await _probe_scope()
        _scope_probed_at = time.monotonic()
        _scope_stale = False
        changed = previous != _scope_supported
    # Only on a change of answer. Every TTL it would be a line that never
    # says anything, which is the same mistake the preflight valve flag
    # exists to avoid.
    if not changed:
        return _scope_supported
    if _scope_supported:
        log.info(
            "Session scopes enabled, sessions run in %s", config.SESSION_SLICE,
        )
    else:
        log.warning(
            "Session scopes unavailable (%s), sessions will run inside the "
            "bot's own cgroup, as they did before. An oomd kill will take the "
            "whole unit with them.",
            _scope_reason,
        )
    return _scope_supported


async def _probe_scope() -> tuple[bool, str]:
    if not config.SESSION_SCOPES_ENABLED:
        return False, "disabled by config"
    if sys.platform != "linux":
        return False, "not linux"
    if shutil.which("systemd-run") is None:
        return False, "systemd-run not found"
    if memory._own_cgroup_path() is None:
        return False, "no cgroup v2"
    cmd = [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        f"--slice={config.SESSION_SLICE}",
        # Unique per probe. A fixed name survives as a loaded unit after the
        # scope exits and every later probe fails with "already loaded",
        # which reads as "scopes do not work here" on a machine where they do.
        f"--unit={_SCOPE_PREFIX}probe-{os.getpid()}.scope",
        "--", "true",
    ]
    # Deliberately blocking subprocess.run on a worker thread, and NOT
    # asyncio.create_subprocess_exec. The runner spawns sessions through the
    # asyncio API, and every harness that counts session spawns patches it; a
    # probe sharing that seam is recorded as a session start, which is how a
    # one-shot `true` turned into a phantom third attempt in the memory-guard
    # suite. Same rule the PSI readers follow: a diagnostic gets its own seam.
    try:
        res = await asyncio.to_thread(
            subprocess.run, cmd,
            capture_output=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return False, "systemd-run probe timed out"
    except Exception as exc:
        return False, f"{type(exc).__name__} running systemd-run"
    if res.returncode != 0:
        detail = (res.stderr or b"").decode(errors="replace").strip().splitlines()
        return False, (detail[-1] if detail else f"rc={res.returncode}")
    return True, ""


def wrap_command(cmd: list[str], instance_id: str) -> list[str]:
    """Prefix ``cmd`` with the scope wrapper, or hand it straight back.

    Returning the command unchanged is a supported outcome, not a failure
    path: it is what happens on Windows, on a machine without systemd, and
    whenever the probe said no. The caller does not branch on it.
    """
    if not _scope_supported:
        return list(cmd)
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        f"--slice={config.SESSION_SLICE}",
        f"--unit={scope_unit_name(instance_id)}",
        "--", *cmd,
    ]


# --- Layer 4: what a session's own cgroup lets us do --------------------------


@dataclass
class SessionCgroup:
    """A live session's own cgroup, when it got one."""

    path: Path
    unit: str = ""
    applied: tuple[str, ...] = field(default_factory=tuple)

    def current_mb(self) -> float | None:
        """Memory charged to this session, including anything it reparented.

        The number the tree walker cannot produce. ``dotnet build`` leaves a
        Roslyn server detached and parented to PID 1 on purpose, so it leaves
        every process tree the bot holds while never leaving the cgroup it was
        charged to.
        """
        value = memory._read_int(self.path / "memory.current")
        return None if value is None else value / _MB

    def kill(self) -> bool:
        """Kill every process in the subtree at once, via ``cgroup.kill``.

        Atomic where ``kill_tree`` is a walk: nothing can fork out from under
        it, and a process that reparented away is still in here. Returns False
        when the file is absent (pre-5.14 kernels) so the caller falls back.

        Absence is checked, not inferred from the write failing. On a real
        cgroupfs a missing attribute cannot be created, so the write would
        error by itself, but that is a property of the filesystem rather than
        of this code: anywhere else the open creates the file, this reports a
        kill that killed nothing, and the caller skips the tree walk that is
        the entire mechanism on an older kernel.
        """
        target = self.path / "cgroup.kill"
        if not target.is_file():
            return False
        try:
            target.write_text("1", encoding="utf-8")
            return True
        except OSError:
            return False


def cgroup_of_pid(pid: int) -> Path | None:
    """Filesystem path of the cgroup ``pid`` is in, or None."""
    if sys.platform != "linux":
        return None
    try:
        raw = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            path = Path("/sys/fs/cgroup") / parts[2].lstrip("/")
            return path if path.is_dir() else None
    return None


def _write_limit(path: Path, name: str, mb: int) -> bool:
    if mb <= 0:
        return False
    try:
        (path / name).write_text(str(int(mb) * _MB), encoding="utf-8")
        return True
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _apply_ceilings(path: Path, unit: str) -> SessionCgroup:
    cg = SessionCgroup(path=path, unit=unit)
    applied: list[str] = []
    # high before max. If both are going to be written, the moment between
    # them should be the safe ordering: a cgroup briefly holding only a soft
    # throttle is harmless, one briefly holding only a hard cap is not.
    if _write_limit(path, "memory.high", config.SESSION_MEM_HIGH_MB):
        applied.append(f"high={config.SESSION_MEM_HIGH_MB / 1024:.1f}GB")
    if _write_limit(path, "memory.max", config.SESSION_MEM_HARD_MB):
        applied.append(f"max={config.SESSION_MEM_HARD_MB / 1024:.1f}GB")
    cg.applied = tuple(applied)
    return cg


async def adopt_session(
    pid: int, instance_id: str, timeout_s: float | None = None,
) -> SessionCgroup | None:
    """Wait for a freshly spawned session's scope, then apply its ceilings.

    **Polled, not read once, and that is the whole correctness of it.**
    ``systemd-run --scope`` registers the transient unit over D-Bus and only
    execs the payload once the manager has answered, so at the instant
    ``create_subprocess_exec`` returns, the pid is still charged to the bot's
    own cgroup. Measured on this machine, three trials out of three: still in
    ``claude-bot.service`` at t=0, in its own scope by t=50ms. A single read
    at t=0 therefore fails the identity check every single time, and fails it
    *silently* -- it is indistinguishable from "this session got no scope", so
    no ceiling is written, no cgroup accounting replaces the tree walk, and no
    atomic kill is available, on a machine where all three were working.

    Returns None when the process never lands in a scope of its own, which the
    caller treats as "carry on exactly as before". The identity check stays:
    without it a failed scope would leave the session in the bot's own cgroup
    and this would cheerfully write a 6 GB ``memory.high`` onto the
    supervisor. Waiting is skipped entirely when scopes are not in use, so the
    machines that never had one do not pay the budget on every spawn.
    """
    if not _scope_supported:
        return None
    unit = scope_unit_name(instance_id)
    budget = (
        config.SESSION_SCOPE_ADOPT_SECS if timeout_s is None else timeout_s
    )
    deadline = time.monotonic() + max(0.0, budget)
    while True:
        path = cgroup_of_pid(pid)
        if path is not None and path.name == unit:
            return _apply_ceilings(path, unit)
        # A process that is already gone is not late, it is finished, and
        # there is nothing to adopt or to conclude about the machine.
        if not _pid_alive(pid):
            return None
        if time.monotonic() >= deadline:
            # It was wrapped and it never arrived. Distrust the cached probe:
            # on a wedged service manager systemd-run blocks forever and the
            # session has not even execed yet, so every later spawn would
            # hang the same way. See invalidate_scope_probe.
            log.warning(
                "%s did not reach its own scope within %.1fs (cgroup is %s); "
                "re-checking whether scopes work before the next spawn",
                instance_id, budget, path.name if path else "unreadable",
            )
            invalidate_scope_probe()
            return None
        await asyncio.sleep(config.SESSION_SCOPE_ADOPT_POLL_SECS)


def session_slice_path() -> Path | None:
    """Directory of the slice sessions run in, or None when there isn't one."""
    if not _scope_supported:
        return None
    parent = memory.parent_slice_path()
    if parent is None:
        return None
    path = parent / config.SESSION_SLICE
    return path if path.is_dir() else None


def session_slice_roots() -> tuple[Path, ...]:
    """Extra cgroup roots the orphan scan has to cover, now that sessions moved.

    Before scopes, "our cgroup minus our process tree" found every reparented
    build daemon, because there was only one cgroup. A session's Roslyn server
    now stays in that session's scope after the session ends, where the bot's
    own ``cgroup.procs`` will never list it. Handing these roots to the scan
    keeps the reclaim path seeing exactly what it saw before.
    """
    path = session_slice_path()
    return (path,) if path is not None else ()


def slice_headroom_mb() -> float | None:
    """MB left before the session slice's own ``memory.max``, or None.

    None when sessions are not in a slice, when the slice carries no limit, or
    when the files will not read. Admission treats None as "no finding", the
    same rule the pressure readers follow: a budget check that cannot measure
    must not refuse work.
    """
    path = session_slice_path()
    if path is None:
        return None
    limit = memory._read_int(path / "memory.max")
    current = memory._read_int(path / "memory.current")
    if limit is None or current is None:
        return None
    return max(0.0, (limit - current) / _MB)
