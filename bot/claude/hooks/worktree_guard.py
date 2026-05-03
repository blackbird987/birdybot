"""PreToolUse hook: block destructive ops against the main repo from a worktree.

Part of the t-3541 Layer 3 enforcement.  Runs as a standalone subprocess
invoked by Claude Code's hook system.  Reads the proposed Bash command
from stdin (JSON envelope), inspects it against a narrow denylist scoped
to the genuinely dangerous reachable failure modes (per Probe B Part 2),
and either allows or blocks with a structured reason.

Hook protocol (Claude Code PreToolUse) — UNVERIFIED, see Probe B Part 1:
  stdin:  JSON like {"tool_name": "Bash", "tool_input": {"command": "..."}}
  stdout: JSON {"decision": "allow"} or {"decision": "block", "reason": "..."}

The exact field names and expected response format above are best-effort
guesses based on prior-art hook conventions.  Probe B Part 1 must
empirically verify (a) that Claude Code passes this envelope shape,
(b) that it parses this response shape, and (c) that env vars on the
hook entry actually reach the subprocess.  The hook fails open on any
unrecognized envelope so guess-mismatch produces a no-op rather than a
crash, but enabling WORKTREE_HOOK_ENABLED without verification will
result in zero protection.

The hook is configured per-worktree by the bot writing
``.claude/settings.local.json`` into the worktree root.  The settings file
references this script by absolute path.  Two env vars supplied at install:

  CLAUDE_BOT_WORKTREE   — absolute path of the worktree this hook is for
  CLAUDE_BOT_REPO_PATH  — absolute path of the main repo (the worktree's parent)

If those env vars aren't present (e.g. user invoked the hook outside the
bot), the hook fails open (allow) so it can't accidentally lock people
out of their own machines.

NOTE: This is scaffolding.  ``WORKTREE_HOOK_ENABLED`` must stay off
until Probe B Part 1 (does Claude Code load project hooks under
CLAUDE_CONFIG_DIR?) and Part 2 (which ops are actually reachable?) are
empirically verified — see bot/config.py for the procedure.
"""

from __future__ import annotations

import json
import os
import re
import sys


def _decision(decision: str, reason: str = "") -> dict:
    payload: dict = {"decision": decision}
    if reason:
        payload["reason"] = reason
    return payload


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()


def _path_in(haystack: str, needle: str) -> bool:
    """Case-insensitive substring match for filesystem paths.

    Windows paths are case-insensitive; Linux/macOS are case-sensitive.
    Matching loosely catches both — false positives here mean blocking a
    legitimate command that happens to contain a case variant of the
    path, which is rare; false negatives mean failing to block a real
    threat, which is worse.
    """
    if not needle:
        return False
    return needle.lower() in haystack.lower()


def _is_against_main_repo(command: str, repo_path: str) -> bool:
    """True if the command targets the main repo path via -C, --git-dir, or
    an absolute path argument."""
    if not repo_path:
        return False
    # Normalize separators only.  Keep flag case (so `-C` ≠ `-c`); paths
    # are matched case-insensitively via _path_in.
    cmd_norm = command.replace("\\", "/")
    needle = repo_path.replace("\\", "/").rstrip("/")
    # `git -C <repo>` — `-C` is case-sensitive in git (lowercase `-c` is
    # `-c key=value`, an unrelated flag we must NOT block).  Locate the
    # `-C` token, then check the following arg matches the repo path.
    for m in re.finditer(r"\bgit\s+-C\s+(\S+)", cmd_norm):
        if _path_in(m.group(1), needle):
            return True
    # `--git-dir=<repo>/.git` (and similar `--work-tree=<repo>`).
    for m in re.finditer(r"--(?:git-dir|work-tree)=(\S+)", cmd_norm):
        if _path_in(m.group(1), needle):
            return True
    # `rm <flags> <target>` — destruction targeting the main repo via
    # absolute path.  Block when at least one flag contains `r` (-r,
    # -rf, -fr, -r --no-preserve-root, etc.) AND the target contains
    # the repo path.  Allowing the flags portion to be one-or-more
    # generic flag tokens covers the common multi-flag forms like
    # `rm -r -f <path>` that a strict per-token "must contain r"
    # regex would miss.
    for m in re.finditer(r"\brm\s+((?:-\S+\s+)+)(\S+)", cmd_norm):
        flags_str, target = m.group(1), m.group(2)
        if "r" not in flags_str.lower():
            continue
        if _path_in(target, needle):
            return True
    return False


def _is_self_worktree_remove(command: str, worktree_path: str) -> bool:
    """True if the command tries to remove THIS worktree (self-deletion).

    `git worktree remove <this>` would yank the LLM's own CWD out from
    under it mid-run — git allows this and the resulting state confuses
    everything downstream.
    """
    if not worktree_path:
        return False
    cmd_norm = command.replace("\\", "/")
    needle = worktree_path.replace("\\", "/").rstrip("/")
    for m in re.finditer(r"\bgit\s+worktree\s+remove\s+(?:-\S+\s+)*(\S+)", cmd_norm):
        if _path_in(m.group(1), needle):
            return True
    return False


def _evaluate(command: str, worktree_path: str, repo_path: str) -> dict:
    if _is_self_worktree_remove(command, worktree_path):
        return _decision(
            "block",
            "Refusing to `git worktree remove` the current worktree — "
            "that would delete your CWD mid-run. If you really want this "
            "session ended, ask the user to use the bot's Discard button.",
        )
    if _is_against_main_repo(command, repo_path):
        # Don't name a specific branch (master/main/etc.) — this hook
        # has no access to instance.original_branch and would lie to
        # the LLM if it guessed wrong.  Generic phrasing keeps the
        # reason accurate across repos.
        return _decision(
            "block",
            f"Refusing destructive operation against the main repo at "
            f"`{repo_path}`. The main repo must not be touched so parallel "
            f"builds on it aren't disrupted. Operate inside the worktree at "
            f"`{worktree_path}` instead.",
        )
    return _decision("allow")


def main() -> None:
    worktree_path = os.environ.get("CLAUDE_BOT_WORKTREE", "")
    repo_path = os.environ.get("CLAUDE_BOT_REPO_PATH", "")

    # Fail open if invoked without the bot's env wiring — the hook is a
    # bot-managed protection layer, not a general-purpose safety net.
    if not worktree_path or not repo_path:
        _emit(_decision("allow"))
        return

    try:
        envelope = json.load(sys.stdin)
    except Exception:
        _emit(_decision("allow"))
        return

    if envelope.get("tool_name") != "Bash":
        _emit(_decision("allow"))
        return

    command = (envelope.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str) or not command.strip():
        _emit(_decision("allow"))
        return

    _emit(_evaluate(command, worktree_path, repo_path))


if __name__ == "__main__":
    main()
