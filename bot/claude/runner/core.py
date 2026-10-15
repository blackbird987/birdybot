"""ClaudeRunner: spawning the CLI, streaming its output, recovery, tasks and kills."""

from __future__ import annotations

import asyncio
import enum
import json as _json_mod
import logging
import os
import time
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from bot.store.state import StateStore

from bot import config
from bot.textutil import parse_iso_utc
from bot.claude.auth_health import account_label
from bot.claude import cgroups, memory
from bot.claude.parser import (
    RunResult,
    detect_path_poisoning,
    extract_progress,
    extract_result,
    extract_summary,
    extract_usage,
    format_ask_question,
    iter_tool_blocks,
    last_assistant_text,
    parse_stream_line,
)
from bot.claude.provider import ProviderConfig, get_provider
from bot.claude.types import (
    Instance, KillOutcome,
)
from bot.claude.runner.accounts import AccountsMixin
from bot.claude.runner.admission import AdmissionMixin
from bot.claude.runner.git_merge import MergeMixin
from bot.claude.runner.git_release import ReleaseMixin
from bot.claude.runner.prompt_build import PromptMixin
from bot.claude.runner.session_files import SessionFilesMixin
from bot.claude.runner.worktrees import WorktreeMixin
from bot.claude.runner.common import (
    ContextResetCallback,
    ProgressCallback,
    RecoveryCallback,
    SESSION_STRIPPED_ENV_VARS,
    StallCallback,
    _NOWND,
    _notify,
)
from bot.claude.runner.reaping import (
    _classify_oom_kill,
    _lifetime_kill_result,
    _memory_kill_result,
    _turn_completed_successfully,
    is_kill_shape,
)
from bot.claude.runner.recovery import (
    RecoveryMixin,
    _RECOVERY_STEPS,
    _RunAttempt,
    _is_primary_model,
)
from bot.claude.runner.watchdog import _Watchdog

log = logging.getLogger(__name__)


def _lower_priority(pid: int) -> None:
    """Renice a freshly spawned session CLI below the bot's own priority.

    Applied from the parent *after* the spawn, deliberately, and NOT as a
    ``preexec_fn``. preexec_fn is the obvious way to do this and it is the
    wrong one here: CPython runs it in the child between fork and exec, where
    only async-signal-safe work is legal, and this bot forks from a process
    with well over a hundred threading callsites. A child that lands on a
    lock some other thread held at fork time — the allocator's, logging's —
    deadlocks before exec, which would hang a session spawn permanently while
    holding the runner's slot. The documented failure mode is far worse than
    the problem being solved.

    The cost of doing it here instead is a window of a few microseconds in
    which the CLI runs at the bot's own priority. That is safe in a way the
    deadlock is not: the CLI is node and spends hundreds of milliseconds
    starting up before it forks anything, so nothing real is created inside
    the window, and the worst case is a session that runs at normal priority
    rather than one that never runs at all.

    Niceness survives exec and is inherited by children, so this single call
    covers the whole tree the session goes on to build — its shells, dotnet,
    Roslyn, npm — without the runner having to find them.

    The target is computed from the supervisor's *own* nice rather than set
    absolutely, because SESSION_CPU_NICE is documented as a distance below the
    supervisor: a unit that later grows a ``Nice=`` would otherwise silently
    close that gap. Clamped to 19, the floor the scheduler accepts. Only ever
    an increase, which needs no privileges — a decrease would, and config
    clamps negatives away so one is never attempted.
    """
    nice = config.SESSION_CPU_NICE
    # setpriority is POSIX-only; on Windows there is no nice(2) ladder to walk.
    if not nice or not hasattr(os, "setpriority"):
        return
    try:
        own = os.getpriority(os.PRIO_PROCESS, 0)
        os.setpriority(os.PRIO_PROCESS, pid, min(19, own + nice))
    except (OSError, ValueError):
        # Includes the process already having exited. Best-effort by design:
        # a session running at full priority is a slower machine, not a fault.
        log.debug("Could not renice session pid %s", pid, exc_info=True)


@dataclass
class _SpawnSpec:
    """What ``_prepare_spawn`` built, and the process ``_spawn_and_stream`` started."""

    cmd: list[str]
    prompt_text: str
    env: dict[str, str]
    working_dir: str | None
    scope_unit: str | None
    scoped: bool
    system_prompt_file: str | None
    api_key_file: str | None
    rules_file: str | None
    proc: asyncio.subprocess.Process | None = None
    adopt_task: asyncio.Task | None = None


class RebootResult(enum.Enum):
    """Outcome of ``Runner.request_reboot``.

    - ``QUEUED``: reboot accepted; will fire on idle (or be force-killed
      after ``REBOOT_DRAIN_TIMEOUT_SECS``).
    - ``DEFERRED``: another non-self task was active; ``reboot_request.deferred.json``
      written for diagnostics. Auto-promoted at the next idle session-end (within
      ``REBOOT_DEFERRED_TTL_SECS``); dropped after that.
    """
    QUEUED = "queued"
    DEFERRED = "deferred"


async def _wait_or_kill(proc: asyncio.subprocess.Process, grace: float = 5.0) -> None:
    """Give a process we already terminated ``grace`` seconds, then SIGKILL it."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


def _save_result_file(instance: Instance, text: str) -> None:
    """Write a run's final text to its result file and point the instance at it."""
    result_path = config.RESULTS_DIR / f"{instance.id}.md"
    result_path.write_text(text, encoding="utf-8")
    instance.result_file = str(result_path)


