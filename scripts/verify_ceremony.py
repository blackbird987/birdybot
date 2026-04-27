"""End-to-end verification harness for the post-merge release ceremony.

Builds a throwaway git repo in a temp dir, runs the pure helpers and the
full ceremony against several scenarios, and prints a PASS/FAIL line per
assertion. Exits non-zero if any assertion failed.

Usage:
    python scripts/verify_ceremony.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Make sure we import from this worktree's bot/ package
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.engine import release_ceremony as rc  # noqa: E402


PASS = "PASS"
FAIL = "FAIL"

passed = 0
failed = 0


def assert_eq(name: str, got, want) -> None:
    global passed, failed
    if got == want:
        print(f"{PASS} {name}")
        passed += 1
    else:
        print(f"{FAIL} {name}\n    got:  {got!r}\n    want: {want!r}")
        failed += 1


def assert_true(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        print(f"{PASS} {name}")
        passed += 1
    else:
        print(f"{FAIL} {name}{(' — ' + detail) if detail else ''}")
        failed += 1


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False,
    )


def init_repo(repo: Path, *, version: str = "0.1.0", changelog: str = "") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8",
    )
    (repo / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")


# ---------------------------------------------------------------------------
# Pure-helper tests
# ---------------------------------------------------------------------------

CL_WITH_LEAK = """\
# Changelog

## [Unreleased]

### Added
- A new feature
- Bug fix in handler

## v0.2.0 — Drop B (2026-01-01)
### Fixed
- Bug fix in handler

## v0.1.0 — Initial (2025-12-01)
### Added
- Initial commit
"""

shipped = rc.extract_versioned_bullets(CL_WITH_LEAK)
assert_true("dedupe-extracts-shipped", "Bug fix in handler" in shipped)
assert_true("dedupe-extracts-old", "Initial commit" in shipped)

parsed = rc.parse_unreleased(CL_WITH_LEAK)
assert_true("parse-unreleased-found", parsed is not None)
body = parsed[0]
deduped, kept = rc.dedupe_unreleased_body(body, shipped)
assert_eq("dedupe-kept-count", len(kept), 1)
assert_true("dedupe-keeps-A", "A new feature" in kept)
assert_true("dedupe-drops-leak", "Bug fix in handler" not in kept)

assert_eq("patch-bump", rc.bump((1, 2, 3), "patch"), (1, 2, 4))
assert_eq("minor-bump", rc.bump((1, 2, 3), "minor"), (1, 3, 0))
assert_eq("major-bump", rc.bump((1, 2, 3), "major"), (2, 0, 0))

py_text = '[project]\nname = "x"\nversion = "1.2.3"\n'
new_py = rc.write_version_file_value(py_text, "pyproject", (1, 2, 4))
assert_true("pyproject-rewrite", '"1.2.4"' in (new_py or ""))


# ---------------------------------------------------------------------------
# End-to-end ceremony tests in a temp repo
# ---------------------------------------------------------------------------

tmp = Path(tempfile.mkdtemp(prefix="ceremony_test_"))
try:
    # --- Scenario 1: clean Unreleased -> release cut ---
    repo1 = tmp / "repo1"
    init_repo(
        repo1,
        version="0.1.0",
        changelog=(
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            "### Added\n"
            "- New widget rendering\n\n"
            "## v0.1.0 — Initial (2025-12-01)\n"
            "### Added\n"
            "- Initial commit\n"
        ),
    )
    res1 = rc.run_release_ceremony(str(repo1), "patch", today_iso="2026-04-28")
    assert_true("ceremony-cut", res1.cut, res1.skipped_reason or "")
    assert_eq("ceremony-version", res1.version, "0.1.1")
    assert_eq("ceremony-tag", res1.tag, "v0.1.1")
    py_after = (repo1 / "pyproject.toml").read_text(encoding="utf-8")
    assert_true("version-file-updated", '"0.1.1"' in py_after)
    cl_after = (repo1 / "CHANGELOG.md").read_text(encoding="utf-8")
    new_unreleased = rc.parse_unreleased(cl_after)
    assert_true("changelog-unreleased-empty",
                bool(new_unreleased) and not rc._BULLET_RE.search(new_unreleased[0]))
    assert_true("changelog-keeps-bullets", "New widget rendering" in cl_after)
    assert_true("changelog-keeps-old", "Initial commit" in cl_after)
    tag_check = git(repo1, "tag", "--list", "v0.1.1")
    assert_eq("tag-created", tag_check.stdout.strip(), "v0.1.1")

    # --- Scenario 2: empty Unreleased -> ceremony skipped ---
    repo2 = tmp / "repo2"
    init_repo(
        repo2,
        version="1.0.0",
        changelog=(
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            "## v1.0.0 — Initial (2025-12-01)\n"
            "- thing\n"
        ),
    )
    res2 = rc.run_release_ceremony(str(repo2), "patch", today_iso="2026-04-28")
    assert_true("empty-unreleased-skipped", not res2.cut)
    assert_true("empty-unreleased-reason",
                "empty" in (res2.skipped_reason or "").lower())

    # --- Scenario 3: Unreleased contains only leaked bullets -> skip ---
    repo3 = tmp / "repo3"
    init_repo(
        repo3,
        version="0.2.0",
        changelog=(
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            "- already shipped bullet\n\n"
            "## v0.2.0 — Drop B (2026-01-01)\n"
            "- already shipped bullet\n"
        ),
    )
    res3 = rc.run_release_ceremony(str(repo3), "patch", today_iso="2026-04-28")
    assert_true("dedupe-skips-cut", not res3.cut)
    assert_true(
        "dedupe-skip-reason",
        "dedupe" in (res3.skipped_reason or "").lower()
        or "shipped" in (res3.skipped_reason or "").lower(),
        res3.skipped_reason or "",
    )

    # --- Scenario 4: tag is ahead of pyproject -> next version uses tag floor ---
    repo4 = tmp / "repo4"
    init_repo(
        repo4,
        version="0.1.0",  # version file is behind
        changelog=(
            "# Changelog\n\n"
            "## [Unreleased]\n\n"
            "- something new\n\n"
            "## v0.5.0 — Big jump (2026-02-01)\n"
            "- earlier release done out of band\n"
        ),
    )
    # Create a tag higher than the file value to simulate a divergence
    git(repo4, "tag", "v0.5.0")
    res4 = rc.run_release_ceremony(str(repo4), "patch", today_iso="2026-04-28")
    assert_true("tag-floor-cut", res4.cut, res4.skipped_reason or "")
    assert_eq("tag-floor-version", res4.version, "0.5.1")

finally:
    shutil.rmtree(tmp, ignore_errors=True)


print()
print(f"{passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
