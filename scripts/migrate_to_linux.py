#!/usr/bin/env python3
"""Rewrite this bot's Windows paths to their Linux equivalents.

The bot's *code* is cross-platform; only its configuration is not. Three
places hold absolute Windows paths and all three make the bot useless until
they are translated:

  1. ``.env``            — CLAUDE_BINARY, REPOS_BASE_DIR, CLAUDE_ACCOUNTS
  2. ``data/state.json`` — the registered-repo table (every ``/repo`` target)
                           plus per-instance repo/worktree/account paths
  3. git worktrees       — stale ``.worktrees/*`` entries record absolute
                           Windows gitdirs (reported here, pruned by git)

Dry-run by default so it can be previewed from Windows before the switch::

    python scripts/migrate_to_linux.py                 # show the plan
    python scripts/migrate_to_linux.py --apply         # write it

Originals are copied to ``*.windows.bak`` before anything is written.

Note this only translates paths. It does not move any files — copy the repo
tree across first, then run this on the Linux side.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parent.parent

# .env keys whose values are paths. Anything else is left alone.
_PATH_KEYS = {"CLAUDE_BINARY", "REPOS_BASE_DIR", "DATA_DIR"}
_PATH_LIST_KEYS = {"CLAUDE_ACCOUNTS"}

# state.json instance fields that hold absolute paths (verified against the
# live file: repo_path, session_account, worktree_path). `prompt` also
# contains Windows paths in a handful of records but that is historical
# conversation text — rewriting it would falsify the record.
_INSTANCE_PATH_FIELDS = ("repo_path", "worktree_path", "session_account")

_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def _norm(p: str) -> str:
    return p.replace("\\", "/")


class Mapper:
    """Translate absolute Windows paths under the old home to the new home."""

    def __init__(self, win_home: str, linux_home: str) -> None:
        self.win_home = _norm(win_home).rstrip("/")
        self._match = self.win_home.lower()
        self.linux_home = _norm(linux_home).rstrip("/")
        self.unmapped: set[str] = set()

    def __call__(self, value):
        if not isinstance(value, str) or not value:
            return value
        n = _norm(value)
        if n.lower() == self._match:
            return self.linux_home
        if n.lower().startswith(self._match + "/"):
            rest = n[len(self._match) + 1:]
            return str(PurePosixPath(self.linux_home) / rest)
        if _WIN_ABS.match(n):
            # An absolute Windows path outside the old home — a guess here
            # would be worse than a report.
            self.unmapped.add(value)
        return value


def _resolve_claude_binary(linux_home: str) -> tuple[str, str]:
    """Best available path to the Claude CLI, plus how we found it.

    Only trusts PATH when we are already on the target platform — previewing
    from Windows, ``which("claude")`` finds ``claude.EXE`` and would write a
    Windows binary path into the Linux config.
    """
    if os.name != "nt":
        found = shutil.which("claude")
        if found:
            return found, "found on PATH"
    guess = str(PurePosixPath(linux_home) / ".local/bin/claude")
    reason = ("previewing from Windows — guessed; re-run on Linux to pick up "
              "the real path" if os.name == "nt"
              else "NOT on PATH — guessed, install the CLI then verify")
    return guess, reason


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------

def migrate_env(path: Path, mapper: Mapper, apply: bool) -> list[str]:
    if not path.exists():
        return [f"!! {path.name} not found — skipped"]

    original = path.read_text(encoding="utf-8")
    changes: list[str] = []
    out: list[str] = []

    for line in original.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue

        key, _, value = line.partition("=")
        key_clean = key.strip()
        new_value = value

        if key_clean == "CLAUDE_BINARY":
            new_value, how = _resolve_claude_binary(mapper.linux_home)
            if new_value != value:
                changes.append(f"  CLAUDE_BINARY   {value}  ->  {new_value}   ({how})")
        elif key_clean in _PATH_LIST_KEYS:
            parts = [mapper(p.strip()) for p in value.split(",") if p.strip()]
            new_value = ",".join(parts)
            if new_value != value:
                changes.append(f"  {key_clean:<15} {value}  ->  {new_value}")
        elif key_clean in _PATH_KEYS or _WIN_ABS.match(_norm(value.strip())):
            new_value = mapper(value.strip())
            if new_value != value:
                changes.append(f"  {key_clean:<15} {value}  ->  {new_value}")

        out.append(f"{key}={new_value}" if new_value != value else line)

    if apply and changes:
        shutil.copy2(path, path.with_suffix(path.suffix + ".windows.bak"))
        path.write_text("\n".join(out) + "\n", encoding="utf-8")

    return changes or ["  (nothing to change)"]


# --------------------------------------------------------------------------
# data/state.json
# --------------------------------------------------------------------------

def migrate_state(path: Path, mapper: Mapper, apply: bool) -> list[str]:
    if not path.exists():
        return [f"!! {path.name} not found — skipped"]

    data = json.loads(path.read_text(encoding="utf-8"))
    changes: list[str] = []

    repos = data.get("repos")
    if isinstance(repos, dict):
        for name, old in list(repos.items()):
            new = mapper(old)
            if new != old:
                repos[name] = new
                changes.append(f"  repo {name:<28} {old}  ->  {new}")

    field_counts: dict[str, int] = {}
    for inst in data.get("instances") or []:
        if not isinstance(inst, dict):
            continue
        for field in _INSTANCE_PATH_FIELDS:
            old = inst.get(field)
            new = mapper(old)
            if new != old:
                inst[field] = new
                field_counts[field] = field_counts.get(field, 0) + 1

    for field, count in sorted(field_counts.items()):
        changes.append(f"  instance history: {count} x {field} rewritten")

    if apply and changes:
        shutil.copy2(path, path.with_suffix(path.suffix + ".windows.bak"))
        tmp = path.with_suffix(path.suffix + ".new")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)

    return changes or ["  (nothing to change)"]


# --------------------------------------------------------------------------

def check_worktrees() -> list[str]:
    wt_dir = REPO / ".git" / "worktrees"
    if not wt_dir.is_dir():
        return ["  (none registered)"]
    names = sorted(p.name for p in wt_dir.iterdir() if p.is_dir())
    if not names:
        return ["  (none registered)"]
    return [
        f"  {len(names)} stale worktree(s) registered: {', '.join(names)}",
        "  These record absolute Windows gitdirs and cannot survive the move.",
        "  On Linux run:  git worktree prune && git worktree list",
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # portability: ok - this tool exists to rewrite the old Windows paths
    ap.add_argument("--windows-home", default="C:/Users/Quincy",
                    help="Old Windows home directory (default: %(default)s)")
    ap.add_argument("--linux-home", default=None,
                    help="New home directory (default: the current user's home)")
    ap.add_argument("--apply", action="store_true",
                    help="Write the changes. Without this, only print them.")
    args = ap.parse_args()

    linux_home = args.linux_home or str(Path.home())
    if os.name == "nt" and not args.linux_home:
        linux_home = "/home/" + (os.environ.get("USERNAME") or "user").lower()
        print(f"[note] running on Windows — assuming Linux home {linux_home}\n"
              f"       override with --linux-home if your username differs\n")

    mapper = Mapper(args.windows_home, linux_home)
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== {mode} ===")
    print(f"  {mapper.win_home}  ->  {mapper.linux_home}\n")

    print(".env")
    for line in migrate_env(REPO / ".env", mapper, args.apply):
        print(line)

    print("\ndata/state.json")
    for line in migrate_state(REPO / "data" / "state.json", mapper, args.apply):
        print(line)

    print("\ngit worktrees")
    for line in check_worktrees():
        print(line)

    if mapper.unmapped:
        print("\n!! Absolute Windows paths outside the old home — left untouched,")
        print("   decide these by hand:")
        for value in sorted(mapper.unmapped):
            print(f"     {value}")

    if not args.apply:
        print("\nNothing was written. Re-run with --apply to commit these changes.")
    else:
        print("\nDone. Originals saved as *.windows.bak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
