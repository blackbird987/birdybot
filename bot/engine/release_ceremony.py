"""Deterministic post-merge release ceremony.

Runs on master inside the per-repo git lock after a successful merge. Replaces
the old LLM-driven `release` autopilot step so two parallel autopilots cutting
back-to-back releases on the same repo cannot drift versions or duplicate
bullets.

Algorithm
---------
1. Parse `## [Unreleased]` body from CHANGELOG.md
2. Build the set of bullets that already shipped under any prior `## vX.Y.Z`
   header. Drop any bullet from the Unreleased body that already shipped
   (this absorbs union-merge leaks from sibling worktrees).
3. If the deduped Unreleased body is empty -> skip (no release).
4. Compute the next version: `max(version-file-value, all-v*-tags) + bump`
5. Atomically:
   - rewrite CHANGELOG.md (Unreleased -> versioned, fresh empty Unreleased)
   - rewrite the project's version file (pyproject/package.json/Cargo/csproj)
   - `git add` both, single commit, single tag
6. On any exception inside the staged region: roll back files from in-memory
   backups and `git reset HEAD --` the staged paths.

The runner pushes `--follow-tags` *once* after the ceremony so the commit + tag
land in the same atomic remote update.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Hide the console window for child processes on Windows so headless runs
# don't flash subprocess windows up at the user.
_NOWND: dict = {}
if sys.platform == "win32":
    _NOWND = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


_UNRELEASED_RE = re.compile(
    r"(?ms)^##\s*\[Unreleased\]\s*\n(?P<body>.*?)(?=^##\s|\Z)"
)
# Versioned section header: matches both em-dash and hyphen, both " — " and " - "
_VERSION_HEADER_RE = re.compile(
    r"(?ms)^##\s*v(?P<ver>\d+\.\d+\.\d+)\s*[—\-]\s*(?P<title>[^\n]*)\n(?P<body>.*?)(?=^##\s|\Z)"
)
_BULLET_RE = re.compile(r"(?m)^\s*[-*]\s+(?P<text>.+?)\s*$")
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_PYPROJECT_VERSION_RE = re.compile(
    r'(?m)^(?P<prefix>version\s*=\s*")(?P<ver>\d+\.\d+\.\d+)(?P<suffix>")'
)
_PACKAGE_VERSION_RE = re.compile(
    r'(?m)^(?P<prefix>\s*"version"\s*:\s*")(?P<ver>\d+\.\d+\.\d+)(?P<suffix>")'
)
_CARGO_VERSION_RE = re.compile(
    r'(?m)^(?P<prefix>version\s*=\s*")(?P<ver>\d+\.\d+\.\d+)(?P<suffix>")'
)
_CSPROJ_VERSION_RE = re.compile(
    r"(?s)(?P<prefix><Version>\s*)(?P<ver>\d+\.\d+\.\d+)(?P<suffix>\s*</Version>)"
)


@dataclass
class CeremonyResult:
    cut: bool
    version: str | None = None
    tag: str | None = None
    commit_sha: str | None = None
    summary: str | None = None
    skipped_reason: str | None = None
    bullets: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure helpers (no I/O, easy to unit-test)
# ---------------------------------------------------------------------------


def parse_unreleased(changelog_text: str) -> tuple[str, int, int] | None:
    """Return (body, span_start, span_end) for the [Unreleased] block, or None."""
    m = _UNRELEASED_RE.search(changelog_text)
    if not m:
        return None
    return (m.group("body"), m.start(), m.end())


def extract_versioned_bullets(changelog_text: str) -> set[str]:
    """Return the set of bullet texts that have already shipped under any version.

    Scans EVERY versioned section, not a bounded N — this is what makes the
    dedupe robust against union-merge leaks that re-introduce already-shipped
    bullets weeks later.
    """
    seen: set[str] = set()
    for m in _VERSION_HEADER_RE.finditer(changelog_text):
        body = m.group("body")
        for bullet in _BULLET_RE.finditer(body):
            seen.add(bullet.group("text").strip())
    return seen


def dedupe_unreleased_body(body: str, shipped_bullets: set[str]) -> tuple[str, list[str]]:
    """Return (body_with_duplicates_removed, kept_bullets).

    Lines that aren't bullets (blank lines, ### subheaders, prose) are kept
    verbatim. Only top-level bullet lines whose stripped text already shipped
    are dropped.
    """
    out_lines: list[str] = []
    kept: list[str] = []
    for line in body.splitlines():
        bm = _BULLET_RE.match(line)
        if bm:
            text = bm.group("text").strip()
            if text in shipped_bullets:
                continue  # leak — drop
            kept.append(text)
        out_lines.append(line)
    return ("\n".join(out_lines), kept)


def list_version_tags(repo_path: str) -> list[tuple[int, int, int]]:
    """Return every v* tag in the repo as (major, minor, patch) tuples."""
    try:
        result = subprocess.run(
            ["git", "tag", "--list", "v*"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **_NOWND,
        )
    except Exception:
        return []
    out: list[tuple[int, int, int]] = []
    for line in result.stdout.splitlines():
        m = _TAG_RE.match(line.strip())
        if m:
            out.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return out


def detect_version_file(repo_path: str) -> tuple[Path, str, str] | None:
    """Find the project's version source file.

    Returns (path, file_type, original_text) or None. Order follows the
    universal versioning convention in the user's global CLAUDE.md.
    """
    repo = Path(repo_path)
    candidates = [
        ("pyproject.toml", "pyproject"),
        ("package.json", "package"),
        ("Cargo.toml", "cargo"),
    ]
    for name, kind in candidates:
        p = repo / name
        if p.exists():
            try:
                return (p, kind, p.read_text(encoding="utf-8"))
            except Exception:
                continue
    # csproj — first match wins
    for csproj in repo.glob("*.csproj"):
        try:
            return (csproj, "csproj", csproj.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


def read_version_file_value(text: str, file_type: str) -> tuple[int, int, int] | None:
    """Extract the current version from the version file body, or None."""
    pat = {
        "pyproject": _PYPROJECT_VERSION_RE,
        "package": _PACKAGE_VERSION_RE,
        "cargo": _CARGO_VERSION_RE,
        "csproj": _CSPROJ_VERSION_RE,
    }.get(file_type)
    if not pat:
        return None
    m = pat.search(text)
    if not m:
        return None
    parts = m.group("ver").split(".")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def write_version_file_value(text: str, file_type: str, version: tuple[int, int, int]) -> str | None:
    """Return rewritten file text with the new version, or None if the pattern didn't match."""
    pat = {
        "pyproject": _PYPROJECT_VERSION_RE,
        "package": _PACKAGE_VERSION_RE,
        "cargo": _CARGO_VERSION_RE,
        "csproj": _CSPROJ_VERSION_RE,
    }.get(file_type)
    if not pat:
        return None
    new_ver = f"{version[0]}.{version[1]}.{version[2]}"
    new_text, n = pat.subn(
        lambda m: f"{m.group('prefix')}{new_ver}{m.group('suffix')}", text, count=1
    )
    if n != 1:
        return None
    return new_text


