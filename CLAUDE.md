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

## You can read the bot's own instance registry

When you are asked "are the sessions you spawned done?", the bot already knows
and `scripts/instances.py` reads it. Do not answer "I can't tell, run /list
yourself" — that happened on 2026-09-08 while `/list` had the full answer.
`ListAgents` is not the same dataset: it lists peer Claude CLI sessions on the
machine and knows nothing about bot instances.

```bash
python scripts/instances.py children q-16871         # did my spawned children finish?
python scripts/instances.py list --status running    # what is live right now
python scripts/instances.py show t-8206              # branch + worktree of a build
python scripts/instances.py log t-8206 --tail 40     # what it reported
python scripts/instances.py find <thread_id>         # everything that ran in a thread
```

`tree`, `diff` and `--json` are there too. From another repo, call it by its
absolute path — it resolves the *installed* bot's `data/` via
`procutil.install_root`, so it reads the same registry from anywhere, including
from inside a build worktree.

Four things worth knowing before you quote it:

- **It is read-only by omission**, like the mail and telegram readers in The
  Citadel: no kill, no retry, no write path exists in the file. It also does
  not go through `StateStore` (whose `save()` is one typo away) or import
  `bot.config` (which resolves `DATA_DIR` against the *caller's* cwd and drops
  a path marker on init).
- **A spawned child carries no `parent_id`.** Only button/chain steps do. A
  `/spawn` child is joined back to its parent through the forum thread —
  `history.jsonl` first, the live session→thread map only as a fallback,
  because a thread moves on to a newer session and resolving by session alone
  would report a finished child under whatever is running there now.
- **An instance that died before the CLI reported a session id and never
  finalized has neither link**, so it reports as unlinkable rather than being
  guessed at from timing. That is the shape of a bot restart mid-run.
- **The view can lag by up to a minute** (the store flushes on a 60s
  auto-save), and terminal instances are pruned from `state.json` after
  `INSTANCE_RETENTION_DAYS` / `INSTANCE_MAX_RETAINED` along with their result
  files. Every command prints the state file's age; `show` falls back to the
  append-only history for a pruned id.

Harness: `python scripts/test_instances.py`

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
a cache whose miss costs a full rewrite is worse than no cache. A repo that
says nothing about itself caches the *miss*, so it costs stats rather than six
failed opens forever. The signature names every
candidate that exists together with its own mtime *and size*, deliberately not
just the newest mtime: a source restored from a tarball or read over a mount
with a skewed clock carries a *future* mtime, and behind it a newly created
`.claude/repo.json` would never move a maximum, so the manual override would
silently never apply. Size comes free out of the same `stat` and catches the
other half — a file restored with its timestamp preserved but its content
changed. There is no sync twin of the refresh, on purpose: one existed with no
caller but the harness, and a sync/async pair of the same fifteen lines lets a
fix land on one half while the tests keep passing against the other. `_localise_paths` translates the recorded path for the
same reason it translates `repos` — the other machine's spelling never
matches, and every refresh would re-read the files the cache exists to skip.

`.claude/repo.json` is the manual override, written by `/repo desc <text>`
(`/repo desc <name> <text>`, `/repo desc clear`, bare `/repo desc` to show it
and its source). `desc`, `deploy` and `clear` are reserved repo names, so a
repo cannot shadow either the subcommand or the `clear` argument. With no name it targets the repo of the **channel it was
typed in**, before the globally active one: the command is typed inside a
repo's forum, and defaulting to whatever was `/repo switch`ed to last writes
the sentence into the wrong repo. That needs both halves — the engine prefers
`ctx.repo_name`, and `cmd_repo` has to fill it in, because `_run_slash` builds
its ctx with no repo and the engine half alone is inert on the slash path.
`ForumManager.repo_for_channel` resolves it, falling back to the parent forum
so the Control Room post itself resolves like any session thread — and it is
handed the interaction's own channel, not just its id, because a Control Room
auto-archives like any forum post and discord.py drops an archived thread from
its cache, so the id-only lookup misses exactly the case the fallback is for.
It is wired into `/repo` only: setting it in `_run_slash` would also change which repo
`/bg` runs in. A repo whose directory is gone is refused, not created —
`mkdir(parents=True)` on a stale registration would conjure an empty tree that
looks like the real thing. It is written into the *repo*, not into bot state, because
the sentence describes the repo and should travel with a clone — and it sits
next to the per-repo config files that already live there (`test.json`,
`workflow.json`, `sensors.json`, `deploy.json`). Other keys in the file are
preserved on write, and one that will not parse is refused rather than
replaced — it is committed alongside them and may hold keys written by hand.

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

