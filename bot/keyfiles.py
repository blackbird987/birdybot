"""Real keys read from the key files on this machine, for the outbound filter.

The shape rules in bot/platform/redact.py guess. This module knows: it reads
the ``.env`` style files in each registered repo's main checkout and under
``~/.config/*/``, and hands bot/platform/redact.py the values whose NAME says
they are a key. A literal match has no false positives by construction, which
is what lets the shape rules relax for public ids (tx hashes, pool ids).

Rules (docs/design/secret-intake.md, "Outbound filter"):

- Files: ``.env``, ``.env.*`` and ``*.env`` at a repo's main checkout root
  (never a ``.worktrees`` copy, never a subfolder), plus ``~/.config/*/*.env``.
  Template files (``.example``, ``.sample``, ``.template``, ``.dist``) are
  skipped: they hold placeholders, not keys.
- A value counts when its name has a key-like segment (``PK``, ``KEY``,
  ``PRIVATE``, ``SECRET``, ``PASSWORD``, ``MNEMONIC``, ``SEED``, ``TOKEN`` and
  the door's other secret segments) and does not name a public thing: a last
  segment such as ``ADDRESS``, ``ID``, ``IDS``, ``POOL``, ``HOST`` or ``PATH``
  (``is_public_name``), or a ``PUBLIC``/``PUBKEY`` segment. A name ending in
  ``URL`` is not a key either, except a ``WEBHOOK`` URL, which is one.
- At least 8 characters, and not a placeholder.

Nothing here raises, logs a value, or reads on every call: the file set is
re-checked at most every ``RECHECK_SECONDS`` and re-parsed only when a file or
directory mtime moved. A file that cannot be read keeps its last good values.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

from bot import secret_detect

log = logging.getLogger(__name__)

MIN_LEN = 8
RECHECK_SECONDS = 5.0

# Overridable by tests (patch on this module).
CONFIG_DIR: Path = Path.home() / ".config"

_KEY_SEGMENTS = frozenset({"PK", "PRIVATE", "PRIVKEY", "MNEMONIC", "SEED"}) | (
    secret_detect._SECRET_NAME_SEGMENTS - {"AUTH"}
)
# A last segment that names a public thing, not a credential. Shared with the
# outbound filter's name=value rule (bot/platform/redact.py), so the two agree.
_PUBLIC_SUFFIXES = frozenset({
    "ADDRESS", "ADDRESSES", "ADDR", "ID", "IDS", "POOL", "HOST", "PORT", "PATH", "DIR",
})
_PUBLIC_SEGMENTS = frozenset({"PUBLIC", "PUBKEY"})
_CAMEL = re.compile(r'([a-z0-9])([A-Z])')
_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".tmpl")
_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")

_lock = threading.Lock()
_roots: tuple[tuple[str, str], ...] = ()  # (repo name, main checkout path)
_checked_at = 0.0
_signature: tuple = ()
_per_file: dict[str, tuple[tuple[int, int], tuple[tuple[str, str], ...]]] = {}
_result: tuple[tuple[str, str], ...] = ()


def set_repo_roots(repos: dict) -> None:
    """The registered repos, name -> path. Called by the store when it changes."""
    global _roots, _checked_at
    roots: list[tuple[str, str]] = []
    for name, path in (repos or {}).items():
        if isinstance(name, str) and isinstance(path, str) and path:
            roots.append((name, path))
    with _lock:
        new = tuple(sorted(roots))
        if new != _roots:
            _roots = new
            _checked_at = 0.0


def segments(name: str) -> list[str]:
    """``tokenAddress``, ``TOKEN_ADDRESS`` and ``token-address`` all give
    ``["TOKEN", "ADDRESS"]``."""
    return [p for p in re.split(r"[_\-.\s]+", _CAMEL.sub(r"\1_\2", name).upper()) if p]


def is_public_name(name: str) -> bool:
    """Does this name say its value is public (an address, an id, a host)?"""
    parts = segments(name)
    if not parts:
        return False
    return parts[-1] in _PUBLIC_SUFFIXES or bool(set(parts) & _PUBLIC_SEGMENTS)


def name_is_key(name: str) -> bool:
    """Does this variable name say its value unlocks something?"""
    parts = segments(name)
    if not parts:
        return False
    upper = name.upper()
    keyish = bool(set(parts) & _KEY_SEGMENTS) or "APIKEY" in upper or "PASSWORD" in upper
    if not keyish or is_public_name(name):
        return False
    if parts[-1] == "URL" and "WEBHOOK" not in parts:
        return False
    return True


def parse(text: str) -> list[tuple[str, str]]:
    """KEY=VALUE pairs from an env file, quotes and ``export `` removed."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        name, raw = m.group(1), m.group(2)
        if raw[:1] in ("'", '"'):
            q = raw[0]
            end = raw.find(q, 1)
            value = raw[1:end] if end > 0 else raw[1:]
        else:
            value = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
        out.append((name, value))
    return out


