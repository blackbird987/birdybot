# Costs are recorded, dollar gates are off by default

Until 2026-10-14 the parser read `cost_usd` while the CLI emits
`total_cost_usd`, so every run was stored at $0 and every dollar gate was
inert. On a subscription that figure is a notional API price (a busy day is
roughly $1K of it), and the limit that actually runs out is the plan's usage
percentage, so waking the gates against `DAILY_BUDGET_USD=20` would have
refused all work within the hour. Costs are now recorded; whether they gate is
`COST_GATES_ENFORCE` (default off). Every dollar check goes through
`cmd_state.within_dollar_budget`, and `scripts/test_cost_gates.py` fails on a
comparison that bypasses it. The API fallback's own daily cap is real money
and is enforced regardless. The intended brake is a gate on real plan-usage %
(`bot/engine/plan_usage.py`, see TODO.md), not a dollar figure.
