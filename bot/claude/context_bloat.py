"""Find the instruction files that keep refilling a session's context.

An autocompact thrash has two shapes. The common one is a burst of large reads
or tool output, and a resume clears it because the thrash counter lives in the
CLI process. The other is a fixed-size load the CLI attaches on its own: a
nested ``CLAUDE.md`` is attached as a ``nested_memory`` row every time the
session Reads, Edits or Writes a file in that folder, and the project's own
instruction files come back after every compaction. When one of those is a few
hundred KB, every compaction is immediately undone and no number of resumes
helps. t-9142 (AIAgent, 2026-10-11) re-attached a 317K-char
``Services/Automation/CLAUDE.md`` 22 times before the CLI gave up.

This module only *names* such a file, from the session's own transcript, so
the resumed attempt can be told to work around it and the final failure can
say what to slim. It decides nothing: the runner retries exactly as often with
or without a finding.

Everything here fails open. A transcript that is missing, unreadable or in a
shape we do not recognise is no finding and a debug line, never an exception,
because this runs inside the failure path of a run that is already failing.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from bot import config

log = logging.getLogger(__name__)

# Lines are only parsed when they contain one of these. A transcript runs to
# tens of MB and nearly every line is a message or tool result, so decoding
# all of it would cost seconds for rows we would throw away. The CLI writes
# compact JSON, but the spaced spelling costs nothing to accept.
_MARKERS = (
    '"nested_memory"',
    '"type":"instructions"',
    '"type": "instructions"',
)

KIND_NESTED = "nested"
KIND_INSTRUCTIONS = "instructions"


@dataclass
class Autoload:
    """One instruction file the CLI kept putting back into context."""

    path: str
    display_path: str
    chars: int
    loads: int
    kind: str  # KIND_NESTED or KIND_INSTRUCTIONS

    @property
    def kb(self) -> int:
        return max(1, round(self.chars / 1024))

    @property
    def approx_tokens_k(self) -> int:
        # ~4 chars per token is the usual rule of thumb for English and code;
        # the number is for scale in a sentence, not for accounting.
        return max(1, round(self.chars / 4000))


def _content_len(content) -> int:
    """Size of an attachment's content, whichever shape it arrived in.

    ``nested_memory`` carries a dict (``{"path", "type", "content"}``) and an
    instructions file entry carries the text directly. Anything else counts
    as zero rather than guessing.
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, dict):
        inner = content.get("content")
        if isinstance(inner, str):
            return len(inner)
    return 0


def _display(path: str, roots: list[str]) -> str:
    """Path relative to the deepest root that contains it, else as is.

    ``roots`` is tried deepest first, so a file inside a build worktree and
    the same file in the main repo come out with one name and merge.
    """
    for root in sorted((r for r in roots if r), key=len, reverse=True):
        try:
            rel = os.path.relpath(path, root)
        except ValueError:
            continue  # different drive on Windows
        rel = rel.replace("\\", "/")
        if rel != ".." and not rel.startswith("../"):
            return rel
    return path


def display_roots(repo_path: str | None, worktree_path: str | None) -> list[str]:
    """The directories a display path is measured from.

    A registered repo can be a subfolder of its git checkout (AIAgent's is
    ``AIAgent/`` inside the checkout), while a build worktree is a copy of the
    whole checkout. The repo's own folder inside the worktree is therefore not
    the worktree root, and is found through git. The checkout root is a root
    too, so an instruction file above the repo folder gets a short name rather
    than an absolute path. The CLI's own ``displayPath`` is not used for this:
    it is relative to wherever the session's shell was standing, so the same
    file shows up as ``../CLAUDE.md`` from one folder and
    ``AIAgent/Services/Automation/CLAUDE.md`` from another.
    """
    roots = [r for r in (repo_path, worktree_path) if r]
    if repo_path:
        try:
            from bot.claude.gitpaths import git_toplevel

            top = git_toplevel(repo_path)
            if top:
                rel = os.path.relpath(repo_path, top)
                if worktree_path and rel != "." and not rel.startswith(".."):
                    roots.append(os.path.join(worktree_path, rel))
                roots.append(top)
        except Exception:
            log.debug("context bloat: git toplevel lookup failed for %s",
                      repo_path, exc_info=True)
    return roots


