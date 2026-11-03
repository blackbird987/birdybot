# The orphan safety-net (age + silence)

A run is never killed for being *old*. It is killed for being **silent**, and
its age only decides when we start asking. Three knobs, all in `bot/config.py`:

- `MAX_PROCESS_LIFETIME_SECS` (4h) — age past which the watchdog begins
  checking. On its own it kills nothing.
- `MAX_PROCESS_SILENCE_SECS` (30m) — how long a run past that age must have
  produced **no output at all** before it is reaped. This is the actual trigger.
- `MAX_PROCESS_HARD_LIFETIME_SECS` (24h, `0` = off) — age-only backstop, so a
  process that heartbeats forever without finishing is not immortal.

Why: on 2026-08-27 the age-only cap killed q-15433, a four-hour benchmark that
had produced output **five minutes earlier**, had not gone quiet for even sixty
seconds in its final two hours, and was sitting at 350 MB with live HTTPS
connections open. Raising the number would only have moved the guillotine — a
bench that farms work out in serial subagent batches can legitimately run all
day. Only two lifetime kills had ever fired; the other (q-15010) had been
silent for 43 minutes, which the new rule still catches.

The reap keeps the work. It used to return a bare `RunResult`, so four hours of
real work rendered as an empty red FAILED card. Both watchdog reaps — this one
and the memory guard's — now go through `_reaped_result_base`, which is where
"what a reap must preserve" is written down once: the recovered last assistant
text, the tools used (the chain reads that list to decide whether a build
changed code at all), the cost/token counters, and the `session_id` captured
from the init event, without which Retry starts over instead of resuming. Only
`error_message` differs between the two. `num_turns` is deliberately **not**
synthesised; the account-failover heuristic reads `>1 turn` as proof the
account took the turn.

Three things that must not drift:

- The failure wording **must contain the phrase "lifetime limit"**.
  `parser.is_account_agnostic_error` matches on it to suppress the no-turns
  failover heuristic — otherwise a reaped run is handed to the backup
  subscription to burn the same hours again.
- The session is told **before** `proc.terminate()`, not after. Terminating
  closes the CLI's stdout, which ends the reader loop, whose `finally` cancels
  the watchdog — same ordering rule as `_reap_session` in the memory guard.
- The reap's return must stay **below** the two stand-downs, which both reaps
  share: a `result` event proving the turn finished anyway, and the session
  being in `_intentional_kills`. A user's Kill landing inside the reap window
  otherwise renders as a red FAILED card — and a failure with no turns is the
  account-failover branch's signature, so it can be restarted on the backup
  subscription. Same bug class as v0.101.11.

`WATCHDOG_TICK_SECS` (10s) is the poll cadence for this, the stall warning and
the memory guard. It exists so the harness can scale the whole watchdog down
instead of sleeping through real hours.

Harness: `python scripts/test_lifetime_cap.py`

## The end-of-turn watchdog is not this net

A separate, much shorter check lives in the reader loop in `core.py`: it ends a
CLI that stays silent for `END_OF_TURN_GRACE_SECS` (30s) after its turn ended,
because `claude -p` can fail to close stdout and sit there for hours (q-8396).
It reports success, built from what the stream already carried.

`result` is not always the last event. With a background task open, the CLI
stays alive on purpose, wakes the model when the task ends, emits a second
`result` and exits 0. Until 2026-11-04 the watchdog took that wait for the hang:
it terminated the CLI at 30s, which killed the job, and reported the "started"
message as the outcome. Seven runs in one day of logs, some merged. A live probe
on CLI 2.1.310 gives this order for a background `sleep`:

```
background_tasks_changed [task]   tool_result "running in background"
assistant "started"   result (num_turns 2, cost .00397)
task_updated   task_notification   background_tasks_changed []
system init (again)   assistant "done"   result (num_turns 1, cost .00433)
```

What follows from that, and must not drift:

- **`background_tasks_changed` is the authority**, kept in
  `_Watchdog.background_tasks`. While it lists anything, the silence limit is
  `END_OF_TURN_BACKGROUND_MAX_SECS` (1h) instead of the grace. If a future CLI
  stops sending it, behaviour falls back to the old kill, not to a hang.
- **The last task ending disarms the watchdog.** The model is about to take a
  turn, and that call or a compaction can run past 30s (94-126s seen). Its own
  `result` re-arms it. A CLI that hangs in that gap falls to the stall warning
  and to the lifetime net above.
- **The wait is not a stall.** No "quiet, /kill if stuck" card while it lasts;
  the card names the job. Both limits still bound it.
- **A `result` is not final while a task is open or just ended.**
  `_turn_completed_successfully` says so, or a job that hits the memory ceiling
  stands the reap down and is reported as its own "started" message.
- **Passing the ceiling adds a line to the report** naming the job, because the
  last message was written expecting to hear back and terminating the CLI stops
  the job. A job meant to outlive the session belongs in `/watch`. The ceiling
  is the only bound short of the lifetime net: the CLI's 10-minute limit applies
  to a command it moved to the background itself, and a `run_in_background` job
  with no timeout ran its full 23 minutes and woke the model in a probe.

The last `result` wins in `extract_result`: its cost is already cumulative, while
`num_turns` is per `result` and is summed.

Harness: `python scripts/test_runner_watchdog.py`
