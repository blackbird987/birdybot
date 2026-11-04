+++
id = "2026-08-27-a-4h-age-only-cap-killed-a-working-run"
title = "A 4h age-only cap killed a run that was still working"
date = "2026-08-27"
status = "shut"
question = "Can a run be reaped on age alone at 4 hours?"
verdict = "Ran and failed: on 2026-08-27 the 4h cap killed q-15433, a benchmark whose last output was about five minutes old. It had fired twice ever; the new rule still catches the other kill (43 minutes silent). Reaping now needs 30 minutes of silence past 4h. Covers the 4h cap only: a 24h age-only backstop stays on by design."
tags = ["watchdog"]
source = { file = "docs/design/orphan-safety-net.md", section = "Why" }
+++
Second source: commit b6fef7c, and the CHANGELOG.md entry that names the same regression.

The note argues that raising the number would only move the kill, since a bench farming out serial subagent batches can run all day. That is reasoning: no higher cap was run.

A separate limit, a separate event, in docs/design/spawn-wave-join.md: the spawn wave timeout at 45 minutes was also age-only, and the note says it "guillotined the conductor's 3h bench children" on every wave. It now fires only while no outstanding child has a live process, up to a 6h ceiling. That limit releases a wave to its parent. It is not this reaper and shares no knob with it.

What this does not establish. That 30 minutes of silence is the right number: it still catches q-15010, and nothing else is reported. A job quiet for 30 minutes past 4h is still reaped. The end-of-turn watchdog is a different check (2026-11-04-result-is-not-the-last-event-with-a-background).
