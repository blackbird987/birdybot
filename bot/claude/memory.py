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
    ``cgroup_memory`` is read alongside the tree and why the recovery nudge
    tells the agent to check for background jobs it left running.

    Nothing here ever waits on the *root*, and that restriction is load-bearing.
    The root is the CLI process, which asyncio spawned and therefore owns:
    ``psutil.wait_procs`` calls ``os.waitpid`` on any process that is a child of
    this one, so waiting on the root reaps its exit status out from under
    asyncio's child watcher, which then reports returncode 255 instead of the
    signal it was sent. That is not cosmetic — the runner's intentional-kill
    classifier requires a negative returncode, so a Kill or Steer that landed in
    the same moment as a reap came back as a red failure instead of the quiet
    stop the user asked for. The descendants are safe to wait on because they
    are not our children; ``waitpid`` refuses them and psutil falls back to
    polling ``/proc``.
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
    deadline = time.monotonic() + grace_secs
    # Root excluded from the wait (see docstring) — asyncio owns its reaping.
    descendants = [proc for proc in alive if proc.pid != pid]
    survivors = descendants
    if descendants:
        try:
            _, survivors = psutil.wait_procs(descendants, timeout=grace_secs)
        except Exception:
            survivors = descendants
    for proc in survivors:
        try:
            proc.kill()
            log.warning("SIGKILL for pid %s — ignored SIGTERM", proc.pid)
        except Exception:
            pass
    if any(proc.pid == pid for proc in alive):
        _escalate_root(root, pid, deadline)
    return signalled


def _escalate_root(root, pid: int, deadline: float) -> None:
    """SIGKILL the tree's root if it outlives ``deadline``, without reaping it.

    Liveness is polled from ``/proc`` rather than inferred from a wait, because
    a wait on this particular process is exactly what must not happen (see
    ``kill_tree``). A process that has exited but not yet been reaped reads as a
    zombie, which is the signal to stop: asyncio still owes it a ``waitpid`` and
    SIGKILLing it would be a no-op with a misleading log line attached.
    """
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return
    while True:
        try:
            if root.status() == psutil.STATUS_ZOMBIE:
                return  # already exited; asyncio will collect the status
        except psutil.NoSuchProcess:
            return  # already reaped
        except Exception:
            break  # can't tell — escalate rather than leave 13 GB standing
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    try:
        root.kill()
        log.warning("SIGKILL for root pid %s — ignored SIGTERM", pid)
    except psutil.NoSuchProcess:
        pass
    except Exception as exc:
        log.warning("Could not SIGKILL root pid %s: %s", pid, type(exc).__name__)


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
    # MemoryHigh: the throttle watermark, not a kill. Read alongside max
    # because it is the one that says "our own sessions are filling this" long
    # before anything dies. `memory.events` counts crossings in the millions
    # over an uptime, which is why the guard reads the current anon-vs-high
    # gap rather than that counter — but nothing in the bot had read either.
    high_mb: float | None = None
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
    high_bytes = _read_int(cg / "memory.high")
    if high_bytes is not None:
        out.high_mb = high_bytes / _MB
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


# --- Pressure: what the MACHINE has left, not just what we are allowed --------
#
# Everything above this line answers "how is the bot doing against its own
# limits". That question was answered correctly on 2026-08-21 and the machine
# died anyway. The kernel's own dump at 16:55:50 is the record: 57249 free
# pages — under 230 MB free on a 31 GB box — `Free swap = 68kB` with the whole
# 8 GB of zram gone, page cache squeezed to ~180 MB, and ~25 GB of anonymous
# memory nothing could reclaim. It ran a *global* OOM kill
# (`constraint=CONSTRAINT_NONE ... global_oom`) and shot a 5.44 GB Chrome.
# `memory.events` recorded oom_kill 0 for our unit: the bot was neither the
# victim nor, by its own accounting, the offender, and nothing in it had ever
# asked what was left OUTSIDE its cgroup before spawning more work.
#
# The available-memory rule below is the one that is *provably* early here:
# 230 MB is far under any sane floor, and it was true before the kill.
#
# So this section reads the machine, not the unit. Four signals, because no one
# of them is honest alone:
#
#   available  — what can be allocated without swapping. The headline number,
#                but it lags: it reads fine right up until it doesn't.
#   swap %     — swap here is zram, i.e. COMPRESSED RAM. Filling it does not
#                relieve pressure, it consumes the very thing that is short. A
#                full zram is a late signal but an unambiguous one.
#   PSI        — /proc/pressure/memory: the share of the last 10s that tasks
#                spent stalled waiting on memory. The only signal that reports
#                *thrashing* rather than *occupancy*, which is the difference
#                between a machine that is full and a machine that is dying.
#   cgroup anon vs MemoryHigh — our own contribution, so a message can say
#                whether we are the problem or a bystander.
#
# Nothing here raises and nothing here fails closed. A reading that cannot be
# taken is None, and a level computed from no readings at all is OK: a guard
# that refused to start sessions on any machine without /proc would be a much
# worse bug than the one it is preventing.

