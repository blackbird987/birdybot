#!/usr/bin/env python3
"""Regression test: a session that eats the machine's RAM is reaped by the bot,
instead of the kernel reaping the bot.

The incident (2026-08-17, twice — 01:09 and 02:34): a build session in the
Ev-nova-remake repo was running diffusion upscale experiments. One of its
Python subprocesses reached ~13.7 GB resident on a 31 GB box whose 8 GB of
zram swap was already full, so the kernel's global OOM killer fired. It picked
the right victim — the experiment, not the ~220 MB bot — but every process a
session spawns lives inside the bot's systemd cgroup, and the unit's default
``OOMPolicy=stop`` meant systemd then tore down the whole unit. The bot and
every other session died with it. It happened a second time ninety minutes
later because the restart resumed the same session, which re-ran the same job.

Two things had to change, and both are asserted here.

  * The unit must survive a descendant being killed (``OOMPolicy=continue``)
    and must have a ceiling at all (``MemoryMax``), so one runaway cannot take
    the machine down to where the *global* killer has to choose.
  * The bot must notice and reap first, below that ceiling, and then tell the
    resumed session what the ceiling is — because a silent re-run is exactly
    what turned one OOM into two.

Asserted here:

  * a real three-level process tree is measured through its *descendants*: the
    grandchild holding the memory is found and named as the offender. This is
    the specific blindness that made the incident hard to see — the existing
    stall diagnostics reported "336MB" for this session, because they measured
    only the CLI supervisor and never what it had spawned
  * ``kill_tree`` reaps that grandchild, and the control case shows why it has
    to: signalling only the root leaves the runaway alive, holding all of it
  * descendants are signalled before the root, so the runaway is never
    orphaned out of the tree it was about to be reaped from
  * the journal verdict is read correctly off the *real* recorded output of
    both kills, including the trap that systemd logs its ``memory peak``
    summary line *after* the ``oom-kill`` verdict
  * a clean previous exit is not reported as an OOM just because an older run
    in the same window died of one
  * the thresholds are ordered warn < kill < the unit's own ``MemoryMax``, so
    the bot always acts before systemd does
  * the note handed to the resumed session states the numbers, says a
    re-run unchanged will die again, and warns that ``&``/``nohup`` jobs count
    against the same ceiling — the failure mode that started this
  * a reaped run auto-resumes exactly once (``MEMORY_KILL_MAX_RETRIES``),
    prefixes the note onto the resumed prompt, carries ``--resume``, tells the
    user, and gives up rather than looping
  * an ordinary build failure never enters this path

The process-tree cases spawn real processes and allocate real memory (~220 MB,
briefly). Everything else is pure.

Run: ``python scripts/test_memory_guard.py``  (exit 0 on pass).
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import copy
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Same reasoning as test_context_thrash: importing bot.config runs a real
# path-map init that would drop a root marker in whoever's home this runs
# under. Nothing here depends on the map.
os.environ.setdefault("BOT_PATHS_DISABLED", "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[1]

from bot import config
from bot.claude import memory
from bot.claude import runner as runner_mod
from bot.claude.runner import ClaudeRunner
from bot.claude.types import Instance, InstanceStatus, InstanceType, RunResult

# How much the planted runaway allocates. Big enough to stand out from any
# interpreter's baseline, small enough to be rude to nobody.
HOG_MB = 220

# Verbatim from `journalctl --user -u claude-bot.service -o cat`, the 01:09:56
# kill. Kept exactly as recorded, noise lines included: the "Stopping at
# filesystem boundary" line is git complaining from inside a child process, and
# it is here on purpose because it starts with "Stopping" and a start-marker
# match that is even slightly loose will trip over it.
JOURNAL_OOM = """\
2026-08-17 01:09:11 INFO    bot.claude.runner: Streaming output for t-7376
claude-bot.service: A process of this unit has been killed by the OOM killer.
claude-bot.service: The kernel OOM killer killed some processes in this unit.
Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set).
claude-bot.service: Failed with result 'oom-kill'.
claude-bot.service: Consumed 3h 5min 25.371s CPU time over 5h 26min 6.303s \
wall clock time, 13.9G memory peak, 96.2M memory swap peak.
claude-bot.service: Scheduled restart job, restart counter is at 1.
Starting claude-bot.service - Claude Code Discord bot...
Started claude-bot.service - Claude Code Discord bot.
2026-08-17 01:10:40 INFO    bot.app: Starting Claude Bot...
"""

# The same shape, but the run that just ended stopped cleanly. An older run in
# the window did die of OOM — reporting that one would blame a restart the user
# performed themselves on a kill from hours earlier.
JOURNAL_CLEAN_AFTER_OOM = """\
claude-bot.service: Failed with result 'oom-kill'.
claude-bot.service: Consumed 1min CPU time, 13.8G memory peak.
claude-bot.service: Scheduled restart job, restart counter is at 2.
Starting claude-bot.service - Claude Code Discord bot...
Started claude-bot.service - Claude Code Discord bot.
2026-08-17 02:35:01 INFO    bot.app: Starting Claude Bot...
2026-08-17 09:12:00 INFO    bot.app: Reboot requested, shutting down
Stopping claude-bot.service - Claude Code Discord bot...
claude-bot.service: Deactivated successfully.
claude-bot.service: Consumed 4min CPU time, 402.1M memory peak.
Stopped claude-bot.service - Claude Code Discord bot.
Starting claude-bot.service - Claude Code Discord bot...
Started claude-bot.service - Claude Code Discord bot.
2026-08-17 09:12:20 INFO    bot.app: Starting Claude Bot...
"""

# First boot after the machine came up: nothing to say about a previous run.
JOURNAL_FIRST_BOOT = """\
Starting claude-bot.service - Claude Code Discord bot...
Started claude-bot.service - Claude Code Discord bot.
2026-08-17 09:30:00 INFO    bot.app: Starting Claude Bot...
"""


# --- Part 1: measuring and reaping a real process tree ------------------------
#
# The runaway is planted two levels down, under a shell, because that is the
# shape the incident had: the bot spawns the Claude CLI, the CLI runs a tool
# through a shell, and the shell runs the thing that allocates. Every level
# matters -- a walk that stops at the child finds nothing.


def _plant_tree(hold_secs: int = 60) -> subprocess.Popen:
    """A shell whose child python holds HOG_MB and does nothing else."""
    hog = (
        f"import time; buf = bytearray({HOG_MB} * 1024 * 1024);\n"
        # Touch every page: an untouched bytearray on Linux may not be resident
        # yet, and RSS is what both the guard and the kernel actually count.
        f"[buf.__setitem__(i, 1) for i in range(0, len(buf), 4096)];\n"
        f"time.sleep({hold_secs})"
    )
    return subprocess.Popen(
        # The trailing `exit $?` is load-bearing: given a single command, dash
        # exec()s it and replaces itself, collapsing the tree to one process
        # and quietly turning this into a test of nothing.
        ["/bin/sh", "-c", '"$1" -c "$2"; exit $?', "sh", sys.executable, hog],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_for_tree(pid: int, want_mb: float, timeout: float = 30.0) -> memory.TreeMemory:
    """Poll until the planted allocation is resident, or give up."""
    deadline = time.monotonic() + timeout
    sample = memory.sample_tree(pid)
    while time.monotonic() < deadline and sample.total_mb < want_mb:
        time.sleep(0.25)
        sample = memory.sample_tree(pid)
    return sample


def _check_tree_measurement(failures: list[str]) -> None:
    import psutil

    proc = _plant_tree()
    try:
        sample = _wait_for_tree(proc.pid, HOG_MB * 0.8)

        if sample.total_mb < HOG_MB * 0.8:
            failures.append(
                f"a {HOG_MB}MB runaway two levels down measured as only "
                f"{sample.total_mb:.0f}MB ({sample.proc_count} procs) — this is "
                "the 336MB blindness the incident was invisible behind"
            )
        if sample.proc_count < 2:
            failures.append(
                f"tree walk saw {sample.proc_count} process(es); the planted "
                "tree is a shell plus a python, so descendants were missed"
            )
        if sample.error:
            failures.append(f"tree walk reported an error: {sample.error!r}")

        # The root shell holds almost nothing. If the guard measured only the
        # root -- which is what the old diagnostics did -- it would see this.
        try:
            root_only = psutil.Process(proc.pid).memory_info().rss / (1024 * 1024)
        except psutil.Error as exc:
            failures.append(f"could not read the root's own RSS: {exc}")
            root_only = 0.0
        if root_only > HOG_MB * 0.5:
            failures.append(
                "the planted tree holds its memory in the root, so this case "
                "cannot show that descendants are counted"
            )

        # The offender has to be nameable: the message the user reads says
        # which process it was, and "python" is the whole point.
        if sample.biggest_mb < HOG_MB * 0.8:
            failures.append(
                f"biggest process reported as {sample.biggest_mb:.0f}MB, but the "
                f"runaway holds ~{HOG_MB}MB — the offender was misidentified"
            )
        if sample.biggest_pid == proc.pid:
            failures.append(
                "the root shell was named as the biggest process; the "
                "grandchild holding the memory should be"
            )
        offender = sample.offender()
        if not offender or str(sample.biggest_pid) not in offender:
            failures.append(f"offender() is not usable text: {offender!r}")
        summary = sample.summary()
        if "GB" not in summary and "MB" not in summary:
            failures.append(f"summary() carries no size: {summary!r}")
    finally:
        _hard_kill(proc)


def _hard_kill(proc: subprocess.Popen) -> None:
    """Leave nothing behind, whatever the test did or didn't get to."""
    import psutil

    try:
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
    except psutil.Error:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _check_tree_reap(failures: list[str]) -> None:
    """kill_tree takes the whole tree; killing the root alone does not."""
    import psutil

    # Control: signal only the root, the way a plain proc.terminate() would.
    proc = _plant_tree()
    orphan_pid = None
    try:
        _wait_for_tree(proc.pid, HOG_MB * 0.8)
        try:
            kids = psutil.Process(proc.pid).children(recursive=True)
            orphan_pid = kids[-1].pid if kids else None
        except psutil.Error:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.5)
        if orphan_pid is None:
            failures.append("control case could not find the grandchild to watch")
        elif not psutil.pid_exists(orphan_pid):
            failures.append(
                "signalling only the root also took the grandchild down, so "
                "this case cannot show why kill_tree walks the tree"
            )
    finally:
        _hard_kill(proc)
        if orphan_pid is not None:
            try:
                psutil.Process(orphan_pid).kill()
            except psutil.Error:
                pass

    # The real thing.
    proc = _plant_tree()
    try:
        _wait_for_tree(proc.pid, HOG_MB * 0.8)
        try:
            watched = [c.pid for c in psutil.Process(proc.pid).children(recursive=True)]
        except psutil.Error:
            watched = []
        if not watched:
            failures.append("kill_tree case could not find any descendant to watch")

        signalled = memory.kill_tree(proc.pid, grace_secs=2.0)
        if not signalled:
            failures.append("kill_tree reported signalling nothing at all")
        # Every signalled name should be recognisable text, not a repr.
        if any(not isinstance(s, str) or not s for s in signalled):
            failures.append(f"kill_tree returned unusable labels: {signalled!r}")

        time.sleep(0.5)
        alive = [pid for pid in watched if psutil.pid_exists(pid)]
        # A zombie still "exists" until reaped; the parent is gone, so check it
        # is not actually running.
        still_running = []
        for pid in alive:
            try:
                if psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                    still_running.append(pid)
            except psutil.Error:
                pass
        if still_running:
            failures.append(
                f"kill_tree left the runaway alive (pids {still_running}) — it "
                "would keep its memory and drop out of every later tree walk"
            )
        if psutil.pid_exists(proc.pid):
            try:
                if psutil.Process(proc.pid).status() != psutil.STATUS_ZOMBIE:
                    failures.append("kill_tree left the root process running")
            except psutil.Error:
                pass
    finally:
        _hard_kill(proc)


