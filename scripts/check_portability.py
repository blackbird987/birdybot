#!/usr/bin/env python3
"""Guard against Linux/Windows drift.

This repo is worked on from both a Linux desktop and a Windows laptop. The
bugs that caused are all invisible on the platform you happen to be sitting
at: a Windows checkout looks clean while Linux sees the whole repo rewritten;
a config path derived from $HOME resolves correctly on Windows and to an
empty directory on Linux. This script makes that class of problem fail loudly.

Check 7 belongs here for the same reason without being an OS difference: it
fires where the harnesses are actually run — in the bot's child processes,
which inherit no virtualenv — and not from a developer's activated shell, so
it too is invisible from where you are standing.

Checks:
  1. Every text blob in the git index is LF.
  2. Shell scripts are marked executable in git's index.
  3. Files Windows can't create (reserved names, ':' etc.) aren't tracked.
  4. Nothing hardcodes a drive letter or a backslash-joined path.
  5. No Windows-only API is reached outside a platform branch.
  6. No command in .claude/test.json is single-platform.
  7. Every script .claude/test.json runs finds its own interpreter.

Exit 0 = clean, 1 = problems found. Run from anywhere in the repo.
"""

from __future__ import annotations

import ast
import functools
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The shim that relaunches a script under the project virtualenv. This file does
# not import it and must not: it reads the repo with the standard library only,
# so it stays runnable on a checkout that has no virtualenv yet.
BOOTSTRAP = "_bootstrap"

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


