"""Read-only view of the bot's own instance registry.

Answers, from a shell, the questions a session running under this bot could
previously only punt back to the user: did the children I spawned finish, what
did they produce, and where did their code go. `ListAgents` does NOT answer
this -- it lists peer Claude CLI sessions on the machine, which is a different
dataset that knows nothing about bot instances.

    python scripts/instances.py list --repo aiagent --limit 10
    python scripts/instances.py show q-16875
    python scripts/instances.py children q-16871
    python scripts/instances.py tree q-16871
    python scripts/instances.py log q-16875 --tail 40
    python scripts/instances.py diff t-8206
    python scripts/instances.py find <thread_id|session_id>

Every command takes --json.

Read-only by construction, the way the mail and telegram readers in The Citadel
are: there is no kill, no retry and no write path in this file at all. It reads
`data/state.json` and `data/history.jsonl` and opens result files; it never
opens either for writing, and it does not go through StateStore, whose save()
and mark_dirty() would be one typo away.

Two boundaries worth knowing before you trust an answer:

* The running bot holds its state in memory and flushes on a 60s auto-save (or
  immediately on a critical update), so this view can lag reality by up to a
  minute. Every command prints how old the file is; --json carries it as
  `state_age_secs`.
* Terminal instances are pruned from state.json after INSTANCE_RETENTION_DAYS
  (7) or once more than INSTANCE_MAX_RETAINED (250) survive, and their result
  files are deleted with them. `data/history.jsonl` is append-only and keeps a
  one-line record forever, so an id this tool cannot `show` may still be
  findable in the history index -- which is why `show` falls back to it.
"""

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SCRIPT_ROOT))

# Deliberately NOT bot.config: it derives DATA_DIR from a relative `DATA_DIR=data`
# in .env, so importing it from another repo's working directory would resolve
# the state file against that repo. It also seeds the path map, which writes a
# marker file and can rewrite roots.json -- both disqualifying for a read-only
# tool. Everything imported below is dependency-free and side-effect-free.
from bot.claude.types import Instance, InstanceStatus  # noqa: E402
from bot.procutil import install_root  # noqa: E402

# The bot's real installation, even when this script is run from a build
# worktree (which has its own empty data/ tree and would read as a bot that has
# never run anything).
ROOT = install_root(_SCRIPT_ROOT)


def _resolve_data_dir(root: Path) -> Path:
    """Where the installed bot keeps state.json, without importing bot.config.

    Mirrors config's DATA_DIR resolution for the one case that matters: the
    value in .env is relative (`DATA_DIR=data`) and the bot resolves it against
    its own working directory, which is always the install root.
    """
    value = os.getenv("DATA_DIR")
    if not value:
        env_file = root / ".env"
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("DATA_DIR=") and not line.startswith("#"):
                        value = line.split("=", 1)[1].strip().strip("'\"")
                        break
            except OSError:
                value = None
    if not value:
        return root / "data"
    p = Path(value)
    return p if p.is_absolute() else root / p


DATA_DIR = _resolve_data_dir(ROOT)
STATE_FILE = DATA_DIR / "state.json"
HISTORY_FILE = DATA_DIR / "history.jsonl"
RESULTS_DIR = DATA_DIR / "results"


# --- formatting helpers ----------------------------------------------------


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age(value: str | None, suffix: str = " ago") -> str:
    dt = _parse_iso(value)
    if dt is None:
        return "-"
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    return _dur(secs) + suffix


def _dur(secs: float) -> str:
    secs = max(0.0, secs)
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs / 60)}m"
    if secs < 172800:
        return f"{secs / 3600:.1f}h"
    return f"{int(secs / 86400)}d"