def _check_descendants_signalled_first(failures: list[str]) -> None:
    """Order matters: the root must not be signalled before its children.

    Kill the CLI first and the runaway is reparented to init — still holding
    every byte, and no longer a descendant of anything the guard walks. It
    would then be invisible to the check that was about to reap it.
    """
    import types

    order: list[str] = []

    class _NoSuchProcess(Exception):
        pass

    class _Recorder:
        """Stands in for one process; records the order it is signalled in."""

        def __init__(self, pid, label):
            self.pid = pid
            self.label = label

        def name(self):
            return self.label

        def children(self, recursive=False):
            # Root has two levels below it, so "signal descendants first" has
            # to mean deepest-first, not merely "before the root".
            if self.label != "root":
                return []
            return [_Recorder(2, "child"), _Recorder(3, "grandchild")]

        def memory_info(self):
            return types.SimpleNamespace(rss=1024 * 1024)

        def terminate(self):
            order.append(self.label)

        def kill(self):
            order.append(self.label + ":kill")

    fake = types.ModuleType("psutil")
    fake.Process = lambda pid: _Recorder(pid, "root")  # type: ignore[attr-defined]
    fake.NoSuchProcess = _NoSuchProcess  # type: ignore[attr-defined]
    fake.AccessDenied = _NoSuchProcess  # type: ignore[attr-defined]
    fake.Error = _NoSuchProcess  # type: ignore[attr-defined]
    # Everything died on SIGTERM, so no SIGKILL round is expected.
    fake.wait_procs = lambda procs, timeout=None: (list(procs), [])  # type: ignore[attr-defined]

    saved = sys.modules.get("psutil")
    sys.modules["psutil"] = fake
    try:
        memory.kill_tree(1, grace_secs=0.0)
    except Exception as exc:
        failures.append(f"kill_tree raised against a stubbed tree: {exc!r}")
    finally:
        if saved is not None:
            sys.modules["psutil"] = saved
        else:  # pragma: no cover -- psutil is a hard dependency here
            del sys.modules["psutil"]

    terminated = [o for o in order if not o.endswith(":kill")]
    if terminated and terminated[-1] != "root":
        failures.append(
            "kill_tree signalled the root before its descendants "
            f"(order: {order}) — that orphans the runaway instead of reaping it"
        )
    if set(terminated) != {"root", "child", "grandchild"}:
        failures.append(
            f"kill_tree did not signal the whole tree, only {terminated}"
        )


