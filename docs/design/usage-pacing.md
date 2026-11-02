# Usage pacing: start sessions at a pace the plan can afford

**Off by default since 2026-11-02, along with the peak-hour gate and its DMs.**
Anthropic removed the five-hour limit for verified Ultra accounts starting
2026-10-30. Confirmed live on 2026-11-02: a minimal CLI call on `~/.claude-main`
returned a `rate_limit_event` whose `unifiedWindows` holds only `seven_day`, with
no `five_hour`. The session-window reasoning below no longer applies to this
account; it stays here because the code stays, and for any account that still has
the window.

- **Off:** the session-% tiers, the peak-hour cap, the hold and the fast lane
  (`USAGE_PACE_ENABLED`), and the Run now / Queue / Cancel gate and the 5am/11am
  PT DMs (`USAGE_PEAK_GATE_ENABLED`, read in `_offer_gate` and
  `StartupMixin._start_usage_notifier`).
- **Still on:** the staggered, ranked release of parked retries
  (`COOLDOWN_RETRY_STAGGER_SECS`, `lifecycle.cooldown_retry_rank`), which the
  owner kept on purpose. Also limit detection, account failover and parking,
  account alerts, `MAX_CONCURRENT`, the memory limits, the usage queue's drain
  (an old entry and its buttons still work) and the plan usage figures.
- **The cooldown loop's slot check needs its own off branch.** With pacing off
  the hold returns `UsageSlot(None)`, so running sessions are counted under the
  `None` key while `usage_slot_free` would look at the real account, see zero and
  report a slot with `MAX_CONCURRENT` sessions running. Retries would then stack
  behind the semaphore and several could start together as slots free, which
  defeats the stagger. So with pacing off `usage_slot_free` reports whether the
  semaphore has a slot, and does not read usage or pick an account. The rule "only
  when the runner has a usage slot free" below therefore means "only when the
  concurrency ceiling has room" while pacing is off.
- **To re-enable:** set `USAGE_PACE_ENABLED=1` and/or `USAGE_PEAK_GATE_ENABLED=1`
  in `.env`, then reboot. They are independent. The harnesses switch both on
  explicitly, so the disabled code stays covered.
- **To turn one off again:** set it to `0`, do not comment it out. A reboot
  inherits the old process environment, so a removed line leaves the previous
  value live (the stale env item in `TODO.md`).

The rest of this note describes the behaviour with pacing on.

On 2026-10-15 every five-hour window ran out 9, 8, 4 and then 3 minutes after
it opened (resets at 02:50, 07:50, 12:50 and 17:50 local), and one account hit
its weekly cap. The limit was not exponential. The cooldown loop released the
whole parked backlog, about sixteen sessions, in the minute each window
reopened. Each one resumed a long conversation whose prompt cache had expired
during the five-hour wait, so each paid to re-read its entire history before
doing any work, and each re-parked itself when it hit the wall again. The
backlog grew every round, and the last two windows also fell in Anthropic's
weekday peak hours (5-11 Pacific), when session limits drain faster.

Two pieces fix it:

