"""Drill for the per-repo test-suite mutex hook (bot/claude/hooks/test_mutex.py).

Simulates two parallel sessions contending for one repo's test lock by
invoking the hook script as a real subprocess (same way Claude Code
does), with a fake repo layout in a temp dir and fast timings via env
overrides. Also exercises the runner-side release backstop.

Run: python scripts/test_mutex_drill.py
Exit 0 = all scenarios pass, non-zero = failure (message on stdout).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "bot" / "claude" / "hooks" / "test_mutex.py"

FAST_ENV = {
    **os.environ,
    "TEST_MUTEX_WAIT_SECS": "2",
    "TEST_MUTEX_POLL_SECS": "0.2",
    "TEST_MUTEX_STALE_SECS": "1800",
    "TEST_MUTEX_GRACE_SECS": "60",
}

_passed = 0


def run_hook(phase: str, wt: Path, repo: Path, command: str,
             env: dict | None = None) -> subprocess.CompletedProcess:
    envelope = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    return subprocess.run(
        [sys.executable, str(HOOK), phase, str(wt), str(repo)],
        input=envelope, capture_output=True, text=True,
        env=env or FAST_ENV, timeout=30,
    )


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed
    if not cond:
        print(f"FAIL: {name}" + (f" — {detail}" if detail else ""))
        sys.exit(1)
    _passed += 1
    print(f"  ok: {name}")


def lock_dir(repo: Path) -> Path:
    return repo / ".worktrees" / ".test-mutex"


def owner_of(repo: Path) -> str:
    try:
        return json.loads((lock_dir(repo) / "owner.json").read_text())["worktree"]
    except (OSError, ValueError, KeyError):
        return ""


def main() -> None:
    if not HOOK.is_file():
        print(f"FAIL: hook script missing at {HOOK}")
        sys.exit(1)

    tmp = Path(tempfile.mkdtemp(prefix="mutex-drill-"))
    try:
        repo = tmp / "repo"
        wt_a = repo / ".worktrees" / "t-aaa"
        wt_b = repo / ".worktrees" / "t-bbb"
        for d in (wt_a, wt_b):
            d.mkdir(parents=True)

        print("scenario: non-test command passes through")
        r = run_hook("pre", wt_a, repo, "git status && ls -la")
        check("non-test allowed", r.returncode == 0, r.stderr)
        check("no lock taken", not lock_dir(repo).exists())

        print("scenario: session A acquires on a test command")
        r = run_hook("pre", wt_a, repo, "dotnet test MyProj.sln --logger trx")
        check("A acquires", r.returncode == 0, r.stderr)
        check("owner is A", owner_of(repo) == str(wt_a), owner_of(repo))

        print("scenario: session A is re-entrant")
        r = run_hook("pre", wt_a, repo, "pytest -x tests/")
        check("A re-enters", r.returncode == 0, r.stderr)

        print("scenario: session B waits then blocks with a reason")
        t0 = time.time()
        r = run_hook("pre", wt_b, repo, "npm test")
        waited = time.time() - t0
        check("B blocked (exit 2)", r.returncode == 2, f"rc={r.returncode}")
        check("B waited ~WAIT_SECS", 1.5 <= waited <= 15, f"{waited:.1f}s")
        check("reason names holder", "t-aaa" in r.stderr, r.stderr)

        print("scenario: post-phase release by non-holder is a no-op")
        r = run_hook("post", wt_b, repo, "npm test")
        check("B post exits 0", r.returncode == 0, r.stderr)
        check("lock still held by A", owner_of(repo) == str(wt_a))

        print("scenario: post-phase release by holder frees the lock")
        r = run_hook("post", wt_a, repo, "dotnet test MyProj.sln")
        check("A post exits 0", r.returncode == 0, r.stderr)
        check("lock released", not lock_dir(repo).exists())

        print("scenario: B acquires after release")
        r = run_hook("pre", wt_b, repo, "cargo test --all")
        check("B acquires", r.returncode == 0, r.stderr)
        check("owner is B", owner_of(repo) == str(wt_b))

        print("scenario: stale lock (old timestamp) is stolen")
        meta = json.loads((lock_dir(repo) / "owner.json").read_text())
        meta["acquired_at"] = time.time() - 99999
        (lock_dir(repo) / "owner.json").write_text(json.dumps(meta))
        r = run_hook("pre", wt_a, repo, "go test ./...")
        check("A steals stale lock", r.returncode == 0, r.stderr)
        check("owner is A again", owner_of(repo) == str(wt_a))

        print("scenario: holder worktree deleted -> lock stolen")
        # A holds; delete A's worktree dir (merged/discarded), B contends.
        shutil.rmtree(wt_a)
        r = run_hook("pre", wt_b, repo, "pytest")
        check("B steals dead-holder lock", r.returncode == 0, r.stderr)
        check("owner is B", owner_of(repo) == str(wt_b))
        run_hook("post", wt_b, repo, "pytest")
        wt_a.mkdir(parents=True)

        print("scenario: B's wait resolves when A releases mid-wait")
        run_hook("pre", wt_a, repo, "pytest")
        envelope = json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "pytest"}}
        )
        proc_b = subprocess.Popen(
            [sys.executable, str(HOOK), "pre", str(wt_b), str(repo)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=FAST_ENV,
        )
        proc_b.stdin.write(envelope)
        proc_b.stdin.close()
        time.sleep(0.6)
        run_hook("post", wt_a, repo, "pytest")  # A finishes mid-wait
        rc_b = proc_b.wait(timeout=30)
        check("B acquires once A releases", rc_b == 0, proc_b.stderr.read())
        check("owner is B after handoff", owner_of(repo) == str(wt_b))
        run_hook("post", wt_b, repo, "pytest")

        print("scenario: per-repo opt-out via .claude/parallel.json")
        cfg_dir = repo / ".claude"
        cfg_dir.mkdir()
        (cfg_dir / "parallel.json").write_text('{"test_mutex": false}')
        r = run_hook("pre", wt_a, repo, "dotnet test Everything.sln")
        check("opt-out allows", r.returncode == 0, r.stderr)
        check("opt-out takes no lock", not lock_dir(repo).exists())

        print("scenario: extra_test_patterns extends matching")
        (cfg_dir / "parallel.json").write_text(
            '{"extra_test_patterns": ["run-integration\\\\.sh"]}'
        )
        r = run_hook("pre", wt_a, repo, "bash ./run-integration.sh")
        check("custom pattern locks", r.returncode == 0 and lock_dir(repo).exists())
        run_hook("post", wt_a, repo, "bash ./run-integration.sh")
        (cfg_dir / "parallel.json").unlink()

        print("scenario: runner-side release backstop")
        run_hook("pre", wt_a, repo, "pytest")
        check("A holds before backstop", owner_of(repo) == str(wt_a))
        sys.path.insert(0, str(ROOT))
        from bot.claude.runner import ClaudeRunner
        ClaudeRunner._release_test_mutex(str(repo), str(wt_b))
        check("non-holder backstop no-op", lock_dir(repo).exists())
        ClaudeRunner._release_test_mutex(str(repo), str(wt_a))
        check("holder backstop releases", not lock_dir(repo).exists())

        print(f"\nALL PASS ({_passed} checks)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
