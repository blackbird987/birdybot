#!/usr/bin/env bash
# Linux/macOS counterpart to start.bat — stop any running bot, start a fresh one detached.
#
# Safe to double-click from a file manager (see claude-bot.desktop) or run from
# a terminal. Self-heals the two things that break on a fresh/live boot:
#   * missing .venv or missing dependencies -> creates/installs them
#   * stale CLAUDE_BINARY path in .env      -> repoints it at the real claude
#
# For a machine that should always be running the bot, prefer the systemd user
# service (scripts/claude-bot.service) — it restarts on crash and survives a
# reboot. This script is the manual / one-off equivalent.
set -uo pipefail
cd "$(dirname "$0")"

# When launched by double-click there is no terminal attached; keep the window
# open at the end so errors are readable. LAUNCHED_FROM_GUI is set by the
# .desktop entry.
GUI="${LAUNCHED_FROM_GUI:-0}"
fail() {
    printf '\n\033[31mERROR:\033[0m %s\n' "$1" >&2
    [[ "$GUI" == "1" ]] && read -r -p "Press Enter to close..."
    exit 1
}

# --- 0. don't fight the systemd service -------------------------------------
# When claude-bot.service is active it owns the bot process. This script's first
# act is to kill any running bot, which systemd would immediately restart — and
# then we'd start a second, unmanaged copy on top. Hand over instead.
# INVOCATION_ID is set by systemd, so the service's own ExecStart skips this.
if [[ -z "${INVOCATION_ID:-}" ]] && systemctl --user is-active --quiet claude-bot.service 2>/dev/null; then
    echo "claude-bot.service is running — restarting through systemd instead of starting a second copy."
    systemctl --user restart claude-bot.service || fail "systemctl restart failed"
    systemctl --user --no-pager --lines=0 status claude-bot.service || true
    [[ "$GUI" == "1" ]] && read -r -p "Press Enter to close..."
    exit 0
fi

# --- 1. python / venv -------------------------------------------------------
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    if [[ -x .venv/bin/python ]]; then
        PYTHON=.venv/bin/python
    else
        command -v python3 >/dev/null || fail "python3 not found — install Python 3.11+."
        echo "No .venv found — creating one (first run takes a minute)..."
        python3 -m venv .venv || fail "could not create .venv"
        PYTHON=.venv/bin/python
        "$PYTHON" -m pip install --quiet --upgrade pip
        "$PYTHON" -m pip install --quiet -e . || fail "dependency install failed"
        echo "Dependencies installed."
    fi
fi

# Deps can also go missing when the venv predates a dependency bump, or when a
# live-USB session wiped a venv that lived outside the repo.
if ! "$PYTHON" -c 'import discord, dotenv, httpx, cryptography, psutil' 2>/dev/null; then
    echo "Dependencies missing or out of date — installing..."
    "$PYTHON" -m pip install --quiet -e . || fail "dependency install failed"
fi

# --- 2. claude CLI path -----------------------------------------------------
# config.py loads .env with override=True, so an exported CLAUDE_BINARY would be
# ignored — the stale value has to be fixed in the file itself.
if [[ -f .env ]]; then
    env_bin="$(grep -E '^CLAUDE_BINARY=' .env | head -1 | cut -d= -f2-)"
    if [[ -n "$env_bin" && ! -x "$env_bin" ]]; then
        real_bin="$(command -v claude || true)"
        if [[ -n "$real_bin" ]]; then
            echo "CLAUDE_BINARY was stale ($env_bin) — repointing to $real_bin"
            sed -i "s|^CLAUDE_BINARY=.*|CLAUDE_BINARY=$real_bin|" .env
        else
            fail "claude CLI not found on PATH and CLAUDE_BINARY=$env_bin does not exist.
       Install the Claude Code CLI, then re-run this script."
        fi
    fi
fi

# --- 3. git ------------------------------------------------------------------
# Not fatal — the bot boots and serves Discord without it — but every build,
# worktree and merge operation shells out to `git` and will throw
# FileNotFoundError without it. Warn loudly rather than fail silently later.
if ! command -v git >/dev/null; then
    printf '\n\033[33mWARNING:\033[0m git is not installed. The bot will start, but builds,\n'
    printf '         worktrees and merges will all fail. Install it with:\n'
    printf '             sudo dnf install -y git\n\n'
fi

# --- 4. stop any existing bot ----------------------------------------------
echo "Stopping existing bot instances..."
# The bot relaunches itself when signalled (see _emergency_reboot_handler in
# bot/app.py). Without this marker it would come straight back and race the
# fresh instance started below for the PID lock.
mkdir -p data
: > data/stop_requested
if [[ -f data/bot.pid ]]; then
    pid="$(cat data/bot.pid 2>/dev/null || true)"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        kill "$pid" 2>/dev/null || true
        for _ in {1..20}; do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.5
        done
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f data/bot.pid
fi
# Belt and braces: anything still running the module, but never this script.
pkill -f 'python[0-9.]*  *-m  *bot' 2>/dev/null || true
sleep 1
# Consumed by whichever instance caught the signal, or left behind if none
# did; either way it must not be sitting there for the new one.
rm -f data/stop_requested

# --- 5. start ---------------------------------------------------------------
mkdir -p data/logs
echo "Starting bot in background..."
setsid nohup "$PYTHON" -m bot >> data/logs/stdout.log 2>&1 < /dev/null &
started_pid=$!

# Give it a moment to crash on import/config errors rather than reporting a
# false success — the common failure mode is an exception during startup.
sleep 5
if kill -0 "$started_pid" 2>/dev/null; then
    echo "Bot started (pid $started_pid). Tail with: tail -f data/logs/bot.log"
    [[ "$GUI" == "1" ]] && { echo; read -r -t 10 -p "Closing in 10s..."; }
    exit 0
else
    echo "Bot exited immediately. Last output:" >&2
    tail -n 20 data/logs/stdout.log >&2
    [[ "$GUI" == "1" ]] && read -r -p "Press Enter to close..."
    exit 1
fi
