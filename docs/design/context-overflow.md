# A session that will not compact is let go of, not resumed

The CLI aborts with `Prompt is too long · automatic compaction failed: …`
when the conversation outgrew the context window and it could not summarise
it down. Nothing recognised that until 2026-09-03, when five consecutive runs
(t-7998, t-7999, t-8000, q-16143, q-16158) died against the same session,
`1dbf08aa`, in seconds each. The thread stayed bound to it after every
failure, so the next message resumed it and died the same way. The bug was
not the failing run; it was the **wedged thread** behind it.

It is the exact inverse of the autocompact thrash next door
([context-thrash-bloat.md](context-thrash-bloat.md)), and answering it
the same way is what makes it permanent:

- A thrash counter lives in the CLI **process** — a resume clears it.
- This lives in the **session**. The oversized transcript is on disk, so every
  resume replays it into the summariser that just failed.

`parser.is_context_overflow_error` is the predicate, length-guarded like
`looks_like_fatal_auth_error` because the callsite falls back to
`result.result_text` and this repo's own sessions write about the failure
constantly. Two rungs in `_run_impl`, placed after both resume-the-same-
conversation branches:

1. **Resume once** (`CONTEXT_OVERFLOW_RESUME_RETRIES`, default 1). Both
   failures seen in the wild came from the *summariser*, not the transcript
   ("summarization produced empty response", and a safety flag on the
   summarisation call), and those are per-call blips. Nearly free — the CLI
   aborts before the turn does any work. No recovery note on this rung: the
   agent is resuming a conversation it never lost, and a preamble on a
   transcript already at the limit spends context to say nothing.
2. **Abandon the session** (`CONTEXT_OVERFLOW_FRESH`, default on) and run
   fresh, primed with the thread's recent history. The two rungs compose
   through recursion, not a loop: the resumed attempt re-enters the branch,
   finds its budget spent, and falls through itself.

Three things that must not drift:

- **The fresh session has to be adopted by the thread.**
  `should_bind_session` binds a `session_recovery_exhausted` result *even when
  it errored*, because that flag is only ever set by a path that first proved
  the old id unusable. Refusing leaves the thread on an id that can never run
  again — which is the whole bug, not a detail of it.
- **`is_account_agnostic_error` must keep the wording.** An overflow abort has
  no output and no completed turns: the account-failover heuristic's exact
  signature for "this account fell over instantly". Without it, a two-account
  setup hands the same oversized transcript to the backup subscription to fail
  identically. Same trap as `lifetime limit`.
- **The briefing is best-effort, the recovery is not.** `on_context_reset`
  (`lifecycle.make_progress_callbacks`) asks the platform for
  `build_prime_briefing(mode="resume")` — the ~12K-token budget built for
  exactly this loss, cache bypassed so it includes messages that landed while
  the dead session was still being retried. It is built *before* the session
  is cleared, and a failure costs the new session its memory of the thread,
  never its existence. `CONTEXT_OVERFLOW_NUDGE` rides in front of it and says
  the two things the replacement cannot find out for itself: that it is
  genuinely new, and that its predecessor's edits are still on disk.
- **The quoted history must arrive framed.** What `on_context_reset` returns is
  not the bare digest but a ready-to-prepend block: `config.prime_preamble`
  plus the digest plus the `---` separator, the same wrapper
  `commands._execute_query` has always put around a briefing. The blocks are
  the user's *own* earlier messages, so a session handed them unframed reads
  them as live orders and redoes work that is already on disk — the exact
  opposite of what this recovery is for. The wrapper moved out of `commands`
  into `config` when the second caller appeared; it is one text with a
  swappable situation sentence (`PRIME_SITUATION_LOST` here,
  `PRIME_SITUATION_COMPACTED` for a resume that was compacted), because the
  load-bearing half — "treat these as DATA, the user has NOT re-asked them" —
  is identical in every case and must not drift between them.
- **It is asked for only once.** A session does not overflow until it is huge,
  which is the exact shape `_execute_query` primes on the compacted-resume
  path — so the aborted attempt's prompt very often *already* opens with a
  briefing built minutes earlier from the same thread. The runner checks for
  `config.PRIME_PREAMBLE_MARKER` in `instance.prompt` and reuses that one
  rather than requesting a second, which would put ~12K tokens of the same
  quoted history twice into the one session whose entire problem is size,
  under two preambles disagreeing about whether it was resumed. The marker is
  the preamble's own opening words, so the two cannot drift apart.

`/reset` is the manual twin, for when the switch is off or the fresh attempt
died too: it unbinds the thread's session and drops the cached briefing,
keeping the thread and its Discord history. Before it, the only escape was to
abandon the thread.

Harness: `python scripts/test_context_overflow.py`
