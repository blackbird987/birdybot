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
_SHA_MAX = 12


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

    The reader rejects a whole record for one bad field, so a value it would
    refuse stops here. ``--short`` runs past the minimum in a large repo.
    """
    if sha is not None:
        sha = sha[:_SHA_MAX]
        if not SHA_RE.fullmatch(sha):
            sha = None
    if ahead is not None:
        ahead = min(ahead, MAX_COUNT)
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


OFFLINE_AFTER_SECS = 15 * 60


# --- Stage 2: questions and answers, carried in each machine's outbox thread ---
#
# Same rules as the directory: what comes in was written by the other machine,
# one bad field rejects the message and nothing here repairs one. Three outcomes
# are kept apart, because the reader does different things with them:
# NotARecord is the bot's own chatter (a card) and is skipped quietly,
# UnknownProtocol is a newer peer and holds the cursor, InvalidRecord is logged
# and skipped.

LINK_PROTOCOL = 1
QUESTION_MAX_CHARS = 1400
ANSWER_MAX_CHARS = 8000
ANSWER_MAX_PARTS = 16
# Well under Discord's limit, measured after JSON escaping.
LINK_MESSAGE_LIMIT = 1900

ANSWERED, DECLINED, UNKNOWN_REPO, FAILED = "answered", "declined", "unknown_repo", "failed"
ANSWER_STATUSES = frozenset({ANSWERED, DECLINED, UNKNOWN_REPO, FAILED})

ID_RE = re.compile(r"[0-9a-f]{16}")
_Q_KEYS = {"p", "v", "id", "from", "to", "repo", "text"}
_A_KEYS = {"p", "v", "id", "n", "m", "status", "text"}
_TRUNCATED = "\n[answer cut: too long for the link]"


class NotARecord(ValueError):
    """Not one of ours: not JSON, or JSON that does not claim to be a record."""


class UnknownProtocol(ValueError):
    """A record from a newer protocol. It may be fine: this machine cannot tell."""


def _dumps(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def encode_question(qid: str, sender: str, to: str, repo: str, text: str) -> str:
    """A question's message text. Raises ValueError if it cannot go out whole."""
    if len(text) > QUESTION_MAX_CHARS:
        raise ValueError(f"longer than {QUESTION_MAX_CHARS} characters")
    message = _dumps({"p": "q", "v": LINK_PROTOCOL, "id": qid, "from": sender,
                      "to": to, "repo": repo, "text": text})
    if len(message) > LINK_MESSAGE_LIMIT:
        raise ValueError("too long once encoded (non-ASCII text takes more room)")
    return message


def _answer_message(qid: str, n: int, m: int, status: str, text: str) -> str:
    return _dumps({"p": "a", "v": LINK_PROTOCOL, "id": qid, "n": n, "m": m,
                   "status": status, "text": text})


def _escaped_len(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=True)) - 2


def encode_answer(qid: str, status: str, text: str) -> list[str]:
    """An answer as message texts, each under the limit after escaping.

    Splits on escaped length, so a page of non-ASCII text cannot push a part past
    Discord's limit. Too long for the part cap gets a visible cut, not a silent one.
    """
    if len(text) > ANSWER_MAX_CHARS:
        text = text[:ANSWER_MAX_CHARS - len(_TRUNCATED)] + _TRUNCATED
    room = LINK_MESSAGE_LIMIT - len(_answer_message(qid, ANSWER_MAX_PARTS, ANSWER_MAX_PARTS, status, ""))
    chunks: list[str] = []
    current: list[str] = []
    used = 0
    for char in text:
        size = _escaped_len(char)
        if used + size > room:
            chunks.append("".join(current))
            current, used = [], 0
        current.append(char)
        used += size
    chunks.append("".join(current))
    if len(chunks) > ANSWER_MAX_PARTS:
        chunks = chunks[:ANSWER_MAX_PARTS]
        chunks[-1] = chunks[-1][:max(0, room - _escaped_len(_TRUNCATED))] + _TRUNCATED
    return [_answer_message(qid, i, len(chunks), status, chunk)
            for i, chunk in enumerate(chunks, 1)]


def parse_link(text) -> dict:
    """A message from the other machine's outbox.

    Returns the record, or raises NotARecord, UnknownProtocol or InvalidRecord.
    """
    if not isinstance(text, str) or not text.startswith("{"):
        raise NotARecord("not a record")
    if len(text) > MAX_MESSAGE_CHARS:
        raise InvalidRecord("longer than a Discord message can be")
    try:
        record = json.loads(text, object_pairs_hook=_no_duplicates,
                            parse_constant=_no_constants)
    except InvalidRecord:
        raise
    except (ValueError, RecursionError):
        raise NotARecord("not JSON") from None
    if not isinstance(record, dict) or "p" not in record:
        raise NotARecord("does not claim to be a record")
    version = record.get("v")
    if type(version) is not int:
        raise InvalidRecord("bad protocol version")
    if version != LINK_PROTOCOL:
        raise UnknownProtocol(f"protocol {version}")
    kind = record["p"]
    if kind == "q":
        _check_question(record)
    elif kind == "a":
        _check_answer(record)
    else:
        # A record type this version has never heard of, under a version it knows.
        raise InvalidRecord("unknown record type")
    return record


def _check_id_and_text(record: dict, limit: int) -> None:
    if not isinstance(record["id"], str) or not ID_RE.fullmatch(record["id"]):
        raise InvalidRecord("bad id")
    text = record["text"]
    if not isinstance(text, str) or len(text) > limit:
        raise InvalidRecord("bad text")


def _check_question(record: dict) -> None:
    if set(record) != _Q_KEYS:
        raise InvalidRecord("wrong question fields")
    _check_id_and_text(record, QUESTION_MAX_CHARS)
    if not record["text"].strip():
        raise InvalidRecord("empty question")
    for field in ("from", "to", "repo"):
        value = record[field]
        if not isinstance(value, str) or not NAME_RE.fullmatch(value):
            raise InvalidRecord(f"bad {field}")


def _check_answer(record: dict) -> None:
    if set(record) != _A_KEYS:
        raise InvalidRecord("wrong answer fields")
    _check_id_and_text(record, MAX_MESSAGE_CHARS)
    if not _is_int(record["m"], 1, ANSWER_MAX_PARTS) or not _is_int(record["n"], 1, record["m"]):
        raise InvalidRecord("bad part numbers")
    if record["status"] not in ANSWER_STATUSES:
        raise InvalidRecord("bad status")
