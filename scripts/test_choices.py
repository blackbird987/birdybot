#!/usr/bin/env python3
"""One-tap choices: the /choices directive, its row, and what a tap does.

Pins the invariants in docs/design/choices.md:

  * parsing: masked scan, quoted examples, refusals (length, command-like,
    directive syntax), dedupe, 2..5 labels, no arguments, a body required,
    suppressed by a running chain or a reply that also hands off;
  * the row budget: the choice row goes first and a full card stays within
    five rows by dropping Mode, then Missed, then Branch; Expand and Collapse
    are never the ones that go; /list-style specs never carry a row;
  * redraws keep the row only for the offer's own instance and message;
  * claiming: a double tap, a stale tap, a tap on another card or past the
    week all find nothing; the offer survives a StateStore round trip and
    expires;
  * closing strips the row through ``strip_button_rows`` and logs why, and
    both turn starts (``_execute_query`` and ``run_instance``) close before
    ``begin_task``;
  * the tap path: ``_handle_choice_tap`` calls ``commands.on_text`` (never
    ``_replay_to_thread``) with ``source="choice_tap"``, the tapper's access
    and the tapper tracked in the thread's user_ids; ``on_text`` then runs the
    usage gate, resets the wave cap, skips fast repo commands, queues behind a
    running turn with the source kept, and the budget gate refuses a tap the
    same way it refuses typed text;
  * ``send_result`` returns the button message id only when the row is shown,
    a buttons-hidden channel records no offer, the chip vanishes, the eval
    check fires on an unoffered either/or.

Run: python scripts/test_choices.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import inspect
import json
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config  # noqa: E402
from bot.claude.types import (  # noqa: E402
    Instance, InstanceOrigin, InstanceStatus, InstanceType,
)
from bot.engine import cmd_state, commands, lifecycle, query  # noqa: E402
from bot.engine import pending as pending_mod  # noqa: E402
from bot.engine.directives import choices  # noqa: E402
from bot.platform.base import HUMAN_SOURCES, RequestContext  # noqa: E402
from bot.platform.buttons import (  # noqa: E402
    MAX_ROWS, action_button_specs, expanded_button_specs,
)
from bot.platform.formatting import collapse_bot_directives  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

FAILS: list[str] = []
PASSES = 0


def _check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(f"{name}: {detail}" if detail else name)
        print(f"FAIL {name} {detail}")


def _reply(*labels: str, extra: str = "") -> str:
    body = "\n".join(labels)
    return f"Keep it, or rename it?\n\n[BOT_CMD: /choices]\n~~~choices\n{body}\n~~~\n{extra}"


def _inst(iid: str = "q-1", **kw) -> Instance:
    base = dict(
        id=iid, name=None, instance_type=InstanceType.QUERY, prompt="p",
        repo_name="bot", repo_path="/tmp", status=InstanceStatus.COMPLETED,
        session_id="sess-1",
    )
    base.update(kw)
    return Instance(**base)


class _Messenger:
    def __init__(self, shows: bool | None = True) -> None:
        self.sent: list[tuple[str, object]] = []
        self.stripped: list[tuple[str, str, str]] = []
        self._next = 100
        if shows is None:
            # A messenger that predates shows_buttons (a platform without it).
            self.shows_buttons = None  # type: ignore[assignment]
        else:
            self._shows = shows

    def shows_buttons(self, _cid):  # noqa: ANN001
        return self._shows

    async def strip_button_rows(self, cid, msg_id, prefix):  # noqa: ANN001
        self.stripped.append((cid, str(msg_id), prefix))

    def _id(self) -> str:
        self._next += 1
        return str(self._next)

    async def send_text(self, _cid, text, buttons=None, silent=False, **_kw):  # noqa: ANN001
        self.sent.append((text, buttons))
        return self._id()

    async def send_result(self, _cid, text, metadata=None, buttons=None, **_kw):  # noqa: ANN001
        self.sent.append((text, buttons))
        return self._id()

    def markdown_to_markup(self, t):  # noqa: ANN001
        return t

    def chunk_message(self, t):  # noqa: ANN001
        return [t]

    def format_mention(self, uid):  # noqa: ANN001
        return f"<@{uid}>"

    async def is_conversation_closed(self, _cid):  # noqa: ANN001
        return False


class _Runner:
    def __init__(self) -> None:
        self.begun: list[str] = []

    def begin_task(self, iid, *_a, **_kw):  # noqa: ANN001
        self.begun.append(iid)

    def end_task(self, *_a, **_kw):
        pass

    def check_spawn_allowed(self):
        return None

    is_draining = False

    def active_instance_for_session(self, _sid):  # noqa: ANN001
        return "q-run"

    class provider:  # noqa: N801
        supports_steer = False


def _store(tmp: Path) -> StateStore:
    return StateStore(tmp / "state.json", tmp / "results")


def _ctx(store, messenger=None, *, source="user_message", cid="chan-1") -> RequestContext:
    return RequestContext(
        messenger=messenger or _Messenger(), channel_id=cid, platform="discord",
        store=store, runner=_Runner(), session_id="sess-1", repo_name="bot",
        source=source,
    )


def _events(tmp: Path) -> list[dict]:
    p = tmp / "choices.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _ids(rows) -> list[list[str]]:  # noqa: ANN001
    return [[b.callback_data for b in r] for r in rows]


# --- parsing ---------------------------------------------------------------


def case_parse() -> None:
    p = choices.parse_choices(_reply("Keep it", "Rename it"))
    _check("parse basic", p.labels == ["Keep it", "Rename it"] and not p.notices, repr(p))

    p = choices.parse_choices(_reply("A", "a", "B", "", "  C  "))
    _check("parse dedupe and strip", p.labels == ["A", "B", "C"], repr(p.labels))

    p = choices.parse_choices(_reply("x" * 41, "/merge", "!go", "has [BOT_CMD: /x]", "a ~~~ b", "Ok", "Fine"))
    _check("parse refusals", p.labels == ["Ok", "Fine"] and len(p.refused) == 5, repr(p))
    _check("parse refusals noticed", sum("left out" in n for n in p.notices) == 5, repr(p.notices))

    p = choices.parse_choices(_reply("1", "2", "3", "4", "5", "6", "7"))
    _check("parse keeps first five", p.labels == ["1", "2", "3", "4", "5"], repr(p.labels))
    _check("parse notes the cut", any("first 5" in n for n in p.notices), repr(p.notices))

    p = choices.parse_choices(_reply("Only one"))
    _check("parse one label is no offer", p.labels == [] and p.suppressed, repr(p))

    p = choices.parse_choices(_reply("x" * 50, "Only one"))
    _check("parse refusal leaving one is no offer",
           p.labels == [] and any("Fewer than two" in n for n in p.notices), repr(p))

    p = choices.parse_choices("Q?\n[BOT_CMD: /choices style=big]\n~~~choices\nA\nB\n~~~")
    _check("parse arguments refused", p.labels == [] and p.suppressed == "arguments", repr(p))

    p = choices.parse_choices("Q?\n[BOT_CMD: /choices]\n")
    _check("parse no body", p.labels == [] and p.suppressed == "no body", repr(p))

    two = _reply("A", "B") + "\n[BOT_CMD: /choices]\n~~~choices\nC\nD\n~~~\n"
    p = choices.parse_choices(two)
    _check("parse first of two", p.labels == ["A", "B"] and any("first of 2" in n for n in p.notices), repr(p))

    nested = (
        "Spawning.\n\n[BOT_CMD: /x]\n~~~spawn\nWhen done, end with:\n"
        "[BOT_CMD: /choices]\n~~~choices\nA\nB\n~~~\n~~~\n"
    )
    p = choices.parse_choices(nested)
    _check("parse nested inside a tilde body never arms", p.labels == [] and not p.notices, repr(p))

    quoted = "Use it like `[BOT_CMD: /choices]` then a block.\n> [BOT_CMD: /choices]\n"
    p = choices.parse_choices(quoted)
    _check("parse quoted example skipped", p.labels == [] and p.suppressed is None, repr(p))

    p = choices.parse_choices(_reply("A", "B"), chain_active=True)
    _check("parse suppressed by running chain", p.labels == [] and p.suppressed == "chain running", repr(p))

    for verb in ("chain preset=ship", "spawn repo=bot title=\"x\""):
        p = choices.parse_choices(_reply("A", "B", extra=f"\n[BOT_CMD: /{verb}]\n~~~plan\nx\n~~~\n"))
        _check(f"parse suppressed by /{verb.split()[0]}", p.labels == [] and p.suppressed, repr(p))

    p = choices.parse_choices(_reply("token sk-ant-api03-" + "a" * 30, "B"))
    _check("parse redacts secrets", all("a" * 30 not in lab for lab in p.labels), repr(p.labels))

    _check("chip vanishes", collapse_bot_directives(_reply("A", "B")).strip() == "Keep it, or rename it?",
           repr(collapse_bot_directives(_reply("A", "B"))))


# --- rows ------------------------------------------------------------------


def case_rows() -> None:
    labels = ["Keep it", "Rename it", "Ask later"]
    # Fullest default card: New/Retry, Plan/Build/Done, Continue Autopilot,
    # Mode, Branch, and with show_expand the Expand row.
    inst = _inst()
    for kw in ({}, {"show_expand": True}, {"has_autopilot_chain": True},
               {"show_expand": True, "has_autopilot_chain": True}):
        rows = action_button_specs(inst, choices=labels, **kw)
        ids = _ids(rows)
        _check(f"row budget {kw}", len(rows) <= MAX_ROWS, repr(ids))
        _check(f"choice row first {kw}", ids[0] == ["choice:q-1:0", "choice:q-1:1", "choice:q-1:2"], repr(ids))
        _check(f"workflow row kept {kw}", any("plan:q-1" in r for r in ids), repr(ids))
        if kw.get("show_expand"):
            _check(f"expand kept {kw}", any("expand:q-1" in r for r in ids), repr(ids))
        plain = action_button_specs(inst, **kw)
        if len(plain) + 1 > MAX_ROWS:
            _check(f"mode dropped first {kw}",
                   not any(c.startswith("mode_") for r in ids for c in r), repr(ids))
        else:
            _check(f"nothing dropped when it fits {kw}", len(rows) == len(plain) + 1, repr(ids))
        _check(f"no choices, no row {kw}",
               not any(c.startswith("choice:") for r in _ids(plain) for c in r), repr(_ids(plain)))

    rows = expanded_button_specs(inst, choices=labels)
    ids = _ids(rows)
    _check("expanded within budget", len(rows) <= MAX_ROWS, repr(ids))
    _check("expanded keeps collapse last", ids[-1][0] == "collapse:q-1", repr(ids))
    _check("expanded choice row first", ids[0][0] == "choice:q-1:0", repr(ids))

    # Card that fits: nothing dropped.
    small = _inst(session_id=None)
    rows = action_button_specs(small, choices=labels)
    _check("small card keeps mode", any(c.startswith("mode_") for r in _ids(rows) for c in r), repr(_ids(rows)))

    # Failed card: send_result clears choices, and the specs never add one
    # unless asked, so a /list redraw (no choices=) never shows a row.
    failed = _inst(status=InstanceStatus.FAILED)
    _check("failed card no row", not any(
        c.startswith("choice:") for r in _ids(action_button_specs(failed)) for c in r))

    # Structural: the only callers passing choices= are the redraws of the
    # card itself and send_result. /list and the instance commands never do.
    root = Path(__file__).resolve().parents[1] / "bot"
    allowed = {"engine/callbacks.py", "engine/branch_actions.py", "engine/lifecycle.py",
               "engine/query.py", "platform/buttons.py"}
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("engine/directives/"):
            continue
        if "choices=" in path.read_text(encoding="utf-8") and rel not in allowed:
            offenders.append(rel)
    _check("choices= only on the card's own redraws", not offenders, repr(offenders))
    src = (root / "engine" / "instance_cmds.py").read_text(encoding="utf-8")
    _check("/list passes no choices", "choices=" not in src)


# --- store, offer, claim, close, expire -------------------------------------


def case_offer_lifecycle(tmp: Path) -> None:
    store = _store(tmp)
    m = _Messenger()
    ctx = _ctx(store, m)
    inst = _inst()
    choices.open_offer(ctx, inst, ["A", "B"], "555")
    _check("offer stored", (store.get_choice_offer("chan-1") or {}).get("msg_id") == "555")
    _check("offered logged", any(e["event"] == "offered" for e in _events(tmp)))

    # Round trip through a fresh StateStore on the same file.
    again = _store(tmp)
    off = again.get_choice_offer("chan-1")
    _check("offer survives restart", off is not None and off["labels"] == ["A", "B"], repr(off))

    # Redraws: only the offer's own instance and message.
    _check("redraw own card", choices.offer_labels_for(store, "chan-1", "q-1", "555") == ["A", "B"])
    _check("redraw other msg", choices.offer_labels_for(store, "chan-1", "q-1", "556") is None)
    _check("redraw other instance", choices.offer_labels_for(store, "chan-1", "q-2", "555") is None)
    _check("redraw no msg", choices.offer_labels_for(store, "chan-1", "q-1", None) is None)
    _check("redraw other channel", choices.offer_labels_for(store, "chan-2", "q-1", "555") is None)

    # Claim guards.
    _check("tap other card", choices.claim_for_tap(store, "chan-1", "q-1", 0, msg_id="999") == (None, None))
    _check("tap other instance", choices.claim_for_tap(store, "chan-1", "q-0", 0, msg_id="555") == (None, None))
    _check("tap out of range", choices.claim_for_tap(store, "chan-1", "q-1", 2, msg_id="555") == (None, None))
    _check("tap bad index", choices.claim_for_tap(store, "chan-1", "q-1", -1, msg_id="555") == (None, None))
    offer, label = choices.claim_for_tap(store, "chan-1", "q-1", 1, msg_id="555")
    _check("tap claims", label == "B" and offer is not None)
    _check("tap pops", store.get_choice_offer("chan-1") is None)
    _check("double tap finds nothing", choices.claim_for_tap(store, "chan-1", "q-1", 0, msg_id="555") == (None, None))
    _check("claim persisted", _store(tmp).get_choice_offer("chan-1") is None)

    # TTL on the tap.
    choices.open_offer(ctx, inst, ["A", "B"], "600")
    off = store.get_choice_offer("chan-1")
    off["offered_at"] = time.time() - choices.OFFER_TTL_SECONDS - 5
    _check("stale tap past a week", choices.claim_for_tap(store, "chan-1", "q-1", 0, msg_id="600") == (None, None))

    # Expiry sweep strips and logs; a fresh offer stays.
    ctx2 = _ctx(store, m, cid="chan-2")
    choices.open_offer(ctx2, inst, ["C", "D"], "700")
    n = asyncio.run(choices.expire_stale(store, m))
    _check("expire one", n == 1 and store.get_choice_offer("chan-1") is None
           and store.get_choice_offer("chan-2") is not None, f"n={n}")
    _check("expire strips", ("chan-1", "600", "choice:") in m.stripped, repr(m.stripped))
    _check("expired logged", any(e["event"] == "expired" for e in _events(tmp)))

    # close_offer: typed vs superseded, strips the card, no-op when none.
    asyncio.run(choices.close_offer(ctx2, "chan-2", choices.close_reason("user_message")))
    _check("close typed strips", ("chan-2", "700", "choice:") in m.stripped, repr(m.stripped))
    _check("close typed logged", _events(tmp)[-1]["event"] == "typed", repr(_events(tmp)[-1]))
    choices.open_offer(ctx2, inst, ["C", "D"], "701")
    asyncio.run(choices.close_offer(ctx2, "chan-2", choices.close_reason("schedule")))
    last = _events(tmp)[-1]
    _check("close superseded logged", last["event"] == "superseded" and last["detail"] == "schedule", repr(last))
    before = len(m.stripped)
    asyncio.run(choices.close_offer(ctx2, "chan-2", "typed"))
    _check("close with no offer is a no-op", len(m.stripped) == before)
    _check("choice_tap closes as superseded, the tap already claimed it",
           choices.close_reason("choice_tap") == "superseded:choice_tap")

    # A store with no offer methods (a fake) is a no-op everywhere.
    bare = types.SimpleNamespace()
    asyncio.run(choices.close_offer(_ctx(bare), "chan-1", "typed"))
    _check("fails open without store support", choices.claim_for_tap(bare, "c", "i", 0) == (None, None))


def case_prepare_and_send(tmp: Path) -> None:
    store = _store(tmp)
    inst = _inst()

    m = _Messenger(shows=False)
    labels = asyncio.run(choices.prepare(_ctx(store, m), inst, _reply("A", "B")))
    _check("buttons hidden: no labels", labels is None)
    _check("buttons hidden logged", any(
        e["event"] == "suppressed" and e.get("reason") == "buttons hidden" for e in _events(tmp)))

    m = _Messenger(shows=None)
    _check("no shows_buttons: no labels",
           asyncio.run(choices.prepare(_ctx(store, m), inst, _reply("A", "B"))) is None)

    m = _Messenger()
    labels = asyncio.run(choices.prepare(_ctx(store, m), inst, _reply("A", "B", "x" * 60)))
    _check("prepare labels", labels == ["A", "B"], repr(labels))
    _check("prepare posts notice before the card", any("left out" in str(t) for t, _ in m.sent), repr(m.sent))

    # A running chain on the session suppresses.
    store.set_autopilot_chain("sess-1", ["review_code", "verify"])
    _check("chain suppresses", asyncio.run(
        choices.prepare(_ctx(store, _Messenger()), inst, _reply("A", "B"))) is None)
    store.clear_autopilot_chain("sess-1")

    # send_result: id only when the row is on the card.
    store2 = _store(tmp / "s2")
    m = _Messenger()
    ctx = _ctx(store2, m)
    got = asyncio.run(lifecycle.send_result(ctx, _inst(), "Keep it?", choices=["A", "B"]))
    _check("send_result returns button msg", got == "101", repr(got))
    rows = m.sent[-1][1]
    _check("send_result card has row", rows and rows[0][0].callback_data == "choice:q-1:0", repr(rows))
    got = asyncio.run(lifecycle.send_result(ctx, _inst(), "Keep it?"))
    _check("send_result without choices returns None", got is None, repr(got))
    got = asyncio.run(lifecycle.send_result(
        ctx, _inst(status=InstanceStatus.FAILED, error="x"), "", choices=["A", "B"]))
    _check("send_result failed card no offer", got is None, repr(got))
    rows = m.sent[-1][1] or []
    _check("failed card has no choice row", not any(
        b.callback_data.startswith("choice:") for r in rows for b in r), repr(rows))
    long_text = "x " * (config.RESULT_INLINE_MAX + 10)
    got = asyncio.run(lifecycle.send_result(ctx, _inst(), long_text, choices=["A", "B"]))
    rows = m.sent[-1][1]
    _check("collapsed card returns id", got is not None, repr(got))
    _check("collapsed card keeps expand and row", rows[0][0].callback_data.startswith("choice:")
           and any(b.callback_data.startswith("expand:") for r in rows for b in r), repr(rows))


# --- the gated path: on_text with source=choice_tap ------------------------


def case_gates(tmp: Path) -> None:
    store = _store(tmp)
    calls: dict[str, list] = {"gate": [], "run": [], "reset": [], "exec": []}

    async def gate(c, text):  # noqa: ANN001
        calls["gate"].append((c.source, text))
        return False

    orig_run = query._run_query

    async def fake_run(c, text, *, fast_repo_commands=False):  # noqa: ANN001
        calls["run"].append(fast_repo_commands)

    query._run_query = fake_run
    try:
        for src in ("choice_tap", "user_message"):
            ctx = _ctx(store, source=src)
            ctx.offer_usage_limit_choice = gate
            ctx.reset_spawn_wave_count = lambda: calls["reset"].append(1)
            asyncio.run(query.on_text(ctx, "User picked: A"))
    finally:
        query._run_query = orig_run
    _check("usage gate consulted for a tap", calls["gate"][0] == ("choice_tap", "User picked: A"), repr(calls))
    _check("tap skips fast repo commands", calls["run"] == [False, True], repr(calls["run"]))
    _check("tap resets the wave cap", len(calls["reset"]) == 2, repr(calls["reset"]))
    _check("choice_tap is human", "choice_tap" in HUMAN_SOURCES)

    # Gate that holds the tap: nothing runs.
    calls["run"].clear()

    async def holding(c, text):  # noqa: ANN001
        return True

    query._run_query = fake_run
    try:
        ctx = _ctx(store, source="choice_tap")
        ctx.offer_usage_limit_choice = holding
        asyncio.run(query.on_text(ctx, "User picked: A"))
    finally:
        query._run_query = orig_run
    _check("usage gate holds a tap", calls["run"] == [], repr(calls["run"]))

    # Budget gate: a tap is refused exactly like typed text, through the
    # real _execute_query, with nothing spawned.
    orig_budget = cmd_state.check_budget
    orig_enforce = config.COST_GATES_ENFORCE
    texts: dict[str, list[str]] = {}
    try:
        cmd_state.check_budget = lambda _ctx: False
        for src in ("choice_tap", "user_message"):
            m = _Messenger()
            ctx = _ctx(store, m, source=src, cid=f"budget-{src}")
            asyncio.run(query.on_text(ctx, "User picked: A"))
            texts[src] = [str(t) for t, _ in m.sent]
            _check(f"budget refused {src} spawned nothing", ctx.runner.begun == [], repr(ctx.runner.begun))
    finally:
        cmd_state.check_budget = orig_budget
        config.COST_GATES_ENFORCE = orig_enforce
    _check("budget refuses a tap", any("budget" in t.lower() for t in texts["choice_tap"]), repr(texts))
    _check("budget refuses a tap like typed text", texts["choice_tap"] == texts["user_message"], repr(texts))

    # A tap during a run queues behind the lock with the Queued card, and the
    # pending entry keeps source=choice_tap for a Steer.
    registered: list[dict] = []
    orig_reg = pending_mod.register
    orig_exec = query._execute_query

    def rec_register(*a, **kw):  # noqa: ANN002, ANN003
        registered.append(kw)
        return orig_reg(*a, **kw)

    async def rec_exec(c, prompt):  # noqa: ANN001
        calls["exec"].append((c.source, prompt))

    async def during_run() -> list[str]:
        m = _Messenger()
        ctx = _ctx(store, m, source="choice_tap", cid="busy-1")
        lock = query._get_channel_lock("busy-1")
        await lock.acquire()
        task = asyncio.create_task(query.on_text(ctx, "User picked: B"))
        for _ in range(20):
            await asyncio.sleep(0)
        queued = [str(t) for t, _ in m.sent]
        lock.release()
        await task
        return queued

    orig_pending_file = config.PENDING_PROMPTS_FILE
    config.PENDING_PROMPTS_FILE = tmp / "pending.json"
    pending_mod.register = rec_register
    query._execute_query = rec_exec
    try:
        queued = asyncio.run(during_run())
    finally:
        pending_mod.register = orig_reg
        query._execute_query = orig_exec
        config.PENDING_PROMPTS_FILE = orig_pending_file
    _check("tap during a run shows Queued", any("Queued" in t for t in queued), repr(queued))
    _check("queued tap keeps its source", registered and registered[0].get("source") == "choice_tap", repr(registered))
    _check("queued tap runs after the lock", calls["exec"] == [("choice_tap", "User picked: B")], repr(calls["exec"]))

    p = pending_mod.PendingPrompt.from_json(
        {**pending_mod.PendingPrompt(
            id="x", channel_id="c", session_id=None, prompt_text="t", message_id="1",
            active_instance_id=None, created_at=0.0, source="choice_tap").to_json()})
    _check("pending source round trip", p.source == "choice_tap", repr(p))


# --- the tap handler -------------------------------------------------------


def case_tap_handler(tmp: Path) -> None:
    from bot.discord import interactions
    from bot.discord.bot import ClaudeBot

    store = _store(tmp)
    ran: list[tuple[str, str, bool, str | None]] = []
    stripped: list[int] = []
    followups: list[tuple[str, bool]] = []
    posted: list[str] = []
    saved: list[int] = []

    info = types.SimpleNamespace(session_id="sess-1", user_ids=set())
    proj = types.SimpleNamespace(repo_name="bot")

    class _Forums:
        def thread_to_project(self, _cid):  # noqa: ANN001
            return (proj, info)

        def attach_session_callbacks(self, *_a):
            pass

        def save_forum_map(self):
            saved.append(1)

    class _Bot:
        _store = store
        _forums = _Forums()
        _track_thread_user = ClaudeBot._track_thread_user

        def _ctx(self, cid, session_id=None, repo_name=None, thread_info=None,  # noqa: ANN001
                 access_result=None, source="system"):
            c = _ctx(store, source=source, cid=cid)
            c.session_id = session_id
            c.repo_name = repo_name
            c.is_owner = access_result.is_owner
            return c

        def _cancel_sleep(self, _cid):  # noqa: ANN001
            pass

        async def _clear_thread_sleeping(self, _ch):  # noqa: ANN001
            pass

        async def _set_thread_active_tag(self, _ch, _on):  # noqa: ANN001
            pass

        async def _refresh_dashboard(self):
            pass

        async def _after_turn(self, _ctx, _cid):  # noqa: ANN001
            pass

        async def _replay_to_thread(self, *_a, **_kw):
            raise AssertionError("a tap must not replay")

    class _Resp:
        async def defer(self):
            pass

    class _Followup:
        async def send(self, text, ephemeral=False):  # noqa: ANN001
            followups.append((text, ephemeral))

    class _Channel:
        async def send(self, text, **_kw):  # noqa: ANN001
            posted.append(text)

    def interaction(msg_id: int, uid: int = 42):
        return types.SimpleNamespace(
            response=_Resp(), followup=_Followup(), channel=_Channel(),
            channel_id=777, message=types.SimpleNamespace(id=msg_id),
            user=types.SimpleNamespace(id=uid, display_name="Guest*"),
        )

    async def fake_on_text(c, text):  # noqa: ANN001
        ran.append((c.source, text, c.is_owner, c.user_id))

    async def fake_strip(_i):  # noqa: ANN001
        stripped.append(1)

    access = types.SimpleNamespace(is_owner=False, allowed=True)
    orig_on_text = commands.on_text
    orig_strip = interactions._strip_choice_row
    commands.on_text = fake_on_text
    interactions._strip_choice_row = fake_strip
    try:
        ctx = _ctx(store, cid="777")
        choices.open_offer(ctx, _inst(), ["Keep it", "Rename it"], "900")

        async def two_taps():
            await asyncio.gather(
                interactions._handle_choice_tap(_Bot(), interaction(900), "q-1:1", access),
                interactions._handle_choice_tap(_Bot(), interaction(900), "q-1:0", access),
            )

        asyncio.run(two_taps())
        _check("tap runs once on a double tap", len(ran) == 1, repr(ran))
        _check("tap sends label through on_text as choice_tap",
               ran and ran[0][:2] == ("choice_tap", "User picked: Rename it"), repr(ran))
        _check("tap runs with the tapper's access", ran and ran[0][2] is False and ran[0][3] == "42", repr(ran))
        _check("tap tracks the tapper in user_ids", info.user_ids == {"42"} and saved == [1],
               f"{info.user_ids} {saved}")
        _check("second tap gets the stale line",
               followups == [(choices.STALE_TAP_TEXT, True)], repr(followups))
        _check("both taps strip the row", len(stripped) == 2, repr(stripped))
        _check("picked line posted, escaped", posted == ["Guest\\* picked: **Rename it**"], repr(posted))
        _check("tapped logged", any(e["event"] == "tapped" and e["index"] == 1 for e in _events(tmp)))

        # A tap on another card with an offer open elsewhere.
        ran.clear()
        followups.clear()
        choices.open_offer(ctx, _inst(), ["Keep it", "Rename it"], "901")
        asyncio.run(interactions._handle_choice_tap(_Bot(), interaction(900), "q-1:0", access))
        _check("tap on an old card is stale", not ran and followups, repr(followups))
        _check("old-card tap leaves the open offer", store.get_choice_offer("777") is not None)
        asyncio.run(interactions._handle_choice_tap(_Bot(), interaction(901), "q-1:0", access))
        _check("tap on the live card runs", ran and ran[0][1] == "User picked: Keep it", repr(ran))
        _check("a repeat tracker save is skipped", saved == [1], repr(saved))
    finally:
        commands.on_text = orig_on_text
        interactions._strip_choice_row = orig_strip

    # Structural: the router sends choice to the tap handler ahead of the
    # owner check, and the handler never replays.
    src = inspect.getsource(interactions.handle)
    i_choice = src.find('action == "choice"')
    i_owner = src.find("_OWNER_ONLY_ACTIONS")
    _check("router dispatches choice before the owner gate", 0 < i_choice < i_owner, f"{i_choice} {i_owner}")
    tap_src = inspect.getsource(interactions._handle_choice_tap)
    _check("tap never replays", "_replay_to_thread" not in tap_src and "commands.on_text" in tap_src)
    _check("tap ctx is choice_tap", 'source="choice_tap"' in tap_src)
    on_msg = inspect.getsource(ClaudeBot._route_screened)
    _check("typed messages share the user tracker", "_track_thread_user(info, message.author.id)" in on_msg)


# --- structural: both turn starts close before begin_task -------------------


def case_turn_starts() -> None:
    for name, fn in (("_execute_query", query._execute_query), ("run_instance", lifecycle.run_instance)):
        src = inspect.getsource(fn)
        i_close = src.find("close_offer(")
        i_begin = src.find("begin_task(")
        _check(f"{name} closes before begin_task", 0 < i_close < i_begin, f"{i_close} {i_begin}")
    src = inspect.getsource(lifecycle.run_instance)
    _check("run_instance skips not-a-turn origins", "_NOT_A_TURN_ORIGINS" in src[:src.find("close_offer(")])
    src = inspect.getsource(query._execute_query)
    _check("offer opened only with a button message", "if labels and button_msg" in src)


# --- eval -----------------------------------------------------------------


def case_eval() -> None:
    from bot.engine.eval import _ATTRIBUTION, _check_unoffered_choice

    inst = _inst()
    flags = _check_unoffered_choice(inst, "Done.\n\nKeep the old name, or rename it?")
    _check("eval flags unoffered either/or", [f.category for f in flags] == ["unoffered_choice"], repr(flags))
    _check("eval quiet with choices", not _check_unoffered_choice(inst, _reply("Keep", "Rename")))
    _check("eval quiet on open question", not _check_unoffered_choice(inst, "What should it be called?"))
    _check("eval quiet on statement", not _check_unoffered_choice(inst, "Keep it or rename it."))
    _check("eval quiet on builds", not _check_unoffered_choice(_inst(branch="b"), "Keep, or rename?"))
    _check("eval quiet on workflow steps",
           not _check_unoffered_choice(_inst(origin=InstanceOrigin.REVIEW_CODE), "Keep, or rename?"))
    _check("eval attributed to BOT_CONTEXT", ("unoffered_choice", "", "BOT_CONTEXT") in _ATTRIBUTION)


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        orig_data = config.DATA_DIR
        config.DATA_DIR = tmp
        try:
            case_parse()
            case_rows()
            for name, case in (("a", case_offer_lifecycle), ("b", case_prepare_and_send),
                               ("c", case_gates), ("d", case_tap_handler)):
                sub = tmp / name
                (sub / "s2").mkdir(parents=True)
                config.DATA_DIR = sub  # choices.jsonl lands next to the case's state
                case(sub)
            case_turn_starts()
            case_eval()
        finally:
            config.DATA_DIR = orig_data
    print(f"{PASSES} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
