"""Step four of the judge replay: run the judge on the frozen cases and score it.

    replay.py go --split dev --rev 0 --replicates 1
    replay.py go --split held_out --rev 1 --replicates 2
    replay.py report --rev 1

The judge's prompt, input and command line all come from the bot's own code in a child
process (see scripts/replay_ab/prompt.py for why a child), so what runs here is what a
listed repo would get. It runs in the replay sandbox on a shallow clone of the commit
as it stood, so no later object exists for it to find. Resumable: a finished replicate
is never rerun, and the spend ledger is read back from what is on disk.

Scoring is fixed here, before any run:

  miss        pass or could_not_check: not caught. only_you on a looks-or-feel miss:
              caught. Anything else: caught when a fresh scorer, shown the owner's
              correction and the judge's words, says they name the same problem.
  clean       any verdict but pass is a flag, could_not_check included.
  review_fix  the same scorer ticks which of the fix commit's issues the judge named.

Each replicate is scored on its own and the replicates are averaged. Raw text stays
under data/judge_replay/. Bars live in the study record, not here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKOUT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent / "replay_ab"))

from common import CACHE_ROOT, LIVE_DATA, assert_outside_live_data, git, venv_python  # noqa: E402
from run import Job, run_one  # noqa: E402  (replay_ab's)
from sandbox import check_clone_path  # noqa: E402

RAW = LIVE_DATA / "judge_replay"
RUNS = RAW / "runs"
CLONES = CACHE_ROOT / "judge-replay"
ACCOUNT = str(Path.home() / ".claude-main")
JUDGE_BUDGET = 5.0
SCORER_BUDGET = 0.5
TOTAL_CAP = 200.0
CONCURRENCY = 2
MIN_FREE_MB = 6000
JUDGE_TIMEOUT = 900
SCORER_TIMEOUT = 180
SCORER_MODEL = "claude-sonnet-5-5"
FROZEN_SHA = "36e843fe86d2d7b4cc353842df12ac1112a0214739af664d857cd8d238de98a4"
_INFRA_LIMIT = 3


def load_cases() -> list[dict]:
    path = RAW / "cases.jsonl"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != FROZEN_SHA:
        raise SystemExit(f"cases changed since they were frozen: {digest}")
    return [json.loads(ln) for ln in path.open(encoding="utf-8")]


# --- The child: everything that has to come from the bot's own code ---

def _child(spec_path: str, out_path: str) -> int:
    sys.path.insert(0, str(CHECKOUT))
    from bot import config
    from bot.claude.provider import _CLAUDE
    from bot.claude.runner.prompt_build import PromptMixin
    from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType
    from bot.engine import judge, step_parse
    from bot.engine.step_policy import resolve_spawn_model
    from bot.platform.redact import redact_secrets

    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    if spec["op"] == "parse":
        v = judge.parse_verdict(spec["text"])
        out = {"kind": v.kind, "problems": list(v.problems), "line": v.line}
        Path(out_path).write_text(json.dumps(out), encoding="utf-8")
        return 0

    verify = ""
    if spec.get("verify_text"):
        blocks = step_parse._VERIFY_BLOCK_RE.findall(spec["verify_text"])
        verify = blocks[-1].strip() if blocks else ""
    goal = judge.repo_goal(spec.get("goal_dir") or spec["clone"])
    text = judge.build_input([redact_secrets(m) for m in spec["owner"]], goal, spec["diff"], verify)

    class _Builder(PromptMixin):
        provider = _CLAUDE

    inst = Instance(
        id=f"judge-replay-{spec['id']}", name=None, instance_type=InstanceType.QUERY, prompt=text,
        repo_name=spec["repo"], repo_path=spec["clone"], status=InstanceStatus.RUNNING,
        mode="explore", origin=InstanceOrigin.JUDGE,
    )
    inst.bash_policy = "none"
    inst.bash_policy_baseline = "none"
    routed = resolve_spawn_model(InstanceOrigin.JUDGE)
    if routed:
        inst.model = routed
    system_prompt = _Builder()._build_system_prompt(inst, context=None, sibling_context=None)
    cmd = _CLAUDE.build_command(
        inst, binary=config.CLAUDE_BINARY, system_prompt_file=spec["system_prompt_file"],
        system_prompt_inline=None, api_fallback=False, api_key_file=None,
    )
    Path(out_path).write_text(json.dumps({
        "input": text, "system_prompt": system_prompt, "cmd": cmd, "has_verify": bool(verify),
        "had_goal": bool(goal.strip()),
    }), encoding="utf-8")
    return 0


def call_child(spec: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="judge_replay_child_") as scratch:
        spec_path, out_path = Path(scratch) / "spec.json", Path(scratch) / "out.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        proc = subprocess.run(
            [venv_python(), str(Path(__file__).resolve()), "--child", str(spec_path), str(out_path)],
            cwd=scratch, env={**os.environ, "BOT_PATHS_DISABLED": "1"},
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"child failed ({proc.returncode}): {proc.stderr[-2000:]}")
        return json.loads(out_path.read_text(encoding="utf-8"))


# --- One case ---

def shallow_clone(source: str, sha: str, dest: Path) -> Path:
    """`sandbox.make_clone` at depth one: a read-only judge has no use for the history."""
    assert_outside_live_data(dest)
    check_clone_path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    git(dest, "init", "-q", "-b", "master")
    git(dest, "fetch", "-q", "--no-tags", "--depth", "1", source, sha, timeout=900)
    git(dest, "checkout", "-q", "-B", "master", sha, timeout=900)
    (dest / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    return dest


def final_text(stream: Path) -> str:
    """The whole final answer. The run summary cuts it, and the verdict comes last."""
    text = ""
    try:
        with stream.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"type":"result"' in line or '"type": "result"' in line:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("type") == "result":
                        text = ev.get("result") or ""
    except OSError:
        pass
    return text


def spent() -> float:
    total = 0.0
    for name in ("run.json", "score_run.json"):
        for p in RUNS.glob(f"*/*/*/{name}"):
            try:
                total += json.loads(p.read_text(encoding="utf-8")).get("total_cost_usd") or 0.0
            except (OSError, ValueError):
                pass
    return total


def free_mb() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def _scorer_job(label: str, prompt: str, workdir: Path) -> dict:
    scratch = CLONES / "scorer" / re.sub(r"[^A-Za-z0-9.-]+", "-", label)
    scratch.mkdir(parents=True, exist_ok=True)
    cmd = [shutil.which("claude") or "claude", "-p", "--output-format", "stream-json", "--verbose", "--model", SCORER_MODEL, "--tools", "",
           "--setting-sources", "", "--system-prompt", "You compare two short texts and answer in JSON."]
    job = Job(label=label, workdir=workdir, repo=scratch, account=ACCOUNT, cmd=cmd, prompt=prompt,
              budget_usd=SCORER_BUDGET, timeout_s=SCORER_TIMEOUT)
    res = run_one(job)
    # Beside the judge's own run.json, so the spend ledger sees both.
    (workdir.parent / "score_run.json").write_text(json.dumps(res), encoding="utf-8")
    text = final_text(workdir / "stream.jsonl")
    shutil.rmtree(scratch, ignore_errors=True)
    m = re.findall(r"\{.*\}", text, re.DOTALL)
    try:
        return json.loads(m[-1]) if m else {}
    except ValueError:
        return {}


def judge_words(v: dict) -> str:
    return "\n".join(f"- {p}" for p in v.get("problems") or []) or v.get("line") or ""


def score(case: dict, v: dict, rdir: Path) -> dict:
    kind = v["kind"]
    if case["group"] == "clean":
        return {"flagged": kind != "pass"}
    if case["group"] == "miss":
        if kind in ("pass", "could_not_check"):
            return {"caught": False, "matched": False}
        prompt = (
            "An owner looked at finished work and corrected it with these words:\n\n"
            f"<correction>\n{case['correction']}\n</correction>\n\n"
            "Before that, a reviewer who never saw those words said:\n\n"
            f"<reviewer>\n{judge_words(v)}\n</reviewer>\n\n"
            "Does the reviewer name the same underlying problem the owner complains about, even in "
            "other words? A general worry that merely touches the same area does not count. If the "
            "owner raises several problems, naming one of them counts.\n\n"
            'Answer with only this JSON: {"match": true or false, "why": "one short sentence"}'
        )
        got = _scorer_job(f"score-{case['id']}-{rdir.name}", prompt, rdir / "scorer")
        matched = got.get("match") is True
        caught = matched or (kind == "only_you" and case.get("looks"))
        return {"caught": bool(caught), "matched": matched, "why": got.get("why", "")}
    # review_fix
    if kind in ("pass", "could_not_check"):
        return {"caught_issues": 0, "issues": len(case["issues"])}
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(case["issues"], 1))
    prompt = (
        "A code review found these issues in some work:\n\n"
        f"<issues>\n{numbered}\n</issues>\n\n"
        "Separately, a different reviewer who never saw that list said:\n\n"
        f"<reviewer>\n{judge_words(v)}\n</reviewer>\n\n"
        "Which numbered issues did the second reviewer name, even in other words? A general worry "
        "that merely touches the same area does not count.\n\n"
        'Answer with only this JSON: {"caught": [numbers], "why": "one short sentence"}'
    )
    got = _scorer_job(f"score-{case['id']}-{rdir.name}", prompt, rdir / "scorer")
    nums = {n for n in (got.get("caught") or []) if isinstance(n, int) and 1 <= n <= len(case["issues"])}
    return {"caught_issues": len(nums), "issues": len(case["issues"]), "why": got.get("why", "")}


class Stop(Exception):
    pass


def run_case(case: dict, rev: str, replicates: int, state: dict, lock: threading.Lock) -> None:
    case_dir = RUNS / rev / case["id"]
    todo = [k for k in range(replicates) if not (case_dir / f"r{k}" / "score.json").exists()]
    if not todo:
        return
    # A registered repo can be a folder inside its git tree (aiagent is). Fetch from the
    # tree, and read the goal from the folder, which is where the bot reads it.
    source = git(Path(case["repo_path"]), "rev-parse", "--show-toplevel").strip()
    prefix = git(Path(case["repo_path"]), "rev-parse", "--show-prefix").strip()
    clone = shallow_clone(source, case["head"], CLONES / "clones" / rev / case["id"].replace("_", "-"))
    goal_dir = clone / prefix
    try:
        for k in todo:
            with lock:
                if state["stop"]:
                    return
                if spent() + state["reserved"] + JUDGE_BUDGET + SCORER_BUDGET > TOTAL_CAP:
                    state["stop"] = "budget"
                    return
                state["reserved"] += JUDGE_BUDGET + SCORER_BUDGET
            try:
                _run_replicate(case, clone, goal_dir, case_dir / f"r{k}", rev, k, state, lock)
            finally:
                with lock:
                    state["reserved"] -= JUDGE_BUDGET + SCORER_BUDGET
    finally:
        shutil.rmtree(clone, ignore_errors=True)


def _run_replicate(case: dict, clone: Path, goal_dir: Path, rdir: Path, rev: str, k: int, state: dict,
                   lock: threading.Lock) -> None:
    rdir.mkdir(parents=True, exist_ok=True)
    verify_text = ""
    if case.get("verify_id"):
        result = LIVE_DATA / "results" / f"{case['verify_id']}.md"
        if result.exists():
            verify_text = result.read_text(encoding="utf-8", errors="replace")
    sp_file = rdir / "system_prompt.txt"
    built = call_child({"op": "build", "id": case["id"], "repo": case["repo"], "clone": str(clone),
                        "goal_dir": str(goal_dir),
                        "owner": case["owner"], "diff": case["diff"], "verify_text": verify_text,
                        "system_prompt_file": str(sp_file)})
    sp_file.write_text(built["system_prompt"], encoding="utf-8")
    (rdir / "input.txt").write_text(built["input"], encoding="utf-8")
    job = Job(label=f"{rev}-{case['id']}-r{k}", workdir=rdir, repo=clone, account=ACCOUNT,
              cmd=built["cmd"], prompt=built["input"], budget_usd=JUDGE_BUDGET, timeout_s=JUDGE_TIMEOUT)
    res = run_one(job)
    cost = res.get("total_cost_usd") or 0.0
    text = final_text(rdir / "stream.jsonl")
    # A run that never got going says nothing about the judge: an account limit, a dead CLI.
    if not cost and not text:
        (rdir / "run.json").unlink(missing_ok=True)
        with lock:
            state["infra"] += 1
            print(f"  infra failure on {job.label}: {res.get('stderr_tail', '')[-300:]}", flush=True)
            if state["infra"] >= _INFRA_LIMIT:
                state["stop"] = "infrastructure"
        return
    parsed = call_child({"op": "parse", "text": text})
    # What a crashed or budget-capped run is in production too.
    if res.get("timed_out") or res.get("is_error"):
        parsed = {"kind": "could_not_check", "problems": [], "line": f"run ended {res.get('subtype')}"}
    verdict = {**parsed, "has_verify": built["has_verify"], "had_goal": built["had_goal"],
               "cost": cost, "peeks": res.get("peeks") or []}
    (rdir / "verdict.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")
    scored = score(case, parsed, rdir)
    (rdir / "score.json").write_text(json.dumps(scored, ensure_ascii=False, indent=1), encoding="utf-8")
    with lock:
        print(f"  {job.label}: {parsed['kind']} {scored} cost=${cost:.2f} total=${spent():.2f}", flush=True)


def go(args: argparse.Namespace) -> int:
    cases = [c for c in load_cases() if c["split"] == args.split]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only.split(",")]
    print(f"{len(cases)} cases, {args.replicates} replicate(s), spent so far ${spent():.2f}", flush=True)
    state = {"stop": None, "reserved": 0.0, "infra": 0}
    lock = threading.Lock()
    queue = list(cases)

    def worker() -> None:
        while True:
            with lock:
                if not queue or state["stop"]:
                    return
                if free_mb() < MIN_FREE_MB:
                    state["stop"] = "memory"
                    return
                case = queue.pop(0)
            try:
                run_case(case, args.rev, args.replicates, state, lock)
            except Exception as exc:  # one broken case must not end the round silently
                with lock:
                    print(f"  case {case['id']} failed: {exc}", flush=True)
                    state["infra"] += 1
                    if state["infra"] >= _INFRA_LIMIT:
                        state["stop"] = "infrastructure"

    threads = [threading.Thread(target=worker) for _ in range(CONCURRENCY)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"stopped: {state['stop'] or 'finished'}; total spent ${spent():.2f}", flush=True)
    return 0 if not state["stop"] else 3


def report(args: argparse.Namespace) -> int:
    cases = [c for c in load_cases() if c["split"] == args.split]
    rows = []
    for c in cases:
        for rdir in sorted((RUNS / args.rev / c["id"]).glob("r*")):
            sp, vp = rdir / "score.json", rdir / "verdict.json"
            if sp.exists() and vp.exists():
                rows.append((c, json.loads(vp.read_text(encoding="utf-8")), json.loads(sp.read_text(encoding="utf-8"))))
    if not rows:
        print("nothing scored yet")
        return 1

    def avg(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 3) if xs else None

    def per_case(group_rows: list, value) -> list[float]:
        """The replicates of a case averaged, so a case counts once."""
        by: dict[str, list[float]] = {}
        for c, v, s in group_rows:
            by.setdefault(c["id"], []).append(value(c, v, s))
        return [sum(x) / len(x) for x in by.values()]

    miss = [r for r in rows if r[0]["group"] == "miss"]
    clean = [r for r in rows if r[0]["group"] == "clean"]
    rev_f = [r for r in rows if r[0]["group"] == "review_fix"]
    caught = lambda c, v, s: float(s["caught"])  # noqa: E731
    strict = lambda c, v, s: float(s["matched"])  # noqa: E731
    flagged = lambda c, v, s: float(s["flagged"])  # noqa: E731
    disagree = 0
    for group in (miss, clean):
        by: dict[str, set] = {}
        for c, v, s in group:
            by.setdefault(c["id"], set()).add(s.get("caught", s.get("flagged")))
        disagree += sum(1 for x in by.values() if len(x) > 1)
    costs = [v["cost"] for _, v, _ in rows]
    issues = sum(s["issues"] for _, _, s in rev_f)
    out = {
        "split": args.split, "rev": args.rev, "cases": len({c["id"] for c, _, _ in rows}), "runs": len(rows),
        "miss_cases": len({c["id"] for c, _, _ in miss}),
        "caught_share": avg(per_case(miss, caught)),
        "named_the_problem_share": avg(per_case(miss, strict)),
        "too_narrow": {
            "cases": len({c["id"] for c, _, _ in miss if c["kind"] == "too_narrow"}),
            "caught_share": avg(per_case([r for r in miss if r[0]["kind"] == "too_narrow"], caught)),
        },
        "by_kind": {k: avg(per_case([r for r in miss if r[0]["kind"] == k], caught))
                    for k in ("not_what_i_meant", "bug", "too_narrow")},
        "looks": {"cases": len({c["id"] for c, _, _ in miss if c["looks"]}),
                  "caught_share": avg(per_case([r for r in miss if r[0]["looks"]], caught))},
        "clean_cases": len({c["id"] for c, _, _ in clean}),
        "clean_flagged_share": avg(per_case(clean, flagged)),
        "review_issues": issues,
        "review_issues_caught_share": round(sum(s["caught_issues"] for _, _, s in rev_f) / issues, 3) if issues else None,
        "median_cost": round(statistics.median(costs), 3),
        "max_cost": round(max(costs), 3),
        "verdicts": dict(Counter(v["kind"] for _, v, _ in rows)),
        "verdicts_by_group": {g: dict(Counter(v["kind"] for c, v, _ in rows if c["group"] == g))
                              for g in ("miss", "clean", "review_fix")},
        "cases_without_verify_output": len({c["id"] for c, v, _ in rows if not v["has_verify"]}),
        "runs_with_could_not_check": sum(1 for _, v, _ in rows if v["kind"] == "could_not_check"),
        "replicate_disagreements": disagree,
        "peeks": sum(1 for _, v, _ in rows if v.get("peeks")),
        "total_spent": round(spent(), 2),
    }
    print(json.dumps(out, indent=1))
    (RAW / f"report-{args.rev}-{args.split}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("go")
    g.add_argument("--split", required=True, choices=("dev", "held_out"))
    g.add_argument("--rev", required=True)
    g.add_argument("--replicates", type=int, default=1)
    g.add_argument("--only", default="")
    r = sub.add_parser("report")
    r.add_argument("--split", required=True, choices=("dev", "held_out"))
    r.add_argument("--rev", required=True)
    args = ap.parse_args()
    return go(args) if args.cmd == "go" else report(args)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        sys.exit(_child(sys.argv[2], sys.argv[3]))
    raise SystemExit(main())
