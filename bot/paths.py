"""Machine-portable absolute paths for a config that is shared across machines.

The problem this solves is specific and was expensive: on a dual-boot machine
whose Windows drive is mounted from Linux, ``.env`` and ``data/state.json``
live on that shared drive and are read by *both* operating systems.  Every path
in them is absolute, so the same folder is::

    C:/Users/Quincy/Desktop/Programming/X          (booted into Windows)
    /run/media/quincy/SYSTEM/Users/Quincy/...      (booted into Linux)

One config file cannot hold both spellings, and the previous answer was
``scripts/migrate_to_linux.py`` — a one-shot, one-directional rewrite of every
stored path.  On a dual boot that has to be run in both directions on every
reboot, and it still leaves conversation history stranded, because the CLI
files history under a directory named after the path it was launched from.

So instead: keep storing whatever spelling the local machine uses, and
translate on the way *in*.  Two doors take every path — the ``.env`` load in
``bot.config`` and the state-file load in ``bot.store.state`` — so translating
there leaves all ~300 downstream readers untouched.

Groups are discovered, not configured.  A marker file inside the user root
(``.bot-root-id``, holding a UUID) travels with the volume, so every machine
that mounts it reads the *same* id and appends its own spelling to the *same*
group.  Boot Windows once and Linux once and the map assembles itself; nothing
has to be typed, and a wrong guess is impossible because the id comes off the
disk rather than from a heuristic about what a path looks like.

Translation is read-only and prefix-based.  If the map is empty or a path is
under no known root, it passes through untouched — the failure mode is exactly
today's behaviour, never a corrupted path.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

# Written inside the user root (e.g. C:/Users/Quincy). Identifies the physical
# location across every name it can be reached by. Hidden, tiny, and safe to
# delete — a fresh one is minted on the next boot (the group then re-forms from
# whichever spellings are still listed).
MARKER_NAME = ".bot-root-id"

ROOTS_FILENAME = "roots.json"

# {group_id: [spelling, ...]} — alternate absolute names for one directory.
_groups: dict[str, list[str]] = {}
# {group_id: spelling} — the name that works on THIS machine.
_local: dict[str, str] = {}
# Longest-first (spelling, group_id) so nested roots resolve unambiguously.
_index: list[tuple[str, str]] = []
_roots_file: Path | None = None
_initialised = False


def _norm(p: str | os.PathLike) -> str:
    """Forward slashes, no trailing separator. Comparison form, not output."""
    return str(p).replace("\\", "/").rstrip("/")


def _cmp(p: str) -> str:
    """Case-folded comparison key.

    Roots are user-profile directories on volumes that are case-insensitive in
    practice (NTFS, and NTFS-via-fuse from Linux). Folding avoids a spelling
    mismatch on drive letter or profile-name case defeating the whole map.
    """
    return _norm(p).lower()


def _is_windows_style(p: str) -> bool:
    n = _norm(p)
    return len(n) >= 2 and n[1] == ":" and n[0].isalpha()


def _render(root: str, rest: str) -> str:
    """Join a local root with a remainder, in that root's separator style."""
    joined = _norm(root) + rest
    return joined.replace("/", "\\") if _is_windows_style(root) else joined


def _rebuild_index() -> None:
    global _index
    pairs: list[tuple[str, str]] = []
    for gid, members in _groups.items():
        if gid not in _local:
            # No spelling of this group exists here — translating into it would
            # produce a path this machine cannot open.
            continue
        for m in members:
            pairs.append((_cmp(m), gid))
    pairs.sort(key=lambda t: len(t[0]), reverse=True)
    _index = pairs


