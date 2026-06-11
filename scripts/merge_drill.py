"""Merge-path regression drill.

Reproduces the failure class that made auto-resolve go 0-for-69: a repo
registered at a SUBDIRECTORY of its git toplevel (e.g. AIAgent at
``DegenAI/AIAgent/AIAgent``).  Porcelain paths are toplevel-relative, so
every pathspec-consuming resolve command missed when run from the
registered dir, and the CHANGELOG union merge driver was written to a
junk ``<registered>/.git`` that git never read.

The drill builds a throwaway repo in that shape, manufactures a real
two-sided conflict (code file + CHANGELOG), then runs the production
machinery — the union-driver setup logic and
``ClaudeRunner._auto_resolve_merge_conflicts`` — and asserts:

1. the union driver lands in the REAL gitdir and absorbs the CHANGELOG
   conflict entirely (both sides' entries survive),
2. auto-resolve resolves the code-file conflict (feature side wins) and
   commits the merge.

Run after any change to the merge path:

    python scripts/merge_drill.py

Exit code 0 = all assertions passed.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.claude.gitpaths import git_common_dir, git_dir, git_toplevel  # noqa: E402
from bot.claude.runner import ClaudeRunner  # noqa: E402

_NOWND: dict = config.NOWND

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(label)


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, **_NOWND,
    )


def build_fixture(top: Path) -> Path:
    """Create <top> as a git repo whose project lives at <top>/Proj.

    Returns the registered dir (the subdirectory), mirroring how the
    AIAgent project is registered with the bot.
    """
    git(top, "init", "-b", "master")
    git(top, "config", "user.email", "drill@example.invalid")
    git(top, "config", "user.name", "Merge Drill")
    proj = top / "Proj"
    proj.mkdir()
    (proj / "app.py").write_text("VALUE = 0\n", encoding="utf-8")
    (proj / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n", encoding="utf-8",
    )
    git(top, "add", "-A")
    git(top, "commit", "-m", "base")

    # Feature branch: change app.py and add a CHANGELOG bullet.
    git(top, "checkout", "-b", "feature")
    (proj / "app.py").write_text("VALUE = 1  # feature\n", encoding="utf-8")
    (proj / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n- feature-side entry\n",
        encoding="utf-8",
    )
    git(top, "add", "-A")
    git(top, "commit", "-m", "feature work")

    # Master: conflicting change to the same line, different bullet.
    git(top, "checkout", "master")
    (proj / "app.py").write_text("VALUE = 2  # master\n", encoding="utf-8")
    (proj / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n- master-side entry\n",
        encoding="utf-8",
    )
    git(top, "add", "-A")
    git(top, "commit", "-m", "master work")
    return proj


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="merge_drill_") as tmp:
        top = Path(tmp) / "repo"
        top.mkdir()
        registered = str(build_fixture(top))

        print("Drill: subdirectory-registered repo")
        check(
            "git_toplevel resolves to the real root",
            (git_toplevel(registered) or "").replace("\\", "/")
            == str(top.resolve()).replace("\\", "/"),
            f"got {git_toplevel(registered)}",
        )
        check(
            "registered dir has no .git of its own",
            not (Path(registered) / ".git").exists(),
        )

        # Production code path — the same setup merge_branch runs.
        ClaudeRunner._ensure_union_merge_driver(registered)
        gd = git_common_dir(registered)
        check(
            "union driver written to the real gitdir",
            gd is not None
            and (Path(gd) / "info" / "attributes").exists()
            and not (Path(registered) / ".git").exists(),
        )

        # Merge exactly like merge_branch: from the registered dir.
        merge_r = git(Path(registered), "merge", "feature", "--no-ff",
                      "-m", "Merge feature (drill)")
        check("merge conflicts as expected", merge_r.returncode != 0)

        status = git(top, "status", "--porcelain").stdout
        conflicted = [
            line[3:] for line in status.splitlines()
            if line[:2] in ("UU", "AA", "DU", "UD", "DD", "AU", "UA")
        ]
        check(
            "CHANGELOG absorbed by union driver (not conflicted)",
            "Proj/CHANGELOG.md" not in conflicted,
            f"conflicted: {conflicted}",
        )
        check(
            "app.py is the remaining conflict",
            conflicted == ["Proj/app.py"],
            f"conflicted: {conflicted}",
        )

        runner = ClaudeRunner()
        resolved = runner._auto_resolve_merge_conflicts(
            registered, "feature", "(drill)",
        )
        check("auto-resolve reports success", resolved > 0,
              f"returned {resolved}")

        # MERGE_HEAD is per-worktree state — resolved via git_dir, matching
        # the production MERGE_HEAD checks.
        merge_head_dir = git_dir(registered)
        check("merge committed (no MERGE_HEAD left)",
              merge_head_dir is not None
              and not (Path(merge_head_dir) / "MERGE_HEAD").exists())
        app = (top / "Proj" / "app.py").read_text(encoding="utf-8")
        check("conflict resolved to feature side", "feature" in app,
              f"content: {app!r}")
        changelog = (top / "Proj" / "CHANGELOG.md").read_text(encoding="utf-8")
        check(
            "CHANGELOG kept both sides' entries",
            "feature-side entry" in changelog
            and "master-side entry" in changelog,
            f"content: {changelog!r}",
        )

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) FAILED")
        return 1
    print("\nAll checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
