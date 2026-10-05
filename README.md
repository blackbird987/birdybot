# Claude Code Bot

Drive [Claude Code](https://docs.anthropic.com/en/docs/claude-code) from your phone. One Discord forum channel per repo, one thread per session — you approve work from a chat message and the bot builds it in an isolated git worktree, reviews it, verifies it by actually running the app, and merges. Dozens of session threads stay open across projects and report back on their own; `MAX_CONCURRENT` bounds how many are actually executing at any moment.

The repo directory and PyPI-style package name still say `claude-telegram-bot` / `claude-bot` — this started as a Telegram bot and the name stuck. It is a Discord bot now.

<!-- TODO: screenshot — a forum thread mid-chain, showing the progress embed and workflow buttons -->

---

## Why it exists

Claude Code is a terminal tool. The interesting work — long builds, reviews, multi-hour benchmarks — doesn't need you at the terminal, it needs you to say yes at the right moments. This puts those moments in a chat app you already have on your phone, and gives every session enough autonomy that "yes" is usually the only thing you have to type.

---

## Architecture

### Forum per repo, thread per session

On startup the bot provisions a private Discord category visible only to you and itself, plus a top-level dashboard channel called **The Ark**. Each registered repo gets a forum channel; each session gets a thread inside it.

- A message in a forum thread resumes that thread's Claude session. An archived thread wakes up and resumes.
- A message in The Ark gets an informational reply — it is a dashboard, not a session router.
- Forum tags (`active`, `completed`, `failed`, `cli`, `build`) are the real-time status board. Thread-name edits are rate-limited by Discord; tags are not.
- Each repo's forum has a pinned **control room** thread. A forum has exactly one pin slot, so the control room owns it and everything else is forbidden from pinning (`ForumManager.reconcile_forum_pins` repairs drift on boot).

Data structures live in `bot/discord/forums.py`, persisted to `data/state.json`.

### Build isolation via git worktrees

Every build task gets its own worktree at `{repo}/.worktrees/{instance-id}/` on its own branch.

- The main checkout never leaves its default branch (`master` or `main`, detected per repo) — no `git checkout` in the shared directory.
- Parallel builds on the same repo don't collide.
- Session files are copied between the main repo and the worktree so `--resume` keeps working.
- A per-repo asyncio lock serializes git admin (worktree add/remove, merge, branch delete).
- When the build lands you get **Merge** / **Discard** buttons. Autopilot merges for you.
- `/branches` finds orphaned branches and leftover worktree directories.

### The workflow chain

Work advances through named steps (`bot/engine/workflows.py`):

```
review_loop → build → sensors → review_code → verify → done → verify_release → release → merge
```

That is the full autopilot chain. A session that has already agreed a plan in chat skips `review_loop` and hands the plan straight to the pipeline with a `/chain` directive rather than building inline. Three presets:

| Preset | Steps | Use |
|---|---|---|
| `ship` | build → sensors → review_code → verify → done → verify_release → release → merge | approved, land it |
| `hold` | same, minus `merge` | land it but let me look first |
| `verify` | build → sensors → review_code → verify | build-and-verify loop, branch stays open |

**`verify` means the app actually runs.** The step brings the app up, drives the changed feature against it and reads the result back — not lint, not typecheck, not a unit-test suite. A repo declares how in `.claude/test.json`: a health command, start/stop commands, where the log lives and which markers count as failure, plus named interaction commands the step can call. A singleton app (this bot is one — one token, one state file) sets `"singleton": true` and the step drives the instance that is already running instead of booting a second one that would fight it for shared state.

### Computational sensors

Between build and review, a deterministic step runs fast machine checks in the build worktree and feeds the **raw tool output** back to the build session so it fixes its own mistakes before a reviewer sees them (`bot/engine/sensors.py`).

Auto-detected per stack: `dotnet build` for C#, ruff (critical errors only — syntax errors and undefined names, not style) for Python, `npx tsc --noEmit` where a tsconfig exists. No stack, no tools, step skips silently. Override per repo in `.claude/sensors.json`, with `policy` either `block` (persistent failures halt the chain) or `warn`.

### Spawn waves

A session can fan work out to child sessions, each with its own brief, via a `/spawn` directive. The bot holds the wave open and joins it back (`bot/discord/orchestrator.py`):

- Child state is *derived* from live instances, never stored — a child parked on a question is `blocked`, not `completed`.
- The wave is sealed when the dispatch loop ends, so a fast-failing first child can't close it while siblings are still spawning.
- A full wave auto-resumes the parent with every child's **full report file path**, not an excerpt.
- The timeout is for a child that is *gone*, not one that is slow: it only fires while no outstanding child has a live CLI process.
- A report that lands after its wave closed is still delivered, once, with a note that the earlier conclusion is stale.
- A parent answers its own blocked child with `/reply` instead of bothering you.

### Watches and self-wake

A turn ends when the session sends its final message; the process exits and nothing resumes it. So a session that kicks off a long detached job arms its own resume (`bot/engine/watches.py`):

- `/wake` — a plain timer.
- `/watch pid=... log=... done=...` — event-triggered. Fires when the process disappears or a regex appears in the log tail. PID reuse is defended by capturing the process start time at arm time.

While a watch is pending the thread stays visibly busy: the `active` tag is retained, the idle marker is suppressed, and a single heartbeat message edits itself in place with a progress bar, elapsed time and the last log line.

Only an explicit directive arms a watch. Heuristic arming was tried twice and removed twice — it fired on prose that merely *discussed* a job.

### The orphan safety-net

A run is never killed for being old. It is killed for being **silent**, and its age only decides when the bot starts asking. `MAX_PROCESS_LIFETIME_SECS` (4h) is when checking begins; `MAX_PROCESS_SILENCE_SECS` (30m of no output at all) is the actual trigger; `MAX_PROCESS_HARD_LIFETIME_SECS` (24h, `0` disables) is an age-only backstop. A reap preserves the work — recovered output, tools used, cost counters, and the session id, so Retry resumes rather than restarting.

### Multi-account failover

List several Claude config directories in `CLAUDE_ACCOUNTS` and the bot rotates when one hits its 5-hour or weekly limit. Each directory needs its own OAuth login — Claude Code stores credentials per `CLAUDE_CONFIG_DIR`, so two accounts cannot share `~/.claude`. Sessions are pinned to the account that started them, so `--resume` always lands in the right place. A directory that exists but isn't logged in is sidelined per-run and rejoins automatically once you sign in — no restart.

---

## Setup

### Prerequisites

- Python 3.11+
- Claude Code CLI installed and authenticated (`claude` runs)
- A Claude Pro or Max subscription — the bot spawns CLI instances
- A Discord bot application and token, invited to a server you own
- `Server Members Intent` enabled on the application (needed for permission overwrites when the bot creates its private category)

### Install

```bash
git clone https://github.com/blackbird987/birdybot.git
cd birdybot
pip install -e .
```

### Configure

The wizard is the easy path — it only needs a bot token, and auto-detects the rest:

```bash
python scripts/setup.py
```

Or by hand:

```bash
cp .env.example .env
```

Required:

| Variable | Description |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot token from the Discord developer portal |
| `DISCORD_GUILD_ID` | The server the bot operates in |
| `DISCORD_USER_ID` | Your Discord user id — the owner |

The first two are enforced at startup; the bot refuses to boot without them. `DISCORD_USER_ID` is not checked at boot but the ownership test fails closed, so without it nobody is the owner and every command is refused.

Everything else has a default. The ones worth knowing:

| Variable | Default | Description |
|---|---|---|
| `DISCORD_CATEGORY_NAME` | — | Name for the auto-provisioned private category |
| `CLAUDE_BINARY` | per provider | Path to the CLI binary (`claude`, or `agent` under the cursor provider) |
| `CLAUDE_ACCOUNTS` | — | Comma-separated config dirs for multi-account failover |
| `MAX_CONCURRENT` | `5` | Max parallel CLI instances |
| `DAILY_BUDGET_USD` | `20.0` | Daily spend limit |
| `MAX_PROCESS_LIFETIME_SECS` | `14400` | Age past which the silence watchdog starts checking |
| `MAX_PROCESS_SILENCE_SECS` | `1800` | Silence that actually triggers a reap |
| `ORCH_AUTO_RESUME` | `true` | Auto-resume a parent when its spawn wave completes |
| `ORCH_WAVE_TIMEOUT_MIN` | `45` | Partial-release a wave whose children vanished (`0` = wait forever) |
| `DATA_DIR` | `data` | State, logs and results |
| `LOG_LEVEL` | `INFO` | Logging level |
| `PC_NAME` | hostname | Label shown in notifications on multi-machine setups |

See `.env.example` for the full annotated set, including model routing, log triage and monitor channels.

### Run

```bash
python -m bot
# or
claude-bot
```

Logs go to `data/logs/bot.log`.

---

## Commands

Slash commands are guild-synced, so they register instantly.

**Sessions and instances**

```
/new                 start a fresh conversation
/bg <prompt>         background task in build mode
/list                show instances
/kill <id>           terminate a running instance
/retry <id>          re-run an instance
/log <id>            full output
/export <id>         session as an HTML transcript
/history             recent completed sessions
/session             list or resume desktop CLI sessions
/sync                pull sessions in from the CLI
/sync-channel        refresh this thread's session history
/ref <thread>        reference another thread's context
/clear               archive old instances
```

**Building and shipping**

```
/done                wrap up — commit, changelog, release
/diff <id>           git diff from a build task
/merge <id>          merge a build branch
/discard <id>        delete a build branch
/branches            list orphaned branches and worktrees
/release             cut a versioned release
/fleet               ship all sessions: merge committed work, deploy, verify back
/deferred            view or clear deferred review items
```

**Scheduling**

```
/schedule every <N>m|h|d <prompt>      recurring task
/schedule at <HH:MM> <prompt>          one-shot at a UTC time
/schedule at +<duration> <prompt>      one-shot after a delay
/schedule list | delete <id>
```

**Per-thread settings** (nothing here is global)

```
/mode explore|plan|build      permission mode
/model <name>                 model for this thread
/effort low|medium|high|max   reasoning effort
/verbose 0|1|2                progress detail
/provider claude|cursor       CLI provider
/context set <text>           context pinned to every prompt
/alias set|list|delete        command shortcuts
```

**Repos and access**

```
/repo add|create|remove|switch|list|deploy
/access grant|revoke|list|set
/diagnostics             toggle diagnostic scaffolding for this repo
/monitor setup|refresh|remove|list
```

**Operations**

```
/status      health dashboard
/auth        Claude account panel — who's signed in, who's sidelined
/cost        spending breakdown
/usage       token usage and rate-limit estimates
/budget      budget info and reset
/evals       recurring session-quality flags and what owns each
/report      session quality report
/logs        bot log
/help
/reboot      restart to apply code changes
/shutdown
```

### Directives a session can emit

These are not slash commands — a running session writes them into its own response and the bot acts on them: `/chain` (hand a plan to the build pipeline), `/spawn` (fan out to child sessions), `/reply` (answer your own blocked child), `/watch` and `/wake` (arm a resume), `/image` (put a picture in the chat).

---

## Per-repo configuration

Four optional files in a repo's `.claude/` directory:

| File | Controls |
|---|---|
| `test.json` | How the verify step brings the app up, checks its health, drives it and reads its log |
| `sensors.json` | Which deterministic checks run after build, and whether failures block or warn |
| `workflow.json` | Merge autonomy — `hold`, `merge` or `ship` |
| `deploy.json` | A deploy command, which adds a Deploy button to the repo's control room |

---

## Testing

`scripts/` holds 40-odd standalone harnesses, one per hard-won behaviour — `test_forum_pins.py`, `test_lifetime_cap.py`, `test_orchestrator_join.py`, `test_kill_shape.py`, `test_cooldown_session_bind.py` and so on. Each runs on its own:

```bash
python scripts/test_orchestrator_join.py
python scripts/smoke_test.py          # is the bot healthy right now
```

Live Discord integration:

```bash
python scripts/discord_test.py list-channels
python scripts/discord_test.py list-threads <forum_id>
python scripts/discord_test.py read <thread_id> 5
```

---

## CLAUDE.md is the interesting file

If you are here to build your own version rather than run this one, read [`CLAUDE.md`](CLAUDE.md) before the source.

It is the instruction file the agent itself loads, and almost every rule in it is written as *the incident that produced it* — the watchdog that reaped a live four-hour benchmark for being old rather than silent, the three overnight child sessions lost because a rate-limited turn never recorded its session id, the forum pin race that left five of fourteen channels pinned to the wrong post, the kill that read as a crash because the CLI exits 143 instead of -15.

Not "do X" but "X, because on this date Y broke this way." That framing is most of why the agent follows the rules instead of drifting from them, and it is the part that transfers to any project.

---

## License

MIT — see [LICENSE](LICENSE).
