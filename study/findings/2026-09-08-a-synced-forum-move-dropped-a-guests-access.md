+++
id = "2026-09-08-a-synced-forum-move-dropped-a-guests-access"
title = "A synced forum move would have dropped a guest's access"
date = "2026-09-08"
status = "stands"
question = "Is it safe to pass sync_permissions=True when moving a repo forum between categories?"
verdict = "No, for a forum with its own grant: against the live API a synced move took a forum from 4 overwrites to 3 and dropped the grant role, and an unsynced one round-tripped all 4. The note says syncing back on unhide would not restore it, and reports no test of that. Caught while verifying repo hide, on throwaway channels: the commit says hiding would have revoked it."
tags = ["discord", "permissions"]
source = { file = "docs/design/repo-hide.md", section = "Three things that must not drift, first bullet" }
+++
Anyone writing a channel move outside repo hide would not read that note. Second source: commit e64ebf0, "hiding a repo revoked a guest's access to it", and the comment at the move in `bot/discord/forums.py`.

A per-repo access grant is an extra entry on the forum rather than on the category, which is why syncing to the category's list loses it.

What this does not establish. Anything about a forum with no grant of its own. `bot/discord/channels.py` syncs a forum right after creating it, which is not a move and is not covered here.
