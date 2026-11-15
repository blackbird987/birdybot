+++
id = "2026-11-15-control-room-edits-not-the-ark-were-nearly-all"
title = "Control Room edits, not The Ark, were nearly all of the bot's 429s"
date = "2026-11-15"
status = "stands"
question = "What kept the bot on Discord's global rate limit, and why did The Ark collect 23 pins?"
verdict = "Unbounded per-room edits carrying the usage bar caused the 429s; treating any HTTPException as a deleted message caused the pins."
+++
The owner had stopped looking at The Ark. Before redesigning what it shows, it was worth knowing why it had 23 pinned dashboards and why the log was full of rate-limit lines, since a redesign on top of a refresh path that floods Discord would inherit the flood.

Read from the live bot on 2026-11-15: The Ark's pins (23, all bot dashboards, 22 no longer updating) and the four `data/logs/bot.log*` files (about 67,000 lines matching `rate limit|429`). Sorting the 429 request lines by route put about 98% on Control Room starter-message PATCHes, not on the dashboard. The code explained both. `_refresh_dashboard_impl` caught `(discord.NotFound, discord.HTTPException)` as "message gone", so each 429 or 5xx on the dashboard edit posted and pinned a new one. And each refresh fired one unbounded task per Control Room (31), each handed the account-wide usage bar, so every room differed every time and every edit went out. The fix (`docs/design/ark-refresh.md`) recreates only on NotFound, coalesces every redraw through one refresher, skips unchanged renders, caps room edits at 2 at once and drops the usage bar from rooms.

This does not establish how far the 429 rate falls after the fix; that is measured from the log an hour either side of the restart that ships it. The 98% share is from one reading of the current logs, not a trend. It also says nothing about whether The Ark's content is useful, which is the separate redesign question.
