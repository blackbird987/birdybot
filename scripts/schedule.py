#!/usr/bin/env python3
"""CLI tool for managing bot schedules externally.

Modifies data/state.json directly. The running bot detects file changes
and reloads within ~30 seconds.

Usage:
    python scripts/schedule.py list
    python scripts/schedule.py add --every 24h "Your prompt here"
    python scripts/schedule.py add --at 11:00 "Your prompt here"
    python scripts/schedule.py add --at +2h "Your prompt here"
    python scripts/schedule.py delete sch-001
    python scripts/schedule.py update sch-001 --prompt "New prompt"
    python scripts/schedule.py update sch-001 --every 12h
    python scripts/schedule.py update sch-001 --enabled true
"""

from __future__ import annotations

import json
import re
import sys
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_FILE = Path(__file__).parent.parent / "data" / "state.json"


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"schedules": [], "schedule_counter": 0}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(STATE_FILE.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        Path(tmp_path).rename(STATE_FILE)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def parse_interval(s: str) -> int:
    """Parse '24h', '30m', '1d' etc. to seconds."""
    m = re.match(r"(\d+)([smhd])", s)
    if not m:
        raise ValueError(f"Invalid interval: {s}")
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(m.group(1)) * mult[m.group(2)]


def parse_at(s: str) -> str:
    """Parse 'HH:MM' or '+2h' to ISO timestamp."""
    now = datetime.now(timezone.utc)
    # Relative: +2h, +30m
    m = re.match(r"\+(\d+)([smhd])", s)
    if m:
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        delta = int(m.group(1)) * mult[m.group(2)]
        return (now + timedelta(seconds=delta)).isoformat()
    # Absolute: HH:MM
    m = re.match(r"(\d{1,2}):(\d{2})", s)
    if m:
        target = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                             second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.isoformat()
    raise ValueError(f"Invalid time: {s}")


def format_interval(secs: int | None) -> str:
    if not secs:
        return "one-shot"
    if secs >= 86400 and secs % 86400 == 0:
        return f"every {secs // 86400}d"
    if secs >= 3600 and secs % 3600 == 0:
        return f"every {secs // 3600}h"
    if secs >= 60 and secs % 60 == 0:
        return f"every {secs // 60}m"
    return f"every {secs}s"


def cmd_list(data: dict) -> None:
    schedules = [s for s in data.get("schedules", []) if s.get("enabled", True)]
    if not schedules:
        print("No active schedules.")
        return
    for s in schedules:
        interval = format_interval(s.get("interval_secs"))
        next_run = (s.get("next_run_at") or "")[:16]
        prompt = s["prompt"][:60]
        print(f"  {s['id']}  {interval:<12}  next: {next_run}  {prompt}")


def cmd_add(data: dict, args: list[str]) -> None:
    # Parse flags
    interval_secs = None
    run_at = None
    is_recurring = False
    mode = "explore"
    repo_name = data.get("active_repo", "")
    repos = data.get("repos", {})
    repo_path = repos.get(repo_name, "") if repo_name else ""

    i = 0
    prompt_parts = []
    while i < len(args):
        if args[i] == "--every" and i + 1 < len(args):
            interval_secs = parse_interval(args[i + 1])
            is_recurring = True
            i += 2
        elif args[i] == "--at" and i + 1 < len(args):
            run_at = parse_at(args[i + 1])
            i += 2
        elif args[i] == "--build":
            mode = "build"
            i += 1
        elif args[i] == "--repo" and i + 1 < len(args):
            repo_name = args[i + 1]
            repo_path = repos.get(repo_name, "")
            i += 2
        else:
            prompt_parts.append(args[i])
            i += 1

    prompt = " ".join(prompt_parts)
    if not prompt:
        print("Error: no prompt provided")
        sys.exit(1)

    counter = data.get("schedule_counter", 0) + 1
    data["schedule_counter"] = counter
    sid = f"sch-{counter:03d}"

    now = datetime.now(timezone.utc)
    if interval_secs:
        next_run = (now + timedelta(seconds=interval_secs)).isoformat()
    elif run_at:
        next_run = run_at
    else:
        print("Error: specify --every or --at")
        sys.exit(1)

    schedule = {
        "id": sid,
        "prompt": prompt,
        "repo_name": repo_name,
        "repo_path": repo_path,
        "mode": mode,
        "interval_secs": interval_secs,
        "run_at": run_at,
        "is_recurring": is_recurring,
        "last_run_at": None,
        "next_run_at": next_run,
        "last_summary": None,
        "enabled": True,
    }

    if "schedules" not in data:
        data["schedules"] = []
    data["schedules"].append(schedule)
    save_state(data)
    print(f"Created {sid}: {format_interval(interval_secs)}  next: {next_run[:16]}")
    print(f"  Prompt: {prompt[:80]}")
    print(f"  Repo: {repo_name or '(none)'}  Mode: {mode}")


def cmd_delete(data: dict, sid: str) -> None:
    schedules = data.get("schedules", [])
    found = False
    for i, s in enumerate(schedules):
        if s["id"] == sid:
            schedules.pop(i)
            found = True
            break
    if not found:
        print(f"Schedule {sid} not found")
        sys.exit(1)
    save_state(data)
    print(f"Deleted {sid}")


def cmd_update(data: dict, sid: str, args: list[str]) -> None:
    schedules = data.get("schedules", [])
    sched = None
    for s in schedules:
        if s["id"] == sid:
            sched = s
            break
    if not sched:
        print(f"Schedule {sid} not found")
        sys.exit(1)

    i = 0
    while i < len(args):
        if args[i] == "--prompt" and i + 1 < len(args):
            sched["prompt"] = args[i + 1]
            i += 2
        elif args[i] == "--every" and i + 1 < len(args):
            sched["interval_secs"] = parse_interval(args[i + 1])
            sched["is_recurring"] = True
            now = datetime.now(timezone.utc)
            sched["next_run_at"] = (now + timedelta(seconds=sched["interval_secs"])).isoformat()
            i += 2
        elif args[i] == "--at" and i + 1 < len(args):
            sched["run_at"] = parse_at(args[i + 1])
            sched["next_run_at"] = sched["run_at"]
            sched["is_recurring"] = False
            sched["interval_secs"] = None
            i += 2
        elif args[i] == "--enabled" and i + 1 < len(args):
            sched["enabled"] = args[i + 1].lower() in ("true", "1", "yes")
            i += 2
        elif args[i] == "--mode" and i + 1 < len(args):
            sched["mode"] = args[i + 1]
            i += 2
        else:
            print(f"Unknown flag: {args[i]}")
            sys.exit(1)

    save_state(data)
    print(f"Updated {sid}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/schedule.py <list|add|delete|update> [args]")
        sys.exit(1)

    cmd = sys.argv[1]
    data = load_state()

    if cmd == "list":
        cmd_list(data)
    elif cmd == "add":
        cmd_add(data, sys.argv[2:])
    elif cmd == "delete":
        if len(sys.argv) < 3:
            print("Usage: python scripts/schedule.py delete <schedule-id>")
            sys.exit(1)
        cmd_delete(data, sys.argv[2])
    elif cmd == "update":
        if len(sys.argv) < 4:
            print("Usage: python scripts/schedule.py update <schedule-id> --flag value")
            sys.exit(1)
        cmd_update(data, sys.argv[2], sys.argv[3:])
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
