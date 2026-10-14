# TODO

## Fable-5 Capitalization — Wave 2 (after wave-1 spawns land)

- [x] **Judgment-distillation skills for aiagent and The-Citadel** — landed on both repos' main branches 2026-07-07. Bot-repo equivalent: `.claude/skills/debugging-playbook/`.
- [x] **Post-Fable model policy decision** — decided 2026-07-07: default new sessions to Opus, spend Fable PPU only on plan/review steps. Encoded as `DEFAULT_SESSION_MODEL` + `MODEL_ROUTING` in `bot/config.py` (values documented in `.env.example`); activation scheduled for July 8 when Fable goes metered.

## Features

- [ ] **Structured actions framework for multi-user** — Instead of giving non-owners raw Claude Code access (Edit/Write/Bash), expose pre-built actions (update spreadsheet, search products, generate report) as tools. Claude orchestrates between them but can't run arbitrary commands. Basically an MCP-style tool layer between the user and the filesystem. Needed when more people get access to different projects.

- [ ] **Positional variable substitution in /alias**: extend alias templates with `$1 $2 $N` placeholders, `$$` for literal `$`. Missing args → error with template echoed back; extra args → appended (preserves current concat behavior, so no migration). Split invocation args via `shlex.split` (quoted args containing spaces). The only expansion site left is inside `/bg` (`on_bg` in `bot/engine/commands.py`); typing `/<alias>` on its own expanded through a Telegram-era handler that had no Discord caller and was deleted in the 2026-10 refactor, so wiring a typed alias back in would need a new route. Add an `_expand_alias(template, extra)` helper there. Deferred: user does not currently use /alias. See inline note at top of the alias handler in `bot/engine/commands.py`.

- [ ] **Gate autopilot/spawns/wakes on real plan-usage %, see `bot/engine/plan_usage.py`.** The dollar gates are off by default (`COST_GATES_ENFORCE`) because subscription costs are notional; the figure that actually runs out is the session/weekly percentage `plan_usage` already reads per account. A brake there (hold unattended chains, spawn waves and self-wakes past, say, 90% weekly) is the real version of what `DAILY_BUDGET_USD` was meant to be.

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
- [ ] **D23 remainder**: the wizard now refreshes through `bot._refresh_dashboard()`, but `_dashboard_pending_flag=[False]` (`bot/discord/bot.py`) is still a mutable-list flag handed to the dashboard module. A small refresher object would replace it.
- [ ] **The `/new` repo picker buttons do not carry the thread's mode**, so a session started from the picker opens in the default mode.
- [ ] **`scripts/claude-bot.service` still names the old `/run/media/quincy/SYSTEM/...` paths.** Unit files were out of scope for the refactor; the installed copy is regenerated by `migrate-off-windows-disk.sh`, so check which one is live before editing.
- Deliberately not merged, recorded so nobody tries again without reading why:
  - The three process-tree killers (`memory.kill_tree`, `usage._kill_process_tree`, `sensors._kill_tree`) have different contracts: bottom-up with a grace period and labels for the memory guard (and it must never wait on the root), an immediate sync kill, and an async process-group kill plus reap.
  - The three current-branch reads (`worktrees._get_default_branch`, `_fix_repo_head`, `forums._get_repo_branch`) differ on a detached HEAD: `symbolic-ref` fails there and `rev-parse --abbrev-ref` returns `HEAD`.
  - `usage._last_good` is not dead: it is the ccusage bar's last-known-good fallback, which stays alongside `plan_usage`.

## Deferred Revisions
<!-- Auto-managed by code review. Remove items when addressed. -->
- [ ] [UX/UI] Mention text should vary by outcome not just path (Low)
- [ ] [Reliability] Forum/thread locks hold Discord API awaits (Medium)
- [ ] [Reliability] Pending voice/refs state not persisted (Low)
- [ ] [UX/UI] Empty change list produces invalid embed field (Low)
- [ ] [UX/UI] Notify control room thread when file-based config is auto-registered (Medium)
- [ ] [UX/UI] Followup message says "Rebooting" even after timeout force-reboot (Low)
- [ ] [Modularity] BOT_CMD scanner belongs in its own module (Low)
- [x] [DRY/Cleanup] Extract git helper methods to a shared location — done, `procutil.run_capture`
- [ ] [Performance] Skip auto-follow for owner-only repos (Low)
- [ ] [UX/UI] Show cache age when serving stale fallback data (Low)
- [x] [DRY/Cleanup] Extract chain resume logic into shared helper — not extractable: the three `advance_chain_phase` calls are 3-4 lines each at different points of one `while` loop, each immediately followed by `break` or `continue`, so the control flow cannot move into a function
- [x] [DRY/Cleanup] Extract instance-cloning helper shared by retry and PPU — done, `StateStore.clone_instance_for_rerun`, shared by `/retry`, the Retry button, the cooldown auto-retry and pay-per-use
- [ ] [Bug Risk] Discard path leaves stale completed tag on archived thread (Medium)
- [ ] [Reliability] apply_thread_tags silently swallows tag-creation failures (Low)
- [x] [DRY/Cleanup] Merged check duplicated across two call sites — already unified: one `merge-base --is-ancestor` remains, and the ahead/behind check is the shared `fleet._commits_ahead`
- [ ] [DRY/Cleanup] Setup steps should be a one-shot script (Low)
- [ ] [Integration] Verification plan assumes Cursor CLI is free to test (Low)
- [ ] [UX/UI] Failed CI should tag thread for visibility (Medium)
- [ ] [Performance] Sequential chain processing blocks independent threads unnecessarily (Medium)
- [ ] [Reliability] Archived thread guard for chain resume (Medium)
- [ ] [Reliability] Mid-file JSONL corruption disables branching (Low)
- [ ] [DRY/Cleanup] Reuse existing age/truncation helpers if present (Low)
- [ ] [Modularity] Centralize image lifecycle instead of scattering (Medium)
- [ ] [Reliability] Phase id matching format unspecified (Medium)
- [ ] [Reliability] Explicit rollback action when verify step fails (Low)
- [ ] [Bug Risk] Recover full backlink metadata, not just thread_name (Medium)
- [ ] [DRY/Cleanup] Use set_status("dismissed") instead of hard delete (Medium)
- [ ] [Modularity] Fold into reusable /verify cleanup admin command (Medium)
- [ ] [Reliability] Guard against suffix landing inside trailing code fence (Low)
- [ ] [Reliability] Defensive inline comment naming prior incidents (Medium)
- [ ] [Bug Risk] Mixed semantics in state.json (Medium)
- [ ] [Reliability] Resume-from-reboot may lose iterative revisions (Medium)
- [ ] [Modularity] Lock release/reacquire dance is fragile; prefer internal no-lock merge variant (Medium)
- [ ] [Bug Risk] Enumerate every release-spawn entrypoint (Medium)
- [ ] [Performance] list_instances scans all sessions before filter (Low)
- [ ] [Reliability] Automated tests for lock and hydration paths (Medium)
- [ ] [UX/UI] Worktree creation latency needs progress feedback (Medium)
- [ ] [UX/UI] Button-aware CTA wording (Medium)
- [ ] [Reliability] Wake + smart-title rename rate-limit collision (Medium)
- [ ] [UX/UI] Drop pipe separator from sleep name (Low)
- [ ] [UX/UI] Notify user when an account looks cancelled (Medium)
- [ ] [Bug Risk] Classifier patterns may match task error content (Low)
