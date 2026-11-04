# The plan check

The first step of every /chain preset and of Build & Ship. One session that never
saw the conversation reads the plan against the repo. If it can show a defect, the
session that wrote the plan decides what to do about it. The code is
`bot/engine/plan_check.py`, the harness is `scripts/test_plan_check.py`.

It replaces nothing at runtime: `_review_plan_loop` stays, since `auto_fix.py` and
cards already posted still use it. The Autopilot, Autopilot (Hold) and Review Plan
buttons are gone from new cards. Their handlers stay for old ones.

## Why fresh

The old loop resumed the author and asked for its best revisions, so it reviewed
its own reasoning and always had something to say: 754 of 781 chains had at least
one revision round, and whether any were real was never recorded. A session with no
history reads what the plan says and what the code says, which is the only thing it
can be right about here.

The input is the owner's messages, the repo goal, the plan and the git history of
the code it touches. `prior_art.CHAIN_INSTRUCTION` is stripped from that history,
because it says to ask the user and nobody can answer here.

## Why one pass

A loop gives a reviewer another chance to find something and an author another
chance to give in. It also needs a way to halt, and 10 chains ended
`review_did_not_converge`. One look, one decision, then the build.

## Why narrow

The prompt allows four things, each shown with a file and a line: the plan is wrong
about the code, it misses a part of the system it must touch, it re-adds something
reversed without saying why, or it leaves out something the owner asked for. Nothing
found is a good answer. The judge finding
(`study/findings/2026-11-04-a-fresh-judge-did-not-catch-what-the-owner.md`) says what
a fresh reader can catch: what the repo can show, and little of what only the owner
would notice. That record is about finished work and is not re-run here.

## Why the author adjudicates

The reviewer can be wrong about intent and knows it has not seen the conversation.
The author has, so it applies or rejects each Critical and High item with a reason,
and it must reject anything that changes what the owner asked for. Medium and Low go
to the chain's deferred list with no further run.

Being accepted is not proof of a real defect, since the author may simply give in.
The kill bar does not count acceptance as evidence.

## The override write

The revised plan only reaches the build through `set_chain_plan_override`, because
the override outranks everything else in `_extract_latest_plan_text`. It keeps the
attached history, survives a reboot, and gives Build & Ship an override it did not
have before. It is adopted only when the `~~~plan` block is complete and non-empty,
the run finished without a question, and it fits `_BUILD_PLAN_INJECT_MAX`. A plan
over that cap stays as it was instead of being cut.

## Why it fails open

The check is an extra look and must not become a new way for a chain to die. A
reviewer crash, a usage limit, a missing `review-status` block or an unusable revised
plan leaves the plan unchanged, posts one quiet line, writes a record row, and the
build goes on. It never calls `_exit_chain_needs_input`. `_needs_revision` reads a
missing block as "no revision", so the check reads the block itself and treats a
missing one as could not check.

Two things do stop the chain:

- **A Kill** of the reviewer or the author. Kill means stop.
- **A reboot drain.** The step stops with the chain's state kept at `plan_check`, so
  boot recovery runs the check again instead of building unchecked. `spawn_from` no
  longer gates `check_session` on `resume_session`: a replay queued for this fresh
  session would put the thread in the drain queue and make boot recovery skip the chain.

That drain case exposed a bug in every step. A spawn refused during a drain returned
None, the chain called it a failure, and the chain state was cleared. The chain now
returns before that guard while draining.

## What must not drift

- The build resumes the author or the apply instance, never the reviewer.
  `current_id` moves only to a completed `PLAN_CHECK_APPLY` run.
- Both origins stay in `_NO_DIRECTIVE_ORIGINS`. The reviewer's input is quoted text
  from elsewhere, and a `/chain` in an answer must dispatch nothing.
- Both stay behind the read-only floor, with no result card and no workflow buttons.
- The reviewer is not a turn: it takes no channel and leaves the thread's tag alone.
  The author resumes the thread's session, so it holds the channel.
- Neither retries on a timer. A usage limit means the check could not run.
- Both spawn with `copy_branch=True`, so they read the tree the build will start from.
- The reviewer's spawn strips the tapped card's buttons, since a second tap during
  the check would otherwise queue a second chain.

## The record and the kill bar

Each check appends a row to `data/plan_checks.jsonl`: time, repo, thread, source,
session, reviewer, Critical and High count, Medium and Low count, applied, rejected,
adopted, outcome (`clean`, `revised`, `kept`, `could_not_check`, `skipped`), reason,
and both costs. The thread id is what joins it to `data/missed.jsonl`.

The bar is in `study/findings/2026-11-04-does-a-fresh-one-pass-plan-check-catch-real.md`,
written before the first run: read 30 days of rows, confirm each accepted revision
against the code at that commit, and if fewer than 1 in 5 checked chains has a real
defect, set `PLAN_CHECK_ENABLED` off.
