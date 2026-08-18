"""Watches — an event-triggered self-wake.

A self-wake fires on a clock: a session facing a 40-minute job has to GUESS a
delay, and the thread looks dead while it waits. A *watch* fires on the job
itself. The session names a process (or a done-marker in its log), the poller
below checks it on the scheduler's existing 30s tick, and the moment it
finishes the watch calls ``store.add_wake`` with ``next_run_at=now``.

That last part is the whole design: a watch is **not** a second resume path.
It manufactures an ordinary wake, so the runaway cap, the busy re-arm,
``_replay_to_thread`` and the unattended-turn protocol are inherited unchanged
from code that already works.

While a watch is armed the thread carries a heartbeat message that EDITS
itself in place (never posts again), so "still running" is visible at a glance
without the thread filling up. ``bot/discord/tags.py`` and ``idle.py`` also
consult ``has_armed_watch`` so a watching thread keeps its *active* tag and
never picks up the 💤 idle marker — a thread that is genuinely busy must not
look asleep.
"""

from __future__ import annotations

import errno
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from bot import config
from bot.claude.types import Watch
from bot.platform.base import ButtonSpec
from bot.platform.formatting import format_delay_secs

if TYPE_CHECKING:
    from bot.platform.base import Messenger
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


# --- Directive parsing ------------------------------------------------------
# Deliberately a sibling of lifecycle._parse_wake_directive rather than a
# generalisation of it: same kv + tilde-body + quoted-line-skip shape, but the
# fields, defaults and failure modes are different enough that sharing would
# mean a parameterised parser nobody can read.
_WATCH_DIRECTIVE_RE = re.compile(r"\[BOT_CMD:\s*/watch(?:\s+(.+?))?\s*\]")
_WATCH_BODY_RE = re.compile(r"~~~watch\s*\n(.*?)\n~~~", re.DOTALL)
_WATCH_KV_RE = re.compile(r'''(\w+)=(?:"([^"]*)"|'([^']*)'|(\S+))''')
_WATCH_QUOTED_PREFIX = re.compile(r"^\s*(?:>|`|```|#{1,3}\s)")
_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_DURATION_MULT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(raw: str | None, default: int) -> int:
    """``"6h"`` / ``"90m"`` / ``"3600"`` -> seconds. Garbage -> ``default``.

    Sessions write durations the way humans do, and a typo'd unit must not
    silently drop a watch — it falls back to the default like /wake's delay.
    """
    if raw is None:
        return default
    m = _DURATION_RE.match(str(raw))
    if not m:
        return default
    try:
        return int(float(m.group(1)) * _DURATION_MULT[m.group(2).lower()])
    except (ValueError, KeyError, OverflowError):
        return default


def _unquoted_directives(text: str):
    """Yield every ``[BOT_CMD: /watch ...]`` match that is a REQUEST.

    Skips EXAMPLES, not requests — the same three ways a directive can be a
    quoted demo that ``lifecycle._parse_wake_directive`` guards against: a
    quoted/code/heading line, a position inside an open ``` fence, or inline
    backticks in prose. Discussing this feature must not arm it.
    """
    if not text:
        return
    for m in _WATCH_DIRECTIVE_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if _WATCH_QUOTED_PREFIX.match(text[line_start:m.start()]):
            continue
        if text.count("```", 0, m.start()) % 2 == 1:
            continue
        if m.start() > 0 and text[m.start() - 1] == "`":
            continue
        yield m


def has_watch_directive(text: str) -> bool:
    """True if the turn WROTE a real /watch directive, parseable or not.

    ``parse_watch_directive`` returning None is ambiguous on its own: no
    directive at all, or one that couldn't be armed. The caller needs to tell
    those apart, because the second case must be explained rather than met
    with silence — an unexplained non-armed watch is exactly the dead-end
    this feature exists to remove.
    """
    return any(True for _ in _unquoted_directives(text))


