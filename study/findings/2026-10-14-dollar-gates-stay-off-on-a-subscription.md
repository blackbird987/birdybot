+++
id = "2026-10-14-dollar-gates-stay-off-on-a-subscription"
title = "Dollar gates stay off by default on a subscription"
date = "2026-10-14"
kind = "decision"
status = "stands"
question = "Once run costs are recorded correctly, should DAILY_BUDGET_USD and the other dollar gates enforce?"
verdict = "No, off by default: on a subscription the figure is a notional API price, a busy day roughly $1K, so the note reasons that enforcing DAILY_BUDGET_USD=20 would refuse all work within the hour. That was reasoned, not run. The API fallback's own cap is enforced regardless. The plan-usage brake it points at is still a TODO."
tags = ["usage", "cost"]
source = { file = "docs/design/cost-gates.md", section = "whole note" }
+++
Until 2026-10-14 the parser read `cost_usd` while the CLI emits `total_cost_usd`, so every run was stored at $0 and every dollar gate was inert. Second source: the auto-memory file context-budget-replay-ab-proposal.md, which counted all 259 instances in state.json at cost 0. Checked against the repo: `bot/claude/parser.py` now reads `total_cost_usd`, and `COST_GATES_ENFORCE` defaults to false.

The note's reasoning: recording costs would have woken the gates against DAILY_BUDGET_USD=20, so costs are recorded and enforcing them is a separate switch.

The note's intended brake, a gate on real plan-usage %, is an unchecked item in TODO.md. It is a different thing from the session-start pacing that went off on 2026-11-02 (2026-11-02-usage-pacing-and-the-peak-gate-off-by-default), though both read `bot/engine/plan_usage.py`.

What this does not establish. The $1K is the note's rough figure, with no data behind it here, and it may have moved. Nothing says a plan-usage brake would work.
