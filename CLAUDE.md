# Claude Code Bot

Discord bot for managing Claude Code instances remotely.

## Quick Start

```bash
python -m bot          # start the bot
```

## Key Paths

- **Entry point**: `bot/__main__.py` -> `bot/app.py:run()`, which starts `bot/autonomy.py` (merge and sweep loop) and `bot/auto_update.py`, and calls `bot/boot_recovery.py` for what the previous process left behind
- **Config**: `bot/config.py` (reads `.env`)
- **Log file**: `data/logs/bot.log`
- **State**: `data/state.json`
- **Engine** (platform-agnostic): `bot/engine/commands.py`, `lifecycle.py`, `workflows.py`, `sessions.py`, `eval.py`, `report.py`
- **Commands**: `bot/engine/commands.py` only re-exports. The handlers live in `query.py` (typed messages), `directives/` (`dispatch`, `chain`, `spawn`, `reply`), `instance_cmds.py`, `branch_actions.py`, `resolver.py`, `repo_cmds.py`, `settings_cmds.py`, `admin_cmds.py`, `callbacks.py` (button dispatch) and `cmd_state.py` (uptime, channel locks, budget gate). Patch a name in the module that reads it; only `on_text` and `handle_callback` may be patched on the facade, because the Discord layer calls them through it (`scripts/test_facades.py`)
- **Workflows**: `bot/engine/workflows.py` only re-exports too. Step handlers live in `steps.py` (plan, build, review, verify, commit, done, merge), `release_steps.py`, `chain_runner.py` (autopilot and /chain loops), `step_spawn.py` (`spawn_from`), `step_parse.py` (review, plan and verify parsing), `step_policy.py` (presets, verify and merge policy) and `chain_git.py` (git reads and instance lookups). Only `on_autopilot`, `on_conversational_chain` and `on_tldr` may be patched on the facade
- **Runner** (`bot/claude/runner/`, a package of mixins): `core.py` (ClaudeRunner: spawn, stream, kills), `recovery.py` (the rescue steps a failed run is offered, in `_RECOVERY_STEPS` order, pinned by `scripts/test_run_recovery_order.py`), `accounts.py`, `admission.py`, `prompt_build.py`, `session_files.py`, `worktrees.py`, `git_merge.py`, `git_release.py`, `reaping.py`, `watchdog.py` (the per-run age, memory and stall checks), `common.py`. `__init__.py` only re-exports, so patch a name in the module that reads it, never on the package (`scripts/test_runner_package.py` fails a blind patch)
- **Platform layer**: `bot/platform/base.py` (Messenger protocol), `bot/platform/formatting.py` (re-exports `buttons.py`, `modes.py`, `redact.py`)
- **Discord**: `bot/discord/bot.py` (orchestrator; its usage gate, turn context and startup work are mixins in `usage_gate.py`, `thread_ctx.py`, `startup.py`), `attachments.py`, `slash_commands.py`, `interactions.py` (deploys and the Reboot button in `deploy_flow.py`), `adapter.py`, `channels.py`, `forums.py`, `idle.py`, `tags.py`, `modals.py`, `monitoring.py`, `formatter.py`

## You can read the bot's own instance registry

When you are asked "are the sessions you spawned done?", the bot already knows
and `scripts/instances.py` reads it. Do not answer "I can't tell, run /list
yourself": `/list` had the full answer the one time that happened.
`ListAgents` is a different dataset (peer CLI sessions, not bot instances).

```bash
python scripts/instances.py children q-16871         # did my spawned children finish?
python scripts/instances.py list --status running    # what is live right now
python scripts/instances.py show t-8206              # branch + worktree of a build
python scripts/instances.py log t-8206 --tail 40     # what it reported
python scripts/instances.py find <thread_id>         # everything that ran in a thread
```

`tree`, `diff` and `--json` too. It is read-only and can lag up to a minute;
the caveats are in `docs/design/instance-registry.md`.

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
- `/branches` also lists release tags that are not reachable from `HEAD`
  (`docs/design/release-ancestry.md`)

## Cross-cutting rules that bite from unrelated edits

- **Failure wording is load-bearing.** `parser.is_account_agnostic_error`
  matches, among others, the phrases "lifetime limit" (watchdog reap), "killed by the
  system's out-of-memory protection" (outside OOM kill) and the context
  overflow wording ("Prompt is too long · automatic compaction failed"). Each of
  those failures has no output and no turns, which is the account-failover
  heuristic's exact signature for "this account fell over". Reword one and a
  two-account setup hands the same doomed run to the backup subscription.
  A Kill or Steer is kept off that branch by `killed_intentionally`, checked
  above it in `_run_impl`.