def _keep(name: str, value: str) -> bool:
    if len(value) < MIN_LEN or not name_is_key(name):
        return False
    return not secret_detect.is_placeholder_value(value)


def _candidate_files() -> tuple[list[tuple[str, Path]], tuple]:
    """(origin prefix, file) pairs, and the directory part of the signature."""
    files: list[tuple[str, Path]] = []
    dir_sig: list[tuple[str, int]] = []

    def scan(directory: Path, label: str, match) -> None:
        try:
            dir_sig.append((str(directory), directory.stat().st_mtime_ns))
            with os.scandir(directory) as it:
                for entry in it:
                    if match(entry.name) and entry.is_file():
                        files.append((label, Path(entry.path)))
        except OSError:
            return

    def repo_match(name: str) -> bool:
        if name.endswith(_TEMPLATE_SUFFIXES):
            return False
        return name == ".env" or name.startswith(".env.") or name.endswith(".env")

    def config_match(name: str) -> bool:
        return name.endswith(".env") and not name.endswith(_TEMPLATE_SUFFIXES)

    for repo, path in _roots:
        scan(Path(path), repo, repo_match)
    cfg = CONFIG_DIR
    try:
        dir_sig.append((str(cfg), cfg.stat().st_mtime_ns))
        with os.scandir(cfg) as it:
            subdirs = sorted(e.path for e in it if e.is_dir(follow_symlinks=False))
    except OSError:
        subdirs = []
    for sub in subdirs:
        scan(Path(sub), "~/.config/" + Path(sub).name, config_match)
    return files, tuple(dir_sig)


def entries() -> tuple[tuple[str, str], ...]:
    """(origin, value) for every key found, longest value first.

    Origin is ``NAME@repo/file`` (or ``NAME@~/.config/dir/file``). Never raises.
    """
    global _checked_at, _signature, _result
    now = time.monotonic()
    if now - _checked_at < RECHECK_SECONDS:
        return _result
    with _lock:
        if now - _checked_at < RECHECK_SECONDS:
            return _result
        failed = False
        try:
            files, dir_sig = _candidate_files()
            file_sig: list[tuple[str, int, int]] = []
            for _, path in files:
                try:
                    st = path.stat()
                    file_sig.append((str(path), st.st_mtime_ns, st.st_size))
                except OSError:
                    file_sig.append((str(path), -1, -1))
            signature = (dir_sig, tuple(file_sig))
            if signature != _signature:
                _result = _rebuild(files)
                _signature = signature
        except Exception:  # pragma: no cover - defensive
            failed = True
        _checked_at = now
        result = _result
    # Logged outside the lock: the log formatter calls back into entries(), and
    # a non-reentrant lock held here would wait on itself.
    if failed:  # pragma: no cover - defensive
        log.debug("Key-file scan failed, keeping the last good list", exc_info=True)
    return result


def _rebuild(files: list[tuple[str, Path]]) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    live: set[str] = set()
    for label, path in files:
        key = str(path)
        live.add(key)
        try:
            st = path.stat()
            ident = (st.st_mtime_ns, st.st_size)
            cached = _per_file.get(key)
            if cached is not None and cached[0] == ident:
                found = cached[1]
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
                found = tuple(
                    (f"{name}@{label}/{path.name}", value)
                    for name, value in parse(text) if _keep(name, value)
                )
                _per_file[key] = (ident, found)
        except OSError:
            cached = _per_file.get(key)
            found = cached[1] if cached is not None else ()
        pairs.extend(found)
    for gone in set(_per_file) - live:
        _per_file.pop(gone, None)
    pairs.sort(key=lambda p: len(p[1]), reverse=True)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for origin, value in pairs:
        if value not in seen:
            seen.add(value)
            out.append((origin, value))
    return tuple(out)


def reset() -> None:
    """Forget everything cached. For tests."""
    global _checked_at, _signature, _result
    with _lock:
        _checked_at = 0.0
        _signature = ()
        _result = ()
        _per_file.clear()
