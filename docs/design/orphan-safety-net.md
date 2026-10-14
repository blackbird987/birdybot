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