def _clip(text: str | None, width: int) -> str:
    if not text:
        return "-"
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _pid_alive(pid: int | None) -> bool | None:
    """True/False on Linux, None where we cannot tell.

    This is PID liveness, not proof the instance is the process: a recycled pid
    reads as alive. Cross-checked against the command line where readable, so a
    pid reused by an unrelated program is not reported as a live session.
    """
    if not pid:
        return None
    proc = Path("/proc") / str(pid)
    if not proc.exists():
        return False if Path("/proc").is_dir() else None
    try:
        cmdline = (proc / "cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return True
    low = cmdline.lower()
    return ("claude" in low) or ("node" in low) or ("cursor" in low)


# --- the registry ----------------------------------------------------------


class ChildRef:
    """One entry of a spawn wave: a thread, and whatever ran in it."""

    def __init__(self, thread_id: str, repo: str | None, topic: str | None,
                 session_id: str | None, instances: list[Instance]):
        self.thread_id = thread_id
        self.repo = repo
        self.topic = topic
        self.session_id = session_id
        self.instances = instances          # oldest first

    @property
    def current(self) -> Instance | None:
        return self.instances[-1] if self.instances else None

    @property
    def state(self) -> str:
        inst = self.current
        if inst is None:
            return "unresolved"
        if inst.status == InstanceStatus.COMPLETED and inst.needs_input:
            return "blocked"
        return inst.status.value


class Registry:
    def __init__(self) -> None:
        if not STATE_FILE.exists():
            raise SystemExit(f"No state file at {STATE_FILE}")
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        self.state_mtime = STATE_FILE.stat().st_mtime

        self.instances: dict[str, Instance] = {}
        for d in raw.get("instances", []):
            try:
                inst = Instance.from_dict(d)
            except Exception:
                continue
            self.instances[inst.id] = inst

        # thread_id -> {"repo", "session_id", "topic", "origin", "mode"}
        self.threads: dict[str, dict] = {}
        forums = (raw.get("platform_state", {}).get("discord", {})
                  .get("forum_projects", {}) or {})
        for repo_key, project in forums.items():
            if not isinstance(project, dict):
                continue
            for tid, info in (project.get("threads") or {}).items():
                if not isinstance(info, dict):
                    continue
                self.threads[str(tid)] = {
                    "repo": project.get("repo_name") or repo_key,
                    "session_id": info.get("session_id"),
                    "topic": info.get("topic"),
                    "origin": info.get("origin"),
                    "mode": info.get("mode"),
                }

        self._by_session: dict[str, list[Instance]] = {}
        for inst in self.instances.values():
            if inst.session_id:
                self._by_session.setdefault(inst.session_id, []).append(inst)
        for group in self._by_session.values():
            group.sort(key=lambda i: i.created_at or "")

        self._history_by_id: dict[str, dict] | None = None
        self._history_by_thread: dict[str, list[str]] | None = None

    # -- history (lazy: 13 MB of JSONL, only some commands need it) ---------

    def _load_history(self) -> None:
        if self._history_by_id is not None:
            return
        by_id: dict[str, dict] = {}
        by_thread: dict[str, list[str]] = {}
        if HISTORY_FILE.exists():
            try:
                with HISTORY_FILE.open(encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(e, dict) or not e.get("id"):
                            continue
                        # Only the small fields, so indexing 19k entries does
                        # not pull every session summary into memory.
                        rec = {
                            "id": e["id"],
                            "thread_id": e.get("thread_id"),
                            "repo": e.get("repo"),
                            "branch": e.get("branch"),
                            "status": e.get("status"),
                            "mode": e.get("mode"),
                            "origin": e.get("origin"),
                            "started": e.get("started"),
                            "finished": e.get("finished"),
                            "topic": _clip(e.get("topic"), 160),
                        }
                        by_id[rec["id"]] = rec      # last line wins (a retry)
                        tid = rec["thread_id"]
                        if tid:
                            ids = by_thread.setdefault(str(tid), [])
                            if rec["id"] not in ids:
                                ids.append(rec["id"])
            except OSError:
                pass
        self._history_by_id = by_id
        self._history_by_thread = by_thread

    def history_for(self, instance_id: str) -> dict | None:
        self._load_history()
        return self._history_by_id.get(instance_id)

    def history_ids_in_thread(self, thread_id: str) -> list[str]:
        self._load_history()
        return list(self._history_by_thread.get(str(thread_id), []))

    # -- lookups ------------------------------------------------------------

    def get(self, id_or_name: str) -> Instance | None:
        inst = self.instances.get(id_or_name)
        if inst:
            return inst
        low = id_or_name.lower()
        for cand in self.instances.values():
            if cand.name and cand.name.lower() == low:
                return cand
        return None

    def thread_of(self, inst: Instance) -> str | None:
        """The forum thread an instance ran in, or None if nothing records it.

        History is authoritative -- it is written per instance at finalize. The
        session lookup is the fallback for an instance that is still running,
        and it is only correct while the thread has not moved on to a newer
        session. An instance that died before the CLI reported a session id AND
        never finalized (a bot restart mid-run) has neither, and is unlinkable.
        """
        rec = self.history_for(inst.id)
        if rec and rec.get("thread_id"):
            return str(rec["thread_id"])
        if inst.session_id:
            for tid, info in self.threads.items():
                if info.get("session_id") == inst.session_id:
                    return tid
        return None

    def instances_in_thread(self, thread_id: str) -> list[Instance]:
        """Every instance known to have run in a thread, oldest first."""
        found: dict[str, Instance] = {}
        info = self.threads.get(str(thread_id)) or {}
        for inst in self._by_session.get(info.get("session_id") or "", []):
            found[inst.id] = inst
        for iid in self.history_ids_in_thread(thread_id):
            inst = self.instances.get(iid)
            if inst is not None:
                found[inst.id] = inst
        return sorted(found.values(), key=lambda i: i.created_at or "")

    def wave_children(self, inst: Instance) -> list[ChildRef]:
        """Children this instance dispatched with [BOT_CMD: /spawn]."""
        out: list[ChildRef] = []
        for tid in inst.spawn_dispatched_thread_ids or []:
            info = self.threads.get(str(tid)) or {}
            out.append(ChildRef(
                thread_id=str(tid),
                repo=info.get("repo"),
                topic=info.get("topic"),
                session_id=info.get("session_id"),
                instances=self.instances_in_thread(tid),
            ))
        return out

    def step_children(self, inst: Instance) -> list[Instance]:
        """Children spawned by a button or chain step (parent_id is set)."""
        kids = [i for i in self.instances.values() if i.parent_id == inst.id]
        return sorted(kids, key=lambda i: i.created_at or "")

    def wave_parent(self, inst: Instance) -> Instance | None:
        """The instance whose spawn wave this instance's thread belongs to."""
        tid = self.thread_of(inst)
        if not tid:
            return None
        for cand in self.instances.values():
            if tid in (cand.spawn_dispatched_thread_ids or []):
                return cand
        return None

    def parent_of(self, inst: Instance) -> tuple[Instance | None, str]:
        if inst.parent_id:
            parent = self.instances.get(inst.parent_id)
            if parent is not None:
                return parent, "step"
            return None, "step (pruned)"
        parent = self.wave_parent(inst)
        if parent is not None:
            return parent, "spawn wave"
        return None, ""

    # -- artefacts ----------------------------------------------------------

    def result_path(self, inst: Instance) -> Path | None:
        return self._artefact(inst.result_file, f"{inst.id}.md")

    def diff_path(self, inst: Instance) -> Path | None:
        return self._artefact(inst.diff_file, f"{inst.id}.diff")

    def _artefact(self, recorded: str | None, fallback_name: str) -> Path | None:
        """Resolve a recorded artefact path, falling back to the local results dir.

        The recorded path is absolute and correct for whichever machine wrote
        it. On a dual-boot that shares state.json (see bot/paths.py) that is the
        other machine's spelling, so rather than reimplement the path map here,
        fall back to this installation's own results directory -- which is
        where these files live by construction.
        """
        if recorded:
            p = Path(recorded)
            if p.exists():
                return p
        p = RESULTS_DIR / fallback_name
        return p if p.exists() else None


# --- rendering -------------------------------------------------------------


def _state_banner(reg: Registry) -> str:
    age = _dur(datetime.now(timezone.utc).timestamp() - reg.state_mtime)
    return (f"# {STATE_FILE} written {age} ago "
            f"({len(reg.instances)} instances retained)")


def _live_flag(inst: Instance) -> str:
    """How much the RUNNING status can be trusted.

    A status field alone cannot tell a live run from one the bot lost track of
    -- an instance killed with the bot is marked failed only on the next
    startup sweep. `nopid` is not a fault: the pid is assigned when the CLI is
    actually spawned, so an instance still queued behind the concurrency limit
    (or one whose pid has not reached the 60s auto-save yet) has none.
    """
    if inst.status != InstanceStatus.RUNNING:
        return ""
    if not inst.pid:
        return "nopid"
    alive = _pid_alive(inst.pid)
    if alive is True:
        return "live"
    if alive is False:
        return "STALE"
    return "?"


def _row(reg: Registry, inst: Instance) -> list[str]:
    return [
        inst.id,
        inst.status.value + (f"/{_live_flag(inst)}" if _live_flag(inst) else ""),
        inst.repo_name or "-",
        _age(inst.created_at, ""),
        inst.mode or "-",
        (inst.origin.value if inst.origin else "-"),
        inst.branch or "-",
        _clip(inst.prompt, 60),
    ]


def _table(rows: list[list[str]], header: list[str]) -> str:
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(header)).rstrip()]
    for row in rows:
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


