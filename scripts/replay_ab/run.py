"""Run replay sessions: one CLI process per (case, arm, replicate).

Each run is the command line ``prompt.build`` produced from the bot's own
``build_command``, plus two flags the bot does not pass:

  --fork-session    the continuation is written under a new session id, so
                    the forked transcript the run started from stays intact
                    and a second replicate could start from it again
  --max-budget-usd  a per-run ceiling on notional spend

and it runs wrapped three times over:

  systemd-run --user --scope in app-claudesessions.slice, with the same
      MemoryHigh/MemoryMax a bot session gets, so oomd sees a replay as a
      session-sized victim and never as the bot
  nice -n 10, as the runner's _lower_priority does for every session
  bwrap with the live checkout and ~/.config read-only and the live .env
      masked, so a replay that wanders out of its clone cannot edit the bot,
      its state or its worktrees, and cannot read its Discord token

At most ``concurrency`` runs at a time (default 2), each one held back while
the sessions slice is short of room, and nothing new starts once the
recorded spend reaches the total cap.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from common import LIVE_ROOT, write_json

SLICE = "app-claudesessions.slice"
MEM_HIGH = "8G"
MEM_MAX = "10G"
NICE = 10

# Session identity of whatever launched the harness. A nested CLI that
# inherits these believes it is a child of this session.
STRIP_ENV = (
    "CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_PID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
    "ANTHROPIC_API_KEY", "NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID",
)
STRIP_PREFIXES = ("DISCORD_", "TEST_WEBHOOK", "TEST_LOBBY")


@dataclass
class Job:
    label: str                 # "<case>/<arm>/r<k>"
    workdir: Path              # replicate directory (stream, logs, meta)
    repo: Path                 # sandbox clone, the session's cwd
    account: str
    cmd: list[str]             # from prompt.build
    prompt: str
    budget_usd: float
    timeout_s: int
    result: dict = field(default_factory=dict)


def finalize_command(cmd: list[str], *, budget_usd: float) -> list[str]:
    """The bot's command line, plus --fork-session on a resume and a spend cap."""
    out = list(cmd)
    if "--resume" in out and "--fork-session" not in out:
        out.insert(out.index("--resume") + 2, "--fork-session")
    if "--max-budget-usd" not in out:
        out.extend(["--max-budget-usd", f"{budget_usd:.2f}"])
    return out


def child_env(account: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in STRIP_ENV and not k.startswith(STRIP_PREFIXES)}
    env["CLAUDE_CONFIG_DIR"] = account
    return env


def sandbox_args(repo: Path, live_root: Path = LIVE_ROOT) -> list[str]:
    """bwrap: the live checkout and ~/.config read-only, its .env masked, the clone writable."""
    home = Path.home()
    args = [
        "bwrap", "--dev-bind", "/", "/",
        "--ro-bind", str(live_root), str(live_root),
        "--ro-bind", str(home / ".config"), str(home / ".config"),
        "--bind", str(repo), str(repo),
        "--chdir", str(repo),
    ]
    env_file = live_root / ".env"
    if env_file.exists():
        args += ["--ro-bind", "/dev/null", str(env_file)]
    return args


def wrap(cmd: list[str], *, repo: Path, unit: str, live_root: Path = LIVE_ROOT) -> list[str]:
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        f"--slice={SLICE}", f"--unit={unit}",
        "-p", f"MemoryHigh={MEM_HIGH}", "-p", f"MemoryMax={MEM_MAX}",
        "--", "nice", "-n", str(NICE), *sandbox_args(repo, live_root), "--", *cmd,
    ]


def slice_dir() -> Path | None:
    try:
        line = Path("/proc/self/cgroup").read_text().strip().splitlines()[-1]
    except OSError:
        return None
    rel = line.split("::", 1)[-1]
    m = re.match(r"(.*/user@\d+\.service)", rel)
    if not m:
        return None
    d = Path("/sys/fs/cgroup" + m.group(1)) / "app.slice" / SLICE
    return d if d.is_dir() else None


def slice_headroom_mb() -> float | None:
    d = slice_dir()
    if not d:
        return None
    try:
        high = (d / "memory.high").read_text().strip()
        if high == "max":
            high = (d / "memory.max").read_text().strip()
        if high == "max":
            return None
        return (int(high) - int((d / "memory.current").read_text())) / 2**20
    except (OSError, ValueError):
        return None


