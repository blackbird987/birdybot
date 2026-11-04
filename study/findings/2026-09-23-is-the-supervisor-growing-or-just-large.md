+++
id = "2026-09-23-is-the-supervisor-growing-or-just-large"
title = "Is the supervisor leaking, or is it just larger than its ~250 MB"
date = "2026-09-23"
status = "open"
question = "Is the supervisor's growth past ~250 MB a large working set or a slow leak?"
verdict = "Unanswered in the note: it read 1.07 GB after 24 hours on 2026-09-23 and one reading cannot separate the two. The log now records a curve. This migration did not read that curve."
tags = ["memory"]
source = { file = "docs/design/app-slice-ceiling.md", section = "The supervisor's size is recorded, not guarded" }
+++
The ~250 MB figure is an argument in this repo: it is why the service keeps the default CPU weight while the sessions slice carries 20, and why its `MemoryMax` stays large. Second source: docs/design/cpu-io-weights.md and docs/design/oomd-session-scopes.md, which both state ~250 MB, the second alongside a 234 MB reading on 2026-09-22.

To answer: `grep 'Supervisor footprint' data/logs/bot.log`, one line every `SUPERVISOR_MEM_LOG_MINS` (30), with a warning past `SUPERVISOR_MEM_WARN_MB` (1536). The note wants this to stay a recorder: no reap, no restart, no ceiling.

What this does not establish. Anything about current size: the 1.07 GB is one reading from 2026-09-23. It may be answerable today from the log, which this migration did not read.
