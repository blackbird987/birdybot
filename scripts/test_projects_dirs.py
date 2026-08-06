#!/usr/bin/env python3
"""Regression harness for CLI session-directory resolution.

The bug this guards: the projects dir was derived from ``Path.home()`` alone.
On Windows ``$HOME`` and the Claude account dir coincide (``C:/Users/x`` and
``C:/Users/x/.claude``) so it worked; on Linux the account dirs commonly live
on another filesystem, so the bot scanned a near-empty directory and every
real session was invisible — session listing, resume, thread titles and
"branch from here" all silently found nothing.

Exit 0 = pass, 1 = failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)


def _restore(saved: dict) -> None:
    for k, v in saved.items():
        setattr(config, k, v)


def main() -> int:
    saved = {
        "CLAUDE_ACCOUNTS": config.CLAUDE_ACCOUNTS,
        "CLAUDE_PROJECTS_DIR": config.CLAUDE_PROJECTS_DIR,
        "PROVIDER": config.PROVIDER,
    }

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        primary = root / "acct-a"
        backup = root / "acct-b"
        missing = root / "acct-gone"
        for a in (primary, backup):
            (a / "projects").mkdir(parents=True)

        try:
            config.PROVIDER = "claude"

            # 1. The primary is the FIRST configured account, not $HOME.
            config.CLAUDE_ACCOUNTS = [str(primary), str(backup)]
            config.CLAUDE_PROJECTS_DIR = config._default_projects_dir()
            check(
                config.CLAUDE_PROJECTS_DIR == primary / "projects",
                f"primary should be first account's projects, got "
                f"{config.CLAUDE_PROJECTS_DIR}",
            )
            check(
                Path.home() not in config.CLAUDE_PROJECTS_DIR.parents,
                "primary must not fall back under $HOME when accounts are set",
            )

            # 2. Every account is scanned — a session on the backup account
            #    was previously invisible on BOTH platforms.
            dirs = config.claude_projects_dirs()
            check(
                primary / "projects" in dirs,
                "primary account's projects dir missing from scan",
            )
            check(
                backup / "projects" in dirs,
                "backup account's projects dir missing from scan — a session "
                "started on the second account would be unreachable",
            )

            # 3. Non-existent account dirs are dropped, not returned.
            config.CLAUDE_ACCOUNTS = [str(primary), str(missing)]
            dirs = config.claude_projects_dirs()
            check(
                missing / "projects" not in dirs,
                "non-existent account dir should be filtered out",
            )

            # 4. Duplicates collapse (same dir listed twice in .env).
            config.CLAUDE_ACCOUNTS = [str(primary), str(primary)]
            dirs = config.claude_projects_dirs()
            check(
                dirs.count(primary / "projects") == 1,
                f"duplicate account entries should collapse, got {dirs}",
            )

            # 5. An explicit override confines the scan. Callers delete files,
            #    so a sandbox must never be widened back to the real dirs.
            config.CLAUDE_ACCOUNTS = [str(primary), str(backup)]
            sandbox = root / "sandbox"
            sandbox.mkdir()
            config.CLAUDE_PROJECTS_DIR = sandbox
            dirs = config.claude_projects_dirs()
            check(
                dirs == [sandbox],
                f"override should confine scan to the sandbox alone, got {dirs}",
            )

            # 6. No accounts configured -> home-derived fallback still works.
            config.CLAUDE_ACCOUNTS = []
            config.CLAUDE_PROJECTS_DIR = config._default_projects_dir()
            check(
                config.CLAUDE_PROJECTS_DIR
                == Path.home() / config.PROVIDER_DIR_NAME / "projects",
                "with no accounts the primary should fall back to $HOME",
            )
        finally:
            _restore(saved)

    if failures:
        print(f"FAIL — {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASS — projects-dir resolution holds on both platforms")
    print(f"  live primary: {config.CLAUDE_PROJECTS_DIR}")
    print(f"  live scan   : {len(config.claude_projects_dirs())} root(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
