+++
id = "2026-09-21-oomd-kills-while-free-memory-reads-fine"
title = "oomd killed the whole unit while every free-memory rule read OK"
date = "2026-09-21"
status = "stands"
question = "Why did the bot's unit die with memory apparently free?"
verdict = "On 2026-09-14, 09-20 and 09-21 systemd-oomd killed the unit on the cgroup's own PSI in app.slice, 90.79% against 80% in the quoted kill, with about 9 GB or more still available and machine-wide pressure in single digits. A rule built on free memory cannot see this."
tags = ["memory"]
source = { file = "docs/design/oomd-session-scopes.md", section = "opening section" }
+++
Anyone reading a kill on this machine starts from "was memory free", and the answer is misleading. Second source: docs/design/app-slice-ceiling.md, read for later changes.

The 2026-09-21 kill: `app.slice` at 90.79% > 80% for over 20s, Avg10 94.76, 10.1G current. Same shape on Sep 14 20:34:46 and Sep 20 10:39:47, six live sessions each time. The bot had no reader for `app.slice`, and `/proc/pressure/memory` is machine-wide.

The same thing recurred at a different cgroup on 2026-10-08, with 11-12 GB free: see 2026-10-08-a-memory-ceiling-at-the-working-set-caused-the-kill.

What this does not establish. The note gives no count of how often this happens, and does not say the guard's later readers would have prevented any of these. It says nothing about kernel OOM kills, a different killer.
