"""Env-based configuration loaded via python-dotenv."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

# On Windows, prevent subprocess console windows from popping up
NOWND: dict = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
)

# Load .env from project root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=True)

# Per-machine overlay, layered on top of the shared .env. Only for settings
# that genuinely DIFFER between machines rather than being two names for one
# thing — CLAUDE_BINARY above all, which is an OS-native install location with
# no counterpart on the other side to translate to. Anything that IS merely
# another spelling of the same directory belongs in the path map below, not
# here. No file = no overlay, which is the normal single-machine case.
_PLATFORM_ENV = {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")
_OVERLAY = _PROJECT_ROOT / f".env.{_PLATFORM_ENV}"
if _OVERLAY.is_file():
    load_dotenv(_OVERLAY, override=True)

# Path portability. `.env` and `data/state.json` are shared verbatim between
# machines (they live on the drive both boot from), so the absolute paths in
# them can only ever be correct for one of those machines. `paths.translate()`
# rewrites a stored path into the local spelling on the way in; this is the
# first of the two doors it is applied at, the other being the state load in
# bot.store.state. Seeded before any path constant below is derived.
#
# roots.json is keyed off the code location rather than DATA_DIR on purpose:
# DATA_DIR is itself a translatable path, and bootstrapping the translator
# from a value it has not translated yet is how you get a map that only works
# when it wasn't needed.
from bot import paths as _paths  # noqa: E402

_paths.init(
    data_dir=_PROJECT_ROOT / "data",
    account_hints=[
        p.strip() for p in os.getenv("CLAUDE_ACCOUNTS", "").split(",") if p.strip()
    ],
    # Where the code itself is sitting. Lets the root be identified by walking
    # up to an existing marker, which is the only detection that survives a
    # mount point no configuration predicted (a live USB, a relabelled drive).
    here=_PROJECT_ROOT,
)


# --- Telegram (stripped — shell only, not started) ---
TELEGRAM_BOT_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID: int | None = (
    int(os.getenv("TELEGRAM_USER_ID")) if os.getenv("TELEGRAM_USER_ID") else None
)
TELEGRAM_ENABLED: bool = False  # Telegram stripped — shell only

# --- Discord ---
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

# --- Twitter/X (direct API v2) ---
TWITTER_BEARER_TOKEN: str = os.getenv("TWITTER_BEARER_TOKEN", "")

# Validate: Discord must be configured
if not DISCORD_ENABLED:
    raise RuntimeError(
        "Discord not configured. Set DISCORD_BOT_TOKEN + DISCORD_GUILD_ID "
        "+ DISCORD_LOBBY_CHANNEL_ID in .env."
    )

# --- Provider selection ---
# "claude" (default), "cursor". "codex" reserved but not yet supported.
PROVIDER: str = os.getenv("PROVIDER", "claude").lower()

# Lazy-loaded at module level — validates provider name immediately.
from bot.claude.provider import get_provider as _get_provider  # noqa: E402
_PROVIDER_CFG = _get_provider(PROVIDER)

# Binary and branch prefix — derived from provider, overridable via env.
# NOT translated, unlike every other path here — the overlay above owns this
# one. Translating it would turn `C:/…/npm/claude.cmd` into a path that really
# does exist on the mounted drive and really is a Windows batch file, so Linux
# would run it and fail with an exec error instead of the honest "that binary
# is not here". A wrong path that resolves is worse than one that doesn't.
CLAUDE_BINARY: str = os.getenv("CLAUDE_BINARY") or _PROVIDER_CFG.binary
BRANCH_PREFIX: str = os.getenv("BRANCH_PREFIX") or _PROVIDER_CFG.branch_prefix

# Cursor-specific: default model (free tier = "auto", paid = specific model)
CURSOR_MODEL: str = os.getenv("CURSOR_MODEL", "auto")
MAX_CONCURRENT: int = int(os.getenv("MAX_CONCURRENT", "5"))
DAILY_BUDGET_USD: float = float(os.getenv("DAILY_BUDGET_USD", "20.0"))
PC_NAME: str = os.getenv("PC_NAME", "") or __import__("platform").node()
STALL_TIMEOUT_SECS: int = int(os.getenv("STALL_TIMEOUT_SECS", "60"))
# Re-log a fresh stall snapshot every N seconds while a session is still
# silent. Lets us correlate CPU%, conn count, and last event type over the
# silent period so a forensic read of bot.log can distinguish "thinking
# with API call open" from "actually hung locally".
STALL_DIAG_RELOG_SECS: int = int(os.getenv("STALL_DIAG_RELOG_SECS", "60"))
MAX_PROCESS_LIFETIME_SECS: int = int(os.getenv("MAX_PROCESS_LIFETIME_SECS", "14400"))

# --- Per-session memory guard ---
#
# The runner watches the resident memory of each session's WHOLE process tree,
# not just the CLI. The distinction is the entire point: on 2026-08-17 the
# stall log for a session reported "336MB" while a grandchild three levels down
# sat at 13.7 GB, seconds from taking the machine out. The CLI is a thin
# supervisor; the memory always lives in what it spawned.
#
# WARN posts once into the thread so the session can see it is heading for
# trouble while it can still do something about it. KILL reaps that session's
# tree — deterministically, naming the session and the number — rather than
# leaving the kernel to pick a victim across the whole machine.
#
# Defaults are sized to fire below the cgroup's MemoryMax (16G, see
# scripts/claude-bot.service) so the bot gets to act first and explain itself;
# the cgroup limit is the backstop for a spike too fast to sample.
# Set SESSION_MEM_KILL_MB=0 to disable the kill and keep warnings only.
#
# These numbers were lowered on 2026-08-21 (from 6 GB warn / 12 GB kill) after
# a global OOM kill took the user's browser while the bot sat inside its own
# limits. A 12 GB per-session ceiling was larger than the machine's entire
# spare capacity with a browser, Discord and a chart app running: it could only
# ever fire after the machine was already lost. A ceiling has to be reachable
# before the thing it protects against happens, or it is decoration.
SESSION_MEM_WARN_MB: int = int(os.getenv("SESSION_MEM_WARN_MB", "4096"))
SESSION_MEM_KILL_MB: int = int(os.getenv("SESSION_MEM_KILL_MB", "8192"))
# How often to sample. The tree walk costs a readdir plus a statm read per
# process, so 30s is cheap even with ten sessions running; the sampler is also
# what makes a slow leak visible in bot.log before it matters.
SESSION_MEM_CHECK_SECS: int = int(os.getenv("SESSION_MEM_CHECK_SECS", "30"))

# --- Machine-wide memory pressure ---
#
# Everything above is the bot measuring itself. These are the bot measuring the
# machine, which is the gap that the 2026-08-21 incident lived in. The kernel's
# dump at 16:55:50 recorded under 230 MB free on a 31 GB box and `Free swap =
# 68kB`, then ran a *global* OOM kill and shot a 5.44 GB Chrome. Our unit's
# `memory.events` recorded oom_kill 0 — every guard in the bot reported normal,
# because none of them had ever asked what was left outside its cgroup. The
# critical-available default below is set where that reading is unambiguous.
#
# Read by bot/claude/memory.py:read_pressure(), which turns them into one of
# ok / tight / critical. Passed as arguments rather than read there directly so
# the harness can drive the classifier at fixed numbers.
MEM_PRESSURE_CRITICAL_AVAIL_MB: float = float(
    os.getenv("MEM_PRESSURE_CRITICAL_AVAIL_MB", "1024")
)
MEM_PRESSURE_TIGHT_AVAIL_MB: float = float(
    os.getenv("MEM_PRESSURE_TIGHT_AVAIL_MB", "2560")
)
# Swap here is zram — compressed RAM. A high figure is normal on a busy machine
# and does not by itself mean the next allocation fails, so full swap alone is
# only TIGHT; it takes low available memory alongside it to be critical.
MEM_PRESSURE_CRITICAL_SWAP_PCT: float = float(
    os.getenv("MEM_PRESSURE_CRITICAL_SWAP_PCT", "90")
)
# Kernel pressure-stall percentages from /proc/pressure/memory: the share of
# the last 10s in which some task was blocked waiting on memory. The only one
# of these signals that reports thrashing rather than occupancy, and the one
# that goes off earliest — `available` can still read in gigabytes while the
# machine is spending most of its time reclaiming.
MEM_PRESSURE_CRITICAL_PSI_PCT: float = float(
    os.getenv("MEM_PRESSURE_CRITICAL_PSI_PCT", "40")
)
MEM_PRESSURE_TIGHT_PSI_PCT: float = float(
    os.getenv("MEM_PRESSURE_TIGHT_PSI_PCT", "10")
)

# --- Admission control: don't add load to a machine that is already out ---
#
# MAX_CONCURRENT bounds how many sessions run at once; it says nothing about
# whether the machine can afford the next one. When pressure reads critical a
# starting session waits in its slot instead of spawning, after first trying to
# reclaim idle build daemons (which is usually enough on its own).
#
# The wait is bounded and then proceeds anyway. Memory pressure the bot did not
# create — a browser that ate 6 GB — would otherwise block work forever, and
# refusing all work because something else is fat is a worse failure than
# starting one more 400 MB CLI.
MEM_ADMISSION_ENABLED: bool = os.getenv(
    "MEM_ADMISSION_ENABLED", "1"
).lower() in ("1", "true", "yes")
MEM_ADMISSION_MAX_WAIT_SECS: int = int(
    os.getenv("MEM_ADMISSION_MAX_WAIT_SECS", "300")
)
MEM_ADMISSION_POLL_SECS: int = int(os.getenv("MEM_ADMISSION_POLL_SECS", "15"))

# --- Reclaiming memory nobody owns ---
#
# Processes charged to the bot's cgroup that are no longer descendants of the
# bot: build servers that outlive the build. In the kernel's task table from
# the 2026-08-21 OOM, a `dotnet` at 4.10 GB was the second-largest process on
# the whole machine while each `claude` session held ~0.3 GB — a detached .NET
# Roslyn compiler server is invisible to a guard that only walks downward from
# each session, and no session could have been reaped to free it.
#
# Anything above MEM_ORPHAN_MIN_MB is logged. Only known cache daemons that are
# also idle and old enough get killed (see RECLAIMABLE_DAEMONS in
# bot/claude/memory.py) — they cost the next build a cold start and nothing
# else. Pids an armed /watch is waiting on are never touched.
MEM_ORPHAN_SWEEP: bool = os.getenv(
    "MEM_ORPHAN_SWEEP", "1"
).lower() in ("1", "true", "yes")
MEM_ORPHAN_MIN_MB: float = float(os.getenv("MEM_ORPHAN_MIN_MB", "256"))
MEM_ORPHAN_MIN_AGE_SECS: float = float(os.getenv("MEM_ORPHAN_MIN_AGE_SECS", "60"))
MEM_ORPHAN_CPU_IDLE_PCT: float = float(os.getenv("MEM_ORPHAN_CPU_IDLE_PCT", "5"))

# --- Cross-session arbitration ---
#
# The per-session ceiling answers "is one session out of control". It cannot
# answer "are five reasonable sessions collectively killing this machine",
# which is the case where the cgroup OOM killer picks a victim instead of the
# bot — safe, since OOMPolicy=continue keeps the unit alive, but silent: the
# session that dies is never told why.
#
# So when the machine reads critical AND the bot's own cgroup is over its
# MemoryHigh — i.e. we are demonstrably the ones filling it — the largest live
# session tree is reaped and told exactly that. Both halves are required. Under
# pressure the bot did not create, reaping our own sessions frees nothing the
# offender will not immediately re-take, and costs a session's work for it.
#
# The victim floor exists because reaping the largest of five 400 MB sessions
# is pure loss: it frees nothing worth having and destroys real work.
SESSION_MEM_FLEET_ARBITRATION: bool = os.getenv(
    "SESSION_MEM_FLEET_ARBITRATION", "1"
).lower() in ("1", "true", "yes")
SESSION_MEM_FLEET_MIN_VICTIM_MB: float = float(
    os.getenv("SESSION_MEM_FLEET_MIN_VICTIM_MB", "1024")
)
# Quiet period after a cross-session reap. Freeing memory is not instant — the
# kernel reclaims over seconds — so the next session's watchdog would read the
# same critical pressure and take itself out for a spike the first kill already
# fixed. Without this, one crunch unwinds the whole fleet in about a minute.
FLEET_REAP_COOLDOWN_SECS: float = float(
    os.getenv("FLEET_REAP_COOLDOWN_SECS", "120")
)
# How many times a run may be auto-resumed after the guard killed it for
# memory. Deliberately lower than CONTEXT_THRASH_MAX_RETRIES: a context thrash
# is a bookkeeping problem that a fresh window genuinely fixes, whereas a
# memory ceiling is physical, so a second identical attempt just burns twenty
# minutes to hit the same wall. One retry buys the agent exactly one chance to
# adapt (smaller batch, fewer frames, or an honest "this does not fit").
# Clamped to 0..3; 0 disables auto-resume.
MEMORY_KILL_MAX_RETRIES: int = max(
    0, min(3, int(os.getenv("MEMORY_KILL_MAX_RETRIES", "1")))
)
# Total wall-clock budget for the post-build computational sensor step
# (dotnet build / ruff / tsc). Sensors that don't fit are marked skipped.
SENSOR_TOTAL_BUDGET_SECS: int = int(os.getenv("SENSOR_TOTAL_BUDGET_SECS", "900"))
# Grace period after the LLM emits stop_reason="end_turn" with no
# tool_use blocks.  If the CLI stays silent for this long, we treat
# the session as complete and force-terminate.  Catches a `claude -p`
# bug where stdout stays open after end_turn.  30s gives any post-stop
# hook (default 30s timeout) room to fire.
END_OF_TURN_GRACE_SECS: int = int(os.getenv("END_OF_TURN_GRACE_SECS", "30"))
REBOOT_DRAIN_TIMEOUT_SECS: int = int(os.getenv("REBOOT_DRAIN_TIMEOUT_SECS", "600"))
# Deferred-reboot retention TTL: when an assistant- or auto-update-initiated
# reboot defers because another session was active, the deferred file is
# auto-promoted to a fresh reboot request at the next idle session-end. After
# this many seconds the original intent is too stale to be safely resumed and
# the deferred file is dropped with an ERROR log line instead.
REBOOT_DEFERRED_TTL_SECS: int = int(os.getenv("REBOOT_DEFERRED_TTL_SECS", "3600"))
TITLE_TIMEOUT_SECS: int = int(os.getenv("TITLE_TIMEOUT_SECS", "15"))
INSTANCE_RETENTION_DAYS: int = int(os.getenv("INSTANCE_RETENTION_DAYS", "7"))
# Hard cap on retained terminal (completed/failed/killed) instances. Even if
# they are younger than the retention window, only the most-recent N survive a
# prune. Bounds state.json size so per-save serialization can't grow unbounded
# and stall the event loop (root cause of "interaction failed" 10062 errors).
INSTANCE_MAX_RETAINED: int = int(os.getenv("INSTANCE_MAX_RETAINED", "250"))

# Install a PreToolUse hook into each build worktree to mechanically block
# tools (Bash, Edit, Write, MultiEdit, NotebookEdit) from touching the main
# repo path. Catches the path-poisoning failure mode where a build session
# resumes a planning session whose context contained main-repo absolute
# paths, then silently edits the wrong tree (t-3920).
#
# Verified against Claude Code 2.1.123: canonical hook schema works, exit-2
# stderr blocks tool calls, settings.local.json loads when the CLI is
# invoked with --setting-sources user,project,local (the Claude provider
# adds that flag automatically). Set WORKTREE_HOOK_ENABLED=0 to disable.
WORKTREE_HOOK_ENABLED: bool = os.getenv("WORKTREE_HOOK_ENABLED", "1") == "1"

# Serialize full test-suite runs across parallel sessions on the same repo
# (t-5976). Worktrees isolate files but not ports, databases, or CPU — five
# parallel `dotnet test` runs starved each other into orphaned/hung suites.
# Installed as PreToolUse (acquire/wait/block) + PostToolUse (release)
# hooks per worktree, piggybacking on the worktree-guard install; requires
# WORKTREE_HOOK_ENABLED. Lock lives at {repo}/.worktrees/.test-mutex/.
# Per-repo opt-out / pattern overrides: {repo}/.claude/parallel.json.
# Set TEST_MUTEX_ENABLED=0 to disable globally.
TEST_MUTEX_ENABLED: bool = os.getenv("TEST_MUTEX_ENABLED", "1") == "1"

# API billing fallback (used when subscription limits are hit)
ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")
API_FALLBACK_MODEL: str = os.getenv("API_FALLBACK_MODEL", "haiku")
API_FALLBACK_MAX_USD: float = float(os.getenv("API_FALLBACK_MAX_USD", "1.0"))
API_FALLBACK_DAILY_MAX_USD: float = float(os.getenv("API_FALLBACK_DAILY_MAX_USD", "5.0"))
API_FALLBACK_ENABLED: bool = bool(ANTHROPIC_API_KEY)

# Model routing: use a lighter model for exploration/plan steps to save cost.
# Set to e.g. "sonnet" to route plan/review_plan/apply_revisions to Sonnet.
EXPLORE_MODEL: str | None = os.getenv("EXPLORE_MODEL")

# --- Post-Fable-PPU model policy (Fable 5 leaves subscription July 8, 2026) ---
# Default model for any session that has no explicit model choice. Unset =
# current behavior: no --model flag, the account's CLI default runs. Applied
# as the LAST fallback at command-build time (provider.build_command) so
# DIRECT sessions get it too — and a model-limit failover downgrade
# (model_override) always beats it, so it can never undo an explicit
# downgrade. _is_primary_model in runner.py resolves None through this value
# so the Fable-limit machinery doesn't misread an Opus-defaulted run as Fable.
DEFAULT_SESSION_MODEL: str | None = os.getenv("DEFAULT_SESSION_MODEL") or None


def _parse_model_routing(raw: str) -> dict[str, str]:
    """Parse ``MODEL_ROUTING=plan:fable,review_code:fable`` into a dict.

    Keys must be InstanceOrigin values (bot/claude/types.py); values are
    ``--model`` names. Malformed entries and unknown origins are skipped
    with a warning so a typo degrades to default routing, never a crash.
    """
    import logging as _logging

    from bot.claude.types import InstanceOrigin as _Origin

    _log = _logging.getLogger(__name__)
    valid_origins = {o.value for o in _Origin}
    routing: dict[str, str] = {}
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        key, sep, value = entry.partition(":")
        key, value = key.strip(), value.strip()
        if not sep or not key or not value:
            _log.warning("MODEL_ROUTING: skipping malformed entry %r", entry)
            continue
        if key not in valid_origins:
            _log.warning(
                "MODEL_ROUTING: skipping unknown origin %r (valid: %s)",
                key, ", ".join(sorted(valid_origins)),
            )
            continue
        routing[key] = value
    return routing


# Per-workflow-step model routing. Applied at spawn time by
# workflows.resolve_spawn_model (the choke point spawn_from and every manual
# spawn site — /bg, /release, the merge resolver — funnel through) as an
# explicit *per-origin override* layered on top of the BUILD_ORIGINS category
# default below — highest per-instance precedence, but still beneath the
# model-limit failover downgrade. Use it to pin one specific step to a model;
# the category default handles the plan-vs-build split for everything else.
MODEL_ROUTING: dict[str, str] = _parse_model_routing(os.getenv("MODEL_ROUTING", ""))

# Explicit suggestion list for the /model autocomplete, e.g.
# MODEL_CHOICES=opus,sonnet,haiku. Purely cosmetic -- /model accepts any
# well-formed name whether or not it appears here. Unset (the normal case)
# means the list is derived at runtime from the models this deployment has
# actually run plus the ones its own settings reference, so it keeps up with
# model renames and version bumps without a code change.
MODEL_CHOICES: list[str] = [
    m.strip().lower() for m in os.getenv("MODEL_CHOICES", "").split(",") if m.strip()
]

# Strong model for build-family origins (see BUILD_ORIGINS in types.py).
# Applied at spawn time by workflows.resolve_spawn_model (spawn_from plus the
# manual spawn sites); beats EXPLORE_MODEL and the DEFAULT_SESSION_MODEL
# fall-through, loses to an explicit MODEL_ROUTING entry and to the model-limit
# failover downgrade. Defaults to opus so this routing is behaviour-neutral
# until DEFAULT_SESSION_MODEL is pointed at a lighter thinking model.
BUILD_MODEL: str = os.getenv("BUILD_MODEL", "opus")

# Model-specific limit failover: some models (Fable 5) have their own quota
# on top of the account-wide 5h/weekly caps.  PRIMARY_MODEL is a substring
# key naming the accounts' default model (matched case-insensitively against
# --model values and used as the cooldown label); MODEL_FALLBACK is what
# runs while the primary is limited.  Subscription-only — this path never
# routes to pay-per-use.  PRIMARY_MODEL is ALSO passed verbatim as --model
# when resuming a session that has no other model resolved (provider.py's
# sticky-resume guard), so it must be a valid CLI model alias, not just a
# match substring.
PRIMARY_MODEL: str = os.getenv("PRIMARY_MODEL", "fable")
MODEL_FALLBACK: str = os.getenv("MODEL_FALLBACK", "opus")

# Multi-account failover: comma-separated list of Claude config dirs.
# When the active account hits its usage limit, the bot automatically
# retries on the next available account.
# e.g. "C:/Users/Quincy/.claude,C:/Users/Quincy/.claude-account2"
CLAUDE_ACCOUNTS: list[str] = [
    _paths.translate(p.strip())
    for p in os.getenv("CLAUDE_ACCOUNTS", "").split(",")
    if p.strip()
]

# Cooldown for an account that fails auth / can't start (e.g. a cancelled or
# paused subscription). Unlike a usage limit this carries no reset time, so we
# sideline the account for a fixed window then re-probe — the probe is the
# auto-recovery path if the subscription is reinstated. 24h default: an
# inactive account kept in CLAUDE_ACCOUNTS costs at most one failed attempt
# per day instead of one per limit-hit. Floored at 60s so a stray 0 can't
# turn every confident match into a per-task double-spawn.
ACCOUNT_AUTH_COOLDOWN_SECS: int = max(
    60, int(os.getenv("ACCOUNT_AUTH_COOLDOWN_SECS", "86400"))
)

# How many times a run may be auto-resumed after the CLI aborts it for
# autocompact thrashing (see parser.is_context_thrash_error). The thrash
# counter is per-process, so a resume clears it — this is the click the user
# used to have to make. Bounded because a session whose context is
# structurally too heavy will keep tripping it, and each attempt is a real
# 20-minute run: 2 retries = 3 attempts total, then the failure surfaces
# normally with the Retry button. Clamped to 0..5; 0 disables auto-resume.
CONTEXT_THRASH_MAX_RETRIES: int = max(
    0, min(5, int(os.getenv("CONTEXT_THRASH_MAX_RETRIES", "2")))
)

# Prepended to the prompt of an auto-resumed attempt only. The CLI's own
# "read in smaller chunks" advice goes to the operator, not to the agent —
# the agent just sees its process vanish, so without this it walks straight
# back into the same wall. The git-status line matters as much as the
# discipline list: the worktree still holds every edit the killed attempt
# made, and an agent that assumes otherwise redoes work it already did.
CONTEXT_THRASH_NUDGE = (
    "--- Automatic recovery: your previous attempt was aborted ---\n"
    "The Claude Code CLI killed your last run: the context window refilled to "
    "the limit within 3 turns of each auto-compaction, 3 times in a row. This "
    "session has been resumed for you, so you may be missing detail that was "
    "compacted away.\n"
    "Your work is NOT lost — every edit the aborted attempt made is still on "
    "disk. Take stock of what is already done FIRST and carry on from there; "
    "do not start over. In a repo that means `git status` and `git diff` "
    "before anything else.\n"
    "To avoid tripping the same guard again: read large files in ranges "
    "(offset/limit) instead of whole, pipe command output through `head`/"
    "`grep`/`wc` instead of dumping it, prefer targeted searches over broad "
    "ones, and delegate large-file sweeps to a subagent so the bulk never "
    "enters this context."
)

# Same slot, same reasoning, different wall: prepended to the prompt of an
# attempt auto-resumed after the memory guard reaped the previous one. Carries
# the actual numbers because they are the whole content of the advice — "use
# less memory" is useless, "you were at 13.7 GB and 12.0 GB is the ceiling"
# tells the agent how much smaller its batch has to get. The command that did
# it is named for the same reason: the agent's own transcript ends before the
# kill, so it cannot otherwise know which of its commands was the problem.
# Placeholders: peak_gb, limit_gb, offender, avail_gb.
MEMORY_KILL_NUDGE_TEMPLATE = (
    "--- Automatic recovery: your previous attempt was stopped for memory ---\n"
    "Your last run was killed by this bot, not by the CLI and not by an error "
    "in your code: the processes it had running grew to {peak_gb} GB of "
    "resident memory, past the {limit_gb} GB ceiling a single session is "
    "allowed on this machine. The largest process at the time was "
    "`{offender}`. About {avail_gb} GB was free machine-wide.\n"
    "This is a hard physical limit, not a flaky failure. Re-running the same "
    "command unchanged WILL be killed again — that exact loop is why this "
    "guard exists. Before anything else, work out how to make the job fit: "
    "smaller batch or tile size, fewer items per process, stream instead of "
    "loading everything at once, process in chunks and write each one out, or "
    "run on the GPU if the memory was a model on the CPU. If it genuinely "
    "cannot be made to fit, say so plainly and stop — that is a useful answer "
    "and far better than another kill.\n"
    "Your work is NOT lost — every edit the killed attempt made is still on "
    "disk. Take stock of what is already done FIRST and carry on from there; "
    "in a repo that means `git status` and `git diff` before anything else.\n"
    "One more thing worth checking: a background job you started with `&` or "
    "`nohup` keeps running after the command that launched it returns, and it "
    "counts against this same ceiling. If you left one running, it was killed "
    "too."
)

# The same note for the other kind of memory kill: this session was not over
# its own ceiling, the MACHINE ran out and this was the largest tree running.
# Kept separate rather than parameterised because the advice differs at the
# root — "your job is too big" and "your job was the biggest of several" call
# for different next steps, and a template that hedges between them would give
# useful guidance for neither. Placeholders: peak_gb, limit_gb, avail_gb,
# pressure.
MEMORY_FLEET_KILL_NUDGE_TEMPLATE = (
    "--- Automatic recovery: your previous attempt was stopped for memory ---\n"
    "Your last run was killed by this bot to keep the machine alive, and the "
    "reason is worth reading carefully because it is NOT the usual one: you "
    "were not over your own per-session ceiling of {limit_gb} GB. Your "
    "processes were at {peak_gb} GB, and the machine as a whole ran out — "
    "{pressure}, with about {avail_gb} GB free. Several sessions run here at "
    "once alongside the user's own desktop applications; yours was simply the "
    "largest tree at the moment something had to give.\n"
    "So the fix is not necessarily to make the job much smaller — it is to "
    "make it fit in a shared machine. Prefer streaming or chunked work over "
    "loading everything at once, bound the parallelism of anything you launch, "
    "and release memory between stages rather than holding it for the whole "
    "run. If the job has a genuine floor above what is available here, say so "
    "plainly and stop rather than being reaped a second time.\n"
    "Your work is NOT lost — every edit the killed attempt made is still on "
    "disk. Take stock of what is already done FIRST and carry on from there; "
    "in a repo that means `git status` and `git diff` before anything else.\n"
    "One more thing worth checking: a background job you started with `&` or "
    "`nohup` keeps running after the command that launched it returns, and it "
    "counts against this same ceiling. If you left one running, it was killed "
    "too."
)

# Told to every session up front, in the system prompt, rather than only after
# a kill. The old arrangement taught an agent about the memory ceiling by
# enforcing it: the first thing it ever heard about memory was that its run had
# just been destroyed. An agent that knows the budget in advance can choose a
# streaming approach the first time instead of discovering the limit with a
# twenty-minute job. Placeholders: kill_gb, warn_line — the warning sentence is
# passed in already rendered rather than as a bare number, because
# SESSION_MEM_WARN_MB=0 is a legal setting and "you get one warning at 0 GB" is
# worse than saying nothing about warnings at all.
MEMORY_BUDGET_CONTEXT_TEMPLATE = (
    "--- Memory Budget ---\n"
    "This machine runs several sessions at once, alongside the user's own "
    "desktop applications (browser, chat, editors). Memory is shared and it is "
    "the scarcest resource here — it has twice been exhausted badly enough to "
    "freeze the machine.\n"
    "Your session has a ceiling of {kill_gb} GB of resident memory across "
    "EVERY process you start — not just the CLI, but any script, build, test "
    "run, or background job it spawns, at any depth.{warn_line} Past the "
    "ceiling your whole process tree is killed and the run fails.\n"
    "What that means when you work:\n"
    "- Prefer streaming or chunked processing over loading a whole dataset, "
    "model, or file set into memory at once. Write each chunk out as you go.\n"
    "- Bound the parallelism of anything you launch. `-j$(nproc)` on a large "
    "build, or a worker pool sized to the CPU count, multiplies peak memory by "
    "the worker count.\n"
    "- A job started with `&` or `nohup` keeps running after the command that "
    "launched it returns and still counts against your ceiling. If you start "
    "one, either wait for it or arm a `/watch` on it — never leave it running "
    "unattended.\n"
    "- Build servers outlive the build that started them and can hold "
    "gigabytes doing nothing: run `dotnet build-server shutdown` after a .NET "
    "build you are not about to repeat.\n"
    "- Check before you commit to a big job rather than after: `free -m` costs "
    "nothing, and a job sized to what is actually free beats one that gets "
    "reaped at minute nineteen.\n"
    "- If a job genuinely does not fit on this machine, say so plainly and "
    "stop. That is a useful answer and far better than being killed for it.\n"
    "If the machine is short on memory the bot may hold your session briefly "
    "before it starts, or reap the largest running session. Neither is a bug, "
    "and both are reported in the thread."
)

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ccusage cache TTL in seconds (adaptive: shortened near rate limits)
CCUSAGE_CACHE_TTL: int = int(os.getenv("CCUSAGE_CACHE_TTL", "60"))

# Claude plan settings (for usage percentage display)
PLAN_NAME: str = os.getenv("PLAN_NAME", "Max 20x")
PLAN_MONTHLY_COST: float = float(os.getenv("PLAN_MONTHLY_COST", "200.0"))
PLAN_DAILY_LIMIT_USD: float = float(os.getenv("PLAN_DAILY_LIMIT_USD", "0"))
PLAN_WEEKLY_LIMIT_USD: float = float(os.getenv("PLAN_WEEKLY_LIMIT_USD", "0"))
PLAN_BLOCK_LIMIT_USD: float = float(os.getenv("PLAN_BLOCK_LIMIT_USD", "0"))

# Session evaluation
EVAL_ENABLED: bool = os.getenv("EVAL_ENABLED", "1").lower() in ("1", "true", "yes")

# Recent-session history injected into every system prompt.
# SESSION_HISTORY_RANKING="relevance" keeps the entries most related to the
# current prompt; "recency" selects newest-first instead, and exists as a
# no-deploy revert switch if relevance ranking ever hides something useful.
SESSION_HISTORY_MAX: int = int(os.getenv("SESSION_HISTORY_MAX", "6"))
SESSION_HISTORY_RANKING: str = os.getenv("SESSION_HISTORY_RANKING", "relevance").strip().lower()
if SESSION_HISTORY_RANKING not in ("relevance", "recency"):
    # A typo must not silently disable ranking — that is a behaviour change
    # nobody asked for and nothing would report. Warn and use the default.
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "Unknown SESSION_HISTORY_RANKING=%r — using 'relevance'",
        SESSION_HISTORY_RANKING,
    )
    SESSION_HISTORY_RANKING = "relevance"
# Backstop on the rendered block. Trims whole entries, never mid-word — the
# real constraint is context cost, not command-line length (the system prompt
# goes to the CLI via --append-system-prompt-file, not as an argv string).
SESSION_HISTORY_MAX_CHARS: int = int(os.getenv("SESSION_HISTORY_MAX_CHARS", "6000"))

# Outlook integration (optional — Windows only, requires pywin32 + Outlook installed)
OUTLOOK_ENABLED: bool = os.getenv("OUTLOOK_ENABLED", "").lower() in ("1", "true", "yes")

# Auto-update: secondary devices auto-pull and reboot when code changes
AUTO_UPDATE: bool = os.getenv("AUTO_UPDATE", "").lower() in ("1", "true", "yes")
AUTO_UPDATE_INTERVAL_SECS: int = int(os.getenv("AUTO_UPDATE_INTERVAL_SECS", "300"))
AUTO_UPDATE_BRANCH: str | None = os.getenv("AUTO_UPDATE_BRANCH")

# Log triage: periodic `claude -p` scan of bot.log, posts anomalies to The Ark.
LOG_TRIAGE_ENABLED: bool = os.getenv("LOG_TRIAGE_ENABLED", "").lower() in ("1", "true", "yes")
LOG_TRIAGE_INTERVAL_SECS: int = int(os.getenv("LOG_TRIAGE_INTERVAL_SECS", "21600"))  # 6h
LOG_TRIAGE_MAX_LINES: int = int(os.getenv("LOG_TRIAGE_MAX_LINES", "500"))
LOG_TRIAGE_TIMEOUT_SECS: int = int(os.getenv("LOG_TRIAGE_TIMEOUT_SECS", "60"))
LOG_TRIAGE_MODEL: str = os.getenv("LOG_TRIAGE_MODEL", "claude-haiku-4-5-20251001")

# Data directory
DATA_DIR: Path = Path(
    _paths.translate(os.getenv("DATA_DIR")) or str(_PROJECT_ROOT / "data")
).resolve()
RESULTS_DIR: Path = DATA_DIR / "results"
LOGS_DIR: Path = DATA_DIR / "logs"
STATE_FILE: Path = DATA_DIR / "state.json"
LOG_FILE: Path = LOGS_DIR / "bot.log"

# Base directory for new repos (optional — falls back to sibling of active repo)
REPOS_BASE_DIR: Path | None = (
    Path(_paths.translate(v)).resolve() if (v := os.getenv("REPOS_BASE_DIR")) else None
)

# Workspace roots for repo wizard directory browser (comma-separated paths)
# Falls back to parent directories of registered repos when empty
WORKSPACE_ROOTS: str = ",".join(
    _paths.translate(p.strip())
    for p in os.getenv("WORKSPACE_ROOTS", "").split(",")
    if p.strip()
)

if REPOS_BASE_DIR and not REPOS_BASE_DIR.is_dir():
    import warnings
    warnings.warn(f"REPOS_BASE_DIR does not exist: {REPOS_BASE_DIR}")
    REPOS_BASE_DIR = None

# Ensure data dirs exist
REBOOT_MSG_FILE: Path = DATA_DIR / "reboot_message.json"
REBOOT_REQUEST_FILE: Path = DATA_DIR / "reboot_request.json"
# Informational record of a deferred reboot — written fresh on each defer,
# auto-promoted to REBOOT_REQUEST_FILE at the next idle session-end, dropped
# after REBOOT_DEFERRED_TTL_SECS staleness threshold. Latest-wins on stacked
# defers (intentional: each defer is a fresh intent that supersedes the prior).
REBOOT_REQUEST_DEFERRED_FILE: Path = DATA_DIR / "reboot_request.deferred.json"
DRAIN_QUEUE_FILE: Path = DATA_DIR / "drain_queue.json"
PENDING_PROMPTS_FILE: Path = DATA_DIR / "pending_prompts.json"
USAGE_QUEUE_FILE: Path = DATA_DIR / "usage_queue.json"
PENDING_IMAGES_DIR: Path = DATA_DIR / "pending_images"
# Uploads are kept for a retention window, NOT deleted when the turn that
# received them returns.  The path we hand the session lives on in its
# transcript, so a steer, a retry, or a plain "look at that screenshot again"
# turn can read the file long after the receiving message frame is gone.
# Frame-scoped deletion could never be right for a resumable session: it wiped
# an upload one second before the steered run that referenced it started.
# The window is measured from the last time a run was *given* the path, not
# from when the file arrived — see refresh_image_retention.
PENDING_IMAGES_TTL_HOURS: float = float(os.getenv("PENDING_IMAGES_TTL_HOURS", "48"))
# Disk guard.  Retention alone is unbounded if someone dumps a burst of large
# uploads, so the sweep also evicts oldest-first back under this cap...
PENDING_IMAGES_MAX_BYTES: int = int(
    os.getenv("PENDING_IMAGES_MAX_BYTES", str(500 * 1024 * 1024))
)
# ...but never touches a file this young, whatever the cap OR the TTL says —
# that floor is what keeps an in-flight run's image out of the reaper's hands,
# and it outranks both rules so a mistyped TTL can't revoke it.
PENDING_IMAGES_MIN_AGE_SECS: int = int(
    os.getenv("PENDING_IMAGES_MIN_AGE_SECS", "900")
)
PENDING_IMAGES_SWEEP_SECS: int = int(os.getenv("PENDING_IMAGES_SWEEP_SECS", "3600"))
# Self-wake: an inner session writes data/wakes/<instance_id>.json to have the
# bot re-invoke it in the same thread after a delay (see WAKE_GUIDANCE). Per
# instance-id so concurrent sessions never clobber a shared file, and absolute
# so it works regardless of the session's cwd (worktree builds run elsewhere).
WAKE_DIR: Path = DATA_DIR / "wakes"
# Self-wake delay clamp and runaway cap.
WAKE_MIN_DELAY_SECS: int = 30
WAKE_MAX_DELAY_SECS: int = 86400          # 24h
MAX_CONSEC_WAKES: int = 25                # stop a never-completing poll loop
# Default delay for a /wake directive that carries a prompt but omits (or
# typos) delay= — arm with something sane instead of dropping the request.
#
# NOTE: heuristics used to SCHEDULE wakes off this value too — WAKE_PROMISE_RE
# (watch-verb near job-noun, "I'll monitor the build") and an auto-armed
# re-check when WAKE_CLAIM_RE matched. Both kept firing phantom 3-min wakes on
# prose that merely DISCUSSED builds/backtests or this very feature, so
# heuristic scheduling is gone entirely: an explicit parsed [BOT_CMD: /wake]
# directive is the only thing that arms a wake. WAKE_CLAIM_RE below survives
# as a notice-only check.
WAKE_FALLBACK_DELAY_SECS: int = 180

# --- Watches: an event-triggered self-wake ----------------------------------
# A wake fires on a clock, so a session facing a 40-minute job has to GUESS a
# delay. A watch fires on the job itself: the bot polls the process (or a
# done-marker in its log) on the scheduler's existing 30s tick and, the moment
# it finishes, calls add_wake with next_run_at=now. So a watch is not a second
# resume path — it is a wake whose trigger is an event, and everything
# downstream of a wake (runaway cap, busy re-arm, _replay_to_thread, the
# unattended-turn protocol) is inherited unchanged.
WATCH_DEFAULT_TIMEOUT_SECS: int = int(os.getenv("WATCH_DEFAULT_TIMEOUT_SECS", "21600"))
WATCH_MAX_TIMEOUT_SECS: int = 86400          # 24h — matches WAKE_MAX_DELAY_SECS
# Heartbeat cadence. The heartbeat EDITS one message rather than posting, so
# the floor exists to stay well clear of Discord's per-message edit limits
# however small a session asks for.
WATCH_HEARTBEAT_SECS: int = int(os.getenv("WATCH_HEARTBEAT_SECS", "120"))
WATCH_MIN_HEARTBEAT_SECS: int = 60
# Bytes of the watched log read per poll — enough to hold the last progress
# line and the done marker, small enough that polling a multi-GB log is cheap.
WATCH_LOG_TAIL_BYTES: int = 8192
# Global ceiling. One watch per thread is enforced by add_watch; this stops a
# fleet of threads from turning the 30s tick into a filesystem sweep.
WATCH_MAX_ACTIVE: int = int(os.getenv("WATCH_MAX_ACTIVE", "40"))
# Notice-only contradiction check: a turn that ASSERTS it armed a self-wake
# ("Self-wake queued (~4 min)") while no directive parsed is narration of the
# action without the action — the user gets a heads-up that nothing is
# scheduled (never an auto-armed wake). Requires a scheduling VERB adjacent to
# "wake" so a meta-explanation ("self-wake lets you continue after a deploy")
# doesn't trip it; code spans and quoted phrases are stripped before matching
# (lifecycle._CLAIM_META_RE) so quoting a trigger phrase doesn't either.
WAKE_CLAIM_RE = re.compile(
    r"(self.?wake|wake[\s-]?file)s?\s+"
    r"(queued|scheduled|armed|set|written|wrote|created|in\s+place)"
    r"|(queued|scheduled|armed|wrote|written|set|created)\s+"
    r"(a\s+|the\s+|one\s+)?(self.?)?wake",
    re.IGNORECASE,
)

# --- Unattended-turn end-of-turn protocol -----------------------------------
# A system-initiated turn (cooldown retry, self-wake fire) runs with NOBODY
# watching. If it ends dangling — a "next I'll..." plan with no action — the
# thread silently dies (the q-12314 cooldown-retry dead-end that motivated this).
# So every unattended turn must end with an EXPLICIT marker: a [BOT_CMD: /wake]
# directive (work still pending) OR TURN_COMPLETE_SENTINEL (done / needs the
# user). Neither ⇒ lifecycle.check_wake_request auto-nudges the session once
# (capped at MAX_CONSEC_NUDGES) to force one of the two. This is a deterministic
# parse of an explicit marker — NOT the phrase-sniffing heuristic that was
# removed for firing phantom wakes on prose that merely discussed the feature.
TURN_COMPLETE_SENTINEL = "[TURN_COMPLETE]"
MAX_CONSEC_NUDGES: int = 2

# --- Orchestrator join (parent waits for its whole spawn wave) ---------------
# A parent that fans out N children used to get N separate callbacks, each with
# its own Resume button, so the human was the join point. The wave is now
# reported ONCE, when every child has settled. "Settled" is derived per child
# from its own thread/instance records (terminal status and not parked on a
# question) — never from a stored roster that a reboot could desynchronise.
#
# A child that dies without ever finalizing (crash, kill during a reboot) would
# otherwise hold the wave open forever, so the autonomy loop releases a partial
# wave once this many minutes have passed since the parent dispatched it. The
# release names the children that never came back. 0 disables the partial
# release — a wave then waits for its children however long they take. Either
# way a wave nobody can act on any more (12h+, including every wave recorded
# before this join existed) is retired silently rather than reported.
ORCH_WAVE_TIMEOUT_MIN: int = int(os.getenv("ORCH_WAVE_TIMEOUT_MIN", "45"))
# Resume the parent automatically once its wave closes, instead of waiting for
# a "Resume parent" tap. Safe against runaway because the resumed turn cannot
# spawn past _MAX_SPAWN_WAVES (bot/engine/commands.py) — a callback resume does
# not reset that counter. A PARTIAL (timed-out) release never auto-resumes:
# deciding whether to proceed without a straggler is a human call.
ORCH_AUTO_RESUME: bool = os.getenv("ORCH_AUTO_RESUME", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

UNATTENDED_TURN_PROTOCOL = """\

