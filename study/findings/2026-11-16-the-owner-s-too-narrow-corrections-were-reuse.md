+++
id = "2026-11-16-the-owner-s-too-narrow-corrections-were-reuse"
title = "The owner's too-narrow corrections were reuse and follow-through, not code usage"
date = "2026-11-16"
status = "stands"
question = "When the owner corrects work as too narrow, what was the agent missing: other code that uses what it changed, or something else?"
verdict = "Something else, in all 6 labelled cases: 2 rebuilt what already existed, 1 one-off vs reusable, 1 shipped half the goal, 1 wrong domain fact, 1 other running sessions. None was other code using the changed thing, and none was in aiagent."
+++
The owner asked for a big-picture overview after narrow changes, trialled on aiagent first. The first design was a bot-computed map of code using what a change touched. The question: were the owner's real corrections of that kind?

The judge replay labelled 25 owner corrections, 2026-10-06 to 2026-11-03 (data/judge_replay/labels.jsonl): 12 not what I meant, 7 bug, 6 too narrow. aiagent has 10 of the 25 and none of the too narrow ones. The 6:
- rebuilt instead of reused, 2 (strategy-lab)
- one-off or reusable, 1 (Ev-nova-remake)
- shipped half the goal, 1 (smartmoney: sold on ETH, never bought on AVAX)
- wrong domain fact, 1 (strategy-lab)
- other running sessions affected, 1 (strategy-lab)

A fresh read-only reviewer, given the cases and not this conclusion, found none where other code using the changed thing needed the same change. Two point outside the repo, which an in-repo map cannot see. data/missed.jsonl held 0 events: the Missed button was never tapped.

So the map was dropped and the aiagent trial was not run, since aiagent had no too-narrow case to measure against. The owner chose a reuse question and a whole-goal check instead (2026-11-16-do-the-reuse-and-whole-goal-checks-cut-too), and can still overrule this and have the map trialled.

What this does not establish: 6 cases, one 28-day window, one labeller. Nothing about whether a map would help with bugs, other miss kinds, or aiagent, since none of that was run, nor that the chosen checks work.
