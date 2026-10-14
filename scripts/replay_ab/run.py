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
  bwrap with everything that already knows the answer hidden behind an
      empty tmpfs (see ``hidden_paths``), ~/.config read-only, and only the
      clone and its own transcript directory writable

Hiding, not read-only, is the point. On master the live checkout holds the
merged solution, ``data/results`` holds the original build's own report,
and the account's ``projects/`` holds the original build's transcript; a
read-only bind left all of it one ``cat`` away from the session being
measured against it. ``find_peeks`` is the tripwire behind the wall: any
tool call that names a hidden path is recorded in ``run.json``, so a
replicate that went looking is visible in the report even though it found
nothing.

At most ``concurrency`` runs at a time (default 2), each one held back while
the sessions slice is short of room. A run starts only if the recorded spend
plus every in-flight run's own cap still fits under the total, so the total
is a ceiling and not a point past which starts merely stop.
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

from common import LIVE_ROOT, RUNS_ROOT, under, write_json

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

# What an account dir holds about other sessions: transcripts and their
# memory, file-edit backups, plans, todos, per-session env and shell state.
# The CLI writes its own fresh copies of these, into the tmpfs.
ACCOUNT_PRIVATE = ("projects", "file-history", "plans", "todos", "session-env",
                   "shell-snapshots", "paste-cache")
# Files rather than directories: masked with /dev/null.
ACCOUNT_PRIVATE_FILES = ("history.jsonl",)


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
    project_dir: Path | None = None  # the forked transcript's dir, kept visible
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


def hidden_paths(*, account: str | Path | None, live_root: Path = LIVE_ROOT,
                 runs_root: Path = RUNS_ROOT, home: Path | None = None) -> list[Path]:
    """Everything a replay must not see, existing paths only.

    The live checkout (master, ``data/``, every worktree), every replay run
    (another replicate's diff and score), every other account whole, and the
    private parts of the run's own account. ``account=None`` hides every
    account whole, which is right for scoring: a harness has no business in
    any of them. A path that contains the home directory is never returned,
    so a stray ``REPLAY_AB_HOME`` cannot hide the whole home.
    """
    home = home or Path.home()
    own = Path(account).resolve() if account else None
    accounts = {p.resolve() for p in home.glob(".claude*") if p.is_dir()}
    if own:
        accounts.add(own)
    out = [live_root, runs_root]
    for acct in sorted(accounts):
        if acct == own:
            out += [acct / n for n in ACCOUNT_PRIVATE + ACCOUNT_PRIVATE_FILES]
        else:
            out.append(acct)
    return [p for p in out if p.exists() and not under(home, p)]


def kept_paths(repo: Path, *, project_dir: Path | None = None,
               live_root: Path = LIVE_ROOT) -> list[tuple[Path, str]]:
    """What is bound back over the hidden paths: (path, "ro" | "rw")."""
    out: list[tuple[Path, str]] = []
    venv = live_root / ".venv"
    if venv.is_dir():
        # The interpreter sessions are told to use. Its editable install of
        # the bot points into the hidden checkout, so an import that misses
        # the clone fails instead of quietly loading the shipped solution.
        out.append((venv, "ro"))
    if project_dir:
        out.append((project_dir, "rw"))
    out.append((repo, "rw"))
    return out


def sandbox_args(repo: Path, live_root: Path = LIVE_ROOT, *, account: str | Path | None = None,
                 project_dir: Path | None = None, runs_root: Path = RUNS_ROOT,
                 home: Path | None = None, contain: bool = False) -> list[str]:
    """bwrap: the answer hidden, ~/.config read-only, the clone writable.

    ``--die-with-parent`` ties the sandbox to whoever launched it. With
    ``contain`` it also gets its own pid namespace, so killing bwrap (a
    scoring timeout) takes every process a harness started with it; an agent
    run does not need it, because its systemd scope is killed whole.
    """
    home = home or Path.home()
    args = ["bwrap", "--dev-bind", "/", "/", "--die-with-parent"]
    if contain:
        args += ["--unshare-pid", "--proc", "/proc"]
    for p in hidden_paths(account=account, live_root=live_root, runs_root=runs_root, home=home):
        # A file is masked with /dev/null as a device bind: a plain bind is
        # nodev, so the CLI's own read of it would fail with EACCES. Reads
        # come back empty and its appends go nowhere.
        args += ["--tmpfs", str(p)] if p.is_dir() else ["--dev-bind", "/dev/null", str(p)]
    args += ["--ro-bind-try", str(home / ".config"), str(home / ".config")]
    for p, mode in kept_paths(repo, project_dir=project_dir, live_root=live_root):
        args += ["--ro-bind" if mode == "ro" else "--bind", str(p), str(p)]
    return args + ["--chdir", str(repo)]


