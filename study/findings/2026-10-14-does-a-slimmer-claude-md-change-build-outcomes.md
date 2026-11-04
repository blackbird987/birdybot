+++
id = "2026-10-14-does-a-slimmer-claude-md-change-build-outcomes"
title = "Does a slimmer CLAUDE.md change build outcomes"
date = "2026-10-14"
status = "open"
question = "Does the pre-slim CLAUDE.md against the current one change what a replayed build does, beyond the noise of two identical arms?"
verdict = "Not measured. The slim shipped 2026-10-14 on the owner's approval without the A/B, and TODO.md still lists the A/A round and that arm as not done. Only one spike has run."
tags = ["context", "replay"]
source = { file = "/home/quincy/.claude-main/projects/-home-quincy-Programming-claude-telegram-bot/memory/context-budget-replay-ab-proposal.md", section = "whole file" }
+++
Second sources: TODO.md (Replay A/B item), CHANGELOG.md (v0.102.2, v0.102.4).

Readings from the memory, taken 2026-10-14 before the slim: about 30-40K chars injected per turn by the bot, CLAUDE.md at 117,630 chars loaded by the CLI, and 4 of 6 checked builds auto-compacted within about 40s. Only the CLAUDE.md size has a second source (the changelog says about 118K, to about 16K). The rest should not be read as current.

Harness: `scripts/replay_ab/`, control arm `git show d32927c:CLAUDE.md`. Spike on t-9086: the fork resumed, $3.52, 90s, its own test passed, and the shipped test failed on import because it fixed the same thing under a different helper name. TODO.md next steps: an A/A round (both arms `current`, k=3, 4-6 cases), then the arm. It also lists as missing the pairwise judge, the `--review` score and project auto-memory in a replay.

The memory cites two papers for the design, not re-read here: "The Replay Gap" (arXiv 2608.08239, do not splice logged tool results into a variant run) and "On Randomness in Agentic Evals" (arXiv 2602.07150, about 9 runs per arm for a 2pt effect). It says replay candidates live in state.json, which prunes after 7 days, so its case list (t-9086, t-9315, t-9073, t-9155, t-9005) needs checking first.

What this does not establish. That the slim helped or hurt: nothing compared them. The changelog's 25K tokens lighter is a size, not an outcome. Whether a noise floor would let a 2pt effect show on this repo is unknown.
