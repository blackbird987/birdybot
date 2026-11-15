+++
id = "2026-11-15-needs-you-list-on-the-live-state-what-it-shows"
title = "Needs you list on the live state: what it shows and hides"
date = "2026-11-15"
status = "stands"
question = "On the real state, how many items does the waiting list show, do the stale merge and chain records stay hidden, and is a per-thread Your turn list readable?"
verdict = "2 items shown (both unapproved deploys); 21 of 21 stale failed merges and 11 of 11 stale paused chains hidden; Your turn estimated at 22 threads in 9 repos, so it renders as a count line"
+++
The old Needs Attention and Failed Recently sections grew into noise (7185f50), and failures were later hidden after 30 minutes because nothing closed them (ae10dfa). `bot/engine/waiting.py` filters stored records at read time instead of deleting them. That needed a check against the real state. Does it hide the 21 failed-merge and 11 paused-chain records known to be dead? Is "Your turn" short enough to list?

`python scripts/test_waiting.py --live` read a copy of data/state.json (253 instances, 3443 threads) and ran git reads against the registered repos. `collect` took 0.30s. It showed 2 items, both deploys never approved: generals-kart (10 merges since the last deploy) and smartmoney (1). Every other kind was 0 before dedup. All 21 failed merges were hidden: each is older than 3 days. Of the 11 paused chains, 10 were older than 7 days and 1 had its instances pruned. All 11 were hidden. The first render said "5 changes" for both deploys. `pending_changes` is capped changelog lines, not a count, so the line now counts `pending_sessions`.

"Your turn" was estimated from instances: the latest real turn completed within 48h, did not ask, and has no newer run. That gave 22 threads in 9 repos (smartmoney 6, strategy-lab 4, aiagent 3), mostly finished research. That is more than the 15-line budget, so it renders as one count line (`YOUR_TURN_AS_COUNT`).

Not established: the your-turn figure is an upper bound, because thread stamps start with this change. The kinds that were empty that day are covered only by the fake-store harness. Hiding the 21 merges says they are old, not that each branch was merged or deleted. Whether a never-approved deploy belongs on the list is left to use.