--- Unattended Turn — You MUST Signal How It Ends ---
No user is watching this turn (the system fired it, nobody typed it). When it \
ends your process EXITS and NOTHING resumes this thread unless YOU arrange it \
now. So you must end this response with exactly ONE of:

1. Work still pending (a job to poll, more steps to run after a wait) — schedule \
a self-wake with a [BOT_CMD: /wake] directive (see the self-wake guidance). That \
is what re-invokes you here to continue.
2. Work finished, or you genuinely need the user before going further — put this \
marker on its own line at the TOP LEVEL of your message (NOT inside a ``` code \
block or quoted with >):
[TURN_COMPLETE]

Do NOT end with a "next I'll..." plan and no action — that strands the thread \
with nothing to resume it. Either do the work now, schedule a wake, or emit \
[TURN_COMPLETE]. If you catch yourself about to describe what you'd do next, do \
it THIS turn instead."""
DATA_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
PENDING_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
WAKE_DIR.mkdir(parents=True, exist_ok=True)

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
IMPORTANT: The user is in a chat app (Discord). They see ONLY your final text responses. They CANNOT see tool calls, file contents, diffs, command output, or intermediate steps. Your text output is their ENTIRE window into what happened.

Always address the user directly — your audience is a person on their phone, not your tools or your own reasoning.

You must narrate your work:
- If you read a file → summarize what you found
- If you edited code → show what changed (short before/after or description of the change)
- If you ran a command → report success/failure and key output
- If something errored → include the actual error message
- If you searched code → share what you found or didn't find
- If you diagnosed/tested something → explain what you checked, what the result was, and what fixed it
- If something now works → explain WHY it works (what was wrong before, what changed)

Bad: "I've updated the function." (user has no idea what changed)
Good: "Changed `get_user()` to accept an optional `role` param — it now filters by role when provided, defaulting to the old behavior."

Bad: "All good — token working now." (user has no idea what was wrong or what you tested)
Good: "Tested the new token against GitLab's API — push and MR creation both succeed now. The old token was missing the `write_repository` scope."

Think of it like pair programming over text — your partner can't see your screen.

Plain-language storytelling (CRITICAL):
The user does NOT have the code open and never will. Function names, class names,
variable names, and internal identifiers mean NOTHING to them — a report built out
of `SomeMethodName` -> `SomeOtherName` is unreadable noise.

- Describe every component by what it DOES, not what it is CALLED.
  Bad:  "`TrackBatchCloidsAsync` records them to `PendingExitWatchCloids`"
  Good: "the bot writes the order IDs to its watch-list, so it knows to wake up
         when one of those orders fills"
- Tell findings as a story: what was supposed to happen, what you checked, what
  actually happened, and what that means for the user.
- Use an exact identifier ONLY when the user needs the literal string to act —
  a command to run, a setting to flip, an error message to recognize — and put it
  in parentheses after the plain-language description, never instead of it.
- Test names, file paths, and log excerpts follow the same rule: lead with meaning,
  keep the literal only if it's actionable.
- If you catch yourself writing a numbered list where every item leads with a code
  identifier, stop and rewrite it as prose about behavior.
- Exception: if the user explicitly asks where something lives in the code, give
  real names and file paths.

- If you used subagents (Agent tool) to research → present ALL findings in your response.
  The user can't see agent results — if you don't write the findings out, they're invisible.
- Never reference findings without listing them. If you mention a count ("4 quick wins",
  "3 issues"), every item MUST appear in your response with a brief description.
- Your text output IS the deliverable. There is no other channel for the user to see results.
- CRITICAL: If you write text between tool calls, the user MAY NOT see it. Never say
  "as shown above" or "the analysis I shared earlier" — always include the full content
  in your final response. If it's important, it must be in your last message.
"""

HONESTY_CONSTRAINT = """
--- Honesty & Verification ---
- When providing URLs, links, prices, product specs, or any externally-sourced data: verify with WebFetch before presenting. If you cannot verify, explicitly say "I haven't verified this" — never present unverified information as confirmed.
- Only claim work is "done" or "fixed" for things you actually verified with a tool call. For bulk operations, report verified count vs total: "Updated 70 cells — verified 5 are working, the rest I couldn't confirm."
- If you hit a limitation (can't verify data, can't access a service, can't confirm results), say so immediately. Don't paper over it with confident language.
- Never claim to have "checked" or "verified" something you didn't actually test with a tool call.
- After triggering an action (reboot, deploy, service restart), do NOT assume success from indirect evidence ("you're still talking to me"). Check actual indicators: logs, process status, file state changes.
"""

EFFORT_FRAMING = """
--- Effort Framing ---
You are an LLM. Code changes that would take a human developer days or weeks take you minutes. Do NOT frame tradeoffs in human-developer time.

- NEVER say "this would take 2-3 weeks of work" or "the proper fix is a multi-day refactor". Calendar time is irrelevant — you are the one doing the work, and you are fast.
- NEVER offer "quick hack now vs proper fix later" purely on time grounds. If the proper fix is the right answer, just do it. The user pays in tokens and minutes, not weeks.
- Legitimate reasons to prefer a smaller change: regression risk, blast radius (how many files/systems affected), reversibility, test coverage gaps, load-bearing code you don't fully understand. NOT "more lines of code".
- If a change really is too big for one session, say so in terms of *scope and risk* ("touches 40 files across 3 subsystems, want tests per subsystem first"), not wall-clock time.
- More lines of code is free for you. Don't propose a worse solution to save typing.
"""

BOT_CONTEXT = """

--- Bot Context ---
You are running inside a bot that manages Claude Code instances. The user is chatting from their phone. You can do normal Claude Code work (read files, search code, run commands, etc.) but the bot also has these capabilities the user can invoke directly:

IMPORTANT — Scope awareness:
The commands, capabilities, and reboot instructions below all refer to the MANAGEMENT BOT you are running inside — NOT the project you are currently working on. If the user's project has its own bot, service, or process that needs managing, figure that out from the project's own code. Do not apply management bot rules (like reboot_request.json or "don't kill the process") to the target project.

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
- /model <name> — model for this thread (`/model default` clears it)
- /context set <text> — pin context to all prompts
- /repo add|remove|create|switch|list — manage repos
- /repo create <name> [path] [--github] [--public] — create new repo (git init + register)
- /repo remove <name> — unregister a repo (does not delete files)
- /provider claude|cursor — switch CLI provider
- /alias set|list|delete — saved command shortcuts
- /new — start a fresh conversation
- /cost — spending breakdown
- /evals — recurring session-quality flags and which prompt block owns each
- /status — health dashboard

If the user asks to do something the bot handles (like scheduling, switching repos, etc.), guide them to the right command rather than saying you can't do it.

Natural language repo management:
When the user asks you to register, create, or switch repos conversationally (e.g., "this is my project", "hook up my repo"), determine the correct command and output it on its own line in this exact format:
[BOT_CMD: /repo add myapp /path/to/myapp]
The management bot will detect and execute this automatically. Only output BOT_CMD when you're confident about the name and path. Confirm with the user first if details are unclear.

If you cannot perform an action because of your current mode (e.g. Explore mode blocks file writes), tell the user exactly what they need: "This needs Build mode — tap the Mode button below or type /mode build." Don't just say you can't — tell them how to fix it.

Sharing an image into the chat:
You CAN put a picture in front of the user. Emit this on its own line and the bot uploads the file into the thread, above your response:

[BOT_CMD: /image path="docs/architecture.png" caption="The auth flow"]

Use it whenever a picture beats prose: an image or diagram that already lives in the repo, a screenshot you took while running the app, or a chart you generated during a build. If the user asks to SEE something, this is how — don't tell them where the file is and make them go open it.

Rules:
- `path` — relative to the repo (or the build worktree) you're working in, or absolute. Bare form also works: [BOT_CMD: /image docs/arch.png]
- `caption` (optional) — one short line describing the picture.
- Formats: .png .jpg .jpeg .gif .webp .bmp. SVG is refused (Discord won't render it inline).
- Up to 4 images per response, 8 MB each. The file must live under the repo you're working in, its build worktree, or the bot's data dir — anything else is refused for safety, so write generated images inside the repo rather than to a temp folder.
- Generating an image on the fly (a rendered chart, a screenshot) requires Build mode; sharing one that already exists works in any mode.
- Don't narrate the directive ("I'm emitting an image command") — just say what the picture shows. The directive line is stripped from what the user reads.
"""

# Depth-0 spawn capability. Appended only when the current thread is NOT
# itself a spawned thread (instance.spawn_depth == 0). A spawned (depth>=1)
# thread gets SPAWN_CAPPED_NOTICE instead, because the recursion cap in
# bot/engine/commands.py refuses any /spawn it would emit.
SPAWN_CONTEXT = """
Spawning a fresh session with a generated prompt:
When the user asks you to "start a new session", "spawn a new thread", or "kick off another task" — and you have written or can write the prompt that should run there — output a /spawn directive at the END of your response. This hands the generated prompt off to a brand-new forum thread instead of forcing the user to copy-paste between threads.

Format (BOTH the directive and the body block are required, in this order, adjacent):

[BOT_CMD: /spawn repo=<repo_name> title="Short Title" mode=build]
~~~spawn
The full prompt body that will be sent into the new session as the first user message. This can be many lines, include code fences, file paths, whatever the new session needs to get going.
~~~

Rules:
- `repo` (required): name of a registered repo. Use `/repo list` if unsure — never guess.
- `title` (required): short thread title, ≤80 chars, double-quoted.
- `mode` (optional, default "build"): explore | plan | build.
- `effort` (optional): low | medium | high | max.
- The fenced body MUST use tilde fences (`~~~spawn` / `~~~`), not backticks — this avoids colliding with backtick code blocks in the prompt.
- Up to 5 /spawn directives may run per response. To fan out parallel subsessions, emit multiple directive+body pairs back-to-back — each directive IMMEDIATELY followed by its own ~~~spawn body, then the next directive. A directive without its own body block is skipped; directives beyond 5 are ignored.
- One short sentence in your response telling the user what's happening is enough; the substance lives in the ~~~spawn blocks. Don't restate the prompt bodies in prose — that's just noise.
- The bot will reject a directive if: this thread was itself spawned (depth-1 cap), autopilot is running/paused on this thread, the repo is unknown, the body exceeds 32 KiB, or this thread has already spawned 12 children since your last real user message (run cap).
"""

# Answering your own children. Appended alongside SPAWN_CONTEXT (depth-0 only):
# a spawned thread has no children of its own, and the engine refuses a /reply
# whose target it did not spawn, so telling a depth-1 thread about this would
# only produce directives that get rejected.
REPLY_CONTEXT = """
Answering a child session you spawned:
When a child you spawned stops and asks a question, the bot tells you — not the user. You wrote that child's brief, so you are usually the one who can answer it. Reply to it directly instead of handing the question back to the user.

Format (directive on its own line, immediately followed by its ~~~reply body):

[BOT_CMD: /reply thread=1536292012725248061]
~~~reply
Your answer to the child, written as a message to it. Same shape as any instruction you would have put in its original prompt.
~~~

Rules:
- `thread` (required): the child's thread id, exactly as given to you in the "waiting on an answer" notice.
- You may only reply to threads YOU spawned. Any other id is refused.
- Tilde fences (`~~~reply` / `~~~`), never backticks — same reason as /spawn.
- Up to 5 /reply directives per response, each with its own adjacent body.
- Hand the question to the user ONLY when the answer genuinely depends on something only they know (a preference, a credential, a business decision). A question about scope, approach, or which file to touch is yours to answer.
- Your spawn wave stays open until that child finishes, so answering it is what lets the wave close.
"""

# Spawn-wave join. Appended alongside SPAWN_CONTEXT so a parent knows the
# report it will be woken with is a set of file paths, not chat text, and that
# it is expected to go read them.
SPAWN_JOIN_CONTEXT = """
How your spawned children report back:
- You are NOT told about children one at a time. The bot waits until every child in the wave has finished, then wakes you once with all of them.
- That wake-up lists each child's status and the absolute path to its FULL report file. The inline excerpt is only the report's opening lines — read the files before drawing conclusions or summarizing for the user.
- A child that stopped to ask a question does not close the wave. You get a separate notice for it and are expected to answer it with /reply.
- If a child never came back at all, the wave is released without it and says so. Report that gap plainly rather than filling it in.
"""

# Depth-1 variant. Appended when instance.spawn_depth >= 1 — this thread was
# itself spawned, so the recursion cap means any /spawn it emits is refused.
# Tell it that plainly and give a copy-ready handoff format instead.
SPAWN_CAPPED_NOTICE = """
Spawning (DISABLED here): this thread was itself spawned, so the depth-1 cap means any /spawn directive you emit WILL be refused. Do NOT output a [BOT_CMD: /spawn ...] block.
If follow-up work needs a fresh session, end your response with a handoff block the user (or the depth-0 parent thread) can launch directly:

Next session →
repo: <repo_name>
title: <short title>
prompt: <ready-to-run prompt body>
"""

# Chain handoff — appended for every thread (independent of spawn depth). Lets
# the session agent recognize approval mid-conversation and hand the plan it
# already discussed into the automated build→ship chain, instead of the user
# tapping Build/Ship or typing "merge".
CHAIN_CONTEXT = """
Handing work off to the build→ship chain (KEY — this is how you ship):
When the user approves scoped work you've been discussing — "go", "ship it", "build it", "do it", "build and verify", "yes go ahead" — do NOT start building inline in this chat. Instead, END your turn with a /chain directive that carries the plan you already worked out from the conversation. The bot then runs the whole pipeline in this thread: build (in an isolated worktree) → review code → verify → (release) → (merge), pausing only if something fails or needs your input.

Format (directive on its own line, immediately followed by its ~~~plan body):

[BOT_CMD: /chain preset=ship]
~~~plan
The full implementation plan, written from our conversation: every file to change and what the change is, the approach, and anything to verify afterward. This is injected as the build's brief, so make it concrete and self-contained.
~~~

Presets (pick by what the user asked for):
- `ship` — build → review → verify → release → merge, closes the thread. Use for "ship it" / "go" / a plain approval when the repo auto-ships.
- `hold` — build → review → verify → release, then STOPS before merge and leaves Merge/Discard for you to decide. Use when the user wants to inspect before it lands. (No auto-merge here even on an auto-ship repo — `hold` is an explicit "don't merge yet".)
- `verify` — build → review → verify only, then stops with the branch open. Use for "build and verify" / a build+verify loop.
- Omit `preset=` to let the repo's own autonomy policy choose.

Rules:
- Tilde fences (`~~~plan` / `~~~`), never backticks — same reason as /spawn.
- One /chain per response. It's refused if a chain is already running on this thread.
- Only emit it once the user has actually approved. While still planning or asking questions, just talk — no directive.
- One short sentence telling the user you're kicking off the chain is enough; the plan lives in the ~~~plan block, not in prose.
"""

# Tail of the bot context — appended after the depth-correct spawn block so
# reboot/wake guidance keeps its original position in the system prompt.
BOT_CONTEXT_TAIL = """
Rebooting the management bot:
- Do not kill the bot process directly (taskkill, kill, etc.) — prefer the reboot_request.json approach as it waits for active queries to finish and resumes cleanly.
- If the user asks you to reboot, do it immediately — don't question whether it's necessary.
- You can reboot the bot yourself when needed (e.g. to apply code changes you just made). Write a JSON file to data/reboot_request.json:
  {"message": "why you're rebooting", "resume_prompt": "what you want to do when you wake back up"}
  The bot picks this up after your response completes, waits for other queries to finish, reboots, and then sends resume_prompt back to this thread — resuming your session so you continue seamlessly.
- Bootstrap case: If you write reboot_request.json and nothing happens, the bot may be running code from before the reboot-watcher feature was added. Tell the user to restart the process manually once — after that first manual restart, future reboots will work through the JSON file.
- If the reboot file isn't working AND the user explicitly asks you to kill the process, you may do so — but warn them it will interrupt any active queries.
- Use this naturally as part of your workflow. For example, if you edit bot code and need to apply it:
  1. Make the code changes
  2. Tell the user what you did and that you're rebooting to apply them
  3. Write the reboot file with a resume_prompt that has full context: what you changed, what to verify, what to do next
  4. The bot restarts, you wake up with that context, and you continue — check logs, verify the fix, report back
- The resume_prompt should read like your own notes-to-self. Include enough context to pick up exactly where you left off.
- IMPORTANT: You ARE the bot process. If you run taskkill/kill, you kill YOURSELF mid-response and the user sees "interrupted by bot restart" with no result. Only do this as a last resort when the user explicitly asks.

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

Continuing after your turn (CRITICAL — read before you promise to "watch" anything):
- Your turn ENDS when you send your final message. The process EXITS. You do NOT keep running, polling, watching, or waiting afterward. NOTHING resumes you — not the deploy system, not CI, not a webhook, not a "notification". There is exactly ONE way to continue after an external event: YOU schedule a self-wake (see the self-wake section below, present for non-worktree sessions).
- Therefore these are ALL false promises and are BANNED unless you have JUST scheduled a self-wake this turn — never say any of them: "I'm polling in the background", "I'll report back when it's done", "I'll keep checking", "I'll monitor X and update you", "I'll wait for the deploy/build/CI", "I'll get notified when it lands", "I'll trigger the next step once X finishes". Saying any of these without a scheduled self-wake means the user waits forever for something that will never happen.
- If you intend to continue after a long external job (backtest, deploy, build, CI — anything you are "waiting" on), that intent IS your cue to schedule a self-wake NOW, before you finish (see the self-wake section below for how). If that section is absent (worktree build), you cannot self-wake — finish and explicitly tell the user to reply or tap a button to continue.
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

# Universal working context — injected into EVERY session regardless of repo.
# Covers the user's workflow, Discord UI, branch model, and design principles.
WORKING_CONTEXT = """

--- Working Context ---
The user manages development from Discord on their phone, running 10+ sessions in parallel across multiple repos.

Standard workflow: Plan → Review Plan (auto-loops) → Build → Review Code → Verify → Commit → Done.
"Verify" starts the app and tests the feature through diagnostic endpoints — not just linting or type checks.
"Autopilot" automates this full loop. Individual steps are also available as buttons below each response.
When proposing changes, always design to fit this workflow. All settings are per-thread — never assume single-session.

The user sees: forum sidebar (thread names truncated ~40 chars, tags like active/completed/failed), thinking/result embeds, and contextual workflow buttons they tap to advance. Tags are the real-time status indicator (thread name edits are rate-limited).

Build tasks use git worktrees for isolation — each build gets its own directory. After completion, user taps Merge or Discard. Autopilot auto-merges. The main repo always stays on master.

Deploy integration: To connect a reboot/deploy sequence for this repo, create .claude/deploy.json with {"command": "your deploy command", "label": "Deploy"}. After merge, the bot detects it and adds a Deploy button to the repo's control room (requires user approval before first use).

Design for: mobile-first conciseness, maximum throughput, at-a-glance visibility, per-thread state over globals.

--- Discord Formatting ---
Discord does NOT support these markdown features — never use them:
- Pipe tables (| col | col |) — render as raw text with visible pipes
- Nested/indented bullet lists — indentation is ignored, everything flattens
- Image syntax (![alt](url)) — not rendered
- Horizontal rules (---) — render as empty space
For structured data use: bullet lists with **bold** and `inline code`, or padded monospace inside ```code blocks```.
"""

# Per-step behavioral guidance — tells Claude what its role is in the current workflow step.
# Keys MUST match InstanceOrigin enum values in bot/claude/types.py.
WORKFLOW_GUIDANCE: dict[str, str] = {
    "direct": (
        "You're responding to a direct user message. Answer their question, "
        "then they'll choose the next step via workflow buttons.\n\n"
        "IMPORTANT: If the message does not ask you to investigate, change, or "
        "verify something in the codebase, answer directly from conversation "
        "context without using tools. Opinions, follow-ups, confirmations, "
        "explanations, and 'what about X?' messages do not need file reads or "
        "commands — just reply.\n\n"
        "Do NOT end a reply by asking 'want me to dig into it?', 'should I "
        "investigate?', or 'want me to look into that?'. If the next step is "
        "read-only — reading files, searching, running diagnostics — just do it "
        "and report what you found. The user almost always says yes, so the "
        "question only adds a round-trip. Still ask first ONLY when the next step "
        "is destructive, outward-facing, or expensive (deploys, deletes, "
        "force-push, sending external messages, long builds)."
    ),
    "plan": (
        "You're creating an implementation plan. Research thoroughly, do NOT implement. "
        "The user will review your plan and then click Build. "
        "If the repo has no .claude/test.json or no diagnostic endpoints yet, "
        "the plan may note that the build step will scaffold them as a prerequisite."
    ),
    "build": (
        "You're implementing a plan that was already reviewed. Follow the plan above — "
        "don't re-plan or redesign. Focus on clean execution.\n\n"
        "BEFORE you say the build is complete or describe 'what changed', you MUST commit:\n"
        "1. Run `git status` — if the working tree has any changes, `git add` and `git commit` them.\n"
        "2. Run `git log -1 --oneline` and confirm your new commit is on the current branch.\n"
        "3. Never claim 'committed locally' or 'implementation complete' unless step 2 shows your commit.\n"
        "4. Never start tests or builds with run_in_background and then end your turn — "
        "background processes die the moment your turn ends in this headless environment "
        "(nothing notifies you, nothing resumes you). Run them in the foreground and wait.\n"
        "If the chain detects zero new commits when you finish, it will halt and your work "
        "will be rolled into a WIP commit you'll have to recover by hand — so just commit before saying done."
    ),
    "review_plan": (
        "You're reviewing a plan for gaps, risks, and improvements. Be critical. "
        "Format revisions in the structured review format."
    ),
    "apply_revisions": (
        "Apply the Critical/High priority revisions from the review above to the plan. "
        "Output the revised plan."
    ),
    "review_code": (
        "You're reviewing code with fresh eyes. Look for bugs, edge cases, and missed "
        "requirements. If you find issues, fix them directly."
    ),
    "sensor_fix": (
        "Deterministic checks (compiler/linter/type checker) flagged errors in the "
        "build you just finished. Fix the root causes — never suppress rules, add "
        "ignore comments, or loosen tool configs to silence them. The same checks "
        "re-run right after your turn, so commit your fixes before finishing."
    ),
    "verify": (
        "You're verifying that the code just built actually works. "
        "Do NOT just read the code, run linters, or check types — that's not verification. "
        "START the app, PERFORM actions through its endpoints, and CHECK the results. "
        "You must interact with the running application like a user would.\n\n"
        "If .claude/test.json has auth config, use it to authenticate your requests. "
        "If the app lacks diagnostic endpoints, note that as a gap in your report "
        "but still try to verify by other means (build, start, check logs, curl health)."
    ),
    "commit": (
        "Commit all changes with a clear message. Update CHANGELOG.md under [Unreleased]. "
        "Don't add features or refactor."
    ),
    "done": (
        "Wrap up: commit changes, update changelog, cut a release if warranted. "
        "Be concise — the user is about to close this thread."
    ),
    "retry": "Re-attempt the previous task that failed. Check what went wrong first.",
    "bg": (
        "You're running as a background build task. Present ALL findings, recommendations, "
        "and results in your response — the user will only see your final text output. "
        "Be thorough and specific. List every item you discover."
    ),
}

# Provider's base directory name (e.g. ".claude", ".cursor")
PROVIDER_DIR_NAME: str = _PROVIDER_CFG.projects_dir_name


def primary_account_dir() -> Path | None:
    """The account config dir to pin ``CLAUDE_CONFIG_DIR`` to, or None.

    ``CLAUDE_ACCOUNTS`` is ordered and the first entry is the default account.
    Anything spawning the CLI outside the normal rotation (title generation,
    one-shot helpers) has to pin to it explicitly: left alone the CLI reads
    ``$HOME/.claude``, which on Linux is frequently not one of the configured
    accounts and may not be signed in at all.

    None means "nothing to pin" — a non-``claude`` provider, no accounts
    configured, or a malformed first entry.
    """
    if PROVIDER != "claude" or not CLAUDE_ACCOUNTS:
        return None
    try:
        return Path(CLAUDE_ACCOUNTS[0]).expanduser()
    except (OSError, RuntimeError):  # malformed entry — behave as unset
        return None


def claude_projects_dirs() -> list[Path]:
    """Every projects root the CLI may have written session JSONLs to.

    The bot launches the CLI with ``CLAUDE_CONFIG_DIR`` pointed at whichever
    account is active, so sessions land under *that* directory — not under
    ``$HOME``.  On Windows the two coincide (``C:/Users/x`` and
    ``C:/Users/x/.claude``), which is why deriving this from ``Path.home()``
    alone worked there.  On Linux they routinely diverge: the account dirs may
    sit on another filesystem entirely, leaving the home-derived path holding a
    handful of stray sessions while every real one lives elsewhere.

    Ordered most-authoritative first (the accounts, in rotation order), with
    the home-derived path last as a fallback for a single-account or
    non-``claude`` setup.  Deduplicated and filtered to dirs that exist, so
    callers can iterate without guarding.

    If ``CLAUDE_PROJECTS_DIR`` has been reassigned away from its derived value
    — which a sandboxed test harness does to keep its fixtures off real
    session data — that override wins and confines the scan to it alone.
    Some callers here *delete* files, so widening a deliberately narrowed
    scope would be actively destructive.

    That test compares against ``_DERIVED_PROJECTS_DIR``, the value *as last
    derived*, and not against a fresh call to :func:`_default_projects_dir`.
    Recomputing reads it as an override whenever the inputs have moved since —
    and they do move: ``bot/app.py`` prunes ``CLAUDE_ACCOUNTS`` at boot, so a
    stale first entry in ``.env`` (nothing rarer than a machine whose account
    paths just changed) made every root vanish and every session reader go
    blind. Comparing to the snapshot asks the question actually meant here —
    "has someone assigned to this global?" — which no input change can fake.
    """
    if CLAUDE_PROJECTS_DIR != _DERIVED_PROJECTS_DIR:
        return [CLAUDE_PROJECTS_DIR] if CLAUDE_PROJECTS_DIR.is_dir() else []

    roots: list[Path] = []
    if PROVIDER == "claude":
        for acct in CLAUDE_ACCOUNTS:
            try:
                roots.append(Path(acct).expanduser() / "projects")
            except (OSError, RuntimeError):  # malformed entry — skip, don't crash
                continue
    roots.append(Path.home() / PROVIDER_DIR_NAME / "projects")

    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r).lower() if os.name == "nt" else str(r)
        if key in seen:
            continue
        seen.add(key)
        if r.is_dir():
            out.append(r)
    return out


def _default_projects_dir() -> Path:
    """The single projects root to WRITE to / treat as primary.

    First configured account when there is one, else the home-derived path.
    Unlike :func:`claude_projects_dirs` this never filters on existence — it
    has to be a usable target even before the directory has been created.
    """
    acct = primary_account_dir()
    if acct is not None:
        return acct / "projects"
    return Path.home() / PROVIDER_DIR_NAME / "projects"


# Session/plan data directory — the primary account's projects root. Readers
# that scan for existing sessions should use claude_projects_dirs() instead so
# a second account's history isn't invisible.
CLAUDE_PROJECTS_DIR: Path = _default_projects_dir()

# The same value, kept as a snapshot of what the config *derives*. Whenever the
# two differ, something has deliberately overridden the projects root and every
# scan confines itself to it (see claude_projects_dirs). Anything that changes
# an input to the derivation must refresh this alongside CLAUDE_PROJECTS_DIR,
# or an ordinary config change reads as an override — use set_accounts() /
# set_provider() rather than assigning CLAUDE_ACCOUNTS or PROVIDER by hand.
_DERIVED_PROJECTS_DIR: Path = CLAUDE_PROJECTS_DIR


def set_accounts(accounts: list[str]) -> None:
    """Replace the account list and re-derive the paths that follow from it.

    ``bot/app.py`` prunes entries whose directory is missing, which moves the
    primary account. Without re-deriving, the primary projects root and the
    ``CLAUDE_CONFIG_DIR`` pin both stay aimed at the dropped entry.

    An override of ``CLAUDE_PROJECTS_DIR`` that is already in effect survives —
    a test sandbox must not be silently un-sandboxed by a config update.
    """
    global CLAUDE_ACCOUNTS, CLAUDE_PROJECTS_DIR, _DERIVED_PROJECTS_DIR

    overridden = CLAUDE_PROJECTS_DIR != _DERIVED_PROJECTS_DIR
    CLAUDE_ACCOUNTS = list(accounts)
    _DERIVED_PROJECTS_DIR = _default_projects_dir()
    if not overridden:
        CLAUDE_PROJECTS_DIR = _DERIVED_PROJECTS_DIR


def set_provider(name: str) -> None:
    """Switch the active provider at runtime.

    Atomically reassigns all provider-derived module globals.
    Validates the binary exists on PATH (raises RuntimeError if not found).
    """
    import logging as _logging
    import shutil as _shutil

    global PROVIDER, _PROVIDER_CFG, CLAUDE_BINARY, BRANCH_PREFIX
    global PROVIDER_DIR_NAME, CLAUDE_PROJECTS_DIR, CURSOR_MODEL
    global _DERIVED_PROJECTS_DIR

    new_cfg = _get_provider(name)

    # Resolve binary to full path — critical on Windows where PATH may not
    # include the provider install dir in the inherited subprocess env.
    # Only use CLAUDE_BINARY env override if it matches the target provider
    # (otherwise switching from claude→cursor would still use claude.exe).
    env_binary = os.getenv("CLAUDE_BINARY")
    if env_binary and name == PROVIDER:
        # Keep env override when re-confirming current provider
        binary_name = env_binary
    else:
        binary_name = new_cfg.binary
    resolved = _shutil.which(binary_name)
    if not resolved and sys.platform == "win32" and not binary_name.endswith(".cmd"):
        resolved = _shutil.which(binary_name + ".cmd")
    if not resolved:
        raise RuntimeError(
            f"Binary '{binary_name}' not found on PATH. "
            f"Install the {name} CLI or set CLAUDE_BINARY to the full path."
        )

    overridden = CLAUDE_PROJECTS_DIR != _DERIVED_PROJECTS_DIR

    PROVIDER = name
    _PROVIDER_CFG = new_cfg
    CLAUDE_BINARY = resolved
    BRANCH_PREFIX = os.getenv("BRANCH_PREFIX") or new_cfg.branch_prefix
    PROVIDER_DIR_NAME = new_cfg.projects_dir_name
    _DERIVED_PROJECTS_DIR = _default_projects_dir()
    if not overridden:  # don't un-sandbox a test that set the root explicitly
        CLAUDE_PROJECTS_DIR = _DERIVED_PROJECTS_DIR
    CURSOR_MODEL = os.getenv("CURSOR_MODEL", "auto")
    _logging.getLogger(__name__).info(
        "Provider switched to %s (binary=%s)", name, CLAUDE_BINARY,
    )


# --- Canned prompts for contextual action buttons ---

# Title-generation marker. Used as the literal prefix of the title-gen
# subprocess prompt in bot/discord/titles.py and as the skip marker in
# bot/engine/sessions.py + the startup cleanup. Defined here (rather than in
# titles.py) so the engine layer doesn't have to reach into the discord layer.
# If you change the title prompt wording, update this prefix in lockstep.
TITLE_PROMPT_MARKER = "Generate a 4-6 word title for this coding session"

PLAN_PROMPT_PREFIX = (
    "Create a detailed implementation plan for the following task. "
    "Explore the codebase, understand existing patterns and architecture, "
    "and design your approach. Do NOT implement anything yet — just plan.\n\n"
    "If the work breaks naturally into sequential PRs or phases, emit a "
    "`phase-plan` fenced block at the end. Each phase is a line of the form "
    "`- id: <slug> | title: <title> | gate: mechanical|design|risk` "
    "(optionally followed by `| reason: <one short sentence>`). "
    "Use `mechanical` for refactors/renames/file moves with no behavior change. "
    "Use `design` when human input on the approach should land before starting. "
    "Use `risk` for production-behavior changes (data, auth, payments, infra) "
    "where a human should review before each phase ships. "
    "Omit the block entirely if the task is single-phase.\n\n"
    "Task: "
)

# Prefix injected ahead of build prompts when the chain has a plan/review
# instance available. Defends against session compaction wiping the plan
# from the resumed session's in-memory history (failure mode observed on
# t-3655: build agent reported "the previous session was compacted; what I
# have is a summary of the plan, not the full plan text" and then halted).
# {plan_text} is substituted via .replace() in the call sites — keeps the
# template fail-soft if a future placeholder is added without updating the
# substitution call.
BUILD_PLAN_INJECTION_PREFIX = (
    "## Plan to implement (verbatim)\n\n"
    "The session below was resumed and may have been compacted. "
    "Treat the following block as the source of truth for what to build, "
    "not your in-memory recollection. Implement it exactly.\n\n"
    "{plan_text}\n\n"
    "---\n\n"
)

BUILD_FROM_PLAN_PROMPT = (
    "Now implement the plan above. You have full build permissions."
)

BUILD_FROM_QUERY_PROMPT = (
    "Now implement the above. You have full build permissions."
)

# Finishup nudge — one corrective continuation turn for a build that ended
# without moving HEAD, typically because it started its test suite with
# run_in_background and ended the turn expecting to be re-invoked (t-5592:
# headless -p processes exit with the turn; the background job dies and
# nothing resumes the session). Post-compact-safe and self-contained: the
# parent usually died near the context ceiling, so the prompt re-states the
# worktree, branch, and the parent's own final words instead of relying on
# the resumed session's in-memory history. Placeholders are substituted via
# .replace() at the call site (fail-soft, same convention as
# BUILD_PLAN_INJECTION_PREFIX).
FINISHUP_NUDGE_PROMPT = (
    "--- Build continuation (automatic) ---\n"
    "Your previous turn ended while a job was still running in the background. "
    "Background processes die the moment your turn ends in this environment — "
    "nothing notifies you and nothing resumes you, so that job never finished.\n\n"
    "Working location (verify with `pwd` before doing anything):\n"
    "- Worktree: {worktree}\n"
    "- Branch: {branch}\n\n"
    "Your final message before the turn ended:\n"
    "{summary}\n\n"
    "Finish the build NOW, in this turn:\n"
    "1. Re-run the unfinished work in the FOREGROUND — do NOT use "
    "run_in_background; wait for completion synchronously.\n"
    "2. Fix any failures it surfaces.\n"
    "3. Commit ALL work on the current branch with a descriptive message "
    "(`git add -A`, then `git commit`), and confirm with `git log -1 --oneline`.\n"
    "Do not end your turn until the commit exists."
)

BUILD_PHASE_PROMPT = (
    "You're on Phase {id}: {title}. Implement ONLY this phase from the plan. "
    "Commit it separately with a message prefixed `[{id}] `. "
    "When done, emit a `phase-status` fenced block at the end of your response:\n"
    "```phase-status\n"
    "done: true\n"
    "commit: <sha>\n"
    "```\n"
    "Do not start the next phase — the chain handles that."
)