## A repo you are not using is hidden, not removed

Twenty registered repos are twenty forums in one category, and most of them
are not being worked on this month. `/repo hide <name...>` parks a repo;
`/repo unhide <name...>` brings it back. Nothing is unregistered, nothing is
deleted, and `/repo list` still shows a hidden repo under its own `Hidden:`
heading; that listing is how you find one again.

Three things that must not drift:

- **Permission overwrites are not the mechanism, and never were.** Discord
  shows the server owner every channel regardless of overwrites, so denying
  `view_channel` hides nothing from the one person who asked for it. The
  forum is *moved*, into a `<bot category> · Archive` category created on
  demand next to the main one (`channels.ensure_archive_category`,
  `forums._move_repo_forum`). The channel, its threads and its pinned posts
  survive the move untouched. The move must **not** pass
  `sync_permissions=True`: a repo forum can carry its own overwrites, since a
  per-repo access grant is one extra entry on the forum rather than on the
  category, and syncing replaces the forum's list with the destination
  category's -- silently revoking that guest, with an unhide syncing to the
  main category and still not restoring it. Confirmed against the live API:
  a synced move took a forum from 4 overwrites to 3 and dropped the grant
  role; an unsynced one round-tripped all 4. The move alone hides the forum;
  it keeps the private overwrites it was created with either way.
- **`list_repos()` stays unfiltered, deliberately.** It has dozens of callers
  that resolve a repo *path* through it: resume, merge, worktree recovery,
  deploy, session fork. A hidden repo has to keep working for all of them, so
  filtering there would turn "hide" into "quietly break". Hiding is display
  state: only surfaces that draw a list for a human read
  `list_active_repos()` / `is_repo_dormant()`: the dashboard's Projects
  field, the `/repo` switch menu, the `/new` repo picker, and the startup
  reconcile loops that would otherwise redraw control rooms and repair pin
  slots in a forum nobody is looking at. A hidden repo is still switchable
  and still startable *by name*; naming one is intent.
- **Work in a hidden repo un-hides it.** A spawn landing, a self-wake firing
  or a message in one of its threads would otherwise post into a parked
  forum and be seen by nobody. `wake_repo_if_dormant` is the one
  implementation, and it has exactly three callers, one per way something
  can appear in a forum:
  `get_or_create_session_thread` (a new session thread; it sits **above**
  that function's already-has-a-thread early return, because a *resumed*
  session in a hidden repo is precisely the case that would keep posting
  into the parked forum), the forum-message route in `bot.py` (the user
  types in an existing thread), and `_replay_to_thread` (every unattended
  resume: a fired self-wake, a tripped `/watch`, a `--here` schedule, an
  orchestrator wave join, a post-reboot replay -- none of which touch
  `get_or_create_session_thread`, since their thread already exists). A
  plain schedule deliberately wakes nothing: its result is broadcast to the
  owner, not posted into the repo forum, so there is nothing parked to
  miss. The flag is cleared even if the channel move fails, because a repo
  left flagged dormant while its work runs is the failure this exists to
  prevent. This is what makes hiding safe enough to do casually.

`add_repo` and `remove_repo` both discard the dormant flag, so a name
re-registered later cannot come back invisible with nothing on screen to
explain why. `hide` and `unhide` are reserved repo names, and both take
several names at once.

Harness: `python scripts/test_repo_hide.py`

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

## A session that will not compact is let go of, not resumed

The CLI aborts with `Prompt is too long · automatic compaction failed: …`
when the conversation outgrew the context window and it could not summarise
it down. Nothing recognised that until 2026-09-03, when five consecutive runs
(t-7998, t-7999, t-8000, q-16143, q-16158) died against the same session,
`1dbf08aa`, in seconds each. The thread stayed bound to it after every
failure, so the next message resumed it and died the same way. The bug was
not the failing run; it was the **wedged thread** behind it.

It is the exact inverse of the autocompact thrash next door, and answering it
the same way is what makes it permanent:

- A thrash counter lives in the CLI **process** — a resume clears it.
- This lives in the **session**. The oversized transcript is on disk, so every
  resume replays it into the summariser that just failed.

`parser.is_context_overflow_error` is the predicate, length-guarded like
`looks_like_fatal_auth_error` because the callsite falls back to
`result.result_text` and this repo's own sessions write about the failure
constantly. Two rungs in `_run_impl`, placed after both resume-the-same-
conversation branches:

