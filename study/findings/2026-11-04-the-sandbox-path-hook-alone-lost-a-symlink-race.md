+++
id = "2026-11-04-the-sandbox-path-hook-alone-lost-a-symlink-race"
title = "The sandbox path hook alone lost a read symlink race"
date = "2026-11-04"
status = "stands"
question = "Is the path hook enough on its own against a symlink swapped under a file tool?"
verdict = "Not for reads in the 2026-11-04 trial: the hook alone lost on 9 of 60 reads, and with the read block on 0 of 60. A separate Write test, 40 concurrent attempts, landed nothing, which the note calls a test and not a proof."
tags = ["sandbox"]
source = { file = "data/results/q-22184.md", section = "Still open with these settings" }
+++
This stops someone dropping `blockReadsOutsideWorkingDirectories` as redundant with the hook. Second source: docs/design/session-sandbox.md, "What this does not stop".

Two different tests, kept apart:
- Reads, in the trial: hook alone 9 of 60 lost; hook plus read block 0 of 60.
- Writes, recorded in the note: 40 concurrent attempts against a swapping link landed nothing. The trial itself listed the symlink race on Write as untested. The note says this rests on the CLI's own guards plus the hook.

The trial's hook also failed open on a crash or missing file (canary read). The note says the shipped hook exits 2 on any exception and its settings command adds `|| exit 2`.

What this does not establish. 0 of 60 and nothing in 40 are small samples of a race. The Write figure does not say the read result carries over, or the other way round. Neither source repeats the 60-read count on the shipped hook.