PLAN_REVIEW_PROMPT = (
    'Review the plan above and propose your best revisions. '
    'If the plan includes a `phase-plan` block, also sanity-check the phase '
    'breakdown: are gate types right (mechanical/design/risk)? Should any '
    'phase be split or merged? Flag issues as revisions like any other.\n\n'
    'Format your response EXACTLY as described below.\n\n'
    'START with a plain summary paragraph (no bold, no bullets). '
    '1-2 sentences: how many revisions, their priorities, general theme. '
    'Example: "Found 5 revisions across architecture and reliability. '
    '2 are high-priority structural changes, 3 are cleanup improvements."\n\n'
    'Then a compact summary list (one bullet per revision):\n'
    '- **Priority** `Tag` — Short title\n\n'
    'Then list each revision using this EXACT format (do NOT deviate):\n\n'
    '### Tag \u2014 Short title\n'
    'Priority \u00b7 Impact: Low/Medium/High\n\n'
    'One or two sentences max describing the change, why it matters, '
    'and any tradeoffs.\n\n'
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

TRIAGE_DEFERRED_PROMPT = (
    'The review above found the following Medium/Low priority revisions '
    'that were not auto-applied:\n\n{deferred_items}\n\n'
    'Evaluate each one. Apply any that are:\n'
    '- Quick wins (minimal effort, clear improvement)\n'
    '- Bug risk reducers\n'
    '- Directly relevant to the plan\'s core goals\n\n'
    'Skip any that are:\n'
    '- Purely cosmetic or stylistic\n'
    '- Scope creep (adding features not in the plan)\n'
    '- Risky refactors that could introduce bugs\n\n'
    'Apply the selected revisions directly to the plan. '
    'Then at the end, add:\n\n'
    '### Triaged\n'
    'List each: "[TAG] Title \u2014 applied (reason)" or '
    '"[TAG] Title \u2014 deferred (reason)".\n\n'
    'End with:\n'
    '```triage-result\n'
    'APPLIED: <count>\n'
    'DEFERRED:\n'
    '- [TAG] Title (Priority)\n'
    '```'
)

CODE_REVIEW_PROMPT = (
    'Now carefully read over all of the new code you just wrote and other '
    'existing code you just modified with "fresh eyes" looking super '
    'carefully for any obvious bugs, errors, problems, issues, confusion, '
    'etc. Carefully fix anything you uncover. Use ultrathink. '
    'Review if this is DRY, scalable, maintainable and modular. '
    'If a CHANGELOG entry or commit message has been written, verify each '
    'bullet corresponds to a real code change in the current diff — flag '
    'and fix phantom claims.'
)

# Appended to CODE_REVIEW_PROMPT only when the repo declares a model-provider
# SDK (see bot/engine/ai_project.py). These are the failure modes that generic
# review misses in LLM-shaped code — most of them are ones we have actually
# shipped, including a simulator whose summary fields reported a closed winning
# trade while the raw records showed the position still open.
AI_PROJECT_REVIEW_LENS = (
    '\n\nThis repo calls an LLM. Also review the diff through these lenses, '
    'and report on any that apply:\n'
    '- Evals: does anything assert on the SHAPE and QUALITY of model output, '
    'or do the tests only prove the call did not throw?\n'
    '- Output contract: is model output parsed into a validated structure, or '
    'regex-scraped out of prose?\n'
    '- Derived-data honesty: are headline/summary/aggregate fields computed '
    'from the raw records, or written independently where they can drift? '
    'Flag any summary field that could report success while the underlying '
    'records disagree.\n'
    '- Failure taxonomy: are refusal, truncation, rate limit, tool error and '
    'timeout distinguished, or all swallowed as one generic exception?\n'
    '- Prompt assembly: is stable content (system prompt, tool definitions, '
    'static context) assembled BEFORE volatile content, so the cached prefix '
    'survives between turns?\n'
    '- Tool surface: scoped per task, or does every call get everything?\n'
    '- Model IDs: pinned, and current?\n'
    'Only raise these where the diff actually touches them — do not pad the '
    'review with lenses that do not apply.'
)

SENSOR_FIX_PROMPT = (
    'Deterministic checks (compiler/linter/type checker) failed after your '
    'build. Fix the ROOT CAUSES of every reported error. Do NOT suppress '
    'rules, add ignore/noqa comments, delete tests, or loosen tool configs '
    'to make the checks pass — the same checks re-run after your fixes, and '
    'a suppressed error counts as a failure. Commit your fixes when done. '
    'The raw tool output follows verbatim:'
)

VERIFY_PROMPT = (
    'You just wrote code. Now verify it actually works by USING the app.\n\n'

    '## Pre-check: Skip verification if it is not needed\n'
    'Before Step 0, check whether verification applies:\n'
    '- If the change does not affect runtime behavior (docs, comments, '
    'formatting, renames, dead code removal, type hints, config-only edits) '
    '— skip.\n'
    '- If the previous build step stated it skipped diagnostic scaffolding '
    'for one of these reasons — honor that and skip verification too: '
    '"library/notebook covered by existing tests" or "no runtime change". '
    'If build skipped because "existing diagnostic surface" was already '
    'present, do NOT skip verify — proceed to Step 0 and USE that surface.\n'
    '- If the project has no diagnostic surface AND no .claude/test.json '
    '"start" or "interact" config — report the gap briefly and skip. '
    'Do NOT fabricate a setup from scratch just to satisfy verification.\n\n'
    'When skipping, state the reason at the top of your response, then emit '
    'the structured block below so downstream consumers (autopilot, parsers) '
    'still have something to read. Do NOT run any of the steps below.\n'
    '```verify\n'
    'RESULT: skip\n'
    'TESTS_RUN: 0\n'
    'ACTIONS_TESTED: none\n'
    'ENDPOINTS_USED: none\n'
    'SUMMARY: <one-line reason — e.g. "docs-only change", '
    '"build skipped scaffolding (tests cover)", '
    '"no diagnostic surface available">\n'
    '```\n'
    'Otherwise continue to Step 0.\n\n'

    '## Step 0: Load config and clean up stale processes\n'
    'Read .claude/test.json in the repo root for all config fields.\n'
    'Then determine the expected port (from "health" URL or "interact.base_url").\n'
    'If a port is known, kill any stale process on it BEFORE starting the app:\n'
    '- Detect the platform first. Use the right tool:\n'
    '  - Linux/macOS: lsof -t -i:<port> | xargs kill -9\n'
    '  - Windows (bash/Git Bash): netstat -ano | grep :<port> | '
    "awk '{print $5}' | xargs -I{} taskkill /PID {} /F\n"
    '  - Windows (PowerShell): Get-NetTCPConnection -LocalPort <port> '
    '| Stop-Process -Force\n'
    '  - Or check for a PID file left by a previous run\n'
    'This prevents "port in use" failures from previous crashed verify runs.\n\n'

    '## Step 1: Run test commands\n'
    'If "commands" exist in test.json, run each one. Report pass/fail per command.\n\n'

    '## Step 2: Start the app\n'
    'If "start" exists, run it in the background.\n'
    'Poll "health" until ready (timeout 30s). If health never responds, '
    'check the process output for startup errors — report and fail.\n'
    'If no "start" exists, try to build and start the app yourself.\n\n'

    '## Step 3: Authenticate\n'
    'If "interact.auth" exists, use it:\n'
    '- {"type": "api_key", "header": "X-Dev-Api-Key", "env": "DEV_API_KEY"} '
    '→ read the key from that env var and send it as a header\n'
    '- {"type": "basic", "env_user": "...", "env_pass": "..."} '
    '→ read credentials from env vars\n'
    '- {"type": "none"} or missing → no auth needed (dev mode)\n\n'

    '## Step 4: Interactive verification (CRITICAL)\n'
    'This is the most important step. Use the diagnostic endpoints to test '
    'the feature you just built:\n'
    '- Call action endpoints to PERFORM the operation (POST/PUT/DELETE)\n'
    '- Call state endpoints to READ the result and confirm it worked\n'
    '- Don\'t just check "no error" — verify the actual outcome matches intent\n'
    '- If "interact.endpoints" lists available routes, use the relevant ones\n'
    '- If no interact config exists, test manually: curl URLs, check logs, '
    'run the CLI, inspect output files\n\n'
    'Example verification flow:\n'
    '  POST /_dev/actions/create-widget {"name": "test"} → 200\n'
    '  GET /_dev/state/widgets → should contain "test" widget\n'
    '  POST /_dev/actions/delete-widget {"id": "..."} → 200\n'
    '  GET /_dev/state/widgets → should be empty\n\n'

    '## Step 5: Check logs\n'
    'Tail the app\'s log file (if known) for errors, warnings, or unexpected '
    'behavior.\n\n'

    '## Step 6: Cleanup (ALWAYS run this — even if earlier steps failed)\n'
    'This is a finally block — it must execute regardless of pass/fail:\n'
    '- If "stop" exists in test.json, run it\n'
    '- Kill any background processes you started (by PID or port)\n'
    '- Use the same platform-appropriate kill method from Step 0\n'
    '- Verify the port is free after cleanup\n'
    'Do NOT skip cleanup on failure — orphan processes break the next '
    'verify run.\n\n'

    '## Step 7: Report\n'
    'If the change worked, RESULT: pass.\n'
    'If you tested it and found a real bug, fix and re-run from Step 1; if '
    'still broken after fixing, RESULT: fail.\n'
    'If verification was IMPOSSIBLE this run (no diagnostic surface, UI-only '
    'change you cannot script, external service unreachable, app would not '
    'start, port stuck despite Step 0 cleanup), emit RESULT: manual and a '
    'WHY: line — do NOT mark fail. The chain will proceed and surface the '
    'WHY line in the thread so a human can eyeball it.\n\n'
    'Output a structured block at the end:\n'
    '```verify\n'
    'RESULT: pass | fail | manual\n'
    'WHY: <required for manual — one line, what a human should check>\n'
    'TESTS_RUN: <count or "manual">\n'
    'ACTIONS_TESTED: <what you did — e.g. "created user, verified in state, deleted">\n'
    'ENDPOINTS_USED: <which diagnostic endpoints you hit>\n'
    'SUMMARY: <one line>\n'
    '```'
)

# Self-wake guidance — injected per-session (non-build origins only). Lets a
# session waiting on a long external job re-invoke ITSELF in THIS thread after a
# delay instead of falsely promising to "poll in the background". Scheduling is
# done with a [BOT_CMD: /wake] directive in the turn's output (parsed by
# lifecycle._parse_wake_directive) — the same proven channel as /spawn, and
# more reliable than a separate file-write the model can narrate but skip.
WAKE_GUIDANCE = """\

--- Continuing After Your Turn (self-wake) ---
HARD FACT: when this turn ends your process EXITS and NOTHING resumes you — not \
the deploy system, not CI, not a webhook, not a "notification". There is no \
passive way to "get notified" or "be told when it's done"; that silently never \
happens and leaves the user waiting. The ONLY way to continue after an external \
event is to schedule a self-wake.

So whenever you catch yourself about to tell the user you'll "poll", "monitor", \
"watch", "wait for", "check back on", "report back when", "get notified when", \
or "continue once X finishes" — STOP. That sentence is your cue to schedule a \
self-wake RIGHT NOW, in your final message. Don't describe the intent; act on it.

Schedule it by ending your response with this directive — the bot reads it \
straight from your output, so there is no file to write and no tool to call:

[BOT_CMD: /wake delay=300 reason="<short why, shown to the user>"]
~~~wake
<concrete next step when you wake — e.g. re-check whether the deploy is live; \
if it is, run the planned tests and report the result; if it's still running, \
emit a fresh [BOT_CMD: /wake] to keep polling>
~~~

- delay is in seconds, clamped to [30, 86400] (30s–24h). Pick one that fits the \
job (~120-300s for a deploy to land, longer for a long backtest).
- The ~~~wake body IS the prompt that re-invokes THIS session in THIS thread \
after the delay — that is how you continue; it is not optional decoration.
- To poll a still-running job, emit a fresh [BOT_CMD: /wake] each time you wake, \
and stop once the job is done and you've reported the result.
- Put the [BOT_CMD: /wake] line at the top level of your message — NOT indented \
inside a ``` code block or quoted with > (a fenced/quoted example is ignored on \
purpose, so it can be discussed without firing).

The ONLY time you skip self-wake is when the wait is trivial or the user is \
clearly right there — then finish now and tell them to reply "update" or tap a \
button. Never promise to watch something passively: either self-wake, or hand \
it back to the user explicitly.

BETTER THAN A TIMER — [BOT_CMD: /watch] when you can name the job
If what you are waiting on is a process on THIS machine, do not guess a delay. \
Watch the job itself: the bot polls it and resumes you the moment it finishes, \
and meanwhile the user sees a live progress line in the thread instead of a \
thread that looks dead.

Background it so it survives your turn ending, and CAPTURE THE PID — that is \
the step to not skip:

  setsid nohup ./long_job.sh > run.log 2>&1 < /dev/null & echo $!

CHECK the pid is still alive before you arm (`ls -d /proc/$PID`). setsid and \
nohup FORK when the caller is already a process-group leader, so `$!` can hand \
you a launcher that exits the instant the real job starts — arm on that and \
you get woken immediately and told a job finished that never ran. If the pid \
is gone or the job runs behind a wrapper script, use done= + log= instead.

Then end your response with:

[BOT_CMD: /watch pid=12345 log="run.log" progress="step (\\d+)/(\\d+)" \
label="model fit" timeout=6h]
~~~watch
<what to do when it finishes — read the log, pull the numbers, report>
~~~

- pid= is the trigger. Use done="<regex>" instead (or as well) when there is no \
PID to hold — a job on another machine, or one you started via a wrapper. \
done= is matched against the log, so it REQUIRES log=; a done marker with no \
log to read is refused rather than left to time out. Either trigger firing \
ends the watch.
- log= is otherwise optional and only feeds the display; progress= is optional \
too — one capture group is read as a percentage, two as current/total. Get it \
wrong or omit it and the user still sees elapsed time and the log's last line.
- timeout= (default 6h, max 24h) is a safety net, not the plan: if it expires \
you are resumed anyway and told the job did NOT finish, so you can decide \
whether to keep waiting or report.
- Same quoting rules as /wake — top level, not inside ``` or after >.
- Use /wake for everything else: a deploy to propagate, an external API to \
settle, anything with no local process to point at.
"""


DIAGNOSTIC_GUIDANCE = """\

--- Self-Verification Requirement ---
Before implementing the feature, check: can you verify your own work afterward?

FIRST — skip this whole section if any of these apply:
- The change doesn't affect runtime behavior (docs, comments, formatting, \
renames, dead code removal, type hints, config-only edits)
- The project type is library or research/notebook AND an existing test suite, \
runnable demo, or re-run workflow already covers the changed behavior
- The app already has usable RUNTIME diagnostic surface — admin routes, debug \
commands, --self-test or dev-inspection flags, runnable demos. A unit test \
suite alone does NOT qualify.

If skipping, state which reason applies before building. Otherwise continue.

Read .claude/test.json. If it has an "interact" section, you have diagnostic \
access — proceed to implementation. If not, scaffold diagnostic infrastructure \
FIRST, then build the feature.

NOTE: Adding diagnostics is a prerequisite, not scope creep. You may scaffold \
even if the reviewed plan doesn't mention it — this is the one exception to \
"follow the plan, don't re-plan." Don't extend beyond what's needed to verify \
the planned feature.

## Step 1: Classify the project

Look at entry points, dependencies, and structure. Hints:
- `fastapi`, `flask`, `express`, `asp.net`, `gin` → Web API / HTTP service
- `discord.py`, `telegram`, `slack-bolt`, long-running `asyncio.run()` → Bot / worker
- `[project.scripts]`, `bin/`, single-command entry, no server → CLI tool
- `electron`, `tauri`, `tkinter`, desktop framework → Local app
- `.ipynb`, `jupyter`, script-heavy + data files → Research / notebook
- `pytest` + no entry point, package-only `setup.py` → Library
- `next`, `vite`, `webpack`, `index.html`, no backend → Static site / frontend

## Step 2: Scaffold the pattern for that type

Web API / HTTP service:
- Admin route group at /_dev with action POST + state GET endpoints
- Must let you DO things, not just READ (e.g. POST /_dev/actions/create-user, \
  then GET /_dev/state/users to verify)
- Auth: API key header (X-Dev-Api-Key) from .env, or bypass in DEBUG mode

Bot / worker:
- Admin HTTP sidecar (even trivial) exposing state + action triggers
- OR internal debug commands the bot responds to
- Structured state snapshots writable to a known file on demand

CLI tool:
- `--json` flag for machine-readable output
- `--self-test` subcommand exercising core paths
- Meaningful exit codes (0 ok, non-zero per failure type)

Local / desktop app:
- Action scripts driving the app (subprocess, IPC, or framework API)
- State-dump command writing current state to a known file
- Structured log file you can tail

Research / notebook:
- Export key state/results to a known file path after each run
- Structured checkpoint prints at each major step (not bare print)
- Runnable .py equivalent if the notebook is the source of truth

Library / module:
- Dedicated harness script (scripts/verify.py) that imports the library, \
  runs representative scenarios, prints pass/fail
- Not the test suite — a lightweight runner for diagnostic inspection

Static site / frontend:
- Dev server with /_dev/state endpoint returning config + app state
- OR Playwright automation script driving the UI and asserting outcomes

## Step 3: Universal contract (every type)

- Write .claude/test.json with the fields that apply to this project type:
  - `start`: command to launch the app (skip for libraries, notebooks that auto-run)
  - `health`: liveness check (servers only — skip for CLI/library/notebook)
  - `stop`: shutdown command — see stop-command guidance below
  - `interact`: endpoints/commands/flags the verify step can invoke
- Production guard where applicable: debug surface must not REGISTER in prod \
  (conditional mount based on env var), not just rely on auth. Leaking debug \
  endpoints to prod is a security incident.
- Structured logs to a known file path (JSON or key=value), not bare print
- Include auth config in .claude/test.json if servers with admin auth apply

Stop-command guidance (pick by project type):
- Port-based (web API, bot with HTTP sidecar, static dev server):
  - Unix/macOS: kill $(lsof -t -i:<port>)
  - Windows: netstat -ano | findstr :<port> → taskkill /PID <pid> /F
  - Or: write PID to file on start, read/kill on stop (cross-platform)
- Process-based (bot without HTTP, desktop app, long-running CLI):
  - Write PID to a known file on start; stop reads and kills it
  - Or: pkill/taskkill matching the process name, if unique
- No-op (CLI tool, library, notebook that exits on its own):
  - Omit `stop` entirely, or set it to `true` (no action needed)

This diagnostic surface is for YOU — the AI — to verify your own work next step.
"""

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
    '- Pick vX.Y.Z so it is strictly greater than every existing v* tag '
    '(run `git tag --list "v*"` and bump above the highest). Never reuse '
    'a tag — if your computed version already exists, bump again.\n'
    '- Do NOT run `git tag` — the bot creates the tag from your commit '
    'message after merge. Stop after the commit.\n'
)