# --- Part 2: reading the previous run's verdict off the journal ---------------


def _check_journal_verdict(failures: list[str]) -> None:
    verdict = memory._verdict_from_journal(JOURNAL_OOM)
    if not verdict:
        failures.append(
            "the real recorded journal of the 01:09 OOM kill was read as "
            "'not an OOM' — the sessions it interrupted would still be told "
            "only 'interrupted by bot restart'"
        )
    elif "memory" not in verdict.lower():
        failures.append(f"OOM verdict text does not mention memory: {verdict!r}")

    if memory._verdict_from_journal(JOURNAL_CLEAN_AFTER_OOM) is not None:
        failures.append(
            "a clean restart was blamed on an OOM kill from an earlier run in "
            "the same window"
        )
    if memory._verdict_from_journal(JOURNAL_FIRST_BOOT) is not None:
        failures.append("a first boot was reported as following an OOM kill")
    if memory._verdict_from_journal("") is not None:
        failures.append("empty journal output produced a verdict")
    # No systemd start line at all: started by hand, nothing can be concluded.
    if memory._verdict_from_journal(
        "claude-bot.service: Failed with result 'oom-kill'.\n"
    ) is not None:
        failures.append(
            "a verdict was returned with no start marker in the window, so it "
            "cannot know which run that kill belonged to"
        )

    # And against the live system, which must at least not throw or hang.
    try:
        memory.previous_run_was_oom_killed()
    except Exception as exc:
        failures.append(f"previous_run_was_oom_killed raised: {exc!r}")


