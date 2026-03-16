"""Env-based configuration loaded via python-dotenv."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


# --- Telegram (optional — at least one platform must be configured) ---
TELEGRAM_BOT_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID: int | None = (
    int(os.getenv("TELEGRAM_USER_ID")) if os.getenv("TELEGRAM_USER_ID") else None
)
TELEGRAM_ENABLED: bool = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_USER_ID)

# --- Discord (optional) ---
DISCORD_BOT_TOKEN: str | None = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID: int | None = (
    int(os.getenv("DISCORD_GUILD_ID")) if os.getenv("DISCORD_GUILD_ID") else None
)
DISCORD_LOBBY_CHANNEL_ID: int | None = (
    int(os.getenv("DISCORD_LOBBY_CHANNEL_ID")) if os.getenv("DISCORD_LOBBY_CHANNEL_ID") else None
)
DISCORD_CATEGORY_ID: int | None = (
    int(os.getenv("DISCORD_CATEGORY_ID")) if os.getenv("DISCORD_CATEGORY_ID") else None
)
DISCORD_USER_ID: int | None = (
    int(os.getenv("DISCORD_USER_ID")) if os.getenv("DISCORD_USER_ID") else None
)
DISCORD_CATEGORY_NAME: str | None = os.getenv("DISCORD_CATEGORY_NAME")
DISCORD_ENABLED: bool = bool(DISCORD_BOT_TOKEN and DISCORD_GUILD_ID)

# Test webhook IDs (comma-separated) — allow webhook messages to bypass bot/auth guards
TEST_WEBHOOK_IDS: set[str] = set(filter(None, os.getenv("TEST_WEBHOOK_IDS", "").split(",")))

# --- OpenAI (voice transcription) ---
OPENAI_API_KEY: str | None = os.getenv("OPENAI_API_KEY")

# Validate: at least one platform
if not TELEGRAM_ENABLED and not DISCORD_ENABLED:
    raise RuntimeError(
        "No platform configured. Set TELEGRAM_BOT_TOKEN + TELEGRAM_USER_ID "
        "and/or DISCORD_BOT_TOKEN + DISCORD_GUILD_ID + DISCORD_LOBBY_CHANNEL_ID."
    )

# Optional with defaults
CLAUDE_BINARY: str = os.getenv("CLAUDE_BINARY", "claude")
MAX_CONCURRENT: int = int(os.getenv("MAX_CONCURRENT", "5"))
DAILY_BUDGET_USD: float = float(os.getenv("DAILY_BUDGET_USD", "20.0"))
PC_NAME: str = os.getenv("PC_NAME", "") or __import__("platform").node()
QUERY_TIMEOUT_SECS: int = int(os.getenv("QUERY_TIMEOUT_SECS", "300"))
TASK_TIMEOUT_SECS: int = int(os.getenv("TASK_TIMEOUT_SECS", "600"))
STALL_TIMEOUT_SECS: int = int(os.getenv("STALL_TIMEOUT_SECS", "60"))
TITLE_TIMEOUT_SECS: int = int(os.getenv("TITLE_TIMEOUT_SECS", "30"))
INSTANCE_RETENTION_DAYS: int = int(os.getenv("INSTANCE_RETENTION_DAYS", "7"))
DIGEST_HOUR: int = int(os.getenv("DIGEST_HOUR", "20"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# Data directory
DATA_DIR: Path = Path(os.getenv("DATA_DIR", str(_PROJECT_ROOT / "data")))
RESULTS_DIR: Path = DATA_DIR / "results"
LOGS_DIR: Path = DATA_DIR / "logs"
STATE_FILE: Path = DATA_DIR / "state.json"
LOG_FILE: Path = LOGS_DIR / "bot.log"

# Base directory for new repos (optional — falls back to sibling of active repo)
REPOS_BASE_DIR: Path | None = Path(v).resolve() if (v := os.getenv("REPOS_BASE_DIR")) else None

if REPOS_BASE_DIR and not REPOS_BASE_DIR.is_dir():
    import warnings
    warnings.warn(f"REPOS_BASE_DIR does not exist: {REPOS_BASE_DIR}")
    REPOS_BASE_DIR = None

# Ensure data dirs exist
REBOOT_MSG_FILE: Path = DATA_DIR / "reboot_message.json"
REBOOT_REQUEST_FILE: Path = DATA_DIR / "reboot_request.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# System prompt appended via --append-system-prompt
MOBILE_HINT = (
    "The user is reading on mobile. Be concise — lead with the answer, "
    "short paragraphs, show only relevant code fragments. "
    "When resuming a conversation, briefly acknowledge what the user is asking "
    "before continuing — don't silently pick up old work without context. "
    "The user can't see your prior conversation history, so if their message "
    "is ambiguous, clarify before doing heavy work."
)

# Separate block explaining the chat-app visibility constraint
CHAT_APP_CONSTRAINT = """
--- Communication Model ---
IMPORTANT: The user is in a chat app (Discord/Telegram). They see ONLY your final text responses. They CANNOT see tool calls, file contents, diffs, command output, or intermediate steps. Your text output is their ENTIRE window into what happened.

