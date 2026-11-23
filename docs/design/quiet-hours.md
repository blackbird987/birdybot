# Quiet hours, and the chain's own merge decision

Two changes that share one goal: finished work should not wait on the owner
for a call the bot can make. Harness: `scripts/test_quiet_hours.py`. Finding:
`study/findings/2026-11-20-held-branches-were-never-discarded.md`.

## A /chain ships unless it says why it holds

`bot/engine/directives/chain.py` (`_parse_chain_args`, `_resolve_hold`).

- No preset ships. The no-opt-in "hold" from `load_workflow_policy` governs
  unattended auto-merges only (the veto window, Build-button runs). A repo
  whose `.claude/workflow.json` sets `autonomy` to hold, and a confined
  session (guest or sandboxed), still hold by default
  (`step_policy.repo_chose_autonomy`).
- `preset=hold` and `preset=verify` need `reason=`. With no reason they run as
  ship and the start notice says so. A confined session keeps exactly the
  preset it asked for: it must never be able to turn its own chain into a
  merge.
- The reason and the "decided to merge" flag ride in the chain kwargs
  (`set_chain_kwargs(hold_reason=, decided_merge=)`), so a paused chain the
  owner continues, or one boot recovery resumes, still reports them. The
  `get_chain_kwargs` tuple keeps its `(label, silent_close)` shape.
- The chain's end card carries `DECIDED_MERGE_NOTE` in its Notes when the
  chain merged on its own (`get_chain_decided_merge`), and only the chain
  does: Done, the veto auto-merge and fleet ship were the owner's call. A held
  chain's end card says "Held: <reason>." (`docs/design/session-recap.md`).

## Quiet hours

`bot/engine/quiet.py`, `config.QUIET_HOURS` (default `03:00-09:00`),
`config.OWNER_TZ`, `config.QUIET_HOURS_BLOCK`, `/quiet`.

- **Presence beats the clock.** Inside the window, the block is sent only when
  the owner has not been seen for 20 minutes. Seen means a message or any
  interaction (tap or slash command) from the owner's author id
  (`bot.py` `on_message`, `on_interaction`). A guest never counts, and a test
  webhook passes as owner but is not them, so it does not count either.
  `ThreadInfo.last_human_at` is not reused: it is per thread and counts guests.
- **The block goes in the turn prompt, never the system prompt**, right above
  the owner's words (`prompt_build._build_command`). The system prompt must
  stay byte-stable for caching; the harness compares its bytes at 04:00 and
  14:00.
- **Owner turns only**: `is_owner_session`, not confined, and an origin in
  `quiet.QUIET_ORIGINS`. Read-only reviewers, mechanical steps and the machine
  link keep their own formats.
- **What it allows.** Act on choices that can be undone and list them under
  "Decided while you were away:"; treat the owner's own request as approval
  for /chain (the hold rules still apply). It still stops for a missing key,
  deleting data, anything sent to people or spending money, and a pure taste
  call with no safe default.
- **Stamped on the run.** `Instance.quiet_hours` is set when the block was
  sent. The eval's unoffered-choice check stands down on those runs and a
  `quiet_question` flag (owned by `QUIET_HOURS_BLOCK`) counts the ones that
  still asked. `_check_critical_pass` is unchanged.
- **Needs you.** `lifecycle.finalize_run` sets `Instance.decided_away` when a
  quiet run's reply has the "Decided while you were away" line. The list shows
  `decided_away` (just above "Your turn") until anyone writes in the thread,
  48h at most. Filter at read, like every other kind (`needs-you.md`).
- `/quiet` shows status, `/quiet 23:00-07:00` sets a window (may cross
  midnight), `/quiet off`, `/quiet default`. Owner only. Stored in
  platform_state `quiet`, with `owner_seen`.

## What this does not establish

The harness pins parsing, placement, gating and the Needs you item against
fakes. It does not show that sessions follow the block, or write honest hold
reasons: the model writes both, so the eval flag and the merge line are how
that gets measured. Build-button runs still need the CLI's own question tool
to block an auto-merge; a question asked in prose is not detected
(TODO in `lifecycle._maybe_schedule_auto_merge`).