def wrap(cmd: list[str], *, repo: Path, unit: str, live_root: Path = LIVE_ROOT,
         account: str | Path | None = None, project_dir: Path | None = None) -> list[str]:
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        f"--slice={SLICE}", f"--unit={unit}",
        "-p", f"MemoryHigh={MEM_HIGH}", "-p", f"MemoryMax={MEM_MAX}",
        "--", "nice", "-n", str(NICE),
        *sandbox_args(repo, live_root, account=account, project_dir=project_dir),
        "--", *cmd,
    ]


_PATH_TAIL = r"""[^\s"'`;|&<>()]*"""


def find_peeks(texts: list[str], hidden: list[Path], kept: list[Path]) -> list[str]:
    """Hidden paths named in tool calls, other than the ones bound back.

    A tripwire, not the guard: it sees absolute paths only, so ``../..`` or
    ``~`` spellings pass it, and the tmpfs is what actually keeps them out.
    """
    kept_s = [str(k) for k in kept]
    found: list[str] = []
    for h in hidden:
        pat = re.compile(re.escape(str(h)) + _PATH_TAIL)
        for text in texts:
            for m in pat.finditer(text):
                tok = m.group(0).rstrip("/.,")
                if any(tok == k or tok.startswith(k + "/") for k in kept_s):
                    continue
                if tok not in found:
                    found.append(tok)
    return found[:20]


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
    """Hold until the sessions slice has room for one more replay.

    ``bot.claude.cgroups.slice_headroom_mb`` measures the same line, but that
    module imports ``bot.config`` (see common.py), so it is restated here.
    """
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
    cmd = wrap(finalize_command(job.cmd, budget_usd=job.budget_usd), repo=job.repo, unit=unit,
               account=job.account, project_dir=job.project_dir)
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
    res = summarize_stream(
        stream,
        hidden=hidden_paths(account=job.account),
        kept=[p for p, _ in kept_paths(job.repo, project_dir=job.project_dir)],
    )
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
        f"cost=${res.get('total_cost_usd') or 0:.2f} wall={wall:.0f}s"
        + (f" PEEKED at {len(res['peeks'])} hidden path(s)" if res.get("peeks") else ""))
    job.result = res
    return res


def summarize_stream(path: Path, *, hidden: list[Path] = (), kept: list[Path] = ()) -> dict:
    """Turns, tools, tokens, cost, compactions and peeks, from the run's stream-json."""
    tools: dict[str, int] = {}
    inputs: list[str] = []
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
                    inputs.append(json.dumps(b.get("input"), ensure_ascii=False))
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
        "peeks": find_peeks(inputs, list(hidden), list(kept)),
        "result_text": (result.get("result") or "")[:4000],
    }


def run_all(jobs: list[Job], *, concurrency: int = 2, total_cap_usd: float = 20.0,
            min_headroom_mb: float = 8192, on_done=None, log=print) -> list[dict]:
    """Run jobs at bounded concurrency under a total spend ceiling.

    A job that cannot run, or whose scoring raises, is recorded and the queue
    moves on: one bad replicate must not silently end the round. A job never
    started is marked ``{"skipped": True}`` on ``job.result``.
    """
    cond = threading.Condition()
    spent = [0.0]
    reserved = [0.0]
    queue = list(jobs)
    results: list[dict] = []

    def skip_rest() -> None:
        log(f"  spend cap ${total_cap_usd:.2f} reached, skipping {len(queue)} run(s)")
        for j in queue:
            j.result = {"label": j.label, "skipped": True}
        queue.clear()
        cond.notify_all()

    def worker() -> None:
        while True:
            with cond:
                while True:
                    if not queue:
                        return
                    job = queue[0]
                    if spent[0] + reserved[0] + job.budget_usd <= total_cap_usd:
                        break
                    if not reserved[0]:
                        skip_rest()
                        return
                    # A run in flight may come in under its own cap and
                    # leave room; wait for it rather than give up early.
                    cond.wait()
                queue.pop(0)
                reserved[0] += job.budget_usd
            try:
                wait_for_room(min_headroom_mb, log=log)
                res = run_one(job, log=log)
            except Exception as e:  # noqa: BLE001
                res = {"label": job.label, "error": f"{type(e).__name__}: {e}"}
                job.result = res
                log(f"  {job.label} could not run: {res['error']}")
            with cond:
                reserved[0] -= job.budget_usd
                spent[0] += float(res.get("total_cost_usd") or 0.0)
                results.append(res)
                cond.notify_all()
            if on_done and "error" not in res:
                try:
                    on_done(job)
                except Exception as e:  # noqa: BLE001
                    log(f"  scoring {job.label} failed: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, concurrency))]
    for t in threads:
        t.start()
        time.sleep(1)
    for t in threads:
        t.join()
    return results
