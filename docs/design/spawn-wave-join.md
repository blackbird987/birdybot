# Spawn-Wave Join (`bot/discord/orchestrator.py`)

When a session fans work out with `/spawn`, the bot joins the whole wave back
to the parent instead of making the user do it.

- Child state is **derived**, never stored: `ThreadInfo.session_id` -> newest
  `Instance` for that session -> status + `needs_input`. A child that parked on
  a question is `blocked`, not `completed` (finalize marks both COMPLETED).
- The wave roster is `Instance.spawn_dispatched_thread_ids`, sealed with
  `spawn_wave_sealed` when the dispatch loop ends. **A wave is not joinable
  before it is sealed** — otherwise a fast-failing first child closes the wave
  while its siblings are still being created.
- A child's callback resolves the wave whose roster **contains that child**, not
  the newest wave — a parent can have two waves open at once (wave 1 resumes it,
  it dispatches wave 2).
- On close, the parent's resume prompt carries each child's **full report file
  path** (`Instance.result_file`), not an excerpt. The human-facing post gets
  the excerpts.
- Full wave -> parent auto-resumes (`ORCH_AUTO_RESUME`, default on), bounded by
  the existing 12-wave cap since `callback_resume` doesn't reset it. Partial or
  timed-out release -> manual "Resume parent" button.
- Sweep in `autonomy_loop` (every ~5 min): partial-releases a wave past
  `ORCH_WAVE_TIMEOUT_MIN` (default 45; `0` = wait forever), closes a
  fully-settled wave early (a killed child never calls back), and silently
  *retires* any wave older than `_WAVE_ABANDON_HOURS` (12) — which is what
  absorbs waves recorded before this feature existed.
- **The timeout is for a child that is gone, not one that is slow.** It only
  fires while no outstanding child has a live CLI process (`_child_is_live`,
  which asks the *runner* — a status field frozen on RUNNING by a crash would
  otherwise disable the timeout for the exact case it exists for), up to
  `ORCH_WAVE_MAX_MIN` (default 6h; `0` = no ceiling). Age-only, it guillotined
  the conductor's 3h bench children at 45 minutes on every wave.
- **A report that lands after its wave closed is still delivered.** A released
  wave used to swallow the straggler's finalize on a `debug` line, so a child
  the partial release had written off finished, wrote a full report, and told
  nobody — which is what pushed the parent onto its self-wake fallback.
  `_deliver_late_child` posts it on its own (full report path + "your earlier
  'missing' conclusion is stale"), auto-resuming only when it was the last one
  outstanding, and records it in `Instance.spawn_late_reported_thread_ids` so a
  retry can't post it twice. No deadline is right for every child; this is what
  makes a wrong release recoverable instead of lossy.
- **Only a child the release could not account for may be reported late.**
  `release_wave` snapshots those into `Instance.spawn_wave_unresolved_thread_ids`
  (same await-free block as the released flag, so the two can't disagree), and
  `_deliver_late_child` requires membership. Without that gate, every later turn
  in a child thread — a user follow-up, a re-finalize — reads as a straggler and
  wakes the parent. Gating on "the release was partial" is the tempting wrong
  answer: a child paused by a usage limit is recorded FAILED and *settled*, so
  its wave closes as complete, and the report from its retry is exactly the one
  that must still arrive. Pre-existing waves carry an empty list and are inert.
- Blocked-child wake-ups are budgeted at `_MAX_BLOCKED_RESUMES` (4) per wave
  (`Instance.spawn_blocked_resumes`) — parent answers, child asks again, repeat.
- `[BOT_CMD: /reply thread=<id>]` + `~~~reply` body lets a parent answer its own
  blocked child. Target must be in this session's own dispatched ids.
- Harness: `python scripts/test_orchestrator_join.py`
