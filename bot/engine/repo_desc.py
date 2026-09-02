"""A one-line "what is this repo about" blurb for a repo's Control Room.

The control room embed used the filesystem path as its entire description, so
a forum full of repos told you where each one lived and nothing about what it
was for. Every repo already says what it is somewhere — in its CLAUDE.md, its
README, or its package metadata — so the blurb is *derived* rather than typed
in, and `.claude/repo.json` exists only for the cases where the derived line is
wrong.

The cache is the load-bearing part. `refresh_control_room` runs on every
instance start and completion, several repos at a time; reading six files per
refresh to render one sentence that changes once a month would be pure waste.
So the hot path is stat-only, and the file bodies are read again only when the
newest candidate file's mtime moves.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

MAX_LEN = 120

# Candidate sources in priority order: the manual override first, then the two
# files a human actually reads, then package metadata as a last resort. Each
# entry is (relative path, parser name).
_SOURCES: tuple[tuple[str, str], ...] = (
    (".claude/repo.json", "repo.json"),
    ("CLAUDE.md", "CLAUDE.md"),
    ("README.md", "README.md"),
    ("pyproject.toml", "pyproject.toml"),
    ("package.json", "package.json"),
    ("Cargo.toml", "Cargo.toml"),
)

# Read at most this much of a markdown file. The blurb is in the first few
# lines; a 400 KB README must not become a 400 KB read on the hot path.
_MAX_READ_BYTES = 64 * 1024
# How much of the opening paragraph to gather before truncating it. Enough
# that a wrapped sentence is always whole, small enough to stay cheap.
_MAX_PARAGRAPH = 600

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^\s*#")
_BULLET_RE = re.compile(r"^\s*([-*+]\s|\d+[.)]\s)")
_RULE_RE = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_SETEXT_RE = re.compile(r"=+|-+")
_BADGE_RE = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)|!\[[^\]]*\]\([^)]*\)|\[[^\]]*\]\([^)]*\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")

_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_BADGE_LINK_RE = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)")
# Paired emphasis only.  A blunt ``.replace("_", "")`` would turn a sentence
# about ``data/state.json`` and ``repo_desc`` into mush, and snake_case shows
# up in exactly the kind of first line a developer README opens with.
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_ITALIC_US_RE = re.compile(r"(?<![\w_])_(?!_)(.+?)(?<!_)_(?![\w_])")
_CODE_RE = re.compile(r"`([^`]*)`")

# A sentence end, for truncation. Requires the following space (or end of
# string) so "v1.2" and "e.g." do not read as boundaries.
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")
# Below this a single-sentence cut is too terse to be a description, so the
# word-boundary ellipsis is better.
_MIN_SENTENCE_LEN = 24


# --- text cleanup ---------------------------------------------------------


def _is_badge_line(line: str) -> bool:
    """True when a line is nothing but links, images and punctuation.

    A README's first non-heading line is very often a row of CI/coverage
    badges. Stripping every link and image and finding nothing left is the
    only reliable tell — badge rows have no fixed shape.
    """
    rest = _BADGE_RE.sub("", line)
    rest = _HTML_TAG_RE.sub("", rest)
    return not re.search(r"[A-Za-z0-9]", rest)


def clean_text(raw: str) -> str:
    """Reduce a markdown fragment to a single plain-text line."""
    s = _BADGE_LINK_RE.sub("", raw)
    s = _IMAGE_RE.sub("", s)
    s = _LINK_RE.sub(r"\1", s)
    s = _HTML_TAG_RE.sub("", s)
    s = _CODE_RE.sub(r"\1", s)
    s = _BOLD_RE.sub(lambda m: m.group(1) or m.group(2) or "", s)
    s = _ITALIC_STAR_RE.sub(r"\1", s)
    s = _ITALIC_US_RE.sub(r"\1", s)
    s = s.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _truncate(s: str) -> str:
    """Fit ``s`` to the cap, preferring to end on a full sentence.

    Cutting mid-sentence and appending an ellipsis is the fallback, not the
    goal: most repos open with a short sentence naming the thing followed by
    detail, and "Agentic media downloader." is a better blurb than the first
    118 characters of the paragraph it starts.
    """
    if len(s) <= MAX_LEN:
        return s
    # Longest run of whole sentences that fits, as long as it is substantial
    # enough to be a description on its own.
    best = ""
    for m in _SENTENCE_END_RE.finditer(s):
        end = m.end()
        if end > MAX_LEN:
            break
        best = s[:end]
    if len(best) >= _MIN_SENTENCE_LEN:
        return best.strip()
    cut = s[: MAX_LEN - 1]
    space = cut.rfind(" ")
    if space > MAX_LEN // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-—") + "…"


def _finalise(raw: str | None) -> str | None:
    if not raw:
        return None
    text = _truncate(clean_text(raw))
    return text if len(text) >= 3 else None


# --- per-source parsers ---------------------------------------------------


def _read_head(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        return fh.read(_MAX_READ_BYTES)


def _first_prose_line(text: str) -> str | None:
    """First *paragraph* of a markdown document that reads as prose.

    Skips the title — both ``# ATX`` and the underlined ``setext`` spelling —
    fenced code, bullets, block quotes, HTML comments, horizontal rules, table
    rows and badge rows. A wrong blurb is worse than none, and the title is
    the wrong blurb: the control room embed already shows the repo's name.
    """
    lines = text.splitlines()
    in_fence = False
    in_comment = False
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        i += 1
        if in_comment:
            if "-->" in stripped:
                in_comment = False
                after = stripped.split("-->", 1)[1].strip()
                if after and not _HEADING_RE.match(after):
                    return after
            continue
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        if stripped.startswith("<!--"):
            if "-->" not in stripped:
                in_comment = True
            continue
        if _HEADING_RE.match(stripped) or _RULE_RE.match(stripped):
            continue
        if _BULLET_RE.match(stripped) or stripped.startswith(">"):
            continue
        if stripped.startswith("|"):
            continue
        if _is_badge_line(stripped):
            continue
        # Setext heading: the line under it is all = or all -. Reached only
        # from a prose-looking line, so a bare "---" here cannot be a rule.
        if i < len(lines) and _SETEXT_RE.fullmatch(lines[i].strip()):
            i += 1
            continue
        # Take the rest of the paragraph too. A hard-wrapped README's first
        # line ends mid-sentence ("...take a plain-language request like"),
        # which as a blurb reads as truncation with no ellipsis to say so.
        para = [stripped]
        while i < len(lines) and len("".join(para)) < _MAX_PARAGRAPH:
            nxt = lines[i].strip()
            if (not nxt or _FENCE_RE.match(nxt) or _HEADING_RE.match(nxt)
                    or _RULE_RE.match(nxt) or _BULLET_RE.match(nxt)
                    or nxt.startswith((">", "|", "<!--"))
                    or _SETEXT_RE.fullmatch(nxt)):
                break
            para.append(nxt)
            i += 1
        return " ".join(para)
    return None


def _from_repo_json(path: Path) -> str | None:
    data = json.loads(_read_head(path))
    if isinstance(data, dict):
        value = data.get("description")
        if isinstance(value, str):
            return value
    return None


def _from_package_json(path: Path) -> str | None:
    data = json.loads(_read_head(path))
    if isinstance(data, dict):
        value = data.get("description")
        if isinstance(value, str):
            return value
    return None


def _from_toml(path: Path, tables: tuple[str, ...]) -> str | None:
    import tomllib

    data = tomllib.loads(_read_head(path))
    for table in tables:
        node: object = data
        for part in table.split("."):
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(part)
        if isinstance(node, dict):
            value = node.get("description")
            if isinstance(value, str):
                return value
    return None


def _parse(rel: str, path: Path) -> str | None:
    if rel == ".claude/repo.json":
        return _from_repo_json(path)
    if rel in ("CLAUDE.md", "README.md"):
        return _first_prose_line(_read_head(path))
    if rel == "pyproject.toml":
        return _from_toml(path, ("project", "tool.poetry"))
    if rel == "package.json":
        return _from_package_json(path)
    if rel == "Cargo.toml":
        return _from_toml(path, ("package",))
    return None


# --- resolution -----------------------------------------------------------


def _candidate(repo_path: str, rel: str) -> Path | None:
    """The readable file at ``rel``, or None.

    A symlink pointing outside the repo is refused: the blurb is derived from
    files the repo owns, and a control-room refresh must not be a way to read
    arbitrary paths on the host.
    """
    root = Path(repo_path)
    path = root / rel
    try:
        if not path.is_file():
            return None
        if root.resolve() not in path.resolve().parents:
            return None
    except OSError:
        return None
    return path


def resolve_repo_description_with_source(repo_path: str) -> tuple[str, str] | None:
    """Derive ``(text, source)`` for a repo, or None when nothing says anything.

    Every source is guarded on its own — an unreadable README must not stop
    the pyproject description behind it from being found, and neither may ever
    take down a control-room refresh.
    """
    if not repo_path:
        return None
    for rel, label in _SOURCES:
        try:
            path = _candidate(repo_path, rel)
            if path is None:
                continue
            text = _finalise(_parse(rel, path))
            if text:
                return text, label
        except Exception:
            log.debug("repo description: %s unreadable in %s", rel, repo_path, exc_info=True)
    return None


def resolve_repo_description(repo_path: str) -> str | None:
    found = resolve_repo_description_with_source(repo_path)
    return found[0] if found else None


# --- cache ----------------------------------------------------------------


def source_signature(repo_path: str) -> tuple[str, float]:
    """``(signature, newest mtime)`` for the candidate files.

    Stat-only, so this is what the hot path pays: six ``os.stat`` calls, no
    reads.

    The signature names every candidate that exists and its own mtime, rather
    than just the newest one. Collapsing to a maximum looks equivalent — a
    file written now is newer than anything already there — but it is not:
    a source restored from a tarball or a network mount with a skewed clock
    carries a *future* mtime, and behind it a newly created ``.claude/repo.json``
    would never move the maximum and the override would silently never apply.
    ``newest`` is returned alongside only because it is worth reading in
    ``data/state.json`` when something looks stale.
    """
    parts: list[str] = []
    newest = 0.0
    for rel, _ in _SOURCES:
        try:
            mtime = os.stat(os.path.join(repo_path, rel)).st_mtime
        except OSError:
            continue
        parts.append(f"{rel}:{mtime!r}")
        newest = max(newest, mtime)
    return "|".join(parts), newest


def _is_fresh(cached: dict | None, repo_path: str, signature: str) -> bool:
    return bool(
        cached
        and cached.get("path") == repo_path
        and cached.get("sig") == signature
    )


def _record(store, repo_name: str, repo_path: str, signature: str, newest: float,
            found: tuple[str, str] | None) -> str | None:
    text, source = found if found else ("", "")
    store.set_repo_description(repo_name, text, source, signature, newest, repo_path)
    return text or None


def refresh_repo_description_sync(store, repo_name: str, repo_path: str) -> str | None:
    """Cached blurb for a repo, re-derived only when its sources changed.

    A repo that says nothing about itself caches the empty string, so the
    "nothing found" case costs stats rather than six failed opens per refresh.
    """
    if not repo_path:
        return None
    signature, newest = source_signature(repo_path)
    cached = store.get_repo_description_entry(repo_name)
    if _is_fresh(cached, repo_path, signature):
        return cached.get("text") or None
    return _record(store, repo_name, repo_path, signature, newest,
                   resolve_repo_description_with_source(repo_path))


async def refresh_repo_description(store, repo_name: str, repo_path: str) -> str | None:
    """Async twin of :func:`refresh_repo_description_sync`.

    Only the filesystem work goes to a thread; the store is read and written
    on the event loop, because ``StateStore`` is not thread-safe and its save
    path rewrites the whole state file.
    """
    if not repo_path:
        return None
    try:
        signature, newest = await asyncio.to_thread(source_signature, repo_path)
        cached = store.get_repo_description_entry(repo_name)
        if _is_fresh(cached, repo_path, signature):
            return cached.get("text") or None
        found = await asyncio.to_thread(resolve_repo_description_with_source, repo_path)
        return _record(store, repo_name, repo_path, signature, newest, found)
    except Exception:
        log.debug("repo description refresh failed for %s", repo_name, exc_info=True)
        return None


# --- manual override ------------------------------------------------------


def repo_json_path(repo_path: str) -> Path:
    return Path(repo_path) / ".claude" / "repo.json"


def write_manual_description(repo_path: str, text: str | None) -> None:
    """Set or clear ``description`` in ``<repo>/.claude/repo.json``.

    Other keys in the file are preserved — this sits next to test.json,
    workflow.json and sensors.json, and a future repo.json may well grow more
    fields than this one.
    """
    path = repo_json_path(repo_path)
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    if text:
        data["description"] = text
    else:
        data.pop("description", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
