+++
id = "2026-09-22-cpuweight-20-did-not-hold-on-the-service"
title = "CPUWeight=20 did not hold on claude-bot.service"
date = "2026-09-22"
status = "stands"
question = "Why did CPUWeight=20 on claude-bot.service read 100 live while the unit files said 20?"
verdict = "The note gives two reasons and does not reconcile them: a --runtime drop-in discarded when an oomd kill stopped the unit, and uresourced, seen live on 2026-09-22, rewriting the service to 100 many times a second. The 20 now sits on app-claudesessions.slice, which read 20 live."
tags = ["cpu", "systemd"]
source = { file = "docs/design/cpu-io-weights.md", section = "Bot versus you" }
+++
Someone putting a weight back on the service would fight this. Second source: docs/design/oomd-session-scopes.md, where a direct `cpu.weight` write reading back unchanged is listed as a sign of a wedged manager, a third reading of a similar symptom.

The two accounts in one section:
- `--runtime` lives in `/run` and systemd discards it when the unit stops, and an oomd kill stops the unit. On 2026-09-21 the live cgroup read `cpu.weight=100` while both unit files said 20 and `systemctl show` agreed with them. The note says this cost a week.
- `uresourced --user` (PID confirmed through its D-Bus name on 2026-09-22) calls `SetUnitProperties` on the service many times a second, burning about 14% of a core. "That, and not a stale drop-in, is why the 20 never held there."

The second sentence reads as replacing the first, but the note leaves both standing.

The incident behind the weights, 2026-09-08 on 12 cores: load average 110, one `VBCSCompiler` at 605% CPU with six sessions live, memory PSI 3%, 11 GB or more available.

What this does not establish. Why `uresourced` targets the service and not the slice: the note only says it does not touch the slice. Whether that still holds on the current Fedora. No measurement of desktop responsiveness with the weights in place is given.
