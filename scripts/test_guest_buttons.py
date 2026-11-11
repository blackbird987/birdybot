"""The owner can turn off every button for one guest.

A guest who only chats (RipKiwi, the owner's mother) was confused by Build &
Ship, Plan, Done, Retry and the rest. access.json now carries "buttons": false
per user, and the Discord sender drops the buttons of every card in a thread
whose family has that guest in it. This pins the file round-trip, the
per-channel answer, and the sender dropping the view.
"""
import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.discord import access as A
from bot.discord import adapter as AD
from bot.discord import thread_ctx as TC
from bot.discord.forums import ThreadInfo
from bot.platform.base import ButtonSpec

fails: list[str] = []


def check(label, got, want):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + label + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        fails.append(label)


OWNER, KIWI, VIN = "7", "1570171819175051374", "1001"

# --- access.json round-trip ---

entry = {
    "user_id": KIWI, "display_name": "RipKiwi",
    "repos": {"huis": {"mode": "build", "max_daily_queries": 40, "bash_policy": "none"}},
    "global_access": False, "forum_channel_id": "500", "welcome_posted": True,
    "control_thread_id": "501", "control_message_id": "501", "archive_thread_id": "502",
}
ua = A.UserAccess.from_dict(entry)
check("absent buttons field means on", ua.buttons, True)
check("an untouched entry writes back unchanged", ua.to_dict(), entry)
ua.buttons = False
d = ua.to_dict()
check("off is written", d.get("buttons"), False)
check("other fields untouched", {k: v for k, v in d.items() if k != "buttons"}, entry)
check("off survives a reload", A.UserAccess.from_dict(d).buttons, False)

with tempfile.TemporaryDirectory() as tmp:
    old_file = A.ACCESS_FILE
    A.ACCESS_FILE = Path(tmp) / "access.json"
    try:
        A.ACCESS_FILE.write_text(json.dumps({"users": {KIWI: entry}}))
        A._invalidate_cache()
        cfg = A.load_access_config()
        cfg.users[KIWI].buttons = False
        A.save_access_config(cfg)
        on_disk = json.loads(A.ACCESS_FILE.read_text())["users"][KIWI]
        check("save keeps buttons=false on disk", on_disk.get("buttons"), False)
        check("save keeps the control room ids", on_disk.get("control_thread_id"), "501")
        check("load reads it back", A.load_access_config().users[KIWI].buttons, False)
    finally:
        A.ACCESS_FILE = old_file
        A._invalidate_cache()

cfg = A.AccessConfig()
cfg.users[KIWI] = A.UserAccess(user_id=KIWI, display_name="RipKiwi", forum_channel_id="500",
                               control_thread_id="501", archive_thread_id="502", buttons=False)
cfg.users[VIN] = A.UserAccess(user_id=VIN, display_name="Vin", forum_channel_id="600")
check("buttons_disabled_for the guest with them off", A.buttons_disabled_for(cfg, KIWI), True)
check("buttons_disabled_for a guest with them on", A.buttons_disabled_for(cfg, VIN), False)
check("buttons_disabled_for an unknown user", A.buttons_disabled_for(cfg, "999"), False)
check("buttons_disabled_for no user", A.buttons_disabled_for(cfg, None), False)

# --- the per-channel answer ---

TC.load_access_config = lambda: cfg


class FakeProject:
    def __init__(self, threads):
        self.threads = threads


class FakeForums:
    def __init__(self, threads):
        self.forum_projects = {"huis": FakeProject(threads)}

    def thread_to_project(self, tid):
        for proj in self.forum_projects.values():
            if tid in proj.threads:
                return proj, proj.threads[tid]
        return None


class FakeChannel:
    def __init__(self, cid, parent_id=None):
        self.id = int(cid)
        self.parent_id = int(parent_id) if parent_id else None
        self.sent = []
        self.messages = {}

    async def send(self, **kw):
        self.sent.append(kw)
        msg = FakeMessage(len(self.sent))
        self.messages[str(msg.id)] = msg
        return msg

    async def fetch_message(self, mid):
        return self.messages.setdefault(str(mid), FakeMessage(mid))