def _inst_json(reg: Registry, inst: Instance, full: bool = False) -> dict:
    out = {
        "id": inst.id,
        "status": inst.status.value,
        "live": _pid_alive(inst.pid) if inst.status == InstanceStatus.RUNNING else None,
        "repo": inst.repo_name,
        "mode": inst.mode,
        "origin": inst.origin.value if inst.origin else None,
        "branch": inst.branch,
        "worktree_path": inst.worktree_path,
        "created_at": inst.created_at,
        "finished_at": inst.finished_at,
        "needs_input": inst.needs_input,
        "prompt": _clip(inst.prompt, 200),
    }
    if not full:
        return out
    result = reg.result_path(inst)
    diff = reg.diff_path(inst)
    parent, kind = reg.parent_of(inst)
    out.update({
        "name": inst.name,
        "prompt_full": inst.prompt,
        "repo_path": inst.repo_path,
        "original_branch": inst.original_branch,
        "chained_from": inst.chained_from,
        "duration_secs": (inst.duration_ms / 1000.0) if inst.duration_ms else None,
        "session_id": inst.session_id,
        "session_account": inst.session_account,
        "resumed_session": inst.resumed_session,
        "pid": inst.pid,
        "error": inst.error,
        "summary": inst.summary,
        "cost_usd": inst.cost_usd,
        "num_turns": inst.num_turns,
        "input_tokens": inst.input_tokens,
        "output_tokens": inst.output_tokens,
        "context_tokens": inst.context_tokens,
        "model": inst.model,
        "effort": inst.effort,
        "tools_used": inst.tools_used,
        "user_name": inst.user_name,
        "parent_id": parent.id if parent else inst.parent_id,
        "parent_kind": kind or None,
        "thread_id": reg.thread_of(inst),
        "spawn_depth": inst.spawn_depth,
        "spawn_dispatched_thread_ids": inst.spawn_dispatched_thread_ids,
        "spawn_wave_released": inst.spawn_wave_released,
        "spawn_wave_sealed": inst.spawn_wave_sealed,
        "manual_recovery_needed": inst.manual_recovery_needed,
        "path_poisoning": inst.path_poisoning,
        "result_file": str(result) if result else None,
        "diff_file": str(diff) if diff else None,
        "retry_count": inst.retry_count,
        "schedule_id": inst.schedule_id,
        "history": reg.history_for(inst.id),
    })
    return out


