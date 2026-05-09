"""PreToolUse hook: block ops against the main repo from a worktree.

Runs as a standalone subprocess invoked by Claude Code's hook system.
Reads the proposed tool invocation from stdin (JSON envelope), inspects
it against a narrow denylist scoped to genuinely dangerous reachable
failure modes, and either allows (exit 0) or blocks (exit 2 with reason
on stderr — Claude Code's documented contract for blocking a tool call
and feeding the reason back to the model).

Hook protocol (verified against Claude Code 2.1.123):
  stdin: JSON like {"tool_name": "Edit", "tool_input": {"file_path": "..."}}
  exit 0 + empty stdout/stderr -> allow
  exit 2 + reason on stderr     -> block, reason fed to the model
  any other exit                -> Claude Code logs and treats as allow
                                   (so this hook fails open on bugs)

Configured per-worktree by the bot writing
``.claude/settings.local.json`` referencing this script by absolute
path. Paths are passed as positional argv (worktree path then repo
path) to avoid Claude Code's hook-env scrubbing; env vars are also
honored as a fallback for local manual runs.

Covered tools:
  Bash         — git -C <repo>, --git-dir/--work-tree, rm -r <repo>,
                 git worktree remove <self>
  Edit, Write,
  MultiEdit    — file_path under main repo (but NOT under worktree)
  NotebookEdit — notebook_path under main repo

Why both worktree and repo paths: the worktree lives *inside* the main
repo (under .worktrees/), so a substring check against repo_path alone
would block edits inside the worktree itself. Allow when the path is
under the worktree; block when it's under the repo but not the worktree.
"""

from __future__ import annotations

import json
import os
import re
import sys


def _norm(p):
    return p.replace("\\", "/").rstrip("/")


def _is_windows():
    return os.name == "nt"


def _path_eq_or_under(candidate, parent):
    if not parent or not candidate:
        return False
    c = _norm(candidate)
    p = _norm(parent)
    if _is_windows():
        c = c.lower()
        p = p.lower()
    return c == p or c.startswith(p + "/")


def _block(reason):
    sys.stderr.write(reason)
    sys.stderr.flush()
    sys.exit(2)


def _allow():
    sys.exit(0)


def _is_against_main_repo_bash(command, repo_path, worktree_path):
    if not repo_path:
        return False
    cmd_norm = command.replace("\\", "/")

    def _hits_repo_not_wt(target):
        # Strip surrounding quotes that the regex captured along with the path.
        target = target.strip().strip('"').strip("'")
        if not target:
            return False
        # Worktree lives *inside* the main repo, so check worktree first —
        # an edit equal-to-or-under the worktree is allowed even though it
        # would also match the main-repo prefix check.
        if worktree_path and _path_eq_or_under(target, worktree_path):
            return False
        return _path_eq_or_under(target, repo_path)

    for m in re.finditer(r"\bgit\s+-C\s+(\S+)", cmd_norm):
        if _hits_repo_not_wt(m.group(1)):
            return True
    for m in re.finditer(r"--(?:git-dir|work-tree)=(\S+)", cmd_norm):
        if _hits_repo_not_wt(m.group(1)):
            return True
    for m in re.finditer(r"\brm\s+((?:-\S+\s+)+)(\S+)", cmd_norm):
        flags_str, target = m.group(1), m.group(2)
        if "r" not in flags_str.lower():
            continue
        if _hits_repo_not_wt(target):
            return True
    return False


def _is_self_worktree_remove(command, worktree_path):
    if not worktree_path:
        return False
    cmd_norm = command.replace("\\", "/")
    for m in re.finditer(r"\bgit\s+worktree\s+remove\s+(?:-\S+\s+)*(\S+)", cmd_norm):
        target = m.group(1).strip().strip('"').strip("'")
        if _path_eq_or_under(target, worktree_path):
            return True
    return False


def _evaluate_bash(command, worktree_path, repo_path):
    if _is_self_worktree_remove(command, worktree_path):
        return (
            "Refusing to `git worktree remove` the current worktree — "
            "that would delete your CWD mid-run. If you really want this "
            "session ended, ask the user to use the bot's Discard button."
        )
    if _is_against_main_repo_bash(command, repo_path, worktree_path):
        return (
            f"Refusing destructive operation against the main repo at "
            f"`{repo_path}`. The main repo must not be touched so parallel "
            f"builds on it aren't disrupted. Operate inside the worktree at "
            f"`{worktree_path}` instead."
        )
    return None


def _evaluate_file_edit(tool_name, file_path, worktree_path, repo_path):
    if not file_path:
        return None
    if worktree_path and _path_eq_or_under(file_path, worktree_path):
        return None
    if repo_path and _path_eq_or_under(file_path, repo_path):
        return (
            f"Refusing {tool_name} on `{file_path}` — that path is in the "
            f"main repo (`{repo_path}`), not the worktree (`{worktree_path}`). "
            f"This usually means the session resumed planning context that "
            f"referenced main-repo paths. Re-target the edit to the equivalent "
            f"path inside the worktree instead."
        )
    return None


def _extract_paths_from_input(tool_name, tool_input):
    paths = []
    if tool_name in ("Edit", "Write", "MultiEdit"):
        fp = tool_input.get("file_path")
        if isinstance(fp, str) and fp:
            paths.append(fp)
    elif tool_name == "NotebookEdit":
        np = tool_input.get("notebook_path")
        if isinstance(np, str) and np:
            paths.append(np)
    return paths


def main():
    if len(sys.argv) >= 3:
        worktree_path = sys.argv[1]
        repo_path = sys.argv[2]
    else:
        worktree_path = os.environ.get("CLAUDE_BOT_WORKTREE", "")
        repo_path = os.environ.get("CLAUDE_BOT_REPO_PATH", "")

    if not worktree_path or not repo_path:
        _allow()

    try:
        envelope = json.load(sys.stdin)
    except Exception:
        _allow()

    tool_name = envelope.get("tool_name", "")
    tool_input = envelope.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        _allow()

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if not isinstance(command, str) or not command.strip():
            _allow()
        reason = _evaluate_bash(command, worktree_path, repo_path)
        if reason:
            _block(reason)
        _allow()

    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        for path in _extract_paths_from_input(tool_name, tool_input):
            reason = _evaluate_file_edit(tool_name, path, worktree_path, repo_path)
            if reason:
                _block(reason)
        _allow()

    _allow()


if __name__ == "__main__":
    main()
