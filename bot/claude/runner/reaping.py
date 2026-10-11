"""Kill shapes, reap results and the process snapshots they quote."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass

from bot import config
from bot.claude import cgroups, memory
from bot.claude.parser import (
    RunResult,
    extract_result,
    iter_tool_blocks,
    looks_like_fatal_auth_error,
)

log = logging.getLogger(__name__)


# Exit codes that mean "this process died from the signal we sent it".
#
# There are two shapes and we have to accept both.  When a process does NOT
# handle the signal, the kernel kills it and Python reports a NEGATIVE
# returncode (-15 for SIGTERM).  When a process DOES handle it — installs a
# handler, shuts down cleanly, then exits — it exits normally with the shell's
# 128+N convention, which is a POSITIVE returncode (143 for SIGTERM).
#
# Only the negative shape used to be accepted, and that quietly stopped
# matching: the Claude CLI is now a compiled binary that traps SIGTERM and
# exits 143.  Every Kill / Steer therefore failed the kill-shape filter, was
# classified as a crash, and rendered as a red "Failed: Exit code 143" card —
# and worse, reached the account-failover branch, where "no output, no turns"
# reads as "this account fell over instantly".
#
# SIGINT is included because a Ctrl-C-style interrupt is the same intent, and
# SIGKILL because ``kill()`` escalates to it after a 5s grace period.
_KILL_SIGNALS = tuple(
    sig for sig in (
        signal.SIGTERM,
        getattr(signal, "SIGKILL", None),  # POSIX only
        signal.SIGINT,
    ) if sig is not None
)
_SIGNAL_EXIT_CODES: frozenset[int] = frozenset(128 + int(s) for s in _KILL_SIGNALS)


def is_kill_shape(returncode: int | None) -> bool:
    """True when *returncode* is consistent with a signal we sent.

    Only consulted after we already know we fired ``proc.terminate()`` on this
    instance, so it is a corroboration test, not a detection test — its job is
    to stop a process that genuinely crashed in the same second we asked it to
    stop from being reported as a clean cancellation.

    Windows is a blanket True: ``proc.terminate()`` there calls
    ``TerminateProcess(handle, 1)``, which always yields returncode 1, and real
    CLI failures exit 1 too — so there is no returncode that distinguishes
    them and the test degenerates to "we called terminate".
    """
    if os.name == "nt":
        return True
    if returncode is None:
        return False
    return returncode < 0 or returncode in _SIGNAL_EXIT_CODES


def _no_productive_work(res: RunResult) -> bool:
    """True when a run died on its own before producing any real work product.

    Used by the failover paths to decide whether a backup account "took" the
    turn or fell over instantly.  The obvious test — "did it emit any output
    text?" — is wrong: the CLI writes fatal auth errors into ``result_text``,
    not ``error_message`` (t-6614; ``data/results/t-6570.md`` was exactly the
    99-byte 401 string).  That misread a dead account as a genuine mid-work
    failure, dropped the original usage-limit reset, and surfaced a raw 401 as
    a build failure instead of scheduling the normal auto-retry.

    Two guards keep a genuine mid-work failure on the backup visible: a run
    that completed more than one turn is real work no matter what it says, and
    the auth check is the strict ``looks_like_fatal_auth_error`` (short output
    only), so a one-turn session that merely *writes about* 401s isn't
    swallowed as "the backup never ran".

    An intentional kill is excluded outright: a Steer or a Kill button press
    also lands here with one turn and no text, but the account did nothing
    wrong.  Treating it as "the backup fell over" made the failover paths
    stamp a usage-limit reset onto it, so the caller would schedule an
    auto-retry countdown for a run the user had just asked to stop.
    """
    if res.killed_intentionally:
        return False
    if res.num_turns and res.num_turns > 1:
        return False
    text = (res.result_text or "").strip()
    if not text:
        return True
    return (looks_like_fatal_auth_error(text)
            or looks_like_fatal_auth_error(res.error_message))


@dataclass
class StallSnapshot:
    """Diagnostic snapshot captured when a CLI run goes silent.

    Built by ``_capture_process_snapshot`` and surfaced both in the bot.log
    stall warning and in the user-visible Discord stall message. Fields are
    optional because process introspection (psutil) may be unavailable or
    blocked by OS perms — in that case the event-derived fields still render.
    """

    cpu_percent: float | None = None
    rss_mb: float | None = None
    # Memory of the whole process tree, not just the CLI. rss_mb above measures
    # the supervisor, which is nearly always a few hundred MB no matter what is
    # happening underneath it — during the 2026-08-17 OOM incident it read
    # "336MB" while a grandchild sat at 13.7 GB. This is the field that tells
    # you whether a session is in trouble.
    tree_rss_mb: float | None = None
    tree_proc_count: int | None = None
    # The fattest process in the tree, as "name (pid N) X MB". The pid is part
    # of it on purpose: this field exists to be read after the fact, and a name
    # and a size alone cannot be looked up in `ps` an hour later.
    tree_biggest: str | None = None
    conn_count: int | None = None
    https_conn_count: int | None = None
    children_count: int | None = None
    last_event_type: str | None = None
    last_tool_name: str | None = None
    total_events: int = 0
    end_of_turn_seen: bool = False
    error: str | None = None  # set when psutil capture failed

    def summary_line(self) -> str:
        parts: list[str] = []
        if self.cpu_percent is not None:
            parts.append(f"CPU {self.cpu_percent:.0f}%")
        if self.rss_mb is not None:
            parts.append(f"{self.rss_mb:.0f}MB")
        if self.tree_rss_mb is not None:
            tree = f"tree {self.tree_rss_mb / 1024:.1f}GB"
            if self.tree_proc_count:
                tree += f"/{self.tree_proc_count}p"
            if self.tree_biggest:
                tree += f" top={self.tree_biggest}"
            parts.append(tree)
        if self.conn_count is not None:
            parts.append(f"{self.conn_count} conns ({self.https_conn_count or 0}×:443)")
        if self.children_count is not None:
            parts.append(f"{self.children_count} children")
        if self.last_event_type:
            evt = self.last_event_type
            if self.last_tool_name:
                evt = f"{evt}/{self.last_tool_name}"
            parts.append(f"last={evt}")
        parts.append(f"events={self.total_events}")
        if self.end_of_turn_seen:
            parts.append("end_turn_seen")
        if self.error:
            parts.append(f"err={self.error}")
        return " · ".join(parts)


def _last_tool_name(events: list[dict]) -> str | None:
    """Walk back through recent events to find the most recent tool name.

    Handles both ``tool_use`` (assistant-issued) and ``tool_result`` (user
    role) blocks. Bounded to the last 30 events so a long session doesn't
    pay an O(n) scan on every stall log tick.
    """
    for ev in reversed(events[-30:]):
        for tool_name, _ in iter_tool_blocks(ev):
            if tool_name:
                return tool_name
        msg = ev.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        return "tool_result"
    return None


async def _capture_process_snapshot(
    pid: int | None,
    events: list[dict],
    end_of_turn_seen: bool,
) -> StallSnapshot:
    """Build a StallSnapshot for the given CLI process + event stream.

    Event-derived fields (last_event_type, last_tool_name, totals) are always
    populated. psutil-derived fields populate on a best-effort basis: missing
    psutil, NoSuchProcess, AccessDenied, or any unexpected exception
    degrades cleanly to ``snapshot.error`` instead of raising.
    """
    snapshot = StallSnapshot(
        total_events=len(events),
        end_of_turn_seen=end_of_turn_seen,
    )
    if events:
        last = events[-1]
        snapshot.last_event_type = last.get("type") or None
        snapshot.last_tool_name = _last_tool_name(events)

    if pid is None:
        snapshot.error = "no pid"
        return snapshot

    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        snapshot.error = "psutil not installed"
        return snapshot

    # Tree memory first, and in a call of its own, so that nothing below can
    # cost us it. Everything in the CPU/connection block shares a single try and
    # a single early return: one unexpected psutil error down there and the
    # snapshot comes back with no fields at all — and the tree figure is the one
    # that mattered during the 2026-08-17 incident, when the CLI's own RSS read
    # a reassuring 336MB. It is also the cheap half (a readdir plus a small read
    # per process, against a deliberate 2-second CPU sample), so measuring it
    # first costs the rest of the snapshot nothing.
    try:
        tree = await asyncio.to_thread(memory.sample_tree, pid)
    except Exception as exc:
        tree = None
        log.debug("Tree memory sample failed: %s", type(exc).__name__)
    if tree is not None and tree.proc_count:
        snapshot.tree_rss_mb = tree.total_mb
        snapshot.tree_proc_count = tree.proc_count
        if tree.biggest_name:
            snapshot.tree_biggest = f"{tree.offender()} {tree.biggest_mb:.0f}MB"

    def _sample() -> tuple[float | None, float | None, int | None, int | None, int | None, str | None]:
        try:
            proc = psutil.Process(pid)
            # 2s sample is the smallest interval that gives a meaningful CPU%
            # reading from psutil. Runs in a worker thread so the runner's
            # event loop isn't blocked while we measure.
            cpu = proc.cpu_percent(interval=2.0)
            rss_mb = proc.memory_info().rss / (1024 * 1024)
            try:
                conns = proc.net_connections(kind="tcp")
                established = [c for c in conns if c.status == psutil.CONN_ESTABLISHED]
                https = [
                    c for c in established
                    if c.raddr and getattr(c.raddr, "port", None) == 443
                ]
                conn_n: int | None = len(established)
                https_n: int | None = len(https)
            except (psutil.AccessDenied, OSError):
                conn_n = None
                https_n = None
            try:
                children_n: int | None = len(proc.children(recursive=True))
            except (psutil.AccessDenied, OSError):
                children_n = None
            return cpu, rss_mb, conn_n, https_n, children_n, None
        except psutil.NoSuchProcess:
            return None, None, None, None, None, "NoSuchProcess"
        except psutil.AccessDenied:
            return None, None, None, None, None, "AccessDenied"
        except Exception as exc:  # defensive — never let psutil crash the runner
            return None, None, None, None, None, f"{type(exc).__name__}"

    try:
        cpu, rss_mb, conn_n, https_n, children_n, err = await asyncio.to_thread(_sample)
    except Exception as exc:
        snapshot.error = f"to_thread failed: {type(exc).__name__}"
        return snapshot
    snapshot.cpu_percent = cpu
    snapshot.rss_mb = rss_mb
    snapshot.conn_count = conn_n
    snapshot.https_conn_count = https_n
    snapshot.children_count = children_n
    if err:
        snapshot.error = err
    return snapshot


# --- Memory guard: what the user is told, and when a reap is moot -------------
#
# Pulled out of the watchdog loop rather than written inline. Both messages are
# pure functions of a tree measurement and the configured ceiling, they are the
# only part of the guard the user ever reads, and inline they sat six levels of
# indentation deep inside a 400-line method where nothing could reach them.


def _turn_completed_successfully(events: list[dict]) -> bool:
    """True if the stream's ``result`` event reports success.

    Used to decide that a memory reap is moot, which means it has to agree with
    ``extract_result`` about what success is — otherwise a run reported as
    failed loses the memory explanation that would have gone with it. So this
    reads the LAST ``result`` event, exactly as ``extract_result`` does, rather
    than asking whether *any* of them looks successful. And it reads the same
    default: a ``result`` event that omits ``is_error`` is a completed turn.
    """
    verdict: dict | None = None
    for ev in events:
        if ev.get("type") == "result":
            verdict = ev
    return verdict is not None and not verdict.get("is_error", False)


def _memory_kill_detail(tree: memory.TreeMemory, kill_mb: int) -> str:
    """The in-thread notice for a session the guard has decided to reap.

    Says "largest single process", never "most of it": the headline figure is a
    sum across the tree, so on a session running many processes at once the
    biggest one can be a small share of the total. Saying otherwise would
    misdirect the reader in exactly the case that is hardest to diagnose.
    """
    return (
        f"Its {tree.proc_count} processes reached "
        f"{tree.total_mb / 1024:.1f} GB between them "
        f"(ceiling {kill_mb / 1024:.1f} GB); the largest single one was "
        f"{tree.offender()} at {tree.biggest_mb / 1024:.1f} GB. "
        f"Killing it here keeps the other sessions alive."
    )


def _fleet_kill_detail(
    tree: memory.TreeMemory, pressure: memory.MemoryPressure,
) -> str:
    """The in-thread notice for a session reaped because the MACHINE ran out.

    Deliberately does not lead with this session's number. It was not over its
    ceiling and saying "reached 3.1 GB" first invites the reader to conclude it
    misbehaved, when the true statement is that several well-behaved sessions
    plus the desktop added up to more than the machine has. The order is:
    the machine ran out, we were the ones filling our own limit, this was the
    biggest tree, so this is the one that went.
    """
    return (
        f"The machine ran out of memory — {pressure.human()} — and the bot's "
        f"sessions together are over their shared limit. This session was not "
        f"over its own ceiling; it was simply the largest tree running "
        f"({tree.total_mb / 1024:.1f} GB across {tree.proc_count} processes, "
        f"biggest {tree.offender()}), so it is the one being stopped rather "
        f"than leaving the kernel to pick. Its work is on disk and it will "
        f"pick up from there."
    )


def _memory_warning_detail(tree: memory.TreeMemory, kill_mb: int) -> str:
    """The in-thread warning for a session that is heading for the ceiling.

    Sent whether or not the kill is armed. ``SESSION_MEM_KILL_MB=0`` is
    documented as "warnings only", and a warning nobody outside bot.log can see
    is not that — so what varies with the setting is the sentence about being
    stopped, not whether the session hears about it at all.
    """
    ceiling = (
        f" It will be stopped at {kill_mb / 1024:.1f} GB."
        if kill_mb > 0 else
        " Nothing will stop it automatically — the per-session kill is "
        "switched off here."
    )
    return (
        f"Its {tree.proc_count} processes are at "
        f"{tree.total_mb / 1024:.1f} GB between them; the largest single one "
        f"is {tree.offender()} at {tree.biggest_mb / 1024:.1f} GB."
        + ceiling
        + " If that's a job you started, make it smaller now."
    )


def _fmt_duration(secs: float) -> str:
    """Hours-and-minutes for a number the reader has to reason about.

    "14407s" is what the log said about the run that should not have been
    killed, and nobody reading it converts that to four hours in their head.
    """
    total = int(secs)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    if total < 60:
        # Only the harness runs the watchdog at these speeds, but "past the 0m
        # hard lifetime limit" is a sentence no one should have to read.
        return f"{total}s"
    return f"{minutes}m"


def _lifetime_kill_detail(
    elapsed_secs: float, silence_secs: float, hard_cap: bool,
) -> str:
    """The in-thread notice for a session the orphan safety-net is stopping.

    Leads with the silence, not the age, because the silence is the actual
    reason — the age only decided that we were allowed to look.
    """
    if hard_cap:
        return (
            f"It has been running for {_fmt_duration(elapsed_secs)}, past the "
            f"{_fmt_duration(config.MAX_PROCESS_HARD_LIFETIME_SECS)} hard "
            f"ceiling that applies however busy a run looks. Whatever it "
            f"managed to say is kept, and the session can be resumed from "
            f"where it got to."
        )
    return (
        f"It produced nothing for {_fmt_duration(silence_secs)} after running "
        f"for {_fmt_duration(elapsed_secs)}, which is the shape of a hung "
        f"process rather than a slow one. Whatever it managed to say is kept, "
        f"and the session can be resumed from where it got to."
    )


def _reaped_result_base(
    events: list[dict],
    session_id: str | None,
    poisoning: list[str] | None,
) -> RunResult:
    """What a watchdog reap must preserve, whichever watchdog fired.

    Both reaps — the memory guard and the orphan safety-net — kill a run that
    has usually done real work, and both used to be written from scratch. The
    lifetime cap returned a bare ``RunResult``, so four hours of work rendered
    as an empty red FAILED card. Naming the salvage once is what stops the two
    paths drifting apart again.

    ``extract_result`` is the whole point: it carries the tool record (which
    the chain reads via ``CODE_CHANGE_TOOLS`` to decide whether a build changed
    code at all, so an empty list reads as "no changes made"), the bash log the
    eval surface feeds on, the cost/duration/token counters — which is the only
    place a reaped attempt will ever report them from — and ``result_text``,
    which its own no-``result``-event fallback fills from the last substantial
    assistant turn.

    Deliberately NOT synthesised: ``num_turns``. The account-failover heuristic
    reads ``>1 turn`` as proof the account took the turn, so inventing turns
    here could hide a dead backup account. ``_carry_forward_work_record``
    refuses to merge it for exactly the same reason.

    ``error_message`` is left to the caller — it is what ``finalize_run``
    actually shows the user, and it is the only part that differs between the
    two reaps.
    """
    result = extract_result(events)
    result.is_error = True
    # A reap lands mid-turn, long before the CLI emits a `result` event, so the
    # id stamped from the init event on turn one is usually the only one there
    # is — and it is what makes Retry a resume rather than a fresh start.
    if not result.session_id:
        result.session_id = session_id
    if poisoning:
        result.path_poisoning = list(poisoning)
    return result


# A SIGKILL as asyncio reports it: -9 when the kernel delivered it to the CLI
# directly, 137 (128+9) when it went through a shell first. systemd-oomd kills
# every process in the scope this way, and so does the kernel when the CLI
# itself is the process it picks.
_SIGKILL_EXIT_CODES = (-9, 137)
# A SIGTERM as asyncio reports it: -15, or 143 from a CLI that handled it and
# exited. This is how a session dies when the kernel's OOM killer picks some
# OTHER process in its scope (a build, a test runner): scopes carry systemd's
# default OOMPolicy=stop, so the service manager then stops the whole scope
# with a SIGTERM. Verified on 2026-10-08 with a 60M scope whose child
# overran it: the parent exited -15 and the journal read "The kernel OOM
# killer killed some processes in this unit." A SIGTERM has many innocent
# senders, so this shape only counts when the journal says so.
_SIGTERM_EXIT_CODES = (-15, 143)
# How long a SIGTERM-shaped exit waits for the journal to catch up before
# concluding it was not an out-of-memory stop. systemd logs the OOM line before
# it sends the SIGTERM, so this only covers journald's ingestion lag.
_OOM_JOURNAL_LAG_SECS = 1.0


def _describe_last_tool(result: RunResult) -> str:
    """What the session was doing when it died, in a form it can act on."""
    if result.last_tool == "Bash" and result.bash_commands:
        cmd = " ".join(result.bash_commands[-1].split())
        if len(cmd) > 160:
            cmd = cmd[:157] + "..."
        return f"the Bash command `{cmd}`"
    if result.last_tool:
        return f"the {result.last_tool} tool"
    return "something no tool call recorded (it may have been thinking)"


async def _classify_oom_kill(result: RunResult, scope_unit: str) -> bool:
    """Recognise a run the operating system killed for memory, and say so.

    The shape is a SIGKILL on a scoped run that the bot did not send: the
    caller has already ruled out its own kill() and memory reap
    (``_bot_signalled``), a Kill or Steer (``killed_intentionally``), and an
    unscoped run. A reap that went through normally returns a synthesised
    result with no exit code and never reaches here; the mark is for the one
    that stood down. The journal is asked which killer it was, and for a
    SIGKILL a lookup that finds nothing still classifies: a -9 the bot did not
    send on a session scope is an outside kill either way, and resuming it
    once is the safe answer.

    A SIGTERM is the other shape (see ``_SIGTERM_EXIT_CODES``), and for that
    one the journal IS the gate. Nothing else separates systemd stopping the
    scope after an OOM kill from any other SIGTERM, including the bot's own
    terminates that carry no ``_bot_signalled`` mark (the oversized-line
    guard, a lifetime reap that stood down).

    Rewrites ``error_message`` to carry "killed by the system's out-of-memory
    protection", which ``parser.is_account_agnostic_error`` matches. Without
    it a session killed before its first turn has no output and no turns, the
    no-turns failover heuristic's exact signature, and the work is handed to
    the backup subscription to be killed the same way.
    """
    if not result.is_error or result.memory_kill_note:
        return False
    if result.exit_code in _SIGKILL_EXIT_CODES:
        needs_evidence = False
    elif result.exit_code in _SIGTERM_EXIT_CODES:
        needs_evidence = True
    else:
        return False
    how = await cgroups.oom_kill_evidence(scope_unit)
    if how is None and needs_evidence:
        await asyncio.sleep(_OOM_JOURNAL_LAG_SECS)
        how = await cgroups.oom_kill_evidence(scope_unit)
        if how is None:
            return False
    doing = _describe_last_tool(result)
    if how:
        result.error_message = (
            f"Killed by the system's out-of-memory protection ({how}) "
            f"while running {doing}"
        )
    else:
        result.error_message = (
            "Killed by the system's out-of-memory protection, or by another "
            "outside SIGKILL (no journal entry confirmed which), while "
            f"running {doing}"
        )
        how = "most likely systemd-oomd; the journal did not confirm it"
    result.oom_kill_note = config.OOM_KILL_NUDGE_TEMPLATE.format(
        how=how, doing=doing,
    )
    log.warning(
        "OOM kill classified for scope %s: %s", scope_unit, result.error_message,
    )
    return True


def _lifetime_kill_result(
    events: list[dict],
    session_id: str | None,
    poisoning: list[str] | None,
    elapsed_secs: float,
    silence_secs: float,
    hard_cap: bool,
) -> RunResult:
    """The failure an orphan-reaped run reports, built from what it did.

    The wording MUST keep the substring "lifetime limit".
    ``parser.is_account_agnostic_error`` matches on it to suppress the no-turns
    account-failover heuristic. The salvaged ``result_text`` alone defeats that
    heuristic in the common case, but a run reaped before it ever said anything
    still has the exact "this account fell over instantly" shape, and that is
    the case the phrase covers.
    """
    result = _reaped_result_base(events, session_id, poisoning)
    if hard_cap:
        result.error_message = (
            f"Stopped after {_fmt_duration(elapsed_secs)}: past the "
            f"{_fmt_duration(config.MAX_PROCESS_HARD_LIFETIME_SECS)} hard "
            f"lifetime limit, which applies however busy a run looks."
        )
    else:
        result.error_message = (
            f"Stopped: no output for {_fmt_duration(silence_secs)} after "
            f"{_fmt_duration(elapsed_secs)} of running — past the "
            f"{_fmt_duration(config.MAX_PROCESS_LIFETIME_SECS)} lifetime "
            f"limit, and silent long enough to look hung rather than slow."
        )
    return result


def _memory_kill_result(
    events: list[dict],
    tree: memory.TreeMemory,
    avail_mb: float | None,
    session_id: str | None,
    poisoning: list[str] | None = None,
    pressure: memory.MemoryPressure | None = None,
) -> RunResult:
    """The failure a reaped run reports, built from what that run actually did.

    Built on top of ``extract_result`` rather than from scratch, and that is the
    whole reason it is a function. A reaped attempt has usually done real work,
    and ``_carry_forward_work_record`` can only forward what this result
    carries: hand it a bare ``RunResult`` and the attempt that resumes inherits
    an empty ``tools_used`` — which the chain reads to decide whether a build
    changed code at all, so a build that made every one of its edits before
    being reaped comes back as "no changes made" and its diff is treated as
    nothing. The cost, duration and token counters are the same story: this is
    the only place the reaped attempt will ever report them from.

    ``result_text`` is kept for the same reason the ordinary non-zero-exit path
    keeps it — whatever the session managed to say before it died is still the
    best account of what it was doing — while ``error_message`` below is what
    the user is actually shown, because ``finalize_run`` prefers it.

    The salvage itself lives in ``_reaped_result_base``, shared with the orphan
    safety-net's reap; the session id it carries is worthless unless it reaches
    a RESUMED attempt, which is what the note below is for.
    """
    result = _reaped_result_base(events, session_id, poisoning)
    # "?" rather than a phrase, in both templates below: they read "About
    # {avail_gb} GB was free", so anything but a number-shaped value produces a
    # broken sentence in the one message whose entire job is to be read
    # carefully.
    avail_gb = f"{avail_mb / 1024:.1f}" if avail_mb is not None else "?"
    if pressure is not None:
        # Reaped for the machine, not for its own ceiling. Both the failure the
        # user reads and the note the resumed attempt gets have to say that, or
        # the agent spends its next run shrinking a job that was never too big.
        result.error_message = (
            f"Stopped to keep the machine alive: memory ran out "
            f"machine-wide ({pressure.human()}) and this session's "
            f"{tree.total_mb / 1024:.1f} GB was the largest of those running. "
            f"It was under its own "
            f"{config.SESSION_MEM_KILL_MB / 1024:.1f} GB ceiling."
        )
        result.memory_kill_note = config.MEMORY_FLEET_KILL_NUDGE_TEMPLATE.format(
            peak_gb=f"{tree.total_mb / 1024:.1f}",
            limit_gb=f"{config.SESSION_MEM_KILL_MB / 1024:.1f}",
            avail_gb=avail_gb,
            pressure=pressure.human(),
        )
        return result
    result.error_message = (
        f"Stopped: this session's processes reached "
        f"{tree.total_mb / 1024:.1f} GB of memory, over the "
        f"{config.SESSION_MEM_KILL_MB / 1024:.1f} GB ceiling for a single "
        f"session. Largest was {tree.offender()}"
        + (
            f"; {avail_mb / 1024:.1f} GB was free machine-wide."
            if avail_mb is not None else "."
        )
    )
    result.memory_kill_note = config.MEMORY_KILL_NUDGE_TEMPLATE.format(
        peak_gb=f"{tree.total_mb / 1024:.1f}",
        limit_gb=f"{config.SESSION_MEM_KILL_MB / 1024:.1f}",
        offender=tree.offender(),
        avail_gb=avail_gb,
    )
    return result