def check_line_endings() -> None:
    """Every text blob in the index must be LF — including the .bat files.

    No extension is exempt, and an earlier version's exemption list for
    `.bat`/`.cmd`/`.ps1`/`.vbs` was a misreading of how git works. `eol=crlf`
    is a *working tree* directive: git stores those files as LF like everything
    else and converts on checkout. Every tracked text blob here reads `i/lf`,
    so the list never once fired — it was pure hole, skipping the only files
    whose CRLF would have been deliberate enough to look plausible.

    Answered by a single `git ls-files --eol`, which reports the index
    encoding for every tracked file at once. The first version shelled out to
    `git show :<path>` per file — ~150 subprocesses for the same answer.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--eol"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError) as e:
        problems.append(f"Could not read index line endings ({e})")
        return

    # Each row: "i/lf    w/crlf  attr/text=auto eol=lf   \t<path>"
    for line in out.splitlines():
        attrs, _, path = line.partition("\t")
        path = path.strip()
        if not path:
            continue
        index_eol = next(
            (f[2:] for f in attrs.split() if f.startswith("i/")), ""
        )
        # "-text" is binary and "none" is an empty file — neither can be wrong.
        if index_eol in ("crlf", "mixed"):
            problems.append(
                f"{index_eol.upper()} in git index: {path} "
                f"— run `git add --renormalize .`"
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

# Escape hatch for the cases a platform guard can't express: a migration tool
# whose whole job is old Windows paths, a test fixture that is a synthetic
# string and never touches disk. Without one, the check fails forever on
# known-good code and stops being read — which is the same as not having it.
ALLOW_RE = re.compile(r"#\s*portability:\s*ok")


def _doc_and_comment_lines(text: str) -> set[int]:
    """Line numbers that are purely docstring or comment.

    Docstrings routinely *document* Windows paths (`C:\\Users\\foo` →
    `C--Users-foo`), and flagging those is noise that trains you to ignore the
    check. But they have to be excluded precisely: an earlier version skipped
    every line holding a STRING token, and since a hardcoded path is *always*
    a string literal, that silently disabled the whole check — it could not
    fire on anything. Comments come from the tokenizer; docstrings are found
    as bare string-expression statements, which is what a docstring is and
    what a real assignment is not.
    """
    import ast
    import io
    import tokenize

    skip: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                skip.update(range(tok.start[0], tok.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass  # unparsable — fall through and check every line

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return skip
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            skip.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return skip


def check_hardcoded_paths(files: list[tuple[str, str]]) -> None:
    """A literal C:\\ or /home/ outside a platform branch means one OS wins.

    Only real code counts, and only when no platform guard appears in the
    preceding few lines — `os.environ.get("ProgramData", r"C:\\ProgramData")`
    inside an `if sys.platform == "win32":` block is correct, not a defect.

    Only this file is exempt, and only because its own search needles look
    exactly like the thing it hunts for. It used to skip all of `scripts/`,
    which quietly excused every harness and helper — including the ones the
    verify step runs on both machines — to hide that single self-match.
    """
    self_rel = Path(__file__).resolve().relative_to(REPO).as_posix()
    for _mode, path in files:
        if not path.endswith(".py") or path == self_rel:
            continue
        try:
            # utf-8-sig, not utf-8: a leading BOM makes ast.parse raise, which
            # would drop this file back to comments-only skipping and flag
            # every path its docstrings merely document.
            text = (REPO / path).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue

        skip = _doc_and_comment_lines(text)
        lines = text.splitlines()

        for i, line in enumerate(lines, 1):
            if i in skip:
                continue
            # `# portability: ok` on the line itself or the one above it.
            if ALLOW_RE.search(line) or (i > 1 and ALLOW_RE.search(lines[i - 2])):
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


# Names that simply are not there on POSIX Python. Touching one outside a
# platform branch raises AttributeError (or ImportError) at the moment it
# runs, which — in the restart paths, where every one of these lived inside a
# broad `except` — is indistinguishable from the feature quietly not working.
#
# The constants are matched bare rather than as `subprocess.X`, because the
# two real defects were spelled `_sp.DETACHED_PROCESS` through an aliased
# import and a prefix-anchored pattern walked straight past them. The names
# are distinctive enough that the only things this catches otherwise are
# deliberate local definitions of the same value — which belong inside a
# platform branch anyway, and are therefore already excused by the guard.
WIN_ONLY_API_RE = re.compile(
    r"""\b(?:DETACHED_PROCESS
            |CREATE_NEW_PROCESS_GROUP
            |CREATE_NO_WINDOW
            |CREATE_NEW_CONSOLE
            |CREATE_BREAKAWAY_FROM_JOB
            |STARTF_USESHOWWINDOW
            |STARTUPINFO
            |SW_HIDE)\b
        |\bctypes\.(?:windll|WinDLL|OleDLL)\b
        |\bos\.startfile\b
        |\b(?:import|from)\s+(?:winreg|msvcrt|win32api|win32con|win32com|
                               win32gui|win32process|pywintypes|pythoncom)\b""",
    re.VERBOSE,
)


def _import_error_guarded_lines(text: str) -> set[int]:
    """Lines of imports wrapped in `try: ... except ImportError:`.

    A platform branch is not the only correct guard. `bot/services/outlook.py`
    reaches pywin32 this way and degrades to a feature flag when the import
    fails, which is exactly what should happen on Linux — demanding a
    `sys.platform` check there would be a false alarm, and a check that cries
    wolf gets silenced.
    """
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()

    def catches_import_error(handler: "ast.ExceptHandler") -> bool:
        if handler.type is None:
            return True  # bare except
        parts = (
            handler.type.elts
            if isinstance(handler.type, ast.Tuple) else [handler.type]
        )
        return any(
            getattr(p, "id", "") in ("ImportError", "ModuleNotFoundError", "Exception")
            for p in parts
        )

    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(catches_import_error(h) for h in node.handlers):
            continue
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    end = sub.end_lineno or sub.lineno
                    guarded.update(range(sub.lineno, end + 1))
    return guarded


def check_windows_only_apis(files: list[tuple[str, str]]) -> None:
    """A Windows-only name reached on Linux is an AttributeError, not a no-op.

    Same window-and-guard rule as the hardcoded-path check, and for the same
    reason: `subprocess.CREATE_NO_WINDOW` inside `if sys.platform == "win32":`
    is correct. Outside one it is a landmine that only goes off on the other
    machine — `bot/app.py` carried two, so the emergency relaunch and the
    whole `/reboot` path were dead on Linux while reading as if they worked.
    """
    self_rel = Path(__file__).resolve().relative_to(REPO).as_posix()
    for _mode, path in files:
        if not path.endswith(".py") or path == self_rel:
            continue
        try:
            text = (REPO / path).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue

        skip = _doc_and_comment_lines(text) | _import_error_guarded_lines(text)
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            if i in skip:
                continue
            if ALLOW_RE.search(line) or (i > 1 and ALLOW_RE.search(lines[i - 2])):
                continue
            m = WIN_ONLY_API_RE.search(line)
            if not m:
                continue
            if GUARD_RE.search("\n".join(lines[max(0, i - 7):i])):
                continue
            problems.append(
                f"{path}:{i} Windows-only {m.group(0).strip()} outside a "
                f"platform branch — it does not exist on Linux"
            )


# Shell utilities that exist on only one of the two platforms. A command
# leading with one of these cannot be run by the verify step on the other
# machine. `tail -n 50 data/logs/bot.log` sat in logs.tail for exactly this
# reason — the earlier version of this check only looked at start/stop/health
# and only at file extensions, so it never saw it.
POSIX_ONLY_CMDS = {
    "tail", "head", "cat", "grep", "sed", "awk", "ls", "rm", "cp", "mv",
    "touch", "which", "chmod", "chown", "kill", "pkill", "ps", "sh", "bash",
    "sudo", "systemctl", "journalctl",
}
WINDOWS_ONLY_CMDS = {
    "taskkill", "tasklist", "dir", "del", "copy", "move", "where", "cmd",
    "powershell", "pwsh", "reg", "sc",
}


# Keys whose value is data, not something anyone runs.
NON_COMMAND_KEYS = {"project_type", "logs.file", "logs.failure_markers"}


def _iter_json_commands(node, path: str = ""):
    """Yield (json_path, command_string) for every command-ish string.

    Prose lives under `_`-prefixed keys by convention and is skipped there.
    Lists are walked rather than ignored: today the only one holding
    non-prose is `logs.failure_markers` (named in NON_COMMAND_KEYS), but a
    future `"steps": [...]` of real commands must not be invisible — a check
    that quietly stops looking is the failure mode this whole script exists
    to prevent.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key.startswith("_"):
                continue
            yield from _iter_json_commands(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_json_commands(item, path)
    elif isinstance(node, str):
        yield path, node


@functools.lru_cache(maxsize=1)
def _test_json() -> dict | None:
    """Parsed .claude/test.json, or None if it is absent or unreadable.

    Cached so the two checks that read it neither parse it twice nor report a
    broken file twice — and, more to the point, so neither has to assume the
    other ran first to do the reporting.
    """
    cfg_path = REPO / ".claude" / "test.json"
    if not cfg_path.exists():
        return None
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f".claude/test.json is not readable: {exc}")
        return None