PRESSURE_OK = "ok"
PRESSURE_TIGHT = "tight"
PRESSURE_CRITICAL = "critical"

# Ordering used wherever two levels are compared; higher index is worse.
_PRESSURE_ORDER = (PRESSURE_OK, PRESSURE_TIGHT, PRESSURE_CRITICAL)


def pressure_at_least(level: str, floor: str) -> bool:
    """True when ``level`` is as bad as ``floor`` or worse."""
    try:
        return _PRESSURE_ORDER.index(level) >= _PRESSURE_ORDER.index(floor)
    except ValueError:
        return False


def swap_used_pct() -> float | None:
    """Machine-wide swap utilisation, 0-100, or None when it can't be read."""
    try:
        import psutil  # type: ignore[import-not-found]

        swap = psutil.swap_memory()
        if not swap.total:
            # No swap configured is not 100% full, and it is not an error
            # either — it is a machine that simply cannot swap.
            return 0.0
        return float(swap.percent)
    except Exception:
        return None


def psi_some_avg10() -> float | None:
    """Percent of the last 10s that *some* task stalled on memory, or None.

    Read straight from ``/proc/pressure/memory`` rather than through psutil,
    which does not expose PSI. Absent on non-Linux and on kernels built without
    ``CONFIG_PSI``, both of which read as None rather than as zero — "no
    thrashing" and "cannot tell" must not collapse into the same value.
    """
    if sys.platform != "linux":
        return None
    try:
        raw = Path("/proc/pressure/memory").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in raw.splitlines():
        if not line.startswith("some "):
            continue
        for field in line.split():
            key, _, val = field.partition("=")
            if key == "avg10":
                try:
                    return float(val)
                except ValueError:
                    return None
    return None


@dataclass
class MemoryPressure:
    """How close the whole machine is to being out, and why.

    ``level`` is the verdict; ``reasons`` is the evidence, in the words a
    message can use directly. Both matter: a hold that says "waiting for memory"
    is noise, one that says "1.2 GB free and swap is full" is a fact the user
    can act on.
    """

    avail_mb: float | None = None
    swap_pct: float | None = None
    psi_pct: float | None = None
    cgroup_anon_mb: float | None = None
    cgroup_high_mb: float | None = None
    level: str = PRESSURE_OK
    reasons: tuple[str, ...] = ()

    def is_critical(self) -> bool:
        return self.level == PRESSURE_CRITICAL

    def at_least(self, floor: str) -> bool:
        return pressure_at_least(self.level, floor)

    def over_own_high(self) -> bool:
        """True when the bot's own cgroup is past its MemoryHigh watermark.

        This is the "are we the problem" bit. Crossing MemoryHigh does not kill
        anything — the kernel throttles and reclaims — so on its own it is not a
        crisis. Combined with machine-wide pressure it is the difference between
        "our sessions did this" and "a browser did this to our sessions".
        """
        if self.cgroup_anon_mb is None or self.cgroup_high_mb is None:
            return False
        return self.cgroup_anon_mb >= self.cgroup_high_mb

    def summary(self) -> str:
        """One line for the log, e.g. ``critical: 0.6GB free, swap 99%, psi 61%``."""
        bits: list[str] = []
        if self.avail_mb is not None:
            bits.append(f"{self.avail_mb / 1024:.1f}GB free")
        if self.swap_pct is not None:
            bits.append(f"swap {self.swap_pct:.0f}%")
        if self.psi_pct is not None:
            bits.append(f"psi {self.psi_pct:.0f}%")
        if self.cgroup_anon_mb is not None:
            own = f"ours {self.cgroup_anon_mb / 1024:.1f}GB"
            if self.cgroup_high_mb is not None:
                own += f"/{self.cgroup_high_mb / 1024:.1f}GB"
            bits.append(own)
        return f"{self.level}: " + (", ".join(bits) or "no readings")

    def human(self) -> str:
        """The same facts as a sentence, for a message in a thread."""
        if not self.reasons:
            return "memory looks fine"
        return "; ".join(self.reasons)


