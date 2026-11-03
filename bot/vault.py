"""The secret vault: one JSON file per machine, outside the repo and data/.

Names and values pasted through the door live here, along with the few door
ids that must survive a restart (hold markers, /secret request tokens, which
transcripts were already flagged). Raw held text is never written here.

Not a containment boundary. Sessions share the bot's uid and can read this file
like any .env; see ``docs/design/secret-intake.md``.

A corrupt file is reported and never overwritten: every write reads first, and
a read that fails raises ``VaultUnavailable`` so the door stores nothing and
tells the owner, instead of replacing their keys with an empty file.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from bot import config
from bot.secret_detect import REMOVED

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")

# Below this a value is left out of literal stripping: blanket-replacing a short
# string would gut unrelated text. Same floor as redact's env secrets.
LITERAL_MIN_LEN = 8
# Last characters are only quoted back when they give away little of the value.
TAIL_MIN_LEN = 12
# Door state nobody may ever answer. Oldest goes first.
_REQUESTS_MAX = 64
_FLAGGED_SESSIONS_MAX = 512

_DENIED_EXACT = frozenset({
    "PATH", "HOME", "USER", "SHELL", "TMPDIR", "BASH_ENV", "ENV", "IFS",
    "PROMPT_COMMAND", "GITHUB_TOKEN", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
})
_DENIED_GLOBS = (
    "LD_*", "DYLD_*", "PYTHON*", "NODE_*", "GIT_*", "GH_*", "SSH_*", "CLAUDE_*",
    "CLAUDECODE*", "ANTHROPIC_*", "CURSOR_*", "XDG_*",
)

# Guards read-modify-write. Plain reads do not need it, because a write replaces
# the file whole, and literal_values must not take it: it runs inside the log
# formatter, so a line logged under this lock would wait on itself.
_lock = threading.Lock()
_literal_cache: tuple[tuple[str, int, int], tuple[str, ...]] | None = None
_denied_cache: frozenset[str] | None = None


class VaultUnavailable(Exception):
    """The vault file exists but cannot be read as a vault. Nothing was changed."""


@dataclass(frozen=True)
class AddResult:
    name: str
    # "new", "existing" (same value again) or "conflict" (name taken, stored as NAME_2).
    status: str
    conflict_with: str | None
    tail: str


def _path() -> Path:
    return Path(config.VAULT_DIR) / "vault.json"


def _empty() -> dict:
    return {"entries": [], "requests": {}, "hold_markers": [], "flagged": {}}


def _check_shape(data: object) -> None:
    """A hand edit can leave valid JSON that the code below cannot walk."""
    if not isinstance(data, dict):
        raise ValueError("not a vault")
    entries = data.get("entries", [])
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("name"), str) for e in entries
    ):
        raise ValueError("bad entries")
    if not isinstance(data.get("requests", {}), dict) or not isinstance(data.get("flagged", {}), dict):
        raise ValueError("bad door state")
    if not isinstance(data.get("hold_markers", []), list):
        raise ValueError("bad door state")


def _load() -> dict:
    """Read the file without logging, for the one caller inside the log formatter."""
    path = _path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty()
    except OSError as exc:
        raise VaultUnavailable(f"could not be read: {type(exc).__name__}") from exc
    try:
        data = json.loads(raw)
        _check_shape(data)
    except ValueError as exc:
        # The parse error can quote the file, so its text stays out of the message.
        raise VaultUnavailable("is corrupt") from exc
    base = _empty()
    base.update(data)
    return base


def _read_unlocked() -> dict:
    try:
        return _load()
    except VaultUnavailable as exc:
        log.error("Vault file at %s %s; leaving it untouched", _path(), exc)
        raise


def _write_unlocked(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    # mkstemp creates the file 0600, so the value is never world-readable.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="vault.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _changed() -> None:
    global _literal_cache
    _literal_cache = None


def is_available() -> bool:
    """False when a vault file exists but is unreadable, so nothing can be stored."""
    with _lock:
        try:
            _read_unlocked()
        except VaultUnavailable:
            return False
    return True


# --- Names ---------------------------------------------------------------


def _denied_exact() -> frozenset[str]:
    """Everything a session's environment depends on, including what the bot strips."""
    global _denied_cache
    if _denied_cache is None:
        from bot.claude import provider as provider_mod
        from bot.claude.runner.common import SESSION_STRIPPED_ENV_VARS

        names = set(_DENIED_EXACT) | set(SESSION_STRIPPED_ENV_VARS)
        for p in provider_mod.PROVIDERS.values():
            if p is None:
                continue
            names.add(p.config_dir_env)
            names.update(p.nested_env_vars)
        _denied_cache = frozenset(names)
    return _denied_cache


