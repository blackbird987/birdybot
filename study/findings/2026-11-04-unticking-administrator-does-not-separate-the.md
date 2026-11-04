+++
id = "2026-11-04-unticking-administrator-does-not-separate-the"
title = "Unticking Administrator does not separate the two bots"
date = "2026-11-04"
status = "stands"
question = "What does it take for the desktop and laptop bots to not reach each other's Discord categories?"
verdict = "Computed from the live guild on 2026-11-04: unticking Administrator is not enough, because Manage Roles above Analyst, Community Manager and Signal bot lets a token assign itself Administrator. The role also has to move below them, and Kick and Ban have to go."
tags = ["discord", "permissions", "peer-link"]
source = { file = "scripts/peer_perms.py" }
+++
A later session would otherwise propose "just untick Administrator" for the desktop bot (TT-GPT) and the laptop bot (office bot). `scripts/peer_perms.py` rechecks all of this live.

Roles read by REST on 2026-11-04, from the top: Founder 71 (admin), TT-GPT 70 (admin, Manage Roles, not managed), Analyst 69 (admin), Community Manager 68 (admin), Signal bot 65 (admin), office bot role 5 (no admin). Office bot also holds Signal bot and TT-GPT.

Live, each bot sees the other's category and can assign itself an Administrator role. With only Administrator removed from TT-GPT, Analyst, Community Manager and Signal bot stay assignable.

The proposal that computes clean: move TT-GPT below Signal bot; untick Administrator, Kick and Ban on it (it would still outrank the office bot, and no code calls them); give the office bot's role Manage Messages, Attach Files, Manage Roles and Pin Messages, which it had only through the admin roles; then take those two roles off it.

Missed at first:
- A third bot, alpha birb (1482820304819720353), holds the TT-GPT role with no other Administrator, so the change reaches it. A new Administrator role for it must sit above TT-GPT.
- Managed roles (Dyno 58, AIAgent 6) have Administrator but cannot be assigned, per Discord's documentation. Counting them was wrong.

What this does not establish. Effective permissions were computed from REST, not exercised: no role changed. Thread-level overwrites and other users of either token are not ruled out, and a process that reads without posting would not show in the activity scan (newest post outside its category: 2025-06-11, last 100 messages per channel). That managed roles cannot be assigned was read, not tried. What alpha birb needs is unknown. After the change TT-GPT can still edit the office bot's role, and nothing stops a session using a token directly.