def read_pressure(
    critical_avail_mb: float = 1024.0,
    tight_avail_mb: float = 2560.0,
    critical_swap_pct: float = 90.0,
    critical_psi_pct: float = 40.0,
    tight_psi_pct: float = 10.0,
) -> MemoryPressure:
    """Take every reading available and reduce them to one verdict.

    Thresholds are arguments rather than config reads so the harness can drive
    the classifier at fixed numbers instead of through the environment; the
    runner passes the configured values in.

    The rules, and why each one is where it is:

    * Available memory below ``critical_avail_mb`` is critical on its own.
      Nothing else needs to agree — there is no reading that makes 500 MB free
      acceptable.
    * PSI at or above ``critical_psi_pct`` is critical on its own. It is the
      only rule that can fire *before* the others: a machine can be thrashing
      hard while ``available`` still shows gigabytes, because the kernel counts
      reclaimable pages as available right up to the moment it cannot reclaim
      them fast enough. PSI was not recorded during the 2026-08-21 incident, so
      whether it would have fired first there is a guess — the available-memory
      rule provably would have, at under 230 MB free.
    * Full swap alone is only TIGHT. On zram a high figure is normal on a busy
      machine and says nothing about whether allocation will succeed — it takes
      a second signal (low available) to make it a crisis.
    """
    out = MemoryPressure()
    out.avail_mb = available_mb()
    out.swap_pct = swap_used_pct()
    out.psi_pct = psi_some_avg10()
    cg = cgroup_memory()
    out.cgroup_anon_mb = cg.anon_mb
    out.cgroup_high_mb = cg.high_mb

    reasons: list[str] = []
    level = PRESSURE_OK

    def escalate(to: str, why: str) -> None:
        """Record the evidence always; raise the verdict only upward."""
        nonlocal level
        if _PRESSURE_ORDER.index(to) > _PRESSURE_ORDER.index(level):
            level = to
        reasons.append(why)

    if out.avail_mb is not None and out.avail_mb < critical_avail_mb:
        escalate(
            PRESSURE_CRITICAL,
            f"only {out.avail_mb / 1024:.1f} GB of memory is free machine-wide",
        )
    elif out.avail_mb is not None and out.avail_mb < tight_avail_mb:
        escalate(
            PRESSURE_TIGHT,
            f"{out.avail_mb / 1024:.1f} GB of memory is free machine-wide",
        )

    if out.psi_pct is not None and out.psi_pct >= critical_psi_pct:
        escalate(
            PRESSURE_CRITICAL,
            f"the machine is thrashing on memory ({out.psi_pct:.0f}% stall)",
        )
    elif out.psi_pct is not None and out.psi_pct >= tight_psi_pct:
        escalate(
            PRESSURE_TIGHT,
            f"the machine is stalling on memory ({out.psi_pct:.0f}%)",
        )

    if out.swap_pct is not None and out.swap_pct >= critical_swap_pct:
        # Swap full plus low available is the pair that means "the next
        # allocation may not land". Either alone is survivable.
        if out.avail_mb is not None and out.avail_mb < tight_avail_mb:
            escalate(PRESSURE_CRITICAL, f"swap is {out.swap_pct:.0f}% full")
        else:
            escalate(PRESSURE_TIGHT, f"swap is {out.swap_pct:.0f}% full")

    out.level = level
    out.reasons = tuple(reasons)
    return out


