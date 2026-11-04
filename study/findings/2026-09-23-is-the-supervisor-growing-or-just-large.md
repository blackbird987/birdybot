+++
id = "2026-09-23-is-the-supervisor-growing-or-just-large"
title = "Is the supervisor leaking, or is it just larger than its ~250 MB"
date = "2026-09-23"
status = "open"
question = "Is the supervisor's growth past ~250 MB a large working set or a slow leak?"
verdict = "Unanswered in the note: it read 1.07 GB after 24 hours on 2026-09-23 and one reading cannot separate the two. The log now holds a curve, which falls back on its own and reads larger with uptime, and does not settle it."
tags = ["memory"]
source = { file = "docs/design/app-slice-ceiling.md", section = "The supervisor's size is recorded, not guarded" }
+++
The ~250 MB figure is an argument in this repo: it is why the service keeps the default CPU weight while the sessions slice carries 20, and why its `MemoryMax` stays large. Second source: docs/design/cpu-io-weights.md and docs/design/oomd-session-scopes.md, which both state ~250 MB, the second alongside a 234 MB reading on 2026-09-22.

To answer: `grep -h 'Supervisor footprint' data/logs/bot.log*`, because the newest file alone holds under a day. One line every `SUPERVISOR_MEM_LOG_MINS` (30), with a warning past `SUPERVISOR_MEM_WARN_MB` (1536). The note wants this to stay a recorder: no reap, no restart, no ceiling.

Read on 2026-11-04, 2011 lines from 2026-09-23: RSS median 315 MB, 95th percentile 833, maximum 1130, none at the warning. By uptime, mean 318 MB under 6 hours, 358 at 48-96, 460 past 96. It falls unaided, 655 to 213 MB in 30 minutes on 2026-10-12, and the median is already past ~250 MB, which looks stale.

What this does not establish. That there is no leak: RSS leaves out swapped pages, so a fall could be pages moving to swap, and these lines do not show swap. The logs are gitignored and rotate, so these lines will not stay.
