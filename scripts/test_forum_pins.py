#!/usr/bin/env python3
"""Harness for the forum pin reconcile — the Control Room owns the one pin slot.

A Discord forum channel has a single pin slot. The archive and monitor posts
used to pin themselves on creation, racing the control room for it; whichever
landed last won and nothing re-checked. ForumManager.reconcile_forum_pins()
unpins everything else first, then pins the control room.

Modes:
  (no args)  offline logic test against a fake Discord client
  --live     read the real pin state of every repo/user forum via REST
  --live --fix
             repair the live forums the same way the reconcile does
             (unpin others, then pin the Control Room)
  --repo DIR the checkout holding the live .env and data/ (default: this one).
             Pass the main checkout when running from a build worktree, which
             has neither.

--live needs DISCORD_BOT_TOKEN in .env. It uses plain REST, never the gateway,
so it is safe to run while the bot is up (this project is a singleton).
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PINNED_FLAG = 1 << 1
API = "https://discord.com/api/v10"
UA = "DiscordBot (claude-telegram-bot test_forum_pins, 1.0)"


# --------------------------------------------------------------------------
# Offline: drive _reconcile_forum_pin with a fake client
# --------------------------------------------------------------------------

class FakeFlags:
    def __init__(self, pinned: bool):
        self.pinned = pinned


class FakeThread:
    def __init__(self, tid: int, name: str, pinned: bool, log: list, archived: bool = False):
        self.id = tid
        self.name = name
        self.flags = FakeFlags(pinned)
        self.archived = archived
        self._log = log

    async def edit(self, *, pinned: bool | None = None, archived: bool | None = None):
        if archived is not None:
            self._log.append((self.id, "archived", archived))
            self.archived = archived
            return self  # discord.py returns the updated Thread
        # Mirrors Discord error 50083: an archived thread accepts nothing else.
        if self.archived:
            raise RuntimeError("Thread is archived (50083)")
        self._log.append((self.id, "pinned", pinned))
        self.flags.pinned = bool(pinned)
        return self


class FakeForum:
    def __init__(self, threads):
        self.id = 999
        self.name = "fake-forum"
        self.threads = threads


class FakeClient:
    def __init__(self, by_id):
        self._by_id = by_id

    def get_channel(self, cid):
        return self._by_id.get(cid)

    async def fetch_channel(self, cid):
        ch = self._by_id.get(cid)
        if ch is None:
            raise LookupError(cid)
        return ch


async def _run_case(name, threads_spec, control_id, tracked, cached_only=()):
    """threads_spec: list of (id, name, pinned[, archived]). cached_only: ids
    hidden from forum.threads so they must be resolved through fetch_channel."""
    import discord
    from bot.discord.forums import ForumManager

    calls: list = []
    threads = [FakeThread(t[0], t[1], t[2], calls, *(t[3:])) for t in threads_spec]
    by_id = {t.id: t for t in threads}
    visible = [t for t in threads if t.id not in cached_only]
    forum = FakeForum(visible)

    mgr = object.__new__(ForumManager)
    mgr._client = FakeClient(by_id)

    # The real method type-checks with isinstance(x, discord.Thread); patch the
    # module's reference so our fakes pass without subclassing discord internals.
    real_thread = discord.Thread
    discord.Thread = (FakeThread,)  # type: ignore[assignment,misc]
    try:
        await mgr._reconcile_forum_pin(forum, control_id, set(tracked))
    finally:
        discord.Thread = real_thread  # type: ignore[assignment]

    return name, calls, {t.id: t.flags.pinned for t in threads}


async def offline() -> int:
    CONTROL, ARCHIVE, MONITOR, SESSION = 1, 2, 3, 4
    failures = []

    def check(label, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
        if not cond:
            failures.append(label)

    print("case: archive holds the pin (the live bug)")
    _, calls, final = await _run_case(
        "bug", [(CONTROL, "Control Room", False), (ARCHIVE, "Archive", True)],
        CONTROL, {ARCHIVE},
    )
    check("archive ends unpinned", final[ARCHIVE] is False)
    check("control ends pinned", final[CONTROL] is True)
    check("unpin happens before pin",
          calls == [(ARCHIVE, "pinned", False), (CONTROL, "pinned", True)], str(calls))

    print("case: already correct -> no edits issued")
    _, calls, final = await _run_case(
        "ok", [(CONTROL, "Control Room", True), (ARCHIVE, "Archive", False)],
        CONTROL, {ARCHIVE},
    )
    check("no edits issued", calls == [], str(calls))
    check("control still pinned", final[CONTROL] is True)

    print("case: a stray pinned session thread also yields the slot")
    _, calls, final = await _run_case(
        "stray", [(CONTROL, "Control Room", False), (SESSION, "some session", True)],
        CONTROL, set(),
    )
    check("stray unpinned", final[SESSION] is False)
    check("control pinned", final[CONTROL] is True)

    print("case: monitor pinned, control not in forum cache (REST fallback)")
    _, calls, final = await _run_case(
        "uncached", [(CONTROL, "Control Room", False), (MONITOR, "Monitor", True)],
        CONTROL, {MONITOR}, cached_only=(CONTROL,),
    )
    check("monitor unpinned", final[MONITOR] is False)
    check("control resolved and pinned", final[CONTROL] is True)

    print("case: no control room recorded -> nothing is pinned, others still yield")
    _, calls, final = await _run_case(
        "nocontrol", [(ARCHIVE, "Archive", True)], None, {ARCHIVE},
    )
    check("archive unpinned", final[ARCHIVE] is False)
    check("no pin attempted", all(v is False for _, _, v in calls), str(calls))

    print("case: control room fell asleep -> unarchive, then pin")
    _, calls, final = await _run_case(
        "archived", [(CONTROL, "Control Room", False, True), (ARCHIVE, "Archive", True)],
        CONTROL, {ARCHIVE},
    )
    check("archive unpinned", final[ARCHIVE] is False)
    check("control pinned despite being archived", final[CONTROL] is True)
    check("unarchive precedes the pin",
          calls == [(ARCHIVE, "pinned", False), (CONTROL, "archived", False),
                    (CONTROL, "pinned", True)], str(calls))

    print("case: the post holding the slot is asleep -> wake it, then unpin")
    _, calls, final = await _run_case(
        "archived-holder", [(CONTROL, "Control Room", False), (ARCHIVE, "Archive", True, True)],
        CONTROL, {ARCHIVE},
    )
    check("sleeping archive still yields the pin", final[ARCHIVE] is False)
    check("control pinned", final[CONTROL] is True)
    check("archive woken before unpin",
          calls == [(ARCHIVE, "archived", False), (ARCHIVE, "pinned", False),
                    (CONTROL, "pinned", True)], str(calls))

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("All offline checks passed.")
    return 0


# --------------------------------------------------------------------------
# Live: read (and optionally repair) the real forums over REST
# --------------------------------------------------------------------------

LIVE_ROOT = ROOT


def _env() -> dict:
    env = {}
    for line in (LIVE_ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _req(token, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": UA,
            "Content-Type": "application/json",
        },
    )
    try:
        return json.load(urllib.request.urlopen(req))
    except urllib.error.HTTPError as e:
        return {"_error": e.code, "_body": e.read().decode()[:160]}


def _scopes() -> list[tuple[str, str, dict]]:
    """(label, forum_id, {kind: thread_id}) for every repo and user forum."""
    out = []
    state = json.loads((LIVE_ROOT / "data" / "state.json").read_text())
    projects = state.get("platform_state", {}).get("discord", {}).get("forum_projects", {})
    for name, p in projects.items():
        if not p.get("forum_channel_id"):
            continue
        out.append((name, p["forum_channel_id"], {
            k: p.get(f"{k}_thread_id") for k in ("control", "archive", "monitor")
            if p.get(f"{k}_thread_id")
        }))
    acc = LIVE_ROOT / "data" / "access.json"
    if acc.exists():
        cfg = json.loads(acc.read_text())
        for uid, ua in (cfg.get("users") or {}).items():
            if not ua.get("forum_channel_id"):
                continue
            out.append((f"user:{ua.get('display_name', uid)}", ua["forum_channel_id"], {
                k: ua.get(f"{k}_thread_id") for k in ("control", "archive")
                if ua.get(f"{k}_thread_id")
            }))
    return out


def live(fix: bool) -> int:
    token = _env().get("DISCORD_BOT_TOKEN")
    if not token:
        print("DISCORD_BOT_TOKEN missing from .env")
        return 2

    wrong = 0
    for label, forum_id, threads in _scopes():
        control_id = threads.get("control")
        states: dict[str, bool | None] = {}   # None = the fetch failed
        flags: dict[str, int] = {}
        archived: dict[str, bool] = {}
        for kind, tid in threads.items():
            ch = _req(token, "GET", f"/channels/{tid}")
            if "_error" in ch:
                states[kind] = None
            else:
                flags[kind] = ch.get("flags", 0)
                archived[kind] = bool((ch.get("thread_metadata") or {}).get("archived"))
                states[kind] = bool(flags[kind] & PINNED_FLAG)
            time.sleep(0.12)

        # Same rule the reconcile applies: nothing but the control room may
        # hold the slot, and the control room takes it when one is recorded.
        others_pinned = any(v for k, v in states.items() if k != "control")
        ok = not others_pinned and (states.get("control") is True if control_id else True)
        rendered = "  ".join(
            f"{k}={'PIN' if v else ('--' if v is False else 'ERR')}" for k, v in states.items()
        )
        if not control_id:
            rendered += "  (no control room recorded)"
        print(f"{label:34s} {rendered}{'' if ok else '   <- wrong'}")
        if ok:
            continue
        wrong += 1
        if not fix:
            continue

        def _repin(kind, tid, want):
            """Flip one thread's pin bit, waking it first if it is asleep.

            Sends flags as a masked edit rather than a bare value so unrelated
            channel flag bits survive — the same thing discord.py's
            Thread.edit(pinned=...) does.
            """
            if archived.get(kind):
                _req(token, "PATCH", f"/channels/{tid}", {"archived": False})
                print(f"    woke {kind} first (it was archived)")
                time.sleep(0.5)
            cur = flags.get(kind, 0)
            value = (cur | PINNED_FLAG) if want else (cur & ~PINNED_FLAG)
            r = _req(token, "PATCH", f"/channels/{tid}", {"flags": value})
            print(f"    {'pinned' if want else 'unpinned'} {kind}: "
                  f"{'ok' if '_error' not in r else r}")
            time.sleep(0.5)

        for kind, tid in threads.items():
            if kind != "control" and states.get(kind) is True:
                _repin(kind, tid, False)
        if control_id and states.get("control") is not True:
            _repin("control", control_id, True)

    print()
    if wrong == 0:
        print("All forums: Control Room holds the pin.")
        return 0
    print(f"{wrong} forum(s) wrong" + (" — repaired, re-run to confirm" if fix else ""))
    return 0 if fix else 1


if __name__ == "__main__":
    if "--repo" in sys.argv:
        i = sys.argv.index("--repo") + 1
        if i >= len(sys.argv):
            print("--repo needs a directory")
            sys.exit(2)
        LIVE_ROOT = Path(sys.argv[i]).resolve()
    if "--live" in sys.argv:
        sys.exit(live(fix="--fix" in sys.argv))
    sys.exit(asyncio.run(offline()))
