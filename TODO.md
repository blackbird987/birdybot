# TODO

## Fable-5 Capitalization — Wave 2 (after wave-1 spawns land)

- [x] **Judgment-distillation skills for aiagent and The-Citadel** — landed on both repos' main branches 2026-07-07. Bot-repo equivalent: `.claude/skills/debugging-playbook/`.
- [x] **Post-Fable model policy decision** — decided 2026-07-07: default new sessions to Opus, spend Fable PPU only on plan/review steps. Encoded as `DEFAULT_SESSION_MODEL` + `MODEL_ROUTING` in `bot/config.py` (values documented in `.env.example`); activation scheduled for July 8 when Fable goes metered.

## Features

- [ ] **Structured actions framework for multi-user** — Instead of giving non-owners raw Claude Code access (Edit/Write/Bash), expose pre-built actions (update spreadsheet, search products, generate report) as tools. Claude orchestrates between them but can't run arbitrary commands. Basically an MCP-style tool layer between the user and the filesystem. Needed when more people get access to different projects.

- [ ] **Positional variable substitution in /alias**: extend alias templates with `$1 $2 $N` placeholders, `$$` for literal `$`. Missing args → error with template echoed back; extra args → appended (preserves current concat behavior, so no migration). Split invocation args via `shlex.split` (quoted args containing spaces). The only expansion site left is inside `/bg` (`on_bg` in `bot/engine/commands.py`); typing `/<alias>` on its own expanded through a Telegram-era handler that had no Discord caller and was deleted in the 2026-10 refactor, so wiring a typed alias back in would need a new route. Add an `_expand_alias(template, extra)` helper there. Deferred: user does not currently use /alias. See inline note at top of the alias handler in `bot/engine/commands.py`.

- [ ] **Gate autopilot/spawns/wakes on real plan-usage %, see `bot/engine/plan_usage.py`.** The dollar gates are off by default (`COST_GATES_ENFORCE`) because subscription costs are notional; the figure that actually runs out is the session/weekly percentage `plan_usage` already reads per account. A brake there (hold unattended chains, spawn waves and self-wakes past, say, 90% weekly) is the real version of what `DAILY_BUDGET_USD` was meant to be.

- [ ] **Replay A/B: a finer measure before the next arm.** The A/A round and the first real arm ran on 2026-11-20 (finding `2026-11-20-pre-slim-claude-md-against-the-slim-one-on-six`). What that run could not do: shipped-test pass moves in steps of 0.2 to 1.0 per case and sat pinned on two of six, so at k=3 it cannot see an effect under about 0.2. The finer measures are still not built: the pairwise judge (`main.py` docstring) and the `--review` code-quality score (`score.py` docstring). An arm that adds context cannot be replayed on a build whose forked transcript already sits near the window (two of six did not fit about 35K more tokens), so pick cases by their start context. Known gap: a replay starts with no project auto-memory (`sandbox.py` docstring).
- [ ] **Replay sandbox: a sibling copy of the checkout is outside the wall.** `run.hidden_paths` hides the live checkout, but `claude-telegram-bot.stale-20260807` beside it is readable, and five replicates read its `data/state.json` on 2026-11-20. It holds August code and no report for a current case, so nothing leaked, but it also holds an old `.env`. Hide siblings that are copies of the checkout, or remove the copy. The peek tripwire flags it only by accident (a string prefix of the hidden path).

## Machine link, later stages (decisions in `docs/design/peer-link.md`)

- [ ] **Stage 2: questions between machines.** A bot directive, a pending-request record and `store.add_wake` when the answer lands, not a background script with /watch. The answering session reads an export of tracked files through an allowlist, with directive dispatch off on both paths. The doc has the reasons; do not re-derive them.
- [ ] **Stage 3: tasks between machines.** Deferred. One tap would give a remote prompt a full shell on the work laptop.

## Tech Debt

