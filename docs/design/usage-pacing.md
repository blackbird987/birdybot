# Usage pacing: start sessions at a pace the plan can afford

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
  (`AdmissionMixin._await_usage_headroom`, called first inside the
  concurrency semaphore in `ClaudeRunner.run`). How many is enough comes from
  `bot/claude/usage_pacing.py:pace_limit`, from the real figures
  (`bot/engine/plan_usage.py`) of the account the spawn would land on (`_pacing_account`, the same
  `_pick_account` arguments `_run_impl` uses, so a resume is read on its
  session's account):
  session-% tiers (`USAGE_PACE_SESSION_TIERS`), weekly pace (used more than
  the share of the week gone plus `USAGE_PACE_WEEK_SLACK_PCT`), a weekly
  ceiling, and a peak-hour cap. The tightest cap wins.
- **The cooldown loop releases parked retries one at a time**, at most one
  per `COOLDOWN_RETRY_STAGGER_SECS`, only when the runner has a usage slot
  free, ranked by `lifecycle.cooldown_retry_rank`: typed messages, then
  workflow steps, then late child reports. A SYSTEM NUDGE is dropped with a
  one-line note in the thread rather than resumed from a cold cache.

Rules that must not drift:

- **The cap never drops below one.** A hold only ever waits for a running
  session to finish, so it cannot deadlock. It is also bounded
  (`USAGE_PACE_MAX_WAIT_SECS`), then starts anyway, like the memory hold.
- **No reading is no usage cap.** The endpoint is undocumented and the access
  token is often expired between runs (the bot never refreshes it). Missing
  figures must not stop work; only the peak-hour cap still applies.
- **A figure whose window already reset counts as zero.** plan_usage serves
  the last good reading for up to three hours.
- **The slot is counted on the statement right after the hold**, with no
  await between the hold's last check and the increment, and given back in a
  `finally`. An await there lets two waiters take one slot.
- **bot.claude does not import bot.engine.** The reader is wired by `app.py`
  (`runner.set_usage_reader(plan_usage.reading_for_pacing)`).
- **Only a prompt that starts with the nudge text is dropped.** A typed
  message that quotes a nudge in its background briefing is still the user's.

Knobs: `USAGE_PACE_*` and `COOLDOWN_RETRY_STAGGER_SECS` in `bot/config.py`.

Harness: `python scripts/test_usage_pacing.py`.
