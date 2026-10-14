# Watches — event-triggered self-wake (`bot/engine/watches.py`)

A self-wake is a timer; a **watch** is the same wake with an *event* as its
trigger. A session that starts a long detached job arms one instead of guessing
a delay, and the thread stays visibly busy until the job actually ends.

- Directive (parsed post-turn, same rules as `/wake` and `/spawn`):
  ```
  [BOT_CMD: /watch pid=959988 log="artifacts/run.log" label="sculpt fit" progress="(\d+)/(\d+) frames" every=120 timeout=6h]
  ~~~watch
  The sculpt fit finished. Read the tail of artifacts/run.log, ...
  ~~~
  ```
  Capture the pid when launching: `setsid nohup ./job.sh > run.log 2>&1 < /dev/null & echo $!`
- Triggers: `pid=` (process gone) or `done=` (regex appears in the log tail).
  At least one is required, plus a non-empty body — otherwise nothing is armed.
  `timeout=` is a safety net, never the plan.
- **A timer may be days long.** Both ceilings — `/wake delay=` and `/watch
  timeout=` — are 30 days, not the 24h they were until 2026-09-08. Nothing else
  in the path had to change for that: a wake is an ordinary one-shot schedule,
  polled on the same 30s tick, persisted and never pruned, so its distance out
  was only ever the clamp's business. It is still bounded because a wake firing
  weeks later resumes a session whose CLI transcript may have been cleaned up by
  then — the runner recovers from "No conversation found" by running fresh, but
  the thread loses its history.
  Both directives share **one** duration grammar (`45s`/`90m`/`6h`/`3d`/`2w`, or
  a bare number of seconds), and so does the chip that renders them back. It
  lives in `bot/textutil.py` rather than next to `/watch`, because the chip
  renderer is in `bot.platform`, upstream of `bot.engine`, and a leaf module is
  the only home all three can import without closing a cycle. It is anchored, so
  `3days` falls back to the caller's default instead of parsing as its `3d`
  prefix. Before it was shared, `delay=3d` hit an `int(float(...))` and became
  the 180-second fallback in silence — the failure any new spelling here must
  not reintroduce.
- **Only an explicit directive arms a watch.** Heuristic wake-arming was ripped
  out twice for firing on prose that merely *discussed* a job — don't reintroduce
  it here.
- PID reuse is defended by capturing field 22 of `/proc/<pid>/stat` (start time)
  at arm time; a mismatched token reads as "gone", not "still running". Zombie
  (`Z`) also counts as finished.
- Firing does **not** add a second resume path: the poller calls
  `store.add_wake(..., next_run_at=now)`, so a tripped watch becomes an ordinary
  due wake and inherits the runaway cap, busy re-arm and unattended-turn nudge.
- One thing per thread: `add_watch` supersedes an existing watch, arming a watch
  calls `cancel_wakes`, and arming a `/wake` deletes an armed watch.
- Busy indication while it waits: the `active` forum tag is retained
  (`bot/discord/tags.py`), the 💤 idle prefix is suppressed (`bot/discord/idle.py`),
  and one heartbeat message **edits itself in place** (never re-posts — thread
  name edits are rate-limited, message edits are not) with a progress bar,
  elapsed time, log path, last log line and a "Stop watching" button
  (`watch_stop` in `bot/discord/interactions.py`).
- Persisted in `data/state.json` under `watches` / `watch_counter`, so a watch
  survives a bot restart. Polled by `Scheduler._check_watches` each 30s tick.
- Knobs: `WATCH_*` in `bot/config.py`. Harness:
  `python scripts/test_watch.py`

### A promise to report back is nudged, never auto-armed

`lifecycle.check_wake_request` is where a finished turn is judged, and a turn
that armed nothing has three possible endings:

- **Unattended dead-end** (a cooldown retry or self-wake fire with no
  `[TURN_COMPLETE]`) → `_nudge_or_stop` re-invokes it.
