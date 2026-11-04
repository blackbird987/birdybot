+++
id = "2026-07-02-scanning-reply-prose-to-arm-a-wake-misfired"
title = "Arming a wake by scanning reply prose misfired, twice"
date = "2026-07-02"
status = "shut"
question = "Can the bot arm a self-wake from what a reply says, without an explicit directive?"
verdict = "Ran and failed twice: the promise heuristic (2026-06-18 to 2026-07-02) armed 180s re-checks on prose discussing a job, about 20 false wakes a week, and the claim-only version left behind fired on a report quoting its trigger. Covers arming from prose only: re-invoking a session once from similar prose shipped 2026-08-31 and is not shut."
tags = ["wake", "directives"]
source = { file = "docs/design/prior-art.md", section = "opening paragraph" }
+++
This idea already came back as a fresh proposal once: it is the worked example in the prior-art note. Second sources: docs/design/watches.md ("ripped out twice"), commits cdc54c8 (added), d5f8aa8 and 0e5c25c (removed, both 2026-07-02; the false-wake rate and the quoted-trigger case are in their messages).

The second failure "survived one turn", per 0e5c25c. Meta-discussion of the feature is routine in bot threads, which that commit gives as the reason prose scanning never schedules.

The nudge from 2026-08-31 reuses the name `WAKE_PROMISE_RE` but schedules nothing: it resumes the session once, and a false positive costs one turn. The notice-only claim check also survives.

What this does not establish. That no prose detector could be accurate: only these two ran, and both scheduled. The 20 a week is the commit's figure, with no count to check here.
