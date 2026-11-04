+++
id = "2026-10-11-a-large-nested-claude-md-undid-each-compaction"
title = "A large nested CLAUDE.md came back on each file touch in its folder and undid compaction"
date = "2026-10-11"
status = "stands"
question = "Why can a resume fail to clear an autocompact thrash?"
verdict = "In one case, t-9142 (AIAgent, 2026-10-11): a 317K-char nested CLAUDE.md was re-attached 22 times, so each compaction was undone by the next Read and resuming could not clear it. Files over 60K chars are now named as a hint, not as proof, and the retries stay the same."
tags = ["context"]
source = { file = "docs/design/context-thrash-bloat.md", section = "opening section" }
+++
A session adding a big `CLAUDE.md` to a subfolder, in any repo, would not read this bot's note first. Second source: docs/design/context-overflow.md, which sets this next to the opposite failure.

The note says a nested `CLAUDE.md` returns as a `nested_memory` transcript row every time the session Reads, Edits or Writes a file in its folder, and the project's instruction files return after every compaction. The usual thrash, a burst of large reads, is different: a resume clears it because the counter lives in the CLI process.

The advice the bot gives on a finding: name the file, delegate work in that folder to a subagent, prefer Bash reads there, and slim the file.

What this does not establish. How small is safe: 60K chars is the bot's reporting threshold, not a measured limit. This is one transcript, and the behaviour is the CLI's, so it may have changed. The note gives no figure for how well the advice works.