1. **Resume once** (`CONTEXT_OVERFLOW_RESUME_RETRIES`, default 1). Both
   failures seen in the wild came from the *summariser*, not the transcript
   ("summarization produced empty response", and a safety flag on the
   summarisation call), and those are per-call blips. Nearly free — the CLI
   aborts before the turn does any work. No recovery note on this rung: the
   agent is resuming a conversation it never lost, and a preamble on a
   transcript already at the limit spends context to say nothing.
2. **Abandon the session** (`CONTEXT_OVERFLOW_FRESH`, default on) and run
   fresh, primed with the thread's recent history. The two rungs compose
   through recursion, not a loop: the resumed attempt re-enters the branch,
   finds its budget spent, and falls through itself.

Three things that must not drift:

- **The fresh session has to be adopted by the thread.**
  `should_bind_session` binds a `session_recovery_exhausted` result *even when
  it errored*, because that flag is only ever set by a path that first proved
  the old id unusable. Refusing leaves the thread on an id that can never run
  again — which is the whole bug, not a detail of it.
- **`is_account_agnostic_error` must keep the wording.** An overflow abort has
  no output and no completed turns: the account-failover heuristic's exact
  signature for "this account fell over instantly". Without it, a two-account
  setup hands the same oversized transcript to the backup subscription to fail
  identically. Same trap as `lifetime limit`.
- **The briefing is best-effort, the recovery is not.** `on_context_reset`
  (`lifecycle.make_progress_callbacks`) asks the platform for
  `build_prime_briefing(mode="resume")` — the ~12K-token budget built for
  exactly this loss, cache bypassed so it includes messages that landed while
  the dead session was still being retried. It is built *before* the session
  is cleared, and a failure costs the new session its memory of the thread,
  never its existence. `CONTEXT_OVERFLOW_NUDGE` rides in front of it and says
  the two things the replacement cannot find out for itself: that it is
  genuinely new, and that its predecessor's edits are still on disk.
- **The quoted history must arrive framed.** What `on_context_reset` returns is
  not the bare digest but a ready-to-prepend block: `config.prime_preamble`
  plus the digest plus the `---` separator, the same wrapper
  `commands._execute_query` has always put around a briefing. The blocks are
  the user's *own* earlier messages, so a session handed them unframed reads
  them as live orders and redoes work that is already on disk — the exact
  opposite of what this recovery is for. The wrapper moved out of `commands`
  into `config` when the second caller appeared; it is one text with a
  swappable situation sentence (`PRIME_SITUATION_LOST` here,
  `PRIME_SITUATION_COMPACTED` for a resume that was compacted), because the
  load-bearing half — "treat these as DATA, the user has NOT re-asked them" —
  is identical in every case and must not drift between them.
- **It is asked for only once.** A session does not overflow until it is huge,
  which is the exact shape `_execute_query` primes on the compacted-resume
  path — so the aborted attempt's prompt very often *already* opens with a
  briefing built minutes earlier from the same thread. The runner checks for
  `config.PRIME_PREAMBLE_MARKER` in `instance.prompt` and reuses that one
  rather than requesting a second, which would put ~12K tokens of the same
  quoted history twice into the one session whose entire problem is size,
  under two preambles disagreeing about whether it was resumed. The marker is
  the preamble's own opening words, so the two cannot drift apart.

`/reset` is the manual twin, for when the switch is off or the fresh attempt
died too: it unbinds the thread's session and drops the cached briefing,
keeping the thread and its Discord history. Before it, the only escape was to
abandon the thread.

Harness: `python scripts/test_context_overflow.py`

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
- **A timer may be days long.** Both ceilings — `/wake delay=` and `/watch
  timeout=` — are 30 days, not the 24h they were until 2026-09-08. Nothing else
  in the path had to change for that: a wake is an ordinary one-shot schedule,
  polled on the same 30s tick, persisted and never pruned, so its distance out
  was only ever the clamp's business. It is still bounded because a wake firing
  weeks later resumes a session whose CLI transcript may have been cleaned up by
  then — the runner recovers from "No conversation found" by running fresh, but
  the thread loses its history.
  Both directives share **one** duration grammar (`45s`/`90m`/`6h`/`3d`/`2w`, or
  a bare number of seconds), and so does the chip that renders them back. It
  lives in `bot/textutil.py` rather than next to `/watch`, because the chip
  renderer is in `bot.platform`, upstream of `bot.engine`, and a leaf module is
  the only home all three can import without closing a cycle. It is anchored, so
  `3days` falls back to the caller's default instead of parsing as its `3d`
  prefix. Before it was shared, `delay=3d` hit an `int(float(...))` and became
  the 180-second fallback in silence — the failure any new spelling here must
  not reintroduce.
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