- **A false claim** ("Self-wake queued (~4 min)") with no directive parsed →
  notice only, `claims_self_wake`.
- **A bare promise** ("I'll report back when the tests finish") → since
  2026-08-31, `promises_continuation` + `_promise_nudge` re-invoke the session
  once with `_PROMISE_NUDGE_PROMPT`. Before that it fell through to "ended
  cleanly" and the thread died holding a promise nothing could keep.

It **nudges rather than arms** on purpose. `WAKE_PROMISE_RE` is the name a
deleted predecessor held: it *scheduled* a 3-minute wake off this same prose
and fired phantom re-checks on text that merely discussed a build. Re-invoking
the session keeps "only an explicit directive arms anything" true, and puts
the decision where the pid, the log path and the real duration are known. A
false positive therefore costs one turn that answers `[TURN_COMPLETE]`.

The detector has to survive this repo describing itself. Every guard in
`WAKE_PROMISE_RE` exists because a sentence in these docs, a review report or
a result file tripped it: a bare participle needs "in the background", that
participle needs a first-person subject or a clause start (so "the scheduler
is polling in the background" is prose, not a promise), a subjectless wait is
rejected after "is/are/was/were/to", and the first-person contractions require
their apostrophe — optional, and "id", "ill" and "im" read as "I'd", "I'll"
and "I'm". Any new alternative must be checked the same way, against the
archived result files rather than against invented examples.

The nudge stands down whenever the thread already has something to resume it
(an armed watch, a pending wake — which is how a tripped watch looks —, a
worktree build, a context-exhausted session), shares `MAX_CONSEC_NUDGES` and
one body (`_nudge_once`) with the unattended nudge so the two can't ping-pong
or drift, and loses to the claim notice when both would fire.

"Pending" means armed for *later*. `Scheduler._execute_wake` awaits the
resumed turn and deletes the row in its `finally`, so during a wake-sourced
turn the wake still in the store is the one being consumed —
`_thread_has_pending_wake` discounts it. Counting it would silence the nudge
for the likeliest case there is: a watch trips, the job is still running, and
the resumed turn promises to report back again. `eval._check_unarmed_promise` counts recurrences and
attributes them to `WAKE_GUIDANCE`, so `/evals` names the block that was
supposed to prevent it.
Harness: `python scripts/test_wake_promise_nudge.py`

### A directive inside another session's block belongs to that session

A body routinely contains a directive meant for its reader: a /spawn brief
telling the child to end with a /wake, a ~~~plan saying what the build should
arm, a ~~~reply handing a child the /watch it should use. On 2026-09-27
(q-18729) exactly that armed a 30-day wake on the *parent*, and cut the
child's brief off at the nested ~~~wake's closer, because every body parser
was a flat `~~~tag\n(.*?)\n~~~` and every directive scan read the text flat.

`bot/textutil.py` owns the fix, and every parser goes through it:

- **`find_tilde_block` finds a body by depth.** An opener is a line of `~~~`
  plus a tag, a closer is a line of exactly `~~~`; a nested block neither ends
  the outer one nor is found as a top-level block. An unclosed block has no
  body, so its directive reports "no body" instead of guessing an end.
- **`mask_tilde_bodies` hides bodies from directive scans.** Scans run on a
  copy with every top-level body blanked to spaces, newlines kept, so offsets
  index the original and the line-based quoted-example guards see the same
  lines. An unclosed block is masked to the end of the text, as an unclosed
  Markdown fence renders.
- **Mask before you scan, read the body from the original.** Wake, watch,
  spawn, chain, reply, /repo and /image all do this, and so do the promise,
  claim and `[TURN_COMPLETE]` scans: "I'll report back" in a child's brief is
  the child's promise, not this turn's. `eval._check_unarmed_promise` inherits
  it by calling the runtime predicates rather than re-parsing. A new directive
  parser that scans the raw text reintroduces the bug.

Harness: `python scripts/test_nested_directives.py` (replays the real
q-18729 result file when the installed bot still has it).