def _check_marker_roundtrip(failures: list[str]) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="oom-marker-"))
    try:
        reason = "the machine ran out of memory and the kernel killed the bot"
        memory.write_oom_marker(tmp, reason)
        got = memory.read_oom_marker(tmp)
        if got != reason:
            failures.append(f"OOM marker did not survive a round trip: {got!r}")
        # Stale markers must not re-announce an old outage on every restart.
        if memory.read_oom_marker(tmp, max_age_secs=0) is not None:
            failures.append("an expired OOM marker was still reported as news")
        memory.clear_oom_marker(tmp)
        if memory.read_oom_marker(tmp) is not None:
            failures.append("clear_oom_marker left the marker readable")
        # A missing marker is the common case and must be quiet.
        if memory.read_oom_marker(Path(tmp) / "nope") is not None:
            failures.append("a missing marker produced a reason")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- Part 3: the ceiling, and where the bot's own limits sit relative to it ---


def _unit_settings() -> dict[str, str]:
    text = (REPO_ROOT / "scripts" / "claude-bot.service").read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


def _as_mb(value: str) -> float | None:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?)", value.strip())
    if not m:
        return None
    scale = {"": 1 / (1024 * 1024), "K": 1 / 1024, "M": 1.0,
             "G": 1024.0, "T": 1024.0 * 1024}[m.group(2)]
    return float(m.group(1)) * scale


