+++
id = "2026-11-16-do-the-reuse-and-whole-goal-checks-cut-too"
title = "Do the reuse and whole-goal checks cut too-narrow corrections"
date = "2026-11-16"
status = "open"
question = "Do the reuse question (proposals and plan check) and the whole-goal check (verify) lower the share of the owner's corrections that are too narrow?"
verdict = "Not known yet. The bar below was written before the checks shipped."
+++
Of 25 labelled owner corrections from 2026-10-06 to 2026-11-03, 6 were too narrow, and they were about reuse and follow-through rather than code that uses what changed (2026-11-16-the-owner-s-too-narrow-corrections-were-reuse). Three prompt changes were built together on 2026-11-16: a reuse question in the critical pass on proposals, a "builds something the repo already has" revision in the plan check, and a whole-goal section in verify that puts any part not delivered in SUMMARY as "not done: ...". Nothing has been measured yet.

The bar, written before the checks ran. After 30 days of use, mine the owner's corrections over the new window with scripts/judge_replay/mine.py and label them by hand in label.py and compare too narrow per labelled correction against the baseline of 6 in 25. Also count how often the plan check raised a reuse revision, from the titles in data/plan_checks.jsonl (it keeps titles, not kinds), and how often a verify SUMMARY carried "not done:". Result files are pruned within days, so read verify output from the session transcripts, as recover_verify.py does. Two things to fix before the rerun: mine.py has its window end hard-coded (NOW = 2026-11-04), and label.py rewrites data/judge_replay/labels.jsonl, so copy the baseline file aside first. Fewer than 10 new labelled corrections is inconclusive, not a pass. The Missed button's Too narrow kind would be a lighter signal if it were used; it had 0 taps when this was written.

What this does not establish. It does not establish that the checks help: nothing has run. A lower share would not separate the checks from a change in what the owner worked on in that month, and the labels are one reader's.
