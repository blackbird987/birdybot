#!/usr/bin/env bash
# Linux/macOS counterpart to start.bat — stop any running bot, start a fresh one detached.
#
# For a machine that should always be running the bot, prefer the systemd user
# service (scripts/claude-bot.service) — it restarts on crash and survives a
# reboot. This script is the manual / one-off equivalent.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    if [[ -x .venv/bin/python ]]; then
        PYTHON=.venv/bin/python
    else
        PYTHON=python3
    fi
fi

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

mkdir -p data/logs
echo "Starting bot in background..."
setsid nohup "$PYTHON" -m bot >> data/logs/stdout.log 2>&1 < /dev/null &
echo "Bot started (pid $!). Tail with: tail -f data/logs/bot.log"
