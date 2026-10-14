"""Score one finished replicate against the shipped solution.

Everything is deterministic except the optional judge:

  fail_to_pass  the harnesses the shipped solution added or changed, copied
                in from the reference tip *after* the session has ended and
                run against the session's tree. This is the headline: did the
                session build something the real tests accept.
  suite         optionally, ``scripts/run_harnesses.py`` in the clone
  lint          ruff critical-errors-only, when ruff is installed
  own_tests     the harnesses the session wrote, on its own tree
  diff          files and lines the session changed, and how far its set of
                touched files overlaps the reference's (Jaccard). Overlap is
                context, not correctness: a different fix can be right.
  stream        turns, tools, tokens, cost, compactions, wall time

The reference tests are copied in only for scoring and the session never saw
them: the clone has no object past the base commit, and the run's sandbox
hid every other copy (``run.hidden_paths``). They go in last, after lint and
the suite, so neither of those ever measures the reference's code as the
session's.

Every harness runs in the same sandbox, plus its own pid namespace so a
timeout kills the whole tree, with ``PYTHONPATH`` set to the clone: the
venv's editable install of the bot points at the live checkout, and an
import that fell through to it would score the shipped solution instead of
the session's tree.

TODO: a ``--review`` pass (the bot's own review-code prompt over the
session's diff) would score code quality the tests cannot see. Deferred: it
is another full CLI run per replicate and the deterministic scores are
what the first rounds need.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

from common import git, read_json, venv_python, write_json
import run as run_mod

TEST_TIMEOUT_S = 600


def snapshot(repo: Path) -> str:
    """Commit whatever the session left uncommitted, so the diff is complete."""
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "--no-verify", "-m", "replay: snapshot",
        identity=True)
    return git(repo, "rev-parse", "HEAD")


def restore(repo: Path, sha: str) -> None:
    """Put the tree back to ``sha``, dropping anything a scoring step wrote."""
    git(repo, "reset", "-q", "--hard", sha)
    git(repo, "clean", "-fdq")


def _changed(repo: Path, a: str, b: str) -> tuple[set[str], int, int]:
    files = set(git(repo, "diff", "--name-only", f"{a}..{b}", check=False).splitlines())
    added = deleted = 0
    for line in git(repo, "diff", "--numstat", f"{a}..{b}", check=False).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            added += int(parts[0])
            deleted += int(parts[1])
    return {f for f in files if f}, added, deleted


def diff_stats(repo: Path, start: str, end: str, live_repo: Path, base: str, tip: str) -> dict:
    mine, add, rem = _changed(repo, start, end)
    ref, radd, rrem = _changed(live_repo, base, tip)
    union = mine | ref
    return {
        "files": sorted(mine), "lines_added": add, "lines_deleted": rem,
        "ref_files": sorted(ref), "ref_lines_added": radd, "ref_lines_deleted": rrem,
        "file_jaccard": round(len(mine & ref) / len(union), 3) if union else 1.0,
    }


# The clone has no .env, and bot.config refuses to import without Discord
# settings. Placeholders, never the real ones: a harness must not reach Discord.
DUMMY_ENV = {
    "DISCORD_BOT_TOKEN": "replay-ab-placeholder",
    "DISCORD_GUILD_ID": "1",
    "DISCORD_LOBBY_CHANNEL_ID": "1",
}


def _run_py(repo: Path, args: list[str], timeout: int) -> dict:
    """Run a harness in the clone, sandboxed like the session was, in its own pid namespace."""
    env = run_mod.child_env("")
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.update(DUMMY_ENV, BOT_PATHS_DISABLED="1", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=str(repo))
    cmd = run_mod.sandbox_args(repo, contain=True) + ["--", venv_python(), *args]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=str(repo), env=env,
                           capture_output=True, text=True, timeout=timeout)
        rc, out = p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired as e:
        rc, out = -1, f"timeout after {timeout}s\n{(e.stdout or '')}"
    return {"rc": rc, "passed": rc == 0, "secs": round(time.time() - t0, 1),
            "tail": out[-1500:] if isinstance(out, str) else ""}


def fail_to_pass(repo: Path, live_repo: Path, tip: str, tests: list[str]) -> dict:
    """Copy the shipped harnesses into the session's tree and run each one."""
    results = {}
    for rel in tests:
        try:
            body = git(live_repo, "show", f"{tip}:{rel}", strip=False)
        except RuntimeError as e:
            results[rel] = {"rc": None, "passed": False, "kind": "missing",
                            "tail": f"not in reference: {e}"}
            continue
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(body, encoding="utf-8")
        res = _run_py(repo, [rel], TEST_TIMEOUT_S)
        res["kind"] = classify(res)
        results[rel] = res
    n = len(results)
    return {"tests": results, "passed": sum(1 for r in results.values() if r["passed"]),
            "interface": sum(1 for r in results.values() if r.get("kind") == "interface"),
            "total": n, "all_passed": n > 0 and all(r["passed"] for r in results.values())}


