+++
id = "2026-10-09-a-ccusage-estimate-read-93-against-15"
title = "A ccusage estimate read 93% where Anthropic had 15%"
date = "2026-10-09"
status = "stands"
question = "Can usage be estimated from a ccusage dollar total over a guessed limit?"
verdict = "It read 93% for a session Anthropic had at 15% on 2026-10-09, one reading, and had no weekly figure. Real figures now come from an undocumented endpoint, but when no account returns figures the old ccusage bar is drawn exactly as before."
tags = ["usage"]
source = { file = "docs/design/plan-usage.md", section = "opening section" }
+++
Worth having when the bar looks wrong, or when someone proposes estimating usage locally. Second source: docs/design/cost-gates.md, which says the figure that runs out is the plan's percentage, not dollars.

The endpoint is the one Claude Code's `/usage` screen calls. The note treats every key in it as optional because it is undocumented. A failed refresh serves the last good reading, up to 3h old, marked with its age.

What this does not establish. How wrong ccusage is in general: this is a single reading, and the guessed limit it was divided by is part of the fault, not separated from ccusage itself. Nothing here says the endpoint will last.
