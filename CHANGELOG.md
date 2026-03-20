# Changelog

## [Unreleased]

### Session Effort Buttons
- Add effort level buttons (Low/Medium/High/Max) to session welcome embed, updating live alongside mode
- Add Effort field to session embed (Origin | Mode | Effort)
- Remove Explore/Plan/Build mode buttons from control rooms (repo + user) — mode is session-scoped
- Remove Mode field from control room embeds
- Add `EFFORT_DISPLAY`, `VALID_EFFORTS`, `effort_name()` to `formatting.py` (mirrors mode pattern)

## v0.34.4 — Fix Cooldown Auto-Retry (2026-03-20)

- Fix auto-retry on usage limit — restore cooldown polling loop accidentally deleted during daily digest removal merge

## v0.34.3 — Silence Archive Channels (2026-03-20)

- Ignore messages in archive channels — bot no longer responds to text posted in `archive-*` channels

## v0.34.2 — Fix Control Room Reboot Button (2026-03-20)

- Fix control room Reboot button not draining active tasks before requesting reboot (unlike /reboot which waited)
- Prevent reboot request loss: defer queue clear until relaunch spawn succeeds; clear on failure to unblock _draining
- Guard against duplicate reboot button clicks while already draining
- Log event handler exceptions to file via on_error override (were only printed to stderr)

## v0.34.1 — Fix Long Prompt Crash (2026-03-20)

- Fix WinError 206 (command line too long) by piping user prompt via stdin instead of CLI argument
- Add stdin write guard — clean error on pipe failure instead of unhandled traceback
- Kill orphaned claude.exe processes on cancellation/unexpected errors
- Cap session history block at 4K to prevent system prompt bloat

## v0.30.1 — Direct Voice Processing (2026-03-20)

- Voice messages now process immediately (transcribe → run as query) instead of showing Send/Cancel confirmation buttons
- Voice transcription echo is truncated to 1900 chars to stay within Discord limits
## v0.33.0 — Per-Repo Dashboard (2026-03-20)

### Per-Repo Dashboard
- Move instance lists (running, attention, completed) from global lobby dashboard into each repo's control room embed
- Add per-repo daily cost display in control room
- Simplify global lobby dashboard to overview: attention items with repo labels, running count, project links, global cost
- Add `get_repo_daily_cost()` to StateStore for repo-scoped cost tracking
- Add field truncation (1024 char limit) to all control room embed fields
## v0.34.0 — Per-Repo Deploy Configs (2026-03-20)

- Add per-repo deploy configs: connect a reboot/deploy command to any repo's control room button
- Support `.claude/deploy.json` convention — Claude instances can write this to auto-register a deploy sequence (requires user approval)
- `/repo deploy set <name> <command>` for manual deploy config, `/repo deploy` to list configs
- File-sourced configs start unapproved with "Approve" button; manual and self-managed configs are pre-approved
- Extend reboot handler: self-managed repos use internal reboot flow, command-based repos run shell command with output capture
- Guard against file-based config overwriting self-managed or manual configs
- Add deploy convention hint to system prompt so Claude instances know how to connect deploy sequences

## v0.29.1 — Remove Daily Digest (2026-03-20)

- Remove daily digest feature (automated broadcast + `format_digest_md`)
- `/report` now always uses `full_report()` for all time ranges
## v0.32.0 — Estimated Usage Display (2026-03-20)

- Add estimated usage display: 5-hour session and 7-day weekly token windows with progress bars and reset countdowns (inspired by claude-counter)
- New `/usage` slash command: budget bar, windowed token usage, cost per window, top spenders
- Usage bars shown in lobby dashboard and per-repo control room embeds
- Token usage tracked in hourly buckets (persisted in state.json), keyed by completion time
- One-time backfill from existing instances on first boot after upgrade
- Configurable token limits via `USAGE_5H_TOKEN_LIMIT` and `USAGE_7D_TOKEN_LIMIT` env vars

## v0.29.0 — Per-Repo Archive Channel (2026-03-20)

- Add per-repo archive channel: posts session summary + thread link on close for searchable session history
## v0.30.0 — Deploy State Tracking (2026-03-20)

- Add deploy state tracking: detect version drift after merges, show "Reboot/Redeploy Required" in per-repo control room embeds
- Auto-detect versions from pyproject.toml, package.json, Cargo.toml, *.csproj, or git tags
- Bot's own repo resets baseline on reboot (reboot = redeploy); other repos use git-tag-based detection that persists across bot reboots
- Show pending changelog entries and session links in control room embed
- Add Reboot button to control room for self-managed repo (triggers reboot_request.json flow)
- Add `on_deploy_state_changed` callback to Messenger protocol for platform-agnostic refresh
## v0.31.0 — Usage-Limit Auto-Retry (2026-03-20)

- Auto-retry on usage-limit cooldown: when Claude CLI hits subscription cap ("You've hit your limit"), bot parses reset time and schedules automatic retry
- 60-second polling loop in app.py with dedup guard (`retrying` set) prevents duplicate retries
- Cancel Auto-Retry button shown on cooldown-pending failed instances
- Max 3 cooldown retries per instance to prevent retry storms; 4-hour fallback when reset time can't be parsed
- Cooldown state persisted to `state.json` — survives bot restarts

## v0.28.0 — Session Evaluation & Reporting (2026-03-17)

### Session evaluation & reporting
- Add per-instance heuristic eval: narration compliance, tool hygiene (checks actual Bash commands), verbosity, claim grounding, efficiency
- Add chain-level eval for autopilot workflows: tracks steps, cost, revision loops, outcome
- Add `/report` slash command with daily (1d) and weekly (7d) modes
- Enhance daily digest with eval summary (flags, warnings, clean session rate)
- Capture Bash commands from CLI stream for tool hygiene analysis
- Eval data stored in `data/evals/` with same retention as instances
- Eval is on by default, disable with `EVAL_ENABLED=0`
## v0.28.1 — LLM-Triaged Medium/Low Revisions (2026-03-17)

- Add LLM-triaged Medium/Low revision step to review loop — after Critical/High converge, the LLM evaluates deferred items and applies quick wins before build
## v0.28.0 — Concurrency & Reliability Hardening (2026-03-17)