- [ ] **Reboot inherits stale process env — commenting out an `.env` var doesn't unset it.** The reboot launcher (`bot/app.py:598`) spawns `relaunch.py` via `subprocess.Popen` with no `env=`, so the child inherits the parent's `os.environ`. `config.py` uses `load_dotenv(override=True)`, which only overrides keys *present* in `.env` — a commented-out/removed key keeps whatever value the previous run baked into the environment. Symptom: reverting the post-Fable flip by commenting `DEFAULT_SESSION_MODEL=opus` left Opus stuck live until the var was set to an explicit empty value instead. Fix properly so operators can unset by commenting: either start the relaunch from a sanitized env (only pass through what's needed, drop app-owned keys), or have config explicitly reset its known keys to `None` when absent from `.env`. Until fixed, unset `.env` vars with `KEY=` (empty), never by commenting.

- [ ] **Audit `_restore_stash` for unmerged-index leakage after stash-pop conflicts.** The t-4114 orphaned-index recovery (`bot/claude/runner.py:_check_main_repo_clean` Path B) catches the symptom downstream, but the precise pre-existing path that left the main repo in this state is most likely the `_restore_stash` call inside `_merge_branch_sync`'s failure handler — a stash pop with conflicts can leave unmerged stages in the index, and we currently swallow the result instead of either aborting or surfacing the leftover state. Trace the failure path: stash push (line ~2935) → merge attempt → conflict → `git merge --abort` → `_restore_stash` (in both the auto-resolve-fail and `CalledProcessError` paths) — and confirm whether the stash pop is the leak source. If so, gate the pop on a clean index post-abort, or auto-recover via the same `git reset --merge` ladder Path B uses.

- [x] **`subprocess.run` boilerplate** (done): `procutil.run_capture` now carries all 125 capture sites across eleven modules, and `NOWND` moved there with it so `config.NOWND` is a re-export. Transformed by AST rather than by hand, and proved by expanding the helper back to the kwargs it forwards and diffing the call tables before and after. Two sites in the merge-file path stay on `subprocess.run` because they capture bytes, and one in `commands.py` because it passes `check=` without `text=`.

- [x] **Two working verification scripts never run** (done): both registered in `.claude/test.json` under `chain_meta` and `session_picker_filter`, and both gained the `import _bootstrap` line `check_portability.py` requires of any registered harness that imports `bot`.

- [ ] **Guard boilerplate duplicated between `spawn_from` and `spawn_resolver_detached`** (`bot/engine/workflows.py:766` and `:977`): the same drain check, budget check and repo-path check, eleven lines, with only the session guard differing. Small enough that a helper plus its docstring roughly breaks even on lines, so it was left alone; worth folding in if a fourth guard is ever added to one of them.

- [x] **The plan-action button rows are written twice** (done): both blocks now call `_plan_action_rows(iid, has_autopilot_chain)`. Checked against the old code over all 130,560 reachable instance states, zero differences.

- [ ] **`scripts/make_hypotheek_dossier.py` is a personal document generator sitting in `scripts/`**: Dutch, generates a Word file for a mortgage meeting, unrelated to the bot. It is gitignored and untracked, so it was left completely alone rather than deleted. Move it out of the repo when convenient.

- [x] **One dead local** (done): `today` in the usage rollup is gone. An AST sweep of `bot/` for other assigned-never-read locals found none.

- [x] **Deduplicate mode-handling logic** (stale, nothing to do): `_handle_control_mode` does not exist anywhere in the repo. `git log -S` puts its deletion in a646c3a, "Add effort buttons to session embeds, remove mode from control rooms". Two mode blocks remain, both in `bot/engine/commands.py` (`on_mode` and the `mode_explore`/`mode_plan`/`mode_build` callback branch), which is below the three-duplicate bar.

- [ ] **Auto-merge: handle untracked-file collisions in main repo.** When the branch wants to add a file that already exists as an *untracked* file in master's working tree, git aborts before creating `MERGE_HEAD` ("error: The following untracked working tree files would be overwritten by merge"). "Resolve with Claude" can't help — there's no conflict state to resolve, so the resolver loops on the same failure. Detect this `failure_kind` specifically and either (a) auto-stash/move the conflicting untracked files aside, attempt the merge, and restore on abort, or (b) surface a dedicated "Move untracked files aside and retry" button instead of the generic resolver path. Symptom seen in thread `1505364903580401795`.

- [x] **Two test scripts failed at HEAD and were unregistered** (done 2026-10-12): `scripts/test_extract_latest_plan_text.py` and `scripts/test_model_limit_failover.py`. The earlier note here called the model-limit failures genuine; they were not. The live `.env` sets `DEFAULT_SESSION_MODEL`, which made every model read as non-primary, and `load_dotenv(override=True)` means an environment variable cannot undo it. The test now pins the setting itself, `scripts/run_harnesses.py` pins it for every harness, and the plan-text extractor's fake store gained the method it was missing. Both are registered.

- [ ] **Legacy Verify Board and archive-channel migration in `bot/discord/forums.py`** (left in place by the 2026-10 refactor): the Verify Board was removed in 48ce49e (v0.94.0) and the archive-channel migration dates from 4fe1b13 (2026-03-21). This machine's `data/state.json` carries neither `verify_board_thread_id` nor `archive_channel_id` on any of its forum projects, but the other machine's state was not checked, and removing the migration there would strand an old archive channel. Delete `ForumProject.archive_channel_id` / `legacy_verify_thread_id`, `_migrate_archive_channel` and the Verify Board cleanup in `reconcile_forums` once that machine has been started on a current build (or its state.json checked for the two keys).

### Left over from the 2026-10 refactor (scout reports in `docs/refactor/`)

- [ ] **S9, split the prompts out of `bot/config.py`**: deferred because three things key on that path. `prior_art.block_history` follows each block with `git log -L ...:bot/config.py`, which does not follow a move across files; the prompt review tells the reviewer owners live in `bot/config.py`; and rejection fingerprints hash `(file, flag)`, so every stored rejection would stop matching. Needs a fingerprint migration and a `block_history` that knows both paths before the move.
- [ ] **E14 leftovers**: the empty-diff guard still exists twice in the chain loop (`chain_runner.py`, per-phase and after the build), and chain builds still do not stack on an earlier build the way the Build button does (430097e). The plan-injected build prompt was unified.
- [x] **E17**: `_finalize_merge` returns success on the release-orphaned and repo-unusable paths, so the chain records "merged" and logs "complete". Safe (it stops before deploy and close), but the log line misleads. Fixed: it returns `MergeOutcome.STOPPED`, and fleet ship no longer deploys such a repo.
- [x] **E18**: a verify-release result of `None` counts as a failure in the chain but as a pass after an amend. Pick one meaning. Fixed: "not passed" in both (`release_verified`).
- [x] **D23 remainder**: the wizard now refreshes through `bot._refresh_dashboard()`, but `_dashboard_pending_flag=[False]` (`bot/discord/bot.py`) is still a mutable-list flag handed to the dashboard module. A small refresher object would replace it. Done: `DashboardRefresher` (`docs/design/ark-refresh.md`).
- [ ] **The `/new` repo picker buttons do not carry the thread's mode**, so a session started from the picker opens in the default mode.
- [ ] **`scripts/claude-bot.service` still names the old `/run/media/quincy/SYSTEM/...` paths.** Unit files were out of scope for the refactor; the installed copy is regenerated by `migrate-off-windows-disk.sh`, so check which one is live before editing.
- Deliberately not merged, recorded so nobody tries again without reading why:
  - The three process-tree killers (`memory.kill_tree`, `usage._kill_process_tree`, `sensors._kill_tree`) have different contracts: bottom-up with a grace period and labels for the memory guard (and it must never wait on the root), an immediate sync kill, and an async process-group kill plus reap.
  - The three current-branch reads (`worktrees._get_default_branch`, `_fix_repo_head`, `forums._get_repo_branch`) differ on a detached HEAD: `symbolic-ref` fails there and `rev-parse --abbrev-ref` returns `HEAD`.
  - `usage._last_good` is not dead: it is the ccusage bar's last-known-good fallback, which stays alongside `plan_usage`.

## Deferred Revisions
<!-- Auto-managed by code review. Remove items when addressed. -->
- [x] [UX/UI] Mention text should vary by outcome not just path (Low) (stale, 2026-11-20: each chain exit outcome has its own wording now (chain_runner _CHAIN_EXIT_MESSAGES, 60ff297))
- [x] [Reliability] Forum/thread locks hold Discord API awaits (Medium) (stale, 2026-11-20: holding the lock across the Discord call is a documented choice (forums.py, b95f458), auto-follow moved outside it)
- [x] [Reliability] Pending voice/refs state not persisted (Low) (stale, 2026-11-20: pending refs are saved since b95f458, and voice has had no pending state since 1a916f7)
- [x] [UX/UI] Empty change list produces invalid embed field (Low) (stale, 2026-11-20: the field falls back to "Changes detected" when empty (channels.py, since 8bb0ef0))
- [ ] [UX/UI] Notify control room thread when file-based config is auto-registered (Medium) (owner question below)
- [x] [UX/UI] Followup message says "Rebooting" even after timeout force-reboot (Low) (stale, 2026-11-20: the Rebooting followup is gone, 871cbb2 replaced it and a timeout shows its own text (deploy_flow.py))
- [x] [Modularity] BOT_CMD scanner belongs in its own module (Low) (stale, 2026-11-20: it has its own module, bot/engine/directives/dispatch.py (fed8bbb))
- [x] [DRY/Cleanup] Extract git helper methods to a shared location — done, `procutil.run_capture`
- [x] [Performance] Skip auto-follow for owner-only repos (Low) (stale, 2026-11-20: not worth doing: following the owner into every bot thread is the point (90433f5), one call per thread)
- [ ] [UX/UI] Show cache age when serving stale fallback data (Low) (queued in the leftovers build, 2026-11-20)
- [x] [DRY/Cleanup] Extract chain resume logic into shared helper — not extractable: the three `advance_chain_phase` calls are 3-4 lines each at different points of one `while` loop, each immediately followed by `break` or `continue`, so the control flow cannot move into a function
- [x] [DRY/Cleanup] Extract instance-cloning helper shared by retry and PPU — done, `StateStore.clone_instance_for_rerun`, shared by `/retry`, the Retry button, the cooldown auto-retry and pay-per-use
- [ ] [Bug Risk] Discard path leaves stale completed tag on archived thread (Medium) (owner question below)
- [x] [Reliability] apply_thread_tags silently swallows tag-creation failures (Low) (stale, 2026-11-20: not worth doing: the only silent failure is the 20-tag cap and the bot uses 9)
- [x] [DRY/Cleanup] Merged check duplicated across two call sites — already unified: one `merge-base --is-ancestor` remains, and the ahead/behind check is the shared `fleet._commits_ahead`
- [x] [DRY/Cleanup] Setup steps should be a one-shot script (Low) (stale, 2026-11-20: too vague to recover, scripts/setup.py exists and accounts are a one-off .env edit)
- [x] [Integration] Verification plan assumes Cursor CLI is free to test (Low) (stale, 2026-11-20: a note on a plan's test section, that plan shipped (48d0ff0, 7f178db))
- [x] [UX/UI] Failed CI should tag thread for visibility (Medium) (stale, 2026-11-20: no CI watcher was ever built, its sibling item was removed in 6a19a5c)
- [ ] [Performance] Sequential chain processing blocks independent threads unnecessarily (Medium) (queued in the leftovers build, 2026-11-20)
- [ ] [Reliability] Archived thread guard for chain resume (Medium) (owner question below)
- [x] [Reliability] Mid-file JSONL corruption disables branching (Low) (stale, 2026-11-20: not worth doing: session_fork.py stops on a corrupt middle line on purpose (fail closed), never seen in the logs)
- [x] [DRY/Cleanup] Reuse existing age/truncation helpers if present (Low) (stale, 2026-11-20: folded into the shared format_elapsed and parse_iso_utc in 04ad62c)
- [x] [Modularity] Centralize image lifecycle instead of scattering (Medium) (stale, 2026-11-20: inbound image lifecycle moved into bot/discord/attachments.py in 02b4b4d)
- [x] [Reliability] Phase id matching format unspecified (Medium) (stale, 2026-11-20: nothing matches on the phase id now, phase completion is HEAD moving (chain_runner.py))
- [x] [Reliability] Explicit rollback action when verify step fails (Low) (stale, 2026-11-20: the Verify Board and its cleanup were removed in 48ce49e)
- [x] [Bug Risk] Recover full backlink metadata, not just thread_name (Medium) (stale, 2026-11-20: the Verify Board and its cleanup were removed in 48ce49e)
- [x] [DRY/Cleanup] Use set_status("dismissed") instead of hard delete (Medium) (stale, 2026-11-20: the Verify Board and its cleanup were removed in 48ce49e)
- [x] [Modularity] Fold into reusable /verify cleanup admin command (Medium) (stale, 2026-11-20: the Verify Board and its cleanup were removed in 48ce49e)
- [ ] [Reliability] Guard against suffix landing inside trailing code fence (Low) (queued in the leftovers build, 2026-11-20)
- [x] [Reliability] Defensive inline comment naming prior incidents (Medium) (stale, 2026-11-20: both guards name the incident inline (forums.py set_thread_session, query.py))
- [x] [Bug Risk] Mixed semantics in state.json (Medium) (stale, 2026-11-20: the 656a090 cache-token change is the likely meaning, and every record from before it has been pruned)
- [x] [Reliability] Resume-from-reboot may lose iterative revisions (Medium) (stale, 2026-11-20: 4860dc6 added the store lookup for the newest revision (step_parse.py), it fails safe past 24h)
- [x] [Modularity] Lock release/reacquire dance is fragile; prefer internal no-lock merge variant (Medium) (stale, 2026-11-20: no code releases and retakes the lock, merge_branch takes it once and calls the no-lock _merge_branch_sync)
- [ ] [Bug Risk] Enumerate every release-spawn entrypoint (Medium) (queued in the leftovers build, 2026-11-20)
- [x] [Performance] list_instances scans all sessions before filter (Low) (stale, 2026-11-20: an in-memory dict of about 250 records, nothing to gain)
- [x] [Reliability] Automated tests for lock and hydration paths (Medium) (stale, 2026-11-20: covered now (test_worktree_recovery_dedupe, test_failover_session, test_account_failover, test_mutex_drill))
- [x] [UX/UI] Worktree creation latency needs progress feedback (Medium) (stale, 2026-11-20: the thinking card with its heartbeat is up before the worktree is made)
- [ ] [UX/UI] Button-aware CTA wording (Medium) (owner question below)
- [x] [Reliability] Wake + smart-title rename rate-limit collision (Medium) (stale, 2026-11-20: 9d2c6dd made the wake rename fire-and-forget, so it no longer holds a reply)
- [x] [UX/UI] Drop pipe separator from sleep name (Low) (stale, 2026-11-20: f6529b9 removed the pipe (channels.py))
- [x] [UX/UI] Notify user when an account looks cancelled (Medium) (stale, 2026-11-20: built: account_alerts.py alerts on REASON_ORG_DISABLED, covered by test_account_alerts)
- [x] [Bug Risk] Classifier patterns may match task error content (Low) (stale, 2026-11-20: eae7f67 added looks_like_fatal_auth_error with a 400-char cap, every runtime caller uses it)
- [ ] 2026-11-30: run `python scripts/choices_report.py --days 14`; remove the /choices buttons if fewer than a third of answered offers were taps.
- [x] Long replies build their buttons in a second place (Medium) (stale, 2026-11-20: every shape is built inside send_result now (lifecycle.py, callbacks.py))
- [x] There is no Telegram messenger (Low) (stale, 2026-11-20: the Telegram package was removed in 35beac3)
- [x] _message_without_row lives elsewhere and matches one id, not a prefix (Low) (stale, 2026-11-20: the choices build added its own prefix matcher (adapter.py view_without_rows), the old helper is right for its callers)
- [x] Peer card link needs an outbox thread id nobody stores (Medium) (stale, 2026-11-20: fixed in 5724b5c, card_thread_id is stored and read (peer_link.py, waiting.py))
- [x] Control Room redraw signature is built from needs_attention (Medium) (stale, 2026-11-20: 5724b5c removed needs_attention, the signature is built from the Needs you slice)
- [ ] A "not done" line on a passing verify changes nothing in the chain (Medium) (queued in the leftovers build, 2026-11-20)
- [x] The re-labelling bar can't run as written, and the rerun overwrites the baseline (Medium) (stale, 2026-11-20: the open-question record 2026-11-16 now says to move the window date and copy the baseline first)
- [x] The reason has no path through the queue or a resume (Medium) (stale, 2026-11-20: fixed in v0.102.34: the reason rides the queue and the saved chain settings (directives/chain.py, store/chains.py))
- [x] Skipping _check_critical_pass in quiet turns hides the failure Part B targets (Medium) (stale, 2026-11-20: v0.102.34 kept the check in quiet turns and added a quiet_question flag (eval.py))
- [x] A key=value parser already exists (Low) (stale, 2026-11-20: the chain parser uses directive_kv (directives/chain.py))
- [x] Time-zone TODO sites are partly wrong (Low) (stale, 2026-11-20: the TODOs now sit at the two Amsterdam sites (parser.py, lifecycle.py), not the Pacific one)

### Owner questions
- [ ] Owner question: when the bot picks up a new .claude/deploy.json, the Control Room already shows an Approve button. Should it also post a message in that thread (yes/no)?
- [ ] Owner question: after Discard, should the thread get a new "discarded" tag, lose its status tag, or keep "completed" as now?
- [ ] Owner question: if you archive a thread while its chain is running and the bot then reboots, should the chain resume (which unarchives the thread) or be dropped?
- [ ] Owner question: when a build stops without committing, should the halt line name a button ("Reply to continue, or tap Retry") or stay "Reply to continue."?