def parse_watch_directive(text: str) -> dict | None:
    """Extract a ``[BOT_CMD: /watch ...]`` directive from a turn's output.

    Returns the raw kv dict plus the resume ``prompt``, or ``None`` when there
    is no real (unquoted) directive carrying both a prompt and a usable
    trigger. Usable means a ``pid=``, or a ``done=`` regex WITH the ``log=``
    it is matched against — a done marker with nothing to read from can only
    ever end in a timeout, so it is refused up front and reported by the
    caller instead of silently becoming a six-hour wait.
    """
    for m in _unquoted_directives(text):
        kv: dict[str, str] = {}
        for kvm in _WATCH_KV_RE.finditer(m.group(1) or ""):
            val = kvm.group(2) if kvm.group(2) is not None else (
                kvm.group(3) if kvm.group(3) is not None else kvm.group(4)
            )
            kv[kvm.group(1)] = val or ""
        body = _WATCH_BODY_RE.search(text, m.end())
        prompt = (body.group(1).strip() if body
                  else (kv.get("prompt") or "").strip())
        if not prompt:
            continue
        pid: int | None = None
        raw_pid = (kv.get("pid") or "").strip()
        if raw_pid:
            try:
                pid = int(raw_pid)
            except ValueError:
                pid = None
        done_re = (kv.get("done") or "").strip()
        log_path = (kv.get("log") or "").strip()
        if pid is None and not (done_re and log_path):
            # No usable trigger: nothing to poll, or a done marker with no log
            # to find it in. Skip so a later real directive can engage.
            continue
        return {
            "prompt": prompt,
            "pid": pid,
            "done": done_re,
            "log": log_path,
            "progress": (kv.get("progress") or "").strip(),
            "label": (kv.get("label") or "").strip(),
            "timeout": kv.get("timeout"),
            "every": kv.get("every"),
        }
    return None


# --- Process liveness -------------------------------------------------------

