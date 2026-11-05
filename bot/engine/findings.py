"""What a repo's study records say is already known not to work.

The bot reads these so a plan reviewer, which runs with Bash closed, gets the
list without being able to run anything itself. Three shapes exist and all of
them have to read: the shared tool (`scripts/study/`) and the Ev-nova-remake and
funny-rock-polisher forks keep `shut` records under `study/findings`, while
strategy-lab's fork keeps `dead` ones under the `records_dir` its config names.
Its folder also holds a `common/` of non-records, so only the top level counts.

The header is parsed here rather than imported from `scripts/study/`, because
that package stays free of the bot and this module has no reason to reach into
`scripts/`. TOML, not a grep: a record's prose can hold a line starting "status".

Fails open: anything unreadable is no finding, never an exception. A repo with
no `study/config.toml` costs one stat per directory up to its git root. See
docs/design/findings.md.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_CONFIG = Path("study") / "config.toml"
_FENCE = "+++"


@dataclass(frozen=True)
class Layout:
    records_dir: Path
    records_rel: str
    dead_status: str


def _is_git_root(d: Path) -> bool:
    """A real repo root: `.git` is a file (a worktree's or submodule's pointer)
    or a folder with HEAD in it. A stray `.git` folder is neither, and stopping
    there hid aiagent's registry. scripts/study/study.py keeps the same rule,
    because neither side imports the other; test_findings.py holds them together.
    """
    dot_git = d / ".git"
    return dot_git.is_file() or (dot_git / "HEAD").is_file()


def layout(repo_path: str | Path) -> Layout | None:
    """Where this repo keeps its records, or None when it has no study folder.

    Walks up to the git root, because a repo can be registered as a subfolder of
    it and `study.py init` writes at the root.
    """
    try:
        for d in (Path(repo_path), *Path(repo_path).parents):
            config = d / _CONFIG
            if config.is_file():
                return _read_layout(d, config)
            if _is_git_root(d):
                return None
    except (OSError, ValueError):  # ValueError: bad TOML, or not UTF-8
        log.debug("findings: no usable config for %s", repo_path, exc_info=True)
    return None


def _read_layout(root: Path, config: Path) -> Layout | None:
    with open(config, "rb") as fh:
        cfg = tomllib.load(fh)
    if "records_dir" in cfg:
        rel, dead_status = cfg["records_dir"], "dead"
    else:
        rel, dead_status = cfg.get("findings_dir", "study/findings"), "shut"
    if not isinstance(rel, str) or not rel:
        return None
    return Layout(root / rel, rel, dead_status)


def _header(path: Path) -> dict | None:
    lines: list[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        if fh.readline().strip() != _FENCE:
            return None
        for line in fh:
            if line.strip() == _FENCE:
                return tomllib.loads("".join(lines))
            lines.append(line)
    return None


def _text(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def dead_ends(repo_path: str | Path) -> list[tuple[str, str]]:
    """``(id, verdict or title)`` per dead-end record, newest first. Never raises."""
    try:
        where = layout(repo_path)
        if where is None or not where.records_dir.is_dir():
            return []
        rows: list[tuple[str, str, str]] = []
        for path in where.records_dir.glob("*.md"):
            try:
                meta = _header(path)
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                log.debug("findings: unreadable record %s", path, exc_info=True)
                continue
            if not meta or meta.get("status") != where.dead_status:
                continue
            record_id = _text(meta.get("id")) or path.stem
            text = _text(meta.get("verdict")) or _text(meta.get("title")) or "(no verdict)"
            rows.append((str(meta.get("date", "")), record_id, text))
    except Exception:
        log.warning("findings: dead ends failed for %s", repo_path, exc_info=True)
        return []
    rows.sort(reverse=True)
    return [(record_id, text) for _, record_id, text in rows]