def _read_marker(root: Path) -> str | None:
    try:
        text = (root / MARKER_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _write_marker(root: Path, gid: str) -> bool:
    try:
        (root / MARKER_NAME).write_text(gid + "\n", encoding="utf-8")
        return True
    except OSError:
        # A read-only mount is a legitimate outcome, not a crash: the map still
        # works for any group already recorded, it just can't learn this root.
        log.warning("Could not write root marker in %s — path map not extended", root)
        return False


def detect_local_root(account_hints: list[str], home: Path | None = None) -> Path | None:
    """The user root as reachable from *this* machine.

    Derived from the first configured account directory that actually exists —
    on Linux that is the mounted Windows profile, which is the root that
    matters.  ``Path.home()`` is the fallback and the normal answer on Windows,
    where the shared ``.env`` names Linux paths that resolve to nothing.
    """
    for hint in account_hints:
        if not hint:
            continue
        try:
            p = Path(hint).expanduser()
        except (OSError, RuntimeError):
            continue
        if p.is_dir():
            return p.parent
    try:
        h = (home or Path.home()).expanduser()
    except (OSError, RuntimeError):
        return None
    return h if h.is_dir() else None


def _load_file(path: Path) -> dict[str, list[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        log.exception("Unreadable %s — continuing with no path map", path)
        return {}
    groups = raw.get("groups")
    if not isinstance(groups, dict):
        return {}
    out: dict[str, list[str]] = {}
    for gid, members in groups.items():
        if isinstance(members, list):
            out[str(gid)] = [_norm(m) for m in members if isinstance(m, str) and m]
    return out


def _save_file(path: Path) -> None:
    payload = {
        "_comment": [
            "Alternate absolute names for the same directory, one group per",
            "physical location. Written automatically: each machine adds its",
            "own spelling on boot, matched by the .bot-root-id marker file",
            "inside the directory itself. Hand-edit to add a spelling that",
            "machine has not booted from yet (e.g. a live-USB mount point).",
        ],
        "version": 1,
        "groups": _groups,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.exception("Could not save %s — path map will re-derive next boot", path)


def init(
    data_dir: Path,
    account_hints: list[str] | None = None,
    home: Path | None = None,
) -> None:
    """Load the root map and record this machine's spelling in it.

    Safe to call repeatedly; the last call wins.  Never raises — a failure
    anywhere here leaves an empty map, and an empty map means every path passes
    through unchanged.
    """
    global _groups, _local, _roots_file, _initialised

    if os.getenv("BOT_PATHS_DISABLED") == "1":
        # Harnesses import bot.config for unrelated reasons and would otherwise
        # seed a group (and drop a marker) from whatever home they happen to
        # run under. An empty map is pass-through, so this disables translation
        # rather than faking it.
        _groups, _local, _roots_file = {}, {}, None
        _rebuild_index()
        _initialised = True
        return

    _roots_file = Path(data_dir) / ROOTS_FILENAME
    _groups = _load_file(_roots_file)
    _local = {}

    local_root = detect_local_root(account_hints or [], home)
    if local_root is not None:
        gid = _read_marker(local_root)
        if gid is None:
            # Adopt the id of a group this root is already listed in, so a
            # deleted marker rejoins its group instead of splitting off a
            # duplicate that translates nothing.
            here = _cmp(local_root)
            gid = next(
                (g for g, ms in _groups.items() if any(_cmp(m) == here for m in ms)),
                None,
            ) or uuid.uuid4().hex
            _write_marker(local_root, gid)
        members = _groups.setdefault(gid, [])
        if not any(_cmp(m) == _cmp(local_root) for m in members):
            members.append(_norm(local_root))
            log.info(
                "Path map: learned local root %s (group %s, %d spelling(s))",
                local_root, gid[:8], len(members),
            )
            _save_file(_roots_file)
        # Ground truth beats existence-probing: this is the root we actually
        # resolved, so pin it even if a stale sibling spelling also mounts.
        _local[gid] = _norm(local_root)

    for gid, members in _groups.items():
        if gid in _local:
            continue
        for m in members:
            try:
                if Path(m).is_dir():
                    _local[gid] = m
                    break
            except (OSError, ValueError):
                continue

    _rebuild_index()
    _initialised = True


def _match(path: str) -> tuple[str, str, str] | None:
    """Return (group_id, matched_spelling, remainder) for a path under a root."""
    n = _cmp(path)
    for member, gid in _index:
        if n == member:
            return gid, member, ""
        if n.startswith(member + "/"):
            return gid, member, _norm(path)[len(member):]
    return None


def translate(path: str | os.PathLike | None) -> str | None:
    """Rewrite a stored absolute path into this machine's spelling.

    Passes anything it does not recognise straight through, so a missing or
    partial map degrades to today's behaviour rather than to a broken path.
    """
    if not path:
        return path if path is None else str(path)
    text = str(path)
    hit = _match(text)
    if hit is None:
        return text
    gid, _member, rest = hit
    return _render(_local[gid], rest)


def aliases(path: str | os.PathLike | None) -> list[str]:
    """Every known spelling of ``path``, local spelling first.

    Used to find CLI conversation history, which is filed under a directory
    named after the path it was created from — so the same conversation lands
    in a different place depending on which machine wrote it.
    """
    if not path:
        return []
    text = str(path)
    hit = _match(text)
    if hit is None:
        return [text]
    gid, _member, rest = hit
    local = _render(_local[gid], rest)
    out = [local]
    for m in _groups.get(gid, []):
        candidate = _render(m, rest)
        if candidate not in out:
            out.append(candidate)
    return out


def is_portable(path: str | os.PathLike | None) -> bool:
    """True when ``path`` lives under a root this map knows.

    A registered repo that is *not* portable exists on one machine only — the
    case that stranded The Citadel on a Linux-only partition while its history
    sat on the shared drive.
    """
    return bool(path) and _match(str(path)) is not None


def describe() -> str:
    """One-line summary for the boot log and /status."""
    if not _initialised:
        return "path map: not initialised"
    if not _groups:
        return "path map: no roots known"
    parts = []
    for gid, members in _groups.items():
        here = _local.get(gid)
        parts.append(f"{gid[:8]}={len(members)} spelling(s)" + ("" if here else " [not local]"))
    return "path map: " + ", ".join(parts)


def snapshot() -> dict:
    """Full state, for diagnostics (``scripts/verify_paths.py``, /status)."""
    return {
        "initialised": _initialised,
        "roots_file": str(_roots_file) if _roots_file else None,
        "groups": {g: list(ms) for g, ms in _groups.items()},
        "local": dict(_local),
    }


def reset_for_test(groups: dict[str, list[str]], local: dict[str, str]) -> None:
    """Install a map directly, bypassing disk. Test harnesses only."""
    global _groups, _local, _initialised
    _groups = {g: [_norm(m) for m in ms] for g, ms in groups.items()}
    _local = {g: _norm(m) for g, m in local.items()}
    _initialised = True
    _rebuild_index()