def _check_unit_file(failures: list[str]) -> None:
    settings = _unit_settings()

    if settings.get("OOMPolicy") != "continue":
        failures.append(
            "the unit does not set OOMPolicy=continue, so a descendant being "
            "OOM-killed still takes the whole bot down — which is exactly what "
            "happened twice on 2026-08-17"
        )
    if settings.get("Restart") not in ("always", "on-failure"):
        failures.append(
            f"the unit's Restart= is {settings.get('Restart')!r}; the bot has "
            "to come back on its own after a kill"
        )

    max_mb = _as_mb(settings.get("MemoryMax", ""))
    high_mb = _as_mb(settings.get("MemoryHigh", ""))
    if max_mb is None:
        failures.append(
            "the unit has no MemoryMax, so one runaway can still push the "
            "machine to where the *global* OOM killer has to choose a victim"
        )
    if high_mb is None:
        failures.append("the unit has no MemoryHigh to throttle against")
    if max_mb is not None and high_mb is not None and not high_mb < max_mb:
        failures.append(
            f"MemoryHigh ({high_mb:.0f}MB) is not below MemoryMax "
            f"({max_mb:.0f}MB), so the throttle never applies before the cap"
        )

    # The ordering that matters: the bot reaps one session before systemd caps
    # the whole unit, so the blast radius is one job rather than everything.
    if not config.SESSION_MEM_WARN_MB < config.SESSION_MEM_KILL_MB:
        failures.append(
            f"warn ({config.SESSION_MEM_WARN_MB}MB) is not below kill "
            f"({config.SESSION_MEM_KILL_MB}MB), so the warning is useless"
        )
    if max_mb is not None and not config.SESSION_MEM_KILL_MB < max_mb:
        failures.append(
            f"the bot's own kill threshold ({config.SESSION_MEM_KILL_MB}MB) is "
            f"not below the unit's MemoryMax ({max_mb:.0f}MB) — systemd would "
            "cap the unit before the bot ever reaped the one session at fault"
        )
    if config.SESSION_MEM_CHECK_SECS <= 0 or config.SESSION_MEM_CHECK_SECS > 120:
        failures.append(
            f"memory is sampled every {config.SESSION_MEM_CHECK_SECS}s; that is "
            "either never or constantly"
        )


def _check_runtime_readings(failures: list[str]) -> None:
    """The two readings the guard's decisions and messages are built from."""
    avail = memory.available_mb()
    if avail is not None and avail <= 0:
        failures.append(f"available_mb() reported {avail}MB of usable memory")
    if sys.platform == "linux" and avail is None:
        failures.append("available_mb() returned nothing on Linux")

    cg = memory.cgroup_memory()
    if cg is None:
        if sys.platform == "linux":
            failures.append("cgroup_memory() returned nothing on Linux")
        return
    if cg.anon_mb < 0 or cg.file_mb < 0:
        failures.append(f"cgroup memory read as negative: {cg}")
    # Headroom is measured against anon only: page cache crossing the limit
    # triggers reclaim, not a kill, so counting it would cry wolf constantly.
    if cg.max_mb:
        headroom = cg.headroom_mb()
        if headroom is None:
            failures.append("a cgroup with a limit reported no headroom figure")
        elif headroom > cg.max_mb:
            failures.append(
                f"headroom ({headroom:.0f}MB) exceeds the limit itself "
                f"({cg.max_mb:.0f}MB) — file cache is being counted as anon"
            )


def _check_nudge_text(failures: list[str]) -> None:
    note = config.MEMORY_KILL_NUDGE_TEMPLATE.format(
        peak_gb="13.7", limit_gb="12.0",
        offender="python (pid 282313)", avail_gb="1.2",
    )
    if "{" in note or "}" in note:
        failures.append(f"nudge template left an unfilled placeholder: {note!r}")
    for needed, why in (
        ("13.7", "the peak it actually reached"),
        ("12.0", "the ceiling it has to fit under"),
        ("282313", "which process was at fault"),
    ):
        if needed not in note:
            failures.append(f"the note handed to the session omits {why}")
    low = note.lower()
    if "again" not in low:
        failures.append(
            "the note does not warn that re-running unchanged will be killed "
            "again — the agent's default move is to retry verbatim"
        )
    if "nohup" not in low and "background" not in low:
        failures.append(
            "the note does not mention that background jobs count against the "
            "same ceiling, which is how the original runaway outlived its turn"
        )
    if "git status" not in low and "not lost" not in low:
        failures.append(
            "the note does not tell the session its edits are still on disk, "
            "so it may redo work it already finished"
        )