def is_denied(name: str) -> bool:
    """Could setting this variable change how a session runs, rather than give it a key?"""
    if name in _denied_exact():
        return True
    return any(fnmatch.fnmatchcase(name, g) for g in _DENIED_GLOBS)


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def safe_name(name: str) -> str:
    """A name that passes validation and the denylist: PASTED_<NAME> if it was denied."""
    cleaned = re.sub(r"[^A-Z0-9_]", "_", (name or "").upper()).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"SECRET_{cleaned}".rstrip("_") if cleaned else "SECRET"
    cleaned = cleaned[:64]
    if len(cleaned) < 3:
        cleaned = f"SECRET_{cleaned}"
    if is_denied(cleaned):
        cleaned = f"PASTED_{cleaned}"[:64]
    return cleaned


def tail_of(value: str) -> str:
    return value[-4:] if len(value) >= TAIL_MIN_LEN else ""


def _entry_tail(entry: dict) -> str:
    return entry.get("last4") or ""


def _find(data: dict, name: str) -> dict | None:
    for entry in data["entries"]:
        if entry.get("name") == name:
            return entry
    return None


# --- Entries -------------------------------------------------------------


def add(
    value: str, name: str, repo: str | None = None, source_thread: str | None = None,
) -> AddResult:
    """Store ``value``. Raises ``VaultUnavailable`` when the file is corrupt.

    The same value again keeps its name and only adds the repo grant, so a
    re-paste is harmless. The same name with a different value is stored as
    NAME_2 at once: nothing raw is held in memory waiting for an answer.
    """
    base = safe_name(name)
    with _lock:
        data = _read_unlocked()
        for entry in data["entries"]:
            if entry.get("value") == value:
                repos = entry.setdefault("repos", [])
                if repo and repo not in repos:
                    repos.append(repo)
                    _write_unlocked(data)
                result = AddResult(entry["name"], "existing", None, _entry_tail(entry))
                break
        else:
            final = base
            conflict = None
            if _find(data, final):
                conflict = final
                n = 2
                while True:
                    suffix = f"_{n}"
                    final = base[:64 - len(suffix)] + suffix
                    if not _find(data, final):
                        break
                    n += 1
            data["entries"].append({
                "name": final,
                "value": value,
                "repos": [repo] if repo else [],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_thread": source_thread,
                "last4": tail_of(value),
            })
            _write_unlocked(data)
            result = AddResult(final, "conflict" if conflict else "new", conflict, tail_of(value))
    _changed()
    return result


def forget(name: str) -> bool:
    with _lock:
        data = _read_unlocked()
        entry = _find(data, name)
        if not entry:
            return False
        data["entries"].remove(entry)
        _write_unlocked(data)
    _changed()
    return True


def rename(old: str, new: str) -> str:
    """Rename an entry and return the stored name. ``ValueError`` says why not."""
    if not valid_name(new):
        raise ValueError("Names use capitals, digits and underscores, 3 to 64 characters.")
    final = safe_name(new)
    with _lock:
        data = _read_unlocked()
        entry = _find(data, old)
        if not entry:
            raise ValueError(f"No secret called {old}.")
        if final != old and _find(data, final):
            raise ValueError(f"{final} already exists.")
        entry["name"] = final
        _write_unlocked(data)
    _changed()
    return final


def replace(name: str, newer: str) -> bool:
    """Give ``name`` the value of ``newer`` and drop ``newer``. Grants are merged."""
    with _lock:
        data = _read_unlocked()
        kept = _find(data, name)
        incoming = _find(data, newer)
        if not kept or not incoming or kept is incoming:
            return False
        kept["value"] = incoming["value"]
        kept["last4"] = incoming.get("last4", "")
        kept["created_at"] = incoming.get("created_at", kept.get("created_at"))
        kept["repos"] = sorted({*kept.get("repos", []), *incoming.get("repos", [])})
        data["entries"].remove(incoming)
        _write_unlocked(data)
    _changed()
    return True


def grant(name: str, repo: str) -> bool:
    with _lock:
        data = _read_unlocked()
        entry = _find(data, name)
        if not entry:
            return False
        repos = entry.setdefault("repos", [])
        if repo not in repos:
            repos.append(repo)
            _write_unlocked(data)
    return True


def _safe_read() -> dict:
    with _lock:
        try:
            return _read_unlocked()
        except VaultUnavailable:
            return _empty()


def has_name(name: str) -> bool:
    return _find(_safe_read(), name) is not None


def has_entries() -> bool:
    return bool(_safe_read()["entries"])


def list_entries() -> list[dict]:
    """Every entry without its value, for /secret list."""
    return [
        {"name": e.get("name"), "repos": list(e.get("repos", [])), "tail": _entry_tail(e)}
        for e in _safe_read()["entries"]
    ]


