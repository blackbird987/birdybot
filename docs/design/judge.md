# The judge, the missed marker and the step-back

The goal: what ships does what the owner meant, holds for the whole class of
thing it is an example of, and the owner's attention goes only to what nobody
else can judge.

## Why the author does not check itself

The code review runs resumed inside the author's session and the critical pass
only asks the author to check itself in chat. Both read the author's reasoning
first. The judge is a separate session that never sees it. Many of the owner's
corrections in the last 30 days (10 of 25) were about looks or feel, found by using
the thing, so "only you can judge" is a first-class answer, not a failure of the
judge.

## The pieces

- `config.SCOPE_QUESTION`: one wording shared by the critical pass, plan review,
  the judge and `/stepback`. General on purpose. It includes the reverse failure,
  a framework built for a true one-off.
- `bot/engine/missed.py`, `data/missed.jsonl`: append-only record of work that got
  past everything. The Missed button on a finished card, or `/missed [note]`. This
  is the defect-escape count every other number here is checked against.
- `bot/engine/judge.py`, `data/judge.jsonl`: the judge and its record.
- `bot/engine/stepback.py`, `/stepback <repo>`: proposes at most three items, by
  hand only, applies nothing.
- `scripts/judge_replay/`: the replay that decided whether the judge is on.

## What the replay showed: off

The judge failed its bars (`study/findings/2026-11-04-a-fresh-judge-did-not-catch-what-the-owner.md`),
so `JUDGE_REPOS` stays empty and the resumed code review stays. On 29 held-out cases
it caught 1 of 12 misses against a bar of 50%, 0 of 5 too_narrow, 0 of 21 review-fix
issues, and flagged 30% of clean work against a bar of 20%. Where it said fix on a
miss it raised something else: the owner's corrections came from knowledge or from
use that the input does not hold. Whether its other findings are real was not scored.

Listing a repo by hand still works, with nothing vouching for it. Before anyone does,
compare against `data/missed.jsonl`, which is what the marker is for.

## Must not drift

**No author text in its input.** `judge.build_input` takes the owner's messages,
the repo goal, the diff and the verify block, and has no parameter for anything
else. The owner's messages come from Discord's thread history, not from the
session's user turns, because those hold injected plans the author wrote.
`verify_output_of` keeps the last ```verify block and drops the prose around it.
`_commit_diffs` leaves commit messages out. The system prompt is returned before
Recent Sessions, siblings and pinned context are added (`_build_system_prompt`).

**It fails closed.** Unparseable text, a crash, a usage limit, a missing diff or
no owner words are all `could_not_check`, and that holds. Nothing but a well
formed `VERDICT: pass` is a pass. This is the opposite of `prior_art`, which fails
open because it only adds context.

**Read-only floor.** Explore with `bash_policy="none"`. Everything it needs is
fetched by the bot. In `_NO_DIRECTIVE_ORIGINS` because its input quotes other
people's text, and nothing in its input is dispatched.

**Bot-side switch.** `JUDGE_REPOS` is in `bot/config.py`, not a repo file, because a
session can write to its own repo. With a repo not listed nothing changes: the two
call sites check `enabled_for` before doing anything, and `scripts/test_judge.py`
pins both.

**Not a turn.** It does not occupy the channel, does not bind its session to the
thread, has no result card, no timer retry and no parent notice (`run_instance`
returns early for its origin), and leaves the thread's near-limit tag alone. A bound judge session would amputate the thread's history.

**Chain path.** After verify. `fix` goes back to the author session to repair,
re-verify and be judged again, `MAX_REPAIR_ROUNDS` times, then holds. `only_you` and
`could_not_check` hold even under `preset=ship`, through the manual-verification
hand-off. Continue resumes at the next step, not the judge. With nothing after
verify there is no Continue, because resuming would re-run verify.

**Chat path.** On idle, after the thread's own session committed code. Master is
shared by parallel sessions, so commits are taken from that session's own
`git commit` tool calls (`bot/engine/commit_calls.py`), never a head-to-head range.
95% of those calls are `git commit -q`, which prints no hash, so a call with no hash
is matched by a log line it printed or the exact subject it passed, against the
repo's log within a few hours. A call that does not match exactly one commit, or
that runs `git commit` twice, is `could_not_check`, reported once. The work is
already committed, so these only report. `fix` gets one repair turn, recorded and
announced before it starts because starting one returns only when its turn is over.
The first idle after it closes the round, whatever it committed, so later work is
still judged. A message sent in the five minutes between the repair ending and that
idle is closed with it, which is the accepted limit of telling them apart by time.
It does not fire while a task, watch or pending prompt is live (`idle._apply_sleep`
guards), and skips a bot that is draining or over budget without recording anything,
so a later idle tries again.

**Sandboxed repos.** Git reads use `sandbox.GIT_SAFE`, files use
`fsutil.read_contained_text`, and the run goes through `core._prepare_spawn` like any
other.

## What it costs and what it cannot do

- A thread with no owner-typed message (a spawned child, slash-started work) has
  nothing to judge against, so it gets `could_not_check`. In listed repos a chain
  started that way always holds.
- A read-only judge can technically Read session files on disk. The prompt forbids
  it, and nothing stops it. That is a limit of the floor, not a boundary.
- It reads no screenshots and runs nothing, so a looks or feel problem can only be
  `only_you`.
- After a held chain that was not merged, the later done and release commits get one
  chat judgement. A merged chain closes its thread and is skipped.

## Follow-ups

- [ ] Run `/stepback` on a timer, or after a third fix to the same area.
- [ ] Score whether the judge's other findings are real: the replay only asked whether
  they matched what the owner caught.
- [ ] Give the judge a way to see the screenshots verify took.
- [ ] Decide what a thread with no owner words should be judged against, such as the
  parent's message for a spawned child.

Harness: `python scripts/test_judge.py`, `python scripts/test_missed.py`.