class ClaudeRunner(
    AccountsMixin,
    AdmissionMixin,
    PromptMixin,
    SessionFilesMixin,
    WorktreeMixin,
    MergeMixin,
    ReleaseMixin,
    RecoveryMixin,
):
    """Manages coding CLI subprocesses (Claude Code, Cursor, etc.)."""

    def __init__(self, store: "StateStore | None" = None) -> None:
        # When ``store`` is provided the runner persists/loads
        # ``_account_cooldowns`` through it so a reboot can't forget which
        # account is mid-cooldown — the bug that produced the t-3452 dementia.
        # Forward-declared via TYPE_CHECKING to keep the import edge clean
        # if state.py ever pulls in runner indirectly.
        self._store = store
        self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENT)
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        # Latest memory reading for each LIVE session, written by that session's
        # own watchdog every SESSION_MEM_CHECK_SECS. Shared state because the
        # question "which of the running sessions is the largest" cannot be
        # answered from inside any one of them, and that is precisely the
        # question a machine-wide crunch asks. Entries are dropped alongside
        # _processes so a finished session can never be picked as a victim.
        self._tree_samples: dict[str, memory.TreeMemory] = {}
        # Per-session cgroups, for the sessions that got one. Keyed the same
        # way as _processes and cleaned up in the same place, so a session can
        # never leave a stale path behind for a scope systemd has collected.
        self._session_cgroups: dict[str, cgroups.SessionCgroup] = {}
        # Serialises the reclaim sweep and rate-limits it: every session's
        # watchdog can reach it, and N sessions each walking the cgroup and
        # sampling CPU on a machine that is already thrashing would be its own
        # small denial of service.
        self._reclaim_lock = asyncio.Lock()
        self._reclaim_last = 0.0
        # When the last cross-session reap happened. A crunch does not clear
        # the instant a victim dies — the kernel takes time to reclaim, and the
        # next session's watchdog would read the same critical pressure and
        # reap itself too. Without this the whole fleet unwinds in one minute
        # over a spike that one kill already fixed.
        self._fleet_last_reap = 0.0
        # Usage pacing (admission.py), all keyed by the account a run was
        # admitted on (None without rotation): how many runs are past the
        # usage hold, the loop time one last went through it (the hold's
        # time limit runs from there, so a stalled line lets one through per
        # interval instead of all its waiters at once), and the held
        # sessions in arrival order. The reader is wired by app.py because
        # the figures live in bot.engine.plan_usage, which this package does
        # not import. None means no reading, so only the peak-hour cap.
        self._paced_running: Counter[str | None] = Counter()
        self._usage_hold_passed_at: dict[str | None, float] = {}
        self._usage_waiters: dict[str, str | None] = {}
        self._usage_reader: Callable[[str | None], Awaitable[object]] | None = None
        # Task-level tracking: covers the full lifecycle of a query/workflow,
        # not just the subprocess.  Prevents reboot from slipping through
        # gaps between autopilot chain steps.
        self._active_tasks: set[str] = set()
        self._active_sessions: dict[str, str] = {}  # session_id -> task_id
        self._active_channels: dict[str, str] = {}  # channel_id -> task_id
        self._idle_event = asyncio.Event()
        self._idle_event.set()  # starts idle

        # Intentional-kill tracking: ids of instances where we successfully
        # fired ``proc.terminate()`` via ``kill_and_wait`` (Kill button, Steer,
        # resolve-cancel, discard).
        # Populated only AFTER terminate returns without raising — so the set
        # always means "we successfully sent the kill signal," never the
        # weaker "we intended to."  Consumed in _stream_output after
        # proc.wait() to stamp RunResult.killed_intentionally so the
        # lifecycle layer can classify as KILLED instead of FAILED.
        self._intentional_kills: set[str] = set()
        # Parallel map populated alongside _intentional_kills: stamps the
        # user-facing reason ("kill" vs "steer") onto RunResult.kill_reason
        # so the lifecycle layer can route label + embed-delivery behavior
        # without conflating "kill button → no replacement coming" with
        # "steer → replacement run is already starting."
        self._kill_reasons: dict[str, str] = {}
        # Second parallel map, cleared at the same three points: the subset of
        # killed instances whose caller will rewrite the live progress message
        # itself (the Kill button edits the message it was attached to).  Kept
        # separate from _kill_reasons because it answers a different question
        # — "why did this stop" vs "who renders the card" — and folding the
        # two into one magic string is what let /kill and the Kill button
        # drift apart in the first place.
        self._kill_card_owners: set[str] = set()
        # Every instance whose process this bot has SIGKILLed or may yet:
        # kill(), intentional or not, since it escalates after a 5s grace, and
        # the memory reap, whose cgroup.kill is one. A -9 the bot sent itself
        # (shutdown's kill_all, a wizard teardown, a reap that stood down)
        # must never be read as the operating system's out-of-memory killer
        # and resumed. Paths that synthesise their own result (the lifetime
        # reap, the end-of-turn watchdog, AskUserQuestion) carry no exit code
        # to classify and are not marked, and a bare SIGTERM needs no mark
        # because it is only classified on the journal's word. Cleared in
        # run() for the next top-level run.
        self._bot_signalled: set[str] = set()

        # Reboot draining: set when a reboot is queued to block new spawns
        self._draining = False

        # Reboot coalescing: multiple instances can queue reboot requests,
        # but only one reboot executes after all tasks finish.
        self._reboot_requests: list[dict] = []
        self._reboot_executing = False
        self._on_idle_callback: Callable[[], Awaitable[None]] | None = None
        self._idle_loop: asyncio.AbstractEventLoop | None = None
        self._drain_timer_task: asyncio.Task | None = None

        # Side channel: classification of the most recent merge_branch
        # failure per instance. Populated inside the per-repo locked section
        # of merge_branch so callers can read it without racing a sibling
        # session's in-flight merge. Values are MERGE_FAIL_* constants.
        # See §3 of the t-4114 orphaned-index plan.
        self._last_merge_failure_kind: dict[str, str] = {}

        # Per-repo lock: serializes git-metadata-mutating operations
        # (worktree add/remove, merge, branch delete) to prevent lock file races
        self._repo_locks: dict[str, asyncio.Lock] = {}

        # Per-repo release lock: serializes release sessions (version pick +
        # tag create) so two worktrees can't race on the same version number.
        # Orthogonal to _repo_locks — release sessions don't block worktree
        # creation or merges, only other releases.
        self._release_locks: dict[str, asyncio.Lock] = {}

        # Multi-account failover: tracks when each account's usage limit resets.
        # Hydrated below from the persisted store so reboots don't forget which
        # account is on cooldown (root cause of t-3452 — fresh runner mistook
        # an exhausted primary for available, blew its --resume on a doomed
        # spawn, hit "No conversation found", then dropped session_id).
        self._account_cooldowns: dict[str, datetime] = {}
        if store is not None:
            try:
                persisted = store.get_account_cooldowns()
            except Exception:
                log.exception("Failed to load persisted account cooldowns")
                persisted = {}
            try:
                persisted_fps = store.get_account_cooldown_fps()
            except Exception:
                log.exception("Failed to load persisted cooldown fingerprints")
                persisted_fps = {}
            now = datetime.now(timezone.utc)
            for acct, iso in persisted.items():
                reset = parse_iso_utc(iso)
                if reset is None:
                    continue
                if reset > now:
                    self._account_cooldowns[acct] = reset
            # Then retire whichever of those no longer belong to the credential
            # sitting in that directory — see _drop_relogged_cooldowns.  Doing
            # it here covers a login that happened while the bot was down; the
            # call in _pick_account covers one that happens while it is up.
            self._drop_relogged_cooldowns(persisted_fps)

        # Model-specific cooldowns (account_dir -> reset): tracks when each
        # account's PRIMARY_MODEL quota (e.g. Fable 5) resets.  Separate from
        # _account_cooldowns — a model limit leaves the account usable for
        # other models, so it must never refuse spawns, only downgrade the
        # run to MODEL_FALLBACK until the reset passes.
        self._model_cooldowns: dict[str, datetime] = {}
        if store is not None:
            try:
                persisted_model = store.get_model_cooldowns()
            except Exception:
                log.exception("Failed to load persisted model cooldowns")
                persisted_model = {}
            now = datetime.now(timezone.utc)
            for acct, iso in persisted_model.items():
                reset = parse_iso_utc(iso)
                if reset is None:
                    continue
                if reset > now:
                    self._model_cooldowns[acct] = reset

        # Last-rebuild timestamp per (account_dir, project_dir) — dedupes the
        # auto-rebuild recovery layer so a flurry of failed resumes doesn't
        # re-scan the same project multiple times.
        self._rebuild_cache: dict[tuple[str, str], float] = {}

        # Last-hydrate timestamp per (account_dir, project_dir, session_id) —
        # caches the cross-account JSONL discovery scan so retries don't
        # re-walk every account's projects/ on each attempt.
        self._hydrate_cache: dict[tuple[str, str, str], float] = {}

        # Freshness-parse cache: (path_str, mtime_ns, size) -> last-turn datetime.
        # The `size` component is load-bearing: on filesystems with coarse
        # mtime granularity (SMB shares, FAT32) two appends within one mtime
        # tick still bust the cache because any append changes size. A future
        # "simplify cache key" refactor MUST keep size.
        self._freshness_cache: dict[tuple[str, int, int], datetime | None] = {}

        # account_dir -> reason, for accounts the credential preflight is
        # currently skipping. Held only to log transitions instead of
        # repeating the same line on every spawn.
        self._unusable_seen: dict[str, str] = {}
        # Whether the "preflight excluded everything" safety valve is currently
        # engaged. Same reason as above: on a host that keeps credentials
        # outside the config dir the valve holds forever, and logging it per
        # spawn would bury the log in a line that never changes.
        self._preflight_valve_open = False
        # Accounts whose *current* cooldown was applied for auth, not usage.
        # Kept apart because the two mean opposite things for recovery: a usage
        # cooldown ends on the clock, an auth one ends when someone logs in —
        # so a fresh login should clear it immediately. In-memory only, so a
        # restart loses the distinction; recovery after one rests entirely on
        # the credential fingerprint stored with the alert, which is why
        # ``_clear_auth_cooldown`` grew a ``force`` escape hatch for callers
        # holding that durable proof.
        self._auth_cooldowns: set[str] = set()
        # Accounts the *server* rejected this run, even though their
        # credentials file looks fine. Distinct from _unusable_seen (which the
        # on-disk probe produces) and load-bearing for "is there anywhere real
        # to fail over to?" — see _known_dead_accounts.
        self._auth_dead: set[str] = set()

    @property
    def provider(self) -> ProviderConfig:
        """Current provider — re-reads config for runtime switching."""
        return get_provider(config.PROVIDER)

    async def check_cli(self) -> str:
        """Verify CLI is available. Returns version string."""
        try:
            proc = await asyncio.create_subprocess_exec(
                config.CLAUDE_BINARY, "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **_NOWND,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            version = stdout.decode().strip()
            if not version:
                raise RuntimeError(f"{self.provider.name} CLI returned empty version")
            return version
        except FileNotFoundError:
            raise RuntimeError(
                f"{self.provider.name} CLI not found: {config.CLAUDE_BINARY}. "
                "Ensure it's installed and in PATH."
            )
        except asyncio.TimeoutError:
            raise RuntimeError(f"{self.provider.name} CLI --version timed out")

    async def run(
        self,
        instance: Instance,
        on_progress: ProgressCallback | None = None,
        on_stall: StallCallback | None = None,
        context: str | None = None,
        sibling_context: str | None = None,
        on_recovery: RecoveryCallback | None = None,
        on_context_reset: ContextResetCallback | None = None,
    ) -> RunResult:
        """Run CLI for an instance. Blocks until completion or timeout."""
        # Fresh failover state per top-level run. The set is mutated via .add()
        # during in-run failover; without this reset, a no-cooldown no-turns
        # failover in a prior run would silently keep an account excluded on
        # the next run of a reused Instance. Reset HERE, never in _run_impl
        # (recursion relies on the set persisting across failover hops within
        # a single run — the one in-run exception is the model-limit
        # downgrade, which clears it deliberately so the fallback-model
        # retry may revisit accounts that are only model-limited).
        instance._accounts_tried = set()
        # A kill mark belongs to the run it was fired at, and a top-level run
        # is by definition a new one.  The lifecycle layer registers the id in
        # _active_tasks just before calling this, which is what lets kill()
        # record intent while there is no live subprocess — so this reset is
        # what makes the check at the top of _run_impl safe to apply to the
        # FIRST attempt as well as to retries.  Without it, a mark stranded by
        # an earlier run (the 4h-lifetime early return skips the usual
        # discard) would stop a re-queued instance before it ever spawned.
        self._intentional_kills.discard(instance.id)
        self._kill_reasons.pop(instance.id, None)
        self._kill_card_owners.discard(instance.id)
        self._bot_signalled.discard(instance.id)
        # Usage first, and outside the concurrency slot: a session held for
        # its account's usage must not sit in one of the MAX_CONCURRENT slots,
        # or a backlog for a nearly spent account locks out work the other
        # account could run. Before memory too, because usage can hold for as
        # long as a running session takes to finish, and a memory reading
        # taken before that wait would be stale by the time it ended.
        paced_account = await self._await_usage_headroom(instance, on_progress)
        # Counted with no await between the hold's last check and here, so
        # two waiters cannot both see the same free slot. Counted from here,
        # through any wait for a concurrency slot, against the account it was
        # admitted on, even if recovery later moves it to another account.
        self._paced_running[paced_account] += 1
        try:
            async with self._semaphore:
                # Inside the slot, before the spawn. Holding the slot while
                # waiting is the point: this session has been admitted by
                # concurrency and is now being admitted by memory, and nothing
                # behind it in the queue should slip past into a machine that
                # is already out.
                await self._await_memory_headroom(instance, on_progress)
                return await self._run_impl(
                    instance, on_progress, on_stall, context, sibling_context,
                    api_fallback=instance.api_fallback,
                    on_recovery=on_recovery,
                    on_context_reset=on_context_reset,
                )
        finally:
            self._paced_running[paced_account] -= 1

    def _stopped_before_spawning(self, instance: Instance) -> RunResult | None:
        """The run was killed at a moment when there was no process to kill.

        A run is registered as active for its whole lifetime but only *holds a
        process* for part of it: not while it waits on the concurrency
        semaphore, not while its worktree is being built or an account picked,
        and not in the gap between one attempt ending and the next starting —
        a gap every recovery layer opens, for milliseconds on the recursive
        ones and a full 30 seconds on the transient-error sleep.  A Kill or
        Steer landing in any of those found nothing to terminate and simply
        evaporated, so the run carried on for another twenty minutes while the
        thread already said it had stopped.  ``kill()`` records the intent
        instead (see its docstring) and the callers of this honour it at every
        point a spawn is about to happen.

        Returns the tombstone to hand back, or None to carry on.  The mark is
        deliberately left set: several recovery layers inspect the result they
        get back and recurse *again* (account failover, model downgrade), so
        it has to keep refusing for the rest of this run.  ``run()`` clears it
        on the way into the next one, which is what makes it safe to check
        this before the first spawn as well as before a retry.
        """
        if instance.id not in self._intentional_kills:
            return None
        log.info(
            "Not spawning for %s — kill requested with no process to signal",
            instance.id,
        )
        return RunResult(
            is_error=True,
            error_message="Stopped before the next attempt started",
            session_id=instance.session_id,
            killed_intentionally=True,
            kill_reason=self._kill_reasons.get(instance.id),
            kill_owns_card=instance.id in self._kill_card_owners,
        )

    async def _run_impl(
        self,
        instance: Instance,
        on_progress: ProgressCallback | None,
        on_stall: StallCallback | None,
        context: str | None = None,
        sibling_context: str | None = None,
        api_fallback: bool = False,
        _provider: ProviderConfig | None = None,
        _binary: str | None = None,
        _recovery_state: set[str] | None = None,
        on_recovery: RecoveryCallback | None = None,
        on_context_reset: ContextResetCallback | None = None,
    ) -> RunResult:
        # Snapshot provider + binary at entry — in-flight sessions keep their
        # provider even if a runtime switch happens mid-run.
        # On recursive retries, the caller passes the original snapshot.
        provider = _provider or self.provider
        binary = _binary or config.CLAUDE_BINARY
        # Sentinel-pattern default — never use a mutable default arg.
        # Tracks recovery strategies already attempted on this run so each
        # fires at most once (bounds recursion depth from the layered
        # No-conversation-found recovery added by the dementia fix).
        recovery_state: set[str] = _recovery_state if _recovery_state is not None else set()
        run = _RunAttempt(
            self, instance, on_progress, on_stall, context, sibling_context,
            api_fallback, provider, binary, recovery_state,
            on_recovery, on_context_reset,
        )
        prepared = await self._prepare_spawn(run)
        if isinstance(prepared, RunResult):
            return prepared
        spawn = prepared
        try:
            result, streamed = await self._spawn_and_stream(run, spawn)
            if not streamed:
                return result

            # Nothing below this line should fire for a run the user stopped
            # on purpose.  Every branch that follows exists to rescue a run
            # that fell over by itself, and each one ends in another spawn —
            # so a Kill or a Steer would be answered by silently starting the
            # work again.  The account-failover branch is the sharpest edge:
            # a terminated process leaves no output and no completed turns,
            # which is exactly its "this account fell over instantly"
            # signature, so on a two-account setup killing a build handed it
            # straight to the backup account.
            if result.killed_intentionally:
                return result

            # The operating system killed this run for memory. Classified
            # here, before error_text is read, so every branch below sees the
            # stable wording rather than "Exit code -9": the account-failover
            # heuristic in particular, which would otherwise read a session
            # killed before its first turn as the account falling over.
            if spawn.scoped and instance.id not in self._bot_signalled:
                await _classify_oom_kill(result, spawn.scope_unit)
            self._bot_signalled.discard(instance.id)

            # Every rescue below is a step in recovery.py, tried in the order
            # _RECOVERY_STEPS gives. The first that answers ends the run.
            run.result = result
            run.error_text = result.error_message or result.result_text or ""
            for step in _RECOVERY_STEPS:
                handled = await getattr(self, step)(run)
                if handled is not None:
                    return handled
            return result

        except Exception as e:
            log.exception("Error running %s", instance.id)
            return RunResult(is_error=True, error_message=str(e))
        finally:
            # Clean up temp files on all exit paths
            for tmp in (spawn.system_prompt_file, spawn.api_key_file):
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            # Clean up Cursor rules file if created
            if spawn.rules_file:
                try:
                    os.unlink(spawn.rules_file)
                except OSError:
                    pass
            # Cancelled before the pops, so a still-waiting adoption cannot
            # write an entry back in behind them and leak it for the life of
            # the process.
            if spawn.adopt_task is not None and not spawn.adopt_task.done():
                spawn.adopt_task.cancel()
            self._processes.pop(instance.id, None)
            self._tree_samples.pop(instance.id, None)
            self._session_cgroups.pop(instance.id, None)
            # Kill process on cancellation/unexpected error to avoid orphans
            if spawn.proc is not None and spawn.proc.returncode is None:
                try:
                    spawn.proc.kill()
                except ProcessLookupError:
                    pass
            if not self._active_tasks and not self._processes:
                self._idle_event.set()

    async def _prepare_spawn(self, run: _RunAttempt) -> RunResult | _SpawnSpec:
        """Everything before the spawn: worktree, account, model, command, env, scope.

        Returns a RunResult when the run must not spawn at all (stopped, or
        every account refused), otherwise the spec ``_spawn_and_stream`` runs.
        Records the picked account and any model downgrade on ``run``, where
        the recovery steps read them.
        """
        instance = run.instance
        provider = run.provider
        recovery_state = run.recovery_state
        on_progress = run.on_progress
        api_fallback = run.api_fallback

        # Waiting on the semaphore, and the gap between one attempt ending and
        # the next starting, are both windows with no process to signal.
        stopped = self._stopped_before_spawning(instance)
        if stopped:
            return stopped

        # Git worktree isolation for build tasks
        if instance.branch:
            await self._ensure_worktree(instance, provider=provider)

        # Use worktree as cwd if available (file isolation)
        working_dir = instance.worktree_path or instance.repo_path or None

        # Multi-account failover (Claude-only: other providers use single account).
        # Honor per-instance session ownership via `prefer`: if the instance
        # has a session_account stamped from a prior successful run, route to
        # that account first so --resume actually finds the session JSONL.
        #
        # Picked BEFORE _build_command so the refuse-to-spawn short-circuit
        # below can return without leaking the temp files _build_command
        # writes (system-prompt, api-key, cursor-rules) — those only get
        # cleaned up in the spawn try/finally block further down.
        account_dir: str | None = None
        if provider.supports_account_failover:
            account_dir = self._spawn_account(instance)
            if not account_dir and config.CLAUDE_ACCOUNTS:
                # A kill that landed while the worktree was being built (the
                # longest of the no-process windows — it takes a per-repo git
                # lock, so it can queue behind a sibling build's merge) would
                # otherwise come back as the re-queueable result below and run
                # hours later.  Every other pre-spawn window is caught after
                # the process exists, but this branch never gets that far.
                stopped = self._stopped_before_spawning(instance)
                if stopped:
                    return stopped
                # Refuse to spawn when every configured account is on cooldown
                # or already excluded.  Spawning anyway would invoke the CLI
                # without CLAUDE_CONFIG_DIR set, which (a) routes the call to
                # whichever account `claude` defaults to — usually the wrong
                # one, and (b) means hydration ran for *some* account_dir but
                # the actual subprocess used a different one, so --resume
                # collapses with "No conversation found", Layer 3 fires, and
                # session_id gets dropped.  This was the t-3452 root cause.
                #
                # Synthesize a usage-limit RunResult instead so the cooldown
                # retry path in app.py can re-queue the instance with its
                # session_id intact.  Session_id is preserved on the result so
                # finalize_run() does NOT clobber instance.session_id with None.
                logged_out = self._known_dead_accounts()
                earliest_reset, retry_reason = self._refusal_retry_plan(
                    logged_out
                )
                labels = ", ".join(sorted(account_label(a) for a in logged_out))
                log.warning(
                    "Refusing spawn for %s: all %d account(s) on cooldown or "
                    "excluded (logged out: %s)",
                    instance.id, len(config.CLAUDE_ACCOUNTS),
                    labels or "none",
                )
                if logged_out:
                    rest = (
                        "the rest are on cooldown"
                        if retry_reason == "backup_logged_out"
                        else "and nothing else is available"
                    )
                    message = (
                        f"No account is available right now — {labels} "
                        f"logged out, {rest}."
                    )
                else:
                    message = (
                        "All configured Claude accounts are on cooldown "
                        "(usage limit or unavailable)."
                    )
                return RunResult(
                    is_error=True,
                    error_message=message,
                    result_text="",
                    session_id=instance.session_id,
                    usage_limit_reset=earliest_reset,
                    retry_reason=retry_reason,
                )

        # Pre-emptive model downgrade: when the chosen account's primary
        # model (e.g. Fable) is on its own limit cooldown, don't burn a spawn
        # on a doomed attempt — go straight to the fallback model on the
        # subscription.  Expiry is automatic: _purge_model_cooldowns drops
        # elapsed entries, so the next run is back on the primary without
        # any manual action.  Never applies to PPU runs (they pin their own
        # model) or to explicit non-primary overrides like EXPLORE_MODEL.
        model_override: str | None = None
        if (
            account_dir
            and not api_fallback
            and _is_primary_model(instance.model)
        ):
            self._purge_model_cooldowns()
            m_reset = self._model_cooldowns.get(account_dir)
            if m_reset:
                model_override = config.MODEL_FALLBACK
                log.info(
                    "Primary model on cooldown for %s until %s — using %s for %s",
                    account_dir[-20:], m_reset.isoformat(),
                    model_override, instance.id,
                )
                if on_progress and "model_downgrade_notice" not in recovery_state:
                    recovery_state.add("model_downgrade_notice")
                    await _notify(
                        on_progress,
                        f"Running on {model_override}",
                        f"{config.PRIMARY_MODEL.title()} limit until "
                        f"~{m_reset.strftime('%H:%M')} UTC — "
                        "switching back automatically",
                        "during model downgrade",
                    )

        # Run the whole prompt-build off the event loop: _build_command makes
        # several synchronous `git` subprocess calls via _build_master_context_block
        # (invoked up to twice on the resume path) plus file reads/writes.  On the
        # loop, a burst of spawns (mass-tapping Build/Review) blocks it past
        # Discord's 3s interaction-ack window, surfacing as "interaction failed"
        # on otherwise-healthy clicks.  The instance is owned by this coroutine
        # under the channel lock here, so the in-thread mutation of instance
        # fields is race-free.
        cmd, prompt_text, system_prompt_file, api_key_file, rules_file = (
            await asyncio.to_thread(
                self._build_command,
                instance, run.context, run.sibling_context,
                api_fallback=api_fallback, provider=provider, binary=run.binary,
                model_override=model_override,
            )
        )

        # Strip provider-specific env vars to prevent nested-session errors.
        # Strip ANTHROPIC_API_KEY on non-PPU runs so the CLI can never
        # silently bill via API; PPU runs keep it as backup for apiKeyHelper.
        env = {**os.environ}
        for var in provider.nested_env_vars:
            env.pop(var, None)
        if not api_fallback:
            env.pop("ANTHROPIC_API_KEY", None)
        # The bot's own Discord identity has no business in a session's
        # environment: nothing a session legitimately does needs it, and `env`
        # is something a debugging turn prints without thinking. Deliberately a
        # denylist, not an allowlist -- an allowlist drops whatever per-project
        # variable we failed to predict (toolchain homes, proxy settings,
        # SSH_AUTH_SOCK, GH_TOKEN) and fails confusingly mid-chain.
        #
        # This is NOT a containment boundary. `.env` is still readable on disk
        # and config re-reads it with override=True, so the bot's own scripts
        # keep working. What it removes is the accidental path: an env dump, a
        # crash trace, a logged subprocess environment.
        #
        # That "keep working" depends on config._env_root() finding the main
        # checkout's `.env` from inside a worktree, since `.env` is gitignored
        # and worktrees have none. Removing that lookup would break every
        # harness a build session runs on this repo -- the two changes are one
        # unit, so keep them together.
        for var in SESSION_STRIPPED_ENV_VARS:
            env.pop(var, None)
        if account_dir:
            env[provider.config_dir_env] = account_dir

        # Hydrate session JSONL into the chosen account's project dir before
        # spawn.  Searches every configured account for the session file and
        # copies it into place if found elsewhere — makes either subscription
        # able to resume any session on demand, including worktree builds
        # whose owning account is on cooldown.
        if account_dir and instance.session_id and working_dir:
            await self._hydrate_session_for_account(
                account_dir, working_dir, instance.session_id, instance,
            )

        # Put the session in its own systemd scope, so that when oomd comes
        # looking for something to kill it finds this session and not the
        # supervisor holding twelve conversations open. Returns `cmd`
        # unchanged wherever that is not possible -- a probed, cached answer,
        # so a machine without systemd-run pays for the discovery once and
        # then spawns exactly as it always did.
        await cgroups.ensure_scope_support()
        # A fresh name per spawn, not per instance: this instance may already
        # have spawned once this run, and that attempt's scope can still be
        # loaded. Adoption must look for this same name.
        scope_unit = cgroups.new_scope_unit(instance.id)
        wrapped = cgroups.wrap_command(cmd, instance.id, unit=scope_unit)
        # Only a scoped run can be told apart from the supervisor when oomd
        # comes looking, so only a scoped run is classified as OOM-killed.
        scoped = wrapped != cmd
        cmd = wrapped

        acct_tag = f" [acct={account_dir[-20:]}]" if account_dir else ""
        log.info("Running %s%s (prompt: %d chars via stdin): %s",
                 instance.id, acct_tag, len(prompt_text), " ".join(cmd)[:500])

        run.account_dir = account_dir
        run.model_override = model_override
        return _SpawnSpec(
            cmd=cmd, prompt_text=prompt_text, env=env, working_dir=working_dir,
            scope_unit=scope_unit, scoped=scoped,
            system_prompt_file=system_prompt_file, api_key_file=api_key_file,
            rules_file=rules_file,
        )

    async def _spawn_and_stream(
        self, run: _RunAttempt, spawn: _SpawnSpec,
    ) -> tuple[RunResult, bool]:
        """Spawn the CLI, feed it the prompt and stream it to its result.

        The bool is False when the run ended before it streamed (stopped
        after the spawn, or the prompt could not be written), so recovery has
        nothing to look at. ``spawn.proc`` and ``spawn.adopt_task`` are set as
        soon as they exist, for ``_run_impl``'s cleanup.
        """
        instance = run.instance
        account_dir = run.account_dir
        proc = await asyncio.create_subprocess_exec(
            *spawn.cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=spawn.working_dir,
            env=spawn.env,
            limit=10 * 1024 * 1024,  # 10MB line buffer (default 64KB too small for stream-json)
            **_NOWND,
        )
        spawn.proc = proc
        # Register immediately so kill/cleanup works even if stdin write fails
        instance.pid = proc.pid
        self._processes[instance.id] = proc
        # No await between the spawn and here, so this is the earliest the
        # child can be reniced from the parent. See _lower_priority for
        # why it is not done in a preexec_fn.
        _lower_priority(proc.pid)

        # Closes the last of the no-process windows: spawning is itself an
        # await, so a kill can land after the checks above and still find
        # nothing to signal.  Checked here rather than before the spawn
        # because this is the first moment the answer can't change — and
        # before _stream_output, which clears stale marks on the way in.
        # The finally below SIGKILLs the process we just started.
        stopped = self._stopped_before_spawning(instance)
        if stopped:
            return stopped, False

        # Pipe user prompt via stdin — avoids Windows command-line length
        # limit (WinError 206) for long prompts / system context.
        try:
            proc.stdin.write(spawn.prompt_text.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            log.error("stdin pipe failed for %s: %s", instance.id, exc)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return RunResult(
                is_error=True, error_message=f"Failed to send prompt: {exc}",
            ), False

        # In the background, and deliberately not awaited here.
        # `--scope` execs in place, so proc.pid is the CLI's own pid and
        # the cgroup it reports is the scope's -- but only once
        # systemd-run's round trip to the service manager has finished,
        # and that round trip is not quick when the manager is busy:
        # measured on this machine at 1.1s, 1.8s and 4.8s across three
        # consecutive trials, against ~50ms on an idle one. So the budget
        # has to be sized for the bad case, and a budget sized for the bad
        # case must not be able to stall a spawn. It buys insurance rather
        # than latency -- systemd-run registers before it execs, so the
        # CLI has nothing to say until the scope exists either way -- but
        # nothing downstream needs adoption to have finished, and every
        # reader of _session_cgroups already treats a missing entry as
        # "walk the process tree instead".
        spawn.adopt_task = asyncio.create_task(
            self._adopt_session_scope(instance.id, proc, spawn.scope_unit)
        )

        result = await self._stream_output(
            proc, instance, run.on_progress, run.on_stall,
            supports_live_usage=run.provider.supports_live_usage,
            account_dir=account_dir,
        )

        # Tag result now — before any early returns — so all exit paths carry the flag.
        result.api_fallback_used = run.api_fallback

        # Stamp session ownership: on a successful run, record which
        # account_dir hosted the session so future resumes prefer it.
        # Pairs with `prefer=instance.session_account` in _pick_account
        # to keep the picker from sending --resume to the wrong home.
        if (
            not result.is_error
            and account_dir
            and (result.session_id or instance.session_id)
        ):
            instance.session_account = account_dir

        # A run that actually completed proves the account authenticates,
        # which is the only reliable all-clear for a runtime 401 sideline
        # (the credentials file looked fine both before and after).
        #
        # Deliberately NOT forced.  A success says "this account can log
        # in"; it says nothing about whether it has usage left, and the
        # forcing path pops the cooldown whatever kind it is.  This bot
        # runs tasks in parallel, so by the time a run finishes a sibling
        # may have just cooled this same account on a 5h limit — force
        # would wipe that and send the next spawn straight into the limit.
        # Nothing is lost by asking: the picker never hands out a cooled
        # account, so a run reaching here can only be carrying a cooldown
        # that arrived mid-flight, i.e. exactly the one to keep.
        if not result.is_error and account_dir:
            self._clear_auth_cooldown(account_dir)
            if self._store:
                try:
                    self._store.resolve_account_alert(account_dir)
                except Exception:
                    log.debug(
                        "Failed to resolve account alert", exc_info=True,
                    )
        return result, True

    async def _adopt_session_scope(
        self, instance_id: str, proc: asyncio.subprocess.Process,
        unit: str | None = None,
    ) -> None:
        """Adopt a session's scope once systemd has registered it.

        The ceilings themselves were set on the ``systemd-run`` command line;
        adoption records the cgroup for accounting and ``cgroup.kill``, and
        reads the ceilings back for the log.

        Runs as its own task so the wait never delays reading the CLI's
        output. Nothing downstream requires it to have finished: the kill
        path and the memory guard both fall back to walking the process
        tree when there is no entry, which is what they did before scopes
        existed.
        """
        try:
            session_cg = await cgroups.adopt_session(
                proc.pid, instance_id, unit=unit,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A resource refinement must never cost a session its run.
            log.debug("scope adoption failed for %s: %s", instance_id, exc)
            return
        if session_cg is None:
            return
        # The run can finish while we wait, and its cleanup has then already
        # been past the pop. Storing now would leak the entry.
        if self._processes.get(instance_id) is not proc:
            return
        self._session_cgroups[instance_id] = session_cg
        if session_cg.applied:
            log.debug(
                "%s in scope %s (%s)", instance_id, session_cg.unit,
                ", ".join(session_cg.applied),
            )

    async def _reap_session(
        self, instance: Instance, proc, on_progress, headline: str, detail: str,
    ) -> None:
        """Tell the session why, then destroy its whole process tree.

        Told BEFORE the reap, not after, and that ordering is
        load-bearing. Reaping closes the CLI's stdout, which ends the
        reader loop, whose ``finally`` cancels the watchdog — and it
        will usually do so while we are still parked inside kill_tree's
        grace wait for a multi-gigabyte process to actually die.
        Anything after that call is on a coin flip, so the one message
        explaining the kill goes out first. The decision is already
        irrevocable by the time this is called.
        """
        await _notify(on_progress, headline, detail, "on memory kill")
        # cgroup.kill is a SIGKILL, and this reap can still fall
        # through to the normal exit path when it stands down. A -9
        # there with no mark would be read as the operating system's
        # OOM killer and resumed.
        self._bot_signalled.add(instance.id)
        signalled: list[str] = []
        # cgroup.kill first where the session has its own cgroup: it
        # is atomic where kill_tree is a walk, so nothing can fork out
        # from under it, and it reaches a process that reparented away
        # and left the tree entirely. kill_tree still runs afterwards
        # -- it is what produces the list of what was signalled for
        # the log, and it is the whole mechanism on a kernel with no
        # cgroup.kill (pre-5.14) or when the scope never happened.
        session_cg = self._session_cgroups.get(instance.id)
        if session_cg is not None:
            try:
                if await asyncio.to_thread(session_cg.kill):
                    log.warning(
                        "Killed cgroup %s for %s",
                        session_cg.unit, instance.id,
                    )
            except Exception:
                log.debug(
                    "cgroup.kill failed for %s", instance.id,
                    exc_info=True,
                )
        try:
            signalled = await asyncio.to_thread(
                memory.kill_tree, proc.pid,
            )
            if signalled:
                log.warning(
                    "Reaped for %s: %s",
                    instance.id, ", ".join(signalled),
                )
        except Exception:
            log.exception("Memory reap failed for %s", instance.id)
        if not signalled:
            # kill_tree swallows per-process failures and returns an
            # empty list rather than raising, so "nothing was
            # signalled" is a silent outcome — and one that must not be
            # silent HERE. We have already decided this run is over:
            # leaving the process alive would let it stream to
            # completion and then be reported as a memory failure it
            # recovered from, which is a lie in whichever direction the
            # run happened to end. Reached only when the tree is still
            # standing, so stdout has not closed, so the watchdog has
            # not been cancelled — the one case where code after the
            # reap is guaranteed to run is also the one case that needs
            # it.
            log.warning(
                "Memory reap signalled nothing for %s \u2014 "
                "terminating the CLI directly",
                instance.id,
            )
            proc.terminate()

    async def _stream_output(
        self,
        proc: asyncio.subprocess.Process,
        instance: Instance,
        on_progress: ProgressCallback | None,
        on_stall: StallCallback | None,
        *,
        supports_live_usage: bool = False,
        account_dir: str | None = None,
    ) -> RunResult:
        """Read stdout line-by-line, parse stream-json, detect stalls.

        No inactivity timeout — processes run until they finish or the user
        kills them.  A safety-net catches truly orphaned processes: past
        MAX_PROCESS_LIFETIME_SECS (default 4h) of age AND
        MAX_PROCESS_SILENCE_SECS (default 30m) of producing nothing, with
        MAX_PROCESS_HARD_LIFETIME_SECS (default 24h) as an age-only backstop.
        Age alone never kills; see ``_Watchdog._check_lifetime`` for why.
        """
        # Defensive: clear any stale intentional-kill marker from a prior
        # run on the same instance id (e.g. a previous lifetime-exceeded
        # early-return below skipped the consumer-side discard). Without
        # this, a stale marker could cause a future genuinely-failed run
        # to be misclassified as KILLED on Windows where any non-zero rc
        # satisfies the kill-shape filter.
        self._intentional_kills.discard(instance.id)
        self._kill_reasons.pop(instance.id, None)
        self._kill_card_owners.discard(instance.id)

        events: list[dict] = []
        captured_session_id: str | None = None
        ask_question_input: dict | None = None  # Set when AskUserQuestion detected
        # Path-poisoning hits observed during streaming (Edit/Write/MultiEdit/
        # NotebookEdit targeting the main repo instead of the worktree). The
        # worktree-guard hook should be blocking these at the syscall level,
        # but we still record them so the post-run scan can surface a clear
        # message to the user — and so we have telemetry if the hook ever
        # fails to load.
        poisoning_hits: list[str] = []
        _poisoning_seen: set[str] = set()
        _wt_path = (instance.worktree_path or "")
        _repo_path = (instance.repo_path or "")

        def _record_poisoning(event: dict, *, warn: bool) -> None:
            if not (_wt_path and _repo_path):
                return
            for hit in detect_path_poisoning(event, _wt_path, _repo_path):
                if hit not in _poisoning_seen:
                    _poisoning_seen.add(hit)
                    poisoning_hits.append(hit)
                    if warn:
                        log.warning(
                            "Path-poisoning hit for %s: %s (worktree=%s, repo=%s)",
                            instance.id, hit, _wt_path, _repo_path,
                        )

        watchdog = _Watchdog(self, instance, proc, events, on_progress, on_stall)
        # End-of-turn watchdog: set by the reader loop once the turn has been
        # silent past END_OF_TURN_GRACE_SECS after a genuine end_turn.
        end_of_turn_done = False
        stall_check_task = asyncio.create_task(watchdog.run())

        try:
            assert proc.stdout is not None
            while True:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=5
                    )
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        break
                    if watchdog.end_of_turn_seen:
                        idle = asyncio.get_event_loop().time() - watchdog.last_output_time
                        if idle > config.END_OF_TURN_GRACE_SECS:
                            recovered_text = last_assistant_text(events)
                            log.warning(
                                "End-of-turn watchdog: %s idle %ds after end_turn "
                                "(session=%s, recovered_text=%d chars) — terminating",
                                instance.id, int(idle),
                                captured_session_id or "?", len(recovered_text),
                            )
                            end_of_turn_done = True
                            proc.terminate()
                            break
                    continue
                except (ValueError, asyncio.LimitOverrunError):
                    log.warning("Oversized line from %s (>10MB), skipping", instance.id)
                    proc.terminate()
                    break

                if not line:
                    break

                watchdog.last_output_time = asyncio.get_event_loop().time()
                watchdog.stall_warned = False

                decoded = line.decode("utf-8", errors="replace")
                event = parse_stream_line(decoded)
                if event:
                    events.append(event)
                    # Eagerly capture session_id so it survives early termination
                    if not captured_session_id and event.get("session_id"):
                        captured_session_id = event["session_id"]
                    # End-of-turn watchdog detection: arm only when this
                    # assistant turn ends with stop_reason="end_turn" AND
                    # no tool_use blocks (nothing left for the CLI to do).
                    if event.get("type") == "assistant":
                        _msg = event.get("message") or event
                        if isinstance(_msg, dict) and _msg.get("stop_reason") == "end_turn":
                            _content = _msg.get("content") or event.get("content") or []
                            _has_tool = any(
                                isinstance(b, dict) and b.get("type") == "tool_use"
                                for b in (_content if isinstance(_content, list) else [])
                            )
                            watchdog.end_of_turn_seen = not _has_tool
                        else:
                            watchdog.end_of_turn_seen = False
                    elif event.get("type") == "result":
                        # `result` is Claude CLI's terminal event — nothing
                        # follows. If the subprocess fails to close stdout
                        # (orphan child holding the pipe, etc.), arm the
                        # watchdog so the post-result grace fires a forced
                        # terminate instead of hanging indefinitely.
                        watchdog.end_of_turn_seen = True
                    # Per-turn usage (Claude only) — fed to progress callback
                    # so the lifecycle layer can keep the context footer live.
                    usage = extract_usage(event) if supports_live_usage else None

                    if on_progress:
                        progress = extract_progress(event)
                        if progress and progress.message:
                            try:
                                if usage is not None:
                                    await on_progress(
                                        progress.message, progress.detail,
                                        usage=usage,
                                    )
                                else:
                                    await on_progress(progress.message, progress.detail)
                            except Exception:
                                log.exception("Progress callback error")
                        elif usage is not None:
                            # No visible progress, but refresh the cached usage
                            # so the heartbeat renders the latest context count.
                            try:
                                await on_progress("", "", usage=usage)
                            except Exception:
                                log.exception("Progress callback error (usage-only)")

                    # Path-poisoning tripwire: record any tool_use that
                    # targets the main repo path instead of the worktree.
                    # Records-only — the worktree-guard hook is the actual
                    # block; this is an observer for the post-run summary.
                    _record_poisoning(event, warn=True)

                    # Detect AskUserQuestion — Claude is blocking on stdin
                    if ask_question_input is None:
                        for tool_name, tool_input in iter_tool_blocks(event):
                            if tool_name == "AskUserQuestion":
                                ask_question_input = (
                                    tool_input if isinstance(tool_input, dict) else {}
                                )
                                log.warning(
                                    "AskUserQuestion detected for %s: %.150s",
                                    instance.id,
                                    format_ask_question(ask_question_input),
                                )
                                proc.terminate()
                                break
                        if ask_question_input is not None:
                            break
        finally:
            if stall_check_task:
                stall_check_task.cancel()
                try:
                    await stall_check_task
                except asyncio.CancelledError:
                    pass

        # Both early exits below terminated the CLI themselves, so they give it
        # a grace period instead of the bare wait the main path uses, and they
        # run the same post-exit housekeeping before returning.
        if ask_question_input is not None or end_of_turn_done:
            await _wait_or_kill(proc)
            await self._after_exit_housekeeping(instance, account_dir)

        # AskUserQuestion: return the question as the result
        if ask_question_input is not None:
            result = extract_result(events)
            result.needs_input = True
            result.is_error = False
            if poisoning_hits:
                result.path_poisoning = list(poisoning_hits)
            # The questions + options live in the tool call, not the assistant
            # text — append them to whatever preamble Claude wrote so the user
            # sees both the wind-up and the actual choices.
            formatted_q = format_ask_question(ask_question_input)
            if formatted_q and result.result_text:
                result.result_text = result.result_text.rstrip() + "\n\n" + formatted_q
            elif formatted_q:
                result.result_text = formatted_q
            elif not result.result_text:
                result.result_text = "Claude is asking a question (text not captured)"
            if not result.session_id:
                result.session_id = captured_session_id
            if result.result_text:
                _save_result_file(instance, result.result_text)
                instance.summary = extract_summary(result.result_text)
            return result

        # End-of-turn watchdog tripped: the CLI hung after stop_reason=end_turn.
        # Synthesise a successful result from the events we captured.
        if end_of_turn_done:
            result = extract_result(events)
            result.is_error = False
            result.error_message = None
            if not result.session_id:
                result.session_id = captured_session_id
            if not result.result_text:
                result.result_text = last_assistant_text(events) or ""
            if poisoning_hits:
                result.path_poisoning = list(poisoning_hits)
            if result.result_text:
                _save_result_file(instance, result.result_text)
                instance.summary = extract_summary(result.result_text)
            return result

        await proc.wait()

        # Post-exit worktree housekeeping, placed here rather than in the tail
        # because the tail is not on every path out of this method: the lifetime
        # cap and the memory reap both return before it. It has to happen before
        # the last-assistant-uuid lookup further down, which reads the JSONL out
        # of the MAIN repo's project dir and so only finds it after the
        # back-copy.
        await self._after_exit_housekeeping(instance, account_dir)

        # Both watchdog reaps stand down for the same two races. Guarded as one
        # block so the O(n) event walk below stays lazy on the ordinary path,
        # where neither watchdog fired.
        if watchdog.memory_kill is not None or watchdog.lifetime_exceeded:
            reap_name = "Memory reap" if watchdog.memory_kill is not None else "Orphan reap"
            reap_detail = (
                watchdog.memory_kill.summary() if watchdog.memory_kill is not None
                else f"silent {int(watchdog.lifetime_silence_secs)}s"
            )

            # A reap and a finishing turn can land in the same instant: the
            # watchdog decides on its own cadence, and while its SIGTERMs go out
            # the CLI can still flush the `result` event that says the turn
            # completed. Reporting the reap then would mark a finished run as
            # failed AND auto-resume it, redoing work whose output is already
            # captured and whose edits are already on disk. The kill is not lost
            # information either way — it was logged the moment it fired.
            if _turn_completed_successfully(events):
                log.warning(
                    "%s for %s raced a completed turn — reporting the "
                    "completion instead (%s)", reap_name, instance.id, reap_detail,
                )
                watchdog.stand_down()

            # The other race worth standing down for: the user asked this session
            # to stop (Steer, /kill) inside the same few seconds the watchdog
            # chose to reap it. Reporting the reap would override an explicit
            # instruction — the run is marked FAILED, then auto-resumed, so the
            # session carries on after the person who stopped it has been told it
            # stopped. Their intent wins, and falling through hands the
            # classification to the normal path, which renders the quiet KILLED
            # tombstone it asked for.
            #
            # (The bookkeeping half of this is already covered: the defensive
            # discard at the top of this method clears a marker an early return
            # skipped, so the concern here is the wrong outcome for THIS run, not
            # a stale flag poisoning the next one.)
            elif instance.id in self._intentional_kills:
                log.warning(
                    "%s for %s raced a requested stop — honouring the stop (%s)",
                    reap_name, instance.id, reap_detail,
                )
                watchdog.stand_down()

        if watchdog.memory_kill is not None:
            return _memory_kill_result(
                events, watchdog.memory_kill, watchdog.memory_kill_avail_mb,
                captured_session_id, poisoning_hits,
                pressure=watchdog.memory_kill_pressure,
            )

        if watchdog.lifetime_exceeded:
            return _lifetime_kill_result(
                events, captured_session_id, poisoning_hits,
                elapsed_secs=watchdog.lifetime_elapsed_secs,
                silence_secs=watchdog.lifetime_silence_secs,
                hard_cap=watchdog.lifetime_hard_cap,
            )

        # Capture stderr for error info
        stderr_text = ""
        if proc.stderr:
            stderr_data = await proc.stderr.read()
            stderr_text = stderr_data.decode("utf-8", errors="replace").strip()

        result = extract_result(events)
        # Final reconciliation pass: walk every event so anything that
        # slipped past the stream-time tripwire (e.g. a content_block_start
        # that arrived in a half-flushed line) is still surfaced.
        for ev in events:
            _record_poisoning(ev, warn=False)
        if poisoning_hits:
            result.path_poisoning = list(poisoning_hits)

        result.exit_code = proc.returncode
        # A turn whose `result` event already reported success and whose
        # process was then signalled finished its work: the signal landed on
        # a CLI that was only exiting. That is exactly the case a reap stands
        # down for above ("raced a completed turn"), and it is also an oomd
        # kill in the same instant. Failing it here would undo the stand-down
        # and, for a SIGKILL, have the out-of-memory recovery resume a turn
        # that is already done. A user's Kill or Steer is left to the normal
        # path, which renders the stop they asked for. Not on Windows, where
        # every non-zero code is kill-shaped and a real exit-1 crash would be
        # hidden.
        completed_then_signalled = (
            proc.returncode != 0
            and os.name != "nt"
            and is_kill_shape(proc.returncode)
            and _turn_completed_successfully(events)
            and instance.id not in self._intentional_kills
        )
        if completed_then_signalled:
            log.warning(
                "%s exited %s after its turn completed; keeping the completion",
                instance.id, proc.returncode,
            )
        elif proc.returncode != 0 and not result.is_error:
            result.is_error = True
            if not result.error_message:
                result.error_message = stderr_text or f"Exit code {proc.returncode}"

        # Keep the conversation resumable after a failure.  extract_result only
        # sees a session_id if the CLI got as far as emitting a `result` event
        # — a process killed mid-run (autocompact thrash, crash, watchdog) often
        # doesn't, so without this a FRESH spawn that died after 50 turns of
        # real work reports no session at all, and both the auto-resume below
        # and the user's Retry button start from scratch instead of resuming.
        # The init event carries the id from turn 1, so captured_session_id is
        # the answer; the AskUserQuestion and end-of-turn paths above already
        # do exactly this.  finalize_run only overwrites instance.session_id
        # when the result carries one, so this can only ever add information.
        if not result.session_id:
            result.session_id = captured_session_id

        # Classify intentional kills (Steer / resolve-cancel) so the
        # lifecycle layer can render a quiet KILLED tombstone instead of
        # a red FAILED embed.  Wrapped in try/finally
        # to guarantee the set entry is discarded even on downstream
        # exceptions — prevents leaks across the runner's uptime.
        was_intentional = instance.id in self._intentional_kills
        try:
            if was_intentional and result.is_error:
                # Require a kill-shape returncode in addition to the flag, so a
                # process that genuinely crashed in the same window we asked it
                # to terminate isn't reclassified as KILLED — see
                # ``is_kill_shape`` for which shapes count and why Windows
                # can't be told apart.  The mitigation for that Windows blind
                # spot is that finalize_run preserves the original
                # error_message on the Instance even when classified as
                # KILLED, so /log + history still surface a real crash that
                # coincided with a Steer.
                if is_kill_shape(proc.returncode):
                    result.killed_intentionally = True
                    result.kill_reason = self._kill_reasons.get(instance.id)
                    result.kill_owns_card = instance.id in self._kill_card_owners
        finally:
            self._intentional_kills.discard(instance.id)
            self._kill_reasons.pop(instance.id, None)
            self._kill_card_owners.discard(instance.id)

        if result.is_error:
            # Log raw events for debugging
            event_dump = _json_mod.dumps(events[-5:], default=str)[:1000] if events else "no events"
            log.warning(
                "CLI error for %s (exit=%s): %s | stderr: %s | last events: %s",
                instance.id, proc.returncode,
                result.error_message or result.result_text or "no message",
                stderr_text[:500] if stderr_text else "empty",
                event_dump,
            )

        # Save result file
        if result.result_text:
            _save_result_file(instance, result.result_text)

        # Save git diff for build tasks
        if instance.branch and not result.is_error:
            await self._save_diff(instance)

        # Mirror the JSONL the CLI just wrote to every other configured
        # account.  Keeps cross-account session storage in lockstep so a
        # later failover's freshness check is unambiguous, and a --resume
        # after reboot / cache-miss always finds a current copy.
        # Lives here (after the worktree back-copy and inside the post-run
        # path of _run_impl) so it fires on BOTH original-account success
        # AND failover-success — failover recurses through _run_impl, so
        # this hook runs on whichever account ultimately succeeded.
        if not result.is_error and instance.session_id and account_dir:
            await self._mirror_session_to_peer_accounts(instance, account_dir)

        # Extract summary
        instance.summary = extract_summary(result.result_text)

        # Capture the JSONL uuid of the final assistant message so the
        # "Branch from here" button can fork the session at this point.
        # Resolve under the active account_dir — the JSONL was just written
        # there.  config.CLAUDE_PROJECTS_DIR points only at the default
        # account, so a non-default-account run (e.g. klerk) would miss.
        session_id = result.session_id or captured_session_id
        if session_id and instance.repo_path:
            try:
                from bot.claude.session_fork import (
                    encode_project_path, get_last_assistant_uuid,
                )
                projects_root = (
                    Path(account_dir) / "projects"
                    if account_dir else config.CLAUDE_PROJECTS_DIR
                )
                proj = projects_root / encode_project_path(instance.repo_path)
                jsonl = proj / f"{session_id}.jsonl"
                last_uuid = await asyncio.to_thread(get_last_assistant_uuid, jsonl)
                if last_uuid:
                    result.last_assistant_uuid = last_uuid
            except Exception:
                log.exception("last_assistant_uuid capture failed for %s", instance.id)

        return result

    async def _after_exit_housekeeping(
        self, instance: Instance, account_dir: str | None,
    ) -> None:
        """Copy a worktree session back and release the test mutex.

        Runs on every way out of ``_stream_output`` once the CLI has exited. The
        mutex is the one with teeth: the release is the backstop for a session
        killed mid-test-run, which is exactly the shape of a watchdog kill, and
        a build that stops on AskUserQuestion holds the lock until its answer
        arrives unless it is released here. Siblings on that repo otherwise wait
        out the hook's stale TTL. Both halves are idempotent (the release
        no-ops unless this worktree owns the lock; the back-copy overwrites).
        """
        if not instance.worktree_path:
            return
        try:
            await asyncio.to_thread(
                self._copy_session_from_worktree, instance, account_dir,
            )
        except Exception:
            log.exception("Session back-copy failed for %s", instance.id)
        try:
            await asyncio.to_thread(
                self._release_test_mutex,
                instance.repo_path, instance.worktree_path,
            )
        except Exception:
            log.exception("Test-mutex release failed for %s", instance.id)

    # --- Task-level tracking ---

    def begin_task(
        self,
        instance_id: str,
        session_id: str | None = None,
        channel_id: str | None = None,
    ) -> None:
        """Mark a high-level task (query/workflow chain) as active."""
        self._active_tasks.add(instance_id)
        if session_id:
            self._active_sessions[session_id] = instance_id
        if channel_id:
            self._active_channels[str(channel_id)] = instance_id
        self._idle_event.clear()

    def end_task(self, instance_id: str) -> None:
        """Mark a high-level task as completed.

        If all tasks are done and reboot requests are pending, fires the
        idle callback (exactly once) to execute the coalesced reboot.
        """
        self._active_tasks.discard(instance_id)
        # Clean up session tracking
        stale = [s for s, t in self._active_sessions.items() if t == instance_id]
        for s in stale:
            del self._active_sessions[s]
        # Clean up channel tracking
        stale_chans = [c for c, t in self._active_channels.items() if t == instance_id]
        for c in stale_chans:
            del self._active_channels[c]
        if not self._active_tasks and not self._processes:
            self._idle_event.set()
            self._maybe_fire_idle_reboot()

    @property
    def is_busy(self) -> bool:
        """Whether any tasks or processes are active."""
        return bool(self._active_tasks) or bool(self._processes)

    @property
    def active_task_count(self) -> int:
        """Number of active high-level tasks."""
        return len(self._active_tasks)

    @property
    def active_count(self) -> int:
        """Number of currently running CLI processes."""
        return len(self._processes)

    @property
    def active_ids(self) -> list[str]:
        """IDs of currently running instances."""
        return list(self._processes.keys())

    @property
    def is_draining(self) -> bool:
        """Whether a reboot is pending and no new work should start."""
        return self._draining

    def is_session_active(self, session_id: str | None) -> bool:
        """Check if a task is currently running for this session."""
        if not session_id:
            return False
        return session_id in self._active_sessions

    def active_instance_for_session(self, session_id: str | None) -> str | None:
        """Return instance_id of the currently-running task for a session, or None."""
        if not session_id:
            return None
        return self._active_sessions.get(session_id)

    def active_instance_for_channel(self, channel_id: str | int | None) -> str | None:
        """Return instance_id of the currently-running task for a channel, or None.

        Used as a defensive duplicate-spawn guard in ``workflows.spawn_from`` —
        catches paths that bypassed the per-channel lock (e.g. a future caller
        that forgets the contract).
        """
        if channel_id is None:
            return None
        return self._active_channels.get(str(channel_id))

    def check_spawn_allowed(self) -> str | None:
        """Return an error message if spawning is blocked, or None if OK.

        Took a session_id until the active-session rejection was removed: the
        per-channel lock in ``bot.engine.commands._get_channel_lock``
        serializes same-channel spawns cleanly and the Queued-embed UX handles
        it visibly.  We only reject during reboot drain, which no caller can
        influence, so the argument had no reader left.
        """
        if self._draining:
            return "Reboot in progress — try again shortly."
        return None

    async def wait_until_idle(self, timeout: float = 300) -> bool:
        """Wait until no tasks or processes are running."""
        try:
            await asyncio.wait_for(self._idle_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # --- Reboot coalescing ---

    def request_reboot(
        self, data: dict, current_iid: str | None = None,
        *, force: bool = False,
    ) -> RebootResult:
        """Queue a reboot request and trigger execution if already idle.

        If any task other than ``current_iid`` is active and ``force`` is
        False, defers instead — writes a fresh ``reboot_request.deferred.json``
        from ``data`` (the original ``reboot_request.json`` was already
        unlinked by the caller), does NOT set ``_draining`` or start the
        drain timer, and returns ``RebootResult.DEFERRED``. The deferred
        file is auto-promoted to ``reboot_request.json`` at the next idle
        session-end (within ``REBOOT_DEFERRED_TTL_SECS``); after that it
        is dropped as stale.

        ``current_iid`` defaults to ``None`` for non-task callers like
        ``app.py`` auto-update; in that case any active task triggers a defer.
        Post-``end_task`` callers may pass the just-finished iid as a safety
        belt (it's already absent from ``_active_tasks``, but the filter is
        correct either way).

        ``force=True`` bypasses the busy-check entirely. Used by user-initiated
        reboot paths (slash command, Deploy button) that already coordinate
        with ``wait_until_idle`` and want to proceed regardless.

        Atomicity: this function is sync and runs on the asyncio event loop.
        The check + queue happen without an intervening ``await``, so no
        TOCTOU window exists where a new task spawns between them.
        """
        other_active = [] if force else [
            iid for iid in self._active_tasks
            if current_iid is None or iid != current_iid
        ]
        if other_active:
            try:
                deferred = dict(data)
                deferred["blocked_by"] = list(other_active)
                deferred["blocked_at"] = datetime.now(timezone.utc).isoformat()
                config.REBOOT_REQUEST_DEFERRED_FILE.write_text(
                    _json_mod.dumps(deferred, indent=2), encoding="utf-8",
                )
            except Exception:
                log.exception("Failed to write deferred reboot file")
            log.warning(
                "Reboot deferred — %d other task(s) active: %s",
                len(other_active), other_active,
            )
            return RebootResult.DEFERRED

        # A queued reboot supersedes any prior deferred state — clear the
        # deferred file so a stale defer from earlier can't auto-promote
        # post-restart and fire a second unwanted reboot.
        try:
            config.REBOOT_REQUEST_DEFERRED_FILE.unlink(missing_ok=True)
        except OSError:
            pass

        self._draining = True
        self._reboot_requests.append(data)
        self._maybe_fire_idle_reboot()
        self._start_drain_timer()
        return RebootResult.QUEUED

    def pending_reboots(self) -> list[dict]:
        """Return a copy of pending reboot requests."""
        return list(self._reboot_requests)

    def clear_reboots(self) -> None:
        """Clear all pending reboot requests and reset draining flag.

        Does NOT delete drain_queue.json — the new process needs it after
        a successful reboot.  Call purge_drain_queue() explicitly when a
        reboot is *aborted* and queued messages should be discarded.
        """
        self._reboot_requests.clear()
        self._draining = False
        if self._drain_timer_task and not self._drain_timer_task.done():
            self._drain_timer_task.cancel()
            self._drain_timer_task = None

    @staticmethod
    def purge_drain_queue() -> None:
        """Delete the drain queue file (e.g. when reboot is aborted)."""
        try:
            config.DRAIN_QUEUE_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    def queue_for_replay(self, entry: dict) -> None:
        """Persist a user request to be replayed after reboot completes."""
        queue: list[dict] = []
        try:
            data = _json_mod.loads(config.DRAIN_QUEUE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                queue = data
        except (FileNotFoundError, _json_mod.JSONDecodeError):
            pass
        queue.append(entry)
        config.DRAIN_QUEUE_FILE.write_text(
            _json_mod.dumps(queue, indent=2), encoding="utf-8",
        )
        log.info("Queued message for post-reboot replay: channel=%s prompt=%s",
                 entry.get("channel_id"), (entry.get("prompt") or "")[:60])

    @staticmethod
    def read_drain_queue() -> list[dict]:
        """Read and delete the drain queue file. Returns [] if absent."""
        try:
            data = _json_mod.loads(config.DRAIN_QUEUE_FILE.read_text(encoding="utf-8"))
            config.DRAIN_QUEUE_FILE.unlink(missing_ok=True)
            return data if isinstance(data, list) else []
        except (FileNotFoundError, _json_mod.JSONDecodeError):
            return []

    def set_on_idle_reboot(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register async callback invoked when last task ends and reboots are pending.

        Captures the running loop for safe task scheduling from sync end_task().
        Must be called from an async context (e.g. during startup).
        """
        self._on_idle_callback = callback
        self._idle_loop = asyncio.get_running_loop()

    def _maybe_fire_idle_reboot(self) -> None:
        """Schedule the reboot callback if idle + reboots pending + not already running."""
        if (self._on_idle_callback
                and self._reboot_requests
                and not self._reboot_executing
                and not self._active_tasks
                and not self._processes
                and self._idle_loop):
            self._reboot_executing = True
            self._idle_loop.call_soon(
                lambda: self._idle_loop.create_task(self._on_idle_callback())
            )

    def _start_drain_timer(self) -> None:
        """Start a background timer that force-kills processes after drain timeout."""
        if self._drain_timer_task and not self._drain_timer_task.done():
            return  # Timer already running
        if not self._idle_loop:
            log.warning("No event loop for drain timer — reboot may hang")
            return
        log.info(
            "Reboot drain started — will force-kill in %ds if not idle",
            config.REBOOT_DRAIN_TIMEOUT_SECS,
        )
        self._drain_timer_task = self._idle_loop.create_task(self._drain_timeout())

    async def _drain_timeout(self) -> None:
        """Wait for drain timeout, then force-kill remaining processes and tasks."""
        try:
            await asyncio.sleep(config.REBOOT_DRAIN_TIMEOUT_SECS)
            if not self._reboot_requests:
                return  # Reboot already executed normally
            if not self._active_tasks and not self._processes:
                return  # Already idle
            log.warning(
                "Reboot drain timed out after %ds — killing %d tasks, %d processes",
                config.REBOOT_DRAIN_TIMEOUT_SECS,
                len(self._active_tasks),
                len(self._processes),
            )
            await self.kill_all()
            self.force_clear_tasks()
            self._maybe_fire_idle_reboot()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Drain timeout error")

    def force_clear_tasks(self) -> int:
        """Clear all active tasks regardless of process state. Returns count cleared.

        Use after kill_all() to unstick orphaned chain tasks.
        """
        count = len(self._active_tasks)
        if count:
            log.warning(
                "Force-clearing %d orphaned tasks: %s",
                count, list(self._active_tasks),
            )
        self._active_tasks.clear()
        self._active_sessions.clear()
        self._idle_event.set()
        return count

    async def kill(
        self, instance_id: str, *,
        intentional: bool = False,
        reason: str | None = None,
        owns_card: bool = False,
    ) -> bool:
        """Terminate a running CLI process.

        When ``intentional=True`` (or a ``reason`` is provided) the instance
        id is added to ``_intentional_kills`` immediately after
        ``proc.terminate()`` returns successfully — never before the kill
        signal is actually fired.  This means the set always reflects "we
        successfully terminated this process," and a no-op kill (process
        already gone) leaves the set untouched.

        ``reason`` (when set) is stamped onto ``RunResult.kill_reason`` so
        the lifecycle layer can distinguish user-Kill from Steer, and
        ``owns_card`` onto ``RunResult.kill_owns_card`` so it knows whether
        the caller is going to rewrite the live progress message itself.

        The one case where the mark IS recorded without a signal: the run is
        still live (its lifecycle task is registered) but sits *between*
        subprocesses — every recovery layer in ``_run_impl`` re-spawns, and
        during that window there is nothing to terminate.  Without the mark
        the kill would evaporate and the retry would spawn a brand new
        attempt seconds after the user asked it to stop.  ``_run_impl``
        checks the mark on re-entry and bails instead.
        """
        proc = self._processes.get(instance_id)
        if not proc:
            if instance_id in self._active_tasks and (
                intentional or reason is not None
            ):
                self._intentional_kills.add(instance_id)
                if reason is not None:
                    self._kill_reasons[instance_id] = reason
                if owns_card:
                    self._kill_card_owners.add(instance_id)
                log.info(
                    "Kill for %s arrived between attempts — retries stopped",
                    instance_id,
                )
                return True
            return False
        # Recorded before the signal, unlike the intent mark below: this one
        # answers "could a SIGKILL on this run be ours", and the escalation
        # further down is exactly such a SIGKILL.
        self._bot_signalled.add(instance_id)
        try:
            proc.terminate()
            # Mark as intentional only after terminate() returns without
            # raising — guarantees the set means "we actually sent the
            # signal," not "we wanted to."
            if intentional or reason is not None:
                self._intentional_kills.add(instance_id)
                if reason is not None:
                    self._kill_reasons[instance_id] = reason
                if owns_card:
                    self._kill_card_owners.add(instance_id)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            return True
        except Exception:
            log.exception("Error killing process %s", instance_id)
            return False
        finally:
            self._processes.pop(instance_id, None)
            if not self._active_tasks and not self._processes:
                self._idle_event.set()

    async def kill_and_wait(
        self, instance_id: str, timeout: float = 10.0,
        *, intentional: bool = True,
        reason: str | None = None,
        owns_card: bool = False,
    ) -> KillOutcome:
        """Kill an instance and wait for its lifecycle task to fully finish.

        Used by Steer: the caller needs the channel lock released (finalize
        done, session file copied back, progress edit applied) before spawning
        a replacement run.  ``kill()`` alone only handles the subprocess —
        it doesn't wait for the surrounding ``run_instance`` coroutine.

        Escalation path: after the inner ``kill()`` returns (which already
        does terminate → 5s wait → SIGKILL), poll for ``_active_tasks`` to
        drop the instance_id for up to *timeout* seconds.  On timeout,
        force-clear the task so the channel lock never stays wedged.

        Returns ``KillOutcome.FINALIZED`` when the lifecycle's finally block
        ran (status flip already happened), ``KillOutcome.FORCE_CLEARED``
        when the timeout fired (caller owns the status flip), or
        ``KillOutcome.NOT_RUNNING`` if the instance wasn't active.

        Defaults ``intentional=True`` because every current caller is a
        user-initiated path (Kill button + resolve-cancel + discard at
        commands.py, Steer at interactions.py).  Pass ``intentional=False``
        from automated paths that should still surface a FAILED embed.
        """
        if instance_id not in self._active_tasks and instance_id not in self._processes:
            return KillOutcome.NOT_RUNNING
        await self.kill(
            instance_id, intentional=intentional, reason=reason,
            owns_card=owns_card,
        )
        # Wait for the lifecycle coroutine to finish its finally block
        # (end_task fires there and removes the instance from _active_tasks).
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while instance_id in self._active_tasks:
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.warning(
                    "kill_and_wait timed out after %.1fs for %s — force-clearing",
                    timeout, instance_id,
                )
                self._active_tasks.discard(instance_id)
                stale = [
                    s for s, t in self._active_sessions.items() if t == instance_id
                ]
                for s in stale:
                    del self._active_sessions[s]
                if not self._active_tasks and not self._processes:
                    self._idle_event.set()
                return KillOutcome.FORCE_CLEARED
            await asyncio.sleep(min(0.1, remaining))
        return KillOutcome.FINALIZED

    async def kill_all(self) -> int:
        """Terminate all running processes. Returns count killed."""
        killed = 0
        for iid in list(self._processes.keys()):
            if await self.kill(iid):
                killed += 1
        return killed

    # --- Per-repo locking ---

    def _get_repo_lock(self, repo_path: str) -> asyncio.Lock:
        """Get or create a per-repo lock for serializing git admin operations."""
        if repo_path not in self._repo_locks:
            self._repo_locks[repo_path] = asyncio.Lock()
        return self._repo_locks[repo_path]

    def _get_release_lock(self, repo_path: str) -> asyncio.Lock:
        """Get or create a per-repo release lock.

        Held for the duration of a release Claude session so two worktrees
        can't both pick the same version number. Orthogonal to _repo_locks —
        does not block worktree creation, merges, or branch deletion.
        """
        if repo_path not in self._release_locks:
            self._release_locks[repo_path] = asyncio.Lock()
        return self._release_locks[repo_path]

    @asynccontextmanager
    async def release_lock_scope(self, repo_path: str, label: str):
        """Acquire the release lock with structured wait/hold logging.

        ``label`` identifies the call site (e.g. "release-chain", "done-standalone",
        "release-cmd") so logs distinguish entrypoints.
        """
        lock = self._get_release_lock(repo_path)
        wait_start = time.monotonic()
        log.info(
            "release-lock repo=%s site=%s event=waiting", repo_path, label,
        )
        async with lock:
            wait = time.monotonic() - wait_start
            hold_start = time.monotonic()
            log.info(
                "release-lock repo=%s site=%s event=acquired wait=%.2fs",
                repo_path, label, wait,
            )
            try:
                yield
            finally:
                hold = time.monotonic() - hold_start
                log.info(
                    "release-lock repo=%s site=%s event=released hold=%.2fs",
                    repo_path, label, hold,
                )