# --- Part 4: the reaped run resumes itself, once ------------------------------


class _FakeStdin:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def write(self, data):
        self._sink.append(
            data.decode("utf-8") if isinstance(data, bytes) else str(data)
        )
        return None

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None


class _FakeProc:
    _next_pid = 94001

    def __init__(self, sink: list[str]):
        self.pid = _FakeProc._next_pid
        _FakeProc._next_pid += 1
        self.returncode = 0
        self.stdin = _FakeStdin(sink)
        self.stdout = None
        self.stderr = None

    def kill(self):
        return None

    async def wait(self):
        return 0


INSTANCE_ID = "t-memhog"
BORN_SESSION = "9c31f0de-4a55-4a1e-9d0b-2f7a51c6b8e3"


class _Harness:
    """One scripted run of the real recovery cascade, stubbed at the subprocess
    boundary only — so the branch under test is the shipped one."""

    def __init__(self, tmp: str, outcomes: list[RunResult]):
        self.account = os.path.join(tmp, "acct_primary")
        self.repo = os.path.join(tmp, "repo")
        for p in (self.account, self.repo):
            os.makedirs(p, exist_ok=True)
        self.outcomes = outcomes
        self.spawn_argvs: list[list[str]] = []
        self.prompts: list[str] = []
        self.progress: list[tuple[str, str]] = []

    async def run(self, *, session_id: str | None = None) -> tuple[RunResult, Instance]:
        saved_accounts = list(config.CLAUDE_ACCOUNTS)
        saved_spawn = asyncio.create_subprocess_exec
        saved_unusable = runner_mod.unusable_reason
        config.CLAUDE_ACCOUNTS[:] = [self.account]
        runner_mod.unusable_reason = lambda acct: None  # type: ignore[assignment]

        async def fake_spawn(*args, **kwargs):
            self.spawn_argvs.append(list(args))
            return _FakeProc(self.prompts)

        asyncio.create_subprocess_exec = fake_spawn  # type: ignore[assignment]

        runner = ClaudeRunner()
        calls = {"n": 0}

        async def fake_stream_output(proc, instance, on_progress, on_stall, **kw):
            i = calls["n"]
            calls["n"] += 1
            # Past the script, keep returning the last outcome: a repeat
            # offender has to be stopped by the retry cap, not by the script
            # running out of answers.
            return copy.deepcopy(self.outcomes[min(i, len(self.outcomes) - 1)])

        runner._stream_output = fake_stream_output  # type: ignore[assignment]

        async def on_progress(headline, detail=""):
            self.progress.append((headline, detail))

        instance = Instance(
            id=INSTANCE_ID,
            name=None,
            instance_type=InstanceType.TASK,
            prompt="Upscale every sprite sheet to 4x and compare the tilings.",
            repo_name="Ev-nova-remake",
            repo_path=self.repo,
            status=InstanceStatus.RUNNING,
            session_id=session_id,
            mode="build",
        )
        runner._active_tasks.add(instance.id)
        try:
            result = await runner.run(instance, on_progress=on_progress)
        finally:
            runner._active_tasks.discard(instance.id)
            asyncio.create_subprocess_exec = saved_spawn  # type: ignore[assignment]
            runner_mod.unusable_reason = saved_unusable  # type: ignore[assignment]
            config.CLAUDE_ACCOUNTS[:] = saved_accounts
        return result, instance

    def resume_ids(self) -> list[str | None]:
        out: list[str | None] = []
        for argv in self.spawn_argvs:
            out.append(argv[argv.index("--resume") + 1] if "--resume" in argv else None)
        return out


def _reap_result() -> RunResult:
    """What the runner produces after it reaps a session for memory."""
    return RunResult(
        is_error=True,
        error_message=(
            "Stopped: this session's processes reached 13.7 GB of memory, over "
            "the 12.0 GB ceiling for one session."
        ),
        session_id=BORN_SESSION,
        num_turns=31,
        memory_kill_note=config.MEMORY_KILL_NUDGE_TEMPLATE.format(
            peak_gb="13.7", limit_gb="12.0",
            offender="python (pid 282313)", avail_gb="1.2",
        ),
    )


