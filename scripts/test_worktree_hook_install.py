#!/usr/bin/env python3
"""Harness: build worktrees get the guard and test-mutex hooks installed.

When runner.py became a package (63cf100, 2026-10-12), the hook install
moved one folder deeper and kept resolving the scripts next to itself, in a
``bot/claude/runner/hooks/`` that does not exist. Every build worktree from
then on ran with no guard and no test mutex, and the only trace was a
WARNING line per build. Nothing exercised the install, so this does:

- The hooks folder the installer reads holds both scripts.
- Installing into a real ``git worktree add``'d tree writes a
  ``settings.local.json`` whose guard and mutex commands name scripts that
  exist, and logs no "not found" warning.
- The installed guard command runs as written: it allows a Write inside
  the worktree and blocks one into the main repo. A path that exists but a
  command line that does not run would be the same silent failure.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import json
import logging
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import config  # noqa: E402
from bot.claude.runner import worktrees as wt_mod  # noqa: E402
from bot.claude.runner import ClaudeRunner  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _run_hook(command: str, tool_input: dict) -> int:
    """Run an installed hook command the way Claude Code does: through a
    shell, with the tool call as JSON on stdin. Returns the exit code."""
    payload = json.dumps({"tool_name": "Write", "tool_input": tool_input})
    return subprocess.run(command, shell=True, input=payload, text=True,
                          capture_output=True, timeout=30).returncode


def _script_paths(entry: dict) -> list[Path]:
    """The .py paths a hook entry's commands point at."""
    out = []
    for h in entry.get("hooks", []):
        for tok in shlex.split(h.get("command", "")):
            if tok.endswith(".py"):
                out.append(Path(tok))
    return out


def check_hooks_dir() -> None:
    hooks_dir = wt_mod._HOOKS_DIR
    check("hooks dir is bot/claude/hooks",
          hooks_dir == ROOT / "bot" / "claude" / "hooks", str(hooks_dir))
    for name in ("worktree_guard.py", "test_mutex.py"):
        check(f"hooks dir holds {name}", (hooks_dir / name).is_file(), str(hooks_dir))


def _check_settings(repo: Path, wt_dir: Path) -> None:
    settings = wt_dir / ".claude" / "settings.local.json"
    check("settings.local.json written", settings.is_file())
    if not settings.is_file():
        return
    hooks = json.loads(settings.read_text(encoding="utf-8")).get("hooks", {})

    guard = [e for e in hooks.get("PreToolUse", [])
             if e.get("_owner") == "claude-telegram-bot"]
    check("guard entry installed", len(guard) == 1, f"{len(guard)} entries")
    if guard:
        paths = _script_paths(guard[0])
        check("guard command names an existing worktree_guard.py",
              any(p.name == "worktree_guard.py" and p.is_file() for p in paths),
              str(paths))
        command = guard[0]["hooks"][0]["command"]
        rc = _run_hook(command, {"file_path": str(wt_dir / "ok.txt")})
        check("installed guard allows a write inside the worktree", rc == 0, f"exit {rc}")
        rc = _run_hook(command, {"file_path": str(repo / "leak.txt")})
        check("installed guard blocks a write into the main repo", rc == 2, f"exit {rc}")

    for event in ("PreToolUse", "PostToolUse"):
        mutex = [e for e in hooks.get(event, [])
                 if e.get("_owner") == "claude-telegram-bot-test-mutex"]
        check(f"test-mutex {event} entry installed", len(mutex) == 1,
              f"{len(mutex)} entries")
        if mutex:
            paths = _script_paths(mutex[0])
            check(f"test-mutex {event} command names an existing test_mutex.py",
                  any(p.name == "test_mutex.py" and p.is_file() for p in paths),
                  str(paths))


def check_install() -> None:
    cap = _Capture()
    logger = logging.getLogger(wt_mod.__name__)
    logger.addHandler(cap)
    saved_mutex = config.TEST_MUTEX_ENABLED
    config.TEST_MUTEX_ENABLED = True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            _git("init", "-q", "-b", "master", cwd=repo)
            _git("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                 "commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
            wt_dir = repo / ".worktrees" / "t-1"
            _git("worktree", "add", "-q", "-b", "claude-bot/t-1", str(wt_dir), cwd=repo)

            ClaudeRunner._install_worktree_hook(str(repo), str(wt_dir))
            _check_settings(repo, wt_dir)
    finally:
        config.TEST_MUTEX_ENABLED = saved_mutex
        logger.removeHandler(cap)

    not_found = [m for m in cap.messages if "not found" in m]
    check("no 'not found' warning logged", not not_found, "; ".join(not_found))


def main() -> int:
    check_hooks_dir()
    check_install()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