class FakeMessage:
    def __init__(self, mid):
        self.id = int(mid)
        self.embeds = []
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)


threads = {
    "100": ThreadInfo(thread_id="100", user_id=KIWI),                          # her thread, shared repo forum
    "101": ThreadInfo(thread_id="101", parent_thread_id="100"),                # spawned from hers
    "200": ThreadInfo(thread_id="200"),                                        # the owner's own
    "201": ThreadInfo(thread_id="201", user_ids={OWNER, KIWI}),                # she joined the owner's
    "300": ThreadInfo(thread_id="300", user_id=VIN),                           # a guest with buttons on
}
channels = {
    "100": FakeChannel("100", "900"), "101": FakeChannel("101", "900"),
    "200": FakeChannel("200", "900"), "201": FakeChannel("201", "900"),
    "300": FakeChannel("300", "900"), "550": FakeChannel("550", "500"),
    "650": FakeChannel("650", "600"),
}


class FakeBot(TC.ThreadContextMixin):
    _discord_user_id = int(OWNER)
    _forums = FakeForums(threads)

    def get_channel(self, cid):
        return channels.get(str(cid))


bot = FakeBot()
hidden = bot._buttons_hidden
check("her thread in the shared repo forum hides", hidden("100"), True)
check("a child of her thread hides", hidden("101"), True)
check("the owner's own thread keeps buttons", hidden("200"), False)
check("an owner thread she posted in hides", hidden("201"), True)
check("a guest with buttons on keeps them", hidden("300"), False)
check("a thread in her personal forum hides", hidden("550"), True)
check("her control room hides", hidden("501"), True)
check("her archive hides", hidden("502"), True)
check("a thread in another guest's forum keeps them", hidden("650"), False)
check("an unmapped channel keeps them", hidden("999"), False)
check("no channel keeps them", hidden(None), False)
check("garbage id fails open", hidden("not-a-number"), False)

# --- the sender ---

messenger = AD.DiscordMessenger(bot)
rows = [[ButtonSpec("Done", "done:t-1"), ButtonSpec("Retry", "retry:t-1")]]


async def sender_checks():
    await messenger.send_result("100", "hi", buttons=rows)
    check("send_result in her thread carries no view", channels["100"].sent[-1]["view"], None)
    await messenger.send_result("200", "hi", buttons=rows)
    check("send_result in the owner's thread keeps the view",
          channels["200"].sent[-1]["view"] is not None, True)
    await messenger.send_text("101", "hi", buttons=rows)
    check("send_text in her child thread carries no view", channels["101"].sent[-1]["view"], None)
    await messenger.edit_text("100", "1", None, buttons=rows)
    check("edit_text in her thread clears the view",
          channels["100"].messages["1"].edits[-1], {"view": None})
    await messenger.edit_text("200", "1", None, buttons=rows)
    check("edit_text in the owner's thread keeps the view",
          channels["200"].messages["1"].edits[-1]["view"] is not None, True)
    handle = await messenger.send_thinking("100", "working", buttons=rows)
    check("send_thinking in her thread carries no view", channels["100"].sent[-1]["view"], None)
    await messenger.edit_thinking(handle, "still working", buttons=rows)
    edited = channels["100"].messages[handle.get("message_id")].edits[-1]
    check("edit_thinking in her thread clears the view", edited["view"], None)


asyncio.run(sender_checks())

# A bot without the check (or one that raises) shows the buttons.
class Raising(FakeBot):
    def _buttons_hidden(self, cid):
        raise RuntimeError("boom")


check("a raising check fails open", AD.DiscordMessenger(Raising())._shown("100", rows), rows)

# --- clearing buttons already posted ---


class Card(FakeMessage):
    def __init__(self, mid, author, components):
        super().__init__(mid)
        self.author = type("U", (), {"id": author})()
        self.components = components


