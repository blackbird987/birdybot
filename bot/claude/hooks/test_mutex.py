"""Per-repo test-suite mutex hook: serialize full test runs across sessions.

Parallel build sessions get file isolation from git worktrees, but they
share one machine — full test suites launched concurrently fight over
fixed localhost ports, shared databases at absolute paths, and CPU
(observed: five parallel `dotnet test` runs starving each other into
orphaned/hung runs — the t-5976 incident). This hook serializes
test-suite commands per repo while leaving everything else parallel.

Runs as a standalone subprocess invoked by Claude Code's hook system,
in two phases (argv[1]):

  pre   (PreToolUse)  — if the Bash command is a test-suite run, acquire
                        the repo's test lock. If another session holds
                        it, wait up to WAIT_SECS for it to free, then
                        block (exit 2 + reason on stderr) so the model
                        can do other work and retry.
  post  (PostToolUse) — release the lock if this session holds it and
                        the completed command was a test-suite run.

Hook protocol (same contract as worktree_guard.py, verified against
Claude Code 2.1.123):
  stdin: JSON like {"tool_name": "Bash", "tool_input": {"command": "..."}}
  exit 0  -> allow / no-op
  exit 2 + reason on stderr -> block (pre phase only; reason fed to model)
  any other exit -> Claude Code treats as allow (fails open on bugs)

Lock design:
  {repo}/.worktrees/.test-mutex/     — atomic os.mkdir() = acquire
  {repo}/.worktrees/.test-mutex/owner.json — holder metadata:
      {"worktree": "...", "acquired_at": <epoch>, "command": "..."}
  Owner identity is the worktree path (worktree dir name == instance id).
  Re-entrant: a session that already holds the lock passes through and
  refreshes the timestamp.

Stale-lock recovery (a crashed holder must not wedge the repo):
  - owner.json older than STALE_TTL_SECS -> steal
  - owner's worktree directory no longer exists -> steal
  - owner.json unreadable/missing and lock dir older than a short
    grace period (mkdir-to-write race window) -> steal
  The bot also releases the lock in the runner's post-run path, so a
  killed session frees the lock as soon as its CLI process exits.

Per-repo config (read from the MAIN repo root so a build branch cannot
rewrite its own rules): {repo}/.claude/parallel.json
  {"test_mutex": false}              — disable the mutex for this repo
  {"test_patterns": ["regex", ...]}  — replace the default patterns
  {"extra_test_patterns": [...]}     — extend the default patterns

Paths are passed as positional argv (phase, worktree, repo) — same
rationale as worktree_guard.py: Claude Code does not reliably propagate
settings env blocks to hook subprocesses. WAIT/TTL are env-overridable
(TEST_MUTEX_WAIT_SECS / TEST_MUTEX_STALE_SECS) for the drill script.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time

# How long the pre-phase waits for a busy lock before blocking the tool
# call. Must stay below the hook `timeout` in settings.local.json (the
# runner installs 150s) so *we* decide the outcome, not the hook reaper.
WAIT_SECS = int(os.environ.get("TEST_MUTEX_WAIT_SECS", "120"))
POLL_SECS = float(os.environ.get("TEST_MUTEX_POLL_SECS", "5"))
# A holder older than this is presumed dead (crash without cleanup).
STALE_TTL_SECS = int(os.environ.get("TEST_MUTEX_STALE_SECS", "1800"))
# Lock dir exists but owner.json doesn't (mkdir-to-write race window, or
# a crash between the two): give the writer this long before stealing.
NO_OWNER_GRACE_SECS = int(os.environ.get("TEST_MUTEX_GRACE_SECS", "60"))

# Commands that count as "a test-suite run". False positives are cheap
# (they just serialize); misses leave that runner parallel.
DEFAULT_TEST_PATTERNS = [
    r"\bdotnet\s+test\b",
    r"\bpytest\b",
    r"\bpython(?:3|w)?(?:\.exe)?\s+-m\s+unittest\b",
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test\b",
    r"\bvitest\b",
    r"\bjest\b",
    r"\bplaywright\s+test\b",
    r"\bgo\s+test\b",
    r"\bcargo\s+test\b",
    r"\bctest\b",
    r"\bgradlew?\b.{0,120}\btest\b",
    r"\bmvn\b.{0,120}\btest\b",
]


def _norm(p: str) -> str:
    p = (p or "").replace("\\", "/").rstrip("/")
    return p.lower() if os.name == "nt" else p


def _lock_dir(repo_path: str) -> str:
    return os.path.join(repo_path, ".worktrees", ".test-mutex")


def _load_patterns(repo_path: str) -> list[str] | None:
    """Resolve test patterns for this repo. None means mutex disabled."""
    patterns = list(DEFAULT_TEST_PATTERNS)
    cfg_path = os.path.join(repo_path, ".claude", "parallel.json")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            if cfg.get("test_mutex") is False:
                return None
            replace = cfg.get("test_patterns")
            if isinstance(replace, list) and replace:
                patterns = [str(p) for p in replace]
            extra = cfg.get("extra_test_patterns")
            if isinstance(extra, list):
                patterns.extend(str(p) for p in extra)
    except (OSError, ValueError):
        pass  # no config / bad config -> defaults
    return patterns


def _is_test_command(command: str, patterns: list[str]) -> bool:
    for pat in patterns:
        try:
            if re.search(pat, command, re.IGNORECASE):
                return True
        except re.error:
            continue  # bad user-supplied regex — skip it, keep the rest
    return False


def _read_owner(lock_dir: str) -> dict | None:
    try:
        with open(os.path.join(lock_dir, "owner.json"), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_owner(lock_dir: str, worktree_path: str, command: str) -> None:
    payload = json.dumps({
        "worktree": worktree_path,
        "acquired_at": time.time(),
        "command": command[:200],
    })
    tmp = os.path.join(lock_dir, "owner.json.tmp")
    dst = os.path.join(lock_dir, "owner.json")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.replace(tmp, dst)


def _is_stale(lock_dir: str, owner: dict | None) -> bool:
    now = time.time()
    if owner is None:
        # No readable owner: steal only after a grace period, measured
        # from the lock dir's mtime (covers the mkdir->write race).
        try:
            return now - os.path.getmtime(lock_dir) > NO_OWNER_GRACE_SECS
        except OSError:
            return False  # dir vanished — the acquire loop will retry mkdir
    acquired = owner.get("acquired_at")
    if not isinstance(acquired, (int, float)) or now - acquired > STALE_TTL_SECS:
        return True
    holder_wt = owner.get("worktree")
    if isinstance(holder_wt, str) and holder_wt and not os.path.isdir(holder_wt):
        return True  # holder's worktree was merged/discarded — it's gone
    return False


def _block(reason: str) -> None:
    sys.stderr.write(reason)
    sys.stderr.flush()
    sys.exit(2)


def _allow() -> None:
    sys.exit(0)


def _fmt_age(secs: float) -> str:
    if secs < 90:
        return f"{int(secs)}s"
    return f"{int(secs / 60)}m"


def _acquire(worktree_path: str, repo_path: str, command: str) -> None:
    """Pre phase: acquire the lock or block with a reason."""
    lock_dir = _lock_dir(repo_path)
    deadline = time.time() + WAIT_SECS
    while True:
        try:
            os.makedirs(os.path.dirname(lock_dir), exist_ok=True)
            os.mkdir(lock_dir)  # atomic acquire
            _write_owner(lock_dir, worktree_path, command)
            _allow()
        except FileExistsError:
            pass
        except OSError:
            _allow()  # filesystem trouble — fail open, never wedge builds

        owner = _read_owner(lock_dir)
        holder_wt = (owner or {}).get("worktree")
        if isinstance(holder_wt, str) and _norm(holder_wt) == _norm(worktree_path):
            # Re-entrant: we already hold it (e.g. back-to-back test
            # commands, or a previous run died before release).
            _write_owner(lock_dir, worktree_path, command)
            _allow()

        if _is_stale(lock_dir, owner):
            shutil.rmtree(lock_dir, ignore_errors=True)
            continue  # race back to mkdir; exactly one contender wins

        if time.time() >= deadline:
            holder = os.path.basename(holder_wt) if holder_wt else "another session"
            age = ""
            acquired = (owner or {}).get("acquired_at")
            if isinstance(acquired, (int, float)):
                age = f" (running {_fmt_age(time.time() - acquired)})"
            _block(
                f"Test-suite lock for this repo is held by session "
                f"`{holder}`{age} — full test runs are serialized across "
                f"parallel sessions because they share ports, databases, "
                f"and CPU. Do other work first, or run a NARROW test "
                f"filter (single test class/file) which is not locked, "
                f"then retry the full suite in a few minutes."
            )
        time.sleep(min(POLL_SECS, max(0.1, deadline - time.time())))


def _release(worktree_path: str, repo_path: str) -> None:
    """Post phase: release the lock if we hold it."""
    lock_dir = _lock_dir(repo_path)
    owner = _read_owner(lock_dir)
    holder_wt = (owner or {}).get("worktree")
    if isinstance(holder_wt, str) and _norm(holder_wt) == _norm(worktree_path):
        shutil.rmtree(lock_dir, ignore_errors=True)


def main() -> None:
    if len(sys.argv) < 4:
        _allow()
    phase, worktree_path, repo_path = sys.argv[1], sys.argv[2], sys.argv[3]
    if phase not in ("pre", "post") or not worktree_path or not repo_path:
        _allow()

    try:
        envelope = json.load(sys.stdin)
    except Exception:
        _allow()
    if not isinstance(envelope, dict) or envelope.get("tool_name") != "Bash":
        _allow()
    tool_input = envelope.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        _allow()
    command = tool_input.get("command", "")
    if not isinstance(command, str) or not command.strip():
        _allow()

    patterns = _load_patterns(repo_path)
    if patterns is None or not _is_test_command(command, patterns):
        _allow()

    if phase == "pre":
        _acquire(worktree_path, repo_path, command)
    else:
        _release(worktree_path, repo_path)
    _allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Any bug in this hook must never wedge a build — fail open.
        sys.exit(0)
