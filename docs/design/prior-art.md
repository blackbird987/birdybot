# A plan is checked against what was already tried (`bot/engine/prior_art.py`)

An idea that was built, shipped and removed on purpose kept coming back as a
fresh proposal, because the session judging a plan only knows its own
context. The worked example: cdc54c8 (2026-06-18) auto-armed a wake when a
reply promised to keep watching, d5f8aa8 (2026-07-02) removed it because it
fired on prose that merely discussed a job, and it came back as a proposal
weeks later. The removal sat in `git log` of the very file being changed the
whole time; nothing asked anyone to look.

So the bot looks. `prior_art.collect(repo_path, plan_text)` takes the files a
plan names that exist in the repo (a bare filename only when exactly one
tracked file has it) and the backticked names that look like code, and reads
three things from git: the newest eight commits in each file's whole
history whose subject opens with a removal verb (tagged `[reversal]`), the
last five commits per file, and commit messages mentioning each name. git
greps every line of a message, so a match on a body line only is kept
untagged and is the first thing a tight cap drops. Reversals are
listed first and are the last thing a tight cap drops. The result is prepended
to the judging step's prompt the same way prior deferred items already are.

Why the bot fetches rather than telling the session to: **the plan reviewer
cannot run git.** It runs behind the read-only floor, and
`_enforce_readonly_floor` closes Bash along with the write tools because Bash
is a write backdoor. It keeps Read and Grep, which is why the review prompt
also sends it to CHANGELOG.md, CLAUDE.md and docs/ for the idea under another
name.
An instruction to "check the history" would also be the soft guard, skipped
exactly when the session is confident.

What it deliberately does not use is pickaxe (`-S`/`-G`) across the whole
history: on AIAgent (about 6,100 commits) that is about 3.8s per term, against about
30ms for a path-limited `git log`. The whole collection for 8 files and 6 names
there takes about 0.6s. Everything fails open: not a repo, a non-zero exit, a
5s per-call timeout or the 10s overall budget is no finding and a debug line,
never an exception and never a blocked step.

Three consumers:

- **Plan review**, both the Review Plan button (`on_review_plan`) and the
  autopilot loop (`_review_plan_loop`, read once before round 0 and reused).
  `PLAN_REVIEW_PROMPT` asks for a High revision tagged `History` when the plan
  re-adds something a `[reversal]` removed without naming it and saying what
  differs, and for a `PRIOR_ATTEMPTS:` line in the review-status block.
- **`/chain`.** `CHAIN_CONTEXT` asks the chat session to check git itself (it
  has Bash) and to write a `Prior attempts:` line into the plan.
  `_handle_chain_directive` then attaches the block to the stored plan with an
  instruction to ask the user with AskUserQuestion before re-adding a removed
  thing, which pauses the chain like any other question.
- **The weekly prompt review.** `build_review_input` appends each owning
  block's own edit history (`prior_art.block_history`, following the block's
  line range in `bot/config.py` with `git log -L`), under its own 1500-char cap
  outside the table's, and the brief tells the reviewer to drop a proposal
  that puts back removed wording without naming the commit.

Two evals measure whether any of it is read: a review handed the block whose
status block has no `PRIOR_ATTEMPTS:` line (owner `PLAN_REVIEW_PROMPT`), and a
dispatched `/chain` whose plan has no `Prior attempts:` line (owner
`CHAIN_CONTEXT`, parsed with the dispatcher's own `_extract_chain_directive`
so a quoted or nested /chain is as invisible to it as to the bot).

Three things that must not drift:

- **Never loosen the read-only floor to let the reviewer run git.** The bot
  reading history for it is the whole reason this is safe.
- **`PRIOR_ATTEMPTS:` stays above `DEFERRED:`.** `_parse_deferred_block` ends
  the deferred list at the first non-bullet line, so a status line placed
  after it would silently cut off every deferred item below it.
- **The /chain history survives the plan cap.** `_extract_latest_plan_text`
  splits the attached block off (`prior_art.split_attached`, searching from
  the end so a plan that quotes the marker is not cut at its quotation), caps
  and strips metadata from the plan half only, then puts the block back under
  its own allowance. Capping the joined text would drop the history, and its
  instruction, off the end of every long plan.

Knobs: `PRIOR_ART_ENABLED`, `PRIOR_ART_MAX_CHARS` in `bot/config.py`.

Harness: `python scripts/test_prior_art.py` (replays this repo's own history,
so a plan re-proposing the auto-armed wake must surface d5f8aa8).