_INTERFACE_RE = re.compile(
    r"^(ImportError: cannot import name|ModuleNotFoundError: No module named"
    r"|AttributeError: module '[^']+' has no attribute)", re.M)


def classify(res: dict) -> str:
    """pass, fail, or interface: the test could not even reach the behaviour.

    A shipped harness imports the helpers its own build happened to add. A
    replay that fixes the same thing under other names fails it on import,
    which says nothing about whether the behaviour is right. t-9086 was the
    first case: the reference test imports ``format_elapsed`` from
    ``bot.platform.formatting``, and the replay put the same rounding inline.
    """
    if res.get("passed"):
        return "pass"
    return "interface" if _INTERFACE_RE.search(res.get("tail") or "") else "fail"


def own_tests(repo: Path, start: str, end: str) -> dict:
    """The harnesses the session itself added or changed, run on its own tree.

    Self-consistency, not correctness: a session can write a test that agrees
    with a wrong fix. Read alongside fail_to_pass, never instead of it.
    """
    out = git(repo, "diff", "--name-only", "--diff-filter=AM", f"{start}..{end}", check=False)
    tests = sorted(p for p in out.splitlines() if re.match(r"scripts/(test|verify)_[^/]+\.py$", p))
    results = {rel: _run_py(repo, [rel], TEST_TIMEOUT_S) for rel in tests}
    return {"tests": results, "passed": sum(1 for r in results.values() if r["passed"]),
            "total": len(results)}


def lint(repo: Path, files: list[str]) -> dict | None:
    ruff = shutil.which("ruff")
    py = [f for f in files if f.endswith(".py") and (repo / f).exists()]
    if not ruff or not py:
        return None
    p = subprocess.run([ruff, "check", "--select", "E9,F63,F7,F82", "--no-cache", *py],
                       cwd=str(repo), capture_output=True, text=True, timeout=120)
    return {"rc": p.returncode, "passed": p.returncode == 0, "tail": (p.stdout + p.stderr)[-1500:]}


def score(rep_dir: Path, case, *, start_sha: str, suite: bool = False) -> dict:
    """Score the replicate in ``rep_dir`` (its clone is ``rep_dir/repo``)."""
    repo = rep_dir / "repo"
    live = Path(case.repo_path)
    run = read_json(rep_dir / "run.json", {}) or {}
    # The session's end state is pinned the first time. A re-score goes back to
    # that commit rather than snapshotting again, so nothing a scoring pass
    # left in the tree (the reference harnesses, test output) can ever be
    # committed as the session's work. Exactly that happened on the first
    # t-9086 spike, before this pin existed.
    end = (read_json(rep_dir / "score.json", {}) or {}).get("snapshot_sha")
    if end:
        restore(repo, end)
    else:
        end = snapshot(repo)
    diff = diff_stats(repo, start_sha, end, live, case.base_sha, case.ref_tip)
    # The session's own tree first: its tests, lint, and the suite. The
    # reference harnesses go in last, because they overwrite a session test
    # of the same name and would otherwise be linted and suite-run as its work.
    own = own_tests(repo, start_sha, end)
    lint_res = lint(repo, diff["files"])
    suite_res = None
    if suite:
        suite_res = _run_py(repo, ["scripts/run_harnesses.py"], 3600)
        restore(repo, end)
    f2p = fail_to_pass(repo, live, case.ref_tip, case.ref_tests)
    out = {
        "label": run.get("label"),
        "run_ok": run.get("returncode") == 0 and not run.get("is_error") and not run.get("timed_out"),
        "resume_failed": run.get("resume_failed"),
        "diff": diff,
        "own_tests": own,
        "fail_to_pass": f2p,
        "lint": lint_res,
        "suite": suite_res,
        "snapshot_sha": end,
        "stream": {k: run.get(k) for k in (
            "num_turns", "total_cost_usd", "wall_s", "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_creation_tokens", "compactions", "tools", "peeks")},
    }
    # Back to the snapshot: the reference harnesses and whatever the test
    # runs wrote must not read as the session's work on a re-score.
    restore(repo, end)
    write_json(rep_dir / "score.json", out)
    return out
