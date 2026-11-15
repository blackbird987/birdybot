# One-tap choices: a tap is a typed message, nothing more

The owner reads on a phone. A reply that ends "keep the old name, or rename
it?" costs them typing a sentence the session already wrote. The CLI's own
question tool (AskUserQuestion) was meant for this and fired zero times in five
days, while about one reply in twenty-five ended on a question with a few fixed
answers. So the session writes the answers itself:

    [BOT_CMD: /choices]
    ~~~choices
    Keep the old name
    Rename it
    ~~~

and they become a row of buttons on the result card. A tap sends
`User picked: <label>` into the thread as the tapper's next message.

Code: `bot/engine/directives/choices.py` (parse, offer, close, claim, expiry,
log), `buttons.choice_row_specs` and `_fit_rows`, `lifecycle.send_result(choices=)`,
`query._execute_query` (reads the directive, opens the offer), and
`interactions._handle_choice_tap`. Prompt: the "One-tap answers" block in
`config.BOT_CONTEXT`. Harness: `scripts/test_choices.py`.

## Things that must not drift

- **A tap goes through `commands.on_text`, like typed text.** Never through
  `_replay_to_thread` or the orchestrator's resume path: those clear
  `offer_usage_limit_choice`, which skips the usage gate. Through `on_text` a
  tap meets the usage gate, the budget gate (`refuse_if_over_budget`), the
  guest rate limit, the channel lock and the Queued card, exactly as a typed
  message does. The ctx is built like `on_message`'s: the tapper's own access,
  `source="choice_tap"`, the tapper added to the thread's `user_ids` (the
  shared `_track_thread_user`, which is what keeps a guest's thread confined
  after a restart).
- **`choice_tap` is human.** `HUMAN_SOURCES` (`bot/platform/base.py`) holds it
  with `user_message`: both reset the spawn-wave cap and skip the child's
  BLOCKED notice. A tap does not run fast repo commands, since the label is the
  session's text, not the person's.
- **The id holds the index, the label is looked up.** `choice:<iid>:<n>`. A
  tap can only send a label the session offered, and the label never has to
  fit the 100-character id.
- **Claimed with no await between check and removal** (`claim_for_tap`). A
  double tap, or a second button on the same card, finds nothing and gets the
  ephemeral "out of date" line. So does a tap on any card but the one the offer
  was recorded on.
- **One offer per thread, closed by the next turn.** Every turn start
  (`_execute_query` and `run_instance`, right before `begin_task`) calls
  `close_offer`: `typed` for a typed message, `superseded:<source>` for
  anything else. A judge, plan check or machine-link answer is not a turn and
  does not close it, and a TL;DR leaves the card it was tapped on alone
  (`docs/design/tldr.md`), row included (`_KEEPS_CHOICE_OFFER_ORIGINS`).
- **Close by stripping the row, never by redrawing from specs.** A workflow
  step strips the source card's workflow buttons (`step_spawn`), and a redraw
  from `action_button_specs` would put them back. `strip_button_rows` rebuilds
  the view from the message itself, less the rows holding a `choice:` button.
- **Redraws keep the row only for the offer's own card.** Mode, Expand,
  Collapse, Merge and Discard redraw a card; they pass the labels only when
  the open offer's instance and message are that card's
  (`offer_labels_for`). `/list` never shows a choice row.
- **Five rows.** The choice row goes first. To fit, the Mode row goes, then
  the standalone Missed row, then Branch/Share. Expand is never dropped, and an
  expanded card holds a row back for Collapse.
- **No offer nobody can tap.** The offer is recorded only when `send_result`
  returns the id of the message that carries the row. A guest with buttons off
  (`shows_buttons` false), the marker-only suppression and the plain-text
  fallback record nothing.
- **Mask before you scan.** The directive is found on the tilde-masked text
  and quoted examples are skipped, so a `/choices` inside a spawned child's
  brief never arms here. A reply that also hands off (`/chain`, `/spawn`), or
  a session with a running autopilot chain, gets no buttons.
- **Refuse, never truncate.** A label over 40 characters, starting with `/` or
  `!`, or holding `[BOT_CMD` or `~~~` is left out with a notice. Labels are run
  through `redact_secrets`. Fewer than two usable labels means no row; more
  than five keeps the first five.
- **Offers live in state.json under `choice_offers`**, not on the Instance:
  instances are pruned, and an offer must survive a restart. The autonomy loop
  expires offers older than a week.

Sandboxed and guest sessions may offer choices on purpose. A tap sends only
the label, as the person who tapped, with that person's access; they could
have typed it. No path, key or command rides on it.

## Measurement

Every event goes to `data/choices.jsonl`: `offered`, `tapped`, `typed`,
`superseded`, `expired`, `refused`, `suppressed`. `scripts/choices_report.py
--days 14` prints the tap share, tapped / (tapped + typed). TODO.md holds the
decision for 2026-11-30: fewer than a third of answered offers tapped, and the
feature goes. The eval check `unoffered_choice` (flag only, attributed to
`BOT_CONTEXT`) counts chat replies that end on an either/or question with no
buttons, judged from raw text because `evaluate_instance` runs before any
offer exists.

## What this does not establish

The tap path is pinned by the harness, not by a live tap: the live check in
the build confirmed the row and its ids render on a real card, and a real tap
by the owner was still outstanding. Whether people tap at all is the open
question the measurement above exists to answer.
