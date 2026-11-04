+++
id = "2026-11-02-usage-pacing-and-the-peak-gate-off-by-default"
title = "Usage pacing and the peak-hour gate went off by default"
date = "2026-11-02"
kind = "decision"
status = "stands"
question = "Should session starts still be paced to the five-hour window?"
verdict = "Off by default since 2026-11-02: Anthropic removed the five-hour limit for verified Ultra accounts from 2026-10-30, seen live on ~/.claude-main, with no other account checked in the sources. The code stays for any account that still has the window, and the staggered release of parked retries stayed on on purpose."
tags = ["usage"]
source = { file = "docs/design/usage-pacing.md", section = "top section" }
+++
Reverses the pacing built on 2026-10-15. Second source: CHANGELOG.md, the v0.102.11 entry.

Evidence: a minimal CLI call on `~/.claude-main` returned a `rate_limit_event` whose `unifiedWindows` holds only `seven_day`, with no `five_hour`.

Off: session-% tiers, peak-hour cap, the hold, the fast lane (`USAGE_PACE_ENABLED`), the Run now / Queue / Cancel gate and the 5am/11am PT DMs (`USAGE_PEAK_GATE_ENABLED`). Still on: staggered ranked retry release, limit detection, failover and parking, account alerts, `MAX_CONCURRENT`, memory limits, the usage queue's drain, plan usage figures.

The note records a trap in this switch: with pacing off, the cooldown loop's slot check must read the concurrency ceiling, or retries stack behind the semaphore and defeat the stagger.

What this does not establish. The live call covered one account. Neither source records a call on any other account, so nothing here says the second account has lost the window. Neither source measures usage with pacing off. Whether the stagger is still needed without a five-hour window is not tested: the owner kept it by choice.