def wait_for_room(min_headroom_mb: float, max_wait_s: int = 1800, poll_s: int = 30,
                  log=print) -> bool:
    """Hold until the sessions slice has room for one more replay."""
    deadline = time.monotonic() + max_wait_s
    while True:
        room = slice_headroom_mb()
        if room is None or room >= min_headroom_mb:
            return True
        if time.monotonic() >= deadline:
            log(f"  slice still short ({room:.0f} MB free) after {max_wait_s}s, starting anyway")
            return False
        log(f"  waiting for memory: sessions slice has {room:.0f} MB free, need {min_headroom_mb:.0f}")
        time.sleep(poll_s)


def run_one(job: Job, *, log=print) -> dict:
    job.workdir.mkdir(parents=True, exist_ok=True)
    unit = f"replay-ab-{re.sub(r'[^A-Za-z0-9]+', '-', job.label)}-{secrets.token_hex(3)}.scope"
    cmd = wrap(finalize_command(job.cmd, budget_usd=job.budget_usd), repo=job.repo, unit=unit)
    write_json(job.workdir / "command.json", {"cmd": cmd, "unit": unit})
    stream = job.workdir / "stream.jsonl"
    started = time.time()
    log(f"  start {job.label}")
    with open(stream, "w", encoding="utf-8") as out, \
            open(job.workdir / "stderr.log", "w", encoding="utf-8") as err:
        proc = subprocess.Popen(cmd, cwd=str(job.repo), env=child_env(job.account),
                                stdin=subprocess.PIPE, stdout=out, stderr=err, text=True)
        try:
            proc.communicate(job.prompt, timeout=job.timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            subprocess.run(["systemctl", "--user", "kill", "--signal=SIGTERM", unit],
                           capture_output=True)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                subprocess.run(["systemctl", "--user", "kill", "--signal=SIGKILL", unit],
                               capture_output=True)
                proc.wait()
    wall = time.time() - started
    res = summarize_stream(stream)
    stderr_tail = (job.workdir / "stderr.log").read_text(encoding="utf-8", errors="replace")[-2000:]
    res.update({
        "label": job.label, "returncode": proc.returncode, "timed_out": timed_out,
        "wall_s": round(wall, 1), "unit": unit,
        "resume_failed": "No conversation found" in stderr_tail
                         or "No conversation found" in (res.get("result_text") or ""),
        "stderr_tail": stderr_tail[-600:],
    })
    write_json(job.workdir / "run.json", res)
    log(f"  done  {job.label}: rc={proc.returncode} turns={res.get('num_turns')} "
        f"cost=${res.get('total_cost_usd') or 0:.2f} wall={wall:.0f}s")
    job.result = res
    return res


def summarize_stream(path: Path) -> dict:
    """Turns, tools, tokens, cost and compactions, from the run's stream-json."""
    tools: dict[str, int] = {}
    compactions = 0
    session_id = None
    result: dict = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "system":
            if ev.get("subtype") == "init":
                session_id = ev.get("session_id") or session_id
            if ev.get("subtype") == "compact_boundary":
                compactions += 1
        elif t == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tools[b.get("name", "?")] = tools.get(b.get("name", "?"), 0) + 1
        elif t == "result":
            result = ev
    usage = result.get("usage") or {}
    return {
        "session_id": session_id or result.get("session_id"),
        "is_error": result.get("is_error"),
        "subtype": result.get("subtype"),
        "num_turns": result.get("num_turns"),
        "total_cost_usd": result.get("total_cost_usd", result.get("cost_usd")),
        "duration_ms": result.get("duration_ms"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        "tools": tools,
        "compactions": compactions,
        "result_text": (result.get("result") or "")[:4000],
    }


def run_all(jobs: list[Job], *, concurrency: int = 2, total_cap_usd: float = 20.0,
            min_headroom_mb: float = 8192, on_done=None, log=print) -> list[dict]:
    """Run jobs at bounded concurrency, stopping new starts at the spend cap."""
    lock = threading.Lock()
    spent = [0.0]
    queue = list(jobs)
    results: list[dict] = []

    def worker() -> None:
        while True:
            with lock:
                if not queue:
                    return
                if spent[0] >= total_cap_usd:
                    log(f"  spend cap ${total_cap_usd:.2f} reached, skipping {len(queue)} run(s)")
                    queue.clear()
                    return
                job = queue.pop(0)
            wait_for_room(min_headroom_mb, log=log)
            res = run_one(job, log=log)
            with lock:
                spent[0] += float(res.get("total_cost_usd") or 0.0)
                results.append(res)
            if on_done:
                on_done(job)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, concurrency))]
    for t in threads:
        t.start()
        time.sleep(1)
    for t in threads:
        t.join()
    return results