class SleepyThread(FakeChannel):
    def __init__(self, cid, archived, history):
        super().__init__(cid, "900")
        self.archived = archived
        self.thread_edits = []
        self._history = history
        self.history_limits = []

    async def edit(self, **kw):
        self.thread_edits.append(kw)

    async def history(self, limit=None):
        self.history_limits.append(limit)
        for m in self._history[:limit]:
            yield m


async def strip_checks():
    BOT_ID, OTHER = 42, 9
    t100 = SleepyThread("100", archived=True, history=[
        Card(1, BOT_ID, ["row"]),       # a result card
        Card(2, OTHER, ["row"]),        # someone else's message
        Card(3, BOT_ID, []),            # already bare
        Card(100, BOT_ID, ["row"]),     # the starter post
    ])
    t101 = SleepyThread("101", archived=False, history=[Card(4, BOT_ID, ["row"])])
    t201 = SleepyThread("201", archived=False, history=[Card(5, BOT_ID, [])])
    t200 = SleepyThread("200", archived=False, history=[Card(6, BOT_ID, ["row"])])
    t300 = SleepyThread("300", archived=False, history=[Card(7, BOT_ID, ["row"])])
    by_id = {"100": t100, "101": t101, "201": t201, "200": t200, "300": t300}

    class StripBot(FakeBot):
        user = type("U", (), {"id": BOT_ID})()

        def get_channel(self, cid):
            return by_id.get(str(cid)) or channels.get(str(cid))

        async def fetch_channel(self, cid):
            return self.get_channel(cid)

    n = await StripBot().strip_hidden_buttons()
    check("strip edits every bot card that still had buttons in her threads", n, 3)
    check("strip cleared her result card and starter",
          [m.edits for m in t100._history], [[{"view": None}], [], [], [{"view": None}]])
    check("strip cleared the child thread's card", t101._history[0].edits, [{"view": None}])
    check("strip woke the archived thread and put it back",
          t100.thread_edits, [{"archived": False}, {"archived": True}])
    check("strip looks back a bounded distance", t100.history_limits, [StripBot.STRIP_HISTORY_LIMIT])
    check("strip left a thread with nothing to clear asleep and untouched",
          (t201.thread_edits, t201._history[0].edits), ([], []))
    check("strip left the owner's thread alone", t200._history[0].edits, [])
    check("strip left the guest with buttons on alone", t300._history[0].edits, [])

    saved = TC.load_access_config
    TC.load_access_config = lambda: A.AccessConfig()
    try:
        check("strip does nothing when nobody has buttons off", await StripBot().strip_hidden_buttons(), 0)
    finally:
        TC.load_access_config = saved


asyncio.run(strip_checks())

# --- the usage gate never waits on a tap she cannot make ---

from bot import config
from bot.discord import usage_notifier
from bot.discord.usage_gate import UsageGateMixin
from bot.platform.base import RequestContext


class GateBot(UsageGateMixin, FakeBot):
    _usage_gate_bypass: dict = {}
    appended: list = []

    async def _usage_queue_append(self, entry):
        self.appended.append(entry)

    async def _usage_queue_update(self, qid, **kw):
        pass


async def gate_checks():
    saved = (config.USAGE_PEAK_GATE_ENABLED, usage_notifier.is_usage_limit_active)
    config.USAGE_PEAK_GATE_ENABLED = True
    usage_notifier.is_usage_limit_active = lambda: True
    try:
        gb = GateBot()
        ctx = RequestContext(messenger=None, channel_id="100", platform="discord", store=None, runner=None)
        handled = await gb._offer_gate(ctx, entry_extras={"type": "text"}, label="this prompt")
        check("gate in her thread lets the turn run", handled, False)
        check("gate in her thread queues nothing", gb.appended, [])
    finally:
        config.USAGE_PEAK_GATE_ENABLED, usage_notifier.is_usage_limit_active = saved


asyncio.run(gate_checks())

print()
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
