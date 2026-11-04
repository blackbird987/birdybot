+++
id = "2026-11-04-result-is-not-the-last-event-with-a-background"
title = "With a background task open, result is not the CLI's last event"
date = "2026-11-04"
status = "stands"
question = "Does the CLI's result event mean the run is over?"
verdict = "Not with a background job open, per a live probe of a background sleep on CLI 2.1.310: after result the CLI stays up, wakes the model when the job ends, and emits a second result. A background agent differs: its first result is held back until it finishes. Reading the first as final terminated the CLI at 30s, killing jobs in seven runs on 2026-11-03. Re-check on a CLI upgrade."
tags = ["cli", "watchdog"]
source = { file = "docs/design/orphan-safety-net.md", section = "The end-of-turn watchdog is not this net" }
+++
Anything new that reads the stream needs this, not only the watchdog. Second source: CHANGELOG.md, the entry on a session waiting on its own background job, and commit 6f243d5.

Separate cases, all from the note:

A background `sleep`, probed: tasks changed, "started", first `result` (num_turns 2), task notification, tasks changed to empty, `system init` again, "done", second `result` (num_turns 1).

A background agent: its first `result` is held back until the agent finishes.

A `run_in_background` job with no timeout, in a probe: it ran its full 23 minutes and woke the model. The CLI's 10-minute limit applies to a command it moved to the background itself.

The model's next call after a task ends, or a compaction, can run past 30s (94-126s seen).

What this does not establish. Any CLI version but 2.1.310, or how often the order holds, since the note reports one trace. The note gives no probe for the agent case. If a later CLI stops sending the tasks event, the note says behaviour falls back to the old kill.