- **The runner holds a starting session while enough are running**
  (`AdmissionMixin._await_usage_headroom`, called in `ClaudeRunner.run`
  before the concurrency semaphore, so a held session does not occupy one
  of the `MAX_CONCURRENT` slots and lock out work for the other account). How many is enough comes from
  `bot/claude/usage_pacing.py:pace_limit`, from the real figures
  (`bot/engine/plan_usage.py`) of the account the spawn would land on
  (`AccountsMixin._spawn_account`, the one helper `_run_impl` also picks
  with, so a resume is read on its session's account):
  session-% tiers (`USAGE_PACE_SESSION_TIERS`) and a peak-hour cap, never
  the weekly figure. The tightest cap wins. Everything is per account
  (`_paced_running`, `_usage_waiters`, `_usage_hold_passed_at`, all
  keyed by the account the hold admitted the run on, `None` without
  rotation): a nearly spent account caps only its own sessions. Held sessions
  queue in arrival order and only the first in line for its account may
  start (`_usage_turn`); a newcomer or the cooldown loop gets a slot only
  when nobody is queued for that account. A run stays counted against the
  account it was admitted on even if recovery moves it to another one.
- **A fast lane for runs that may be quick.** On 2026-10-19, with the week
  ahead of pace, the main account allowed 2 at once and a typed message
  waited over two hours with 8 sessions queued ahead of it. Nothing tells a
  quick reply from a long job before it starts: prompt length, the session's
  size and the thread's last run all failed on the history. Turns tell them
  apart while it runs: half of all runs end within 10 turns and spend 15% of
  the usage between them, while among typed messages the 16 runs past 100
  turns spent 55%. So any run that is not a known-heavy step
  (`usage_pacing.FULL_QUEUE_ORIGINS`: build, /bg, sensor fix, merge resolver)
  skips the hold when one of its account's `USAGE_PACE_LIGHT_SLOTS` is free,
  counted in `_light_running` and not against the cap (`_admit` tries the
  fast lane first, then the capped one). The stream counts its assistant
  turns (`_count_usage_turn`, one per message id, since one message arrives
  as several events), and at `USAGE_PACE_LIGHT_TURNS` its count moves into
  `_paced_running`. Each run's lane lives in a `UsageSlot` in `_usage_slots`
  so the `finally` gives back whichever count it ended in. Held fast-lane
  runs (`_light_waiters`) take freed fast-lane slots in arrival order.
- **The cooldown loop releases parked retries one at a time**, at most one
  per `COOLDOWN_RETRY_STAGGER_SECS`, only when the runner has a usage slot
  free, ranked by `lifecycle.cooldown_retry_rank`: typed messages, then
  workflow steps, then late child reports. A SYSTEM NUDGE is dropped with a
  one-line note in the thread rather than resumed from a cold cache.

Rules that must not drift:

- **The cap never drops below one.** A hold only ever waits for a running
  session to finish, so it cannot deadlock. It is also bounded, per queue
  and not per waiter: a session starts anyway only when nobody has gone
  through the hold for `USAGE_PACE_MAX_WAIT_SECS` (`_usage_hold_passed_at`),
  and that start resets the clock, per account. Only the first in line times
  out, so this is safe only together with the queue: without it, sessions
  arriving as a slot frees could keep resetting the clock and overtake a
  waiter forever. A per-waiter limit releases every session
  that began waiting together in the same minute, which is the stampede again.
- **The weekly limit is not paced** (decided 2026-10-27). A weekly-pace cap
  used to hold an account to 2 sessions whenever its weekly % ran ahead of
  the share of the week gone, even with the five-hour window nearly unused.
  Holding back only delays the same work and wastes session-window capacity
  that does not carry over. Hitting the weekly cap is handled by the normal
  account failover and cooldown.
- **No reading is no usage cap.** The endpoint is undocumented and the access
  token is often expired between runs (the bot never refreshes it). Missing
  figures must not stop work; only the peak-hour cap still applies.
- **A figure whose window already reset counts as zero.** plan_usage serves
  the last good reading for up to three hours.
- **The slot is counted on the statement right after the hold**
  (`_take_usage_slot`, synchronous), with no await between the hold's last
  check and the increment, and given back in a `finally`
  (`_release_usage_slot`). An await there lets two waiters take one slot.
- **Promotion never stops a run.** A fast-lane run that reaches the turn limit
  may push the capped count past the cap. That only makes the line behind it
  wait longer; the cap still only gates starts.
- **A fast-lane start does not stamp the hold's clock**
  (`_usage_hold_passed_at`). It says nothing about whether the capped line is
  moving, and stamping it would keep a stalled line from ever timing out.
- **bot.claude does not import bot.engine.** The reader is wired by `app.py`
  (`runner.set_usage_reader(plan_usage.reading_for_pacing)`).
- **Only a prompt that starts with the nudge text is dropped.** A typed
  message that quotes a nudge in its background briefing is still the user's.

Knobs: `USAGE_PACE_ENABLED` and `USAGE_PEAK_GATE_ENABLED` (both off by default),
`USAGE_PACE_*` (the fast lane: `USAGE_PACE_LIGHT_SLOTS`, 0 turns it
off, and `USAGE_PACE_LIGHT_TURNS`) and `COOLDOWN_RETRY_STAGGER_SECS` in
`bot/config.py`.

Harnesses: `python scripts/test_usage_pacing.py`, `python scripts/test_usage_gate.py`.
