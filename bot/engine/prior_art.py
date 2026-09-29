"""What was already tried in the code a plan touches, read from git.

A session judging a plan only knows what is in its own context, so an idea
that was built, shipped and later ripped out for a good reason comes back
looking new. The auto-armed wake is the worked example: added in cdc54c8,
removed in d5f8aa8 because it fired on prose that merely discussed a job, and
proposed again weeks later by a session that had never seen either commit.
The removal commit sat in `git log` of the very file being changed the whole
time. Nobody looked, because nothing asked anyone to.

So the bot looks, and hands the result to whichever step is judging the plan:
the plan review, the build a `/chain` directive launches, and the weekly
prompt review. Retrieval is deterministic and lives here; judgment stays with
the model. Two reasons it is the bot doing the reading rather than the
session:

- **The plan reviewer cannot run git.** It runs behind the read-only floor
  (`workflows._enforce_readonly_floor`), which closes Bash along with the
  write tools. That floor exists because Bash is a write backdoor, and it is
  not loosened for this.
- **An instruction to "check the history" is the soft guard.** It gets skipped
  exactly when the session is confident, which is exactly when it is wrong.

Everything here fails open. A git read that cannot answer, times out, or runs
outside a repo produces no finding and a debug line, never an exception and
never a blocked step: a history check that can stop work is a history check
that gets switched off.

What is deliberately NOT used: pickaxe (`-S`/`-G`) across the whole history.
Measured on AIAgent (6,087 commits) it costs about 3.8s per term, against
about 30ms for a path-limited `git log`. Paths and commit messages carry the
signal for a fraction of the price; a rename is caught because the removal
commit touched the file the plan names.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from pathlib import Path

from bot import config
from bot.procutil import run_capture

log = logging.getLogger(__name__)

PRIOR_ART_MARKER = "## Prior history of the code this plan touches"

# Appended after the block when it rides a /chain plan into the build. The
# build session is the only reader left that can still stop, and asking is
# cheaper than building something that was removed on purpose.
CHAIN_INSTRUCTION = (
    "If this plan clearly re-adds something a [reversal] commit above removed, "
    "and the plan does not say why this time is different, ask the user with "
    "AskUserQuestion before building. Unrelated history is context only: do "
    "not stop for it."
)

_HEADER = (
    "Collected by the bot from git. [reversal] marks a commit that removed or "
    "undid something in these files. `git show <sha>` has the full reasoning."
)

# A subject opening with one of these words is a commit that took something
# out. Anchored to a line start, so git matches it against any line of the
# message; only a matching SUBJECT is tagged, see `_path_entries`.
_REVERSAL_WORDS = (
    "revert|remove|drop|retire|rip|back out|undo|disable|delete|replace|stop"
)
_REVERSAL_GREP = rf"^({_REVERSAL_WORDS})\b"
_REVERSAL_SUBJECT_RE = re.compile(rf"^({_REVERSAL_WORDS})\b", re.IGNORECASE)

_MAX_PATHS = 8
_MAX_IDENTIFIERS = 6
_REVERSALS_PER_PATH = 8
_RECENT_PER_PATH = 5
_HITS_PER_IDENTIFIER = 4
_SUBJECT_MAX = 140

# Per git call, and for the whole collection. A wedged object store must cost
# a few seconds of a plan review, not the review.
_GIT_TIMEOUT_SECS = 5.0
_TOTAL_BUDGET_SECS = 10.0

_FMT = "--format=%h%x1f%ad%x1f%s"

# A token that could be a path: letters, digits and the usual path
# punctuation. `:` is excluded so `workflows.py:222` yields the file.
_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_./\\-]+")
_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{5,}")


# --- Extraction from the plan text ---------------------------------------------

def _normalise_path(repo_path: str, token: str) -> str | None:
    tok = token.strip().strip(".,;()[]{}'\"").replace("\\", "/")
    if not tok or tok.startswith("-"):
        return None
    if os.path.isabs(tok):
        try:
            tok = os.path.relpath(tok, repo_path)
        except ValueError:
            return None
    tok = os.path.normpath(tok).replace("\\", "/")
    if tok.startswith("..") or tok in (".", ""):
        return None
    return tok


def _touched_paths(repo_path: str, text: str) -> list[str]:
    """Files the plan names that exist in the repo, in order, deduped, capped.

    A bare filename (``workflows.py``) is resolved only when exactly one
    tracked file has that name; guessing between two would put the wrong
    file's history in front of the reviewer.
    """
    root = Path(repo_path)
    found: list[str] = []
    bare: list[str] = []
    for m in _PATH_TOKEN_RE.finditer(text or ""):
        raw = m.group(0)
        if "/" not in raw and "\\" not in raw and not _EXT_RE.search(raw.rstrip(".,;")):
            continue
        rel = _normalise_path(repo_path, raw)
        if not rel or rel in found:
            continue
        if (root / rel).is_file():
            found.append(rel)
        elif "/" not in rel and rel not in bare:
            bare.append(rel)
        if len(found) >= _MAX_PATHS:
            return found
    if bare and len(found) < _MAX_PATHS:
        tracked = _git(repo_path, ["ls-files"])
        if tracked:
            by_name: dict[str, list[str]] = {}
            for line in tracked.splitlines():
                by_name.setdefault(line.rsplit("/", 1)[-1], []).append(line)
            for name in bare:
                hits = by_name.get(name, [])
                if len(hits) == 1 and hits[0] not in found:
                    found.append(hits[0])
                if len(found) >= _MAX_PATHS:
                    break
    return found


def _identifiers(text: str) -> list[str]:
    """Backticked names that look like code, not English, deduped and capped.

    A dotted name is searched by its last segment (`config.PLAN_REVIEW_PROMPT`
    becomes `PLAN_REVIEW_PROMPT`), because that is how commit messages spell
    it. "Looks like code" means an underscore or an inner capital: a
    backticked `review` would match half the history and say nothing.
    """
    out: list[str] = []
    for m in _BACKTICK_RE.finditer(text or ""):
        inner = m.group(1).strip()
        if "/" in inner or "\\" in inner:
            continue
        im = _IDENT_RE.match(inner)
        if not im:
            continue
        name = im.group(0).rstrip(".")
        if "." in name and name.rsplit(".", 1)[-1] in _FILE_EXTS:
            continue    # a filename (`state.json`), which `_touched_paths` owns
        name = name.rsplit(".", 1)[-1]
        if len(name) < 6:
            continue
        if "_" not in name.strip("_") and not re.search(r"[a-z][A-Z]", name) \
                and not name.isupper():
            continue
        if name not in out:
            out.append(name)
        if len(out) >= _MAX_IDENTIFIERS:
            break
    return out


_FILE_EXTS = frozenset({
    "py", "md", "json", "toml", "yml", "yaml", "txt", "cfg", "ini", "js",
    "ts", "tsx", "jsx", "cs", "csproj", "sh", "ps1", "bat", "rs", "go", "html",
    "css", "sql", "log", "lock", "env",
})


# --- Git -------------------------------------------------------------------------

def _git(repo_path: str, args: list[str], deadline: float | None = None) -> str | None:
    """One git read. None on any failure, logged at debug, never raised."""
    timeout = _GIT_TIMEOUT_SECS
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.debug("prior_art: budget spent, skipping git %s", args[:2])
            return None
        timeout = min(timeout, remaining)
    try:
        res = run_capture(
            ["git", "-C", repo_path, *args],
            timeout=timeout, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("prior_art: git %s failed: %s", args[:2], e)
        return None
    if res.returncode != 0:
        log.debug(
            "prior_art: git %s exited %s: %s",
            args[:2], res.returncode, (res.stderr or "").strip()[:200],
        )
        return None
    return res.stdout


def _is_git_repo(repo_path: str) -> bool:
    if not repo_path or not os.path.isdir(repo_path):
        return False
    out = _git(repo_path, ["rev-parse", "--is-inside-work-tree"])
    return bool(out) and out.strip() == "true"


def _parse_log(out: str | None) -> list[tuple[str, str, str]]:
    commits: list[tuple[str, str, str]] = []
    for line in (out or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3 and parts[0]:
            commits.append((parts[0], parts[1], parts[2]))
    return commits


def _line(sha: str, date: str, subject: str, reversal: bool) -> str:
    subject = subject.strip()
    if len(subject) > _SUBJECT_MAX:
        subject = subject[:_SUBJECT_MAX - 1].rstrip() + "…"
    tag = "[reversal] " if reversal else ""
    return f"- {sha} {date} {tag}{subject}"


# --- Rendering -------------------------------------------------------------------

# Priorities: what is worth the budget when the budget runs out.
_P_REVERSAL, _P_IDENT, _P_RECENT = 0, 1, 2


def _render(
    sections: list[str],
    entries: list[tuple[int, str, str, str]],
    max_chars: int,
    head: list[str],
) -> str:
    """Pick entries by priority under the cap, then print them by section.

    ``entries`` is ``(priority, section, sha, line)``. A sha is shown once, in
    the most important place it earned. Selection and layout are separate so a
    tight cap drops recent commits before it drops a single reversal, while
    the reader still sees one heading per file.
    """
    seen: set[str] = set()
    chosen: dict[str, list[tuple[int, str]]] = {}
    used = sum(len(h) + 1 for h in head)
    for prio, section, sha, line in sorted(entries, key=lambda e: e[0]):
        if sha in seen:
            continue
        cost = len(line) + 1
        if section not in chosen:
            cost += len(section) + 1
        if used + cost > max_chars:
            continue
        seen.add(sha)
        used += cost
        chosen.setdefault(section, []).append((prio, line))
    if not chosen:
        return ""
    out = list(head)
    for section in sections:
        lines = chosen.get(section)
        if not lines:
            continue
        out.append(section)
        out.extend(line for _, line in sorted(lines, key=lambda x: x[0]))
    return "\n".join(out)


def _path_entries(
    repo_path: str, path: str, deadline: float,
) -> list[tuple[int, str, str]]:
    """(priority, sha, line) for one file: every reversal ever, then recent."""
    got: list[tuple[int, str, str]] = []
    rev = _git(repo_path, [
        "log", "--no-merges", "-i", "-E", f"--grep={_REVERSAL_GREP}",
        "-n", str(_REVERSALS_PER_PATH), _FMT, "--date=short", "--", path,
    ], deadline)
    for sha, date, subj in _parse_log(rev):
        # The grep matched some line of the message. Only a subject that
        # opens with the verb is tagged: a body line starting "Stop" is as
        # often prose as it is a removal, so it rides along as plain context.
        if _REVERSAL_SUBJECT_RE.match(subj.strip()):
            got.append((_P_REVERSAL, sha, _line(sha, date, subj, True)))
        else:
            got.append((_P_RECENT, sha, _line(sha, date, subj, False)))
    recent = _git(repo_path, [
        "log", "--no-merges", "-n", str(_RECENT_PER_PATH), _FMT,
        "--date=short", "--", path,
    ], deadline)
    for sha, date, subj in _parse_log(recent):
        tagged = bool(_REVERSAL_SUBJECT_RE.match(subj.strip()))
        got.append((
            _P_REVERSAL if tagged else _P_RECENT, sha,
            _line(sha, date, subj, tagged),
        ))
    return got


def collect(repo_path: str, plan_text: str, *, max_chars: int | None = None) -> str:
    """The prior-history block for a plan, or "" when there is nothing to say.

    Returns "" when disabled, outside a git repo, when the plan names no
    existing file and no code identifier, when git finds nothing, and on any
    failure. Never raises. Synchronous: call sites run it in a thread.
    """
    try:
        return _collect(repo_path, plan_text, max_chars)
    except Exception:
        log.warning("prior_art: collect failed for %s", repo_path, exc_info=True)
        return ""


def _collect(repo_path: str, plan_text: str, max_chars: int | None) -> str:
    if not config.PRIOR_ART_ENABLED or not plan_text or not repo_path:
        return ""
    if max_chars is None:
        max_chars = config.PRIOR_ART_MAX_CHARS
    if not _is_git_repo(repo_path):
        return ""
    deadline = time.monotonic() + _TOTAL_BUDGET_SECS
    paths = _touched_paths(repo_path, plan_text)
    idents = _identifiers(plan_text)
    if not paths and not idents:
        return ""

    sections: list[str] = []
    entries: list[tuple[int, str, str, str]] = []
    for path in paths:
        section = f"### {path}"
        sections.append(section)
        for prio, sha, line in _path_entries(repo_path, path, deadline):
            entries.append((prio, section, sha, line))
    for ident in idents:
        section = f"### Commit messages mentioning `{ident}`"
        sections.append(section)
        out = _git(repo_path, [
            "log", "--no-merges", "-F", "-i", f"--grep={ident}",
            "-n", str(_HITS_PER_IDENTIFIER), _FMT, "--date=short",
        ], deadline)
        for sha, date, subj in _parse_log(out):
            tagged = bool(_REVERSAL_SUBJECT_RE.match(subj.strip()))
            entries.append((
                _P_REVERSAL if tagged else _P_IDENT, section, sha,
                _line(sha, date, subj, tagged),
            ))
    return _render(sections, entries, max_chars, [PRIOR_ART_MARKER, _HEADER])


def block_history(
    repo_path: str,
    names: list[str],
    *,
    per_name: int = 5,
    max_chars: int = 1500,
    path: str = "bot/config.py",
) -> str:
    """Recent commits that edited each named prompt block, for the prompt review.

    Follows the block's own line range (``git log -L``), from its definition
    to the next top-level constant, so a commit that rewrote the wording
    without touching the name still shows. Falls back to commits whose diff
    mentions the name when the range cannot be resolved. One file, so both
    are cheap. Never raises; "" when disabled or nothing is found.
    """
    try:
        if not config.PRIOR_ART_ENABLED or not names or not _is_git_repo(repo_path):
            return ""
        deadline = time.monotonic() + _TOTAL_BUDGET_SECS
        sections: list[str] = []
        entries: list[tuple[int, str, str, str]] = []
        for name in names:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name or ""):
                continue
            section = f"### {name}"
            sections.append(section)
            out = _git(repo_path, [
                "log", "--no-merges", "-n", str(per_name), _FMT, "--date=short",
                "--no-patch", "-L",
                f"/^{name}\\b/,/^[A-Z_][A-Z0-9_]* *[:=]/:{path}",
            ], deadline)
            if out is None:
                out = _git(repo_path, [
                    "log", "--no-merges", "-n", str(per_name), _FMT,
                    "--date=short", f"-G\\b{name}\\b", "--", path,
                ], deadline)
            for sha, date, subj in _parse_log(out):
                tagged = bool(_REVERSAL_SUBJECT_RE.match(subj.strip()))
                # Keyed per section, not per sha: one commit that edited two
                # blocks belongs under both, since each block is judged alone.
                entries.append((
                    _P_REVERSAL if tagged else _P_RECENT, section,
                    f"{section}:{sha}", _line(sha, date, subj, tagged),
                ))
        head = [
            f"EDIT HISTORY OF THE OWNING BLOCKS (git log of {path}, newest "
            f"first; [reversal] marks a commit that removed or undid something)",
        ]
        return _render(sections, entries, max_chars, head)
    except Exception:
        log.warning("prior_art: block_history failed", exc_info=True)
        return ""


# --- Riding a /chain plan into the build -----------------------------------------

_ATTACH_SEP = "\n\n" + PRIOR_ART_MARKER + "\n"


def attach(plan_body: str, block: str) -> str:
    """The stored /chain override: the plan, then the block and its instruction."""
    if not block:
        return plan_body
    return f"{plan_body}\n\n{block}\n\n{CHAIN_INSTRUCTION}"


def split_attached(text: str) -> tuple[str, str]:
    """Undo `attach`: ``(plan, tail)``, where tail is "" when nothing was attached.

    Searches from the end: the attached block is always last, and a plan that
    quotes the marker itself (a plan about this module does) must not be cut
    at its own quotation.
    """
    idx = text.rfind(_ATTACH_SEP)
    if idx == -1 or not text.rstrip().endswith(CHAIN_INSTRUCTION):
        return text, ""
    return text[:idx], text[idx + 2:]
