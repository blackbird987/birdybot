# One way of recording findings (`scripts/study/`, `bot/engine/findings.py`)

A session starts knowing nothing its repo learned before, so it re-runs what was
already tried, and a user ends up re-explaining why not. Ev-nova-remake answered
that with a registry, one record per question, and two repos copied it. This is
that idea as a default any repo can have, without another copy.

## What counts as a finding

Something a later session would waste work or repeat a mistake without. A tried
approach that fails, a measured limit, a decision and why, an open question worth
keeping. If nobody could waste work for lack of it, it is not a finding.

The neighbours, so nothing is written twice or nowhere:

- **CHANGELOG** is what changed. **TODO** is what to do.
- **A design note** (this folder) is how something is built and its invariants.
- **Auto-memory** is user preferences. It is per account and untracked (aiagent
  has 20 files under one account and 8 under the other, smartmoney 13 and 4), so
  a finding never lives only there.

## The rule

Read the state and grep before proposing work. A `shut` record is answered with
its id and not re-run. Disagreeing means writing a superseding record, not
ignoring the old one. A record lands in the same commit as its work. The last
paragraph of the body says what this does not establish.

Outside research repos no check can notice a missing record, because there is
nothing to compare it against. So a rule asks instead of a gate pretending to
know: the session-wide one is in the global CLAUDE.md. `COMMIT_PROMPT` asks too,
but it only runs on the Commit button, and new cards no longer carry it, so only
a card already posted can run it. `sessions.py` only strips its text from a thread
topic. A `/chain` build commits on its own and does not see that line.

## The record

`study/findings/<id>.md`, a `+++` TOML header over plain markdown:

```
+++
id = "2026-11-03-short-slug"
title = "..."
date = "2026-11-03"
status = "shut"
question = "..."
verdict = "..."
+++
What was asked, and why it was worth asking.

What was done, and what came back.

What this does not establish.
```

Required: `id`, `title`, `date`, `status`, `question`. Optional: `kind`
(`finding` by default, or `decision`), `verdict`, `supersedes`, `superseded_by`,
`tags`, `scripts`, `source`. The body has a word cap from config (default 300).
Unknown fields warn and do not fail, so a newer record never breaks an older
branch.

Statuses: `open`, `stands`, `shut` (tried, does not work, do not retry), `void`
(the design was broken, no verdict earned), `superseded`, `exploratory`. A
decision record is `stands` or `superseded`. Anything not `open` has a verdict.

## The tool

```
python3 /home/quincy/Programming/claude-telegram-bot/scripts/study/study.py <command>

init                                        start a study folder at the git root
new --title T --question Q [--kind] [--status] [--verdict]
supersede NEW OLD                           writes both ends, flips the old status
state                                       prints what a session reads first
check                                       reports every failure, non-zero on any
```

`state` prints the rule, shut records, open questions, standing verdicts newest
first, decisions, and an "Old sources not yet read" section from `[[unread_source]]`
in config. It is capped from config (default 20000 bytes), clipping standing
verdicts first, oldest first, never shut records, and counts what it clipped. No
clock in the output, so two runs compare equal.

## Why it is built this way

Each choice is a thing the three existing copies paid for.

- **Called from one place, not copied.** The copies drifted to 402, 674 and 322
  lines of `check.py`, and their `ORIGIN.md` files list the differences by hand.
- **State printed, not committed.** 572 of 1,934 Ev-nova-remake commits touch the
  state file. It is also a merge conflict waiting for every parallel branch.
- **Ids that cannot collide.** funny-rock-polisher renumbered 5 records at one
  merge (44729b6, F-B016 to F-B030 and others). A date and a slug from the title
  need no counter. `check` warns about any other id shape, not fails, so a
  migrated record may keep the id older records cite.
- **A byte cap, not a line cap.** Ev-nova-remake and funny-rock-polisher ship 43 KB
  and 45 KB of state per session, against strategy-lab's 20 KB byte cap, and
  Ev-nova-remake had to drop its snippet count from 35 to 7 to stay under its line
  limit.
- **No git hook and no shared git setting.** Those take effect in every worktree at
  once (151 in Ev-nova-remake, 102 in aiagent), so they cannot be rolled out while
  sessions run. Everything here is inert until a repo has `study/config.toml`.
- **Warn only.** A blocking check that can stop work gets switched off. See the
  docstring of `bot/engine/prior_art.py`.
- **Nothing at `/repo create`.** No empty scaffolds: the folder appears at the
  first finding, with `init`.

## Where the bot touches it

- **`bot/engine/findings.py`** reads dead ends for a repo path. It handles
  `findings_dir` with status `shut` (Ev-nova-remake, funny-rock-polisher),
  `records_dir` with status `dead` (strategy-lab, whose `common/` subfolder is not
  records) and the shared layout.
- **The sensor** (`docs/design/sensors.md`), warn-only, the repo's own checker first.
- **Plan review and the /chain brief** (`docs/design/prior-art.md`): the bot fetches
  the list because the reviewer has Bash closed. The floor stays closed.
- **`COMMIT_PROMPT`**: one line asking whether anything was established.

## Three forks stay forks

Ev-nova-remake, funny-rock-polisher and strategy-lab keep their own `tools/study/`
for now, because their required fields differ. Every command of the shared tool
refuses to run in a repo with that folder and names it. `new`, `supersede` and
`init` would write records that repo's checker rejects, and `state` and `check`
would report on the wrong folder or against the wrong rules, and an empty state
reads as "nothing was ever tried". Whether they converge gets decided once five
repos run on the shared tool.

The tool walks up to `study/config.toml` and stops at the git root, so a worktree
forked before the repo had a study folder never reads the main checkout's. `init`
writes at the git root, not the current directory: a repo registered as a subfolder
of its git root (aiagent) has a worktree whose working directory is the root.

## What must not drift

- **The record stays plain markdown, readable without the tool.** A session with
  only `grep` gets everything. The tool only checks and prints.
- **Everything the bot does with it fails open** and does nothing in a repo with no
  `study/config.toml`. A broken record costs itself, an unreadable config costs the
  list, and neither costs a plan review or a chain.
- **The sensor never blocks.** Its failure must not reach `failure_text`, which is
  what starts a fix session.
- **The tool stays standard library only**, Python 3.11+, so it runs from any repo
  with no install. The bot's reader parses the header itself, so `scripts/study/`
  never imports the bot and the bot never imports `scripts/`. A record the tool
  writes must read through both, which `test_findings.py` covers.

Harnesses: `python scripts/test_study.py` (each test breaks one rule in a clean
project), `python scripts/test_findings.py` (the three layouts, a read-only live
check on the three real repos, the sensor, plan review).
