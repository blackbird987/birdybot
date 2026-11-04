+++
id = "2026-10-19-prompt-length-session-size-and-last-run-did-not-predict"
title = "Prompt length, session size and the last run did not predict a run's usage"
date = "2026-10-19"
status = "shut"
question = "Can a quick reply be told from a long job before it starts?"
verdict = "On the 2026-10 history, these three failed: prompt length, session size and the thread's last run. The note says nothing tells them apart but names only those three. Turn count separates them once running: half of all runs end within 10 turns and spend 15% of the usage."
tags = ["usage"]
source = { file = "docs/design/usage-pacing.md", section = "A fast lane for runs that may be quick" }
+++
Asked after a typed message waited over two hours with 8 sessions ahead of it, with the main account allowed 2 at once. Second source: CHANGELOG.md, the fast lane entry ("failed on the 2026-10 history").

Among typed messages, the 16 runs past 100 turns spent 55% of the usage. The fast lane built on this uses the turn count while a run is live, plus a list of known-heavy steps (build, `/bg`, sensor fix, merge resolver). It has been off by default since 2026-11-02 along with the rest of pacing, but the result about predictors stands apart from that.

What this does not establish. Neither source gives the size of the history, the method, or how badly each predictor failed. A predictor neither source names, such as a model reading the prompt, is not covered. Neither source re-measures the 15% and 55% figures.
