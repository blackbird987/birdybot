"""A temporary copy of what an answering session may read about a repo.

Tracked files at HEAD plus that repo's newest session reports, filtered and
redacted, in a directory outside any repo. It reads blobs with ``git ls-tree`` and
``git cat-file --batch`` rather than ``git archive``: archive runs ``export-subst``
and hands back a tar, and neither is wanted here. Pathlib throughout, because this
also runs on the Windows laptop. See docs/design/peer-link.md.

The session never sees a repo: not its config, its hooks, its working tree or
anything uncommitted. The allowlist below is a default about what is worth
sharing, not the boundary. The session's own confinement is ``bot/peer_confine.py``.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from bot.claude.sandbox import GIT_SAFE
from bot.platform.redact import redact_secrets, scrub_known
from bot.procutil import NOWND

log = logging.getLogger(__name__)

EXPORT_PREFIX = "bot-peer-export-"
REPO_DIR = "repo"
QUESTION_FILE = "_question.txt"
NOTES_FILE = "_export_notes.txt"
REPORTS_DIR = "_session_reports"
# CLAUDE.md would load as instructions; the renamed copy is only a file to read.
RENAMED_INSTRUCTIONS = "CLAUDE.md.txt"

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 20_000
MAX_REPORTS = 20
_GIT_TIMEOUT = 120
_NOTES_LIST_MAX = 200

_TOOL_CONFIG = {".claude", ".cursor", ".git", ".vscode", ".idea", ".mcp.json"}
_EXCLUDED_NAMES = {".mcp.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".netrc", ".npmrc",
                   ".pypirc", ".git-credentials"}
_SECRET_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der", ".jks",
                      ".keystore", ".kdbx", ".gpg", ".asc", ".ppk"}
_SECRET_WORDS = {"secret", "secrets", "credential", "credentials", "token", "tokens",
                 "password", "passwords", "passwd", "apikey", "apikeys"}
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")
_INSTRUCTION_FILES = {"claude.md", "claude.local.md", "agents.md"}
_WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)),
                    *(f"LPT{n}" for n in range(1, 10))}


def clean(text: str) -> str:
    """Secrets out of anything that crosses the link or stays on disk."""
    return redact_secrets(scrub_known(text))


def filename_looks_secret(name: str) -> bool:
    """Would this file's NAME suggest it holds a credential?

    Not ``secret_detect.name_looks_secret``: that one judges environment variable
    names and splits on underscores only, so ``credentials.json`` and
    ``api-token.txt`` would both get past it. ``auth_service.py``, ``key_utils.py``
    and ``webhook_handler.py`` are ordinary code and have to survive this.
    """
    lowered = name.lower()
    if lowered in _EXCLUDED_NAMES or lowered.startswith(".env") or lowered.endswith(".env"):
        return True
    stem, suffix = os.path.splitext(lowered)
    if suffix in _SECRET_EXTENSIONS:
        return True
    words = _WORD_SPLIT.split(stem)
    if "api" in words and "key" in words:
        return True
    return any(word in _SECRET_WORDS for word in words)


def _exclusion(path: str, mode: str, size: int) -> str | None:
    """Why this tree entry stays out, or None. Runs on git's own path, always ``/``."""
    if mode not in ("100644", "100755"):
        return "not a regular file"  # symlinks and submodules
    parts = PurePosixPath(path).parts
    if any(part == ".." for part in parts) or path.startswith("/"):
        return "unsafe path"
    # Lower-cased because `.CLAUDE` is the same folder on Windows.
    if any(part.lower() in _TOOL_CONFIG or part.lower().startswith(".git") for part in parts):
        return "tool configuration"
    if any(filename_looks_secret(part) for part in parts):
        return "name suggests a secret"
    if size > MAX_FILE_BYTES:
        return "over the size limit"
    return None


def _unsafe_part(part: str) -> bool:
    """A name that only means itself on Linux. Refused everywhere, so both sides match."""
    if "\\" in part or ":" in part or part != part.rstrip(". "):
        return True
    if any(0xDC80 <= ord(ch) <= 0xDCFF for ch in part):  # not valid UTF-8 in git
        return True
    return part.split(".")[0].upper() in _WINDOWS_DEVICES


def _destination(root: Path, path: str) -> Path | None:
    """Where a tracked file lands, or None for a path this OS could not hold safely."""
    parts = list(PurePosixPath(path).parts)
    if parts[-1].lower() in _INSTRUCTION_FILES:
        parts[-1] = RENAMED_INSTRUCTIONS if parts[-1].lower() == "claude.md" else parts[-1] + ".txt"
    # A backslash, drive colon or device name is a plain character on Linux and a
    # way out of the directory on Windows. Git can track all of them.
    if any(_unsafe_part(part) for part in parts):
        return None
    dest = root.joinpath(*parts)
    return dest if dest.resolve().is_relative_to(root.resolve()) else None


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *GIT_SAFE, "-C", str(repo), "--no-optional-locks", *args],
        input=stdin, capture_output=True, timeout=_GIT_TIMEOUT, check=True, **NOWND,
    ).stdout