# --- Memory nobody owns: what escaped the process trees but not the cgroup ----
#
# The per-session guard walks *downward* from each session's CLI process, so it
# can only ever see what is still a descendant. The kernel's task table from
# the 2026-08-21 OOM shows what that misses: the second-largest process on the
# entire machine was a `dotnet` at 4.10 GB, while the three `claude` sessions
# running at that moment held 0.30, 0.31 and 0.34 GB between them. Not one
# session was anywhere near its ceiling, and reaping every one of them would
# have freed under a gigabyte.
#
# Investigating on 2026-08-24 found the shape of it still there: a detached
# .NET Roslyn compiler server (`VBCSCompiler`) charged to the bot's cgroup and
# parented to PID 1. `dotnet build` starts it deliberately detached and
# deliberately leaves it running, so the next build is faster — which makes it
# structurally invisible to every guard here. No session can be blamed for it,
# and reaping any session does not free a byte of it.
#
# The cgroup is what makes this findable at all. A reparented process leaves
# every process tree we hold, but it never leaves the cgroup it was charged to.
# So: everything in our cgroup, minus everything reachable from the bot's own
# process tree, is memory nobody owns.
#
# Two things stop that definition from being dangerous. A job a session launched
# with `setsid nohup ... &` — which is exactly what the /watch feature tells
# sessions to do — is also reparented and also unowned, so armed watch pids are
# passed in and excluded by pid. And a build server that is genuinely mid-build
# looks identical to an idle one by parentage alone, so "reclaimable" also
# requires it to be doing nothing: a compiler server compiling burns CPU, one
# sitting on gigabytes of nothing does not.

# Long-lived build/language daemons that exist purely as caches. Killing one
# costs the next build a cold start and nothing else — they are designed to be
# restarted, which is what makes them safe to reap and is why nothing else gets
# reaped by name. Matched against the full command line.
RECLAIMABLE_DAEMONS: tuple[str, ...] = (
    "VBCSCompiler",       # Roslyn shared C# compiler server
    "MSBuild.dll",        # MSBuild worker node held open by node reuse
    "MSBuildTaskHost",
    "tsserver.js",        # TypeScript language server
    "typingsInstaller",
)


@dataclass
class OrphanProcess:
    """A process charged to the bot's cgroup that no live session owns."""

    pid: int
    name: str = ""
    cmdline: str = ""
    rss_mb: float = 0.0
    age_secs: float = 0.0
    cpu_pct: float = 0.0
    reclaimable: bool = False   # a known cache daemon, idle, safe to restart
    # Process start time, carried so the reap can prove it is still the same
    # process. The scan samples CPU for a third of a second per candidate and
    # the reap happens afterwards, so a daemon can exit and its pid be handed
    # to something else in between — and this is code that sends SIGKILL. The
    # /watch feature defends the identical hole with /proc/<pid>/stat field 22
    # (see Watch.pid_start); psutil exposes the same clock as create_time().
    create_time: float = 0.0

    def label(self) -> str:
        return f"{self.name or '?'} (pid {self.pid}, {self.rss_mb / 1024:.1f}GB)"


def cgroup_pids() -> set[int]:
    """Every pid charged to the bot's own cgroup. Empty set when unreadable."""
    cg = _own_cgroup_path()
    if cg is None:
        return set()
    try:
        raw = (cg / "cgroup.procs").read_text(encoding="utf-8")
    except OSError:
        return set()
    pids: set[int] = set()
    for line in raw.split():
        try:
            pids.add(int(line))
        except ValueError:
            continue
    return pids


def _owned_pids(root_pid: int) -> set[int]:
    """``root_pid`` and every live descendant of it.

    The bot spawns each session CLI itself, so every session and everything a
    session spawns is inside this set. That is the point: subtracting it from
    the cgroup leaves exactly the processes that have been reparented away.
    """
    owned = {root_pid}
    try:
        import psutil  # type: ignore[import-not-found]

        root = psutil.Process(root_pid)
        for child in root.children(recursive=True):
            owned.add(child.pid)
    except Exception:
        # A partial answer here is dangerous in the wrong direction: it would
        # make owned processes look orphaned. Signal it by returning a set the
        # caller can recognise as untrustworthy.
        return set()
    return owned