def _child_json(reg: Registry, ref: ChildRef) -> dict:
    return {
        "thread_id": ref.thread_id,
        "repo": ref.repo,
        "topic": ref.topic,
        "session_id": ref.session_id,
        "state": ref.state,
        "instances": [_inst_json(reg, i) for i in ref.instances],
    }


# --- commands --------------------------------------------------------------


def cmd_list(reg: Registry, args) -> int:
    insts = list(reg.instances.values())
    if args.repo:
        want = args.repo.lower()
        insts = [i for i in insts if (i.repo_name or "").lower() == want]
    if args.status:
        wanted = {s.strip().lower() for s in args.status.split(",") if s.strip()}
        insts = [i for i in insts if i.status.value in wanted]
    if args.since_hours:
        cutoff = datetime.now(timezone.utc).timestamp() - args.since_hours * 3600
        insts = [i for i in insts
                 if (_parse_iso(i.created_at) or datetime.now(timezone.utc)).timestamp() >= cutoff]
    insts.sort(key=lambda i: i.created_at or "", reverse=True)
    total = len(insts)
    insts = insts[: args.limit]

    if args.json:
        print(json.dumps({
            "state_age_secs": round(datetime.now(timezone.utc).timestamp() - reg.state_mtime, 1),
            "matched": total,
            "shown": len(insts),
            "instances": [_inst_json(reg, i) for i in insts],
        }, indent=2))
        return 0

    print(_state_banner(reg))
    if not insts:
        print("No instances match.")
        return 0
    header = ["ID", "STATUS", "REPO", "AGE", "MODE", "ORIGIN", "BRANCH", "PROMPT"]
    print(_table([_row(reg, i) for i in insts], header))
    if total > len(insts):
        print(f"# {total - len(insts)} more match (raise --limit)")
    return 0