def check_test_json() -> None:
    """.claude/test.json is static JSON with no way to branch per platform, so
    every command it names has to run on both."""
    cfg = _test_json()
    if cfg is None:
        return

    for key, cmd in _iter_json_commands(cfg):
        if key in NON_COMMAND_KEYS:
            continue
        tokens = cmd.split()
        if not tokens:
            continue
        program = Path(tokens[0]).name.lower()
        if program in POSIX_ONLY_CMDS:
            problems.append(
                f".claude/test.json '{key}' starts with POSIX-only `{program}` "
                f"({cmd}) — route it through a python script that runs on both"
            )
        elif program in WINDOWS_ONLY_CMDS:
            problems.append(
                f".claude/test.json '{key}' starts with Windows-only `{program}` "
                f"({cmd}) — route it through a python script that runs on both"
            )
        if cmd.endswith((".bat", ".cmd", ".ps1", ".vbs")):
            problems.append(
                f".claude/test.json '{key}' is Windows-only ({cmd}) — "
                f"point it at a python script that runs on both"
            )
        elif cmd.endswith(".sh"):
            problems.append(
                f".claude/test.json '{key}' is POSIX-only ({cmd}) — "
                f"point it at a python script that runs on both"
            )


def _imported_names(tree: ast.Module) -> set[str]:
    """Every top-level package name imported anywhere in ``tree``.

    Walks the whole tree rather than the module body, because the imports that
    matter most here are the deferred ones — ``smoke_test.py`` reaches for the
    bot from inside a function, which is exactly the case a body-only scan
    would call dependency-free and wave through.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def check_harness_bootstrap() -> None:
    """Scripts .claude/test.json runs must not depend on which `python` wins.

    The verify step inherits the bot's environment, and the bot is launched by
    a direct path into its virtualenv rather than by activating it — so
    `VIRTUAL_ENV` is unset, `.venv/bin` is off `PATH`, and a bare `python` in
    a child process is the *system* interpreter. A script that reaches for the
    bot then dies at import with a `ModuleNotFoundError` shaped exactly like a
    real failure, before any of its own logic runs.

    `scripts/_bootstrap.py` fixes that, but only for the scripts that import
    it, and that is a convention spread across seventeen files with nothing
    holding it up. A harness added to test.json without the line would fail on
    Linux for a reason having nothing to do with the code under test — the
    hazard this whole script exists to catch, so it is checked rather than
    remembered.

    A script importing nothing outside the standard library is exempt: it
    genuinely runs anywhere, and this checker is itself one of them.
    """
    cfg = _test_json()
    if cfg is None:
        return

    seen: set[str] = set()
    for key, cmd in _iter_json_commands(cfg):
        if key in NON_COMMAND_KEYS:
            continue
        for token in cmd.split():
            if not token.endswith(".py") or token in seen:
                continue
            script = REPO / token
            if not script.is_file():
                continue
            seen.add(token)
            try:
                tree = ast.parse(script.read_text(encoding="utf-8"))
            except (OSError, SyntaxError) as exc:
                problems.append(f"Could not parse {token} ({exc})")
                continue
            imported = _imported_names(tree)
            if BOOTSTRAP in imported:
                continue
            foreign = sorted(
                name for name in imported
                if name != BOOTSTRAP and name not in sys.stdlib_module_names
            )
            if foreign:
                problems.append(
                    f"{token} is run by .claude/test.json '{key}' and imports "
                    f"{', '.join(foreign)}, but does not `import {BOOTSTRAP}` — "
                    f"under the bot's environment a bare `python` is the system "
                    f"interpreter and it will die at import"
                )


def main() -> int:
    files = tracked_files()
    check_line_endings()
    check_exec_bits(files)
    check_windows_safe_names(files)
    check_hardcoded_paths(files)
    check_windows_only_apis(files)
    check_test_json()
    check_harness_bootstrap()

    if problems:
        print(f"Portability check FAILED — {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  ✗ {p}")
        return 1

    print(f"Portability check passed ({len(files)} tracked files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