You must narrate your work:
- If you read a file → summarize what you found
- If you edited code → show what changed (short before/after or description of the change)
- If you ran a command → report success/failure and key output
- If something errored → include the actual error message
- If you searched code → share what you found or didn't find

Bad: "I've updated the function." (user has no idea what changed)
Good: "Changed `get_user()` to accept an optional `role` param — it now filters by role when provided, defaulting to the old behavior."

Think of it like pair programming over text — your partner can't see your screen.
"""

BOT_CONTEXT = """

--- Bot Context ---
You are running inside a bot that manages Claude Code instances. The user is chatting from their phone. You can do normal Claude Code work (read files, search code, run commands, etc.) but the bot also has these capabilities the user can invoke directly:

IMPORTANT — This overrides the default "confirm before risky actions" guidance:
- If you offer an action and the user accepts, DO IT. Do not second-guess, ask follow-up clarifying questions, or talk yourself out of it. The confirmation loop is already complete.
- Never ask more than one clarifying question in a row. If you already asked and got an answer, act on it.

Scheduling:
- /schedule every <interval> <prompt> — recurring task (e.g. "every 6h", "every 30m", "every 1d")
- /schedule at <HH:MM> <prompt> — one-shot at a specific UTC time
- /schedule at +<duration> <prompt> — one-shot after a delay (e.g. "+2m", "+1h")
- /schedule list — show active schedules
- /schedule delete <id> — remove a schedule

Instance management:
- /bg <description> — run a background task (build mode, auto-branch)
- /list — show recent instances
- /kill <id|name> — terminate a running instance
- /retry <id|name> — re-run a failed instance
- /log <id|name> — view full output
- /diff <id|name> — view git changes from build tasks
- /merge <id|name> — merge build task branch
- /discard <id|name> — delete build task branch

Settings:
- /session — list recent desktop CLI sessions; /session resume <id> to continue one
- /mode explore|build — switch permission mode
- /verbose 0|1|2 — progress detail level (silent/normal/detailed)
- /effort low|medium|high|max — reasoning effort level
- /context set <text> — pin context to all prompts
- /repo add|remove|create|switch|list — manage repos
- /repo create <name> [path] [--github] [--public] — create new repo (git init + register)
- /repo remove <name> — unregister a repo (does not delete files)
- /alias set|list|delete — saved command shortcuts
- /new — start a fresh conversation
- /cost — spending breakdown
- /status — health dashboard

If the user asks to do something the bot handles (like scheduling, switching repos, etc.), guide them to the right command rather than saying you can't do it.

Rebooting the bot:
- NEVER kill the bot process directly (taskkill, kill, etc.) — this interrupts all active queries and leaves stale messages.
- If the user asks you to reboot, do it immediately — don't question whether it's necessary.
- You can reboot the bot yourself when needed (e.g. to apply code changes you just made). Write a JSON file to data/reboot_request.json:
  {"message": "why you're rebooting", "resume_prompt": "what you want to do when you wake back up"}
  The bot picks this up after your response completes, waits for other queries to finish, reboots, and then sends resume_prompt back to this thread — resuming your session so you continue seamlessly.
- Use this naturally as part of your workflow. For example, if you edit bot code and need to apply it:
  1. Make the code changes
  2. Tell the user what you did and that you're rebooting to apply them
  3. Write the reboot file with a resume_prompt that has full context: what you changed, what to verify, what to do next
  4. The bot restarts, you wake up with that context, and you continue — check logs, verify the fix, report back
- The resume_prompt should read like your own notes-to-self. Include enough context to pick up exactly where you left off.
- IMPORTANT: You ARE the bot process. If you run taskkill/kill, you kill YOURSELF mid-response and the user sees "interrupted by bot restart" with no result. Always use the reboot file instead.