### Concurrency & reliability hardening
- Fix duplicate instance spawning: button callbacks now acquire the per-channel lock (matching text message serialization)
- Add reboot drain guard: new prompts are blocked with a message while a reboot is pending, instead of being started and killed
- Add pre-spawn session-active check: prevents spawning a second instance for a session that already has a running task
- Immediate state.json save on critical status transitions (RUNNING, COMPLETED, FAILED, KILLED) — closes 60s crash window
- Protect startup git cleanup and auto-update with per-repo locks to prevent racing with active worktree operations
- Add state.json backup: last-known-good copy saved to `.bak` before each write

## v0.27.1 — Done Button for Plan States (2026-03-17)

- Add Done button to plan-related completion states so threads with plans can be wrapped up without going back to a previous message

## v0.27.0 — Worktree Reliability Overhaul (2026-03-17)

### Worktree reliability overhaul
- Add "merge" as a formal autopilot chain step — survives bot restarts (persisted in chain state), no longer runs as fragile post-loop code
- Add `needs_input` guard to chain loop — prevents chain from proceeding when Claude asks a question mid-step
- Add startup cleanup: auto-merge completed done instances with branches, fix repos stuck on bot branches, clean orphaned worktrees/branches
- Make merge cleanup robust: `--force` worktree removal with `shutil.rmtree` fallback, `-D` branch delete fallback
- Clear stale branch refs on ALL sibling instances after merge/discard (not just the done instance)
- Manual `/merge` and `/discard` buttons now also clear sibling instance branch refs

## v0.26.0 — Completion Notifications & Sleep Timer Fix (2026-03-17)

### User notifications
- Mention user (@ping) on final result when no autopilot chain is pending — works on embeds and text results
- Mention user when autopilot chain pauses (needs input/failed), build produces no changes, or merge fails
- Users can mute the forum channel and only get pinged when action is needed (@mentions bypass mute)

### Sleep timer fixes
- Fix race condition: remove `_schedule_sleep` from `_generate_smart_title` — background title rename could reset idle timer while a build was running, causing false Zzz
- Guard `schedule_sleep` and `_apply_sleep` against running instances — won't schedule or apply sleep if the channel's session has a running task

## v0.25.0 — Session History & Smart Recall (2026-03-17)

- Add persistent session history log (`data/history.jsonl`) — completed/failed sessions recorded with topic, summary, cost, branch
- Add `/history` slash command — browse recent sessions as clickable thread links, scoped to current repo
- Inject recent session history into system prompt — enables smart recall ("check if the auth fix works") without needing IDs
- Track all interacting users per thread (`user_ids` on ThreadInfo) and mention them on close
- Stop locking archived threads — users can reopen by posting (Discord auto-unarchives)
- Add `bot/store/history.py` module with `append_entry()` and `load_recent()` helpers

## v0.24.0 — Per-Repo Deferred Review Backlog (2026-03-17)

- Add persistent per-repo deferred review storage in `data/deferred/{repo}.md` — Medium/Low items from plan reviews accumulate across sessions instead of being discarded
- Add `/deferred` slash command to view or clear the backlog per repo
- Inject prior deferred items into plan review prompts so they get triaged alongside new findings
- Add `safe_repo_slug()` to sanitize repo names for filesystem safety (prevents path traversal)

## v0.23.3 — Context-First Replies (2026-03-17)

- Add "answer from context" guidance to direct-message workflow — prevents Claude from reflexively using tools for conversational questions

## v0.23.2 — System Prompt Scoping (2026-03-17)

- Add scope-awareness preamble to BOT_CONTEXT — Claude instances now know management bot instructions don't apply to target projects
- Add bootstrap case to reboot instructions — handles first-boot scenario where reboot-watcher isn't loaded yet
- Soften "NEVER kill" to allow process kill as last resort when user explicitly asks
- Add post-action verification rule to honesty constraint — no more claiming success from indirect evidence

## v0.23.1 — Auto-Merge Fix (2026-03-17)

- Use `git merge -X ours` strategy for auto-merge — resolves config/meta file conflicts automatically instead of failing on files outside a hardcoded allowlist
- Remove `_try_auto_resolve_conflicts` method — no longer needed with `-X ours` strategy

## v0.23.0 — Setup Wizard (2026-03-17)

- Add `scripts/setup.py` interactive setup wizard — automates new-device onboarding (token validation, invite URL, guild detection, intent reminders, auto-update config)
- Update `.env.example` with setup hint and Discord-only layout

## v0.22.0 — Strip Telegram (2026-03-17)

- Strip Telegram platform support — gut adapter/bridge/formatter to stubs, remove `python-telegram-bot` dependency, delete `_start_telegram()` orchestration (~860 lines removed)
- Remove dead global-write guards in `RequestContext.update_mode/context/verbose/effort` (only Discord uses per-thread persistence)
- Change default `origin_platform` from `"telegram"` to `"discord"` (backward-compat migration shim kept)
- Remove `find_by_telegram_message()` compat alias from StateStore
- Clean up Telegram references in docstrings, comments, and config across engine, lifecycle, and scripts
- Update CLAUDE.md, .env.example, pyproject.toml description, and MEMORY.md

## v0.21.0 — Multi-Device Auto-Update (2026-03-17)

- Add auto-update feature: secondary devices auto-pull code changes from origin and reboot (opt-in via `AUTO_UPDATE=true`)
- Auto-detect remote default branch (`main`/`master`) with `AUTO_UPDATE_BRANCH` override
- Failure notifications with dedup — broadcasts once per ongoing error, resets on success

## v0.20.0 — Honesty & Mode UX (2026-03-17)

### UX Improvements
- Add honesty/verification clause to system prompt — Claude must disclose when it hasn't verified URLs, prices, or other external data
- Add wrong-mode guidance — Claude now tells users exactly how to switch modes instead of just saying it can't do something
- Suppress duplicate mode-change messages — tapping the same mode button no longer spams the channel
- Always show ceiling explanation when a non-owner's mode request is capped (even on repeat taps)

### Access Control
- Upgrade Mardy (Minecraft 4K Gameplay) to build mode + full bash on MardyShiiiits repo

## v0.19.5 — Auto-Merge Reliability (2026-03-17)