@dataclass
class Export:
    root: Path
    repo_dir: Path
    files: int = 0
    omitted: dict[str, list[str]] = field(default_factory=dict)

    def note(self, reason: str, path: str) -> None:
        self.omitted.setdefault(reason, []).append(path)


def _tree(repo: Path) -> list[tuple[str, str, str, int]]:
    """(mode, sha, path, size) for every entry at HEAD. Raises on a repo with no commits."""
    raw = _git(repo, "ls-tree", "-r", "-z", "-l", "--full-tree", "HEAD")
    rows = []
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        meta, _, name = entry.partition(b"\t")
        fields = meta.decode("ascii", "replace").split()
        if len(fields) != 4:
            continue
        mode, kind, sha, size = fields
        path = name.decode("utf-8", "surrogateescape")
        rows.append((mode, sha, path, int(size) if size.isdigit() and kind == "blob" else -1))
    return rows


def _write_clean(dest: Path, data: bytes) -> bool:
    """Write text with secrets scrubbed. False for anything that is not UTF-8 text."""
    if b"\0" in data:
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(clean(text), encoding="utf-8")
    return True


def _copy_blobs(repo: Path, export: Export) -> None:
    keep: list[tuple[str, str, Path]] = []
    total = 0
    for mode, sha, path, size in _tree(repo):
        reason = _exclusion(path, mode, size)
        if reason:
            export.note(reason, path)
            continue
        try:
            dest = _destination(export.repo_dir, path)
        except OSError:
            dest = None
        if dest is None:
            export.note("unsafe path", path)
            continue
        if len(keep) >= MAX_FILES or total + size > MAX_TOTAL_BYTES:
            export.note("over the total limit", path)
            continue
        keep.append((sha, path, dest))
        total += size
    if not keep:
        return
    out = _git(repo, "cat-file", "--batch", stdin="".join(f"{sha}\n" for sha, _, _ in keep).encode())
    position = 0
    for sha, path, dest in keep:
        end = out.index(b"\n", position)
        header = out[position:end].decode("ascii", "replace").split()
        if len(header) != 3 or header[0] != sha or header[1] != "blob":
            raise ValueError(f"unexpected reply from git for {path!r}")
        size = int(header[2])
        data = out[end + 1:end + 1 + size]
        position = end + 1 + size + 1
        try:
            written = _write_clean(dest, data)
        except OSError:
            # One name this filesystem cannot hold must not cost the whole answer.
            export.note("could not be written", path)
            continue
        if written:
            export.files += 1
        else:
            export.note("binary", path)


def _write_notes(export: Export) -> None:
    lines = [
        "This directory is a filtered copy of the repo's tracked files at HEAD.",
        "Uncommitted work and git history are not here. Secrets were scrubbed from what is.",
    ]
    for reason, paths in sorted(export.omitted.items()):
        lines.append(f"\nLeft out ({reason}), {len(paths)}:")
        lines.extend(f"  {path}" for path in paths[:_NOTES_LIST_MAX])
        if len(paths) > _NOTES_LIST_MAX:
            lines.append(f"  ...and {len(paths) - _NOTES_LIST_MAX} more")
    # errors=replace: git paths that are not UTF-8 arrive surrogate-escaped.
    (export.repo_dir / NOTES_FILE).write_text("\n".join(lines), encoding="utf-8", errors="replace")


def _write_reports(export: Export, reports: list[tuple[str, str]]) -> None:
    for name, text in reports[:MAX_REPORTS]:
        dest = export.repo_dir / REPORTS_DIR / f"{name}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        # errors=replace: a report is whatever the CLI printed, and one bad character
        # must not cost the answer.
        dest.write_text(clean(text), encoding="utf-8", errors="replace")


def build_export(
    repo_path: str | os.PathLike, question_block: str,
    reports: list[tuple[str, str]] | None = None,
    base_dir: str | os.PathLike | None = None,
) -> Export:
    """Make the export. Blocking: callers run it off the event loop.

    ``reports`` is (name, text) for the newest session reports. The caller removes
    the result with ``remove_export`` in a ``finally``. A failure here cleans up
    after itself and raises, since a half-made export is not something to run on.
    """
    root = Path(tempfile.mkdtemp(prefix=EXPORT_PREFIX, dir=base_dir))
    try:
        repo_dir = root / REPO_DIR
        repo_dir.mkdir()
        export = Export(root=root, repo_dir=repo_dir)
        _copy_blobs(Path(repo_path), export)
        _write_reports(export, reports or [])
        _write_notes(export)
        (repo_dir / QUESTION_FILE).write_text(question_block, encoding="utf-8")
        return export
    except BaseException:
        remove_export(root)
        raise


def remove_export(root: str | os.PathLike) -> None:
    shutil.rmtree(root, ignore_errors=True)


def sweep_exports(base_dir: str | os.PathLike | None = None) -> int:
    """Remove exports left behind by a process that died. Boot recovery calls this."""
    base = Path(base_dir) if base_dir else Path(tempfile.gettempdir())
    swept = 0
    try:
        leftovers = [p for p in base.iterdir() if p.name.startswith(EXPORT_PREFIX) and p.is_dir()]
    except OSError:
        return 0
    for path in leftovers:
        remove_export(path)
        swept += 1
    return swept
