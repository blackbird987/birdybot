"""Characterization harness for the usage-limit gate and its queue.

Pins TODAY's behaviour of the code in ``bot/discord/bot.py`` (``ClaudeBot``)
and the button handler in ``bot/discord/interactions.py`` that holds, queues
and replays work during Anthropic's throttle window.  Written ahead of a
refactor that extracts this into a ``UsageGate`` class, so that any change of
behaviour, rather than of location, fails here.

What the gate actually is (read from the code, not assumed)
-----------------------------------------------------------
* It is a TIME WINDOW gate, not a per-account one.  ``is_usage_limit_active()``
  in ``bot/discord/usage_notifier.py`` is true on weekdays 5am-11am PT.  Account
  state (``has_spawnable_account`` and friends) plays no part in it.
* Admission: ``_offer_gate`` returns False (work proceeds) when the window is
  not active, when the channel has a live Run-now bypass, or when the channel
  is not a forum thread.  Otherwise it persists a queue entry, posts a
  Run now / Queue for 11am PT / Cancel prompt and returns True (work held).
* Persistence is a JSON list at ``config.USAGE_QUEUE_FILE`` (NOT the
  StateStore), serialised by ``bot._usage_queue_lock``, written atomically.
* There is no cap on the queue and no dedupe for text prompts.  Callback
  (button) entries are deduped while ``awaiting_choice``.
* Drain: ``_fire_due_entries(now)`` promotes ``awaiting_choice`` entries whose
  ``run_at`` has passed to ``queued`` ("ignoring the prompt = queuing"), pops
  every ``queued`` entry that is due (file order), retires the gate message,
  and replays each one outside the lock.

Names this harness calls or patches (retarget these after the extraction)
------------------------------------------------------------------------
Methods called through ``_Bot`` (unbound functions copied off ``ClaudeBot``;
the stub class below lists them, one line each, in ``_REAL``):
  _offer_gate, _offer_usage_limit_choice, _offer_usage_limit_choice_for_callback,
  _read_usage_queue, _write_usage_queue_atomic, _usage_queue_append,
  _usage_queue_update, _usage_queue_remove, _usage_queue_promote_expired,
  _usage_queue_pop_due, _fire_due_entries, _retire_gate_message,
  _migrate_relative_image_paths, _usage_queue_startup_drain,
  _usage_queue_replay_loop, _replay_to_thread, _replay_callback
Methods replaced on the stub by recorders (the gate's outbound seams):
  _replay_to_thread, _replay_callback (for the drain and Run-now cases),
  _wait_for_ready, _sweep_pending_images (for the startup-drain case),
  get_channel / fetch_channel (Discord lookup)
Bot attributes the gate reads or writes:
  _usage_queue_lock, _usage_gate_bypass, _forums.thread_to_project
Module level in bot.discord.bot:
  prepare_replayed_prompt, refresh_image_retention, unlink_image_paths
Other modules:
  bot.discord.interactions._handle_usage_action, interactions.action_label,
  bot.discord.usage_notifier.is_usage_limit_active / next_window_end_utc
    (PATCHED on the module: the gate imports them lazily from there)
  bot.config.USAGE_QUEUE_FILE (PATCHED to a tempfile)
  bot.engine.commands.on_text / handle_callback (PATCHED, only to observe the
    replay handoff in ``_replay_to_thread`` / ``_replay_callback``)

Not covered here: ``commands.on_text`` consulting ``ctx.offer_usage_limit_choice``
and ``interactions.py`` calling ``_offer_usage_limit_choice_for_callback``
before dispatch (both are callers, not the gate).

Run: python scripts/test_usage_gate.py   (exit 0 on pass)
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bot import config
from bot.discord import bot as bot_mod
from bot.discord import interactions as interactions_mod
from bot.discord import usage_notifier
from bot.discord.bot import ClaudeBot
from bot.engine import commands as commands_mod


_failures: list[str] = []


def _check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}: {name}")
    if not cond:
        _failures.append(name)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class _Msg:
    def __init__(self, mid: int, content: str, view) -> None:
        self.id = mid
        self.content = content
        self.view = view
        self.edits: list[dict] = []
        self.edit_raises = False

    async def edit(self, **kw) -> None:
        if self.edit_raises:
            raise RuntimeError("message gone")
        self.edits.append(kw)


class _Channel:
    def __init__(self, cid: int) -> None:
        self.id = cid
        self.sent: list[_Msg] = []
        self.send_raises = False
        self.on_send = None  # hook run just before the message is created
        self._next = 9000 + cid

    async def send(self, content=None, view=None) -> _Msg:
        if self.on_send:
            self.on_send()
        if self.send_raises:
            raise RuntimeError("cannot send")
        self._next += 1
        m = _Msg(self._next, content, view)
        self.sent.append(m)
        return m

    async def fetch_message(self, mid: int) -> _Msg:
        for m in self.sent:
            if m.id == mid:
                return m
        raise RuntimeError("unknown message")


class _Forums:
    def __init__(self, threads: set[str]) -> None:
        self.threads = threads
        self.info = SimpleNamespace(session_id="sess-1")
        self.proj = SimpleNamespace(repo_name="Repo")
        self.woken: list[str] = []
        self.attached: list[str] = []
        self.persisted: list[object] = []

    def thread_to_project(self, channel_id):
        if channel_id in self.threads:
            return self.proj, self.info
        return None

    async def wake_repo_if_dormant(self, repo, notify_channel_id=None):
        self.woken.append(repo)

    def attach_session_callbacks(self, ctx, info, channel_id):
        self.attached.append(channel_id)

    def persist_ctx_settings(self, ctx):
        self.persisted.append(ctx)


class _Ctx:
    def __init__(self, channel_id="100", repo_name="Repo", images=None) -> None:
        self.channel_id = channel_id
        self.repo_name = repo_name
        self.user_id = "u1"
        self.user_name = "owner"
        self.pending_image_paths = list(images or [])
        self.offer_usage_limit_choice = "hook-still-set"
        self.source = None


_REAL = (
    "_offer_gate", "_offer_usage_limit_choice",
    "_offer_usage_limit_choice_for_callback", "_read_usage_queue",
    "_write_usage_queue_atomic", "_usage_queue_append", "_usage_queue_update",
    "_usage_queue_remove", "_usage_queue_promote_expired",
    "_usage_queue_pop_due", "_fire_due_entries", "_retire_gate_message",
    "_migrate_relative_image_paths", "_usage_queue_startup_drain",
    "_usage_queue_replay_loop", "_replay_to_thread", "_replay_callback",
)


class _Bot:
    """Stand-in for ClaudeBot carrying only what the gate touches."""

    def __init__(self, threads=("100", "200")) -> None:
        # Every bot starts with an empty queue file.
        if config.USAGE_QUEUE_FILE.exists():
            config.USAGE_QUEUE_FILE.unlink()
        self._usage_queue_lock = asyncio.Lock()
        self._usage_gate_bypass: dict[str, datetime] = {}
        self._forums = _Forums(set(threads))
        self.channels: dict[int, _Channel] = {int(t): _Channel(int(t)) for t in threads}
        self.get_returns_none = False
        self.fetched: list[int] = []
        # Recorders for the outbound seams.
        self.replayed: list[tuple] = []        # ("text", ch, prompt, repo) / ("cb", entry)
        self.replay_raises: set[str] = set()   # qids (or prompts) that raise
        self.lock_free_at_replay: list[bool] = []
        self.events: list[str] = []
        self.ready = True
        self.swept = 0
        self.ctx_calls: list[dict] = []

    # --- Discord lookup seam -------------------------------------------------
    def get_channel(self, cid: int):
        return None if self.get_returns_none else self.channels.get(cid)

    async def fetch_channel(self, cid: int):
        self.fetched.append(cid)
        ch = self.channels.get(cid)
        if ch is None:
            raise RuntimeError("no such channel")
        return ch

    # --- Replay seams (replaced by recorders for drain / Run-now cases) -------
    async def _rec_replay_to_thread(self, channel_id, prompt, repo_name=None, *, source="replay"):
        self.events.append(f"replay:{prompt}")
        self.lock_free_at_replay.append(not self._usage_queue_lock.locked())
        self.replayed.append(("text", channel_id, prompt, repo_name))
        if prompt in self.replay_raises:
            raise RuntimeError("replay boom")
        return True

    async def _rec_replay_callback(self, entry):
        self.events.append(f"replay_cb:{entry.get('action')}")
        self.lock_free_at_replay.append(not self._usage_queue_lock.locked())
        self.replayed.append(("cb", entry))
        if entry.get("qid") in self.replay_raises:
            raise RuntimeError("replay boom")

    async def _wait_for_ready(self, label):
        self.events.append("wait_for_ready")
        return self.ready

    async def _sweep_pending_images(self):
        self.events.append("sweep")
        self.swept += 1

    # --- Pieces used by the real _replay_to_thread / _replay_callback ---------
    def _ctx(self, channel_id, **kw):
        self.ctx_calls.append({"channel_id": channel_id, **kw})
        return _Ctx(channel_id=channel_id)

    def _cancel_sleep(self, channel_id):
        pass

    def _schedule_sleep(self, channel_id):
        pass

    async def _try_apply_tags_after_run(self, channel_id):
        pass

    def use_recorders(self) -> "_Bot":
        self._replay_to_thread = self._rec_replay_to_thread
        self._replay_callback = self._rec_replay_callback
        return self


for _name in _REAL:
    setattr(_Bot, _name, getattr(ClaudeBot, _name))


def _qfile_entries() -> list[dict]:
    try:
        return json.loads(config.USAGE_QUEUE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def _write_q(entries: list[dict]) -> None:
    config.USAGE_QUEUE_FILE.write_text(json.dumps(entries), encoding="utf-8")


def _entry(qid, *, status="queued", run_at=None, ch="100", typ="text",
           prompt=None, **extra) -> dict:
    e = {
        "qid": qid, "status": status,
        "run_at": (run_at if run_at is not None
                   else (NOW - timedelta(minutes=1)).isoformat()),
        "channel_id": ch, "message_id": None, "repo_name": "Repo",
        "user_id": "u1", "user_name": "owner",
    }
    if typ is not None:
        e["type"] = typ
    if typ in ("text", None):
        e["prompt"] = prompt if prompt is not None else f"prompt-{qid}"
        e["image_paths"] = []
    else:
        e.update(action="plan", instance_id="inst-1", source_msg_id="m-1")
    e.update(extra)
    return e


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


NOW = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)   # a Monday, 8am PDT
WINDOW_END = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)  # 11am PDT


def _patch_window(active: bool, end: datetime = WINDOW_END):
    usage_notifier.is_usage_limit_active = lambda now_utc=None: active
    usage_notifier.next_window_end_utc = lambda now_utc=None: end


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------

def case_window_functions() -> None:
    print("\n== The window the gate keys on (usage_notifier, unpatched) ==")
    act = _REAL_ACTIVE
    end = _REAL_END
    mon = lambda h, m=0: datetime(2026, 10, 12, h, m, tzinfo=timezone.utc)
    _check("Monday 12:00Z (5am PDT) is inside the window", act(mon(12)) is True)
    _check("Monday 17:59Z (10:59am PDT) is inside the window", act(mon(17, 59)) is True)
    _check("Monday 18:00Z (11am PDT) is outside the window", act(mon(18)) is False)
    _check("Monday 11:59Z (4:59am PDT) is outside the window", act(mon(11, 59)) is False)
    sat = datetime(2026, 10, 17, 15, 0, tzinfo=timezone.utc)
    _check("Saturday is never inside the window", act(sat) is False)
    _check("inside the window the end is today's 11am PT", end(mon(15)) == mon(18))
    fri_after = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)
    _check("Friday after 11am PT rolls to Monday 11am PT",
           end(fri_after) == datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc))


async def case_admission() -> None:
    print("\n== Admission: when work is held or let through ==")
    bot = _Bot()
    ctx = _Ctx("100", images=["/x/a.png"])

    _patch_window(False)
    out = await bot._offer_usage_limit_choice(ctx, "hello")
    _check("window inactive: not handled, work proceeds", out is False)
    _check("window inactive: nothing sent", not bot.channels[100].sent)
    _check("window inactive: nothing persisted", _qfile_entries() == [])

    _patch_window(True)
    out = await bot._offer_usage_limit_choice(_Ctx("555"), "hello")
    _check("active but not a forum thread: not handled", out is False)
    _check("active but not a forum thread: nothing persisted", _qfile_entries() == [])

    out = await bot._offer_usage_limit_choice(ctx, "hello world")
    _check("active in a forum thread: handled (held)", out is True)
    sent = bot.channels[100].sent
    _check("exactly one prompt posted", len(sent) == 1)
    entries = _qfile_entries()
    _check("exactly one entry persisted", len(entries) == 1)
    e = entries[0] if entries else {}
    qid = e.get("qid", "")
    _check("qid is 8 hex chars", re.fullmatch(r"[0-9a-f]{8}", qid) is not None)
    _check("entry starts as awaiting_choice", e.get("status") == "awaiting_choice")
    _check("entry type is text", e.get("type") == "text")
    _check("entry keeps the prompt verbatim", e.get("prompt") == "hello world")
    _check("entry copies the pending image paths", e.get("image_paths") == ["/x/a.png"])
    _check("run_at is the window end", e.get("run_at") == WINDOW_END.isoformat())
    _check("entry carries channel, repo and user",
           (e.get("channel_id"), e.get("repo_name"), e.get("user_id"), e.get("user_name"))
           == ("100", "Repo", "u1", "owner"))
    _check("created_at is a tz-aware timestamp",
           datetime.fromisoformat(e.get("created_at") or "") is not None
           and datetime.fromisoformat(e.get("created_at") or "").tzinfo is not None)
    _check("message_id is back-filled with the posted message id",
           e.get("message_id") == str(sent[0].id))

    print("\n== What the user is told when held ==")
    content = sent[0].content
    ts = int(WINDOW_END.timestamp())
    _check("message names the unlock time as a Discord timestamp",
           f"Usage limits active until <t:{ts}:t>." in content)
    _check("message offers Run now or queue for a text prompt",
           "**this prompt**" in content and "Run now (will be throttled) or queue?" in content)
    items = list(sent[0].view.children)
    _check("three buttons in order Run now / Queue / Cancel",
           [b.label for b in items]
           == ["Run now", "Queue for 11am PT", "Cancel"])
    _check("button custom_ids carry the qid",
           [b.custom_id for b in items]
           == [f"usage_run:{qid}", f"usage_queue:{qid}", f"usage_cancel:{qid}"])
    _check("view has no timeout (survives long waits)", sent[0].view.timeout is None)

    print("\n== No dedupe for text, and no cap ==")
    await bot._offer_usage_limit_choice(ctx, "hello world")
    _check("identical text prompt queues a second entry", len(_qfile_entries()) == 2)
    for i in range(23):
        await bot._offer_usage_limit_choice(ctx, f"p{i}")
    _check("25 entries held with no cap", len(_qfile_entries()) == 25)
    _check("every one got its own prompt message", len(bot.channels[100].sent) == 25)
    _check("qids are unique", len({x["qid"] for x in _qfile_entries()}) == 25)


async def case_persist_before_send() -> None:
    print("\n== Entry is persisted before the prompt is sent ==")
    _patch_window(True)
    bot = _Bot()
    seen: dict = {}

    def hook() -> None:
        ents = _qfile_entries()
        seen["entries"] = ents

    bot.channels[100].on_send = hook
    await bot._offer_usage_limit_choice(_Ctx("100"), "x")
    ents = seen.get("entries", [])
    _check("entry already on disk when send is called", len(ents) == 1)
    _check("... with message_id still None at that moment",
           bool(ents) and ents[0].get("message_id") is None)


async def case_send_failures() -> None:
    print("\n== A failed prompt falls through and leaves no orphan ==")
    _patch_window(True)
    bot = _Bot()
    bot.channels[100].send_raises = True
    out = await bot._offer_usage_limit_choice(_Ctx("100"), "x")
    _check("send failure: not handled, work proceeds", out is False)
    _check("send failure: queue entry removed again", _qfile_entries() == [])

    bot = _Bot()
    bot.get_returns_none = True
    out = await bot._offer_usage_limit_choice(_Ctx("100"), "x")
    _check("cache miss falls back to fetch_channel", bot.fetched == [100])
    _check("cache miss then fetch ok: handled", out is True and len(_qfile_entries()) == 1)

    bot = _Bot(threads=("100", "300"))   # 300 is a thread with no fetchable channel
    bot.get_returns_none = True
    del bot.channels[300]
    out = await bot._offer_usage_limit_choice(_Ctx("300"), "x")
    _check("fetch_channel failure: not handled", out is False)
    _check("fetch_channel failure: queue entry removed", _qfile_entries() == [])


async def case_bypass() -> None:
    print("\n== Per-channel Run-now bypass ==")
    bot = _Bot()
    _patch_window(True)
    bot._usage_gate_bypass["100"] = NOW + timedelta(days=3)   # far future: always live
    out = await bot._offer_usage_limit_choice(_Ctx("100"), "x")
    _check("live bypass: not handled", out is False)
    _check("live bypass: nothing persisted or sent",
           _qfile_entries() == [] and not bot.channels[100].sent)
    _check("live bypass is kept", "100" in bot._usage_gate_bypass)
    out = await bot._offer_usage_limit_choice(_Ctx("200"), "x")
    _check("bypass is per channel: another thread is still gated", out is True)

    bot = _Bot()
    bot._usage_gate_bypass["100"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
    out = await bot._offer_usage_limit_choice(_Ctx("100"), "x")
    _check("expired bypass: gate offered again", out is True)
    _check("expired bypass pruned inline", "100" not in bot._usage_gate_bypass)

    print("\n== Bypass is only consulted while the window is active ==")
    bot = _Bot()
    _patch_window(False)
    bot._usage_gate_bypass["100"] = datetime(2020, 1, 1, tzinfo=timezone.utc)
    out = await bot._offer_usage_limit_choice(_Ctx("100"), "x")
    _check("inactive window: not handled", out is False)
    _check("inactive window: stale bypass is not touched", "100" in bot._usage_gate_bypass)
    _patch_window(True)


async def case_callback_gate() -> None:
    print("\n== Button-callback gating and double-tap dedupe ==")
    _patch_window(True)
    bot = _Bot()
    ctx = _Ctx("100")
    out = await bot._offer_usage_limit_choice_for_callback(ctx, "plan", "inst-1", "msg-7")
    _check("callback in forum thread: handled", out is True)
    e = _qfile_entries()[0]
    _check("callback entry fields",
           (e["type"], e["action"], e["instance_id"], e["source_msg_id"], e["status"])
           == ("callback", "plan", "inst-1", "msg-7", "awaiting_choice"))
    _check("callback entry has no prompt", "prompt" not in e)
    content = bot.channels[100].sent[0].content
    label = interactions_mod.action_label("plan")
    _check("prompt names the action label, not 'this prompt'",
           f"**{label}**" in content and "this prompt" not in content)

    out = await bot._offer_usage_limit_choice_for_callback(ctx, "plan", "inst-1", "msg-8")
    _check("same channel+action+instance while awaiting: handled", out is True)
    _check("... but no second entry", len(_qfile_entries()) == 1)
    _check("... and no second prompt message", len(bot.channels[100].sent) == 1)

    await bot._offer_usage_limit_choice_for_callback(ctx, "plan", "inst-2", None)
    _check("different instance: new entry", len(_qfile_entries()) == 2)
    await bot._offer_usage_limit_choice_for_callback(ctx, "build", "inst-1", None)
    _check("different action: new entry", len(_qfile_entries()) == 3)
    await bot._offer_usage_limit_choice_for_callback(_Ctx("200"), "plan", "inst-1", None)
    _check("different channel: new entry", len(_qfile_entries()) == 4)

    await bot._usage_queue_update(e["qid"], status="queued")
    await bot._offer_usage_limit_choice_for_callback(ctx, "plan", "inst-1", None)
    _check("dedupe only applies to awaiting_choice (queued does not block)",
           len(_qfile_entries()) == 5)

    _patch_window(False)
    out = await bot._offer_usage_limit_choice_for_callback(ctx, "review_code", "inst-9", None)
    _check("callback with window inactive: not handled", out is False)
    _patch_window(True)
    out = await bot._offer_usage_limit_choice_for_callback(_Ctx("555"), "plan", "i", None)
    _check("callback outside a forum thread: not handled", out is False)


async def case_store_primitives() -> None:
    print("\n== Queue file primitives ==")
    bot = _Bot()
    if config.USAGE_QUEUE_FILE.exists():
        config.USAGE_QUEUE_FILE.unlink()
    _check("missing file reads as empty", bot._read_usage_queue() == [])
    config.USAGE_QUEUE_FILE.write_text("{not json", encoding="utf-8")
    _check("corrupt file reads as empty", bot._read_usage_queue() == [])
    config.USAGE_QUEUE_FILE.write_text('{"a": 1}', encoding="utf-8")
    _check("non-list JSON reads as empty", bot._read_usage_queue() == [])
    await bot._usage_queue_append({"qid": "a"})
    _check("append over an unreadable file starts a fresh list", _qfile_entries() == [{"qid": "a"}])
    await bot._usage_queue_append({"qid": "b"})
    _check("append preserves order", [x["qid"] for x in _qfile_entries()] == ["a", "b"])
    upd = await bot._usage_queue_update("b", status="queued", n=1)
    _check("update returns the merged entry",
           upd == {"qid": "b", "status": "queued", "n": 1})
    _check("update is persisted", _qfile_entries()[1].get("status") == "queued")
    _check("update of unknown qid returns None", await bot._usage_queue_update("zz", x=1) is None)
    removed = await bot._usage_queue_remove("a")
    _check("remove returns the removed entry", removed is not None and removed["qid"] == "a")
    _check("remove persists", [x["qid"] for x in _qfile_entries()] == ["b"])
    _check("remove of unknown qid returns None", await bot._usage_queue_remove("zz") is None)
    _check("atomic write leaves no .tmp file",
           not config.USAGE_QUEUE_FILE.with_suffix(".json.tmp").exists())
    _check("lock is released after every primitive", not bot._usage_queue_lock.locked())


async def case_drain() -> None:
    print("\n== Drain: promotion, selection and order ==")
    bot = _Bot().use_recorders()
    past = (NOW - timedelta(minutes=5)).isoformat()
    future = (NOW + timedelta(minutes=5)).isoformat()
    _write_q([
        _entry("q1", status="queued", run_at=past, prompt="first"),
        _entry("q2", status="awaiting_choice", run_at=past, prompt="ignored-prompt"),
        _entry("q3", status="queued", run_at=future, prompt="not-yet"),
        _entry("q4", status="awaiting_choice", run_at=future, prompt="still-asking"),
        _entry("q5", status="queued", run_at=past, typ="callback"),
        _entry("q6", status="queued", run_at=past, typ=None, prompt="legacy-no-type"),
        _entry("q7", status="queued", run_at=NOW.isoformat(), prompt="exactly-now"),
        _entry("q8", status="queued", run_at=None, prompt="x"),
    ])
    # entry with no run_at at all, and one with garbage
    ents = _qfile_entries()
    del ents[-1]["run_at"]
    ents.append(_entry("q9", status="queued", run_at="not-a-date", prompt="garbage-date"))
    ents.append(_entry("q10", status="weird", run_at=past, prompt="odd-status"))
    _write_q(ents)

    await bot._fire_due_entries(NOW)
    texts = [r[2] for r in bot.replayed if r[0] == "text"]
    _check("replay order follows queue-file order, text and callback interleaved",
           bot.events == ["replay:first", "replay:ignored-prompt", "replay_cb:plan",
                          "replay:legacy-no-type", "replay:exactly-now"])
    _check("awaiting_choice past run_at is promoted and fired (ignoring = queuing)",
           "ignored-prompt" in texts)
    _check("run_at == now is due", "exactly-now" in texts)
    _check("entry without type is replayed as text", "legacy-no-type" in texts)
    _check("future queued and future awaiting_choice are not fired",
           "not-yet" not in texts and "still-asking" not in texts)
    left = {x["qid"]: x for x in _qfile_entries()}
    _check("fired entries are popped from the file",
           not any(q in left for q in ("q1", "q2", "q5", "q6", "q7")))
    _check("future queued entry is kept untouched",
           left.get("q3", {}).get("status") == "queued")
    _check("future awaiting_choice entry is kept untouched",
           left.get("q4", {}).get("status") == "awaiting_choice")
    _check("entries with a missing or unparseable run_at are never fired",
           "q8" in left and "q9" in left)
    _check("unknown status is never fired or removed", "q10" in left)
    _check("replays run outside the queue lock", all(bot.lock_free_at_replay))
    cb = [r[1] for r in bot.replayed if r[0] == "cb"][0]
    _check("callback replay receives the whole popped entry",
           cb["qid"] == "q5" and cb["instance_id"] == "inst-1")
    txt = [r for r in bot.replayed if r[2:3] == ("first",)][0]
    _check("text replay gets channel and repo_name",
           txt[1] == "100" and txt[3] == "Repo")

    bot.replayed.clear()
    await bot._fire_due_entries(NOW)
    _check("a second pass has nothing left to fire", bot.replayed == [])

    print("\n== Drain: no dedupe, naive timestamps, empty file ==")
    bot = _Bot().use_recorders()
    _write_q([_entry("a", prompt="same"), _entry("b", prompt="same")])
    await bot._fire_due_entries(NOW)
    _check("two identical queued prompts are both replayed (no dedupe)",
           [r[2] for r in bot.replayed] == ["same", "same"])
    bot = _Bot().use_recorders()
    naive = (NOW - timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    _write_q([_entry("n", run_at=naive, prompt="naive")])
    await bot._fire_due_entries(NOW)
    _check("a naive run_at is read as UTC", [r[2] for r in bot.replayed] == ["naive"])
    config.USAGE_QUEUE_FILE.unlink()
    await bot._fire_due_entries(NOW)
    _check("missing queue file is a quiet no-op", True)

    print("\n== Drain: gate message retired, failures isolated ==")
    bot = _Bot().use_recorders()
    ch = bot.channels[100]
    old = await ch.send(content="gate", view=object())
    _write_q([
        _entry("m1", prompt="with-msg", message_id=str(old.id)),
        _entry("m2", prompt="boom"),
        _entry("m3", prompt="after-boom", message_id="424242"),   # unknown message id
    ])
    bot.replay_raises.add("boom")
    order: list[str] = []
    orig_edit = old.edit

    async def tracking_edit(**kw):
        order.append("retire")
        await orig_edit(**kw)

    old.edit = tracking_edit
    orig_rec = bot._rec_replay_to_thread

    async def tracking_rec(*a, **kw):
        order.append("replay")
        return await orig_rec(*a, **kw)

    bot._replay_to_thread = tracking_rec
    await bot._fire_due_entries(NOW)
    _check("gate message edited to 'Window ended ... running now.' with buttons cleared",
           len(old.edits) == 1
           and old.edits[0].get("content", "").startswith("▶ Window ended")
           and old.edits[0].get("content", "").endswith("running now.")
           and old.edits[0].get("view", "missing") is None)
    _check("gate message is retired before its replay starts", order[:2] == ["retire", "replay"])
    _check("an entry whose replay raises does not stop the next",
           [r[2] for r in bot.replayed] == ["with-msg", "boom", "after-boom"])
    _check("an unfetchable gate message is swallowed", True)
    _check("all three popped despite the failure", _qfile_entries() == [])
    ch.sent[0].edit_raises = True
    await bot._retire_gate_message({"message_id": str(old.id), "channel_id": "100"})
    _check("an edit failure on retire is swallowed", True)
    before = len(bot.fetched)
    await bot._retire_gate_message({"message_id": None, "channel_id": "100"})
    await bot._retire_gate_message({"message_id": "1", "channel_id": None})
    _check("retire without message_id or channel_id does nothing",
           len(bot.fetched) == before and len(old.edits) == 1)


async def case_drain_images() -> None:
    print("\n== Drain: uploads are kept alive and stale refs scrubbed ==")
    with tempfile.TemporaryDirectory() as td:
        a, b = Path(td, "a.png"), Path(td, "b.png")
        gone = Path(td, "gone.png")
        a.write_bytes(b"x")
        b.write_bytes(b"x")
        old = time.time() - 10 * 86400
        os.utime(a, (old, old))
        os.utime(b, (old, old))
        bot = _Bot().use_recorders()
        _write_q([
            _entry("i1", prompt=f"see screenshot saved at `{a}` ok", image_paths=[str(a)]),
            _entry("i2", prompt=f"see saved at `{gone}` and `{b}`",
                   image_paths=[str(gone), str(b)]),
        ])
        b_mtime_at_first_replay: list[float] = []
        orig = bot._rec_replay_to_thread

        async def spy(ch, prompt, repo_name=None, *, source="replay"):
            if not b_mtime_at_first_replay:
                b_mtime_at_first_replay.append(os.stat(b).st_mtime)
            return await orig(ch, prompt, repo_name, source=source)

        bot._replay_to_thread = spy
        await bot._fire_due_entries(NOW)
        _check("existing image prompt passes through untouched",
               bot.replayed[0][2] == f"see screenshot saved at `{a}` ok")
        _check("missing image's 'saved at' clause is stripped, present one kept",
               gone.name not in bot.replayed[1][2] and f"`{b}`" in bot.replayed[1][2])
        _check("retention clock restarted for the batch before the first replay",
               bool(b_mtime_at_first_replay)
               and b_mtime_at_first_replay[0] > time.time() - 60)
        _check("uploads are not deleted by the drain", a.exists() and b.exists())

    with tempfile.TemporaryDirectory() as td:
        p = Path(td, "shot.png")
        p.write_bytes(b"x")
        _check("prepare_replayed_prompt is a no-op when all files exist",
               bot_mod.prepare_replayed_prompt(f"hi at `{p}`", [str(p)]) == f"hi at `{p}`")
        _check("prepare_replayed_prompt with no images returns the prompt as is",
               bot_mod.prepare_replayed_prompt("hi", []) == "hi")


async def case_startup_and_loop() -> None:
    print("\n== Startup drain ==")
    bot = _Bot().use_recorders()
    bot.ready = False
    _write_q([_entry("s1", prompt="boot", run_at="2020-01-01T00:00:00+00:00")])
    await bot._usage_queue_startup_drain()
    _check("bot never ready: nothing fired or swept",
           bot.replayed == [] and bot.swept == 0 and len(_qfile_entries()) == 1)

    bot = _Bot().use_recorders()
    # The drain reads the real clock, so "overdue" has to be unambiguous.
    _write_q([_entry("s1", prompt="boot", run_at="2020-01-01T00:00:00+00:00")])
    await bot._usage_queue_startup_drain()
    _check("ready: migrate, then fire overdue, then sweep uploads",
           bot.events == ["wait_for_ready", "replay:boot", "sweep"])
    _check("overdue entry fired and popped at boot", _qfile_entries() == [])

    print("\n== Relative image path migration ==")
    with tempfile.TemporaryDirectory() as td:
        cwd = os.getcwd()
        os.chdir(td)
        try:
            absp = str(Path(td, "abs.png"))
            bot = _Bot()
            _write_q([
                _entry("r1", prompt="saved at `rel/x.png` and more",
                       image_paths=["rel/x.png"]),
                _entry("r2", prompt=f"at `{absp}`", image_paths=[absp]),
            ])
            await bot._migrate_relative_image_paths()
            es = {e["qid"]: e for e in _qfile_entries()}
            want = str(Path("rel/x.png").resolve())
            _check("relative path made absolute in image_paths",
                   es["r1"]["image_paths"] == [want])
            _check("relative path rewritten inline in the prompt",
                   es["r1"]["prompt"] == f"saved at `{want}` and more")
            _check("absolute entry untouched", es["r2"]["image_paths"] == [absp])
            snap = _qfile_entries()
            await bot._migrate_relative_image_paths()
            _check("migration is idempotent", _qfile_entries() == snap)
        finally:
            os.chdir(cwd)

    print("\n== Periodic replay loop ==")
    bot = _Bot()
    calls: list[datetime] = []
    delays: list[float] = []

    async def fire(now):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("tick failed")

    bot._fire_due_entries = fire
    real_sleep = asyncio.sleep

    async def fake_sleep(d, *a, **k):
        delays.append(d)
        if len(delays) > 3:
            raise asyncio.CancelledError
        await real_sleep(0)

    asyncio.sleep = fake_sleep
    try:
        try:
            await bot._usage_queue_replay_loop()
        except asyncio.CancelledError:
            pass
    finally:
        asyncio.sleep = real_sleep
    _check("loop sleeps 60s before every pass (never fires on entry)",
           delays[:3] == [60, 60, 60] and len(calls) == 3)
    _check("an exception in one tick does not end the loop", len(calls) >= 2)
    _check("each pass is given a tz-aware 'now'",
           all(c.tzinfo is not None for c in calls))


class _Interaction:
    def __init__(self, msg: _Msg | None) -> None:
        self.message = msg
        self.followups: list[tuple] = []
        self.followup = SimpleNamespace(send=self._send)

    async def _send(self, text, ephemeral=False):
        self.followups.append((text, ephemeral))


async def case_buttons() -> None:
    print("\n== Run now / Queue / Cancel buttons ==")
    _patch_window(True)
    handle = interactions_mod._handle_usage_action

    with tempfile.TemporaryDirectory() as td:
        img = Path(td, "u.png")
        img.write_bytes(b"x")

        # Cancel
        bot = _Bot().use_recorders()
        await bot._offer_usage_limit_choice(_Ctx("100", images=[str(img)]), "p")
        qid = _qfile_entries()[0]["qid"]
        msg = bot.channels[100].sent[0]
        it = _Interaction(msg)
        await handle(bot, it, "usage_cancel", qid)
        _check("cancel: entry removed", _qfile_entries() == [])
        _check("cancel: queued upload deleted (it never ran)", not img.exists())
        _check("cancel: message becomes 'Cancelled.' with buttons cleared",
               msg.edits == [{"content": "Cancelled.", "view": None}])
        it2 = _Interaction(msg)
        await handle(bot, it2, "usage_cancel", qid)
        _check("cancel again: told it was already resolved (ephemeral)",
               it2.followups == [("This prompt was already resolved.", True)])
        _check("cancel again: message not rewritten", len(msg.edits) == 1)

        # Queue (text)
        bot = _Bot().use_recorders()
        await bot._offer_usage_limit_choice(_Ctx("100"), "later")
        qid = _qfile_entries()[0]["qid"]
        msg = bot.channels[100].sent[0]
        it = _Interaction(msg)
        await handle(bot, it, "usage_queue", qid)
        _check("queue: status flips to queued", _qfile_entries()[0]["status"] == "queued")
        ts = int(WINDOW_END.timestamp())
        _check("queue: message says when it will run, buttons cleared",
               len(msg.edits) == 1 and msg.edits[0]["view"] is None
               and msg.edits[0]["content"].startswith("\u23f8 Queued")
               and msg.edits[0]["content"].endswith(f"will run at <t:{ts}:t>."))
        it2 = _Interaction(msg)
        await handle(bot, it2, "usage_queue", "nope")
        _check("queue unknown qid: already resolved",
               it2.followups == [("This prompt was already resolved.", True)])

        # Queue (callback) names the action
        bot = _Bot().use_recorders()
        await bot._offer_usage_limit_choice_for_callback(_Ctx("100"), "plan", "i1", None)
        qid = _qfile_entries()[0]["qid"]
        msg = bot.channels[100].sent[0]
        await handle(bot, _Interaction(msg), "usage_queue", qid)
        label = interactions_mod.action_label("plan")
        _check("queue callback: message names the action",
               msg.edits[0]["content"].startswith(f"⏸ {label} queued"))

        # Run now (text)
        bot = _Bot().use_recorders()
        await bot._offer_usage_limit_choice(
            _Ctx("100", images=[str(Path(td, "r.png"))]), "go now")
        qid = _qfile_entries()[0]["qid"]
        msg = bot.channels[100].sent[0]
        it = _Interaction(msg)
        await handle(bot, it, "usage_run", qid)
        await _settle()
        _check("run now: entry removed", _qfile_entries() == [])
        _check("run now: message becomes 'Running now' with buttons cleared",
               msg.edits[0]["view"] is None and msg.edits[0]["content"].startswith("Running now"))
        _check("run now: prompt replayed with channel and repo_name",
               [(r[1], r[2], r[3]) for r in bot.replayed]
               == [("100", "go now", "Repo")])
        _check("run now: bypass set for that channel until the window end",
               bot._usage_gate_bypass.get("100") == WINDOW_END)
        out = await bot._offer_usage_limit_choice(_Ctx("100"), "next")
        _check("run now: later prompts in the same thread pass the gate", out is False)
        out = await bot._offer_usage_limit_choice(_Ctx("200"), "next")
        _check("run now: other threads are still gated", out is True)
        it2 = _Interaction(msg)
        await handle(bot, it2, "usage_run", qid)
        await _settle()
        _check("run now twice: second click is 'already resolved', no second replay",
               it2.followups == [("This prompt was already resolved.", True)]
               and len(bot.replayed) == 1)

        # Run now (callback)
        bot = _Bot().use_recorders()
        await bot._offer_usage_limit_choice_for_callback(_Ctx("100"), "build", "i1", "s1")
        qid = _qfile_entries()[0]["qid"]
        await handle(bot, _Interaction(bot.channels[100].sent[0]), "usage_run", qid)
        await _settle()
        _check("run now callback: whole entry handed to _replay_callback",
               len(bot.replayed) == 1 and bot.replayed[0][0] == "cb"
               and bot.replayed[0][1]["action"] == "build"
               and bot.replayed[0][1]["instance_id"] == "i1")

        # Run now: replay failure is contained
        bot = _Bot().use_recorders()
        await bot._offer_usage_limit_choice(_Ctx("100"), "explode")
        bot.replay_raises.add("explode")
        qid = _qfile_entries()[0]["qid"]
        await handle(bot, _Interaction(bot.channels[100].sent[0]), "usage_run", qid)
        await _settle()
        _check("run now: a failing replay is contained in its task", len(bot.replayed) == 1)

        # No message on the interaction is tolerated
        bot = _Bot().use_recorders()
        await bot._offer_usage_limit_choice(_Ctx("100"), "nomsg")
        qid = _qfile_entries()[0]["qid"]
        await handle(bot, _Interaction(None), "usage_cancel", qid)
        _check("cancel with no interaction message still removes the entry",
               _qfile_entries() == [])


async def case_replay_handoff() -> None:
    print("\n== Replays bypass the gate ==")
    seen: dict = {}
    real_on_text = commands_mod.on_text
    real_cb = commands_mod.handle_callback

    async def fake_on_text(ctx, prompt):
        seen["hook"] = ctx.offer_usage_limit_choice
        seen["prompt"] = prompt

    async def fake_cb(ctx, action, instance_id, source_msg_id=None):
        seen["cb"] = (action, instance_id, source_msg_id)
        from bot.engine.commands import _get_channel_lock
        seen["locked"] = _get_channel_lock(ctx.channel_id).locked()
        seen["cb_user"] = (ctx.user_id, ctx.user_name)

    commands_mod.on_text = fake_on_text
    commands_mod.handle_callback = fake_cb
    try:
        bot = _Bot()
        ok = await ClaudeBot._replay_to_thread(bot, "100", "do it", repo_name="Repo")
        await _settle()
        _check("text replay returns True", ok is True)
        _check("text replay clears ctx.offer_usage_limit_choice (no re-prompt loop)",
               seen.get("hook") is None and seen.get("prompt") == "do it")
        _check("text replay resumes the thread's session",
               bot.ctx_calls and bot.ctx_calls[0].get("session_id") == "sess-1")
        seen.clear()
        ok = await ClaudeBot._replay_to_thread(bot, "999", "x")
        _check("text replay for an unknown thread returns False and runs nothing",
               ok is False and "prompt" not in seen)

        bot = _Bot()
        entry = {"channel_id": "100", "action": "plan", "instance_id": "inst-1",
                 "source_msg_id": "m-1", "user_id": "u9", "user_name": "who"}
        await ClaudeBot._replay_callback(bot, entry)
        await _settle()
        _check("callback replay dispatches action, instance and source message",
               seen.get("cb") == ("plan", "inst-1", "m-1"))
        _check("callback replay runs under the channel lock", seen.get("locked") is True)
        _check("callback replay restores the clicking user",
               seen.get("cb_user") == ("u9", "who"))
        _check("callback replay context is tagged source=replay",
               bot.ctx_calls[0].get("source") == "replay")
        seen.clear()
        await ClaudeBot._replay_callback(bot, {**entry, "instance_id": None})
        await ClaudeBot._replay_callback(bot, {**entry, "action": None})
        await ClaudeBot._replay_callback(bot, {**entry, "channel_id": "999"})
        _check("incomplete or orphaned callback entries are dropped", "cb" not in seen)
    finally:
        commands_mod.on_text = real_on_text
        commands_mod.handle_callback = real_cb


# --------------------------------------------------------------------------

_REAL_ACTIVE = usage_notifier.is_usage_limit_active
_REAL_END = usage_notifier.next_window_end_utc


async def main() -> int:
    case_window_functions()
    for case in (
        case_admission, case_persist_before_send, case_send_failures,
        case_bypass, case_callback_gate, case_store_primitives, case_drain,
        case_drain_images, case_startup_and_loop, case_buttons,
        case_replay_handoff,
    ):
        await case()
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} check(s)")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("All usage gate checks passed.")
    return 0


if __name__ == "__main__":
    _real_file = config.USAGE_QUEUE_FILE
    _tmp = tempfile.TemporaryDirectory(prefix="usage_gate_test_")
    config.USAGE_QUEUE_FILE = Path(_tmp.name) / "usage_queue.json"
    try:
        sys.exit(asyncio.run(main()))
    finally:
        config.USAGE_QUEUE_FILE = _real_file
        usage_notifier.is_usage_limit_active = _REAL_ACTIVE
        usage_notifier.next_window_end_utc = _REAL_END
        _tmp.cleanup()