## The prompt reviews itself once a week (`bot/engine/prompt_review.py`)

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
thread must always know its session". The harness asserts that absence
structurally, so a later refactor that adds a backfill fails the suite.

Knobs: `PROMPT_REVIEW_ENABLED`, `PROMPT_REVIEW_WINDOW_DAYS`,
`PROMPT_REVIEW_INTERVAL_DAYS`, `PROMPT_REVIEW_MAX_PROPOSALS` in
`bot/config.py`, all inert without `EVAL_ENABLED`.

Harness: `python scripts/test_prompt_review.py`

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

## The machine has two resources, and only one of them was governed

Everything under the memory guard measures memory. On 2026-09-08 the desktop
became unusable while the bot sat well inside every one of those limits, and
the logs from it are almost useless because each line answers the wrong
question — how much memory was free — during an incident where the answer was
always "plenty".

The readings that mattered, on a 12-core box: load average **110**, one Roslyn
`VBCSCompiler` at **605% CPU** with six sessions live, CPU PSI 9-18%, IO PSI
11-24%. Memory PSI was 3%, available memory never dropped below 11 GB, and the
100%-full swap belonged to Steam, Telegram and Plasma — the bot's cgroup held
0.88 GB of it. `read_pressure` returned TIGHT and was **right**; nothing was
wrong with memory. The unit simply had no `CPUWeight` or `IOWeight` at all, so
this cgroup fought Plasma and the browser at the default weight of 100, and a
compile farm always wins a fair fight against a desktop.

Three layers, because there are three distinct contests and one knob cannot
settle them:

- **Bot versus you** — `CPUWeight=20`, `IOWeight=20` in
  `claude-bot.service`. Weight, **not `CPUQuota`**, and that distinction is the
  whole design: weights bind only under contention, so an unattended machine
  still runs builds across all 12 cores at full speed, while a machine you are
  sitting at keeps roughly five sixths of the CPU. A quota would buy the
  desktop's responsiveness with permanently slower builds — the wrong trade for
  a box that is usually unattended. Applied to a *running* unit with
  `systemctl --user set-property --runtime`, which is how it landed without
  restarting six live sessions.

  The durable copy is `scripts/claude-bot.service`, which is the one that
  matters: `~/.config/systemd/user/claude-bot.service` is an install-time copy
  with the paths rewritten, and `migrate-off-windows-disk.sh` regenerates it.
  A resource setting added only to the deployed copy survives until the next
  migration and then silently disappears. Both carry it.
- **Bot versus its own sessions** — `config.SESSION_CPU_NICE` (10), applied by
  `runner._lower_priority` immediately after the spawn. `CPUWeight` settles the
  cgroup's share against the desktop and says nothing about how that share is
  divided *inside* it, which is why the bot stopped answering Discord: a
  ~250 MB asyncio loop that must reply within 3 seconds was competing at equal
  priority with six CLIs and a 605% compiler, and a missed gateway heartbeat
  disconnects. Niceness survives exec and is inherited by children, so one call
  covers the CLI, its shells, dotnet and Roslyn without the runner hunting them
  down.

  **It is deliberately not a `preexec_fn`.** That is the obvious way to do it
  and the wrong one here: CPython runs `preexec_fn` in the child between fork
  and exec, where only async-signal-safe work is legal, and this bot forks from
  a process with well over a hundred threading callsites. A child that lands on
  a lock another thread held at fork time deadlocks before exec and hangs the
  spawn forever while holding the runner's slot. Doing it from the parent costs
  a few microseconds in which the CLI runs at normal priority — harmless, since
  node spends hundreds of milliseconds starting before it forks anything — and
  its worst case is a session that runs fast rather than one that never runs.
  The target is computed from the supervisor's *own* nice, so a unit that later
  grows a `Nice=` cannot silently close the gap.
- **Bot versus the machine** — `MemoryPressure.cpu_psi_pct` / `io_psi_pct`,
  recorded and logged, **deliberately never escalated on.** With the two
  weights in place, CPU saturation costs throughput and no longer costs anyone
  their desktop, so a gate here would hold sessions back to fix something that
  no longer hurts. What was missing was never enforcement; it was that the next
  incident of this shape be diagnosable from `bot.log` alone.

Two things that must not drift:

- **Each PSI signal keeps its own reader.** `psi_some_avg10` (memory),
  `cpu_psi_avg10` and `io_psi_avg10` all wrap `_psi_some_avg10` rather than
  sharing one parameterised entry point. The harness stubs the memory reader;
  a single shared seam let that stub silently decide the CPU answer too, which
  is how a test can describe a machine as memory-starved and CPU-idle in the
  same breath.
