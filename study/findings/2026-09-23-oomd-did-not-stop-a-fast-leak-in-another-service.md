+++
id = "2026-09-23-oomd-did-not-stop-a-fast-leak-in-another-service"
title = "oomd did not stop a fast leak in another service"
date = "2026-09-23"
status = "stands"
question = "Does systemd-oomd armed on app.slice protect the machine from one application leaking?"
verdict = "It did not on 2026-09-21 16:32 or 2026-09-23 02:07: smartmoney-api.service reached 13.2 and 10.6 GB and the kernel global-OOMed with swap at 100% and oomd armed at 80%. The 20-second stall reason is the note's explanation, not a measurement of oomd's timing."
tags = ["memory"]
source = { file = "docs/design/app-slice-ceiling.md", section = "opening section" }
+++
Every bot rule at the time bounded the bot, and neither death was a session. Second source: docs/design/oomd-session-scopes.md, which mentions a 13.5 GB OOM kill of an unrelated service on 2026-09-21.

The log says `CONSTRAINT_NONE`, `global_oom`: no cgroup limit was reached, because `app.slice` and `user@1000.service` both read `MemoryMax=infinity`. The kernel picked its victim from the whole task table, desktop in scope.

The note's reading: oomd requires twenty seconds of sustained stall, and a runtime allocating gigabytes in seconds with no swap runway reaches global OOM first. The response was a ceiling, `MemoryHigh=25G`, `MemoryMax=27G` on `app.slice`.

What this does not establish. That the ceiling has since stopped such a leak: the note records no later event. It does not say whether the leak in that service was fixed. The two sources give 13.2 GB and 13.5 GB for 2026-09-21 and do not say whether these are the same event.
