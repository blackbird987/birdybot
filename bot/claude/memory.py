"""Memory accounting for running sessions, and the reaper that acts on it.

Why this exists, concretely. On 2026-08-17 a build session ran a diffusion-model
experiment that grew to 13.7 GB on a 31 GB machine with 13 GB free and 8 GB of
*compressed-RAM* swap already full. The kernel picked the right victim — it shot
the experiment, not the bot — but every process a session spawns lives inside
the bot's systemd cgroup, and systemd's default reaction to a kill in its cgroup
is to stop the whole unit. So one runaway build took the bot and ten unrelated
sessions down with it, twice in ninety minutes, because the restart resumed the
same session and it re-ran the same job.

`OOMPolicy=continue` plus a `MemoryMax` in scripts/claude-bot.service is the
structural half of the fix. This module is the other half: the bot spawned the
runaway, so the bot should be the thing that notices and reaps it — early,
deterministically, and able to say which session and how many gigabytes. A
kernel OOM kill can do none of those things.

The load-bearing detail is that memory is measured across the whole process
tree. The CLI is a thin supervisor; the memory always lives in what it spawned.
The stall diagnostics that ran during the incident reported "336MB" for the
session, and they were not wrong about the process they were looking at.

Everything here is best-effort and must never raise into the runner: a sampler
that can crash a session is worse than no sampler. Failures come back as a
populated ``error`` field.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_MB = 1024 * 1024


@dataclass
class TreeMemory:
    """Resident memory of a process and everything it spawned.

    ``total_mb`` is the sum of RSS across the root process and all live
    descendants. RSS double-counts shared pages between a parent and its forks,
    which for our purpose is the right kind of wrong: it over-estimates
    slightly, and this is a safety guard where over-estimating is the safe
    direction.
    """

    total_mb: float = 0.0
    proc_count: int = 0
    biggest_pid: int | None = None
    biggest_name: str | None = None
    biggest_mb: float = 0.0
    error: str | None = None

    def summary(self) -> str:
        """One-line human form, e.g. ``4.2GB tree/7 procs (python 3.9GB)``."""
        if self.error and not self.proc_count:
            return f"tree=? ({self.error})"
        parts = [f"{self.total_mb / 1024:.1f}GB tree/{self.proc_count} procs"]
        if self.biggest_name and self.biggest_mb >= 1.0:
            parts.append(f"({self.biggest_name} {self.biggest_mb / 1024:.1f}GB)")
        if self.error:
            parts.append(f"[{self.error}]")
        return " ".join(parts)

    def offender(self) -> str:
        """Best available label for the process to blame, for user-facing text."""
        if self.biggest_name and self.biggest_pid:
            return f"{self.biggest_name} (pid {self.biggest_pid})"
        return self.biggest_name or "unknown"


def sample_tree(pid: int | None) -> TreeMemory:
    """Sum resident memory over ``pid`` and every live descendant.

    Synchronous and cheap — a readdir plus a small read per process — but call
    it from a worker thread anyway, because on a machine already thrashing on
    memory even /proc reads can block.

    Processes that exit mid-walk are skipped rather than treated as an error;
    a tree that is actively churning children is the normal case, not a fault.
    """
    snap = TreeMemory()
    if pid is None:
        snap.error = "no pid"
        return snap
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        snap.error = "psutil not installed"
        return snap

    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        snap.error = "NoSuchProcess"
        return snap
    except Exception as exc:  # defensive — never raise into the runner
        snap.error = type(exc).__name__
        return snap

    try:
        procs = [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        # Root died between the lookup and the walk. Not an error worth
        # surfacing — the caller's next tick will see the process gone.
        snap.error = "NoSuchProcess"
        return snap
    except Exception as exc:
        # Can't enumerate descendants (perms, /proc race). Fall back to the
        # root alone: a partial number the caller knows is partial beats none.
        procs = [root]
        snap.error = f"children:{type(exc).__name__}"

    for proc in procs:
        try:
            rss_mb = proc.memory_info().rss / _MB
            name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
        snap.total_mb += rss_mb
        snap.proc_count += 1
        if rss_mb > snap.biggest_mb:
            snap.biggest_mb = rss_mb
            snap.biggest_pid = proc.pid
            snap.biggest_name = name
    return snap


def available_mb() -> float | None:
    """Machine-wide memory available for allocation without swapping, in MB.

    ``available`` rather than ``free`` on purpose: page cache is free for the
    taking, so ``free`` reads alarmingly low on a healthy machine and would
    make every message this feeds into wrong.
    """
    try:
        import psutil  # type: ignore[import-not-found]

        return psutil.virtual_memory().available / _MB
    except Exception:
        return None


def kill_tree(pid: int | None, grace_secs: float = 5.0) -> list[str]:
    """Terminate ``pid`` and all descendants. Returns labels of what was signalled.

    Descendants are signalled *before* the root, and that ordering is the point.
    Killing the CLI first orphans whatever it was running: the runaway keeps its
    grip on the memory, gets reparented to init, and drops out of every process
    tree we know how to walk — so the guard would report a kill while the actual
    problem carried on. Reaping bottom-up leaves nothing behind.

    SIGTERM first so a script gets to clean up, SIGKILL for whatever ignores it.
    A process that eats 13 GB in two minutes is exactly the kind that ignores
    SIGTERM while inside a native allocation.

    Known gap, deliberate: a job launched with `nohup ... &` whose launching
    shell has already exited is no longer a descendant of anything we hold, so
    it cannot be found here. It still counts against the cgroup, which is why
    ``cgroup_usage_mb`` is reported alongside the tree and why the recovery
    nudge tells the agent to check for background jobs it left running.
    """
    signalled: list[str] = []
    if pid is None:
        return signalled
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return signalled

    try:
        root = psutil.Process(pid)
    except Exception:
        return signalled

    try:
        victims = list(reversed(root.children(recursive=True)))
    except Exception:
        victims = []
    victims.append(root)

    alive = []
    for proc in victims:
        try:
            label = f"{proc.name()}(pid {proc.pid}, {proc.memory_info().rss / _MB:.0f}MB)"
        except Exception:
            label = f"pid {proc.pid}"
        try:
            proc.terminate()
            signalled.append(label)
            alive.append(proc)
        except psutil.NoSuchProcess:
            continue
        except Exception as exc:
            log.warning("Could not terminate %s: %s", label, type(exc).__name__)

    if not alive:
        return signalled
    try:
        _, survivors = psutil.wait_procs(alive, timeout=grace_secs)
    except Exception:
        survivors = alive
    for proc in survivors:
        try:
            proc.kill()
            log.warning("SIGKILL for pid %s — ignored SIGTERM", proc.pid)
        except Exception:
            pass
    return signalled


# --- cgroup: what the whole bot is using, and what it is allowed ---------------
#
# The per-session tree answers "which session is at fault". The cgroup answers
# "how close is the bot as a whole to the ceiling systemd will enforce", which
# is the number that decides whether a kill is imminent. It also catches memory
# the tree walk structurally cannot see — a reparented background job stays in
# the cgroup forever even after it stops being anybody's descendant.


def _own_cgroup_path() -> Path | None:
    """Filesystem path of this process's cgroup v2 directory, or None."""
    if sys.platform != "linux":
        return None
    try:
        raw = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    # cgroup v2 has exactly one line, always "0::<path>".
    for line in raw.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            rel = parts[2].lstrip("/")
            path = Path("/sys/fs/cgroup") / rel
            return path if path.is_dir() else None
    return None


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