- **Mask before you scan.** Any new directive parser (`[BOT_CMD: ...]`,
  promise or claim detection) must scan `textutil.mask_tilde_bodies(text)` and
  read bodies with `textutil.find_tilde_block` from the original. Scanning the
  raw text arms directives that live inside another session's `~~~` block.
- **Patch a name in the module that reads it**, never on a facade or package
  (see Key Paths; `scripts/test_facades.py`, `scripts/test_runner_package.py`).
- **Never loosen the read-only floor.** Explore-mode reviewers (plan review,
  prompt review, TL;DR) run with Bash closed along with the write tools
  (`step_spawn._enforce_readonly_floor`, or `bash_policy="none"` set
  directly), because Bash is a write backdoor. If a reviewer needs data,
  the bot fetches it for them (`bot/engine/prior_art.py`).

## Design notes: read before editing

Before changing code named in a bullet, Read that doc. They hold the
invariants ("must not drift") that the harnesses do not fully pin down, and
the incident that made each one necessary. They are not loaded automatically.

- **Instance registry** (`docs/design/instance-registry.md`): `scripts/instances.py`, read-only, how spawned children are joined back through the thread, lag and pruning. `scripts/test_instances.py`
- **Copy-paste blocks** (`docs/design/copy-paste-blocks.md`): `WORKING_CONTEXT` formatting rule, `eval._check_copy_block_wrapping`; nothing unwraps fences on the way out. `scripts/test_copy_block_wrapping.py`
- **Control Room blurb** (`docs/design/control-room-description.md`): `bot/engine/repo_desc.py`, `/repo desc`, `.claude/repo.json`; stat-only cache signature, first prose paragraph of CLAUDE.md is this repo's blurb. `scripts/test_repo_desc.py`
- **TL;DR** (`docs/design/tldr.md`): `steps.on_tldr`, `config.TLDR_PROMPT`, `InstanceOrigin.TLDR`; resumes, read-only, no own button row, leaves the source card's buttons, no directives. `scripts/test_tldr.py`
- **Repo hide** (`docs/design/repo-hide.md`): `/repo hide|unhide`, `forums._move_repo_forum`, `wake_repo_if_dormant`, `list_active_repos`; moved not permission-hidden, `list_repos()` stays unfiltered. `scripts/test_repo_hide.py`
- **Release ancestry** (`docs/design/release-ancestry.md`): `bot/claude/runner/git_release.py` (`_release_containment_warning`, `_is_ancestor`, `RELEASE_ORPHANED_MARKER`), `git_merge._discard_branch_sync`, `deploy_flow.execute_deploy`; gate walks back one release, a git read that cannot answer is `None`. `scripts/test_release_ancestry.py`
- **Orphan safety-net** (`docs/design/orphan-safety-net.md`): `bot/claude/runner/watchdog.py`, `reaping._reaped_result_base`, `MAX_PROCESS_*`; kill on silence not age, "lifetime limit" wording, notify before terminate. `scripts/test_lifetime_cap.py`
- **systemd watchdog** (`docs/design/sd-watchdog.md`): `bot/sd_watchdog.py` (`DownGate`), `app._discord_is_down`, unit `WatchdogSec`; inert unless the unit asks, never widen "down" to "not ready". `scripts/test_sd_watchdog.py`
- **Thread session binding** (`docs/design/thread-session-binding.md`): `should_bind_session`, `backfill_thread_session`, every `lifecycle.run_instance` caller, `on_self_wake`; bind before the cooldown return, backfill fills a gap and never rebinds. `scripts/test_cooldown_session_bind.py`
- **Context overflow** (`docs/design/context-overflow.md`): `parser.is_context_overflow_error`, `CONTEXT_OVERFLOW_*` rungs in `_run_impl`, `on_context_reset`, `config.prime_preamble`, `/reset`; fresh session must be adopted, briefing framed and built once. `scripts/test_context_overflow.py`
- **Context thrash bloat** (`docs/design/context-thrash-bloat.md`): `bot/claude/context_bloat.py`, `CONTEXT_THRASH_BLOAT_NOTE`; diagnoses only, cause line after the CLI's text, paths relative to the repo. `scripts/test_context_bloat.py`
- **Kill / Steer** (`docs/design/kill-steer.md`): `instance_cmds.perform_kill`, `core.kill_and_wait(reason=, owns_card=)`, `reaping.is_kill_shape` (143 and negative codes), `killed_intentionally`. `scripts/test_kill_shape.py`
- **Spawn-wave join** (`docs/design/spawn-wave-join.md`): `bot/discord/orchestrator.py`, `spawn_dispatched_thread_ids`, `release_wave`, `_deliver_late_child`, `/reply`; sealed before joinable, timeout only for a dead child. `scripts/test_orchestrator_join.py`
- **Watches, promise nudge, nested directives** (`docs/design/watches.md`): `bot/engine/watches.py`, `/watch`, `/wake`, `lifecycle.check_wake_request`, `WAKE_PROMISE_RE`, `bot/textutil.py` duration grammar and tilde blocks; only an explicit directive arms anything. `scripts/test_watch.py`, `scripts/test_wake_promise_nudge.py`, `scripts/test_nested_directives.py`
- **Weekly prompt review** (`docs/design/prompt-review.md`): `bot/engine/prompt_review.py`, `EDITABLE_CLASSES`, `_NO_DIRECTIVE_ORIGINS`, `_RETIRED_CATEGORIES`; proposes and never applies, frequency is not correctness. `scripts/test_prompt_review.py`
- **Prior art** (`docs/design/prior-art.md`): `bot/engine/prior_art.py`, `PLAN_REVIEW_PROMPT`, `CHAIN_CONTEXT`, `_extract_latest_plan_text`; `PRIOR_ATTEMPTS:` stays above `DEFERRED:`, fails open. `scripts/test_prior_art.py`
- **Computational sensors** (`docs/design/sensors.md`): `bot/engine/sensors.py`, `.claude/sensors.json` format and policy.
- **CPU and IO weights** (`docs/design/cpu-io-weights.md`): `CPUWeight`/`IOWeight` on `app-claudesessions.slice`, `SESSION_CPU_NICE`, `core._lower_priority` (not a `preexec_fn`), `cgroups.check_weights`, CPU/IO PSI readers. `scripts/test_memory_guard.py`
- **oomd and session scopes** (`docs/design/oomd-session-scopes.md`): `bot/claude/cgroups.py`, `memory.read_pressure(own_cgroup=)`, `adopt_session`, `reaping._classify_oom_kill`, admission hold, `SESSION_MEM_*`, slice units; ceilings via `systemd-run -p`, the rungs must add up. `scripts/test_session_cgroups.py`, `scripts/test_oom_resume.py`, `scripts/test_memory_guard.py`
- **app.slice ceiling, supervisor footprint** (`docs/design/app-slice-ceiling.md`): `scripts/app.slice.d/50-memory.conf`, `cgroups.check_app_slice_ceiling`, `memory.supervisor_footprint` (a recorder, never a kill). `scripts/test_memory_guard.py`
- **Plan usage figures** (`docs/design/plan-usage.md`): `bot/engine/plan_usage.py`; never refresh a token, every key optional, per account, failures cached as states. `scripts/test_plan_usage.py`
- **Usage pacing** (`docs/design/usage-pacing.md`): `bot/claude/usage_pacing.py`, `AdmissionMixin._await_usage_headroom`, `_paced_running` in `ClaudeRunner.run`, `lifecycle.cooldown_retry_rank`, the staggered `cooldown_loop` in `app.py`; cap never below one, no reading is no cap, count the slot with no await after the hold. `scripts/test_usage_pacing.py`
- **Cost gates** (`docs/design/cost-gates.md`): `total_cost_usd`, `COST_GATES_ENFORCE`, `cmd_state.within_dollar_budget`. `scripts/test_cost_gates.py`
- **Multi-account** (`docs/design/multi-account.md`): `CLAUDE_ACCOUNTS` setup, `_pick_account`, sidelining, `REASON_ORG_DISABLED`, `retry_account_now`, `wizard._render_auth_panel`. `scripts/test_account_failover.py`, `scripts/test_account_alerts.py`
- **Discord test setup** (`docs/design/discord-testing.md`): webhook setup for `scripts/discord_test.py`, plus the full testing notes.

## Versioning

See `~/.claude/CLAUDE.md` for universal versioning conventions.
Version source: `pyproject.toml`

## Testing

Most design notes above name their harness (`python scripts/test_<name>.py`).

### Discord integration test tool

```bash
python scripts/discord_test.py <command>
```

Needs the test webhooks in `.env`; one-time setup is in
`docs/design/discord-testing.md`.

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

### Log monitoring

Always tail logs when debugging or testing:
```bash
tail -f data/logs/bot.log        # real-time (run in background)
tail -n 50 data/logs/bot.log     # recent entries
```
