# Ark and Control Room refresh

How The Ark's pinned dashboard and every repo's Control Room get redrawn,
and the invariants that keep that inside Discord's rate limits.

## The incident

By November 2026 The Ark carried 23 pinned dashboards, 22 of them frozen,
and the four log files held about 67,000 rate-limit and 429 lines. Two
causes, both in `bot/discord/dashboard.py`:

1. The dashboard edit caught `(discord.NotFound, discord.HTTPException)` and
   treated both as "the message is gone". `NotFound` is a subclass of
   `HTTPException`, so every 429 and 5xx posted and pinned a fresh dashboard
   and repointed `dashboard_message_id` at it. The old one stayed pinned and
   stopped updating. The same bug had already been fixed once in the monitor
   posts (commit 6362bdd, `monitoring._edit_or_recreate`).
2. Every refresh (every instance start and finish, every chain step, every
   button) also fired one unbounded `create_task` per Control Room, 31 of
   them, and handed each the account-wide usage bar. The usage bar changes
   all the time, so every room differed on every pass and every edit went
   out. 98% of the 429s were these Control Room PATCHes.

## The shape now

One object, `DashboardRefresher` (on the bot as `_dashboard`), owns every
redraw. `bot._refresh_dashboard()` is a thin front for it.

- `request(sweep=False)` returns at once. The first request schedules a pass
  `DEBOUNCE_SECONDS` (3s) out; any that arrive before it fold into it, and any
  that arrive during it make exactly one follow-up pass. Passes start at least
  `MIN_INTERVAL_SECONDS` (15s) apart.
- `refresh_now()` runs a sweep pass at once, still one pass at a time. It is
  for a person waiting on it (the Ark's Refresh button) and for startup.
- The 5-minute periodic loop (`start_periodic_refresh`) only calls
  `request(sweep=True)`.

A pass first builds the "Needs you" list once (`waiting.collect`, in a
thread, with the thread index snapshotted on the loop; see `needs-you.md`).
If that list cannot be built, the pass leaves The Ark and every room as they
are. Then it does two things:

1. **The Ark.** Render, digest the embed and buttons (`forums.control_digest`)
   and skip the edit when the digest matches the last one that landed. The edit
   is a single PATCH through `get_partial_message`. Only `NotFound` posts and
   pins a new dashboard (`edit_or_recreate_dashboard`). Any other error logs a
   warning, keeps the stored id and clears the digest, so the next pass retries
   the same message.
2. **Control Rooms.** Each repo has a cheap signature built from the store with
   no rendering: running ids, the room's slice of the waiting list (item keys,
   times, details), the five latest completed ids,
   today's cost, the deploy state, the message id and the thread count. A
   normal pass redraws only the rooms whose signature moved. A sweep (startup,
   the periodic tick, Refresh) renders every visible room and the personal
   ones, which picks up what the signature misses, such as a branch switch or
   a running line's elapsed minutes. Hidden repos are never redrawn by the
   refresher (`store.is_repo_dormant`, see `repo-hide.md`). A direct
   `refresh_control_room(name)` call still works on any repo.

Inside `ForumManager._refresh_control_message`, which every Control Room
redraw goes through (the refresher and the direct callers alike):

- At most `CONTROL_REFRESH_CONCURRENCY` (2) run at once.
- It renders first and skips the edit when the digest matches what that
  message last showed. Digests are keyed by message id, kept in memory only,
  and stored only after an edit lands, so a restart redraws everything once.
- The fast path edits through the cached thread with one request. The slow
  path (thread not cached, a legacy thread name, or `NotFound` on the fast
  path) keeps the old behaviour: fetch the thread, migrate a legacy name, and
  heal a deleted thread or message. A non-NotFound error on the fast path
  stops there and reports failure: a fetch on top of a 429 only adds to it.
- It returns True when the room shows the current render, and the refresher
  keeps a room's signature only then. A failed room is retried next pass.

Control Rooms no longer carry the usage bar. It is account-wide, it lives
in The Ark, and it was the reason every room changed on every pass. A room
still shows Today's spend for its own repo.

## Startup pin cleanup

`ClaudeBot._cleanup_lobby` (run once after the first refresh) unpins every
dashboard pin except the live `dashboard_message_id`, before its purge, so
the frozen copies become ordinary messages the purge clears. With no stored
id the newest dashboard stays. A dashboard is a message by the bot with a
"The Ark" embed title or an `ark:` button (`is_dashboard_message`). Other
pins are never touched. If the live dashboard is not pinned, it is pinned
again. It pauses after every 5 unpins.

## Invariants

- Nothing redraws The Ark or a Control Room outside the refresher except a
  direct `refresh_control_room` / `refresh_user_control_room` call, and those
  go through the same concurrency cap and unchanged skip.
- Only `NotFound` recreates the dashboard. Do not widen the except again.
- An unchanged render sends nothing; a failed edit never leaves a digest
  behind that would skip its retry.
- No usage bar in Control Rooms.
- The Ark is the "Needs you" list, a Status field and Usage, with version and
  PC in the footer (`needs-you.md`). Every time in it is `<t:EPOCH:R>` markup,
  never a relative time written out, so an unchanged state gives an unchanged
  digest. The waiting list is computed once per pass and each room gets its
  slice.

## What this does not establish

The harness (`scripts/test_ark_refresh.py`) pins the coalescing, the room
selection, the skip, the cap, the NotFound-only recreate and the pin cleanup
against fakes. It does not show the live rate-limit count drop; that is
measured from `data/logs/bot.log` after a restart ("Global rate limit has been
hit" lines per hour, before and after).
