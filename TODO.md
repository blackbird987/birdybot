# TODO

## Features

- [ ] **Structured actions framework for multi-user** — Instead of giving non-owners raw Claude Code access (Edit/Write/Bash), expose pre-built actions (update spreadsheet, search products, generate report) as tools. Claude orchestrates between them but can't run arbitrary commands. Basically an MCP-style tool layer between the user and the filesystem. Needed when more people get access to different projects.

## Tech Debt

- [ ] **Deduplicate mode-handling logic** — 3 near-identical `_handle_control_mode` blocks in `bot/discord/interactions.py` (owner control room, user control room, inline mode select). Extract a shared helper.