### Bug Fixes
- Fix auto-merge failures: search all non-bot branches when detecting default branch (fixes repos with neither `master` nor `main`)
- Fix empty merge error messages: report stdout when stderr is empty (merge conflicts write to stdout)
- Auto-resolve CHANGELOG.md conflicts using git's union merge driver
- Auto-resolve pyproject.toml version conflicts (keeps master's version)
- Stash dirty working tree before merge checkout, safely pop after (with clean-tree guard)

## v0.19.4 — Worktree Session & Merge Fixes (2026-03-17)

### Bug Fixes
- Fix worktree session resume: `_encode_project_path` now replaces dots to match Claude Code's path encoding — plans were lost on every worktree build
- Fix `_get_default_branch` fallback: check HEAD (filtering `claude-bot/*`) instead of blindly returning `"master"` when neither master nor main exists
- Fix merge safety: re-verify `original_branch` exists before checkout, re-detect if stale
- Add empty-build guard: autopilot halts + auto-discards branch/worktree when build produces no changes

## v0.19.3 — Review Fixes (2026-03-16)

### Bug Fixes
- Fix `code_active` detection for worktree builds — Agent-made changes now checked in worktree path, not main repo
- Remove dead `escaped` variable in session resume display
- DRY: deduplicate `_NOWND` subprocess constant (forums.py now imports from runner.py)

## v0.19.2 — Worktree Review Fixes (2026-03-16)

### Review Fixes
- Fix: `_repo_has_changes` now checks worktree path for builds instead of main repo (code_active detection was broken for worktree builds)
- Fix: recursive retries (dead session, transient error) now preserve sibling_context in system prompt
- Fix: `merge_branch`/`discard_branch` guard against missing repo_path instead of running without lock
- Fix: fire-and-forget control room refresh tasks now catch exceptions instead of leaking them
- Remove unused `escaped` variable in session resume

## v0.19.1 — Worktree Hardening (2026-03-16)

### Worktree Hardening
- Worktree reuse: if parent worktree was cleaned up, child instances recreate instead of crashing
- Copy .claude/ directory into worktrees so Claude CLI finds CLAUDE.md and project settings
- Discard cleanup is now best-effort: each step (worktree remove, branch delete) runs independently
- Orphan scan on startup now covers both branches and worktrees
- Dashboard embed shows orphaned branch/worktree count when > 0
- Fix: .claude/ copytree failure no longer crashes build setup (now best-effort with warning)
- Fix: dashboard orphan scan no longer blocks the asyncio event loop (moved to thread)

## v0.19.0 — Git Worktree Isolation (2026-03-16)

### Git Worktrees
- Build tasks now use git worktrees for file isolation — each build gets its own directory (`{repo}/.worktrees/{id}/`)
- Main repo always stays on master — no more `git checkout` in the shared directory
- Parallel builds on the same repo run in separate worktrees without conflicts
- Per-repo asyncio lock serializes git admin operations (worktree add/remove, merge, branch delete)
- Session files are copied between main repo and worktree project directories so `--resume` works
- Merge/discard operations clean up worktrees and session directories automatically
- `/branches` command now also scans for orphaned worktree directories

## v0.18.1 — Discord Bot Refactoring (2026-03-16)

### Refactoring
- Extract `bot/discord/bot.py` (2418 → 847 lines) into 6 focused modules:
  - `slash_commands.py` (704 lines) — all slash command registration
  - `interactions.py` (574 lines) — button/select/modal dispatch
  - `tags.py` (119 lines) — forum tag management
  - `idle.py` (100 lines) — thread sleep/wake timers
  - `modals.py` (81 lines) — QuickTaskModal
  - `monitoring.py` (57 lines) — monitor service lifecycle
- bot.py now contains only core orchestration: init, auth, lifecycle, message routing

## v0.18.0 — Session Context Awareness (2026-03-16)

- Inject universal working context into every spawned session (user workflow, Discord UI, branch model, design principles) via `config.WORKING_CONTEXT`
- Add per-step behavioral guidance (`config.WORKFLOW_GUIDANCE`) so Claude knows its role in each workflow step (plan, build, review, commit, etc.)
- Document branch lifecycle in CLAUDE.md

## v0.17.0 — Control Room Buttons & Branch Management (2026-03-16)

### Branch Management
- New branches always fork from master/main instead of current HEAD (prevents fork drift from parallel sessions)
- Dirty-worktree guard: auto-stash uncommitted changes before branch switch, pop onto new branch
- Autopilot chains auto-merge branch back to master on successful completion
- Done workflow shows Merge/Discard buttons when branch is pending (defers thread close until resolved)
- Merge/Discard after Done now closes the thread automatically
- `/branches` command to list orphaned `claude-bot/*` branches across all repos
- Orphan branch detection on startup with log warnings

### Control Room Buttons
- Expanded repo control room: New Session, Resume Latest, Mode toggle (Explore/Plan/Build), Quick Task modal, Sync CLI, Stop All (conditional), Refresh
- Expanded user control room: New Session per repo, Mode toggle, Refresh
- Button handlers: mode ceiling enforcement for non-owners, sequential stop_all to avoid rate limits
- QuickTaskModal: discord.py modal that collects a prompt and spawns a session in a new thread

### Cross-Repo Access Fix
- Refuse queries when forum repo name doesn't resolve (was: silently fall back to active repo)
- Show available repos in error message to guide the user

## v0.16.1 — Thread Name Edit Dedup (2026-03-16)

- Fix smart title blocked by global `_name_lock` during Discord 429 rate limits — replaced with per-thread dedup set so thread name edits are independent

## v0.16.0 — Auto-Follow Thread Creators (2026-03-16)

- Auto-follow: users who create a forum thread (via /new, control room button, or lobby message) are automatically added to the thread so they get notifications

## v0.15.2 — Smart Title Fix (2026-03-16)
- Fix smart title not firing on first message in /new and lobby threads (flag-based check replaces name check)
- Reduce title generation timeout from 30s to 15s

## v0.15.1 — Deferred Revision Persistence (2026-03-16)

- Persist deferred revisions across autopilot chain steps and reboots; surface them in the Done embed so they're visible even after thread closure

## v0.15.0 — Reasoning Effort & Fixes (2026-03-16)

### Effort (Reasoning Effort)
- Add `/effort` command (low|medium|high|max) — per-thread in Discord, global in Telegram
- Pass `--effort` flag to Claude CLI on every invocation (never rely on CLI default)
- Add `effort` field to Instance, ThreadInfo, RequestContext, and StateStore with full serialization
- Set effort on all 7 instance creation sites (query, bg, release, retry, callback retry, workflow spawn, scheduler)

### Fixes
- Fix smart title not firing on first message in /new threads (elif→if with flag check)
- Fix lobby-created threads unable to retry title gen on follow-up messages
- Reduce title generation timeout from 30s to 15s
- Persist deferred revisions across autopilot chain steps and reboots; surface them in the Done embed so they're visible even after thread closure

## v0.14.1 — Engine Decoupling (2026-03-16)

### Architecture
- Decouple engine from discord.access: rate limits and bash policy resolved via RequestContext callbacks instead of direct imports
- Rename `_run_query_inner` -> `_execute_query` for self-documentation

## v0.14.0 — Discord Extraction & Graceful Shutdown (2026-03-16)

### Architecture
- Extract `ForumManager` class into `bot/discord/forums.py` (1,015 lines) — owns all forum/thread data, lookups, creation, sync, control rooms, and history population
- Extract dashboard embed generation into `bot/discord/dashboard.py` (222 lines) — pure `build_dashboard_embed()` function + serialized refresh
- Extract title generation into `bot/discord/titles.py` (103 lines) — stateless CLI subprocess for 4-6 word titles
- `bot/discord/bot.py` reduced from 3,229 to 2,116 lines (35% reduction); delegates to ForumManager, dashboard_mod, titles
- ForumManager takes `discord.Client` + `StateStore` (not ClaudeBot back-reference), enabling independent reads

### Fixes
- Fix diff save crash: guard `result.stdout` against None before `.strip()` in `runner.py` (affected every build session)
- Add ⚙️ emoji prefix to Control Room thread names and embed titles; existing threads auto-migrate on refresh
- Graceful shutdown drain: wait up to 30s for active queries before tearing down platforms, then kill remaining processes and give 10s for result delivery
- CancelledError handling in `run_instance`: deliver computed results or mark as failed instead of silently dropping
- Add `kill_all()` to ClaudeRunner for bulk process termination during shutdown

## v0.13.1 — Personal Forum Button Fix (2026-03-16)

- Fix: "New" button in user's personal forum control room now creates threads in the correct forum instead of the repo's main forum
- Refactor: Extract ForumManager and data classes (ForumProject, ThreadInfo) into `bot/discord/forums.py`
- Fix: Defensive `result.stdout` handling in runner.py diff capture

## v0.13.0 — Reboot Concurrency & Task Tracking (2026-03-16)

### Concurrency Improvements
- Reboot coalescing: multiple autopilots requesting reboots now produce a single reboot instead of racing (queue + idle callback pattern)
- Dashboard refresh serialization: replace timestamp debounce with lock + pending flag to prevent API storms when multiple instances finish simultaneously
- Platform state race fix: dashboard no longer holds stale dict reference across awaits, preventing `_save_forum_map()` mutations from being overwritten

### Fixes
- Fix `/reboot` not waiting for autopilot chains: track task-level activity (not just subprocesses) so reboot waits for the entire workflow including gaps between steps
- Fix control room refresh never executing: `_refresh_dashboard()` had an early `return` that prevented control room rename migration ("Control Center" → "Control Room") and button re-attachment from ever running

## v0.12.1 — Concise Thread Titles (2026-03-16)

- Tighten smart title generation: prompt asks for 4-6 words with no filler/articles, hard cap at 6 words and 60 chars in `build_title_name()`

## v0.12.0 — Idle Sleep Indicator (2026-03-16)

### Thread Name Overhaul
- Remove mode-colored emoji prefixes (⚪🔵🟢) from Discord forum post names
- Add idle sleep indicator: posts show `💤 | {topic}` after 5 min idle, cleared instantly when processing starts
- Legacy migration: old emoji prefixes stripped automatically on startup
- Rate-limit safe: budgets 1 name edit for sleep + 1 for wake within Discord's 2-per-10-min window

### Control Room
- Fix control room buttons disappearing on refresh (embed-only edit was stripping the view)
- Extract `build_control_view()` / `build_user_control_view()` helpers so create and refresh share button logic

## v0.11.1 — Control Room Button Fix (2026-03-16)

- Fix control room button deleting the control room post on press (`new_repo` handler was calling `delete_original_response()` which destroys the component message)
- Add immediate control room refresh after `new_repo` button press (recovers if embed deleted externally)
- Migrate existing "Control Center" thread names to "Control Room" on refresh

## v0.11.0 — Post-Reboot Smoke Test (2026-03-16)

- Add `scripts/smoke_test.py` — post-reboot health check (log errors, bot ready, platform status, optional response test via `--respond`)
- Add mandatory pre-reboot preflight (py_compile + import check) and post-reboot verification (smoke_test + feature check) to LLM system prompt

## v0.10.0 — Voice Message Transcription (2026-03-16)

- Add voice message transcription: send a voice memo in Discord, bot transcribes via OpenAI Whisper and shows Send/Cancel confirmation before running as a query
- New `OPENAI_API_KEY` env var (optional — voice messages ignored if not set)
- Fix queued prompt UX: "Queued" notice now auto-deletes when execution starts (no more visual overlap with next query)

## v0.9.3 — Plan Button Fix (2026-03-16)
- Fix plan buttons not showing when a regular query enters plan mode (was showing Commit/Review Code instead of Autopilot/Review Plan/Build It)

## v0.9.2 — Sticky Title Fix (2026-03-16)
- Fix thread title generation getting permanently stuck after exceptions (threads staying "new session" forever)

## v0.9.1 — User Control Rooms & Self-Healing (2026-03-16)

- Rename "Control Center" to "Control Room" throughout (thread names, embeds, logs)
- Add Control Room pinned post to personal user forums — shows user name, granted repos, mode, and "New Session" button per repo
- User forum control rooms auto-provisioned on startup, forum creation, and /grant
- Self-healing: if a control room's embed message is deleted, the next refresh cycle detects it, deletes the orphan thread, and recreates the full control room post
- User control room thread IDs tracked in-memory (`_user_control_thread_ids` set) for O(1) message-routing skip checks

## v0.9.0 — Repo Control Center (2026-03-16)

- Add per-repo Control Center pinned post in each forum — shows repo name, path, branch, mode, and active/recent session counts
- Control Center buttons: "New Session" (creates thread in repo forum) and "Sync CLI" (syncs latest CLI sessions)
- Control Center auto-provisioned on startup for existing forums and on first lobby route for new repos
- Control Center embed auto-refreshes alongside dashboard after each query (active/completed/failed counts, branch)
- Graceful recovery: if control center thread is deleted externally, stale IDs are cleared and it re-creates on next access
- Control center threads excluded from session routing (messages in them are ignored, not treated as queries)

## v0.8.4 — Anti-Hedging Prompt Fix (2026-03-16)

- Add anti-hedging prompt guidance to prevent Claude from second-guessing user confirmations (offer-accept-refuse UX bug)
- Add reboot-specific nudge so Claude acts immediately when user requests a reboot

## v0.8.3 — User Forum Welcome Post (2026-03-16)

- Add welcome post with "New Session" button in user personal forums — created on first provisioning, retries on failure, gated by `welcome_posted` flag in access config

## v0.8.2 — Non-Owner Session Creation (2026-03-15)

- Auto-select repo for user forum threads when user has access to only one repo (no tag needed)
- Allow non-owner users to create sessions via `/new` command and repo picker buttons — threads route to their personal forum with correct repo tags
- Non-owner `/new` redirects use ephemeral followup instead of lobby redirect (which they can't see)

## v0.8.1 — Mode Ceiling Security Fixes (2026-03-15)

### Security Fixes
- Fix mode ceiling bypass via Build/Commit/Done buttons — `spawn_from()` now caps spawned instance mode against `ctx.mode_ceiling`
- Fix mode action buttons (`mode_explore`/`mode_plan`/`mode_build`) writing uncapped mode to Instance
- Fix welcome-embed mode_set button ignoring ceiling for non-owners; restrict global mode fallback to owner-only

## v0.8.0 — Access Control & Ref Bugfix (2026-03-15)

### Bugfixes
- Fix `/ref` context injection causing `error: unknown option` when referenced text starts with dashes — prompt now passed after `--` end-of-options separator

### Per-Repo User Access Control
- New `/access` command group: `grant`, `revoke`, `list`, `set` — owner manages who can use which repos
- Per-user personal forum channels: each granted user gets their own private forum with repo tags
- Mode ceiling enforcement: non-owner sessions capped at their grant's mode (explore/plan/build)
- Bash policy: `allowlist` (default), `full`, or `none` — soft enforcement via system prompt for explore mode
- Directory scoping: non-owner sessions run with `cwd` set to repo directory + system prompt boundaries
- Defense-in-depth: `--disallowed-tools` always enforced for non-owner explore sessions regardless of instance mode
- Rate limiting: configurable daily query limit per user, tracked in `data/access.json`
- User attribution: `user_id`/`user_name` on RequestContext, Instance, and ThreadInfo; logged per query
- Dashboard shows `[username]` on non-owner instances
- Access config stored in dedicated `data/access.json` (not platform_state) with 30s cache TTL

## v0.7.0 — Autopilot, Sibling Awareness & Scaling (2026-03-15)

### Autopilot — One-Click Ship
- New **Autopilot** button: chains Review Plan loop → Build → Review Code loop → Done with zero manual clicks
- Smart plan review loop: auto-applies only Critical/High revisions, loops up to 5 rounds until converged, collects Medium/Low as deferred revisions
- New **Build & Ship** button: chains Build → Review Code → Done (for after plan review)
- Review prompt extended with `review-status` structured block (NEEDS_REVISION: yes/no + DEFERRED items); falls back to regex if block missing
- New `APPLY_HIGH_PRIORITY_PROMPT` — tells Claude to apply only Critical/High priority revisions
- Autopilot pauses on failure or AskUserQuestion — "Continue Autopilot" button resumes from the next step after the user answers
- Autopilot chain state stored per-session in state.json (`autopilot_chains`) for reliable pause/resume

### Cross-Session Sibling Awareness
- Running instances in the same repo are now listed in each other's system prompts ("Other active sessions: ...")
- Helps Claude avoid editing files that sibling sessions are likely working on
- Lightweight (~500 chars), informational only

### Dashboard & List at Scale
- Dashboard: added "Needs Attention" section (failed + questions) at the top
- Dashboard: running instances grouped by repo, no 5-item cap
- Dashboard: added "Recently Completed" section (last 5)
- `/list` now supports filtering: `/list running`, `/list failed`, `/list questions`, `/list <repo>` (combinable)
- `/list` groups by repo by default when multiple repos are registered
- New store query methods: `list_by_repo()`, `list_by_status()`, `needs_attention()`

### Workflow Refactor
- All workflow functions (`on_plan`, `on_build`, `on_review_plan`, etc.) now return `Instance | None` for chaining
- Extracted `_last_msg_id()` helper to DRY up message ID lookups across workflow chains
- Added `deferred_revisions` field to Instance for persisting plan review deferrals

### Processing Status Overhaul
- Remove 🔄 processing emoji from thread names — was unreliable due to Discord's 2-per-10-min thread rename rate limit
- Replace with tag-based active indicator (`_set_thread_active_tag`) using tag-only edits (~5/5s rate limit)
- Simplify `_generate_smart_title` — removed `clear_processing` param and all fallback paths
- Simplify `_update_thread_name` — removed `processing` and `applied_tags` params, now only handles mode changes
- Delete `_set_thread_processing` method entirely
- Startup cleanup now clears stale "active" tags instead of parsing thread names for 🔄

### Dashboard Enhancements
- Dashboard refreshes on query **start** (not just end) — shows running instances immediately
- Running instances now show clickable thread links and elapsed time
- Reduced dashboard debounce from 5s to 2s for faster status updates

## v0.6.2 — Plan Buttons & Tag-Based Active State (2026-03-15)

- Fix: plan mode queries via direct messages now show correct plan buttons (Review Plan / Build It / Done) instead of default buttons
- Replace thread name processing emoji (🔄) with forum tag-based active indicator — avoids Discord's 2-per-10-min thread rename rate limit
- Simplify `_generate_smart_title` and `_update_thread_name` — no longer responsible for processing state

## v0.6.1 — Cleanup & Review Prompt Polish (2026-03-15)

- Extract `_attach_session_callbacks()` helper to DRY up lambda wiring in two on_message paths
- Simplify `_repo_has_changes()` to use single `git status --porcelain` instead of two separate diff commands
- Improve plan review prompt readability: concise paragraph format instead of dense field labels; Critical/High/Medium/Low priorities instead of P1/P2/P3

## v0.6.0 — Plan Mode, Session Races & Button Fixes (2026-03-15)

- Fix missing Review Code button when subagents (Agent tool) make code changes — now checks git diff as fallback
- Fix smart thread titles never applying due to Discord rate-limiting 3rd thread name edit; batch smart title + processing-off into a single edit (2 edits total instead of 3)
- Enforce plan mode via system prompt — Claude can research freely but cannot modify files; must output a structured plan for review instead
- Improve plan review formatting: replace dense Change/Pros/Cons/Impact/Priority fields with concise paragraphs; use Critical/High/Medium/Low priority labels instead of P1/P2/P3
- Fix session continuation race condition: second message in a new forum thread could start a fresh session instead of resuming, due to session_id being written to ThreadInfo too late; now uses double-checked locking with callbacks to resolve session_id inside the per-channel lock

## v0.5.1 — Fire-and-Forget & Cleanup (2026-03-15)

- Make all thread name/tag PATCH operations fire-and-forget — prevents Discord 429 rate limits from blocking message processing entirely
- Remove legacy `channel_sessions` migration code and auto-archive text channel loop (forums stable since v0.3.0)
- Delete dead `archive_session_channel()` function (only caller removed above)
- Consolidate budget check in `workflows.spawn_from()` — uses shared `check_budget()` with consistent "/budget reset" hint
- Fix hardcoded absolute path in `scripts/discord_read.py` — now uses relative Path resolution
- Default new repos to `main` branch (`git init -b main`)

## v0.5.0 — Per-Thread Settings & Processing Indicator (2026-03-15)

### Per-Thread Settings (Discord)
- **Critical bug fix**: mode, context, and verbose_level were global singletons — clicking "Mode: Build" in one thread changed it for ALL threads
- Settings are now per-thread in Discord: each forum thread has its own mode, context, and verbose_level
- New threads inherit the global default; existing threads without settings continue using globals (backward compat)
- Added `mode`, `context`, `verbose_level` fields to `ThreadInfo` (persisted in `platform_state`)
- Added `effective_*` properties and `update_*` methods to `RequestContext` for thread-local resolution with global fallback
- Context uses `""` sentinel for "explicitly cleared" (no extra boolean fields needed)
- Centralized persistence: `_persist_ctx_settings()` called from `_run_slash`, `on_interaction`, and `on_message`
- `/new mode:build` now sets mode on the new thread only, not globally
- `mode_set` welcome button writes to ThreadInfo, not global store
- Thread name emoji, processing indicator, and forum tags all use per-thread mode
- Telegram unaffected — continues using global settings as before

### Processing Indicator
- Thread names show 🔄 while LLM is processing: `🔄 🟢 fix login bug` → `🟢 fix login bug` when idle
- Centralized thread name format via `parse_thread_name()`/`build_thread_name()` helpers (DRY)
- Refactored `_update_thread_mode_emoji` → `_update_thread_name` with optional processing state and batched tag updates
- Added `_set_thread_processing()` — batches "active" tag + mode tag with the name update in one API call
- Revived the "active" forum tag (was dead code — now applied at query start)
- All query flows wrapped in `try/finally` for guaranteed cleanup (forum thread, lobby, button callbacks)
- Added missing post-processing (tags + dashboard refresh) for button callback query actions
- Stale processing indicators cleaned up on startup (handles bot crash mid-query)
- `_QUERY_ACTIONS` frozenset identifies which button actions trigger LLM queries
- `_generate_smart_title` preserves processing state when renaming threads
- Tune expanded result view budget from 4000 to 3900 chars for safer Discord embed limits

## v0.4.1 — Review Prompts & Title Fixes (2026-03-15)

- Improved plan review prompts: structured format with tags, priority levels, and character budget for more actionable reviews
- Fix: smart title generation race condition — claim flag early to prevent duplicate concurrent title tasks
- Fix: process cleanup in title generation — await proc.wait() after kill to prevent zombie processes
- Fix: title regex now correctly strips markdown header prefixes (e.g. "# Title")
- Removed unused _rename_thread_from_prompt method (superseded by smart titles)
- Bump expanded result view budget from 3800 to 4000 chars

## v0.4.0 — Thread References & Smart Titles (2026-03-15)

### Thread References
- Add `/ref` slash command with dynamic autocomplete to reference another forum thread's conversation context
- Autocomplete shows `[repo] topic (age)` with newest-first sorting, excludes current thread
- Posts a purple embed with the referenced conversation excerpt (adaptive truncation)
- Injects referenced context into the next prompt so Claude is actually aware of the cross-thread context
- Pending context expires after 10 minutes; multiple `/ref` calls replace previous selection
- Extract shared `format_age()` helper to `formatting.py` (DRY with `sessions.py`)

### Smart Thread Titles
- Smart thread titles: after the first query in a Discord forum thread, an LLM generates a 3-5 word descriptive title (e.g. "Auth Middleware Rewrite") replacing the raw slugified prompt. Fires on lobby route, /new, forum resume, and /sync. Async fire-and-forget with graceful fallback to slug on failure.

## v0.3.16 — Per-Channel Message Queue (2026-03-15)

- Per-channel message queue: messages sent while a query is running in the same session/thread are now queued and processed in order, with a "Queued" notice shown to the user

## v0.3.15 — Explore/Plan Bash Access (2026-03-15)

- Fix: persist `needs_input` flag on Instance so AskUserQuestion state (❓ icon) survives bot restarts
- Fix: explore and plan modes now allow Bash execution (e.g. read scripts, git commands) — previously both mapped to CLI `plan` permission mode which blocked all non-readonly tools. Now uses `bypassPermissions` with `--disallowed-tools Edit,Write,NotebookEdit` to allow Bash while still preventing file modifications.

## v0.3.14 — AskUserQuestion Detection & Repo Paths (2026-03-15)

### AskUserQuestion Detection
- Detect `AskUserQuestion` tool_use in stream-json output — extract the question text, terminate the process, and display the question as the result instead of hanging until inactivity timeout
- Add `needs_input` flag to `RunResult` — lifecycle shows ❓ icon + "asking a question" status, marks COMPLETED (not FAILED)
- Session ID preserved so the user's reply auto-resumes the conversation via `--resume`
- Guarded process termination with 5s timeout + `proc.kill()` fallback
- New `iter_tool_blocks()` generator centralizes tool_use block extraction from stream events

### UX
- Completion status message now shows the action origin (e.g., "✅ t-097 review-code done (2.3m)" instead of just "done")

### Repo Path Resolution
- Fix `/repo create` default path — add `REPOS_BASE_DIR` env var so new repos land in a consistent base directory instead of as siblings of the active repo (which breaks for deeply nested repos)
- `REPOS_BASE_DIR` validated at startup: warns and falls back to sibling logic if the directory doesn't exist
- Normalize all stored repo paths with `.resolve()` — fixes inconsistent slash styles in state.json
- Show path source in confirmation message ("default: REPOS_BASE_DIR" or "sibling of active repo") for transparency

## v0.3.13 — Lobby Mode Emoji Fix (2026-03-15)

- Fix: lobby-routed messages now update thread mode emoji when mode changes (e.g. `/mode build` sent in lobby).

## v0.3.12 — Mode Enforcement (2026-03-15)

- Enforce mode via CLI: explore/plan now use `--permission-mode plan` (read-only), build uses `bypassPermissions`. Previously mode was display-only.
- Refactored thread mode emoji update into `_update_thread_mode_emoji()` helper; mode emoji now updates on `/mode` slash command, text messages, and button callbacks.
- Forum tags use `MODE_EMOJI` dict as single source of truth instead of hardcoded emoji literals.
- Removed unused `EXPLORE_TOOLS` constant.

## v0.3.11 — Mode Color Indicators (2026-03-15)

- Mode color indicators in Discord: thread names prefixed with colored circle emoji (🟢 Build, 🔵 Plan, ⚪ Explore), welcome embed sidebar color matches mode, forum tags get matching emoji, and mode button clicks update both thread name and embed color in real-time.

## v0.3.10 — Clean UI, No Emojis (2026-03-15)

- Stripped all decorative emojis from mode labels, buttons, forum tags, and notification messages — only status icons (🔄 ✅ ❌ ⏳ 💀) remain.
- Simplified `MODE_DISPLAY` from `dict[str, tuple]` to `dict[str, str]`; `mode_label` is now an alias for `mode_name`.
- Token parser handles both flat and nested `usage` formats from CLI stream-json.

## v0.3.9 — Hidden Windows, Session Metrics (2026-03-15)

- Hide Claude CLI console windows on Windows — all subprocesses now use `CREATE_NO_WINDOW` flag to prevent black terminal windows from flashing on screen.
- Redesigned finalize embed: removed emojis, added compact Stats bar with Duration, Turns, Tokens, and Cost.
- New session metrics: `num_turns`, `input_tokens`, `output_tokens` extracted from CLI stream-json and shown in result embeds.

## v0.3.8 — DRY Mode Constants (2026-03-15)

- Mode display constants (`MODE_DISPLAY`, `VALID_MODES`) now used consistently across Discord bot, channels, and formatting — no more hardcoded mode lists.
- Added `mode_name()` helper for emoji-free mode labels.
- Added `start.bat` for quick bot launch on Windows.

## v0.3.7 — Mode Selection, Finalize Fixes (2026-03-15)

### Mode Selection on New Sessions
- `/new` now accepts an optional `mode` parameter (Explore/Plan/Build) as a Discord choice dropdown, so you can start a session in the right mode without a separate `/mode` command.
- New forum threads show mode-selection buttons in the welcome embed — click to switch mode before sending your first message.
- Active mode is highlighted in the button row and shown in the embed's Mode field.

### Finalize Embed Fixes
- `/release` command now also outputs rich finalize embeds (was missed in v0.3.6).
- Commit field truncated to Discord's 1024-char field limit to prevent API errors on long messages.
- Parser robustness: `CHANGELOG:` header matching is now lenient to minor formatting variations.

## v0.3.6 — Stop Button, Rich Embeds, Release Command (2026-03-15)

### Rich Finalize Embeds
- Commit, Done, and Release results now display as rich Discord embeds with structured sections: commit hash+message, changelog entries as a bulleted list, and version badge for releases.
- Prompts output a parseable `summary` block; bot extracts commit, changelog, and version info for display.
- Release results get a gold-colored embed with a "Released vX.Y.Z" title.

### Stop Button
- Added a "Stop" button on all progress/thinking messages while an instance is running, so you can interrupt from Discord (or Telegram) at any time — no need to wait for a stall or type `/kill`.

### Discord New Button
- "New" button on completed instances now creates a new forum thread (same as `/new` command) instead of just clearing session state.

### Release Command
- New `/release [patch|minor|major|X.Y.Z]` command — cuts a versioned release from the [Unreleased] changelog section.
- Wired up on both Telegram and Discord (slash command with `level` parameter).
- `RELEASE_PROMPT` in config handles the full release workflow: changelog freeze, version file update, commit, and git tag.

### Changelog Workflow
- `COMMIT_PROMPT` and `DONE_PROMPT` now direct changes to `## [Unreleased]` instead of version-numbered headers.
- Added `## [Unreleased]` header to CHANGELOG.md for ongoing work.
- Added versioning section to CLAUDE.md pointing to global conventions.

### Plan Button Fix
- Extracted `PLAN_ORIGINS` constant to `types.py` — shared between `lifecycle.py` and `formatting.py` (was duplicated).
- Fixed button priority: plan-workflow origins (Plan, Review Plan, Apply Revisions) now checked before `made_code_changes`, so "Apply Revisions" (which edits the plan file) correctly shows plan buttons instead of code review buttons.

### Auto-Versioning on Done
- Done button now auto-releases after committing: reads `[Unreleased]`, picks semver level via CLAUDE.md rules, bumps version, tags.
- Extracted `_RELEASE_STEPS` shared constant — DRY between `DONE_PROMPT` and `RELEASE_PROMPT`.
- `/release` remains available as manual override for forcing a specific version level.

### Code Review Fixes
- `RELEASE_PROMPT` now guards against dirty working tree (aborts if uncommitted changes exist).
- Extracted `_NEXT_MODE`, `_WORKFLOW_ORIGINS`, `VALID_MODES` to module level in `formatting.py` (was re-created inside function on every call).
- Mode toggle button now persists: added missing `ctx.store.update_instance(inst)` after mode change.
- Result embeds and digest use `mode_label()` for consistent emoji-prefixed mode display.
- Dashboard now refreshes after mode switch via button (was lost when handler moved from `bot.py` to `handle_callback`).

## v0.3.5 — Plan Mode, Mode Toggle, Repo Create/Remove (2026-03-14)

### Plan Mode
- New **plan** mode alongside explore and build — sessions can now be in explore, plan, or build mode.
- `MODE_DISPLAY` dict in `formatting.py` with emoji labels (🔍 Explore, 📋 Plan, 🔨 Build).
- `mode_label()` helper for consistent display across all surfaces.
- `/mode` command accepts `explore|plan|build` (was `explore|build`).
- Mode shown in progress messages, result embeds, and dashboard.
- Discord embed sidebar color varies by mode (blue=explore, blurple=plan, green=build).
- Forum tags `explore` and `plan` added alongside existing `build` tag.

### Mode Toggle Button
- Completed non-workflow queries show a mode cycle button (explore → plan → build → explore).
- Clicking the button updates the global mode and refreshes the source message buttons.
- Discord button styles: green for build, blue for plan/explore.

### Repo Create & Remove
- `/repo create <name> [path] [--github] [--public]` — creates directory, runs `git init`, registers and switches to new repo.
  - Path defaults to sibling of active repo (no new env var needed).
  - `--github` flag runs `gh repo create` to push to GitHub (private by default, `--public` overrides).
  - Handles existing git repos (just registers), non-empty dirs (safe `git init`), missing `git`/`gh` CLI gracefully.
- `/repo remove <name>` — unregisters a repo from the bot (does not delete files on disk).
  - Wires up the existing `StateStore.remove_repo()` method that was previously unreachable.
- Shared `_validate_repo_name()` added — validates format, rejects reserved words. Applied to both `/repo add` and `/repo create`.

### Plan Workflow Button Refinement
- Plan-origin instances (Plan, Review Plan, Apply Revisions) now get plan-specific buttons even when code changes are detected, since their code changes are to the plan file itself.
- Non-plan-origin instances with detected plans get generic Review Plan / Build It buttons.

## v0.3.4 — Schedule CLI Tool, Cleanup (2026-03-14)

### Schedule Management CLI
- New `scripts/schedule.py` — standalone CLI for managing bot schedules externally without the bot running.
- Supports `list`, `add` (with `--every` / `--at`), `delete`, and `update` commands.
- Modifies `data/state.json` directly; the running bot detects changes and reloads.
- Supports `--build` mode flag and `--repo` targeting.

### Housekeeping
- Added `.claude/` to `.gitignore` (Claude Code local config should not be tracked).
- Removed stray junk files from project root.

## v0.3.3 — Apply Revisions Button for Plan Review Flow (2026-03-14)

### Apply Revisions Workflow
- After a **Review Plan** completes with suggested revisions, buttons now show **Apply Revisions / Build It / Done** instead of the generic **Review Plan / Build It / Done**.
- "Apply Revisions" resumes the session and tells Claude to incorporate the proposed revisions into the plan, producing a coherent updated plan.
- After applying, buttons cycle back to **Review Plan / Build It / Done** — enabling a natural review-revise loop until the user is satisfied, then Build.

### Changes
- `InstanceOrigin.APPLY_REVISIONS` enum value added to `types.py`.
- `APPLY_REVISIONS_PROMPT` added to `config.py`.
- `on_apply_revisions()` workflow function in `workflows.py` (explore mode, resumes session).
- Button routing in `commands.py` for `apply_revisions` action.
- `plan_origins` in `lifecycle.py` includes `APPLY_REVISIONS` so `plan_active` stays true.
- Discord `_STYLE_MAP` in `adapter.py` maps `apply_revisions:` to blue (primary) style.
- `sessions.py` strips the new prompt from topic extraction.

## v0.3.2 — Chat-App Communication Model, Done Button for In-Place Edits (2026-03-14)

### Chat-App Communication Model (System Prompt)
- Added `CHAT_APP_CONSTRAINT` — a dedicated system prompt section that explicitly tells Claude the user can only see text responses (not tool calls, diffs, or command output).
- Includes concrete good/bad examples and a "narrate your work" checklist so Claude describes what it read, edited, ran, and any errors.
- Separated from `MOBILE_HINT` (formatting concerns) and `BOT_CONTEXT` (bot capabilities) for clean separation of concerns.
- Injected between MOBILE_HINT and BOT_CONTEXT in `runner.py:_build_system_prompt()`.

### Done Button for In-Place Code Edits
- Added **Done** button to the `made_code_changes` button row (in-place edits without a branch), matching the branch/session button rows.

## v0.3.1 — Session Context, Review Auto-Loop, Done Button (2026-03-14)

### Session Context Flags
- **`plan_active`**: Propagates through session so plan-related buttons (Review Plan / Build It) persist across follow-up messages, not just the initial plan response.
- **`code_active`**: Propagates through session so Commit / Review Code buttons appear on any response after code was written — including chat-based reviews that confirm "no issues."
- Both flags are detected from tools used (`EnterPlanMode`, `Edit`/`Write`/`NotebookEdit`) or inherited from sibling instances in the same session.

### Review Code Auto-Loop
- Clicking **Review Code** now auto-loops up to 5 rounds until a clean review (no code changes). No more pressing the button 4 times.
- Each round's result stays visible so you can track what was fixed. Previous round's buttons are stripped.

### Done Button
- New **Done** button on branch, code_active, plan_active, and default session views.
- Commits all changes, updates changelog, then closes/archives the Discord thread.
- Only closes on successful commit — failed Done shows Retry/Log buttons.

### Repo Name Removal from Thread Names
- Thread names no longer prefixed with `repo│` — redundant since threads are inside repo-named forum channels.
- Forum post embed title simplified to "Session" (was "Session — repo_name"), Repo field removed.
- Result embeds no longer show "Repo" metadata field.
- Startup migration renames existing active threads to strip the legacy prefix.

### DRY: CODE_CHANGE_TOOLS Constant
- `{"Edit", "Write", "NotebookEdit"}` extracted to `CODE_CHANGE_TOOLS` frozenset in `types.py`, used by lifecycle, formatting, and workflows.

### Button Priority Order
- Refined to: branch > made_code_changes (this instance) > code_active (session) > plan_active (session) > default.
- Code review buttons take priority over plan buttons when both flags are set.

### Other
- `on_build` uses `source.plan_active` for prompt selection (was narrow origin check).
- `build_channel_name()` simplified — passthrough to `sanitize_channel_name()`.
- `create_forum_post()` no longer takes `repo_name` parameter.
- Monitor service, reboot resilience, scheduler improvements, test tooling enhancements.
