"""Fetch a thread's owner messages from Discord, the same source the live judge reads.

Cached under data/judge_replay/threads/ so a rerun costs nothing. Raw text stays
there, which git ignores.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "replay_ab"))

from common import LIVE_DATA, LIVE_ROOT  # noqa: E402

RAW = LIVE_DATA / "judge_replay"
CACHE = RAW / "threads"
API = "https://discord.com/api/v10"


def _env(name: str) -> str:
    for line in (LIVE_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{name} not set in the install's .env")


def _get(url: str, token: str) -> list | dict | None:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bot {token}", "User-Agent": "judge-replay (bot, 1.0)"})
    for _ in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(json.load(e).get("retry_after", 2)) + 0.5)
                continue
            if e.code in (403, 404):
                return None
            raise
    return None


def fetch(thread_id: str, token: str, owner: str) -> list[dict] | None:
    """Every owner message in the thread, oldest first, or None if unreadable."""
    path = CACHE / f"{thread_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    before, out = "", []
    while True:
        page = _get(f"{API}/channels/{thread_id}/messages?limit=100{before}", token)
        if page is None:
            return None
        if not page:
            break
        for m in page:
            if m["author"]["id"] == owner and not m["author"].get("bot"):
                out.append({"ts": m["timestamp"], "text": m.get("content") or "",
                            "attachments": [a.get("filename", "") for a in m.get("attachments", [])]})
        before = f"&before={page[-1]['id']}"
        if len(page) < 100:
            break
    out.reverse()
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def credentials() -> tuple[str, str]:
    return _env("DISCORD_BOT_TOKEN"), _env("DISCORD_USER_ID")


def main() -> int:
    token, owner = credentials()
    threads = {json.loads(ln)["thread"] for ln in (RAW / "candidates.jsonl").open(encoding="utf-8")}
    missing = 0
    for tid in sorted(threads):
        if fetch(tid, token, owner) is None:
            missing += 1
    print(f"{len(threads)} threads, {missing} unreadable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
