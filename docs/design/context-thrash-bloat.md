# A thrash a resume cannot clear is named, not skipped

"Autocompact is thrashing" has two shapes. The usual one is a burst of large
reads, and a resume clears it because the thrash counter lives in the CLI
process; that is why `CONTEXT_THRASH_MAX_RETRIES` exists. The other is a
fixed-size load the CLI attaches on its own: a nested `CLAUDE.md` comes back as
a `nested_memory` transcript row every time the session Reads, Edits or Writes
a file in its folder, and the project's instruction files come back after every
compaction. t-9142 (AIAgent, 2026-10-11) re-attached a 317K-char
`Services/Automation/CLAUDE.md` 22 times before it died, so each compaction
was undone by the next Read and resuming could not clear it.

`bot/claude/context_bloat.py` reads the dead session's transcript (streamed,
only lines carrying `nested_memory` or an `instructions` row are parsed) and
reports any auto-attached file over `CONTEXT_BLOAT_MIN_CHARS` (60K chars). The
resumed attempt gets `CONTEXT_THRASH_BLOAT_NOTE` after the usual nudge (name
the file, delegate work in that folder to a subagent, prefer Bash reads there),
and an exhausted thrash gets a `Cause:` line naming the file to slim.

Four things that must not drift:

- **It diagnoses; it never changes the retry policy.** The resume runs exactly
  as often with or without a finding. A finding is a strong hint, not proof,
  and a skipped retry on a wrong hint costs the user the click this whole path
  exists to save.
- **The cause line goes after the CLI's own text.** `is_context_thrash_error`
  and `is_account_agnostic_error` read `error_message`, and the original
  wording has to stay intact for both.
- **A file is named relative to the repo, not by the CLI's `displayPath`.**
  That field is relative to wherever the session's shell stood, so one file
  shows up as `../CLAUDE.md` and as `AIAgent/Services/...` in the same
  transcript. `display_roots` measures from the repo, the worktree, the
  repo's folder inside the worktree (found through git, since a registered
  repo can be a subfolder of its checkout) and the checkout root, deepest
  first, so the worktree and main repo copies merge into one finding.
- **A file is judged at the size of its last load, not its largest.** The
  threshold is applied after the scan, to that size. Keeping the maximum
  would go on blaming a file that was slimmed mid-session and send the user
  to slim it again.

Harness: `python scripts/test_context_bloat.py` (replays the real t-9142
transcript when it is on this machine).