def cmd_show(reg: Registry, args) -> int:
    inst = reg.get(args.id)
    if inst is None:
        rec = reg.history_for(args.id)
        if rec is None:
            print(f"No instance {args.id!r} in {STATE_FILE.name} or history.",
                  file=sys.stderr)
            return 1
        # Pruned from state.json (retention), still in the append-only history.
        if args.json:
            print(json.dumps({"id": args.id, "pruned": True, "history": rec}, indent=2))
        else:
            print(_state_banner(reg))
            print(f"{args.id}: pruned from state.json; history record only")
            for k, v in rec.items():
                print(f"  {k:<12} {v}")
        return 0

    data = _inst_json(reg, inst, full=True)
    if args.json:
        data["state_age_secs"] = round(
            datetime.now(timezone.utc).timestamp() - reg.state_mtime, 1)
        print(json.dumps(data, indent=2))
        return 0

    print(_state_banner(reg))
    live = _live_flag(inst)
    print(f"{inst.id}  {inst.status.value}{(' [' + live + ']') if live else ''}"
          f"  {inst.repo_name or '-'}")
    print()

    def line(label: str, value) -> None:
        if value in (None, "", [], 0, False):
            return
        print(f"  {label:<22} {value}")

    line("name", inst.name)
    line("mode", inst.mode)
    line("origin", inst.origin.value if inst.origin else None)
    line("started", f"{inst.created_at}  ({_age(inst.created_at)})")
    line("finished", f"{inst.finished_at}  ({_age(inst.finished_at)})"
         if inst.finished_at else None)
    line("duration", _dur(inst.duration_ms / 1000.0) if inst.duration_ms else None)
    print()
    line("branch", inst.branch)
    line("worktree", inst.worktree_path)
    line("merges into", inst.original_branch)
    line("stacked on", inst.chained_from)
    line("repo path", inst.repo_path)
    print()
    if data["parent_id"]:
        print(f"  {'parent':<22} {data['parent_id']}  ({data['parent_kind']})")
    line("thread", data["thread_id"])
    line("session", inst.session_id)
    line("session account", inst.session_account)
    line("spawn depth", inst.spawn_depth)
    if inst.spawn_dispatched_thread_ids:
        print(f"  {'spawned children':<22} {len(inst.spawn_dispatched_thread_ids)}"
              f" thread(s) -- see `children {inst.id}`")
    line("needs input", inst.needs_input)
    line("manual recovery", inst.manual_recovery_reason if inst.manual_recovery_needed else None)
    line("edited main repo", ", ".join(inst.path_poisoning) if inst.path_poisoning else None)
    print()
    line("pid", f"{inst.pid} ({'alive' if _pid_alive(inst.pid) else 'gone'})"
         if inst.pid else None)
    line("turns", inst.num_turns)
    line("cost", f"${inst.cost_usd:.4f}" if inst.cost_usd else None)
    line("tokens in/out", f"{inst.input_tokens}/{inst.output_tokens}")
    line("model", inst.model)
    line("tools", ", ".join(sorted(set(inst.tools_used))) if inst.tools_used else None)
    print()
    line("result file", data["result_file"] or (
        "(recorded as %s, missing)" % inst.result_file if inst.result_file else None))
    line("diff file", data["diff_file"])
    print()
    if inst.error:
        print(f"  error: {inst.error}")
    if inst.summary:
        print("  summary:")
        for para in _clip(inst.summary, 1200).split(". "):
            print(f"    {para}")
    print(f"\n  prompt: {_clip(inst.prompt, 600)}")
    return 0


