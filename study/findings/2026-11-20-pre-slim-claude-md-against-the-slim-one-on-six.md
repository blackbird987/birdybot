+++
id = "2026-11-20-pre-slim-claude-md-against-the-slim-one-on-six"
title = "Pre-slim CLAUDE.md against the slim one on six replayed builds"
date = "2026-11-20"
status = "stands"
question = "Does the pre-slim CLAUDE.md against the current one change what a replayed build does, beyond the noise of two identical arms?"
verdict = "No outcome difference showed above the noise on the four builds that fit: shipped-test pass share -0.12 with the old file (SE 0.08), against +0.05 (SE 0.10) between two identical arms. The old file cost 15% more per build and one more compaction on the longest build, and two builds of six could not start with it."
tags = ["context", "replay"]
scripts = ["scripts/replay_ab/"]
source = { file = "~/.cache/replay-ab/runs/arm-preslim-20261120/paired.json", section = "whole file, with runs/aa-20261120/paired.json beside it" }
supersedes = ["2026-10-14-does-a-slimmer-claude-md-change-build-outcomes"]
+++
The slim (about 118K to 21K chars, 2026-10-14) shipped without its A/B. Run 2026-11-20: six finished November builds (t-10872, t-10995, t-10303, t-10996, t-11029, t-10333), k=3 per arm. First both arms `current` (A/A), then `current` against `git show d32927c:CLAUDE.md`. 36 of 36 replicates finished in each round, no limit hits.

A/A noise, paired b minus a, mean of six case means (SE): pass +0.03 (0.06), turns -0.4% (2.2%), cost -1.8% (0.8%), file overlap +0.03 (0.01), wall +2.3% (3.7%). Pass is pinned on two cases (0/1 and 2/2 every time).

The old file adds about 35.5K tokens to every turn. The two builds whose forked transcript starts at 147K and 137K tokens answered "Prompt is too long" in all three old-file replicates. One starting at 134K fit, at 170K. Those six are left unscored.

On the four that fit (12 pairs), old minus slim: pass -0.12 (0.08), against +0.05 (0.10) in the A/A on the same four. Turns +0.3% (7%), overlap +0.02 (0.03), wall -6% (8%). None clears the noise. Cost +15% (7%), higher on all four (+0.5% to +29%). The longest build compacted 3 times in every old-file replicate, 2 times in all nine slim ones.

Sandbox: every call naming the live checkout found only `.venv`. Five t-11029 replicates read `data/state.json` from an unhidden August copy of the checkout, which holds no source or report for these builds.

What this does not establish. That the slim left quality unchanged: pass moves in steps of 0.2 to 1.0 per case, so an effect under about 0.2 would not show at k=3 on four builds of one repo. The old file names pre-refactor paths, so it is staler now than in October. Forked transcripts already carry what the original session learned. A live session would compact earlier, not die.
