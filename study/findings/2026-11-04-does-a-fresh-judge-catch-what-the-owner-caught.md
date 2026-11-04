+++
id = "2026-11-04-does-a-fresh-judge-catch-what-the-owner-caught"
title = "Does a fresh judge catch what the owner caught later"
date = "2026-11-04"
status = "open"
question = "Given only the owner's words, the repo goal, the diff and the verify output, does a fresh session catch what the owner later caught, without flagging clean work?"
# verdict = ""  # one line, required once the status is not open
+++
The author's own check misses what it cannot see. This tests a separate judge on past work before it may hold anything. Bars written before any judge run.

Baseline, 30 days, seven repos: 68 candidates, 25 real misses. not_what_i_meant 12, bug 7, too_narrow 6; 10 are looks or feel. aiagent 10, strategy-lab 7, Ev-nova-remake 4, halo-apex 2, smartmoney 2, funny-rock-polisher 0, bot 0. Only corrections in the same thread after a resumed session are visible.

Cases frozen, sha256 36e843fe86d2d7b4. 18 misses (all 6 too_narrow), 15 clean, 10 followed by review fixes. Split by thread, 29 held out, one prompt revision at most from the rest. Two replicates each, scored separately, averaged. Scoring is fixed in `scripts/judge_replay/replay.py`.

Bars, on held-out cases only:
- catches at least 50% of misses;
- any non-pass verdict is a flag, at most 20% of clean flagged;
- on a looks or feel miss, only_you counts as caught;
- replacing code review also needs at least two thirds of review-fix issues caught and a median judge cost below about $26;
- fewer than 10 linkable misses, or hitting the budget ($200 total, $5 a run), is inconclusive, which is not passed.

Passed: JUDGE_REPOS defaults to aiagent and Ev-nova-remake. Otherwise it stays empty.

Results follow in a superseding record.

What this does not establish. It does not yet establish anything about the judge. Labels are one reader's. Clean cases come from four repos only.
