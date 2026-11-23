# End card: one card where a session used to end in noise

The owner runs about eight threads at once and comes back to one after hours.
A finished chain used to end in up to seven messages: the Done card and the
release card (each with its changelog, cost, tokens and turns), the step
"done" lines, "Couldn't be verified automatically", "Verified, but not
everything asked for was delivered", "Merged into master", and a separate
"Chain complete on branch" message whose buttons repeated the release card's.
None of them said what the thread was about. They had to scroll up to their
own last request to find out.

Now the end is one card:

    Login no longer drops the session                      (title)
    Shipped v1.4.0, merged into master · 4 files, +120 -8 · 2 open
    You asked   Fix the login that kept logging people out
    Before      • Users were logged out after every deploy
    Now         • Sessions survive a deploy
    For you     • Log in once on a real phone
    Next        1. **Add the expiry test**: Write a test that ...
    Notes       • 2 review leftovers filed in TODO.md
    bot · 1h 12m · Opus · abc1234                          (footer)
    [Add the expiry test] [Clean old sessions] [Do all]
    [Close] [Missed]

The shape is the same for every kind of work (a fix, a feature, research, a
decision, a refactor): what was asked, how things stood, how they stand now,
what is the owner's to do, what could come next. It does not sort work into
"problem" and "research", because a lot of it is neither.

Code: `bot/engine/recap.py`. Prompt: `config.RECAP_PROMPT` (with
`PLAIN_LANGUAGE_RULES`). Card: `platform.base.CardSpec`,
`Messenger.send_card`, `discord/adapter.send_card`. Harness:
`scripts/test_session_recap.py`.

## Where the words come from

- **Facts from the bot, not the model.** The status line, the version, the
  commit, the size (`git diff --shortstat target...branch`, three dots, so the
  whole branch and nothing master did meanwhile), the elapsed time and the
  model come from git and the instance records (`gather_facts`, `git_facts`).
  The version is read from the release commit's subject
  (`git_release._RELEASE_COMMIT_RE`), and a commit hash the session wrote is
  used only once `git log -1` confirms it exists. "Check by hand" and "Not
  delivered" come from the chain's own instances (`needs_manual_verification`,
  `verify_not_done`), walked through `parent_id` and `chained_from`.
- **The prose from the session itself.** A read-only recap turn
  (`InstanceOrigin.RECAP`) resumes the session with those facts and the
  owner's own messages, and answers in a ```recap block: HEADLINE, ASKED,
  BEFORE, NOW, FOR_YOU, NEXT (`label | full instruction`), IMAGE. It runs on
  the light model, at the read-only floor with Bash closed, posts nothing of
  its own (its status line is deleted), dispatches no directives and writes no
  history. The last block on the masked text wins.
- **The fallback is the facts alone.** No session, a reboot drain
  (`check_spawn_allowed`), a failed spawn or an unparseable answer gives a card
  built from the commit subject, the owner's first real message and the
  changelog. `prepare` never raises.

## Things that must not drift

- **Anything open keeps the thread open.** "For you" lines, Next items, a
  failed tag push and a held branch all count. Every bot-initiated close goes
  through `recap.close_thread`, which refuses while the thread's card has open
  items or its held branch still exists. "Still exists" is read from the
  branch instance (`_holds_branch`), not the record, so a branch merged or
  discarded some other way (a typed /merge, fleet ship) releases the gate
  too, and a pruned instance holds nothing. The harness fails any
  `.close_conversation(` outside `recap.py`. The
  card's Close button (`recap_close:`) is the owner overriding exactly this, so
  it calls the messenger directly. Notes (leftovers, the "merged on my own"
  line, deploy lines) are information, not open items.
- **Facts are read before the merge.** The branch is gone after it, so the
  chain and the standalone Done call `prepare` first and then
  `_finalize_merge(skip_close=True, report=...)`. The merge line goes into the
  card instead of its own message; the STOPPED warnings still post, because
  they are what stops the chain. A merge that fails still gets its card
  ("The merge did not land"), above the conflict banner it points at, and a
  merge that stops (`kind="merge_stopped"`: it landed on a tree that must not
  ship) gets one under the warning it points at. Neither closes the thread.
- **The step cards it replaces stay suppressed.** `lifecycle._end_card_follows`
  skips the result card for a completed Done (chain or standalone) and for a
  chain's release. Their status line names the version or commit instead
  (`finalize_status`). A Done or release that asks a question is not
  suppressed, and nothing is closed on it.
- **Next buttons send the full instruction.** The label is short; a tap sends
  `PICK_UP_PREFIX + label + ". " + message` (`choices.open_offer(messages=)`,
  `choices.tap_text`) through `on_text` like typed text. "Do all" (two or more
  items) sends one numbered list. The offer is closed by the next turn like any
  other choice row, and the recap turn itself keeps it
  (`_KEEPS_CHOICE_OFFER_ORIGINS`).
- **A held branch's buttons live on the card.** Merge / Discard / Diff point at
  the branch instance. Tapping one strips that row and moves the progress and
  outcome into a message of their own, so the card stays readable;
  `branch_resolved` then lets the gate close the thread if nothing else is
  open, unless the merge landed on a tree that must not ship.
- **The record is per thread and small.** `recaps` in state.json holds one
  record per channel (message id, time, repo, headline, open items, held,
  instance id, branch instance id). A newer card replaces it, Close drops it,
  and records older than 30 days are pruned on the next write. That is also
  how long the gate holds: Needs you stops listing a thread once someone
  writes in it, but the gate keeps refusing until a newer card, Close or the
  prune.
- **Needs you lists open items** (`waiting` kind `open_items`) until someone
  writes in that thread, and for at most 7 days. One item per thread, like the
  other kinds.
- **Pings follow attention.** The card pings when something is open or held,
  or when the entry point asked for a loud end (Autopilot). A clean ship is
  silent, and the close after it never pings again (`skip_mention=True`). A
  merge that failed or stopped (`_TROUBLE_KINDS`) never pings from the card:
  the conflict banner, the stop warning or the chain's exit mention next to it
  pings, so one event is one ping.
- **Discord limits.** Title 256, each field 1024, at most 25 fields, the whole
  embed under 6000 (`_fit_embed` shrinks the longest field first), at most 5
  button rows. An image the session names goes through `images.resolve_image`
  and `sandbox.instance_confined`, then is copied to `DATA_DIR/recap_images` so
  it outlives the worktree; the adapter drops it on an upload error rather than
  losing the card.

## What this does not do

It does not judge whether the work was good: that is review, verify and the
judge. It does not resume anything on its own: a Next item runs only when the
owner taps it. The recap turn costs one light-model turn per ending; a chain
that ends in a failure before the merge still ends the old way (the exit
mention and its buttons), since that is a question, not an ending. Its Done
and release cards stay suppressed all the same, so until the resumed chain
finishes and posts the card, their status lines ("done: v1.4.0", "done:
committed abc1234") are where the version or commit shows.
