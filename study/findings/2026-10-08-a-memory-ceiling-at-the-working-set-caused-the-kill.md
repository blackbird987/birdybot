+++
id = "2026-10-08-a-memory-ceiling-at-the-working-set-caused-the-kill"
title = "A memory.high set at the working set was followed by oomd kills, twice"
date = "2026-10-08"
status = "shut"
question = "Does a memory.high set at the working set protect sessions from oomd?"
verdict = "Ran in production and failed at two levels: SESSION_MEM_HIGH_MB 6144 with sessions pinned at 6.0G (2026-09-22) and slice MemoryHigh 11G with 7.7 GB anon plus cache (2026-10-08). Ceilings above the working set are not covered: 8192 and 19G came after."
tags = ["memory"]
source = { file = "docs/design/oomd-session-scopes.md", section = "The rungs have to add up; The slice ceiling was the killer, again" }
+++
Second source: docs/design/app-slice-ceiling.md, read for later changes.

Per the note, `memory.high` makes the kernel reclaim continuously to hold a cgroup there, and that stall is what oomd kills on.

2026-09-22: 6144 MB per session against 5 concurrent, and a slice of `MemoryHigh=11G`. Peaks read 5.8G, 5.8G, 5.9G, then six sessions at exactly 6.0G. t-8711 died with `Pgscan: 141565`, the slice at 93.14% against its 70% limit.

2026-10-08: two scopes shot in half an hour, t-8999 and q-19143, with 11-12 GB of RAM free and slice pressure 82% and 72% against 70%. The log said "ours 7.7GB/11.0GB", but that is anon only. 5.0 GB of file cache plus 1.1 GB of dirty build output put `memory.current` on the 11G line. zram was also 100% full, so reclaim could only thrash file cache. The second zram device was added in the same fix, so the two changes are not separated.

What this does not establish. That these numbers are right for another workload or machine. The cause is the note's reading of the kill logs and counters, not a controlled comparison of ceilings. It does not say the later values are safe. docs/design/app-slice-ceiling.md applies the same rule one level up (25G over 18.8 GB anon) and records no test of that either.
