#!/usr/bin/env python3
"""Regression harness for CLI session-directory resolution.

The bug this guards: the projects dir was derived from ``Path.home()`` alone.
On Windows ``$HOME`` and the Claude account dir coincide (``C:/Users/x`` and
``C:/Users/x/.claude``) so it worked; on Linux the account dirs commonly live
on another filesystem, so the bot scanned a near-empty directory and every
real session was invisible — session listing, resume, thread titles and
"branch from here" all silently found nothing.

Also guards the second-order bug the fix introduced: the projects root is
derived from ``CLAUDE_ACCOUNTS``, and ``bot/app.py`` prunes that list at boot,
so a derivation that isn't refreshed leaves the root pointing at a dropped
account — and, because "root doesn't match the derivation" is how a test
sandbox announces itself, made the scanner return nothing at all.

Exit 0 = pass, 1 = failure.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

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


def use_accounts(accounts: list[str]) -> None:
    """Apply an account list the way production does.

    Clears any override left by an earlier case first, so each case starts
    from "nothing has overridden the root".
    """
    config.CLAUDE_PROJECTS_DIR = config._DERIVED_PROJECTS_DIR
    config.set_accounts(accounts)


def main() -> int:
    saved = {
        "CLAUDE_ACCOUNTS": config.CLAUDE_ACCOUNTS,
        "CLAUDE_PROJECTS_DIR": config.CLAUDE_PROJECTS_DIR,
        "_DERIVED_PROJECTS_DIR": config._DERIVED_PROJECTS_DIR,
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
            use_accounts([str(primary), str(backup)])
            check(
                config.CLAUDE_PROJECTS_DIR == primary / "projects",
                f"primary should be first account's projects, got "
                f"{config.CLAUDE_PROJECTS_DIR}",
            )
            check(
                Path.home() not in config.CLAUDE_PROJECTS_DIR.parents,
                "primary must not fall back under $HOME when accounts are set",
            )
            check(
                config.primary_account_dir() == primary,
                "CLAUDE_CONFIG_DIR pin should be the first account",
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
            use_accounts([str(primary), str(missing)])
            dirs = config.claude_projects_dirs()
            check(
                missing / "projects" not in dirs,
                "non-existent account dir should be filtered out",
            )

            # 4. Duplicates collapse (same dir listed twice in .env).
            use_accounts([str(primary), str(primary)])
            dirs = config.claude_projects_dirs()
            check(
                dirs.count(primary / "projects") == 1,
                f"duplicate account entries should collapse, got {dirs}",
            )

            # 5. No accounts configured -> home-derived fallback still works.
            use_accounts([])
            check(
                config.CLAUDE_PROJECTS_DIR
                == Path.home() / config.PROVIDER_DIR_NAME / "projects",
                "with no accounts the primary should fall back to $HOME",
            )
            check(
                config.primary_account_dir() is None,
                "with no accounts there is nothing to pin CLAUDE_CONFIG_DIR to",
            )

            # 6. Boot prunes a dead entry (bot/app.py) — everything derived
            #    from the account list has to follow it. Before the setter
            #    existed this left the root aimed at the dropped account, the
            #    scan read that as a deliberate override, and it returned [].
            use_accounts([str(missing), str(primary), str(backup)])
            config.set_accounts([str(primary), str(backup)])  # what app.py does
            check(
                config.CLAUDE_PROJECTS_DIR == primary / "projects",
                f"primary should follow the prune, got "
                f"{config.CLAUDE_PROJECTS_DIR}",
            )
            check(
                config.primary_account_dir() == primary,
                "CLAUDE_CONFIG_DIR pin should follow the prune too — title "
                "generation would otherwise run against a dropped account",
            )
            dirs = config.claude_projects_dirs()
            check(
                primary / "projects" in dirs and backup / "projects" in dirs,
                f"pruning a dead first entry blinded the scanner, got {dirs}",
            )

            # 7. An explicit override confines the scan. Callers delete files,
            #    so a sandbox must never be widened back to the real dirs.
            use_accounts([str(primary), str(backup)])
            sandbox = root / "sandbox"
            sandbox.mkdir()
            config.CLAUDE_PROJECTS_DIR = sandbox
            dirs = config.claude_projects_dirs()
            check(
                dirs == [sandbox],
                f"override should confine scan to the sandbox alone, got {dirs}",
            )

            # 8. ...and it survives a config update, so a sandboxed test can't
            #    be silently pointed back at real session data mid-run.
            config.set_accounts([str(primary)])
            check(
                config.claude_projects_dirs() == [sandbox],
                "set_accounts un-sandboxed an active override",
            )
            check(
                config.CLAUDE_PROJECTS_DIR == sandbox,
                "set_accounts overwrote an active override",
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