def read_pid_start(pid: int) -> str:
    """Start-time token for ``pid``, or ``""`` when it can't be read.

    Field 22 of ``/proc/<pid>/stat``. The comm field is parenthesised and may
    itself contain spaces and parens, so everything up to the LAST ``)`` is
    discarded before splitting — the usual bug in naive stat parsers.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""
    close = raw.rfind(")")
    if close == -1:
        return ""
    fields = raw[close + 2:].split()
    # fields[0] is state (stat field 3), so starttime (field 22) is index 19.
    return fields[19] if len(fields) > 19 else ""


def process_alive(pid: int, start_token: str = "") -> bool:
    """True if ``pid`` is running AND is the process we armed on.

    PIDs are recycled. Without the start-time comparison a watch could latch
    onto a stranger's process and sit until its safety timeout while the job
    it actually cares about finished an hour ago. A zombie counts as finished:
    the work is over, only the exit status is unreaped.
    """
    proc = Path(f"/proc/{pid}")
    if proc.exists():
        current = read_pid_start(pid)
        if start_token and current and current != start_token:
            return False          # PID reused by a different process
        try:
            raw = (proc / "stat").read_text(encoding="utf-8", errors="replace")
            close = raw.rfind(")")
            if close != -1:
                state = raw[close + 2:].split()[0]
                if state == "Z":
                    return False  # exited, just not reaped
        except (OSError, IndexError, ValueError):
            pass
        return True
    if proc.parent.exists():
        return False              # /proc is mounted and the pid is not there
    # No procfs (non-Linux): fall back to a signal-0 probe. EPERM means the
    # process exists but belongs to someone else — alive, not gone.
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    except Exception:
        return True               # unknown -> assume alive; the timeout catches it


# --- Log reading ------------------------------------------------------------

def _resolve_log(watch: Watch) -> Path | None:
    if not watch.log_path:
        return None
    p = Path(watch.log_path)
    if not p.is_absolute() and watch.repo_path:
        p = Path(watch.repo_path) / p
    return p


def read_log_tail(watch: Watch) -> str:
    """Last ``WATCH_LOG_TAIL_BYTES`` of the watched log, or ``""``."""
    p = _resolve_log(watch)
    if p is None:
        return ""
    try:
        size = p.stat().st_size
        with p.open("rb") as fh:
            if size > config.WATCH_LOG_TAIL_BYTES:
                fh.seek(size - config.WATCH_LOG_TAIL_BYTES)
            return fh.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def _sanitize_excerpt(raw: str) -> str:
    """Make a line lifted out of a log file safe to paste into a message.

    Two problems, both caused by echoing a file nobody wrote for Discord: a
    stray backtick pairs with the progress bar's backticks and mangles the
    whole heartbeat, and an ``@everyone`` in a build log would ping the server.
    """
    clean = raw.replace("`", "'")
    clean = clean.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    return clean


def _last_line(tail: str) -> str:
    for line in reversed(tail.splitlines()):
        if line.strip():
            return _sanitize_excerpt(line.strip())
    return ""


def parse_progress(tail: str, pattern: str) -> tuple[float, str] | None:
    """``(fraction, "cur/total")`` from the LAST match of ``pattern``.

    One capture group is read as a percentage, two as current/total. A bad
    regex, no match, or a nonsensical total returns ``None`` — progress is a
    nicety, so every failure degrades to "elapsed time only" rather than
    breaking the heartbeat.
    """
    if not pattern or not tail:
        return None
    try:
        rx = re.compile(pattern)
    except re.error:
        log.debug("watch progress regex invalid: %r", pattern)
        return None
    last = None
    for last in rx.finditer(tail):
        pass
    if last is None:
        return None
    groups = [g for g in last.groups() if g is not None]
    try:
        if len(groups) >= 2:
            cur, total = float(groups[0]), float(groups[1])
            if total <= 0:
                return None
            return max(0.0, min(1.0, cur / total)), f"{groups[0]}/{groups[1]}"
        if len(groups) == 1:
            pct = float(groups[0])
            return max(0.0, min(1.0, pct / 100.0)), f"{groups[0]}%"
    except (TypeError, ValueError):
        return None
    return None


# --- Heartbeat rendering ----------------------------------------------------

_BAR_WIDTH = 16


def _parse_ts(raw: str) -> datetime | None:
    """ISO timestamp -> aware UTC datetime, or None.

    Everything this module writes is timezone-aware, but a hand-edited or
    migrated state file can carry a NAIVE stamp — and subtracting one of those
    from an aware ``now`` raises TypeError deep inside the fire path, where it
    would wedge the watch permanently instead of resuming the thread. Assume
    UTC for a naive stamp rather than letting the arithmetic explode.
    """
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _elapsed_secs(watch: Watch, now: datetime) -> int:
    armed = _parse_ts(watch.armed_at)
    if armed is None:
        return 0
    try:
        return max(0, int((now - armed).total_seconds()))
    except (TypeError, OverflowError, OSError):
        return 0


def render_heartbeat(watch: Watch, now: datetime) -> str:
    """The live "still running" message body."""
    label = watch.label or (f"pid {watch.pid}" if watch.pid else "job")
    elapsed = format_delay_secs(_elapsed_secs(watch, now))
    lines = [f"⏳ **{label}** — running {elapsed}"]

    tail = read_log_tail(watch)
    prog = parse_progress(tail, watch.progress_re)
    if prog is not None:
        frac, detail = prog
        filled = int(round(frac * _BAR_WIDTH))
        bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
        lines.append(f"`{bar}`  {round(frac * 100)}%  ·  {detail}")
    if watch.log_path:
        lines.append(f"-# {watch.log_path[:180]}")
    last = _last_line(tail)
    if last:
        lines.append(f"-# last: {last[:180]}")
    return "\n".join(lines)


def _stop_buttons(watch: Watch) -> list[list[ButtonSpec]]:
    return [[ButtonSpec("Stop watching", f"watch_stop:{watch.id}")]]


# --- Arming -----------------------------------------------------------------

def build_watch(
    data: dict, *, channel_id: str, repo_name: str = "", repo_path: str = "",
    now: datetime | None = None,
) -> Watch:
    """Turn a parsed directive into a Watch record (not yet stored)."""
    now = now or datetime.now(timezone.utc)
    timeout = parse_duration(data.get("timeout"), config.WATCH_DEFAULT_TIMEOUT_SECS)
    timeout = max(60, min(timeout, config.WATCH_MAX_TIMEOUT_SECS))
    every = parse_duration(data.get("every"), config.WATCH_HEARTBEAT_SECS)
    every = max(config.WATCH_MIN_HEARTBEAT_SECS, every)
    pid = data.get("pid")
    return Watch(
        id="",                    # assigned by store.add_watch
        channel_id=channel_id,
        prompt=data.get("prompt", ""),
        label=data.get("label", ""),
        pid=pid,
        pid_start=read_pid_start(pid) if pid else "",
        log_path=data.get("log", ""),
        progress_re=data.get("progress", ""),
        done_re=data.get("done", ""),
        armed_at=now.isoformat(),
        timeout_at=(now + timedelta(seconds=timeout)).isoformat(),
        every_secs=every,
        repo_name=repo_name,
        repo_path=repo_path,
    )


def has_armed_watch(store: StateStore, channel_id: str) -> bool:
    """True if ``channel_id`` is waiting on a job right now.

    Read by the tag and idle paths so a watching thread stays visibly busy.
    """
    try:
        return store.watch_for_channel(channel_id) is not None
    except Exception:
        return False


# --- Polling ----------------------------------------------------------------

def _fire_reason(watch: Watch, now: datetime) -> str | None:
    """``"done"`` / ``"exited"`` / ``"timeout"``, or ``None`` to keep waiting."""
    if watch.done_re:
        tail = read_log_tail(watch)
        if tail:
            try:
                if re.search(watch.done_re, tail):
                    return "done"
            except re.error:
                log.debug("watch done regex invalid: %r", watch.done_re)
    if watch.pid and not process_alive(watch.pid, watch.pid_start):
        return "exited"
    deadline = _parse_ts(watch.timeout_at)
    if deadline is None or now >= deadline:
        return "timeout"          # unreadable deadline -> don't wait forever
    return None


def _resume_prompt(watch: Watch, reason: str, now: datetime) -> str:
    label = watch.label or (f"pid {watch.pid}" if watch.pid else "the job")
    elapsed = format_delay_secs(_elapsed_secs(watch, now))
    if reason == "timeout":
        head = (
            f"The watch on **{label}** timed out after {elapsed} — the job did "
            "NOT report finishing. Check whether it is still making progress: "
            "if it is, arm a fresh [BOT_CMD: /watch]; if it is stuck or dead, "
            "say so plainly rather than assuming it succeeded."
        )
    elif reason == "done":
        head = (
            f"**{label}** finished after {elapsed} — its log hit the done "
            "marker you were watching for."
        )
    else:
        head = f"**{label}** finished after {elapsed} — the process exited."
    if watch.log_path:
        head += f" Log: {watch.log_path}"
    return f"{head}\n\n{watch.prompt}"


def _final_line(watch: Watch, reason: str, now: datetime) -> str:
    label = watch.label or (f"pid {watch.pid}" if watch.pid else "job")
    elapsed = format_delay_secs(_elapsed_secs(watch, now))
    if reason == "timeout":
        return f"⚠️ **{label}** — still running after {elapsed}, waking anyway"
    return f"✅ **{label}** — finished after {elapsed}"


async def poll_watches(
    store: StateStore,
    messenger: Messenger | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """One poll pass. Returns how many watches fired.

    Called from the scheduler's 30s tick. A fired watch becomes a wake due
    immediately, which that same scheduler picks up on its next tick — so the
    end-to-end lag between a job exiting and the session resuming is under a
    minute, without a second polling loop existing anywhere.
    """
    now = now or datetime.now(timezone.utc)
    fired = 0
    for watch in store.list_watches():
        try:
            reason = _fire_reason(watch, now)
        except Exception:
            log.exception("Watch %s check failed — leaving armed", watch.id)
            continue
        if reason is None:
            try:
                await _beat(store, messenger, watch, now)
            except Exception:
                # A heartbeat is cosmetic; aborting the loop here would leave
                # every watch after this one unpolled for the whole tick.
                log.debug("watch %s heartbeat pass failed", watch.id, exc_info=True)
            continue
        # Fire: hand off to the ordinary wake path and stand down.
        try:
            store.add_wake(
                prompt=_resume_prompt(watch, reason, now),
                channel_id=watch.channel_id,
                next_run_at=now.isoformat(),
                repo_name=watch.repo_name,
                repo_path=watch.repo_path,
            )
            store.delete_watch(watch.id)
        except Exception:
            log.exception("Watch %s failed to convert into a wake", watch.id)
            continue
        fired += 1
        log.info(
            "Watch %s fired (%s) for thread %s after %ds",
            watch.id, reason, watch.channel_id, _elapsed_secs(watch, now),
        )
        if messenger is not None and watch.heartbeat_msg_id:
            try:
                await messenger.edit_text(
                    watch.channel_id, watch.heartbeat_msg_id,
                    _final_line(watch, reason, now), buttons=None,
                )
            except Exception:
                log.debug("watch final edit failed", exc_info=True)
    return fired


async def _beat(
    store: StateStore, messenger: Messenger | None, watch: Watch, now: datetime,
) -> None:
    """Refresh (or first-post) the heartbeat message if it's due."""
    if messenger is None:
        return
    last = _parse_ts(watch.last_beat_at) if watch.last_beat_at else None
    if last is not None and now < last + timedelta(seconds=watch.every_secs):
        return
    body = render_heartbeat(watch, now)
    try:
        if watch.heartbeat_msg_id:
            await messenger.edit_text(
                watch.channel_id, watch.heartbeat_msg_id, body,
                buttons=_stop_buttons(watch),
            )
        else:
            msg_id = await messenger.send_text(
                watch.channel_id, body,
                buttons=_stop_buttons(watch), silent=True,
            )
            # An empty id means the channel is gone/unresolvable. Leave it unset
            # so the next beat retries a post rather than editing nothing.
            if msg_id:
                watch.heartbeat_msg_id = str(msg_id)
    except Exception:
        log.debug("watch heartbeat failed for %s", watch.id, exc_info=True)
        return
    watch.last_beat_at = now.isoformat()
    try:
        store.update_watch(watch)
    except Exception:
        log.debug("watch heartbeat persist failed", exc_info=True)
