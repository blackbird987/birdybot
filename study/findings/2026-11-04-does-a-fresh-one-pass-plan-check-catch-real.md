+++
id = "2026-11-04-does-a-fresh-one-pass-plan-check-catch-real"
title = "Does a fresh one-pass plan check catch real defects at a cost worth paying"
date = "2026-11-04"
status = "open"
question = "Does a fresh one-pass plan check catch real defects at a cost worth paying?"
verdict = "Not known yet. The kill bar below was written before the first run."
+++
Plan review ran on 781 chains in data/evals: 754 had at least one revision round and 10 ended without converging, and nobody ever recorded whether the revisions were real. So it was left out of /chain in 6bda254. Now the owner wants one automatic check in every /chain and in Build & Ship, and the Review Plan and Autopilot buttons had 1 and 0 taps from 2026-09-13 to 2026-11-04. The shut record 2026-11-04-a-fresh-judge-did-not-catch-what-the-owner covers a judge of finished work, not plans, and is not re-run. It does say to expect only repo-checkable catches, which is why the check is that narrow and why this bar exists.

Nothing has run yet. Each check writes one row to data/plan_checks.jsonl. The judge's median run cost $0.86, so the estimate is about $200 a month at October's 237 chains. That is unmeasured.

Kill bar, written before the first run. Read the most recent 30 days of data/plan_checks.jsonl. For each revision the author accepted, confirm against the code at that commit that it named a real defect. If fewer than 1 in 5 checked chains has one, set PLAN_CHECK_ENABLED off. The author accepting a revision is not evidence by itself, since the author is the one being corrected and has reason to agree. A chain counts as checked when its outcome is clean, revised or kept; skipped and could_not_check do not.

What this does not establish. It does not establish that the check is worth anything: it has not run. Passing the bar would show that it catches real defects, not that those defects would have survived the build, the code review and verify, so it would not show the check saved anything. The rows cannot tell what a clean result missed; joining the thread id to data/missed.jsonl only sees what the owner marked.
