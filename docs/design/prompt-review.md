# The prompt reviews itself once a week (`bot/engine/prompt_review.py`)

Every session is scored, every recurring flag is attributed to the prompt
block that was supposed to prevent it, and until 2026-09-08 nobody read any
of it in aggregate. The loop was open: findings accumulated on disk and the
prompts that caused them never changed. `/promptreview [days]` runs it on
demand, and `autonomy_loop` fires it weekly off a persisted timestamp.

**It proposes; it never applies.** The reviewing agent is created in
`mode="explore"` with `bash_policy="none"` and `bash_policy_baseline="none"`,
which is the read-only floor: explore alone leaves Bash as a write backdoor
through `sed`, `echo >` and `tee`, so an agent reviewing its own constraints
could edit them. It reads one bounded table (25 rows, 8K chars, built by
`build_review_input` from `eval.build_digest`), never the eval directory. What
comes back is parsed into at most `PROMPT_REVIEW_MAX_PROPOSALS` (3) blocks and
posted to The Ark with Approve and Reject buttons. Approve does not write
anything either: it opens an ordinary build session in the repo's own forum
with the proposal as its brief, carrying the usual Review Code / Commit / Done
buttons, so the edit is made and inspected by the normal path rather than by
the reviewer. It is not a chain and it does not auto-branch: only `/bg`
branches, so this edits in place like any other build-mode message. That is the
whole safety argument, and it is asserted on the created `Instance` rather than
on the prose of a brief.

**Frequency is not correctness.** The finding that made this necessary is also
the trap it has to avoid. `tool_hygiene` flagged every Bash `cat`, `head`,
`sed -n`, `grep` and `find` as "should use the Read/Grep tool", and it was
wrong unconditionally: `bot/claude/provider.py` passes
`--permission-mode bypassPermissions` on **every** run, and that mode's own
system text instructs the session to prefer Bash for exactly those reads. It
fired 47,319 and 25,754 times in 30 days, about 73,000 of roughly 76,000 total
flags, and buried every real finding under a rule the harness itself was
telling sessions to break. Deleting the check because it was loud would have
been the right call for the wrong reason. So the agent must classify each row
before it may touch anything:

- `contradicted` (may become an edit) means the rule conflicts with another
  active instruction or with how the harness actually runs. This is the
  `tool_hygiene` shape.
- `disobeyed` (**report only**) means the rule is right and is simply not being
  followed. Deleting a rule because it is hard to follow is exactly backwards,
  and a top-of-the-table count is what makes it tempting.
- `obsolete` (may become an edit) means it fires so rarely that the prompt real
  estate is not paying for itself.

`EDITABLE_CLASSES` is the gate, enforced in `parse_review` rather than only
stated in the brief, and a dropped block is recorded in `ReviewReport.ignored`
instead of vanishing.

Four more things that must not drift:

- **Nothing it writes is dispatched as a directive.** Its whole job is to quote
  the documents that carry the literal `[BOT_CMD: /watch ...]`, `/spawn`,
  `/reply`, `/image` and `/repo add` examples, so a proposal that quotes one
  would otherwise arm it for real: a watch on a pid that does not exist, a
  spawn into a repo. The quoted-prefix guards in each parser do not help, since
  an example indented inside a fence starts with whitespace, not a backtick.
  `lifecycle._NO_DIRECTIVE_ORIGINS` is where that is settled once, for all
  three dispatchers (`deliver_images`, `_execute_bot_commands`,
  `check_wake_request`), keyed on origin rather than on the text.
- **Its result card carries no repo buttons.** The review's thread lives in The
  Ark, which is not a repo forum, so a Retry, Plan, Build & Ship or Branch
  button on it resolves against whatever repo happens to be globally active.
  `action_button_specs` returns early for the origin with only Kill, Log and
  the Expand row; the Approve and Reject buttons are posted separately, on the
  proposal embed.
- **A retired check's flags stop counting, but its files stay readable.**
  `_RETIRED_CATEGORIES` is skipped inside `build_digest`'s grouping loop and in
  `report.py`, deliberately **not** in `load_evals`: the per-instance view has
  to keep showing what was actually recorded, or an old session becomes
  unexplainable. `attribute_flag` degrades to `"unattributed"` for a retired
  category rather than naming an owner block that no longer has a check.
- **The weekly gate is a persisted clock, stamped before the run.**
  `should_run_now` takes the stored timestamp, not a tick counter: a reboot
  resets a counter, which would either fire on every restart or skip the week
  depending on which way the arithmetic fell. No stamp at all seeds rather than
  fires, so enabling the feature does not immediately spend a run on a window
  nobody asked about. An unparseable stamp reads as due. The stamp is written
  **before** `run_review`, so a crash mid-review costs one week, not an
  infinite retry loop.
- **A rejection is remembered by target, not by text.** `fingerprint` hashes
  the sorted `(file, flag)` pairs, so the same idea coming back reworded next
  week is suppressed by the bot rather than merely discouraged in the brief. A
  proposal that is genuinely new gets through because its targets differ.

`run_instance` is called directly here and `backfill_thread_session` is
deliberately **not**, for the same reason the chain runner does not backfill:
the review's session belongs to the review, not to a conversation. See "A
thread must always know its session" ([thread-session-binding.md](thread-session-binding.md)). The harness asserts that absence
structurally, so a later refactor that adds a backfill fails the suite.

Knobs: `PROMPT_REVIEW_ENABLED`, `PROMPT_REVIEW_WINDOW_DAYS`,
`PROMPT_REVIEW_INTERVAL_DAYS`, `PROMPT_REVIEW_MAX_PROPOSALS` in
`bot/config.py`, all inert without `EVAL_ENABLED`.

Harness: `python scripts/test_prompt_review.py`