def _print_children(reg: Registry, inst: Instance) -> None:
    waves = reg.wave_children(inst)
    steps = reg.step_children(inst)
    if not waves and not steps:
        print(f"{inst.id} dispatched no children "
              f"(no spawn wave, no button/chain steps).")
        return

    if waves:
        counts: dict[str, int] = {}
        for ref in waves:
            counts[ref.state] = counts.get(ref.state, 0) + 1
        summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        sealed = "sealed" if inst.spawn_wave_sealed else "NOT sealed"
        released = "released" if inst.spawn_wave_released else "open"
        print(f"spawn wave of {inst.id}: {len(waves)} children -- {summary} "
              f"[{sealed}, {released}]")
        for ref in waves:
            cur = ref.current
            head = f"  thread {ref.thread_id}  {ref.state}"
            if cur is not None:
                head += (f"  -> {cur.id}  {cur.repo_name or '-'}  "
                         f"{_age(cur.finished_at or cur.created_at)}")
            print(head)
            if ref.topic:
                print(f"      topic:  {_clip(ref.topic, 90)}")
            if cur is None:
                print("      no instance recorded for this thread's session "
                      "(never started, or pruned)")
                continue
            if cur.branch or cur.worktree_path:
                print(f"      branch: {cur.branch or '-'}   worktree: "
                      f"{cur.worktree_path or '-'}")
            if cur.error:
                print(f"      error:  {_clip(cur.error, 90)}")
            earlier = [i for i in ref.instances if i.id != cur.id]
            if earlier:
                print("      earlier in this thread: "
                      + ", ".join(f"{i.id}({i.status.value})" for i in earlier))

    if steps:
        print(f"\nbutton/chain steps of {inst.id}: {len(steps)}")
        print(_table([_row(reg, i) for i in steps],
                     ["ID", "STATUS", "REPO", "AGE", "MODE", "ORIGIN", "BRANCH", "PROMPT"]))


def cmd_children(reg: Registry, args) -> int:
    inst = reg.get(args.id)
    if inst is None:
        print(f"No instance {args.id!r}.", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({
            "id": inst.id,
            "spawn_wave_sealed": inst.spawn_wave_sealed,
            "spawn_wave_released": inst.spawn_wave_released,
            "wave_children": [_child_json(reg, r) for r in reg.wave_children(inst)],
            "step_children": [_inst_json(reg, i) for i in reg.step_children(inst)],
        }, indent=2))
        return 0
    print(_state_banner(reg))
    _print_children(reg, inst)
    return 0


def _root_of(reg: Registry, inst: Instance) -> Instance:
    seen = {inst.id}
    cur = inst
    for _ in range(20):
        parent, _kind = reg.parent_of(cur)
        if parent is None or parent.id in seen:
            return cur
        seen.add(parent.id)
        cur = parent
    return cur


def _walk(reg: Registry, inst: Instance, depth: int, seen: set[str],
          lines: list[str]) -> None:
    pad = "  " * depth
    live = _live_flag(inst)
    lines.append(f"{pad}{inst.id}  {inst.status.value}"
                 f"{(' [' + live + ']') if live else ''}  "
                 f"{inst.repo_name or '-'}  {_age(inst.created_at)}  "
                 f"{_clip(inst.prompt, 60)}")
    if inst.branch:
        lines.append(f"{pad}    branch {inst.branch}")
    if inst.id in seen or depth > 6:
        return
    seen.add(inst.id)
    for ref in reg.wave_children(inst):
        cur = ref.current
        if cur is None:
            lines.append(f"{pad}  thread {ref.thread_id}  unresolved  "
                         f"{_clip(ref.topic, 50)}")
            continue
        _walk(reg, cur, depth + 1, seen, lines)
    for kid in reg.step_children(inst):
        if kid.id not in seen:
            _walk(reg, kid, depth + 1, seen, lines)


