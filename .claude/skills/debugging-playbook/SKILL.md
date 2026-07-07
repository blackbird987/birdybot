---
name: debugging-playbook
description: Operating judgment for THIS repo (the Claude Code management bot) — read before debugging, rebooting, merging, or touching state. Covers the reboot flow, mandatory preflight/verification, worktree merge traps, locks, state.json shape, and Discord hard limits.
---

# Debugging Playbook — claude-telegram-bot

Distilled judgment for operating this repo safely. Written so a smaller model
can follow it mechanically. When in doubt, do the conservative thing and say
what you're unsure about.

## 1. You ARE the bot process — never kill it directly

Every session here runs as a subprocess of the bot you are modifying. Running
`taskkill`, `kill`, `Stop-Process`, or `stop_bot.bat` kills YOU mid-response:
the user sees "interrupted by bot restart" and your work is lost.

The one correct way to restart the bot is the reboot-request file:

```
Write data/reboot_request.json:
{"message": "why you're rebooting", "resume_prompt": "notes-to-self: what you changed, what to verify, what to do next"}
```

The bot picks it up after your response completes, waits for active queries to
drain, restarts, and sends `resume_prompt` back into your thread so you resume
with context. Write the `resume_prompt` like notes to your future self — it is
all the context the resumed turn gets.

Exception: only kill the process if the reboot file demonstrably isn't working
AND the user explicitly asks — and warn them it interrupts active queries.

## 2. Mandatory preflight BEFORE writing reboot_request.json

A reboot that loads broken code takes the whole bot down for every session.
Do NOT write the reboot file until ALL of these pass:

```bash
python -m py_compile bot/<changed_file>.py     # every file you touched
python -c "from bot.<module> import <MainSymbol>"   # every changed module
```

If either fails, fix it first. No exceptions, even for "one-line" changes.

## 3. Mandatory verification AFTER every reboot

Put these steps in your `resume_prompt` verbatim, then actually run them:

```bash
tail -n 50 data/logs/bot.log        # any ERROR/CRITICAL/Traceback?
python scripts/smoke_test.py        # must report healthy
python scripts/discord_test.py read <thread_id> 3   # feature-specific check
```

If `smoke_test.py` reports UNHEALTHY, diagnose and fix BEFORE telling the user
the change is done. Never claim success from indirect evidence ("the bot is
talking to me, so it must have rebooted") — check the log timestamps.

## 4. Diagnosis order: log first, Discord second, code last

1. `tail -n 100 data/logs/bot.log` — most failures are already explained here
   (tracebacks, stall snapshots, account cooldowns, merge errors).
2. `python scripts/discord_test.py read <channel_or_thread_id> 10` — see what
   the user actually saw (embeds, buttons, error banners).
3. Only then read code. Grep for the exact log message text to find the
   emitting site fast.

Long debugging session? Run `tail -f data/logs/bot.log` in the background and
check it as you test. Other useful probes: `python scripts/discord_test.py
list-channels`, `list-threads <forum_id>`, `wait-response <channel_id>`.

## 5. Worktree/merge model — and its trap

Build tasks run in git worktrees at `{repo}/.worktrees/{instance-id}/` on a
branch like `claude-bot/t-1234`. The main repo NEVER leaves master — no
`git checkout` in the shared directory. After a build, the user taps Merge or
Discard; autopilot auto-merges.

**The trap: gitignored files are silently dropped from merges.** A merge only
carries tracked files. If a build creates a file that matches .gitignore, the
branch "has" it in its worktree but the commit doesn't, so the merge lands
without it — no error, no warning. This exactly happened with `.claude/skills/`:
the old `.claude/` ignore rule ate a finished skill from a wave-1 build. The
fix (this very playbook's commit) changed the rule to `.claude/*` +
`!.claude/skills/` because git cannot re-include children of an excluded
directory — only of a glob.

Before assuming a build "lost" work: `git log <branch> --stat` to see what the
commits actually contain, and `git check-ignore -v <path>` on anything missing.

## 6. Locks — don't fight them, don't bypass them

- **Per-repo git-admin lock** (asyncio, in `bot/claude/runner.py`): serializes
  worktree add/remove, merge, branch delete per repo. If a git admin operation
  seems hung, another session's merge is probably in flight — check bot.log.
- **Per-repo test-suite mutex** (`{repo}/.worktrees/.test-mutex/`): full test
  runs (`pytest`, `dotnet test`, `npm test`, ...) serialize across parallel
  sessions via PreToolUse/PostToolUse hooks. A blocked full-suite command means
  "the lock is busy," not "something is broken" — run a narrower filtered test
  or do other work and retry. Stale locks self-heal (30-min TTL, dead-holder
  steal); do not delete the lock dir by hand.

## 7. state.json gotchas

`data/state.json` is the bot's persistence. Shape traps:

- `instances` is a **LIST of dicts, not a dict keyed by id** — find one with
  `next(i for i in state["instances"] if i["id"] == "t-1234")`.
- Discord-side state lives under `platform_state.discord` (e.g.
  `forum_projects`, `fleet_pending_verify`).
- Never hand-edit while the bot runs — it saves frequently and will overwrite
  you. Stop-the-world edits only via reboot flow, or better: change code.

## 8. Discord hard limits (violations throw at runtime)

- Max **5 button rows** per View — truncate, don't crash.
- **2000 chars** per regular message, **4096** per embed description — chunk.
- Interactions must be answered in **3 seconds** — always `defer()` first,
  then do slow work.
- Thread-name edits are rate-limited (~2/10min) — use tags for status, not
  renames.
- No pipe tables, no nested bullets, no `---` rules in bot output — Discord
  renders them as garbage.

## 9. Golden rules

- Fix causes, not symptoms: name "X is wrong because Y at Z" before patching.
- Never claim "done/fixed/verified" without a tool-call proof (log line, test
  output, API response).
- Changelog-first: append to `## [Unreleased]` in CHANGELOG.md as you work;
  version headers only on explicit release.
- All settings are per-thread; 10+ sessions run in parallel — never assume
  yours is the only one (no global state, no port squatting, no full-suite
  test spam).
