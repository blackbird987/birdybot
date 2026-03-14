# Changelog

## v0.3.5 — Plan Mode, Mode Toggle, Repo Create/Remove

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

## v0.3.4 — Schedule CLI Tool, Cleanup

### Schedule Management CLI
- New `scripts/schedule.py` — standalone CLI for managing bot schedules externally without the bot running.
- Supports `list`, `add` (with `--every` / `--at`), `delete`, and `update` commands.
- Modifies `data/state.json` directly; the running bot detects changes and reloads.
- Supports `--build` mode flag and `--repo` targeting.

### Housekeeping
- Added `.claude/` to `.gitignore` (Claude Code local config should not be tracked).
- Removed stray junk files from project root.

## v0.3.3 — Apply Revisions Button for Plan Review Flow

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

## v0.3.2 — Chat-App Communication Model, Done Button for In-Place Edits

### Chat-App Communication Model (System Prompt)
- Added `CHAT_APP_CONSTRAINT` — a dedicated system prompt section that explicitly tells Claude the user can only see text responses (not tool calls, diffs, or command output).
- Includes concrete good/bad examples and a "narrate your work" checklist so Claude describes what it read, edited, ran, and any errors.
- Separated from `MOBILE_HINT` (formatting concerns) and `BOT_CONTEXT` (bot capabilities) for clean separation of concerns.
- Injected between MOBILE_HINT and BOT_CONTEXT in `runner.py:_build_system_prompt()`.

### Done Button for In-Place Code Edits
- Added **Done** button to the `made_code_changes` button row (in-place edits without a branch), matching the branch/session button rows.

## v0.3.1 — Session Context, Review Auto-Loop, Done Button

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