def _success() -> RunResult:
    return RunResult(
        is_error=False,
        result_text="Reduced the tile size to 96 and processed the sheets in batches of 4.",
        session_id=BORN_SESSION,
        num_turns=12,
    )


async def _check_auto_resume(failures: list[str]) -> None:
    tmp = tempfile.mkdtemp(prefix="memguard-")
    try:
        # A reap on attempt 1, then the session fits the job into the ceiling.
        h = _Harness(tmp, [_reap_result(), _success()])
        result, instance = await h.run(session_id=None)

        if result.is_error:
            failures.append(
                "a reaped run was not auto-resumed; it dead-ended on a Retry "
                f"button instead: {result.error_message!r}"
            )
        if len(h.spawn_argvs) != 2:
            failures.append(
                f"expected 2 attempts (reap then resume), got {len(h.spawn_argvs)}"
            )
        if h.resume_ids()[1:2] != [BORN_SESSION]:
            failures.append(
                "the resumed attempt did not carry --resume with the session "
                f"the reaped process created: {h.resume_ids()}"
            )
        if len(h.prompts) >= 2:
            first, second = h.prompts[0], h.prompts[1]
            if "13.7" in first:
                failures.append("the first attempt was told about a kill that hadn't happened")
            if "13.7" not in second or "12.0" not in second:
                failures.append(
                    "the resumed attempt's prompt does not carry the memory "
                    "note, so it will size the job exactly the same way again"
                )
            if instance.prompt not in second:
                failures.append("the resumed prompt lost the original task text")
        else:
            failures.append(f"only {len(h.prompts)} prompt(s) were written")
        if not any("memory" in head.lower() for head, _ in h.progress):
            failures.append(
                f"the user was never told why it restarted: {h.progress}"
            )
        if getattr(instance, "_memory_kill_note", None):
            failures.append(
                "the memory note was left on the instance after the resume; a "
                "cooldown re-queue hours later would open with stale news"
            )

        # A session that keeps hitting the wall must stop, not loop.
        h2 = _Harness(tmp, [_reap_result()])
        result2, _ = await h2.run(session_id=BORN_SESSION)
        expected = config.MEMORY_KILL_MAX_RETRIES + 1
        if len(h2.spawn_argvs) != expected:
            failures.append(
                f"a repeat offender ran {len(h2.spawn_argvs)} times; "
                f"MEMORY_KILL_MAX_RETRIES={config.MEMORY_KILL_MAX_RETRIES} "
                f"allows {expected}"
            )
        if not result2.is_error:
            failures.append("a run that never fit the ceiling was reported as success")
        if result2.is_error and "memory" not in (result2.error_message or "").lower():
            failures.append(
                f"the surfaced error hides the real cause: {result2.error_message!r}"
            )
        if result2.killed_intentionally:
            failures.append(
                "a memory reap was reported as a user-requested stop; nobody "
                "asked for it and it is a real failure"
            )

        # An ordinary failure must not touch any of this.
        h3 = _Harness(tmp, [RunResult(
            is_error=True, error_message="error: pathspec 'main' did not match",
            session_id=BORN_SESSION,
        )])
        result3, _ = await h3.run(session_id=BORN_SESSION)
        if len(h3.spawn_argvs) != 1:
            failures.append(
                f"an ordinary build failure was retried as a memory reap "
                f"({len(h3.spawn_argvs)} attempts)"
            )
        if not result3.is_error:
            failures.append("an ordinary build failure was swallowed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _amain() -> int:
    failures: list[str] = []

    _check_tree_measurement(failures)
    _check_tree_reap(failures)
    _check_descendants_signalled_first(failures)
    _check_journal_verdict(failures)
    _check_marker_roundtrip(failures)
    _check_unit_file(failures)
    _check_runtime_readings(failures)
    _check_nudge_text(failures)
    await _check_auto_resume(failures)

    if failures:
        print("FAIL: session memory guard")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASS: a runaway session is measured through its descendants, reaped")
    print(f"      whole at {config.SESSION_MEM_KILL_MB}MB (below the unit's own cap), and resumed")
    print(f"      once ({config.MEMORY_KILL_MAX_RETRIES}x) knowing the ceiling. The unit survives a")
    print("      descendant being OOM-killed, and an OOM restart says so.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
