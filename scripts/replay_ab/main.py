"""Offline replay A/B: rerun finished builds under two prompt arms and compare.

Usage::

    python scripts/replay_ab/main.py extract t-9086
    python scripts/replay_ab/main.py run --instances t-9086 [--arm-a current] \\
        [--arm-b arms/trimmed.json] [--k 3] [--budget 20] [--per-run 6] \\
        [--max-parallel 2] [--suite]
    python scripts/replay_ab/main.py report <run-dir>
    python scripts/replay_ab/main.py score <run-dir>     # re-score after a scoring fix
    python scripts/replay_ab/main.py clean <run-dir>

A run directory lives under ``~/.cache/replay-ab/runs/<run-id>/`` (or
``$REPLAY_AB_HOME``), never under the bot's ``data/``::

    <run-id>/
      run.json                      arms, instances, k, caps
      <instance>/case.json          what extract() recovered
      <instance>/<arm>/r<k>/        repo/ sysprompt.txt meta.json command.json
                                    stream.jsonl stderr.log run.json score.json

Method, from the replay literature this was designed against:

  * Each replicate reruns live from the original starting point: the base
    commit and the conversation forked at the row the build prompt hung off.
    Logged tool results are never spliced into a variant.
  * Replicates are mandatory, since one run of an agent is one sample. Run an
    A/A round (both arms ``current``) first: its spread is the noise floor a
    real A/B difference has to clear, and ``report`` prints it whenever both
    arms carry the same name prefix or an ``--aa`` run is reported.
  * Replicates of the two arms are interleaved, so drift in the API over the
    run lands on both arms instead of on whichever ran second.

TODO: a pairwise LLM judge (each pair judged twice with the positions
swapped) for the cases the tests do not decide. Deferred with ``--review``
in score.py; the fail-to-pass result is the deciding score for now.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from common import RUNS_ROOT, assert_outside_live_data, read_json, write_json  # noqa: E402
import extract as extract_mod  # noqa: E402
import prompt as prompt_mod  # noqa: E402
import run as run_mod  # noqa: E402
import sandbox as sandbox_mod  # noqa: E402
import score as score_mod  # noqa: E402


def leaks(system_prompt: str, case) -> list[str]:
    """Anything in an arm's system prompt that could tell the session the answer."""
    found = [m for m in prompt_mod.ALWAYS_DROPPED_MARKERS if m in system_prompt]
    for sha in (case.ref_tip, case.merge_sha):
        if sha and sha[:10] in system_prompt:
            found.append(f"solution sha {sha[:10]}")
    return found


def prepare(case, arm: dict, rep_dir: Path) -> run_mod.Job:
    assert_outside_live_data(rep_dir)
    rep_dir.mkdir(parents=True, exist_ok=True)
    sb = sandbox_mod.build(case, rep_dir, arm.get("files"))
    p = prompt_mod.build(arm, case=case, sandbox_repo=sb.repo, session_id=sb.session_id,
                         system_prompt_file=rep_dir / "sysprompt.txt")
    bad = leaks(p["system_prompt"], case)
    if bad:
        raise RuntimeError(f"{rep_dir}: system prompt leaks the future: {bad}")
    write_json(rep_dir / "meta.json", {
        "arm": arm, "start_sha": sb.start_sha, "session_id": sb.session_id,
        "project_dir": str(sb.project_dir) if sb.project_dir else None,
        "system_prompt_chars": len(p["system_prompt"]),
    })
    return run_mod.Job(
        label=f"{case.instance_id}/{arm['name']}/{rep_dir.name}", workdir=rep_dir, repo=sb.repo,
        account=case.account, cmd=p["cmd"], prompt=case.prompt, budget_usd=0.0, timeout_s=0,
    )