def find_orphans(
    root_pid: int | None = None,
    protected_pids: set[int] | None = None,
    min_rss_mb: float = 128.0,
    min_age_secs: float = 60.0,
    cpu_idle_pct: float = 5.0,
    daemon_patterns: tuple[str, ...] = RECLAIMABLE_DAEMONS,
) -> list[OrphanProcess]:
    """Processes in our cgroup that no live session owns, biggest first.

    Returns everything found above ``min_rss_mb`` — including the ones it will
    not touch — because the log line is half the value here. A multi-gigabyte
    process that nobody can account for is worth seeing in bot.log every time,
    whether or not anything is going to be done about it.

    ``reclaimable`` is the narrow subset that is safe to kill: a known cache
    daemon, old enough not to be mid-startup, idle enough not to be mid-build,
    and not a pid an armed watch is waiting on.
    """
    if root_pid is None:
        root_pid = os.getpid()
    protected = protected_pids or set()

    in_cgroup = cgroup_pids()
    if not in_cgroup:
        return []
    owned = _owned_pids(root_pid)
    if not owned:
        # _owned_pids could not enumerate the tree. Every session would look
        # orphaned, so report nothing rather than something wrong.
        log.debug("Orphan scan skipped — could not enumerate our own tree")
        return []

    candidates = in_cgroup - owned
    if not candidates:
        return []

    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return []

    now = time.time()
    found: list[OrphanProcess] = []
    for pid in candidates:
        try:
            proc = psutil.Process(pid)
            rss_mb = proc.memory_info().rss / _MB
            if rss_mb < min_rss_mb:
                continue
            cmdline = " ".join(proc.cmdline())
            item = OrphanProcess(
                pid=pid,
                name=proc.name(),
                cmdline=cmdline,
                rss_mb=rss_mb,
                age_secs=max(0.0, now - proc.create_time()),
                create_time=proc.create_time(),
            )
        except Exception:
            # Exited mid-walk, or not ours to inspect. Either way, skip.
            continue

        is_daemon = any(pat in item.cmdline for pat in daemon_patterns)
        if is_daemon and pid not in protected and item.age_secs >= min_age_secs:
            # Busy check last, because it is the only part that costs time.
            # A compiler server mid-compile burns CPU; one holding gigabytes
            # of nothing does not, and killing the first would fail a build
            # that is currently running.
            try:
                item.cpu_pct = proc.cpu_percent(interval=0.3)
            except Exception:
                item.cpu_pct = 100.0   # unreadable == assume busy == leave it
            item.reclaimable = item.cpu_pct < cpu_idle_pct
        found.append(item)

    found.sort(key=lambda o: o.rss_mb, reverse=True)
    return found


