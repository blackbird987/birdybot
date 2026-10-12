"""Recovery after a failed CLI run: the ordered rungs ``_run_impl`` tries.

A run that fell over by itself is offered to each step in ``_RECOVERY_STEPS``,
in order. A step returns the RunResult to hand back (usually from re-entering
``_run_impl`` through ``_RunAttempt.reenter``), or None to pass the failure on
to the next step. A step may also amend ``run.result`` on its way past, as the
thrash step does with its cause line.

The order is load-bearing and pinned by ``scripts/test_run_recovery_order.py``:
the missing-conversation cascade first, the resume-the-same-conversation cases
(thrash, memory reap, OOM kill) before the context-overflow abandon, the model
limit before the generic usage limit whose parser also matches its wording,
and the account-failure branch last, because its no-turns heuristic matches
almost any failure that produced nothing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from bot import config
from bot.claude import context_bloat
from bot.claude.auth_health import REASON_ORG_DISABLED, REASON_RUNTIME_401
from bot.claude.parser import (
    RunResult,
    is_account_agnostic_error,
    is_context_overflow_error,
    is_context_thrash_error,
    is_org_disabled_error,
    is_transient_error,
    looks_like_fatal_auth_error,
)
from bot.claude.provider import ProviderConfig
from bot.claude.types import Instance
from bot.claude.runner.common import (
    ContextResetCallback,
    ProgressCallback,
    RecoveryCallback,
    StallCallback,
    _notify,
)
from bot.claude.runner.reaping import _no_productive_work

log = logging.getLogger(__name__)

# Tried in this order on every failed run; see the module docstring.
_RECOVERY_STEPS = (
    "_recover_missing_conversation",
    "_recover_context_thrash",
    "_recover_memory_reap",
    "_recover_oom_kill",
    "_recover_context_overflow",
    "_recover_model_limit",
    "_recover_usage_limit",
    "_recover_transient",
    "_recover_account_failure",
)


def _is_primary_model(model: str | None) -> bool:
    """True when *model* resolves to the accounts' default primary model.

    None resolves through DEFAULT_SESSION_MODEL first (the deployment-wide
    default that provider.build_command applies as the last --model fallback);
    only when that is also unset does None mean the primary model runs:
    pinned explicitly on resume (build_command's sticky-resume guard), or
    the account's CLI default on fresh sessions — both of which PRIMARY_MODEL
    names on this deployment. Explicit non-primary overrides (e.g. EXPLORE_MODEL=
    "sonnet", DEFAULT_SESSION_MODEL="opus") are exempt from model-limit
    downgrades — their model has its own quota.
    """
    effective = model or config.DEFAULT_SESSION_MODEL
    if not effective:
        return True
    return config.PRIMARY_MODEL.lower() in effective.lower()


def _carry_forward_work_record(aborted: RunResult, resumed: RunResult) -> RunResult:
    """Fold a discarded attempt's record of work into the attempt that replaced it.

    ``finalize_run`` *assigns* the winning RunResult's fields onto the Instance,
    so anything the aborted attempt recorded is simply dropped.  Which tools ran
    is the field with teeth: a build that made every one of its edits before
    being killed, then resumed only to confirm the work was already on disk,
    would report an empty tool list — and the chain reads that list to decide
    whether a build changed code at all (``CODE_CHANGE_TOOLS``), so a real build
    would render as "no changes made".  The bash log feeds the eval surface and
    the main-repo-path hits feed the poisoning warning; both are equally gone.

    Deliberately NOT merged:
      * ``num_turns`` — the failover heuristic above treats >1 turn as proof the
        account took the turn, so inflating it could hide a dead backup account.
      * ``context_tokens`` / ``model`` — a snapshot of the final call (it drives
        the "context nearly full" warning), not a running total.
      * ``session_id`` / ``result_text`` / status flags — the resumed attempt is
        the authoritative one.

    A killed CLI usually emits no ``result`` event, so the cost/duration/token
    counters below are typically zero on the aborted side; they are summed
    anyway because they are cumulative totals whenever they are present.
    """
    for numeric in (
        "cost_usd", "duration_ms", "duration_api_ms",
        "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_creation_tokens",
    ):
        setattr(resumed, numeric,
                getattr(aborted, numeric) + getattr(resumed, numeric))

    # tools_used and path_poisoning are sets wearing a list's clothes and must
    # not gain duplicates; bash_commands is a chronological log where the same
    # command run twice is two real events.
    for listed, dedupe in (
        ("tools_used", True),
        ("bash_commands", False),
        ("path_poisoning", True),
    ):
        merged = list(getattr(aborted, listed) or [])
        seen = set(merged)
        for item in getattr(resumed, listed) or []:
            if dedupe and item in seen:
                continue
            seen.add(item)
            merged.append(item)
        setattr(resumed, listed, merged)

    return resumed


async def _announce_session_loss(
    on_recovery, instance: Instance, error_text: str, session_id: str | None,
) -> bool:
    """Tell the platform a conversation is being abandoned; True if it landed.

    Called while the old id is still known, so the thread warning can name
    it. Best-effort: a failing callback must not block the recovery. The
    result rides on ``recovery_warning_posted`` so commands.py can drop its
    terser duplicate of the same notice.
    """
    if not on_recovery:
        return False
    try:
        await on_recovery(error_text, session_id, instance.worktree_path)
        return True
    except Exception:
        log.exception("on_recovery callback failed for %s", instance.id)
        return False


# Both memory recoveries hand their note to the next attempt
# through the same slot, so they share one mark/unmark pair.
def _memory_note_slot(
    instance: Instance, note: str,
) -> tuple[Callable[[], None], Callable[[], None]]:
    def _mark() -> None:
        instance._memory_kill_note = note

    def _unmark() -> None:
        instance._memory_kill_note = None

    return _mark, _unmark


class _RunAttempt:
    """One ``_run_impl`` call: its arguments, and what the spawn learned.

    The recovery steps read everything through this, so a step is a method
    rather than a branch in a 1,100-line function. ``account_dir`` and
    ``model_override`` are set while preparing the spawn; ``result`` and
    ``error_text`` once the run has finished. ``error_text`` is read once,
    before any step runs, and is deliberately not refreshed when a step
    amends ``result.error_message``.
    """

    def __init__(
        self,
        runner: RecoveryMixin,  # the ClaudeRunner it is mixed into; core is not imported here
        instance: Instance,
        on_progress: ProgressCallback | None,
        on_stall: StallCallback | None,
        context: str | None,
        sibling_context: str | None,
        api_fallback: bool,
        provider: ProviderConfig,
        binary: str,
        recovery_state: set[str],
        on_recovery: RecoveryCallback | None,
        on_context_reset: ContextResetCallback | None,
    ) -> None:
        self.runner = runner
        self.instance = instance
        self.on_progress = on_progress
        self.on_stall = on_stall
        self.context = context
        self.sibling_context = sibling_context
        self.api_fallback = api_fallback
        self.provider = provider
        self.binary = binary
        self.recovery_state = recovery_state
        self.on_recovery = on_recovery
        self.on_context_reset = on_context_reset
        self.account_dir: str | None = None
        self.model_override: str | None = None
        self.result: RunResult | None = None
        self.error_text = ""

    async def reenter(self, **overrides) -> RunResult:
        """Re-run this turn after a recovery step, forwarding every argument.

        Every rung re-enters _run_impl with the same eleven arguments; only
        api_fallback ever differs, and the transient rung passes it through
        ``overrides``. Written out at each rung it was ten copies of one call,
        which is how a twelfth parameter gets forwarded by nine of them and
        dropped by the tenth. ``provider``, ``binary`` and ``recovery_state``
        are never rebound, so every re-entry shares the first one's snapshot.
        """
        return await self.runner._run_impl(
            self.instance, self.on_progress, self.on_stall,
            self.context, self.sibling_context,
            api_fallback=overrides.get("api_fallback", self.api_fallback),
            _provider=self.provider, _binary=self.binary,
            _recovery_state=self.recovery_state,
            on_recovery=self.on_recovery,
            on_context_reset=self.on_context_reset,
        )

    async def switch_account(self, reason: str) -> RunResult:
        """Announce a move to the next account, then re-run there.

        The three account rungs (model limit, usage limit, unusable account)
        each pick their target and mark the tried set first; this is only the
        shared tail, so their order is untouched.
        """
        await _notify(
            self.on_progress, "Switching to backup account", reason,
            "during failover",
        )
        return await self.reenter()

    # Both recoveries below are the same move: the BOT ended this run
    # (not the model, not the API), so pick the same conversation up
    # again and open it with a note saying what happened. They differ
    # only in policy — how many times, what the note is, what to call it.
    #
    # The mechanism lives in one place because it is eleven steps long
    # and every way of getting it wrong is silent. Skip the
    # session_account stamp and --resume lands on an account whose
    # ~/.claude/projects/ has never seen this session. Skip the unmark
    # and an unrelated cooldown retry opens hours later with a stale
    # "your previous attempt was aborted". Skip the carry-forward and
    # the aborted attempt's record of the files it already changed is
    # lost. Fixing one copy of that and forgetting the other has
    # already happened once in this file.
    async def resume_same_conversation(
        self,
        kind: str,
        max_retries: int,
        subject: str,
        mark: Callable[[], None],
        unmark: Callable[[], None],
        progress: Callable[[int], tuple[str, str]],
        before_resume: Callable[[], Awaitable[None]] | None = None,
    ) -> RunResult | None:
        """Resume this run's conversation once more.

        Returns None for "not handled" — out of retries, or no
        conversation to resume — so the caller falls through to the
        normal failure and the Retry button is still offered.

        ``mark``/``unmark`` set and clear the ephemeral marker that
        _build_command consumes to prefix the note onto the next
        prompt. ``progress`` receives the number of resumes already
        spent, for messages that count attempts. ``before_resume`` runs
        after the budget is spent and before the re-spawn, for a resume
        that has to wait on something first.
        """
        result = self.result
        instance = self.instance
        recovery_state = self.recovery_state
        account_dir = self.account_dir
        on_progress = self.on_progress
        attempts = sum(
            1 for k in recovery_state if k.startswith(f"{kind}:")
        )
        # Prefer the id the run just reported: on a FRESH spawn the
        # instance has none, and the conversation we need to resume is
        # the one the dead process created.
        resume_id = result.session_id or instance.session_id
        if attempts >= max_retries or not resume_id:
            log.warning(
                "%s for %s not auto-resumed (resumes=%d/%d, session=%s)",
                subject, instance.id, attempts, max_retries,
                (resume_id or "none")[:12],
            )
            return None
        recovery_state.add(f"{kind}:{attempts + 1}")
        instance.session_id = resume_id
        if account_dir:
            # Session ownership is normally stamped only on success; do
            # it here too, or _pick_account has no `prefer` hint.
            instance.session_account = account_dir
        mark()  # consumed by _build_command on the very next attempt
        log.warning(
            "%s for %s (resume %d/%d) \u2014 resuming session %s",
            subject, instance.id, attempts + 1, max_retries,
            resume_id[:12],
        )
        if on_progress:
            headline, detail = progress(attempts)
            await _notify(on_progress, headline, detail, f"during {kind} resume")
        # _build_command normally consumes the marker, but the resume
        # can end before it ever gets there — the refuse-to-spawn
        # short-circuit returns as soon as every account is on
        # cooldown, and that path re-queues this same Instance object
        # for a later cooldown retry. A `finally` rather than a plain
        # statement after the await because raising is one of those
        # exits too: _run_impl builds worktrees and spawns processes,
        # and an Instance that survives the exception (a re-queue, or
        # the user pressing Retry) would open with a recovery note
        # about a run that ended hours ago.
        try:
            if before_resume is not None:
                await before_resume()
            resumed = await self.reenter()
        finally:
            unmark()
        # The aborted attempt did real work — its edits are on disk and
        # the resumed attempt may never touch a file again.
        return _carry_forward_work_record(result, resumed)


class RecoveryMixin:
    """The recovery steps, one method per rung, tried in ``_RECOVERY_STEPS`` order."""

    async def _recover_missing_conversation(self, run: _RunAttempt) -> RunResult | None:
        """Layered recovery for a resume whose conversation is gone."""
        # Dead session: layered recovery before silent --resume drop.
        # Layer 1: rebuild the owning account's session-index in-process
        #          and retry once with --resume intact.  Runs FIRST because
        #          claude.exe's "No conversation found" usually means the
        #          JSONL is on disk but missing from its index (e.g. after
        #          a mid-stream crash) — see commit history for t-3260
        #          forensics.  Account-swap before this would futilely
        #          rebuild the wrong account's index.
        # Layer 2: try the OTHER account (the session may live there).
        # Layer 3: clear session_id and run blank (last resort).
        result = run.result
        error_text = run.error_text
        instance = run.instance
        account_dir = run.account_dir
        provider = run.provider
        recovery_state = run.recovery_state
        on_recovery = run.on_recovery
        if result.is_error and "No conversation found" in error_text and instance.session_id:
            # Layer 1: rebuild session-index for the owning account FIRST.
            # _maybe_rebuild_session_index is cheap on cache hit and short-
            # circuits if the project dir doesn't exist, so calling it
            # before the cross-account swap has negligible downside but
            # catches the common case where the JSONL is right here.
            if "rebuild" not in recovery_state:
                recovery_state.add("rebuild")
                owning_account = (
                    instance.session_account
                    if instance.session_account in config.CLAUDE_ACCOUNTS
                    else account_dir
                )
                rebuilt = await self._maybe_rebuild_session_index(
                    owning_account, instance,
                )
                if rebuilt:
                    log.info(
                        "Rebuilt session-index for %s; retrying --resume on %s",
                        instance.id, (owning_account or "?")[-20:],
                    )
                    # Clear _accounts_tried so the picker can revisit the
                    # owning account on the retry.
                    instance._accounts_tried.discard(owning_account or "")
                    return await run.reenter()

            # Layer 2: try other account if we haven't yet and one is available.
            if (
                "other_account" not in recovery_state
                and provider.supports_account_failover
                and account_dir
            ):
                recovery_state.add("other_account")
                instance._accounts_tried.add(account_dir)
                next_account = self._pick_account(exclude=instance._accounts_tried)
                if next_account:
                    log.info(
                        "Session %s missing on %s, trying %s for %s",
                        instance.session_id[:12], account_dir[-20:],
                        next_account[-20:], instance.id,
                    )
                    return await run.reenter()

            # Layer 3 (last resort): drop session and run blank.  Tag the
            # downstream result so commands.py can surface a "lost prior
            # context" notice to the user instead of silently rebinding to
            # a fresh session.  Without this, the thread accumulates orphan
            # JSONLs across retries (the t-3260 cascade: 3 sessions in 90s).
            original_session_id = instance.session_id
            log.warning(
                "Session %s exhausted recovery for %s, retrying without resume",
                instance.session_id, instance.id,
            )
            # Fire the recovery callback BEFORE clearing session_id so
            # the platform layer can post a visible thread warning
            # (Layer 4 of t-3541 fix).  Best-effort — don't let a
            # callback failure block recovery.  Track success so the
            # downstream `session_recovery_exhausted` notice in
            # commands.py can suppress its older, terser duplicate.
            warning_posted = await _announce_session_loss(
                on_recovery, instance,
                error_text or "No conversation found", original_session_id,
            )
            instance.session_id = None
            # Layer 2 marked the account it just used as "tried" — but
            # that mark means "this conversation isn't filed on that
            # account", which stops being true of anything the moment the
            # conversation is abandoned.  Carrying it into a blank run
            # leaves the picker with no candidates and makes it refuse to
            # spawn, so a recoverable missing-session turned into a
            # fabricated "no account available" that then re-queued every
            # 16 minutes forever.  Real reasons to skip an account (usage
            # limit, failed auth) are recorded separately as cooldowns and
            # still apply after this reset.
            instance._accounts_tried = set()
            recovery_state.add("exhausted")
            fresh = await run.reenter()
            fresh.session_recovery_exhausted = True
            fresh.recovery_warning_posted = warning_posted
            # Don't poison the retry path: if the fallback produced no
            # usable output because it never got to run, restore the
            # ORIGINAL session_id so the cooldown retry in app.py
            # can still resume it.  Predicate uses result_text rather
            # than num_turns because a usage-limit hit *after* turn 1
            # also has no recoverable conversation but does have turns.
            #
            # Restoring is only right when the attempt was blocked on
            # account availability — a cooldown or a refusal to spawn —
            # because then the conversation is untested and may well still
            # be there.  When the blank run simply produced nothing, the
            # search above has already proven the conversation exists on no
            # account under any spelling, and handing that id back to the
            # retry queue rebuilds the exact loop this is meant to avoid:
            # resume, fail, restore, re-queue, forever, every 16 minutes.
            fresh_text = (fresh.result_text or "").strip()
            blocked_on_account = bool(
                fresh.retry_reason or fresh.usage_limit_reset
            )
            if (
                fresh.session_id != original_session_id
                and not fresh_text
                and original_session_id
            ):
                if blocked_on_account:
                    log.info(
                        "Layer-3 fresh run never started (%s); "
                        "restoring original session_id %s for %s",
                        fresh.retry_reason or "cooldown",
                        original_session_id[:12], instance.id,
                    )
                    fresh.session_id = original_session_id
                    instance.session_id = original_session_id
                else:
                    log.warning(
                        "Layer-3 fresh run produced no output for %s and "
                        "session %s is on no account \u2014 dropping it rather "
                        "than re-queueing an unresumable id",
                        instance.id, original_session_id[:12],
                    )
            return fresh
        return None

    async def _recover_context_thrash(self, run: _RunAttempt) -> RunResult | None:
        """Resume an autocompact thrash, naming the file that keeps refilling it."""
        # Autocompact thrash: the CLI aborts its own process when the
        # context refills to the limit within 3 turns of a compact, 3 times
        # running.  Nothing is broken — that counter is per-process, so a
        # resume starts from the compact summary with it back at zero,
        # which is why the manual Retry has always worked on the first
        # click.  Do it ourselves instead of blocking a chain and paging
        # the user to press a button whose only job is "run it again".
        #
        # Placed here on purpose: after the dead-session cascade (a thrash
        # is not a missing conversation) and before the model-limit and
        # account-failure branches, neither of which can match this text
        # anyway — the no-turns heuristic needs an empty result and zero
        # turns, and a thrash has ~50 of them.
        result = run.result
        error_text = run.error_text
        instance = run.instance
        if result.is_error and is_context_thrash_error(error_text):
            # Name the file that keeps refilling the context, if there is
            # one. Diagnosis only: the resume below runs exactly as often
            # either way. A nested CLAUDE.md of a few hundred KB is the
            # thrash a resume cannot clear (it reloads after every
            # compaction), so the resumed attempt is told how to stop
            # triggering it, and the final failure says what to slim.
            bloat = await self._thrash_bloat_findings(
                instance, result.session_id or instance.session_id,
            )
            bloat_note = context_bloat.render_note(bloat)

            def _mark_thrash() -> None:
                instance._context_thrash_retry = True
                instance._context_bloat_note = bloat_note or None

            def _unmark_thrash() -> None:
                instance._context_thrash_retry = False
                instance._context_bloat_note = None

            handled = await run.resume_same_conversation(
                kind="context_thrash",
                max_retries=config.CONTEXT_THRASH_MAX_RETRIES,
                subject="Autocompact thrash",
                mark=_mark_thrash,
                unmark=_unmark_thrash,
                progress=lambda spent: (
                    "Context filled up \u2014 resuming automatically",
                    f"The CLI stopped itself to avoid a compaction loop; "
                    f"picking the session back up (attempt {spent + 2} of "
                    f"{config.CONTEXT_THRASH_MAX_RETRIES + 1})",
                ),
            )
            if handled is not None:
                return handled
            # Out of resumes. The cause goes AFTER the CLI's own text:
            # is_context_thrash_error and is_account_agnostic_error read
            # this message, and the original wording has to stay intact
            # for them.
            cause = context_bloat.cause_line(bloat)
            if cause:
                result.error_message = (
                    (result.error_message or error_text) + "\n\n" + cause
                )
        return None

    async def _recover_memory_reap(self, run: _RunAttempt) -> RunResult | None:
        """Resume once after the bot's own memory reap, with the ceiling in the note."""
        # Memory reap: the guard in _stream_output killed this run's process
        # tree for crossing SESSION_MEM_KILL_MB.  Resumed for the same reason
        # a thrash is — halting a chain here would page the user at 2 AM over
        # something the agent can fix itself — but resumed *once*, and only
        # with the note that says how far over it went.  A silent re-run is
        # the exact behaviour that turned one OOM into two on 2026-08-17:
        # nothing told the resumed session what had happened, so it re-ran
        # the same job and died the same way ninety minutes later.
        #
        # Sits after the thrash branch and before the account/model branches
        # for the same reason that one does: the error text here is ours, so
        # no other parser can match it, but the ordering keeps every
        # "resume the same conversation" case in one place.
        result = run.result
        instance = run.instance
        if result.is_error and result.memory_kill_note:
            mark_memory, unmark_memory = _memory_note_slot(
                instance, result.memory_kill_note,
            )
            handled = await run.resume_same_conversation(
                kind="memory_kill",
                max_retries=config.MEMORY_KILL_MAX_RETRIES,
                subject="Memory reap",
                mark=mark_memory,
                unmark=unmark_memory,
                progress=lambda _spent: (
                    "Out of memory \u2014 resuming with a smaller budget",
                    "Picking the session back up and telling it what the "
                    "ceiling is, so it can size the job to fit instead of "
                    "hitting the same wall.",
                ),
            )
            if handled is not None:
                return handled
        return None

    async def _recover_oom_kill(self, run: _RunAttempt) -> RunResult | None:
        """Resume once after the system's OOM protection killed the scope."""
        # OOM kill: the operating system, not the bot, killed this run's
        # scope (systemd-oomd on the session slice's stall, or the kernel at
        # its memory.max). The bot's own reap above has a note of its own;
        # this is the case where nothing in the bot decided anything, and on
        # 2026-10-08 it surfaced as a red FAILED card that the user had to
        # find and retry by hand while the work sat on disk.
        #
        # Two rules that make it a recovery rather than a second kill:
        # the resume goes through the same admission hold a new session
        # does, so it waits for the slice to drain instead of re-entering
        # the stall that just shot it; and it is resumed once
        # (OOM_KILL_RESUME_RETRIES), with a note saying what happened and to
        # lower its parallelism. A second kill is a job that does not fit,
        # and that is the user's call.
        result = run.result
        instance = run.instance
        on_progress = run.on_progress
        if result.is_error and result.oom_kill_note:
            mark_oom, unmark_oom = _memory_note_slot(instance, result.oom_kill_note)

            async def _hold_for_memory() -> None:
                await self._await_memory_headroom(instance, on_progress)

            handled = await run.resume_same_conversation(
                kind="oom_kill",
                max_retries=config.OOM_KILL_RESUME_RETRIES,
                subject="OOM kill",
                mark=mark_oom,
                unmark=unmark_oom,
                progress=lambda _spent: (
                    "OOM-killed: resuming once memory frees up",
                    "The system's out-of-memory protection stopped this "
                    "session. Its edits are on disk; it picks back up as "
                    "soon as there is room, told to run lighter.",
                ),
                before_resume=_hold_for_memory,
            )
            if handled is not None:
                return handled
        return None

    async def _recover_context_overflow(self, run: _RunAttempt) -> RunResult | None:
        """Resume once, then abandon a session that will not compact."""
        # Context overflow: the conversation no longer fits and the CLI's
        # automatic compaction failed on it.  Sits after both resume-the-
        # same-conversation branches because it is the one case where that
        # move is WRONG past the first attempt: the oversized transcript is
        # on disk, so every resume feeds it back to the summariser that
        # just failed.  Left unhandled it wedges the thread rather than the
        # run -- the next message resumes the same session and dies the
        # same way, forever, which is exactly what five consecutive runs
        # did on 2026-09-03.
        #
        # Two rungs, in this order:
        #   1. Resume once.  Both failures seen in the wild came from the
        #      SUMMARISER, not the transcript ("summarization produced
        #      empty response", and a safety flag on the summarisation
        #      call), and those are per-call blips.  Nearly free: the CLI
        #      aborts in seconds, before the turn does any work.
        #   2. Abandon the session and run fresh, primed with the thread's
        #      recent history.  This is the rung that actually unwedges the
        #      thread, because a successful fresh run rebinds it (see
        #      lifecycle.should_bind_session, which treats
        #      session_recovery_exhausted as licence to adopt the new id --
        #      the old one has been proven unusable by the rung above).
        #
        # The two rungs compose through recursion rather than a loop: the
        # resumed attempt re-enters this branch, finds its retry budget
        # spent, and falls through to the fresh path itself.
        #
        # Not ours when the account underneath is dead.  Compaction is an
        # ordinary API call, so an account-level rejection surfaces
        # *through* the summariser and arrives spelled as a compaction
        # failure: "Prompt is too long · automatic compaction failed: Your
        # organization has disabled Claude subscription access for Claude
        # Code".  Answering that as a context problem spends both rungs and
        # then abandons a perfectly good session -- which is what happened
        # to q-17514 and q-17515 on 2026-09-15, two threads amputated to
        # work around an org-disabled subscription.  The transcript is
        # innocent; fall through to the account branch, which sidelines the
        # account and fails over instead.
        result = run.result
        error_text = run.error_text
        instance = run.instance
        recovery_state = run.recovery_state
        on_recovery = run.on_recovery
        on_context_reset = run.on_context_reset
        if (
            result.is_error
            and is_context_overflow_error(error_text)
            and not looks_like_fatal_auth_error(error_text)
        ):
            handled = await run.resume_same_conversation(
                kind="context_overflow",
                max_retries=config.CONTEXT_OVERFLOW_RESUME_RETRIES,
                subject="Context overflow",
                # No note on this rung on purpose. The agent is about to
                # resume a conversation it never lost -- nothing happened
                # that it needs telling about, and a "you were aborted"
                # preamble on a transcript that is already at the limit
                # spends context to say nothing.
                mark=lambda: None,
                unmark=lambda: None,
                progress=lambda _spent: (
                    "Context filled up \u2014 retrying compaction",
                    "The conversation outgrew the context window and "
                    "compacting it failed; trying once more before "
                    "starting a fresh session.",
                ),
            )
            if handled is not None:
                return handled

            original_session_id = instance.session_id
            if (
                config.CONTEXT_OVERFLOW_FRESH
                and original_session_id
                and "overflow_fresh" not in recovery_state
            ):
                recovery_state.add("overflow_fresh")
                log.warning(
                    "Context overflow for %s: session %s will not compact "
                    "\u2014 abandoning it for a fresh session",
                    instance.id, original_session_id[:12],
                )
                # Build the briefing BEFORE the session is cleared and
                # before the warning is posted: it reads Discord history,
                # which can fail slowly, and a failure here must cost the
                # new session its memory of the thread, not its existence.
                # Comes back already framed as data and already terminated
                # by its own "---" separator (see ContextResetCallback), so
                # the prompt _build_command appends lands on the far side
                # of it -- concatenating a bare digest here would hand the
                # new session the user's old messages as live orders.
                # ...unless this turn's prompt already carries one.
                # A session only overflows once it is huge, which is
                # exactly the shape commands._execute_query primes on the
                # compacted-resume path, so the aborted attempt's prompt
                # very often already opens with a briefing built minutes
                # ago from the same thread. Asking for a second copy would
                # put ~12K tokens of the same quoted history twice into
                # the one session whose entire problem is size, under two
                # preambles that contradict each other about whether it
                # was resumed.
                briefing: str | None = None
                already_primed = config.PRIME_PREAMBLE_MARKER in (
                    instance.prompt or ""
                )
                if on_context_reset and not already_primed:
                    try:
                        briefing = await on_context_reset()
                    except Exception:
                        log.exception(
                            "Context-reset briefing failed for %s",
                            instance.id,
                        )
                elif already_primed:
                    log.info(
                        "Context-overflow restart for %s reuses the "
                        "briefing already in its prompt", instance.id,
                    )
                note = config.CONTEXT_OVERFLOW_NUDGE
                if briefing:
                    note = f"{note}\n\n{briefing}"
                # Same ordering rule as Layer 3 above: tell the user while
                # the id is still known, and record whether it landed so
                # commands.py doesn't post its terser duplicate on top.
                warning_posted = await _announce_session_loss(
                    on_recovery, instance,
                    error_text or "Prompt is too long", original_session_id,
                )
                instance.session_id = None
                instance._context_overflow_note = note
                # A `finally` for the same reason the thrash unmark has
                # one: _run_impl can raise or return without ever reaching
                # _build_command (the refuse-to-spawn short-circuit), and
                # an Instance that survives carrying a stale "your previous
                # session could not be continued" would open a cooldown
                # retry hours later with a lie at the top of its prompt.
                try:
                    fresh = await run.reenter()
                finally:
                    instance._context_overflow_note = None
                fresh.session_recovery_exhausted = True
                fresh.recovery_warning_posted = warning_posted
                # The abandoned attempt may have edited files before the
                # context blew out; the fresh one may never touch a file.
                return _carry_forward_work_record(result, fresh)
        return None

    async def _recover_model_limit(self, run: _RunAttempt) -> RunResult | None:
        """Fail over, or downgrade, on a limit that covers only the primary model."""
        # Model-specific limit (e.g. "You've reached your Fable 5
        # limit"): the account is still healthy for every other model,
        # so never cool the WHOLE account down for this.  Route: another
        # account's quota for the same model first; when every account
        # is model-limited, downgrade to MODEL_FALLBACK and keep going.
        # Subscription-only — this path never touches pay-per-use.
        # MUST run before the generic usage-limit branch below: that
        # parser also matches this wording and would sideline the
        # account (blocking fallback-model runs too).
        result = run.result
        error_text = run.error_text
        instance = run.instance
        account_dir = run.account_dir
        provider = run.provider
        recovery_state = run.recovery_state
        on_progress = run.on_progress
        model_override = run.model_override
        if (
            result.is_error
            and provider.supports_account_failover
            and account_dir
            and _is_primary_model(model_override or instance.model)
        ):
            model_hit = provider.parse_model_limit(error_text)
            if model_hit:
                m_label, m_reset = model_hit
                log.info(
                    "%s limit for %s on %s, resets at %s",
                    m_label, instance.id, account_dir[-20:], m_reset,
                )
                self._set_model_cooldown(account_dir, m_reset)
                instance._accounts_tried.add(account_dir)

                async def _downgrade_retry(headline: str) -> RunResult:
                    """Rerun on the fallback model.  Accounts are only
                    model-limited, not dead, so the tried-set is cleared
                    for the retry.  Loop-safe: gated on the
                    model_downgrade recovery key by the caller, and the
                    pre-emptive downgrade at the top of _run_impl sees
                    the cooldown just recorded, so the retry cannot land
                    on the primary model again this run.
                    """
                    recovery_state.add("model_downgrade")
                    # Also claim the notice key: the retry's pre-emptive
                    # downgrade would otherwise post a second, near-
                    # identical "Running on <fallback>" message right
                    # after this one.
                    recovery_state.add("model_downgrade_notice")
                    instance._accounts_tried = set()
                    await _notify(
                        on_progress,
                        headline,
                        f"Continuing on {config.MODEL_FALLBACK} "
                        f"until ~{m_reset.strftime('%H:%M')} UTC \u2014 "
                        "switching back automatically",
                        "during failover",
                    )
                    return await run.reenter()

                next_account = self._pick_account(
                    exclude=instance._accounts_tried,
                    avoid_model_cooldown=True,
                )
                if next_account and next_account not in self._model_cooldowns:
                    # Backup still has primary-model quota — same model
                    # there (hydration copies the session JSONL across).
                    log.info(
                        "Model-limit failover from %s to %s for %s",
                        account_dir[-20:], next_account[-20:], instance.id,
                    )
                    inner = await run.switch_account(
                        f"{m_label} limit on current account",
                    )
                    # Backup died before doing any work (auth-dead, or
                    # its own account-wide cap): this account can still
                    # run the fallback model — keep working instead of
                    # surfacing a raw error or stalling until a reset.
                    # _no_productive_work (not an empty-text test) is what
                    # keeps a real mid-work failure on the backup visible
                    # while still catching a CLI fatal error that landed
                    # in result_text.
                    if inner.is_error and _no_productive_work(inner):
                        if "model_downgrade" not in recovery_state:
                            return await _downgrade_retry(
                                f"{m_label} limit + backup unavailable"
                            )
                        if not inner.usage_limit_reset:
                            # Downgrade already spent — carry the model
                            # reset so the caller schedules the auto-retry
                            # countdown instead of a raw error.
                            inner.usage_limit_reset = m_reset
                            inner.session_id = (
                                inner.session_id or instance.session_id
                            )
                    return inner
                if "model_downgrade" not in recovery_state:
                    return await _downgrade_retry(
                        f"{m_label} limit reached on all accounts"
                    )
                # Downgrade guard already used this run — return with the
                # model reset stamped so the caller schedules a retry
                # countdown instead of hard-failing.  Deliberately skips
                # the generic branch below: an account cooldown here
                # would block fallback-model runs for no reason.
                result.usage_limit_reset = m_reset
                return result
        return None

    async def _recover_usage_limit(self, run: _RunAttempt) -> RunResult | None:
        """Fail over to the next account on a usage limit, else stamp the reset."""
        result = run.result
        error_text = run.error_text
        instance = run.instance
        account_dir = run.account_dir
        provider = run.provider
        # Usage limit: try next account before falling through to cooldown/PPU
        if result.is_error:
            reset_at = provider.parse_usage_limit(error_text)
            if reset_at:
                log.info("Usage limit for %s, resets at %s", instance.id, reset_at)
                # Mark this account as on cooldown — routed through
                # the helper so the value lands on disk too.
                if account_dir:
                    self._set_account_cooldown(account_dir, reset_at)
                    instance._accounts_tried.add(account_dir)
                # Try next account before giving up
                next_account = self._pick_account(exclude=instance._accounts_tried)
                if next_account:
                    log.info("Failing over from %s to %s for %s",
                             account_dir, next_account, instance.id)
                    # Keep session_id — _hydrate_session_for_account copies
                    # the JSONL cross-account so --resume continues the same
                    # conversation on the backup account.
                    inner = await run.switch_account(
                        "Primary account hit usage limit",
                    )
                    # If the failover target died before doing any work
                    # (e.g. paused/cancelled subscription -> 401), the turn
                    # is still fundamentally usage-limited. Carry the
                    # original reset time so the caller schedules the
                    # normal auto-retry countdown instead of surfacing the
                    # backup's raw auth error.  This is the t-6570 path:
                    # the old empty-output guard missed it because the CLI
                    # put its 401 in result_text (see _no_productive_work).
                    if (
                        inner.is_error
                        and not inner.usage_limit_reset
                        and _no_productive_work(inner)
                    ):
                        inner.usage_limit_reset = reset_at
                        inner.session_id = (
                            inner.session_id or instance.session_id
                        )
                        if looks_like_fatal_auth_error(
                            inner.result_text or inner.error_message
                        ):
                            inner.retry_reason = "backup_logged_out"
                    return inner
                # All accounts exhausted — fall through to cooldown/PPU
                result.usage_limit_reset = reset_at
                return result
        return None

    async def _recover_transient(self, run: _RunAttempt) -> RunResult | None:
        """Retry a transient error once after 30 seconds."""
        result = run.result
        instance = run.instance
        api_fallback = run.api_fallback
        # Auto-retry on transient errors (skip if already in API fallback to prevent loops)
        if (result.is_error
            and not api_fallback
            and is_transient_error(result.error_message or result.result_text)
            and instance.retry_count == 0):
            log.info("Transient error for %s, retrying in 30s", instance.id)
            instance.retry_count = 1
            await asyncio.sleep(30)
            return await run.reenter(api_fallback=False)
        return None

    async def _recover_account_failure(self, run: _RunAttempt) -> RunResult | None:
        """Sideline an unusable account and fail over to the next one."""
        # Account-level failure (auth / cancelled subscription / can't start):
        # no reset time, so fail over to another account.
        #  - confident auth-pattern match -> failover + cooldown. The match
        #    is computed independently of the agnostic guard, so it fires
        #    even if both phrases co-occur; the cooldown itself is gated by
        #    >1 account (inline note below).
        #  - else no-turns fallback (hard error before turn 1, wording we
        #    don't match yet) -> failover ONCE, NO cooldown, and ONLY when
        #    NOT account-agnostic (model unavailable / bad flag), so a blip
        #    can't cascade-cool both accounts. (usage-limit and "No
        #    conversation found" are handled above.)
        # NOTE on stamping usage_limit_reset here: it schedules an
        # auto-retry, it does NOT spend money.  The paid-API fallback is an
        # opt-in button offered by schedule_cooldown_retry (and suppressed
        # entirely for autopilot chains), never automatic — the older
        # comment claiming otherwise was wrong.  The real rule is that the
        # retry time must be a REAL wall-clock moment when an account frees
        # up (another account's cooldown expiry), never a fabricated one:
        # retrying against a dead account on a made-up schedule just burns
        # the three attempts and fails anyway.
        result = run.result
        error_text = run.error_text
        instance = run.instance
        account_dir = run.account_dir
        provider = run.provider
        if result.is_error and provider.supports_account_failover and account_dir:
            # Strict form on purpose: error_text falls back to the whole
            # result_text (which is how the t-6570 401 was found at all),
            # so a plain is_account_unusable_error would sideline an
            # account for 24h because a session happened to WRITE about
            # expired tokens. A CLI auth error is one line.
            confident = looks_like_fatal_auth_error(error_text)
            # Which kind of confident rejection: it decides the reason on
            # the alert and the wording of the dead-end message, and both
            # must read it the same way.
            org_disabled = confident and is_org_disabled_error(error_text)
            no_turns = (not (result.result_text or "").strip()
                        and not result.num_turns
                        and not is_account_agnostic_error(error_text))
            if confident or no_turns:
                # A confident auth failure cools the account down so it's
                # skipped while dead — even when there's no failover target
                # left, so an all-dead fleet lands on the clean "all accounts
                # cooled" spawn-refusal instead of re-spawning the last dead
                # account every task. Guarded by >1 account so a SOLE account
                # is never self-sidelined into that refusal. The no-turns
                # heuristic never cools down (cascade guard: a model/flag blip
                # that zero-turns on every account can't take the fleet dark).
                if confident:
                    # The alert is recorded even for a sole account: the
                    # >1 guard exists so we don't sideline the only account
                    # we have, not to hide the fact that it's signed out —
                    # which is precisely when the user most needs telling.
                    self._record_auth_alert(
                        account_dir,
                        REASON_ORG_DISABLED if org_disabled
                        else REASON_RUNTIME_401,
                    )
                    if len(config.CLAUDE_ACCOUNTS) > 1:
                        cooldown = datetime.now(timezone.utc) + timedelta(
                            seconds=config.ACCOUNT_AUTH_COOLDOWN_SECS
                        )
                        self._set_account_cooldown(account_dir, cooldown)
                        self._auth_cooldowns.add(account_dir)
                if not confident:
                    # Unmatched wording — log the text (whether or not a
                    # failover target exists) so we can graduate it to a
                    # confident pattern (and a real cooldown) once the real
                    # cancellation error finally surfaces.
                    log.warning(
                        "Account error unmatched by auth patterns "
                        "(no-turns heuristic) for %s: %s",
                        instance.id, (error_text or "")[:300],
                    )
                next_account = self._pick_account(
                    exclude=instance._accounts_tried | {account_dir}
                )
                if next_account:  # only fail over when there's somewhere to go
                    instance._accounts_tried.add(account_dir)
                    log.warning(
                        "Account %s unusable (confident=%s); %s, failing over "
                        "to %s for %s",
                        account_dir[-20:], confident,
                        "cooled down" if confident else "no cooldown",
                        next_account[-20:], instance.id,
                    )
                    return await run.switch_account(
                        "An account looks unavailable",
                    )
                if confident:
                    # Nowhere to fail over. Don't dead-end the turn on a
                    # raw 401 (the t-6570 symptom): if any other account is
                    # merely cooling down, come back when it frees up.
                    self._soften_auth_dead_end(
                        result, instance, account_dir,
                        org_disabled=org_disabled,
                    )
        return None
