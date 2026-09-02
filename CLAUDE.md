# Claude Code Bot

Discord bot for managing Claude Code instances remotely.

## Quick Start

```bash
python -m bot          # start the bot
```

## Key Paths

- **Entry point**: `bot/__main__.py` -> `bot/app.py:run()`
- **Config**: `bot/config.py` (reads `.env`)
- **Log file**: `data/logs/bot.log`
- **State**: `data/state.json`
- **Engine** (platform-agnostic): `bot/engine/commands.py`, `lifecycle.py`, `workflows.py`, `sessions.py`, `eval.py`, `report.py`
- **Platform layer**: `bot/platform/base.py` (Messenger protocol), `bot/platform/formatting.py`
- **Discord**: `bot/discord/bot.py` (orchestrator), `slash_commands.py`, `interactions.py`, `adapter.py`, `channels.py`, `forums.py`, `idle.py`, `tags.py`, `modals.py`, `monitoring.py`, `formatter.py`

## Discord Limits

- Max 5 button rows per View (truncate, don't crash)
- 2000 char regular message limit, 4096 for embed descriptions
- Slash commands are guild-synced (instant registration)
- 3-second interaction timeout — always `defer()` first
- `intents.members = True` needed for permission overwrites on category creation
- A forum has exactly **one** pin slot. A second pin is REJECTED (error 30047,
  "Maximum number pinned threads in this channel reached (1)") — it does not
  replace the incumbent, so anything already pinned must be unpinned first.
- Archiving a forum post clears its pin, and an archived thread rejects every
  field but `archived` (error 50083) — wake it before editing anything else.

## A copy-paste block must paste clean

Discord soft-wraps a long line to the phone's width by itself. A session that
hard-wraps the line *itself* — to "fit the phone" — bakes real newlines into
whatever the user pastes into their mail client, and they have to strip every
one by hand. On 2026-08-30 an email draft came out wrapped at 48 characters
inside a ``` fence; the newlines were in the message content, not the renderer.

- The rule lives in `WORKING_CONTEXT`'s Discord Formatting block
  (`bot/config.py`): inside a fence, one paragraph is one line, and a newline
  only ever appears where it is part of the content.
- Nothing unwraps fences on the way out, deliberately. A mechanical unwrap
  cannot tell an email paragraph from real code, an ASCII table or a diff, so
  it would mangle the cases it did not mean to touch.
- `eval._check_copy_block_wrapping` reports drift instead: a fence whose prose
  lines are consistently short and break mid-sentence is flagged, and the
  flag→owner map points at `WORKING_CONTEXT` so `/evals` names the block that
  was supposed to prevent it. Fences that look like code, tables or ASCII art
  are skipped — the check only fires on prose.
- Harness: `python scripts/test_copy_block_wrapping.py`

## A Control Room says what its repo is about

The repo control room embed used the filesystem path as its whole
description, so a forum of ten repos read as ten paths and you had to
remember which was which. It now leads with a one-line blurb and demotes the
path to `-#` subtext underneath.

The blurb is **derived, not typed in** (`bot/engine/repo_desc.py`). Every repo
already states its purpose somewhere, so first hit wins:

`.claude/repo.json` → `CLAUDE.md` → `README.md` → `pyproject.toml`
(`[project]`, then `[tool.poetry]`) → `package.json` → `Cargo.toml`

For markdown that means the first *paragraph* that reads as prose — the title
is skipped in both its `#` and underlined spellings, as are fenced code,
bullets, block quotes, HTML comments, rules, table rows and badge rows. A
wrong blurb is worse than none, and the title is the wrong blurb: the embed
already shows the repo's name above it. The paragraph, not the line: most
READMEs here are hard-wrapped, so the first *line* ends mid-sentence ("...take
a plain-language request like") and reads as truncation with no ellipsis to
admit it.

The joined paragraph is reduced to one plain line and fitted to 120 chars,
preferring the longest run of *whole sentences* that fits — "Agentic media
downloader." beats the first 118 characters of the paragraph it opens. A
word-boundary cut with `…` is the fallback, used when the leading sentence is
under 24 chars and too terse to describe anything. Emphasis is unwrapped by
*paired* regex rather than by stripping the characters — a blunt `_` strip
turns a sentence about `data/state.json` and `repo_desc` into mush, and
snake_case is exactly what a developer README's first line contains.

**The cache is the load-bearing part.** `refresh_control_room` runs on every
instance start and completion, so the hot path is stat-only: six `os.stat`
calls producing a signature, and the file bodies are re-read only when that
signature moves. The miss path defers its write (`mark_dirty`, picked up by
the 60s auto-save) rather than saving through — `state.json` is megabytes, and
a cache whose miss costs a full rewrite is worse than no cache. A repo that says nothing about itself caches the *miss*, so
it costs stats rather than six failed opens forever. The signature names every
candidate that exists together with its own mtime, deliberately not just the
newest one: a source restored from a tarball or read over a mount with a
skewed clock carries a *future* mtime, and behind it a newly created
`.claude/repo.json` would never move a maximum, so the manual override would
silently never apply. `_localise_paths` translates the recorded path for the
same reason it translates `repos` — the other machine's spelling never
matches, and every refresh would re-read the files the cache exists to skip.

`.claude/repo.json` is the manual override, written by `/repo desc <text>`
(`/repo desc <name> <text>`, `/repo desc clear`, bare `/repo desc` to show it
and its source). With no name it targets the repo of the **channel it was
typed in**, before the globally active one: the command is typed inside a
repo's forum, and defaulting to whatever was `/repo switch`ed to last writes
the sentence into the wrong repo. That needs both halves — the engine prefers
`ctx.repo_name`, and `cmd_repo` has to fill it in, because `_run_slash` builds
its ctx with no repo and the engine half alone is inert on the slash path.
`ForumManager.repo_for_channel` resolves it, falling back to the parent forum
so the Control Room post itself resolves like any session thread. It is wired
into `/repo` only: setting it in `_run_slash` would also change which repo
`/bg` runs in. A repo whose directory is gone is refused, not created —
`mkdir(parents=True)` on a stale registration would conjure an empty tree that
looks like the real thing. It is written into the *repo*, not into bot state, because
the sentence describes the repo and should travel with a clone — and it sits
next to the per-repo config files that already live there (`test.json`,
`workflow.json`, `sensors.json`, `deploy.json`). Other keys in the file are
preserved on write.

This repo deliberately ships **no** `.claude/repo.json`, so it exercises the
CLAUDE.md fallback in real use.

Harness: `python scripts/test_repo_desc.py`

## Discord Architecture (v0.3.0)

Forum-based: one ForumChannel per project/repo, one thread per session.
- Bot auto-provisions private category + The Ark (top-level dashboard channel) on startup
- Messages in The Ark → informational reply only (no session routing)
- Messages in forum thread → session auto-resumed
- Dashboard embed pinned in The Ark (auto-updates on instance start/complete)
- Per-repo control rooms live as pinned threads inside each repo's forum
- **A forum has ONE pin slot and the Control Room owns it.** Archive and
  monitor posts must never pin themselves — they used to, and racing the
  control room left 5 of 14 forums with the Archive pinned instead.
  `ForumManager.reconcile_forum_pins()` repairs this once on ready:
  unpin everything else, then pin the control room. Either edit wakes a
  sleeping post first — Discord rejects every field but `archived` on an
  archived thread (error 50083), so a post that auto-archived while holding
  the slot would otherwise keep it forever. No edits on correct forums.
  Harness:
  `python scripts/test_forum_pins.py` (add `--live` to read real state,
  `--live --fix` to repair; REST-only, safe against the running bot)
- Forum tags: active, completed, failed, cli, build

Key data structures in `bot/discord/forums.py`:
- `ForumProject`: repo_name + forum_channel_id + threads dict
- `ThreadInfo`: thread_id + session_id + origin + topic
- Persisted in `data/state.json` under `platform_state.discord.forum_projects`

## Build Isolation (Git Worktrees)

Build tasks use git worktrees for parallel isolation:
- Each build creates a worktree at `{repo}/.worktrees/{instance-id}/`
- Main repo always stays on master — no `git checkout` in the shared directory
- Parallel builds on the same repo work without conflicts
- Session files are copied between main repo and worktree project directories so `--resume` works
- Per-repo asyncio lock serializes git admin operations (worktree add/remove, merge, branch delete)
- After Done/Commit → Merge/Discard buttons appear in the thread
- Autopilot auto-merges after a successful chain completes
- `/branches` scans for orphaned branches and worktree directories

## The orphan safety-net (age + silence)

A run is never killed for being *old*. It is killed for being **silent**, and
its age only decides when we start asking. Three knobs, all in `bot/config.py`:

- `MAX_PROCESS_LIFETIME_SECS` (4h) — age past which the watchdog begins
  checking. On its own it kills nothing.
- `MAX_PROCESS_SILENCE_SECS` (30m) — how long a run past that age must have
  produced **no output at all** before it is reaped. This is the actual trigger.
- `MAX_PROCESS_HARD_LIFETIME_SECS` (24h, `0` = off) — age-only backstop, so a
  process that heartbeats forever without finishing is not immortal.

Why: on 2026-08-27 the age-only cap killed q-15433, a four-hour benchmark that
had produced output **five minutes earlier**, had not gone quiet for even sixty
seconds in its final two hours, and was sitting at 350 MB with live HTTPS
connections open. Raising the number would only have moved the guillotine — a
bench that farms work out in serial subagent batches can legitimately run all
day. Only two lifetime kills had ever fired; the other (q-15010) had been
silent for 43 minutes, which the new rule still catches.

The reap keeps the work. It used to return a bare `RunResult`, so four hours of
real work rendered as an empty red FAILED card. Both watchdog reaps — this one
and the memory guard's — now go through `_reaped_result_base`, which is where
"what a reap must preserve" is written down once: the recovered last assistant
text, the tools used (the chain reads that list to decide whether a build
changed code at all), the cost/token counters, and the `session_id` captured
from the init event, without which Retry starts over instead of resuming. Only
`error_message` differs between the two. `num_turns` is deliberately **not**
synthesised; the account-failover heuristic reads `>1 turn` as proof the
account took the turn.

Three things that must not drift:

- The failure wording **must contain the phrase "lifetime limit"**.
  `parser.is_account_agnostic_error` matches on it to suppress the no-turns
  failover heuristic — otherwise a reaped run is handed to the backup
  subscription to burn the same hours again.
- The session is told **before** `proc.terminate()`, not after. Terminating
  closes the CLI's stdout, which ends the reader loop, whose `finally` cancels
  the watchdog — same ordering rule as `reap_this_session` in the memory guard.
- The reap's return must stay **below** the two stand-downs, which both reaps
  share: a `result` event proving the turn finished anyway, and the session
  being in `_intentional_kills`. A user's Kill landing inside the reap window
  otherwise renders as a red FAILED card — and a failure with no turns is the
  account-failover branch's signature, so it can be restarted on the backup
  subscription. Same bug class as v0.101.11.

`WATCHDOG_TICK_SECS` (10s) is the poll cadence for this, the stall warning and
the memory guard. It exists so the harness can scale the whole watchdog down
instead of sleeping through real hours.

Harness: `python scripts/test_lifetime_cap.py`

## A thread must always know its session

Every resume path — the next user message, a fired self-wake, a tripped
`/watch`, a post-reboot replay — reads `ThreadInfo.session_id`. A turn that
finishes without writing that back is a turn the thread can never continue,
and the failure is *silent*: the wake fires, finds nothing, and drops.

Three rules, all pinned by `scripts/test_cooldown_session_bind.py`:

- **The bind happens before the cooldown early-return.** A usage limit is a
  pause, not an ending: `_do_cooldown_retry_locked` resumes that exact
  `session_id`. `commands._execute_query` used to return to schedule the retry
  *first*, so a limited turn never bound at all. On 2026-08-27 that lost three
  overnight children — limit at 00:38, retried on the backup account at 01:55,
  work finished by 02:45, wake-ups dropped as "gone/sessionless" while the
  instances held a perfectly resumable id. The bind is wrapped in a try/except
  precisely *because* it moved above the retry scheduling and the result
  delivery: a failing state write must not cost the turn its retry.
- **`lifecycle.run_instance` does not bind, deliberately.** A workflow step's
  session belongs to the step, not to the conversation, so the chain runner
  must not rewrite the thread's binding. Every *other* caller of
  `run_instance` does own the conversation and has to top the thread up
  itself. There are four — the cooldown auto-retry (`app`), `/retry`, the
  Retry button and continue-on-pay-per-use (`commands`) — and none of them
  did, so re-running the work as many times as you liked never restored a
  lost binding. Pay-per-use is the sharpest of the four: it is the manual
  twin of the cooldown retry, offered on the same usage-limit card. The
  cooldown retry also has to call `attach_session_callbacks` on its ctx, or
  it has no binding mechanism at all — the same omission already fixed once
  for post-reboot replays, see the comment on `_replay_to_thread`.
- **That top-up fills a gap; it never rebinds.** `backfill_thread_session` is
  the one implementation all four share. Chain steps hit usage limits too and
  land in the same retry function, and `/retry <id>` can be pointed at an
  instance belonging to another thread. Rebinding from there would let a plan
  or review step amputate a thread's chat history on the retry path while
  never doing so on the normal one, so the write only happens into an empty
  `session_id`. Worktree builds are refused outright — an isolated build
  session must not become a thread's chat session even when the slot is free.
  The harness asserts this structurally: *every* `lifecycle.run_instance`
  callsite in `app.py` and `commands.py` must be followed by a backfill, so a
  fifth caller added later fails the suite instead of silently losing threads.

`should_bind_session` is where the eligibility rule lives, and it stays narrow.
Success binds; a usage limit binds; **no other error does.** A crashed or
recovery-exhausted run can emit a *fresh* `session_id` carrying none of the
thread's history, and adopting that amputates the conversation. One deliberate
seam: a run that recovered onto a fresh session and *then* hit the limit does
bind, because the old id is already unreachable and the retry resumes the new
one.

`on_self_wake` distinguishes "thread gone" (drop) from "thread alive but
sessionless" (dispatch cold, log at WARNING). Lumping them together is what
made the loss invisible for eight hours.

## Interrupting a session (Kill / Steer)

A kill is only rendered as a quiet tombstone if `RunResult.killed_intentionally`
gets set, and that needs **both** halves:

- The caller must announce intent — `kill_and_wait(..., reason="kill")` (Kill
  button, `/kill`) or `reason="steer"`. The bare `kill()` defaults to
  `intentional=False` and produces a red FAILED card.
- The exit code must corroborate it (`runner.is_kill_shape`). Two shapes count:
  a **negative** returncode (kernel killed a process that ignored the signal)
  and **128+N** (the process handled the signal and exited cleanly). The Claude
  CLI does the second — `terminate()` on it returns **143**, never -15 — and
  accepting only the negative shape once made every Kill and Steer read as a
  crash. Windows can't be told apart at all (`terminate()` always yields 1), so
  it is a blanket True there.

Getting this wrong is not just cosmetic: a killed run has no output and no
turns, which is the account-failover branch's exact signature for "this account
fell over instantly", so an unrecognised kill can be restarted on the backup
subscription. The guard is the `if result.killed_intentionally: return result`
early-return in `_run_impl`, which must stay **above** that branch.

Both the Kill button and typed `/kill` go through one function,
`commands.perform_kill(ctx, inst, source_msg_id)` — they were near-copies, and
the drift between them is what let the button be fixed while the command kept
producing red cards. `source_msg_id` is the message the button sat on: present
means "I will rewrite this card myself", which is passed down as
`kill_and_wait(..., owns_card=True)` and lands on `RunResult.kill_owns_card`.
That flag — **not** the reason string — is what makes `lifecycle.run_instance`
skip its terminal edit of the progress message. `/kill` posts a separate
message and leaves it False, so lifecycle resolves the card to `⏹ stopped`
instead of stranding it on "thinking...". `steered` is reserved for
`reason="steer"`, where a replacement run really is starting.

Harness: `python scripts/test_kill_shape.py` (add `--live` to terminate the real
CLI and check the returncode it actually produces).

## Spawn-Wave Join (`bot/discord/orchestrator.py`)

When a session fans work out with `/spawn`, the bot joins the whole wave back
to the parent instead of making the user do it.

- Child state is **derived**, never stored: `ThreadInfo.session_id` -> newest
  `Instance` for that session -> status + `needs_input`. A child that parked on
  a question is `blocked`, not `completed` (finalize marks both COMPLETED).
- The wave roster is `Instance.spawn_dispatched_thread_ids`, sealed with
  `spawn_wave_sealed` when the dispatch loop ends. **A wave is not joinable
  before it is sealed** — otherwise a fast-failing first child closes the wave
  while its siblings are still being created.
- A child's callback resolves the wave whose roster **contains that child**, not
  the newest wave — a parent can have two waves open at once (wave 1 resumes it,
  it dispatches wave 2).
- On close, the parent's resume prompt carries each child's **full report file
  path** (`Instance.result_file`), not an excerpt. The human-facing post gets
  the excerpts.
- Full wave -> parent auto-resumes (`ORCH_AUTO_RESUME`, default on), bounded by
  the existing 12-wave cap since `callback_resume` doesn't reset it. Partial or
  timed-out release -> manual "Resume parent" button.
- Sweep in `autonomy_loop` (every ~5 min): partial-releases a wave past
  `ORCH_WAVE_TIMEOUT_MIN` (default 45; `0` = wait forever), closes a
  fully-settled wave early (a killed child never calls back), and silently
  *retires* any wave older than `_WAVE_ABANDON_HOURS` (12) — which is what
  absorbs waves recorded before this feature existed.
- **The timeout is for a child that is gone, not one that is slow.** It only
  fires while no outstanding child has a live CLI process (`_child_is_live`,
  which asks the *runner* — a status field frozen on RUNNING by a crash would
  otherwise disable the timeout for the exact case it exists for), up to
  `ORCH_WAVE_MAX_MIN` (default 6h; `0` = no ceiling). Age-only, it guillotined
  the conductor's 3h bench children at 45 minutes on every wave.
- **A report that lands after its wave closed is still delivered.** A released
  wave used to swallow the straggler's finalize on a `debug` line, so a child
  the partial release had written off finished, wrote a full report, and told
  nobody — which is what pushed the parent onto its self-wake fallback.
  `_deliver_late_child` posts it on its own (full report path + "your earlier
  'missing' conclusion is stale"), auto-resuming only when it was the last one
  outstanding, and records it in `Instance.spawn_late_reported_thread_ids` so a
  retry can't post it twice. No deadline is right for every child; this is what
  makes a wrong release recoverable instead of lossy.
- **Only a child the release could not account for may be reported late.**
  `release_wave` snapshots those into `Instance.spawn_wave_unresolved_thread_ids`
  (same await-free block as the released flag, so the two can't disagree), and
  `_deliver_late_child` requires membership. Without that gate, every later turn
  in a child thread — a user follow-up, a re-finalize — reads as a straggler and
  wakes the parent. Gating on "the release was partial" is the tempting wrong
  answer: a child paused by a usage limit is recorded FAILED and *settled*, so
  its wave closes as complete, and the report from its retry is exactly the one
  that must still arrive. Pre-existing waves carry an empty list and are inert.
- Blocked-child wake-ups are budgeted at `_MAX_BLOCKED_RESUMES` (4) per wave
  (`Instance.spawn_blocked_resumes`) — parent answers, child asks again, repeat.
- `[BOT_CMD: /reply thread=<id>]` + `~~~reply` body lets a parent answer its own
  blocked child. Target must be in this session's own dispatched ids.
- Harness: `python scripts/test_orchestrator_join.py`

## Watches — event-triggered self-wake (`bot/engine/watches.py`)

A self-wake is a timer; a **watch** is the same wake with an *event* as its
trigger. A session that starts a long detached job arms one instead of guessing
a delay, and the thread stays visibly busy until the job actually ends.

- Directive (parsed post-turn, same rules as `/wake` and `/spawn`):
  ```
  [BOT_CMD: /watch pid=959988 log="artifacts/run.log" label="sculpt fit" progress="(\d+)/(\d+) frames" every=120 timeout=6h]
  ~~~watch
  The sculpt fit finished. Read the tail of artifacts/run.log, ...
  ~~~
  ```
  Capture the pid when launching: `setsid nohup ./job.sh > run.log 2>&1 < /dev/null & echo $!`
- Triggers: `pid=` (process gone) or `done=` (regex appears in the log tail).
  At least one is required, plus a non-empty body — otherwise nothing is armed.
  `timeout=` is a safety net, never the plan.
- **Only an explicit directive arms a watch.** Heuristic wake-arming was ripped
  out twice for firing on prose that merely *discussed* a job — don't reintroduce
  it here.
- PID reuse is defended by capturing field 22 of `/proc/<pid>/stat` (start time)
  at arm time; a mismatched token reads as "gone", not "still running". Zombie
  (`Z`) also counts as finished.
- Firing does **not** add a second resume path: the poller calls
  `store.add_wake(..., next_run_at=now)`, so a tripped watch becomes an ordinary
  due wake and inherits the runaway cap, busy re-arm and unattended-turn nudge.
- One thing per thread: `add_watch` supersedes an existing watch, arming a watch
  calls `cancel_wakes`, and arming a `/wake` deletes an armed watch.
- Busy indication while it waits: the `active` forum tag is retained
  (`bot/discord/tags.py`), the 💤 idle prefix is suppressed (`bot/discord/idle.py`),
  and one heartbeat message **edits itself in place** (never re-posts — thread
  name edits are rate-limited, message edits are not) with a progress bar,
  elapsed time, log path, last log line and a "Stop watching" button
  (`watch_stop` in `bot/discord/interactions.py`).
- Persisted in `data/state.json` under `watches` / `watch_counter`, so a watch
  survives a bot restart. Polled by `Scheduler._check_watches` each 30s tick.
- Knobs: `WATCH_*` in `bot/config.py`. Harness:
  `python scripts/test_watch.py`

### A promise to report back is nudged, never auto-armed

`lifecycle.check_wake_request` is where a finished turn is judged, and a turn
that armed nothing has three possible endings:

- **Unattended dead-end** (a cooldown retry or self-wake fire with no
  `[TURN_COMPLETE]`) → `_nudge_or_stop` re-invokes it.
- **A false claim** ("Self-wake queued (~4 min)") with no directive parsed →
  notice only, `claims_self_wake`.
- **A bare promise** ("I'll report back when the tests finish") → since
  2026-08-31, `promises_continuation` + `_promise_nudge` re-invoke the session
  once with `_PROMISE_NUDGE_PROMPT`. Before that it fell through to "ended
  cleanly" and the thread died holding a promise nothing could keep.

It **nudges rather than arms** on purpose. `WAKE_PROMISE_RE` is the name a
deleted predecessor held: it *scheduled* a 3-minute wake off this same prose
and fired phantom re-checks on text that merely discussed a build. Re-invoking
the session keeps "only an explicit directive arms anything" true, and puts
the decision where the pid, the log path and the real duration are known. A
false positive therefore costs one turn that answers `[TURN_COMPLETE]`.

The detector has to survive this repo describing itself. Every guard in
`WAKE_PROMISE_RE` exists because a sentence in these docs, a review report or
a result file tripped it: a bare participle needs "in the background", that
participle needs a first-person subject or a clause start (so "the scheduler
is polling in the background" is prose, not a promise), a subjectless wait is
rejected after "is/are/was/were/to", and the first-person contractions require
their apostrophe — optional, and "id", "ill" and "im" read as "I'd", "I'll"
and "I'm". Any new alternative must be checked the same way, against the
archived result files rather than against invented examples.

The nudge stands down whenever the thread already has something to resume it
(an armed watch, a pending wake — which is how a tripped watch looks —, a
worktree build, a context-exhausted session), shares `MAX_CONSEC_NUDGES` and
one body (`_nudge_once`) with the unattended nudge so the two can't ping-pong
or drift, and loses to the claim notice when both would fire.

"Pending" means armed for *later*. `Scheduler._execute_wake` awaits the
resumed turn and deletes the row in its `finally`, so during a wake-sourced
turn the wake still in the store is the one being consumed —
`_thread_has_pending_wake` discounts it. Counting it would silence the nudge
for the likeliest case there is: a watch trips, the job is still running, and
the resumed turn promises to report back again. `eval._check_unarmed_promise` counts recurrences and
attributes them to `WAKE_GUIDANCE`, so `/evals` names the block that was
supposed to prevent it.
Harness: `python scripts/test_wake_promise_nudge.py`

## Computational Sensors (`.claude/sensors.json`)

Chains run a deterministic sensor step (build → **sensors** → review_code → …)
that executes fast checks in the build worktree and feeds raw tool output back
to the build session for self-fixing (`bot/engine/sensors.py`).

- Auto-detection per stack: `dotnet build` (C#), ruff critical-errors-only
  (Python, only if installed — syntax errors/undefined names, not style),
  `npx tsc --noEmit` (tsconfig present). No stack/tools → step skips silently.
- Per-repo override in the main repo (not the worktree), replaces auto-detect:
  ```json
  {
    "sensors": [
      {"name": "ruff", "command": "ruff check .", "blocking": true, "timeout_s": 180}
    ],
    "policy": "block",
    "max_fix_rounds": 2
  }
  ```
- `policy`: `block` (default — persistent failures halt the chain via
  needs_input, like verify-fail) or `warn` (post failures, advance anyway).
- Sits alongside the other per-repo config files: `.claude/test.json`
  (verify policy + diagnostics) and `.claude/workflow.json` (merge autonomy).

## Multi-Account Setup

The bot supports failover across multiple Claude subscriptions. When the active
account hits its 5h or weekly limit, the runner automatically rotates to the
next account in `CLAUDE_ACCOUNTS`.

**Each account needs its own config directory** — Claude Code stores OAuth
credentials per `CLAUDE_CONFIG_DIR`, so two accounts cannot share `~/.claude`.

### One-time setup on a new machine

1. Pick a directory for the second account, e.g. `~/.claude-work`
2. Authenticate it (interactive — Claude can't do this for you):
   - **Bash/zsh**: `CLAUDE_CONFIG_DIR=~/.claude-work claude`
   - **PowerShell**: `$env:CLAUDE_CONFIG_DIR="$HOME/.claude-work"; claude`
   - **cmd.exe**: `set CLAUDE_CONFIG_DIR=%USERPROFILE%\.claude-work && claude`

   Then inside the CLI: `/login` → pick the second account.
3. Add both paths to `.env`:
   ```
   CLAUDE_ACCOUNTS=/home/you/.claude,/home/you/.claude-work
   ```
4. Restart the bot. Boot log should show:
   `Claude accounts configured: 2 (...)`
   A path that doesn't exist is dropped from rotation (ERROR per entry). A
   path that exists but isn't logged in is *sidelined*, not dropped: the
   runner skips it per-run and it rejoins automatically once signed in — no
   restart, no `.env` edit.

### Verify

- Check `data/logs/bot.log` for the startup line above
- `/auth` — per-account panel (identity, cooldown, re-login)
- `/status` — `**Accounts** — N/M usable`, naming any signed-out account
- Dashboard usage label shows `· N accts`, or `· N/M accts` when one is down
- The Ark gets one notice per outage, plus one all-clear on recovery
  (`bot/discord/account_alerts.py`); "Ignore for 7d" mutes it and lets it
  return once when the week is up
- Harnesses: `python scripts/test_account_failover.py`,
  `python scripts/test_account_alerts.py`

### Notes

- Order matters — first entry is the default. Put your primary account first.
- Sessions are pinned to the account that started them (`session_account` in
  `Instance`), so `--resume` always lands on the right account.
- Invalid entries are pruned at startup — `_pick_account()` only rotates among
  validated dirs, so a typo can't cause silent runtime failover failures.

## Versioning

See `~/.claude/CLAUDE.md` for universal versioning conventions.
Version source: `pyproject.toml`

## Testing

### Discord integration test tool

```bash
python scripts/discord_test.py <command>
```

**Setup (one-time):**
1. Create Ark webhook: `python scripts/discord_test.py setup-webhook <ark_channel_id>`
   → Add URL to `TEST_LOBBY_WEBHOOK_URL` in `.env`
2. Create forum webhook: `python scripts/discord_test.py setup-webhook <forum_channel_id>`
   → Add URL to `TEST_WEBHOOK_URL` in `.env`
3. Add both webhook IDs to `TEST_WEBHOOK_IDS` (comma-separated) in `.env`
4. Restart bot

**Commands:**
- `list-channels` — show all channels in bot category (verify forums exist)
- `list-threads <forum_id>` — show active/archived threads + tags
- `channel-info <id>` — channel type, parent, tags, archive status
- `send <channel_or_thread_id> <msg>` — send via webhook (auto-picks Ark vs forum webhook)
- `read <channel_or_thread_id> [limit]` — read messages with embeds/buttons
- `wait-response <channel_id> [timeout]` — poll for bot response after sending
- `run-suite` — automated test sequence (forum creation, thread resume, archived resume, dedup, tags)

**Quick verification after changes:**
```bash
python scripts/discord_test.py list-channels          # forums exist?
python scripts/discord_test.py list-threads <forum_id> # threads created?
python scripts/discord_test.py read <thread_id> 5      # bot responding?
```

### Read Discord messages (lightweight)

```bash
python scripts/discord_read.py [channel_id] [limit]   # default: The Ark, 10
```

### Manual verification

- `/sync 3` → threads created per project with history
- `/new` → fresh thread in project forum
- `/repo` → select menu dropdown (with 2+ repos)
- Workflow buttons (Plan/Build/Review/Commit) work inside forum threads
- Send message in The Ark → informational reply (no routing)
- Send message in archived thread → auto-unarchives + resumes session

### Log monitoring

Always tail logs when debugging or testing:
```bash
tail -f data/logs/bot.log        # real-time (run in background)
tail -n 50 data/logs/bot.log     # recent entries
```
