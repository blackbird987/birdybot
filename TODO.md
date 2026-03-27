# TODO

## Features

- [ ] **Structured actions framework for multi-user** — Instead of giving non-owners raw Claude Code access (Edit/Write/Bash), expose pre-built actions (update spreadsheet, search products, generate report) as tools. Claude orchestrates between them but can't run arbitrary commands. Basically an MCP-style tool layer between the user and the filesystem. Needed when more people get access to different projects.

## Tech Debt

- [ ] **Deduplicate mode-handling logic** — 3 near-identical `_handle_control_mode` blocks in `bot/discord/interactions.py` (owner control room, user control room, inline mode select). Extract a shared helper.

## Deferred Revisions
<!-- Auto-managed by code review. Remove items when addressed. -->
- [ ] [Reliability] send_text fallback for chain exits also looks empty (Medium)
- [ ] [Bug Risk] Closed/auto-merged threads still ping the user (Medium)
- [ ] [UX/UI] Mention text should vary by outcome not just path (Low)
- [ ] [DRY/Cleanup] Three chain exit mentions are copy-pasted logic (Low)
- [ ] [Bug Risk] Startup merge of stale done instances may re-merge diverged branches (Medium)
- [ ] [Reliability] Break after merge skips chain cleanup if placed wrong (Medium)
- [ ] [Reliability] bash_commands capture must also handle content_block_start path (Medium)
- [ ] [Modularity] evaluate_instance should accept Instance alone, not Instance + result_text (Medium)
- [ ] [UX/UI] Inline flag summary on session result embeds (Medium)
- [ ] [DRY/Cleanup] Deduplicate prior-deferred items before injection (Medium)
- [ ] [Reliability] Triage in build mode could make unintended file changes (Medium)
- [ ] [Bug Risk] Forum projects dict mutated outside lock (Medium)
- [ ] [Reliability] Forum/thread locks hold Discord API awaits (Medium)
- [ ] [Reliability] Pending voice/refs state not persisted (Low)
- [ ] [UX/UI] Set channel topic on archive channel creation (Low)
- [ ] [Bug Risk] Reboot button still renders for non-self-managed repos (Medium)
- [ ] [Reliability] No-tag repos silently get wrong baseline model (Medium)
- [ ] [DRY/Cleanup] Three identical post-merge deploy blocks (Medium)
- [ ] [UX/UI] Empty change list produces invalid embed field (Low)
- [ ] [UX/UI] Use embed for transcription echo instead of plain message (Medium)
- [ ] [DRY/Cleanup] on_usage duplicates format logic already in format_usage_field (Low)
- [ ] [UX/UI] Notify control room thread when file-based config is auto-registered (Medium)
- [ ] [UX/UI] Followup message says "Rebooting" even after timeout force-reboot (Low)
- [ ] [DRY/Cleanup] Deduplicate ctx setup between interactions.py and bot.py (Low)
- [ ] [Modularity] BOT_CMD scanner belongs in its own module (Low)
- [ ] [DRY/Cleanup] _send_temp_lobby_msg and _delete_after duplicate pattern (Low)
- [ ] [DRY/Cleanup] Extract git helper methods to a shared location (Low)
- [ ] [UX/UI] Autopilot build shows no Merge but user can still tap Diff then want to discard (Low)
- [ ] [UX/UI] Drop moot deferred item about archive channel topics (Low)
- [ ] [Performance] Skip auto-follow for owner-only repos (Low)
- [ ] [Reliability] Startup merge could auto-push diverged branches (Medium)
- [ ] [UX/UI] Show cache age when serving stale fallback data (Low)
- [ ] [DRY/Cleanup] Extract chain resume logic into shared helper (Low)