@dataclass
class CgroupMemory:
    """Memory accounting for the bot's own cgroup, all sessions together."""

    anon_mb: float | None = None    # actually-allocated memory; not reclaimable
    file_mb: float | None = None    # page cache; reclaimable, NOT a leak
    max_mb: float | None = None     # MemoryMax, or None when unlimited
    oom_kills: int | None = None    # cumulative kills by the cgroup OOM killer

    def headroom_mb(self) -> float | None:
        """MB of anonymous memory left before MemoryMax, or None if unlimited.

        Measured against anon only. Page cache counts toward the limit too, but
        crossing the limit on cache triggers reclaim rather than a kill, so
        counting it here would report a crisis during ordinary repo reads.
        """
        if self.max_mb is None or self.anon_mb is None:
            return None
        return max(0.0, self.max_mb - self.anon_mb)


def cgroup_memory() -> CgroupMemory:
    """Read the bot's own cgroup memory accounting. All fields None off Linux."""
    out = CgroupMemory()
    cg = _own_cgroup_path()
    if cg is None:
        return out
    max_bytes = _read_int(cg / "memory.max")
    if max_bytes is not None:
        out.max_mb = max_bytes / _MB
    try:
        for line in (cg / "memory.stat").read_text(encoding="utf-8").splitlines():
            key, _, val = line.partition(" ")
            if key == "anon":
                out.anon_mb = int(val) / _MB
            elif key == "file":
                out.file_mb = int(val) / _MB
    except (OSError, ValueError):
        pass
    try:
        for line in (cg / "memory.events").read_text(encoding="utf-8").splitlines():
            key, _, val = line.partition(" ")
            if key == "oom_kill":
                out.oom_kills = int(val)
    except (OSError, ValueError):
        pass
    return out


