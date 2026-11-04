+++
id = "2026-11-04-a-masked-key-came-back-through-an-echoing-host"
title = "A masked key came back through an injected host that echoes requests"
date = "2026-11-04"
status = "stands"
question = "Does the sandbox's credentials mask mode keep a key away from a session?"
verdict = "Partly, in the 2026-11-04 trial: a masked key stayed hidden from the session, but an injected host that echoes requests returned the real value. Masking on real GitHub and file masks were not tested."
tags = ["sandbox", "secrets"]
source = { file = "data/results/q-22184.md", section = "Still open with these settings" }
+++
Worth having before anyone leans on masks to hand a session a usable key. Second source: docs/design/session-sandbox.md, which does not mention masks. It says vault keys granted to a repo stay available to Bash, and that anything readable can leave through a host on the list.

The report: a masked key (`mode: mask`) stays hidden from the session, but an injected host that echoes requests returned the real value. Its recommendation adds that the vault's env injection only helps with deny or mask on every variable, because sessions inherit the bot's whole env.

What this does not establish. How a real host on the allowed list would behave: the report says only "an injected host" and lists masking on real GitHub as untested. It does not say deny mode leaks. The report does not describe the mask's mechanism, so no reason is given here for why the value came back.