def cmd_run(args) -> int:
    arm_a = prompt_mod.load_arm(args.arm_a)
    arm_b = prompt_mod.load_arm(args.arm_b) if args.arm_b else None
    if arm_b and arm_b["name"] == arm_a["name"]:
        arm_a = {**arm_a, "name": arm_a["name"] + "-a"}
        arm_b = {**arm_b, "name": arm_b["name"] + "-b"}
    arms = [a for a in (arm_a, arm_b) if a]
    run_id = args.run_id or time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    run_dir = RUNS_ROOT / run_id
    assert_outside_live_data(run_dir)
    write_json(run_dir / "run.json", {
        "instances": args.instances, "arms": arms, "k": args.k, "budget": args.budget,
        "per_run": args.per_run, "max_parallel": args.max_parallel, "suite": args.suite,
    })
    print(f"run dir: {run_dir}")

    jobs, starts, cases = [], {}, {}
    for iid in args.instances:
        case = extract_mod.extract(iid)
        cases[iid] = case
        write_json(run_dir / iid / "case.json", case.to_dict())
        for rep in range(args.k):
            for arm in (arms if rep % 2 == 0 else list(reversed(arms))):
                rep_dir = run_dir / iid / arm["name"] / f"r{rep}"
                job = prepare(case, arm, rep_dir)
                job.budget_usd, job.timeout_s = args.per_run, args.timeout
                jobs.append(job)
                starts[job.label] = (read_json(rep_dir / "meta.json") or {})["start_sha"]
                print(f"  prepared {job.label}")

    def on_done(job: run_mod.Job) -> None:
        case = cases[job.label.split("/", 1)[0]]
        s = score_mod.score(job.workdir, case, start_sha=starts[job.label], suite=args.suite)
        f2p = s["fail_to_pass"]
        print(f"  scored {job.label}: tests {f2p['passed']}/{f2p['total']}, "
              f"{len(s['diff']['files'])} files, overlap {s['diff']['file_jaccard']}")

    run_mod.run_all(jobs, concurrency=args.max_parallel, total_cap_usd=args.budget,
                    on_done=on_done)
    return cmd_report(argparse.Namespace(run_dir=str(run_dir)))


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def _rows(run_dir: Path):
    for score_path in sorted(run_dir.glob("*/*/r*/score.json")):
        rep_dir = score_path.parent
        yield rep_dir.parent.parent.name, rep_dir.parent.name, rep_dir.name, read_json(score_path)


def _metric(s: dict) -> dict:
    f2p, st = s["fail_to_pass"], s["stream"]
    own = s.get("own_tests") or {}
    return {
        "pass": (f2p["passed"] / f2p["total"]) if f2p["total"] else None,
        "iface": (f2p.get("interface", 0) / f2p["total"]) if f2p["total"] else None,
        "own": (own["passed"] / own["total"]) if own.get("total") else None,
        "ok": 1.0 if s["run_ok"] else 0.0,
        "turns": st.get("num_turns"),
        "cost": st.get("total_cost_usd"),
        "wall": st.get("wall_s"),
        "files": len(s["diff"]["files"]),
        "overlap": s["diff"]["file_jaccard"],
    }


def _fmt(vals: list) -> str:
    vals = [v for v in vals if v is not None]
    if not vals:
        return "-"
    m = statistics.fmean(vals)
    sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return f"{m:.2f}±{sd:.2f} (n={len(vals)})"


