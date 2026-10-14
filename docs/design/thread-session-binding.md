# A thread must always know its session

Every resume path — the next user message, a fired self-wake, a tripped
`/watch`, a post-reboot replay — reads `ThreadInfo.session_id`. A turn that
finishes without writing that back is a turn the thread can never continue,
and the failure is *silent*: the wake fires, finds nothing, and drops.

Three rules, all pinned by `scripts/test_cooldown_session_bind.py`:

- **The bind happens before the cooldown early-return.** A usage limit is a
  pause, not an ending: `_do_cooldown_retry_locked` resumes that exact
  `session_id`. `commands._execute_query` used to return to schedule the retry
  *first*, so a limited turn never bound at all. On 2026-08-27 that lost three
  overnight children — limit at 00:38, retried on the backup account at 01:55,
  work finished by 02:45, wake-ups dropped as "gone/sessionless" while the
  instances held a perfectly resumable id. The bind is wrapped in a try/except
  precisely *because* it moved above the retry scheduling and the result
  delivery: a failing state write must not cost the turn its retry.
- **`lifecycle.run_instance` does not bind, deliberately.** A workflow step's
  session belongs to the step, not to the conversation, so the chain runner
  must not rewrite the thread's binding. Every *other* caller of
  `run_instance` does own the conversation and has to top the thread up
  itself. The cooldown auto-retry (`app`), `/retry`, the Retry button,
  continue-on-pay-per-use, and the `/bg` and `/release` task runner (the
  command modules) all do. The first four once did not, so re-running the
  work as many times as you liked never restored a lost binding. Pay-per-use is the sharpest of the four: it is the manual
  twin of the cooldown retry, offered on the same usage-limit card. The
  cooldown retry also has to call `attach_session_callbacks` on its ctx, or
  it has no binding mechanism at all — the same omission already fixed once
  for post-reboot replays, see the comment on `_replay_to_thread`.
- **That top-up fills a gap; it never rebinds.** `backfill_thread_session` is
  the one implementation they all share (a `/bg` build is refused by the
  worktree rule below, a `/release` on master is not). Chain steps hit usage
  limits too and land in the same retry function, and `/retry <id>` can be pointed at an
  instance belonging to another thread. Rebinding from there would let a plan
  or review step amputate a thread's chat history on the retry path while
  never doing so on the normal one, so the write only happens into an empty
  `session_id`. Worktree builds are refused outright — an isolated build
  session must not become a thread's chat session even when the slot is free.
  The harness asserts this structurally: *every* `lifecycle.run_instance`
  callsite in `app.py` and the command modules must be followed by a backfill, so a
  fifth caller added later fails the suite instead of silently losing threads.

`should_bind_session` is where the eligibility rule lives, and it stays narrow.
Success binds; a usage limit binds; **no other error does.** A crashed or
recovery-exhausted run can emit a *fresh* `session_id` carrying none of the
thread's history, and adopting that amputates the conversation. One deliberate
seam: a run that recovered onto a fresh session and *then* hit the limit does
bind, because the old id is already unreachable and the retry resumes the new
one.

`on_self_wake` distinguishes "thread gone" (drop) from "thread alive but
sessionless" (dispatch cold, log at WARNING). Lumping them together is what
made the loss invisible for eight hours.