def bump(triple: tuple[int, int, int], kind: str) -> tuple[int, int, int]:
    major, minor, patch = triple
    if kind == "major":
        return (major + 1, 0, 0)
    if kind == "minor":
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def compute_next_version(
    repo_path: str,
    file_type: str,
    file_text: str,
    bump_kind: str,
) -> tuple[int, int, int]:
    """Pick a version strictly above every v* tag and the version-file value."""
    file_ver = read_version_file_value(file_text, file_type) or (0, 0, 0)
    tags = list_version_tags(repo_path)
    floor = max([file_ver, *tags]) if tags else file_ver
    return bump(floor, bump_kind)


def derive_summary(bullets: list[str]) -> str:
    """Pick a short summary from the kept bullets — first bullet, trimmed."""
    if not bullets:
        return "Release"
    first = bullets[0].strip()
    # Strip leading "Add"/"Fix"/"Update" so the summary reads compactly
    first = re.sub(r"^[A-Z][a-z]+\s+", lambda m: m.group(0), first)  # no-op, keep verb
    if len(first) > 60:
        first = first[:57].rstrip() + "..."
    return first


# ---------------------------------------------------------------------------
# Atomic-with-rollback ceremony
# ---------------------------------------------------------------------------


def _git(repo_path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **_NOWND,
    )