- **CPU and IO print only next to a reading the verdict was made from.** They
  are context for a memory verdict, not a verdict of their own. A pressure read
  that could measure nothing must still summarise as having nothing to go on —
  quoting `/proc/pressure/cpu` at it makes a total failure of the memory
  readings look like a healthy machine. `summary()` gates both on `bits` being
  non-empty; `test_memory_guard.py` asserts it.

Harness: `python scripts/test_memory_guard.py`

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

### An account that comes and goes is parked, not removed

An organization admin can switch Claude Code off for a whole account and
switch it back on later. Nothing about that is an `.env` edit: the account
stays in `CLAUDE_ACCOUNTS` the entire time and the existing sideline
machinery carries it (park, one Ark notice, roughly daily retry, rejoin on
the first successful run). It was inert until 2026-09-15 for one reason,
and that reason is the thing to keep true:

- **The wording has to be matched by name.** "Your organization has disabled
  Claude subscription access for Claude Code · Use an Anthropic API key
  instead, or ask your admin to enable access" hit the klerk account 26 times
  from 12:06 on 2026-09-14 and matched none of `is_account_unusable_error`'s
  patterns, so `_pick_account` kept picking a dead subscription. The no-turns
  fallback that exists for unmatched wording structurally cannot cover this
  one: the CLI does not abort before turn 1 the way a 401 does, it reports the
  refusal as a *completed* turn (`num_turns=1`) whose result text **is** the
  error, so both halves of "produced nothing and took no turns" are false and
  nothing is even logged as unmatched.
- **A runtime rejection spelled as a compaction failure belongs to the account
  branch.** Compaction is an ordinary API call, so the refusal surfaces
  through the summariser as "Prompt is too long · automatic compaction failed:
  Your organization has disabled ...". That reads as a context problem, and
  the context-overflow branch sits *above* the account branch, so it spent
  both recovery rungs and then abandoned a transcript that compacts perfectly
  well. q-17514 and q-17515 were amputated that way at 04:13 on 2026-09-15.
  The overflow branch now stands down on `looks_like_fatal_auth_error`.
- **`REASON_ORG_DISABLED` exists to pick the right advice, not to add text.**
  Every other sideline is answered by signing in again; this one cannot be,
  because the account is signed in fine throughout. The Ark notice drops the
  "is signed out" opener, the `CLAUDE_CONFIG_DIR` command and the login
  button, says an admin has to re-enable access, and offers **Try now**
  instead. Three surfaces carry that advice and all three branch on the
  reason: the notice, the `/auth` panel (which reads the table through
  `StateStore.sidelined_account_reasons`, so a button it offers cannot
  contradict the notice that linked to it) and the failure card
  `runner._soften_auth_dead_end` writes when there is nowhere left to fail
  over to. `RUNTIME_REJECTION_REASONS` is what keeps both runtime verdicts
  out of the reach of the on-disk probe, which can never retire either: the
  rejected credentials file parses exactly like a working one.
- **"Try now" clears an auth cooldown and nothing else.**
  `runner.retry_account_now` drops the day-long `ACCOUNT_AUTH_COOLDOWN_SECS`
  sideline so the next task tries the account; if it is still blocked it is
  re-sidelined exactly as before. It refuses a *usage* cooldown, which ends on
  a real clock, and it reads the persisted alert table as well as the
  in-memory auth/usage split, because a reboot loses that split and would
  otherwise make a restarted auth sideline look like a usage limit for the
  rest of the day. Only a `RUNTIME_REJECTION_REASONS` alert counts as that
  durable evidence, and the button is only drawn for one: the probe reasons
  open an alert without ever arming a cooldown, so reading one as an auth
  sideline would force-clear a real usage limit that happened to be sitting
  behind it. The sole-account case arms no cooldown either (parking the only
  account we have would stop everything), so there the in-memory dead mark is
  the whole sideline and dropping it is the whole retry.
- **One panel renderer.** `/auth` and its Refresh button both go through
  `wizard._render_auth_panel`. They were near-copies and had already drifted:
  Refresh left out the sideline table, so refreshing turned a server-rejected
  account back into a green tick.

Harnesses: the org-disable cases live in `scripts/test_account_failover.py`
and `scripts/test_account_alerts.py`; the compaction-wrapped one lives in
`scripts/test_context_overflow.py`, next to the recovery it must not trigger.

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