def cmd_report(args) -> int:
    run_dir = Path(args.run_dir)
    meta = read_json(run_dir / "run.json", {}) or {}
    by: dict = {}
    for iid, arm, rep, s in _rows(run_dir):
        by.setdefault(iid, {}).setdefault(arm, {})[rep] = _metric(s)
        print(f"{iid} {arm} {rep}: " + json.dumps(_metric(s)))
    if not by:
        print("no scored replicates")
        return 1
    arm_names = [a["name"] for a in meta.get("arms", [])] or sorted({a for v in by.values() for a in v})
    keys = ("pass", "iface", "own", "ok", "turns", "cost", "wall", "files", "overlap")
    print()
    for iid, arms in by.items():
        print(f"== {iid}")
        for arm in arm_names:
            reps = arms.get(arm, {})
            print(f"  {arm}: " + "  ".join(f"{k}={_fmt([r[k] for r in reps.values()])}" for k in keys))
        if len(arm_names) == 2 and all(a in arms for a in arm_names):
            a, b = (arms[n] for n in arm_names)
            paired = sorted(set(a) & set(b))
            for k in ("pass", "turns", "cost"):
                d = [b[r][k] - a[r][k] for r in paired
                     if a[r][k] is not None and b[r][k] is not None]
                print(f"  paired {k} ({arm_names[1]} - {arm_names[0]}): {_fmt(d)}")
    same = len(arm_names) == 2 and arm_names[0].rsplit("-", 1)[0] == arm_names[1].rsplit("-", 1)[0]
    if same:
        print("\nA/A round: the paired spread above is the noise floor for this case set.")
    return 0


def cmd_score(args) -> int:
    """Re-score every replicate of a run, e.g. after a scoring fix."""
    run_dir = Path(args.run_dir)
    for meta_path in sorted(run_dir.glob("*/*/r*/meta.json")):
        rep_dir = meta_path.parent
        if not (rep_dir / "repo").is_dir():
            print(f"  {rep_dir}: clone already removed, skipped")
            continue
        case = extract_mod.Case.from_dict(read_json(rep_dir.parent.parent / "case.json"))
        start = (read_json(meta_path) or {})["start_sha"]
        s = score_mod.score(rep_dir, case, start_sha=start, suite=args.suite)
        f2p = s["fail_to_pass"]
        print(f"  scored {rep_dir.relative_to(run_dir)}: tests {f2p['passed']}/{f2p['total']} "
              f"({f2p.get('interface', 0)} interface)")
    return cmd_report(args)


def cmd_clean(args) -> int:
    run_dir = Path(args.run_dir)
    n = 0
    for meta_path in run_dir.glob("*/*/r*/meta.json"):
        meta = read_json(meta_path, {}) or {}
        pd = meta.get("project_dir")
        sandbox_mod.destroy(meta_path.parent, Path(pd) if pd else None)
        n += 1
    print(f"removed {n} clone(s) and their forked transcripts; results kept in {run_dir}")
    if args.all:
        shutil.rmtree(run_dir, ignore_errors=True)
        print("removed the run directory")
    return 0


def cmd_extract(args) -> int:
    for iid in args.instances:
        d = extract_mod.extract(iid).to_dict()
        d["prompt"] = f"<{len(d['prompt'])} chars> {d['prompt'][:160]!r}"
        print(json.dumps(d, indent=1))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("instances", nargs="+")
    r = sub.add_parser("run")
    r.add_argument("--instances", nargs="+", required=True)
    r.add_argument("--arm-a", default="current")
    r.add_argument("--arm-b", default=None)
    r.add_argument("--k", type=int, default=1)
    r.add_argument("--budget", type=float, default=20.0, help="stop starting runs past this total $")
    r.add_argument("--per-run", type=float, default=8.0, help="--max-budget-usd for each run")
    r.add_argument("--timeout", type=int, default=3600, help="seconds per run")
    r.add_argument("--max-parallel", type=int, default=2)
    r.add_argument("--suite", action="store_true", help="also run scripts/run_harnesses.py per replicate")
    r.add_argument("--run-id", default=None)
    rp = sub.add_parser("report")
    rp.add_argument("run_dir")
    sc = sub.add_parser("score")
    sc.add_argument("run_dir")
    sc.add_argument("--suite", action="store_true")
    c = sub.add_parser("clean")
    c.add_argument("run_dir")
    c.add_argument("--all", action="store_true", help="also delete the results")
    args = ap.parse_args(argv)
    return {"run": cmd_run, "report": cmd_report, "score": cmd_score, "clean": cmd_clean,
            "extract": cmd_extract}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