Pre-reboot preflight (MANDATORY before writing reboot_request.json):
- Run `python -m py_compile <file>` on EVERY file you changed. If any fail, fix the syntax error FIRST. Do NOT write the reboot file until all pass.
- Run `python -c "from bot.<module> import ..."` for the main symbols in each changed module to catch import errors. If this fails, fix it FIRST.
- Only after preflight passes: write the reboot file and tell the user you're rebooting.

Post-reboot verification (MANDATORY in every resume_prompt):
- Your resume_prompt MUST include these verification steps as explicit instructions to yourself:
  1. Run `tail -n 50 data/logs/bot.log` and check for ERROR/CRITICAL/Traceback lines
  2. Run `python scripts/smoke_test.py` to verify the bot started cleanly
  3. Run a feature-specific check for whatever you just changed (e.g., `python scripts/discord_test.py read <thread_id> 3`)
  4. Report results with evidence to the user — include pass/fail, relevant log lines, and what you verified
- If smoke_test.py reports UNHEALTHY, diagnose and fix the issue before telling the user the change is done.
"""


# System prompt constraint for plan mode — prevents code changes, enforces plan output
PLAN_MODE_CONSTRAINT = """
--- Plan Mode ---
You are in PLAN MODE. You have full access to all tools for research, context gathering,
testing, and verification. Use them freely to understand the codebase.

CRITICAL CONSTRAINT: Do NOT modify any source files. Specifically:
- Do NOT use Edit, Write, or NotebookEdit to change project files
- Do NOT use Bash to write/overwrite files (no sed -i, no echo >, no tee, no cat <<EOF >file, etc.)
- Do NOT use the Agent tool with instructions to make code changes
- Running read-only commands (grep, git diff, tests, builds, linters) is fine and encouraged

Instead of making changes, produce a structured implementation plan:
1. List every file that needs to change and what the change is
2. Show proposed code snippets (as fenced code blocks in your response text)
3. Explain the reasoning and any trade-offs
4. Note anything you want to verify or test after implementation