# --- Did the previous run of the bot die of memory? ---------------------------


def previous_run_was_oom_killed(unit: str = "claude-bot.service") -> str | None:
    """Return a description if the bot's last exit was an OOM kill, else None.

    Asked once at startup, on an unclean start only, so that an interrupted
    session can be told *why* it was interrupted. Before this, every case
    collapsed to "interrupted by bot restart", which is how a machine running
    out of memory twice in one night came to look like a bot bug and cost a
    full forensic session to identify.

    The journal is the only honest source. The cgroup's own ``oom_kill``
    counter cannot answer this: the cgroup is recreated on restart, so it
    always reads zero in the run that wants to know. Note this fires for the
    unit *itself* dying — with ``OOMPolicy=continue`` a killed descendant no
    longer stops the unit, so reaching this at all means the bot process was
    the victim, which is worth saying plainly.
    """
    if sys.platform != "linux":
        return None
    import shutil
    import subprocess

    if not shutil.which("systemctl") or not shutil.which("journalctl"):
        return None
    try:
        proc = subprocess.run(
            [
                "journalctl", "--user", "-u", unit,
                "--since", "-2h", "--no-pager", "-o", "cat",
            ],
            capture_output=True, text=True, timeout=20,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.debug("OOM history check failed: %s", exc)
        return None
    if proc.returncode != 0:
        return None
    return _verdict_from_journal(proc.stdout, unit)


def _verdict_from_journal(text: str, unit: str = "claude-bot.service") -> str | None:
    """Decide, from raw journal text, whether the *previous* run died of OOM.

    Split out from the command that fetches it so the decision can be checked
    against real recorded output instead of only against a live system.

    The window is bounded by start markers rather than scanned blindly
    backwards, and getting that wrong is easy: systemd emits its resource
    summary *after* the verdict, so the tail of a real OOM exit reads

        claude-bot.service: Failed with result 'oom-kill'.
        claude-bot.service: Consumed ... 13.9G memory peak, ...
        claude-bot.service: Scheduled restart job, restart counter is at 1.
        Started claude-bot.service - Claude Code Discord bot.

    A naive reverse scan that stops at the summary line therefore steps over
    the very verdict it is looking for. Instead: find our own start line, and
    look only between the start before it and it — that slice is exactly one
    previous run, so a clean exit in between cannot surface an older kill.
    """
    lines = text.splitlines()
    starts = [
        i for i, line in enumerate(lines)
        if line.startswith(f"Started {unit}") or line.startswith(f"Starting {unit}")
    ]
    if not starts:
        # No systemd start in the window — started by hand, or the unit is not
        # what is running. Nothing can be concluded about a previous exit.
        return None
    ours = starts[-1]
    # Walk back past our own "Starting"/"Started" pair to the previous run's.
    prev = 0
    for i in reversed(starts[:-1]):
        if i < ours - 1:
            prev = i
            break
    for line in lines[prev:ours]:
        if "Failed with result 'oom-kill'" in line:
            return "the machine ran out of memory and the kernel killed the bot"
    return None


# --- Startup marker: remember the reason across the restart --------------------
#
# The reason has to survive the process that discovered it. Startup detects the
# OOM and marks the orphaned sessions, but the text is also wanted later, by the
# Ark notice and by whatever resumes those sessions. A tiny file is enough and
# is deliberately not state.json: this is a fact about one boot, not durable
# state, and it must still be readable if state.json is what got corrupted.


def write_oom_marker(data_dir: Path, reason: str) -> None:
    """Record that this run started after an OOM kill. Best-effort."""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "last_oom.txt").write_text(
            f"{int(time.time())}\n{reason}\n", encoding="utf-8",
        )
    except OSError as exc:
        log.debug("Could not write OOM marker: %s", exc)


def read_oom_marker(data_dir: Path, max_age_secs: int = 3600) -> str | None:
    """Read a recent OOM marker, or None if absent, stale or unreadable."""
    path = data_dir / "last_oom.txt"
    try:
        stamp, _, reason = path.read_text(encoding="utf-8").partition("\n")
        if time.time() - int(stamp.strip()) > max_age_secs:
            return None
        return reason.strip() or None
    except (OSError, ValueError):
        return None


def clear_oom_marker(data_dir: Path) -> None:
    """Drop the marker once it has been reported. Best-effort."""
    try:
        os.unlink(data_dir / "last_oom.txt")
    except OSError:
        pass