def run_release_ceremony(
    repo_path: str,
    bump_kind: str,
    *,
    today_iso: str | None = None,
) -> CeremonyResult:
    """Cut a deterministic release. Caller must hold the per-repo git lock."""
    if bump_kind not in ("patch", "minor", "major"):
        return CeremonyResult(cut=False, skipped_reason=f"invalid bump kind '{bump_kind}'")

    repo = Path(repo_path)
    changelog = repo / "CHANGELOG.md"
    if not changelog.exists():
        return CeremonyResult(cut=False, skipped_reason="CHANGELOG.md missing")

    cl_text = changelog.read_text(encoding="utf-8")
    parsed = parse_unreleased(cl_text)
    if not parsed:
        return CeremonyResult(cut=False, skipped_reason="[Unreleased] section missing")
    body, span_start, span_end = parsed

    shipped = extract_versioned_bullets(cl_text)
    deduped_body, kept_bullets = dedupe_unreleased_body(body, shipped)
    if not kept_bullets:
        return CeremonyResult(
            cut=False,
            skipped_reason="[Unreleased] empty after dedupe (all bullets already shipped)",
        )

    vfile = detect_version_file(repo_path)
    if not vfile:
        return CeremonyResult(cut=False, skipped_reason="no version file found")
    vf_path, vf_type, vf_text = vfile

    next_ver = compute_next_version(repo_path, vf_type, vf_text, bump_kind)
    new_vf_text = write_version_file_value(vf_text, vf_type, next_ver)
    if new_vf_text is None:
        return CeremonyResult(
            cut=False,
            skipped_reason=f"could not rewrite version in {vf_path.name}",
        )

    summary = derive_summary(kept_bullets)
    iso = today_iso or date.today().isoformat()
    version_str = f"{next_ver[0]}.{next_ver[1]}.{next_ver[2]}"
    tag_str = f"v{version_str}"

    versioned_block = f"## {tag_str} — {summary} ({iso})\n{deduped_body}"
    new_cl_text = (
        cl_text[:span_start]
        + "## [Unreleased]\n\n"
        + versioned_block
        + cl_text[span_end:]
    )

    backups: list[tuple[Path, str]] = [
        (changelog, cl_text),
        (vf_path, vf_text),
    ]
    staged_rel: list[str] = []

    try:
        changelog.write_text(new_cl_text, encoding="utf-8")
        vf_path.write_text(new_vf_text, encoding="utf-8")

        for p in (changelog, vf_path):
            try:
                rel = str(p.relative_to(repo))
            except ValueError:
                rel = str(p)
            add = _git(repo_path, "add", rel)
            if add.returncode != 0:
                raise RuntimeError(f"git add {rel} failed: {add.stderr.strip()}")
            staged_rel.append(rel)

        msg = f"{tag_str}: {summary}"
        commit = _git(repo_path, "commit", "-m", msg)
        if commit.returncode != 0:
            raise RuntimeError(f"git commit failed: {commit.stderr.strip()}")

        sha_p = _git(repo_path, "rev-parse", "HEAD")
        commit_sha = sha_p.stdout.strip() if sha_p.returncode == 0 else None

        tag = _git(repo_path, "tag", tag_str)
        if tag.returncode != 0:
            # Roll back the commit so the repo doesn't keep a tag-less release commit
            _git(repo_path, "reset", "--soft", "HEAD~1")
            raise RuntimeError(f"git tag {tag_str} failed: {tag.stderr.strip()}")

        return CeremonyResult(
            cut=True,
            version=version_str,
            tag=tag_str,
            commit_sha=commit_sha,
            summary=summary,
            bullets=kept_bullets,
        )
    except Exception as exc:
        # Rollback files
        for p, original in backups:
            try:
                p.write_text(original, encoding="utf-8")
            except Exception:
                pass
        # Unstage anything we added
        for rel in staged_rel:
            _git(repo_path, "reset", "HEAD", "--", rel)
        return CeremonyResult(cut=False, skipped_reason=f"ceremony failed: {exc}")
