"""Append-only session history log backed by a JSONL file.

Each completed/failed session appends one JSON line to data/history.jsonl.
Provides load_recent() for reading entries back (newest first).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from bot import config

log = logging.getLogger(__name__)

HISTORY_FILE: Path = config.DATA_DIR / "history.jsonl"


def append_entry(entry: dict) -> None:
    """Append a single history entry. Best-effort — never raises."""
    try:
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        log.warning("Failed to write history entry", exc_info=True)


def clear_branch(branch_name: str) -> int:
    """Null the `branch` field on all history entries matching branch_name.

    Called after a branch is merged or discarded so stale refs don't leak
    into future sessions' system prompts. Best-effort — never raises.
    Returns the number of entries updated.
    """
    if not branch_name or not HISTORY_FILE.exists():
        return 0
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        log.warning("Failed to read history for clear_branch", exc_info=True)
        return 0

    updated: list[str] = []
    count = 0
    for line in lines:
        if not line.strip():
            updated.append(line)
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            updated.append(line)
            continue
        if entry.get("branch") == branch_name:
            entry["branch"] = None
            count += 1
            updated.append(json.dumps(entry, ensure_ascii=False, default=str))
        else:
            updated.append(line)

    if count == 0:
        return 0
    try:
        HISTORY_FILE.write_text("\n".join(updated) + "\n", encoding="utf-8")
    except Exception:
        log.warning("Failed to write history for clear_branch", exc_info=True)
        return 0
    return count


def get_branch_for_instance(instance_id: str) -> str | None:
    """Return the recorded branch for a history entry by instance id, or None.

    Used in "already resolved" early-return paths where the live instance has
    `branch = None` but the history file may still record the original branch
    name. Scans newest-first and returns the first match.
    """
    if not instance_id or not HISTORY_FILE.exists():
        return None
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("id") == instance_id:
            return entry.get("branch")
    return None


def load_recent(
    repo: str | None = None,
    limit: int = 50,
    dedupe_thread: bool = False,
) -> list[dict]:
    """Load recent history entries, newest first.

    Args:
        repo: Filter by repo name (None = all repos).
        limit: Maximum entries to return.
        dedupe_thread: If True, keep only the latest entry per thread_id.
            Useful for display — collapses autopilot chains into one entry.
    """
    if not HISTORY_FILE.exists():
        return []
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        log.warning("Failed to read history file", exc_info=True)
        return []

    seen_threads: set[str] = set()
    entries: list[dict] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if repo and entry.get("repo") != repo:
            continue
        if dedupe_thread:
            tid = entry.get("thread_id", "")
            if tid and tid in seen_threads:
                continue
            if tid:
                seen_threads.add(tid)
        entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


# --- Relevance ranking -------------------------------------------------
#
# Injecting the N most recent sessions into every prompt spends context on
# whatever happened to run last, which is usually unrelated to the task at
# hand. Ranking by overlap with the current prompt keeps the slots for
# history that might actually inform this session.

# Deliberately small: these are the words that appear in nearly every prompt
# and topic, so scoring on them would rank noise as highly as signal.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "so", "to",
    "of", "in", "on", "at", "for", "from", "by", "with", "without", "into",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "have", "has", "had", "it", "its", "this", "that", "these", "those",
    "i", "we", "you", "he", "she", "they", "me", "us", "them", "my", "our",
    "your", "their", "can", "could", "should", "would", "will", "shall",
    "may", "might", "must", "not", "no", "yes", "all", "any", "some", "as",
    "just", "now", "also", "please", "lets", "let", "make", "made", "get",
    "got", "up", "out", "off", "over", "again", "more", "most", "there",
    "what", "when", "where", "which", "who", "why", "how",
})

_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# Age at which the recency bonus has fully decayed to zero. Roughly matches
# how long a repo's context stays relevant between bursts of work on it.
_RECENCY_HORIZON_DAYS = 14.0
# Weight of a fully-fresh entry relative to keyword overlap. Overlap is a
# ratio in [0, 1], so this keeps recency as a tiebreaker between entries of
# similar topical relevance rather than something that can outvote it.
_RECENCY_WEIGHT = 0.25
# Overlap is normalised by the SMALLER of the two vocabularies — "of the words
# these two could possibly have shared, how many did they". Dividing by the
# prompt alone makes every score shrink as the prompt gets longer (a long brief
# scored near zero on everything and let recency decide); dividing by the entry
# alone makes short prompts unable to beat the recency bonus at all. The floor
# stops a two-word entry from scoring 1.0 on a single common token.
_MIN_ENTRY_TOKENS = 8
# Recent failures are pinned, but capped. One account cooldown fails every
# running session at once, and an uncapped pin would then fill the entire block
# with copies of the same infrastructure failure and evict everything relevant.
_MAX_FAILURE_PINS = 2


def _tokenize(text: str) -> set[str]:
    """Lowercase word set with stopwords and 1-2 char noise removed."""
    if not text:
        return set()
    return {
        t for t in _TOKEN_RE.findall(text.lower())
        if len(t) > 2 and t not in _STOPWORDS
    }


def _recency_score(finished: str, now: datetime) -> float:
    """1.0 for something that just finished, decaying linearly to 0.0."""
    if not finished:
        return 0.0
    try:
        dt = datetime.fromisoformat(finished)
    except (ValueError, TypeError):
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = (now - dt).total_seconds() / 86400.0
    if age_days < 0:
        return 1.0
    return max(0.0, 1.0 - (age_days / _RECENCY_HORIZON_DAYS))


def rank_entries(
    entries: list[dict],
    prompt: str,
    limit: int,
    pin_branches: set[str] | None = None,
    pin_ids: set[str] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Pick the `limit` most useful history entries for the current prompt.

    Pinned entries claim slots first, in this priority order (so if the pins
    alone overflow `limit`, the weakest reason to pin is what gets dropped):
      * anything on a branch this session is working on, and
      * anything whose id this session descends from (parent / stacked build),
        which is how the earlier steps of the *same* piece of work stay visible,
      * up to `_MAX_FAILURE_PINS` sessions that failed in the last 24h — a
        fresh failure is worth knowing about even when it looks topically
        unrelated, but a cooldown that failed ten sessions at once must not
        crowd out everything else.

    Remaining slots go to the highest-scoring entries by keyword overlap with
    the current prompt, with a decaying recency bonus as a tiebreaker.
    Returned newest-first so the rendered block reads chronologically.
    """
    if limit <= 0 or not entries:
        return []
    now = now or datetime.now(timezone.utc)
    pin_branches = {b for b in (pin_branches or set()) if b}
    pin_ids = {i for i in (pin_ids or set()) if i}

    def _is_lineage(e: dict) -> bool:
        """Same branch, or an earlier step of this same piece of work."""
        if e.get("branch") and e["branch"] in pin_branches:
            return True
        return bool(e.get("id") and e["id"] in pin_ids)

    def _is_fresh_failure(e: dict) -> bool:
        if e.get("status") != "failed":
            return False
        # Recent failures only — a month-old failure is just history.
        return _recency_score(e.get("finished", ""), now) > (
            1.0 - (1.0 / _RECENCY_HORIZON_DAYS)
        )

    # Lineage pins are precise, so they are uncapped; failure pins are a
    # heuristic and take at most a couple of slots. Order matters: if the pins
    # alone overflow `limit`, the failures are the ones dropped.
    pinned = [e for e in entries if _is_lineage(e)]
    if len(pinned) < limit:
        pinned += [
            e for e in entries if not _is_lineage(e) and _is_fresh_failure(e)
        ][:_MAX_FAILURE_PINS]
    if len(pinned) >= limit:
        return pinned[:limit]

    prompt_tokens = _tokenize(prompt)
    pinned_ids = {id(e) for e in pinned}
    rest = [e for e in entries if id(e) not in pinned_ids]

    scored: list[tuple[float, int, dict]] = []
    for idx, e in enumerate(rest):
        entry_tokens = _tokenize(f"{e.get('topic', '')} {e.get('summary', '')}")
        if prompt_tokens and entry_tokens:
            denom = max(
                _MIN_ENTRY_TOKENS, min(len(prompt_tokens), len(entry_tokens))
            )
            overlap = len(prompt_tokens & entry_tokens) / denom
        else:
            overlap = 0.0
        score = overlap + _RECENCY_WEIGHT * _recency_score(e.get("finished", ""), now)
        # idx keeps the sort deterministic (and newest-first) among ties.
        scored.append((score, -idx, e))

    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    chosen = pinned + [e for _, _, e in scored[: limit - len(pinned)]]

    # Restore newest-first ordering from the source list.
    order = {id(e): i for i, e in enumerate(entries)}
    chosen.sort(key=lambda e: order.get(id(e), 0))
    return chosen
