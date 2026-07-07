# TODO

## Fable-5 Capitalization — Wave 2 (after wave-1 spawns land)

- [x] **Judgment-distillation skills for aiagent and The-Citadel** — landed on both repos' main branches 2026-07-07. Bot-repo equivalent: `.claude/skills/debugging-playbook/`.
- [x] **Post-Fable model policy decision** — decided 2026-07-07: default new sessions to Opus, spend Fable PPU only on plan/review steps. Encoded as `DEFAULT_SESSION_MODEL` + `MODEL_ROUTING` in `bot/config.py` (values documented in `.env.example`); activation scheduled for July 8 when Fable goes metered.

## Features

- [ ] **Structured actions framework for multi-user** — Instead of giving non-owners raw Claude Code access (Edit/Write/Bash), expose pre-built actions (update spreadsheet, search products, generate report) as tools. Claude orchestrates between them but can't run arbitrary commands. Basically an MCP-style tool layer between the user and the filesystem. Needed when more people get access to different projects.

- [ ] **Positional variable substitution in /alias** — Extend alias templates with `$1 $2 $N` placeholders, `$$` for literal `$`. Missing args → error with template echoed back; extra args → appended (preserves current concat behavior, so no migration). Split invocation args via `shlex.split` (quoted args containing spaces). Two expansion sites share the same 2-line pattern — `bot/engine/commands.py:285-293` (unknown command) and `bot/engine/commands.py:545-550` (inside `/bg`) — so add one `_expand_alias(template, extra)` helper and call from both. Deferred: user does not currently use /alias. See inline note at top of the alias handler in `bot/engine/commands.py`.

## Tech Debt

- [ ] **Audit `_restore_stash` for unmerged-index leakage after stash-pop conflicts.** The t-4114 orphaned-index recovery (`bot/claude/runner.py:_check_main_repo_clean` Path B) catches the symptom downstream, but the precise pre-existing path that left the main repo in this state is most likely the `_restore_stash` call inside `_merge_branch_sync`'s failure handler — a stash pop with conflicts can leave unmerged stages in the index, and we currently swallow the result instead of either aborting or surfacing the leftover state. Trace the failure path: stash push (line ~2935) → merge attempt → conflict → `git merge --abort` → `_restore_stash` (in both the auto-resolve-fail and `CalledProcessError` paths) — and confirm whether the stash pop is the leak source. If so, gate the pop on a clean index post-abort, or auto-recover via the same `git reset --merge` ladder Path B uses.

- [ ] **Deduplicate mode-handling logic** — 3 near-identical `_handle_control_mode` blocks in `bot/discord/interactions.py` (owner control room, user control room, inline mode select). Extract a shared helper.

- [ ] **Auto-merge: handle untracked-file collisions in main repo.** When the branch wants to add a file that already exists as an *untracked* file in master's working tree, git aborts before creating `MERGE_HEAD` ("error: The following untracked working tree files would be overwritten by merge"). "Resolve with Claude" can't help — there's no conflict state to resolve, so the resolver loops on the same failure. Detect this `failure_kind` specifically and either (a) auto-stash/move the conflicting untracked files aside, attempt the merge, and restore on abort, or (b) surface a dedicated "Move untracked files aside and retry" button instead of the generic resolver path. Symptom seen in thread `1505364903580401795`.

## Deferred Revisions
<!-- Auto-managed by code review. Remove items when addressed. -->
- [ ] [UX/UI] Mention text should vary by outcome not just path (Low)
- [ ] [DRY/Cleanup] Three chain exit mentions are copy-pasted logic (Low)
- [ ] [Modularity] evaluate_instance should accept Instance alone, not Instance + result_text (Medium)
- [ ] [DRY/Cleanup] Deduplicate prior-deferred items before injection (Medium)
- [ ] [Reliability] Forum/thread locks hold Discord API awaits (Medium)
- [ ] [Reliability] Pending voice/refs state not persisted (Low)
- [ ] [DRY/Cleanup] Three identical post-merge deploy blocks (Medium)
- [ ] [UX/UI] Empty change list produces invalid embed field (Low)
- [ ] [DRY/Cleanup] on_usage duplicates format logic already in format_usage_field (Low)
- [ ] [UX/UI] Notify control room thread when file-based config is auto-registered (Medium)
- [ ] [UX/UI] Followup message says "Rebooting" even after timeout force-reboot (Low)
- [ ] [DRY/Cleanup] Deduplicate ctx setup between interactions.py and bot.py (Low)
- [ ] [Modularity] BOT_CMD scanner belongs in its own module (Low)
- [ ] [DRY/Cleanup] _send_temp_lobby_msg and _delete_after duplicate pattern (Low)
- [ ] [DRY/Cleanup] Extract git helper methods to a shared location (Low)
- [ ] [Performance] Skip auto-follow for owner-only repos (Low)
- [ ] [UX/UI] Show cache age when serving stale fallback data (Low)
- [ ] [DRY/Cleanup] Extract chain resume logic into shared helper (Low)
- [ ] [DRY/Cleanup] Extract instance-cloning helper shared by retry and PPU (Low)
- [ ] [DRY/Cleanup] status_callback pattern duplicates messenger.send_text (Low)
- [ ] [Bug Risk] Discard path leaves stale completed tag on archived thread (Medium)
- [ ] [Reliability] apply_thread_tags silently swallows tag-creation failures (Low)
- [ ] [DRY/Cleanup] Merged check duplicated across two call sites (Low)
- [ ] [DRY/Cleanup] Setup steps should be a one-shot script (Low)
- [ ] [Integration] Verification plan assumes Cursor CLI is free to test (Low)
- [ ] [UX/UI] Failed CI should tag thread for visibility (Medium)
- [ ] [DRY/Cleanup] Deferred "break after merge" item resolved by this plan (Low)
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
- [ ] [Modularity] Consider resolving PENDING_IMAGES_DIR locally instead of DATA_DIR globally (Medium)
- [ ] [Reliability] Defensive inline comment naming prior incidents (Medium)
- [ ] [Bug Risk] Mixed semantics in state.json (Medium)
- [ ] [Reliability] Resume-from-reboot may lose iterative revisions (Medium)
- [ ] [Modularity] Lock release/reacquire dance is fragile; prefer internal no-lock merge variant (Medium)
- [ ] [Bug Risk] Enumerate every release-spawn entrypoint (Medium)
- [ ] [Performance] list_instances scans all sessions before filter (Low)
- [ ] [DRY/Cleanup] Centralize merge-failed message rendering (Low)
- [ ] [Reliability] Automated tests for lock and hydration paths (Medium)
- [ ] [UX/UI] Worktree creation latency needs progress feedback (Medium)
- [ ] [Modularity] Migration peeks at `forum_manager._forum_projects` private (Low)
- [ ] [UX/UI] Button-aware CTA wording (Medium)
- [ ] [Reliability] Wake + smart-title rename rate-limit collision (Medium)
- [ ] [UX/UI] Drop pipe separator from sleep name (Low)
- [ ] [DRY/Cleanup] _intentional_kills set will be redundant after Step 2 (Medium)
- [ ] [DRY/Cleanup] Deduplicate the three chain-exit mention sites (Medium)
- [ ] [UX/UI] Notify user when an account looks cancelled (Medium)
- [ ] [Bug Risk] Classifier patterns may match task error content (Low)
