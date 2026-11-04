+++
id = "2026-10-15-reopened-usage-windows-gone-in-minutes"
title = "Reopened five-hour windows ran out in minutes from the parked backlog"
date = "2026-10-15"
status = "stands"
question = "Why did every five-hour window on 2026-10-15 run out minutes after it opened?"
verdict = "Per the note, not an exponential limit: the cooldown loop released about sixteen parked sessions in the minute each window reopened, each re-reading its history from an expired cache. Windows ended after 9, 8, 4 and 3 minutes; the last two also fell in weekday peak hours, not separated out. No per-session cost figures are given."
tags = ["usage"]
source = { file = "docs/design/usage-pacing.md", section = "the 2026-10-15 paragraph" }
+++
Read this before anything releases parked sessions together again. The staggered release came from this incident, and the owner kept it on when pacing went off. Second source: CHANGELOG.md, the 2026-10-15 pacing entry, which gives the same figures.

Resets were at 02:50, 07:50, 12:50 and 17:50 local. One account hit its weekly cap the same day. Each session re-parked itself on hitting the wall, and the backlog grew every round. The last two windows also fell in Anthropic's weekday peak hours (5-11 Pacific), when session limits drain faster, and the note does not separate how much of those two was peak drain.

What this does not establish. The cause is the note's account: neither source shows a cost per session or tries the backlog on and off in a controlled way. Neither gives a measurement of a window after the fix. This record does not say how a stampede would show on an account with only a weekly window.