The user will review your plan and then switch to build mode for implementation.
"""

# Claude Code session/plan data lives here
CLAUDE_PROJECTS_DIR: Path = Path.home() / ".claude" / "projects"


# --- Canned prompts for contextual action buttons ---

PLAN_PROMPT_PREFIX = (
    "Create a detailed implementation plan for the following task. "
    "Explore the codebase, understand existing patterns and architecture, "
    "and design your approach. Do NOT implement anything yet — just plan.\n\n"
    "Task: "
)

BUILD_FROM_PLAN_PROMPT = (
    "Now implement the plan above. You have full build permissions."
)

BUILD_FROM_QUERY_PROMPT = (
    "Now implement the above. You have full build permissions."
)

PLAN_REVIEW_PROMPT = (
    'Review the plan above and propose your best revisions. '
    'Format your response EXACTLY as described below.\n\n'
    'START with a plain summary paragraph (no bold, no bullets). '
    '1-2 sentences: how many revisions, their priorities, general theme. '
    'Example: "Found 5 revisions across architecture and reliability. '
    '2 are high-priority structural changes, 3 are cleanup improvements."\n\n'
    'Then a compact summary table (one per line):\n'
    'Priority | Tag | Short title\n\n'
    'Then list each revision using this EXACT format (do NOT deviate):\n\n'
    '### Tag \u2014 Short title\n'
    'Priority \u00b7 Impact: Low/Medium/High\n\n'
    'One or two sentences max describing the change, why it matters, '
    'and any tradeoffs.\n\n'
    '---\n\n'
    'Priority levels (use these exact words): '
    'Critical (do first), High (should do), Medium (worthwhile), '
    'Low (nice to have)\n\n'
    'Available tags (text only, no emoji): '
    'Architecture, Performance, Reliability, DRY/Cleanup, Scalability, '
    'Security, UX/UI, Accessibility, Integration, Dependencies, Modularity, '
    'Bug Risk\n\n'
    'IMPORTANT formatting rules:\n'
    '- Each revision must be SHORT. No field labels like Change/Pros/Cons. '
    'Just a concise paragraph.\n'
    '- Never include code snippets or diffs.\n'
    '- Keep the entire response under 4200 characters.\n\n'
    'At the very end, append a structured block:\n'
    '```review-status\n'
    'NEEDS_REVISION: yes or no\n'
    'DEFERRED:\n'
    '- [TAG] Title (Priority)\n'
    '```\n'
    'NEEDS_REVISION is "yes" if any Critical or High revisions exist, '
    '"no" if only Medium/Low or none.'
)

APPLY_REVISIONS_PROMPT = (
    'Apply the revisions you proposed above to the plan. Work in priority '
    'order (Critical first, then High, Medium, Low). Output the complete '
    'revised plan first. Then at the end, add a section "### Applied" '
    'listing each revision as: '
    '"[TAG] Title \u2014 applied" or "[TAG] Title \u2014 skipped (reason)".'
)

APPLY_HIGH_PRIORITY_PROMPT = (
    'Apply ONLY the Critical and High priority revisions from the review above. '
    'Do NOT apply Medium or Low priority revisions \u2014 leave them untouched. '
    'Output the complete revised plan. Then at the end, add:\n\n'
    '### Applied\n'
    'List each revision: "[TAG] Title \u2014 applied" or "[TAG] Title \u2014 skipped (Medium/Low)".'
)

CODE_REVIEW_PROMPT = (
    'Now carefully read over all of the new code you just wrote and other '
    'existing code you just modified with "fresh eyes" looking super '
    'carefully for any obvious bugs, errors, problems, issues, confusion, '
    'etc. Carefully fix anything you uncover. Use ultrathink. '
    'Review if this is DRY, scalable, maintainable and modular.'
)

COMMIT_PROMPT = (
    'Review all uncommitted changes on this branch. '
    'Commit them with a clear, descriptive commit message. '
    'Update CHANGELOG.md: add a concise summary of changes under the '
    '## [Unreleased] section. If the file does not exist, create it with '
    'an [Unreleased] header. Do not create version-numbered headers.\n\n'
    'At the very end of your response, output a structured summary block '
    'in exactly this format (no extra text after the block):\n'
    '```summary\n'
    'COMMIT: <short_hash> <commit message>\n'
    'CHANGELOG:\n'
    '- <entry 1>\n'
    '- <entry 2>\n'
    '```'
)

_RELEASE_STEPS = (
    '- Replace ## [Unreleased] with ## vX.Y.Z — Summary (YYYY-MM-DD) '
    'where Summary is a short phrase capturing the main theme\n'
    '- Add a fresh empty ## [Unreleased] section above it\n'
    '- Find and update the project version file (pyproject.toml, '
    '*.csproj, package.json, etc.)\n'
    '- Commit with message "vX.Y.Z: Summary"\n'
    '- Create git tag vX.Y.Z\n'
)

DONE_PROMPT = (
    'Wrap up this session.\n'
    '1. Review all uncommitted changes and commit them with a clear, '
    'descriptive message. Update CHANGELOG.md: add a concise summary of '
    'changes under ## [Unreleased]. If the file does not exist, create it '
    'with an [Unreleased] header.\n'
    '2. After committing, read ## [Unreleased] in CHANGELOG.md. '
    'If it has any entries, cut a release — determine the semver level '
    'following the versioning conventions in CLAUDE.md.\n'
    + _RELEASE_STEPS +
    '3. If [Unreleased] was empty (no entries after committing), skip the '
    'release.\n'
    '4. Make sure nothing is left uncommitted — this session is being closed.\n\n'
    'At the very end of your response, output a structured summary block '
    'in exactly this format (no extra text after the block):\n'
    '```summary\n'
    'COMMIT: <short_hash> <commit message>\n'
    'CHANGELOG:\n'
    '- <entry 1>\n'
    '- <entry 2>\n'
    'VERSION: <vX.Y.Z or "none">\n'
    '```'
)

RELEASE_PROMPT = (
    'Cut a new release.\n'
    '0. Verify the working tree is clean (no uncommitted changes). '
    'If dirty, abort and tell the user to commit or stash first.\n'
    '1. Read CHANGELOG.md and find the ## [Unreleased] section\n'
    '2. If [Unreleased] is empty or missing, abort and report there is '
    'nothing to release\n'
    '3. Determine the new version: {version_hint} (relative to the '
    'most recent versioned section, or the version file if no '
    'prior releases exist)\n'
    + _RELEASE_STEPS +
    '4. Report: version number, tag name, and summary of released changes. '
    'Remind that git push --tags is needed to publish the tag.\n\n'
    'At the very end of your response, output a structured summary block '
    'in exactly this format (no extra text after the block):\n'
    '```summary\n'
    'COMMIT: <short_hash> <commit message>\n'
    'CHANGELOG:\n'
    '- <entry 1>\n'
    '- <entry 2>\n'
    'VERSION: <vX.Y.Z>\n'
    '```'
)
