"""PreToolUse path allowlist for sandboxed sessions.

Claude Code's sandbox holds for Bash. The file tools run in the CLI's own
process, outside bwrap, so the deny settings do not reach them on their own.
This hook is what stops them. See docs/design/session-sandbox.md.

Usage: sandbox_paths.py <config.json>, tool call JSON on stdin.
Exit 0 allows, exit 2 with a message on stderr blocks.

The settings wrap the command in ``... || exit 2``, because Claude Code treats
any other non-zero exit as an allow. A crash here must never read as one.
"""

from __future__ import annotations

import json
import os
import sys

READ_TOOLS = {"Read", "Grep", "Glob"}
# Never written with a tool, wherever they sit under the working directory.
_NO_WRITE_PARTS = {".git", ".claude"}
_NO_WRITE_NAMES = {".mcp.json"}


def _block(message: str) -> None:
    print(f"Sandbox: {message}", file=sys.stderr)
    sys.exit(2)


def _resolve(raw: str, cwd: str) -> str:
    path = os.path.expanduser(str(raw))
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.realpath(path)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _glob_root(pattern: str) -> str | None:
    """The literal directory an absolute or home-relative glob starts from."""
    if not pattern.startswith(("/", "~")):
        return None
    parts: list[str] = []
    for part in pattern.split("/"):
        if any(ch in part for ch in "*?[{"):
            break
        parts.append(part)
    return "/".join(parts) or "/"


def check(tool: str, tool_input: dict, cfg: dict) -> str | None:
    """Why this call is blocked, or None when it may run."""
    cwd = cfg["cwd"]
    read_roots = [os.path.realpath(p) for p in cfg["read"]]
    write_roots = [os.path.realpath(p) for p in cfg["write"]]
    deny_read = [os.path.realpath(p) for p in cfg.get("deny_read", [])]

    paths = [tool_input[k] for k in ("file_path", "path", "notebook_path")
             if tool_input.get(k)]
    for key in ("pattern", "glob"):
        if tool not in ("Glob", "Grep"):
            break
        value = str(tool_input.get(key) or "")
        if ".." in value.split("/"):
            return f"'{value}' climbs out of the allowed directories"
        if tool == "Glob" or key == "glob":
            root = _glob_root(value)
            if root:
                paths.append(root)
    if tool not in READ_TOOLS and not paths:
        return f"{tool} without a path"

    for raw in paths:
        path = _resolve(raw, cwd)
        if tool in READ_TOOLS:
            if any(_under(path, d) for d in deny_read):
                return f"{raw} is off limits in this session"
            if not any(_under(path, r) for r in read_roots):
                return f"{raw} is outside this session's directories"
            continue
        if not any(_under(path, r) for r in write_roots):
            return f"{raw} is outside this session's directories"
        if _under(path, os.path.realpath(cwd)):
            rel = os.path.relpath(path, os.path.realpath(cwd)).split(os.sep)
            if _NO_WRITE_PARTS.intersection(rel) or rel[-1] in _NO_WRITE_NAMES:
                return f"{raw} is not writable in this session"
    return None


def main() -> None:
    with open(sys.argv[1], encoding="utf-8") as fh:
        cfg = json.load(fh)
    data = json.load(sys.stdin)
    reason = check(data.get("tool_name") or "", data.get("tool_input") or {}, cfg)
    if reason:
        _block(reason)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:  # fails closed, whatever went wrong
        _block(f"path check failed ({type(exc).__name__})")
