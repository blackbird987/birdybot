+++
id = "2026-11-20-held-branches-were-never-discarded"
title = "Held branches were never discarded"
date = "2026-11-20"
status = "stands"
question = "When a chain holds before the merge, does the owner use the hold to reject work, or does it only delay or strand finished work?"
verdict = "Holds mostly delayed or stranded finished work: 0 Discard taps against 142 Merge taps in two months, and the sampled unmerged branches were finished work left behind, not rejected."
+++
The owner asked for the bot to decide on its own whether finished work merges, instead of parking it on Merge and Discard. A hold only earns its place if the owner uses it to reject work, so the question was what holds actually led to.

Counted from the bot's logs, 2026-09-13 to 2026-11-20: 142 Merge taps and 0 Discard taps; /chain launched ship 354 times, hold 189 and verify 100; 330 auto-merges. So 289 hold or verify chains against 142 Merge taps leaves about 147 held branches that got neither. About 71 bot branches across repos are still unmerged (32 in funny-rock-polisher), and the ones sampled, all in funny-rock-polisher, were finished write-ups that were forgotten, not rejected. One real revert in the window (smartmoney, 2026-11-19, a conflict resolution that dropped code) came after a merge, and a hold would not have caught it either.

So a /chain now ships by default, and hold or verify needs a stated reason (the owner asked to look first, or the change is hard to undo); a hold with no reason runs as ship and says so. Confined sessions and repos whose workflow.json says hold still hold. The merge line says "Merged on my own" when the chain made the call.

What this does not establish: abandoning a branch is how the owner discards, so 0 Discard taps is not proof that every held branch was wanted; only funny-rock-polisher was sampled, and nobody asked the owner about each one. The /discard command and /branches are other discard routes and were not counted separately. Nothing here shows the stated-reason rule will be followed: the model writes the reason itself, so it is a speed bump, not a gate.