DONE_PROMPT_STANDALONE = (
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

# Backward-compat alias used by /done and any non-chain caller.
DONE_PROMPT = DONE_PROMPT_STANDALONE

DONE_PROMPT_CHAIN = (
    'Wrap up this build step (autopilot chain). Do NOT cut a release or '
    'create any tag — that happens in a later chain step.\n'
    '1. Review all uncommitted changes and commit them with a clear, '
    'descriptive message.\n'
    '2. Update CHANGELOG.md: add a concise summary of changes under '
    '## [Unreleased]. If the file does not exist, create it with an '
    '[Unreleased] header. Do NOT replace the [Unreleased] header with a '
    'version number.\n'
    '3. Do NOT update any version file (pyproject.toml, package.json, etc.).\n'
    '4. Do NOT create a git tag.\n'
    '5. Make sure nothing is left uncommitted.\n\n'
    'Format contract for the commit message body:\n'
    '- Write the body as one bullet per discrete change, prefixed with `- `.\n'
    '- Each bullet must reference a real file or symbol you actually '
    'modified in this commit.\n'
    '- Do not list speculative or aspirational changes.\n\n'
    'At the very end of your response, output a structured summary block '
    'in exactly this format (no extra text after the block):\n'
    '```summary\n'
    'COMMIT: <short_hash> <commit message>\n'
    'CHANGELOG:\n'
    '- <entry 1>\n'
    '- <entry 2>\n'
    'VERSION: none\n'
    '```'
)

# Verifier prompt for the autopilot `verify_release` chain step.
# Built via plain string `.replace()` rather than `str.format()` because the
# embedded JSON-schema example contains literal `{`/`}` that would collide
# with format placeholders. Use `build_release_verify_prompt(...)` below
# rather than calling `.format()` directly.
RELEASE_VERIFY_PROMPT = (
    'You are a release-claim verifier. The autopilot chain just ran the '
    '`done` step, which produced one or more commits and a CHANGELOG '
    '[Unreleased] entry. Your job is to cross-check the claims against '
    'the actual diff and report any **phantom** claims (statements about '
    'code that does not exist in the diff).\n\n'
    'You MUST return exactly one fenced ```json``` block as your final '
    'output, with this schema and no extra prose after it:\n'
    '```json\n'
    '{\n'
    '  "verdict": "ok" | "mismatch",\n'
    '  "phantom_bullets": [\n'
    '    "<exact bullet text that has no corresponding code change>"\n'
    '  ],\n'
    '  "missing_bullets": [\n'
    '    "<short description of a real diff change that no claim covers>"\n'
    '  ],\n'
    '  "needs_inspection": [\n'
    '    "<file or claim that requires a deeper look outside the truncated diff>"\n'
    '  ],\n'
    '  "rationale": "<one or two sentences explaining your verdict>"\n'
    '}\n'
    '```\n\n'
    'Verdict rules:\n'
    '- Set `verdict` to "mismatch" ONLY if `phantom_bullets` is non-empty.\n'
    '- `missing_bullets` is informational only — it never flips the verdict.\n'
    '- If the diff was truncated and a claim references a file outside the '
    'window, list it under `needs_inspection` instead of marking it phantom.\n'
    '- If you cannot reasonably evaluate (parser failure, unreadable diff, '
    'malformed inputs), output the JSON block with verdict "mismatch" and '
    'a clear rationale.\n\n'
    'Inputs follow.\n\n'
    '## Commit messages produced by `done`\n'
    '```\n'
    '<<COMMIT_MESSAGES>>\n'
    '```\n\n'
    '## CHANGELOG [Unreleased] block\n'
    '```\n'
    '<<CHANGELOG_UNRELEASED>>\n'
    '```\n\n'
    '## git diff --stat (from chain entry to HEAD)\n'
    '```\n'
    '<<DIFF_STAT>>\n'
    '```\n\n'
    '## git diff (size-capped)<<TRUNCATION_NOTE>>\n'
    '```\n'
    '<<DIFF_PAYLOAD>>\n'
    '```\n'
)


def build_release_verify_prompt(
    *,
    commit_messages: str,
    changelog_unreleased: str,
    diff_stat: str,
    diff_payload: str,
    truncation_note: str,
) -> str:
    """Substitute inputs into RELEASE_VERIFY_PROMPT without using str.format.

    Plain `.replace()` is safe against the literal `{` / `}` in the embedded
    JSON-schema example.
    """
    return (
        RELEASE_VERIFY_PROMPT
        .replace("<<COMMIT_MESSAGES>>", commit_messages)
        .replace("<<CHANGELOG_UNRELEASED>>", changelog_unreleased)
        .replace("<<DIFF_STAT>>", diff_stat)
        .replace("<<DIFF_PAYLOAD>>", diff_payload)
        .replace("<<TRUNCATION_NOTE>>", truncation_note)
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
    '4. Report: version number and summary of released changes. '
    'The bot will create and push the tag after this session completes.\n\n'
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