def reap_orphans(
    orphans: list[OrphanProcess],
    grace_secs: float = 3.0,
    cpu_idle_pct: float = 5.0,
) -> list[str]:
    """SIGTERM then SIGKILL every ``reclaimable`` entry. Returns what was reaped.

    Only ever called with the reclaimable subset in mind, but it filters again
    itself: this is a function that kills things, and a caller that forgot to
    filter should reap nothing rather than everything.

    Two things are re-proved per pid before any signal is sent, because the
    verdict this acts on was formed in ``find_orphans`` and the world moved on
    after it.

    *Identity.* A daemon can exit between the scan and this call and its pid be
    reissued to something else entirely — a session's CLI, one of its builds,
    or one of the user's own programs. Killing by a number alone is how that
    becomes a silent, unattributable failure somewhere else on the machine.

    *Idleness.* The scan samples CPU for a third of a second per candidate,
    serially, and the caller then logs and hops threads before getting here, so
    the first candidate's "idle" is already seconds old. That is the same order
    as a compile burst: a real VBCSCompiler was observed here going from 0% to
    840% and back inside two minutes, which is exactly long enough to be judged
    idle and then killed mid-build. Re-sampling costs 0.3s on a path that only
    runs when memory is already short, and it is the last honest moment to ask.
    """
    reaped: list[str] = []
    targets = [o for o in orphans if o.reclaimable]
    if not targets:
        return reaped
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return reaped

    procs = []
    for item in targets:
        try:
            proc = psutil.Process(item.pid)
            # Same pid is not the same process. A start time that has moved
            # means this number was reissued after the scan, so whatever holds
            # it now was never examined and must not be signalled.
            if item.create_time and abs(proc.create_time() - item.create_time) > 0.001:
                log.warning(
                    "Not reaping pid %d — it was reused since the scan "
                    "(expected %s, found %s)",
                    item.pid, item.name, proc.name(),
                )
                continue
            # Idle when it was scanned is not idle now. A build that started in
            # between would be destroyed by this signal.
            cpu_now = proc.cpu_percent(interval=0.3)
            if cpu_now >= cpu_idle_pct:
                log.info(
                    "Not reaping %s — it went busy since the scan (%.0f%% CPU); "
                    "a build is using it right now",
                    item.label(), cpu_now,
                )
                continue
            proc.terminate()
            procs.append(proc)
            reaped.append(item.label())
        except Exception:
            continue
    if not procs:
        return reaped
    try:
        _gone, alive = psutil.wait_procs(procs, timeout=grace_secs)
        for proc in alive:
            try:
                proc.kill()
            except Exception:
                pass
    except Exception:
        pass
    return reaped


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


_OOM_VERDICT = "Failed with result 'oom-kill'"

# How systemd reports that a *run* of the unit ended. Matched only on lines it
# prefixes with the unit name, because the bot's own stdout lands in this same
# journal and a log line reading "...: Succeeded" would otherwise be mistaken
# for the unit exiting cleanly.
_RUN_ENDED_MARKERS = (
    "Failed with result",
    "Deactivated successfully",
    "Succeeded",
)


def _verdict_from_journal(text: str, unit: str = "claude-bot.service") -> str | None:
    """Decide, from raw journal text, whether the *previous* run died of OOM.

    Split out from the command that fetches it so the decision can be checked
    against real recorded output instead of only against a live system.

    Two traps here, and both produce a silently wrong answer rather than an
    error — which matters more than usual, because a wrong answer here means the
    user is told "interrupted by bot restart" for the second night running.

    The first: systemd emits its resource summary *after* the verdict, so the
    tail of a real OOM exit reads

        claude-bot.service: Failed with result 'oom-kill'.
        claude-bot.service: Consumed ... 13.9G memory peak, ...
        claude-bot.service: Scheduled restart job, restart counter is at 1.
        Starting claude-bot.service - Claude Code Discord bot...
        Started claude-bot.service - Claude Code Discord bot.

    A reverse scan that treats that summary as the end of the previous run steps
    straight over the verdict it came to find.

    The second: a run that ended *cleanly* hours after an earlier OOM must not
    be blamed on it. So the scan walks back from our own start and stops at the
    first line where systemd said how a run ended, reporting only what that line
    says. Anything older belongs to a run before that one.

    Deliberately assumes nothing about our own startup lines beyond "none of
    them is a run-ended marker" — an earlier version bounded the window by
    assuming the unit's ``Starting`` and ``Started`` lines are adjacent, which
    ``ExecStartPre`` output is enough to break, and breaking it meant reading a
    real OOM as a clean restart.
    """
    lines = text.splitlines()
    last_start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith(f"Started {unit}") or line.startswith(f"Starting {unit}"):
            last_start = i
    if last_start is None:
        # No systemd start in the window — started by hand, or the unit is not
        # what is running. Nothing can be concluded about a previous exit.
        return None
    for line in reversed(lines[:last_start]):
        if not line.startswith(f"{unit}: "):
            continue
        if _OOM_VERDICT in line:
            return "the machine ran out of memory and the kernel killed the bot"
        if any(marker in line for marker in _RUN_ENDED_MARKERS):
            # The previous run ended, and not this way. Note this is also what
            # correctly reports "no" when a *descendant* was OOM-killed under
            # OOMPolicy=continue: the unit survived that, so its own result is
            # something else, and the bot was not the victim.
            return None
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
