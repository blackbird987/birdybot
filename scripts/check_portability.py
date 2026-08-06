#!/usr/bin/env python3
"""Guard against Linux/Windows drift.

This repo is worked on from both a Linux desktop and a Windows laptop. The
bugs that caused are all invisible on the platform you happen to be sitting
at: a Windows checkout looks clean while Linux sees the whole repo rewritten;
a config path derived from $HOME resolves correctly on Windows and to an
empty directory on Linux. This script makes that class of problem fail loudly.

Checks:
  1. Line endings match the .gitattributes policy (LF, except *.bat & friends).
  2. Shell scripts are marked executable in git's index.
  3. Files Windows can't create (reserved names, ':' etc.) aren't tracked.
  4. Nothing hardcodes a drive letter or a backslash-joined path.
  5. The commands in .claude/test.json exist on this platform.

Exit 0 = clean, 1 = problems found. Run from anywhere in the repo.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Extensions that legitimately keep CRLF — must mirror .gitattributes.
CRLF_OK = {".bat", ".cmd", ".ps1", ".vbs"}

# Names Windows refuses regardless of extension.
WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Source files may legitimately mention a drive letter inside a Windows-only
# branch or a docstring. We only flag it outside those.
DRIVE_RE = re.compile(r"""["']([A-Za-z]):[\\/]{1,2}""")

problems: list[str] = []


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout


def tracked_files() -> list[tuple[str, str]]:
    """(mode, path) for every tracked file."""
    out = []
    for line in _git("ls-files", "-s").splitlines():
        meta, path = line.split("\t", 1)
        out.append((meta.split()[0], path))
    return out


def check_line_endings(files: list[tuple[str, str]]) -> None:
    """Every text blob in HEAD must be LF; CRLF belongs only in the working
    tree, applied by .gitattributes on checkout."""
    for _mode, path in files:
        if Path(path).suffix.lower() in CRLF_OK:
            continue
        try:
            blob = subprocess.run(
                ["git", "show", f":{path}"], cwd=REPO,
                capture_output=True, check=True,
            ).stdout
        except subprocess.CalledProcessError:
            continue
        if b"\x00" in blob[:8192]:
            continue  # binary
        if b"\r\n" in blob:
            problems.append(
                f"CRLF in git index: {path} — run `git add --renormalize .`"
            )


def check_exec_bits(files: list[tuple[str, str]]) -> None:
    """A .sh recorded 100644 checks out non-executable on ext4, so the first
    command of the setup guide dies with Permission denied. This clone lives
    on NTFS where the on-disk bit always reads as set, hiding it."""
    for mode, path in files:
        if path.endswith(".sh") and mode != "100755":
            problems.append(
                f"Shell script not executable in git: {path} "
                f"(mode {mode}) — run `git update-index --chmod=+x {path}`"
            )


def check_windows_safe_names(files: list[tuple[str, str]]) -> None:
    """Paths Windows cannot check out at all — the clone half-fails there."""
    for _mode, path in files:
        name = Path(path).name
        stem = name.split(".")[0].upper()
        if stem in WIN_RESERVED:
            problems.append(f"Windows-reserved filename: {path}")
        bad = set(name) & set('<>:"|?*')
        if bad:
            problems.append(
                f"Illegal char(s) on Windows {sorted(bad)} in: {path}"
            )
        if name != name.rstrip(". "):
            problems.append(f"Trailing dot/space breaks on Windows: {path}")


GUARD_RE = re.compile(r"win32|os\.name\s*==\s*['\"]nt['\"]|IS_WINDOWS|_is_windows")


def _doc_and_comment_lines(text: str) -> set[int]:
    """Line numbers that are purely docstring or comment.

    Tokenizing beats matching on raw lines: docstrings routinely *document*
    Windows paths (`C:\\Users\\foo` → `C--Users-foo`), and flagging those is
    noise that trains you to ignore the check.
    """
    import io
    import tokenize

    skip: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                skip.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass  # unparsable — fall through and check every line
    return skip


def check_hardcoded_paths(files: list[tuple[str, str]]) -> None:
    """A literal C:\\ or /home/ outside a platform branch means one OS wins.

    Only real code counts, and only when no platform guard appears in the
    preceding few lines — `os.environ.get("ProgramData", r"C:\\ProgramData")`
    inside an `if sys.platform == "win32":` block is correct, not a defect.
    """
    for _mode, path in files:
        if not path.endswith(".py") or path.startswith("scripts/"):
            continue
        try:
            text = (REPO / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        skip = _doc_and_comment_lines(text)
        lines = text.splitlines()

        for i, line in enumerate(lines, 1):
            if i in skip:
                continue
            # Look back a short window for the enclosing platform branch.
            context = "\n".join(lines[max(0, i - 7):i])
            if GUARD_RE.search(context):
                continue

            m = DRIVE_RE.search(line)
            if m:
                problems.append(
                    f"{path}:{i} hardcoded drive letter {m.group(1)}: — "
                    f"guard it behind sys.platform or derive from config"
                )
            if "/home/" in line:
                problems.append(
                    f"{path}:{i} hardcoded /home/ path — derive it instead"
                )


def check_test_json() -> None:
    """.claude/test.json is static JSON with no way to branch per platform, so
    every command it names has to run on both."""
    cfg_path = REPO / ".claude" / "test.json"
    if not cfg_path.exists():
        return
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        problems.append(f".claude/test.json is not valid JSON: {exc}")
        return

    for key in ("start", "stop", "health"):
        cmd = cfg.get(key)
        if not isinstance(cmd, str) or not cmd:
            continue
        if cmd.endswith((".bat", ".cmd", ".ps1", ".vbs")):
            problems.append(
                f".claude/test.json '{key}' is Windows-only ({cmd}) — "
                f"point it at a python script that runs on both"
            )
        if cmd.endswith(".sh"):
            problems.append(
                f".claude/test.json '{key}' is POSIX-only ({cmd}) — "
                f"point it at a python script that runs on both"
            )


def main() -> int:
    files = tracked_files()
    check_line_endings(files)
    check_exec_bits(files)
    check_windows_safe_names(files)
    check_hardcoded_paths(files)
    check_test_json()

    if problems:
        print(f"Portability check FAILED — {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  ✗ {p}")
        return 1

    print(f"Portability check passed ({len(files)} tracked files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