def env_for_repo(repo: str) -> dict[str, str]:
    """The variables for a session in ``repo``.

    The denylist is checked again here, because the file can be edited by hand
    and a name that overrode PATH or the session's config dir would change how
    the session itself runs.
    """
    env: dict[str, str] = {}
    for entry in _safe_read()["entries"]:
        name, value = entry.get("name"), entry.get("value")
        if repo not in entry.get("repos", []):
            continue
        if not valid_name(name or "") or not isinstance(value, str) or is_denied(name):
            log.warning("Vault entry %r skipped at injection: bad or denied name", name)
            continue
        env[name] = value
    return env


def names_for_repo(repo: str) -> list[str]:
    return sorted(
        e["name"] for e in _safe_read()["entries"]
        if repo in e.get("repos", []) and valid_name(e.get("name", "")) and not is_denied(e["name"])
    )


def names_elsewhere(repo: str) -> list[str]:
    return sorted(e["name"] for e in _safe_read()["entries"] if repo not in e.get("repos", []))


# --- Literal values ------------------------------------------------------


def literal_values() -> tuple[str, ...]:
    """Every stored value long enough to strip, longest first.

    Cached on the file's identity, so a hand edit takes effect. This sits under
    every log line and every message the bot renders, so it takes no lock and
    logs nothing (see ``_lock``). A file that stops parsing keeps the last good
    list: losing it would put known keys back into what the bot prints.
    """
    global _literal_cache
    path = _path()
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return ()
    cached = _literal_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        entries = _load()["entries"]
    except VaultUnavailable:
        result = cached[1] if cached is not None else ()
    else:
        values = [
            e["value"] for e in entries
            if isinstance(e.get("value"), str) and len(e["value"]) >= LITERAL_MIN_LEN
        ]
        values.sort(key=len, reverse=True)
        result = tuple(dict.fromkeys(values))
    _literal_cache = (key, result)
    return result


def strip_literals(text: str) -> str:
    """``text`` with every stored value removed."""
    if not text:
        return text
    for value in literal_values():
        if value in text:
            text = text.replace(value, REMOVED)
    return text


# --- Door state: requests, hold markers, flagged names -------------------


def add_request(name: str, repo: str, channel_id: str, session_id: str | None) -> str | None:
    """Record a /secret request. None when the same one is already pending."""
    with _lock:
        data = _read_unlocked()
        for req in data["requests"].values():
            if (req.get("name"), req.get("repo"), req.get("session_id")) == (name, repo, session_id):
                return None
        token = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
        data["requests"][token] = {
            "name": name, "repo": repo, "channel_id": channel_id, "session_id": session_id,
        }
        while len(data["requests"]) > _REQUESTS_MAX:
            data["requests"].pop(next(iter(data["requests"])))
        _write_unlocked(data)
    return token


def pop_request(token: str) -> dict | None:
    with _lock:
        data = _read_unlocked()
        req = data["requests"].pop(token, None)
        if req is not None:
            _write_unlocked(data)
    return req


def add_hold_marker(channel_id: str, hold_msg_id: str) -> None:
    """Note that a held message exists. Ids only: the text stays in memory."""
    with _lock:
        data = _read_unlocked()
        data["hold_markers"].append({"channel_id": channel_id, "hold_msg_id": hold_msg_id})
        _write_unlocked(data)


def remove_hold_marker(hold_msg_id: str) -> None:
    with _lock:
        try:
            data = _read_unlocked()
        except VaultUnavailable:
            return
        kept = [m for m in data["hold_markers"] if m.get("hold_msg_id") != hold_msg_id]
        if len(kept) != len(data["hold_markers"]):
            data["hold_markers"] = kept
            _write_unlocked(data)


def take_hold_markers() -> list[dict]:
    """Markers a previous process left behind, cleared as they are read."""
    with _lock:
        try:
            data = _read_unlocked()
        except VaultUnavailable:
            return []
        markers = data["hold_markers"]
        if markers:
            data["hold_markers"] = []
            _write_unlocked(data)
    return markers


def flag_once(session_id: str, name: str) -> bool:
    """True the first time ``name`` is flagged for ``session_id``."""
    with _lock:
        try:
            data = _read_unlocked()
        except VaultUnavailable:
            return False
        names = data["flagged"].setdefault(session_id, [])
        if name in names:
            return False
        names.append(name)
        while len(data["flagged"]) > _FLAGGED_SESSIONS_MAX:
            data["flagged"].pop(next(iter(data["flagged"])))
        _write_unlocked(data)
    return True


def literal_entries() -> list[tuple[str, str]]:
    """(name, value) for each value long enough to find in a transcript."""
    return [
        (e["name"], e["value"]) for e in _safe_read()["entries"]
        if isinstance(e.get("value"), str) and len(e["value"]) >= LITERAL_MIN_LEN
    ]