def cmd_tree(reg: Registry, args) -> int:
    inst = reg.get(args.id)
    if inst is None:
        print(f"No instance {args.id!r}.", file=sys.stderr)
        return 1
    root = inst if args.no_root else _root_of(reg, inst)
    lines: list[str] = []
    _walk(reg, root, 0, set(), lines)
    if args.json:
        print(json.dumps({"root": root.id, "asked": inst.id, "lines": lines}, indent=2))
        return 0
    print(_state_banner(reg))
    if root.id != inst.id:
        print(f"# root of {inst.id} is {root.id}")
    print("\n".join(lines))
    return 0


def cmd_log(reg: Registry, args) -> int:
    inst = reg.get(args.id)
    if inst is None:
        print(f"No instance {args.id!r}.", file=sys.stderr)
        return 1
    path = reg.diff_path(inst) if args.diff else reg.result_path(inst)
    what = "diff" if args.diff else "result"
    if path is None:
        recorded = inst.diff_file if args.diff else inst.result_file
        note = (f"recorded at {recorded} but not on disk (pruned with retention)"
                if recorded else f"no {what} file was ever written")
        if args.json:
            print(json.dumps({"id": inst.id, "file": None, "reason": note,
                              "summary": inst.summary, "error": inst.error}, indent=2))
        else:
            print(f"{inst.id}: {note}")
            if inst.error:
                print(f"error: {inst.error}")
            if inst.summary:
                print(f"summary: {inst.summary}")
        return 0
    text = path.read_text(encoding="utf-8", errors="replace")
    if args.tail:
        text = "\n".join(text.splitlines()[-args.tail:])
    if args.json:
        print(json.dumps({"id": inst.id, "file": str(path), "text": text}, indent=2))
        return 0
    print(f"# {path}")
    print(text)
    return 0


def cmd_find(reg: Registry, args) -> int:
    """Resolve a thread id or session id to the instances that ran under it."""
    key = args.key
    hits: list[Instance] = []
    kind = ""
    if key in reg.threads or key.isdigit():
        hits = reg.instances_in_thread(key)
        kind = "thread"
    if not hits:
        hits = [i for i in reg.instances.values() if i.session_id == key]
        hits.sort(key=lambda i: i.created_at or "")
        if hits:
            kind = "session"
    if args.json:
        print(json.dumps({"key": key, "kind": kind,
                          "instances": [_inst_json(reg, i) for i in hits]}, indent=2))
        return 0
    print(_state_banner(reg))
    if not hits:
        print(f"Nothing recorded for {key!r}.")
        return 1
    info = reg.threads.get(key)
    if info:
        print(f"thread {key}  repo={info.get('repo')}  "
              f"topic={_clip(info.get('topic'), 70)}")
    print(_table([_row(reg, i) for i in hits],
                 ["ID", "STATUS", "REPO", "AGE", "MODE", "ORIGIN", "BRANCH", "PROMPT"]))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="instances.py",
        description="Read-only view of the bot's instance registry.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="recent instances")
    p.add_argument("--repo")
    p.add_argument("--status", help="comma-separated: running,completed,failed,killed,queued")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--since-hours", type=float, default=0.0)
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="everything known about one instance")
    p.add_argument("id")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("children", help="instances this one spawned")
    p.add_argument("id")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_children)

    p = sub.add_parser("tree", help="the whole parent/child chain")
    p.add_argument("id")
    p.add_argument("--no-root", action="store_true",
                   help="start at this instance instead of walking up first")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_tree)

    p = sub.add_parser("log", help="the instance's recorded output")
    p.add_argument("id")
    p.add_argument("--tail", type=int, default=0)
    p.add_argument("--diff", action="store_true", help="show the diff file instead")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("diff", help="the instance's recorded git diff")
    p.add_argument("id")
    p.add_argument("--tail", type=int, default=0)
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_log, diff=True)

    p = sub.add_parser("find", help="resolve a thread id or session id")
    p.add_argument("key")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_find)

    args = ap.parse_args(argv)
    if not getattr(args, "diff", False):
        args.diff = False
    return args.fn(Registry(), args)


if __name__ == "__main__":
    sys.exit(main())