def find_oversized_autoloads(
    session_file: Path, min_chars: int, roots: list[str] | None = None,
) -> list[Autoload]:
    """Every auto-attached instruction file over ``min_chars``, largest first.

    Streams the transcript line by line. A file is keyed by its path relative
    to ``roots`` (see :func:`display_roots`), so the same ``CLAUDE.md`` reached
    through the main repo and through a build worktree counts as one file
    loaded N times rather than two. Its size is the one it had on its LAST
    load, and the threshold is applied to that: a file slimmed mid-session is
    no longer the cause, and blaming it for the size it used to be would send
    the user to slim it again.
    """
    roots = [r for r in (roots or []) if r]
    found: dict[str, Autoload] = {}

    def _add(path: str, display: str, chars: int, kind: str) -> None:
        if not path:
            return
        key = display or path
        hit = found.get(key)
        if hit is None:
            found[key] = Autoload(path, key, chars, 1, kind)
            return
        hit.loads += 1
        hit.chars = chars
        hit.path = path
        # A file seen as a nested load is described as one: that is the shape
        # the Read advice applies to.
        if kind == KIND_NESTED:
            hit.kind = KIND_NESTED

    try:
        with open(session_file, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not any(m in line for m in _MARKERS):
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                att = row.get("attachment")
                if not isinstance(att, dict):
                    continue
                kind = att.get("type")
                if kind == "nested_memory":
                    path = att.get("path")
                    if not isinstance(path, str):
                        continue
                    _add(path, _display(path, roots),
                         _content_len(att.get("content")), KIND_NESTED)
                elif kind == "instructions":
                    files = att.get("files")
                    if not isinstance(files, list):
                        continue
                    for entry in files:
                        if not isinstance(entry, dict):
                            continue
                        path = entry.get("path")
                        if not isinstance(path, str):
                            continue
                        _add(path, _display(path, roots),
                             _content_len(entry.get("content")),
                             KIND_INSTRUCTIONS)
    except OSError as exc:
        log.debug("context bloat scan: cannot read %s: %s", session_file, exc)
        return []
    except Exception:
        log.debug("context bloat scan failed on %s", session_file, exc_info=True)
        return []

    big = [a for a in found.values() if a.chars >= min_chars]
    return sorted(big, key=lambda a: (-a.chars, a.display_path))


def locate_transcript(session_id: str, cwds: list[str]) -> Path | None:
    """The session's JSONL, newest copy wins.

    A build session's transcript can sit under the worktree's project dir, the
    main repo's, or both (the runner copies it between them so --resume works),
    on any configured account. The newest copy is the one the dead process was
    writing.
    """
    if not session_id:
        return None
    try:
        from bot.claude.session_fork import encode_project_path

        roots: list[Path] = []
        for acct in config.CLAUDE_ACCOUNTS or []:
            roots.append(Path(acct) / "projects")
        roots.extend(config.claude_projects_dirs())
        seen: set[str] = set()
        best: tuple[float, Path] | None = None
        for root in roots:
            for cwd in cwds:
                if not cwd:
                    continue
                p = root / encode_project_path(cwd) / f"{session_id}.jsonl"
                key = str(p)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if best is None or mtime > best[0]:
                    best = (mtime, p)
        return best[1] if best else None
    except Exception:
        log.debug("context bloat: transcript lookup failed for %s",
                  session_id[:12], exc_info=True)
        return None


def scan_session(
    session_id: str, repo_path: str | None, worktree_path: str | None,
) -> list[Autoload]:
    """Locate and scan in one call, for ``asyncio.to_thread``."""
    try:
        cwds = [c for c in (worktree_path, repo_path) if c]
        path = locate_transcript(session_id, cwds)
        if path is None:
            log.debug("context bloat: no transcript for %s",
                      (session_id or "")[:12])
            return []
        return find_oversized_autoloads(
            path, config.CONTEXT_BLOAT_MIN_CHARS,
            display_roots(repo_path, worktree_path),
        )
    except Exception:
        log.debug("context bloat scan failed for %s", (session_id or "")[:12],
                  exc_info=True)
        return []


def _reload_phrase(a: Autoload) -> str:
    """When the CLI puts the file back, in words. One wording for both texts."""
    if a.kind == KIND_NESTED:
        return "every time a file in that folder is Read, Edited or Written"
    return "at the start and after every compaction"


def _file_line(a: Autoload) -> str:
    return (
        f"- `{a.display_path}`: {a.kb} KB (~{a.approx_tokens_k}K tokens), "
        f"loaded {a.loads} time{'s' if a.loads != 1 else ''}; it is "
        f"re-attached {_reload_phrase(a)}"
    )


def render_note(findings: list[Autoload]) -> str:
    """The recovery note for the resumed attempt, or "" with no finding."""
    if not findings:
        return ""
    files = "\n".join(_file_line(a) for a in findings[:3])
    return config.CONTEXT_THRASH_BLOAT_NOTE.format(files=files)


def cause_line(findings: list[Autoload]) -> str:
    """One sentence for the final failure, naming the largest file."""
    if not findings:
        return ""
    a = findings[0]
    return (
        f"Cause: `{a.display_path}` is {a.kb} KB (~{a.approx_tokens_k}K "
        f"tokens) and is reloaded into context {_reload_phrase(a)}. Retrying "
        f"will not fix this; slim that file (move sections into separate docs "
        f"it links to)."
    )
