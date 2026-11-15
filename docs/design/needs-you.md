# Needs you: one list of what is waiting on the owner

The Ark's description, every Control Room's "Needs you" field and
`/list attention` all render one list, built by `bot/engine/waiting.py`
(`collect`). Nothing else decides what "needs you" means. Harness:
`scripts/test_waiting.py` (`--live` reads the real state, read-only).

## Why it replaced Needs Attention

The old Ark stacked Needs Attention, Idle, Failed Recently, a counts row,
Projects, Last Activity, Uptime and Orphans. Commit 7185f50 added the sections
that grew into noise; ae10dfa then hid failures after 30 minutes, because
nothing ever marked one dealt with. Records piled up: 21 failed-merge records
and 11 paused-chain records whose branches, threads or instances were long
gone stayed in state.json. This list closes each item on a real event
instead, and filters old records when it reads them.

## Kinds, most urgent first

The order is the priority. One item per thread, and the highest kind on a
thread wins. A thread whose session is running shows nothing.

1. `secret_request`: a `/secret request` card not yet answered
   (`vault.pending_requests`, needs `created_at`, 3 days).
2. `update_ready`: the carded bot update, while git says the carded sha is not
   in `HEAD` (`auto_update.carded_sha` and `carded_at`, 7 days).
3. `peer_run_once`: a question from the other machine, carded and not yet run
   or declined (`peer_questions.MAX_AGE_SECS`), linked to its card thread.
4. `deploy`: a repo with a deploy config whose merges have not been deployed
   (`needs_reboot` with pending changes), "approve deploy" when the config was
   never approved. The count is the merges since the last deploy
   (`pending_sessions`); `pending_changes` is capped changelog lines, not a
   count.
5. `account`: an unresolved alert for an account in rotation, and login expiry
   warnings.
6. `choice_pick`: an open `/choices` offer, with its labels. Read only through
   `choices.open_offers`, which applies `OFFER_TTL_SECONDS` via `offer_live`.
7. `question`: the thread's last real turn asked (`needs_input`), 3 days.
8. `chain_paused`: a paused chain, with the reason the runner stored, 7 days,
   only when its session still has an instance to resume from.
9. `resume_parent`: a parent waiting on a "Resume parent" tap, until the tap
   or the owner speaks in that thread. Only payloads that carry `thread_id`.
10. `merge_failed`: a failed merge, while git says its branch exists and is
    not in its target, 3 days.
11. `merge_held`: an auto-merge countdown the owner held.
12. `failed_run`: the thread's last real turn failed, 3 days. Judge, plan
    check, TL;DR and similar runs are not turns and never shadow or show.
13. `manual_recovery`: a run that left work needing a hand, 3 days.
14. `ready_to_merge`: built work ahead of its base, not merged
    (`chain_git.ship_candidates`, shared with fleet ship), 7 days.
15. `your_turn`: the bot answered last in the past 48h and nobody has written
    there since (`ThreadInfo.last_turn_at` and `last_human_at`).

Child threads (spawned by another session) never show a question or a
failure: their parent's wave answers for them. Manual recovery still shows.

## Rules that must not drift

- **Filter at read, never delete.** No reader removes or rewrites a stored
  record. A record that cannot be resolved (no timestamp, no branch on record,
  a pruned instance) is hidden.
- **A read that cannot answer hides the item.** `GitReader` maps any rc other
  than 0 and 1, a timeout or an exception to None, and None hides. A kind
  whose reader raises hides itself only. If the whole list cannot be built,
  the refresher pass leaves The Ark and the rooms as they are.
- **No relative times in text.** Every time is `<t:EPOCH:R>`, which the client
  keeps current, so an unchanged state renders the same digest and the
  refresher's skip holds. Uptime is in the Status field, not the footer
  (footers do not render the markup).
- **Computed once per pass.** The refresher snapshots the thread index on the
  loop, runs `collect` in a thread, and hands each room its slice
  (`by_repo`). Git answers are cached 60s with a 5s timeout.
- **Hidden repos' items still show**, marked "(hidden)", in The Ark. Their
  rooms are not redrawn by the refresher (`repo-hide.md`).
- **"Your turn" is a count line** (`YOUR_TURN_AS_COUNT`). The live check on
  2026-11-15 estimated 22 such threads across 9 repos, more than the whole
  15-line budget, and mostly finished research. Lists stay for every other
  kind.

## What closes an item, and when the list hears about it

Every finished turn redraws (`_after_turn` stamps the thread and requests a
pass). Off a turn, `waiting.notify_changed()` asks for a pass at: a key
request posted, Allow or Deny; the update card going out; a peer card, Run
once and Decline; a chain pausing; a merge failing; a merge held; a scheduled
merge finishing; the Resume parent tap. The listener is set in `startup.py`
and is thread-safe.

Left to the 5-minute sweep: account alerts and login expiry, a failed merge
resolved outside a turn (a branch merged or deleted by hand), and every
time-based expiry.

## Layout

The Ark: title "The Ark", description "**Needs you (N)**" and the grouped
lines (at most 15, then "and N more: ..." by kind, under 4096, titles shrink
before any item is dropped), or "Nothing is waiting on you.". Fields: Status
("Running N", an account warning, "Up since"), then Usage. Footer: version and
PC name. Buttons: Refresh and Stop All, then New Repo, Claude Login, Provider.
The orphan count left The Ark; `/branches` prints it.

## What this does not establish

The harness pins each kind's show and hide rules against fakes, and `--live`
showed 0 of the 21 stored failed merges and 0 of the 11 stored paused chains
(all older than their window, one with its instances pruned). It does not show
that every close event redraws within seconds on the running bot; that is
checked after a restart. The your-turn number is an estimate from instances,
because thread stamps only start with this change, so it is an upper bound
that counts a turn as answered only when a newer run started. Whether the
stale deploy items (a repo whose deploys the owner never approved) belong in
the list is a judgement left to use.
