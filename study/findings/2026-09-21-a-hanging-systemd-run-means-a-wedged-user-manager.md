+++
id = "2026-09-21-a-hanging-systemd-run-means-a-wedged-user-manager"
title = "A hanging systemd-run --user pointed at a wedged user service manager"
date = "2026-09-21"
status = "stands"
question = "When systemd-run --user hangs or a cgroup write reads back unchanged, is the code at fault?"
verdict = "On 2026-09-21 it was the user service manager, not the code: jobs all waiting, none running. systemctl --user daemon-reexec clears it, per the note. The note describes this from that one day."
tags = ["memory", "systemd"]
source = { file = "docs/design/oomd-session-scopes.md", section = "One live-machine trap worth writing down" }
+++
A later session debugging a hang here could spend its turn on the code. Second source: docs/design/cpu-io-weights.md, which gives another reason a weight can read back wrong (2026-09-22-cpuweight-20-did-not-hold-on-the-service).

The signs the note lists: `systemd-run --user` hangs, `systemctl --user show` disagrees with `systemctl --user cat`, a direct write into `cpu.weight` reads back unchanged. Check `is-system-running` (degraded), `list-jobs` (all `waiting`), and the manager's CPU.

Two descriptions in the note, which it does not say are the same event:
- Every job `waiting`, none `running`, behind one crash-looping unit, "seen twice on 2026-09-21".
- An unrelated service kernel OOM-killed at a 13.5 GB peak livelocked the manager at 95% CPU for seven minutes, which also stopped the browser opening tabs. docs/design/app-slice-ceiling.md has `smartmoney-api.service` at 13.2 GB that day; neither note says they are one event.

The note adds the cost to a running bot: a cached "scopes work" answer turned every later spawn into a session that never starts, reaped only by the 4h silence watchdog.

What this does not establish. Why the manager wedges in general: only these causes were seen. That daemon-reexec is safe with work running: the note does not say.
