+++
id = "2026-11-04-a-fresh-judge-did-not-catch-what-the-owner"
title = "A fresh judge did not catch what the owner caught"
date = "2026-11-04"
status = "shut"
question = "Given only the owner's words, the repo goal, the diff and the verify output, does a fresh session catch what the owner later caught, without flagging clean work?"
verdict = "Failed the bars: it caught 8% of held-out misses against 50% and flagged 30% of clean work against 20%, so the judge stays off."
supersedes = ["2026-11-04-does-a-fresh-judge-catch-what-the-owner-caught"]
+++
Failed. Conclusive: 25 linkable misses, $66.24 of the $200. The bars did not move.

Held-out, 29 cases, 2 replicates each:
- misses caught: 8% (1 of 12), bar 50%. That one is an only_you on a looks miss. The judge named the owner's actual problem in 0 of 12.
- too_narrow caught: 0 of 5. looks caught: 1 of 4.
- clean flagged: 30% (6 of 20 runs), bar 20%.
- review-fix issues caught: 0 of 21, bar two thirds. Median cost $0.86, under the $26 bar, so only the cost bar held.
- verdicts over 58 runs: pass 32, fix 22, only_you 2, could_not_check 2. Replicates split on 4 cases' scores.

Development third, first prompt, no verify output: 3 of 6 misses, 5 of 5 clean flagged. One revision from that, committed before any held-out run. Pruning had removed the verify output, so it was recovered from transcripts before that run.

Evidence: 20 of 29 held-out cases had no verify output, 7 of them by design.

Reading the raw runs: where the judge said fix on a miss, it raised something else. The owner's corrections came from knowledge or use the input does not hold.

Outcome: JUDGE_REPOS stays empty, code review stays. The rest ships. `data/missed.jsonl` is now the count to watch.

What this does not establish. Whether the judge's other findings are real: they were not scored, and some read as real bugs. Whether the revision or the sample caused the drop from the development third. Anything about a judge with other input, such as screenshots. Labels are one reader's, review issues are parsed from commit messages, clean cases come from four repos, and no clean case has been shown free of defects.
