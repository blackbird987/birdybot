+++
id = "2026-10-27-session-starts-are-not-paced-on-the-weekly-limit"
title = "Session starts are not paced on the weekly limit"
date = "2026-10-27"
kind = "decision"
status = "stands"
question = "Should starts be held back when an account's weekly % runs ahead of the share of the week gone?"
verdict = "No, decided 2026-10-27 and the cap removed: it held an account to 2 sessions even with the five-hour window nearly unused, which only delayed the same work. Hitting the weekly cap is left to account failover and cooldown."
tags = ["usage"]
source = { file = "docs/design/usage-pacing.md", section = "Rules that must not drift" }
+++
A weekly cap was part of the first pacing on 2026-10-15 (at most 2 when the week was used faster than it passed, 1 from 90% of the week) and was removed 12 days later. Second source: CHANGELOG.md, the v0.102.9 and 2026-10-15 pacing entries.

The reason given: holding back only delays the same work and wastes session-window capacity that does not carry over. The 2026-10-19 wait that led to the fast lane, a typed message over two hours behind 8 queued sessions, happened with the week ahead of pace and the main account allowed 2.

Since 2026-11-02 pacing as a whole is off by default (2026-11-02-usage-pacing-and-the-peak-gate-off-by-default). Its "does not carry over" reason was about the five-hour window, which the note says no longer applies to `~/.claude-main`.

What this does not establish. No measurement in either source compares weeks with and without the cap. It says nothing about a weekly cap on an account with only a weekly window: the reason given leans on a window that account no longer has.
