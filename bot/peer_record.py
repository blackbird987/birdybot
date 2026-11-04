"""The record two machines publish about their projects, and its strict reader.

Shared by the publisher (``bot/discord/peer_link.py``) and the reader
(``scripts/peer.py``). Standard library only, and no ``bot.config``, so the
reader can run from any repo. The other machine wrote what comes in here: it
is data, a record that breaks any rule is rejected whole, and nothing here
repairs one. See docs/design/peer-link.md.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

PROTOCOL = 1
# Discord's message limit. The publisher stops a little short of it.
MAX_MESSAGE_CHARS = 2000
PUBLISH_LIMIT = 1900
MAX_REPOS = 200
MAX_COUNT = 1_000_000

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+-]{0,63}")
VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,31}")
SHA_RE = re.compile(r"[0-9a-f]{7,12}")
ALIVE_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

_KEYS = {"v", "machine", "version", "alive", "running", "repos", "omitted"}
_BRANCH_MAX = 40


class InvalidRecord(ValueError):
    """The record broke a rule. Nothing in it should be believed."""


def repo_entry(
    name: str, hidden: bool, sha: str | None, branch: str | None,
    ahead: int | None, dirty: bool | None,
) -> list:
    """One repo as a compact array: name, hidden, sha, branch, ahead, dirty.

    An array, not an object: a desktop with thirty repos has to fit in one
    Discord message. A repo that could not be read has ``None`` for the last
    four, which says so without failing the others.
    """
    if branch is not None:
        if len(branch) > _BRANCH_MAX:
            branch = branch[:_BRANCH_MAX - 3] + "..."
        if not BRANCH_RE.fullmatch(branch):
            branch = None
    return [name, int(hidden), sha, branch, ahead,
            None if dirty is None else int(dirty)]


def alive_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def encode(machine: str, version: str, running: int, repos: list[list],
           alive: str | None = None, limit: int = PUBLISH_LIMIT) -> str:
    """The message text. Repos that do not fit are dropped and counted."""
    # A repo name the reader would refuse must not cost the whole record.
    kept = [r for r in repos if NAME_RE.fullmatch(r[0])]
    omitted = len(repos) - len(kept)
    if not VERSION_RE.fullmatch(version):
        version = "unknown"

    def render() -> str:
        return json.dumps({
            "v": PROTOCOL, "machine": machine, "version": version,
            "alive": alive or alive_stamp(), "running": running,
            "repos": kept, "omitted": omitted,
        }, separators=(",", ":"), ensure_ascii=True)

    text = render()
    while len(text) > limit and kept:
        kept.pop()
        omitted += 1
        text = render()
    return text


def _is_int(value, low: int = 0, high: int = MAX_COUNT) -> bool:
    return type(value) is int and low <= value <= high


def _no_duplicates(pairs: list) -> dict:
    out = dict(pairs)
    if len(out) != len(pairs):
        raise InvalidRecord("a key appears twice")
    return out


def _no_constants(name: str):
    raise InvalidRecord(f"{name} is not allowed")


def _check_repo(entry) -> None:
    if not isinstance(entry, list) or len(entry) != 6:
        raise InvalidRecord("a repo entry is not a six-item list")
    name, hidden, sha, branch, ahead, dirty = entry
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise InvalidRecord("a repo name breaks the name rule")
    if hidden not in (0, 1) or type(hidden) is not int:
        raise InvalidRecord(f"repo {name}: hidden is not 0 or 1")
    if sha is not None and not (isinstance(sha, str) and SHA_RE.fullmatch(sha)):
        raise InvalidRecord(f"repo {name}: bad sha")
    if branch is not None and not (isinstance(branch, str) and BRANCH_RE.fullmatch(branch)):
        raise InvalidRecord(f"repo {name}: bad branch")
    if ahead is not None and not _is_int(ahead):
        raise InvalidRecord(f"repo {name}: bad ahead count")
    if dirty is not None and (dirty not in (0, 1) or type(dirty) is not int):
        raise InvalidRecord(f"repo {name}: bad uncommitted flag")


def parse(text) -> dict:
    """Validate a message's text. Returns the record, or raises InvalidRecord."""
    if not isinstance(text, str) or not text:
        raise InvalidRecord("empty message")
    if len(text) > MAX_MESSAGE_CHARS:
        raise InvalidRecord("longer than a Discord message can be")
    try:
        record = json.loads(text, object_pairs_hook=_no_duplicates,
                            parse_constant=_no_constants)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, InvalidRecord):
            raise
        raise InvalidRecord("not JSON") from None
    if not isinstance(record, dict) or set(record) != _KEYS:
        raise InvalidRecord("wrong fields")
    if record["v"] != PROTOCOL or type(record["v"]) is not int:
        raise InvalidRecord("unknown protocol version")
    machine, version, alive = record["machine"], record["version"], record["alive"]
    if not isinstance(machine, str) or not NAME_RE.fullmatch(machine):
        raise InvalidRecord("bad machine name")
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise InvalidRecord("bad version")
    if not isinstance(alive, str) or not ALIVE_RE.fullmatch(alive):
        raise InvalidRecord("bad alive time")
    try:
        datetime.strptime(alive, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise InvalidRecord("bad alive time") from None
    if not _is_int(record["running"]) or not _is_int(record["omitted"]):
        raise InvalidRecord("bad count")
    repos = record["repos"]
    if not isinstance(repos, list) or len(repos) > MAX_REPOS:
        raise InvalidRecord("bad repo list")
    for entry in repos:
        _check_repo(entry)
    return record
