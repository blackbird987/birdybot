"""Regression test for the orchestrator spawn-wave join (t-7400).

Background: a parent that fanned out N children used to get N separate
callbacks, each with 400 chars of the child's report and its own Resume
button — the human was the join point, the relay, and the watchdog. Now the
wave is reported ONCE when every child has settled, the parent is handed the
report FILE PATHS (not truncated text), it auto-resumes, and a child parked on
a question is reported so the parent can answer it with /reply.

Covers, driving the real code over fake bot/store/forums seams:
- child state derivation: completed / failed / killed / running / pending /
  blocked / gone, and which of those count as settled
- a wave with an outstanding child posts a progress line and does NOT resume
- the last child to land closes the wave: one post, one auto-resume
- the resume prompt carries absolute report FILE PATHS, not just excerpts
- release is idempotent — a second callback and the timeout sweep can't
  re-deliver a wave already released
- a blocked child does not close the wave, and its notice names /reply
- the timeout sweep releases a wave whose child never reached a terminal
  state, marks it partial, and does NOT auto-resume it
- an archived parent gets an Ark notice instead of a silent log line
- /reply pairing + the "only your own children" guard

Run: python scripts/test_orchestrator_join.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from bot import config
from bot.claude.types import Instance, InstanceStatus, InstanceType
from bot.discord import orchestrator as orch
from bot.engine.commands import (
    _MAX_REPLIES_PER_RESPONSE,
    _handle_reply_directives,
    _own_child_thread_ids,
    _pair_reply_directives,
)

_failures: list[str] = []


def _check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


# --- Fakes ------------------------------------------------------------------


class FakeThreadInfo:
    def __init__(self, thread_id, session_id=None, topic=None, parent=None):
        self.thread_id = thread_id
        self.session_id = session_id
        self.topic = topic
        self.parent_thread_id = parent


class FakeProject:
    def __init__(self, threads):
        self.threads = threads
        self.repo_name = "bot"


class FakeForums:
    def __init__(self, threads: dict):
        self._threads = threads
        self.forum_projects = {"bot": FakeProject(threads)}

    def thread_to_project(self, thread_id):
        info = self._threads.get(str(thread_id))
        if info is None:
            return None
        return self.forum_projects["bot"], info


class FakeStore:
    def __init__(self, instances):
        self._instances = instances
        self._platform = {}
        self.saved = 0

    def list_instances(self, all_=False):
        return list(self._instances)

    def update_instance(self, inst, critical=False):
        self.saved += 1

    def get_platform_state(self, key):
        return self._platform.setdefault(key, {})

    def set_platform_state(self, key, val, persist=True):
        self._platform[key] = val

    def save(self):
        self.saved += 1


class FakeMessenger:
    def __init__(self):
        self.posts = []

    async def send_text(self, channel_id, text, buttons=None, silent=False):
        self.posts.append(
            SimpleNamespace(channel=str(channel_id), text=text, buttons=buttons),
        )


class FakeChannel:
    def __init__(self, archived=False, locked=False):
        self.archived = archived
        self.locked = locked


class FakeBot:
    def __init__(self, threads, instances, *, archived_parents=()):
        self._forums = FakeForums(threads)
        self._store = FakeStore(instances)
        self.messenger = FakeMessenger()
        self._lobby_channel_id = "999"
        self._archived = set(archived_parents)
        self.resumes = []

    def get_channel(self, cid):
        return FakeChannel(archived=str(cid) in self._archived)

    async def _replay_to_thread(self, channel_id, prompt, repo_name=None, *, source="replay"):
        self.resumes.append((str(channel_id), prompt, source))
        return True


def _inst(iid, session, status, *, needs_input=False, children=(),
          result_file=None, created="2026-08-18T10:00:00+00:00",
          released=False, summary=None, sealed=True, blocked_resumes=0):
    return Instance(
        id=iid, name=None, instance_type=InstanceType.QUERY, prompt="",
        repo_name="bot", repo_path="/tmp", status=status,
        session_id=session, created_at=created, needs_input=needs_input,
        spawn_dispatched_thread_ids=list(children),
        spawn_wave_released=released, spawn_wave_sealed=sealed,
        spawn_blocked_resumes=blocked_resumes,
        result_file=result_file, summary=summary,
    )


async def _drain() -> None:
    """Let detached auto-resume tasks run."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# --- Tests ------------------------------------------------------------------


def test_child_states() -> None:
    print("\n[child state derivation]")
    threads = {
        "101": FakeThreadInfo("101", "s1", "Research auth"),
        "102": FakeThreadInfo("102", "s2", "Research db"),
        "103": FakeThreadInfo("103", None, "Never started"),
        "104": FakeThreadInfo("104", "s4", "Asked a question"),
        "105": FakeThreadInfo("105", "s5", "Cancelled"),
        "106": FakeThreadInfo("106", "s6", "Still going"),
    }
    insts = [
        _inst("q-1", "s1", InstanceStatus.COMPLETED),
        _inst("q-2", "s2", InstanceStatus.FAILED),
        _inst("q-4", "s4", InstanceStatus.COMPLETED, needs_input=True),
        _inst("q-5", "s5", InstanceStatus.KILLED),
        _inst("q-6", "s6", InstanceStatus.RUNNING),
    ]
    bot = FakeBot(threads, insts)
    got = {tid: orch._child_state(bot, tid).state for tid in threads}
    _check(got["101"] == "completed", "completed child reads completed")
    _check(got["102"] == "failed", "failed child reads failed")
    _check(got["103"] == "pending", "child with no session reads pending")
    _check(got["104"] == "blocked", "needs_input child reads blocked, not completed")
    _check(got["105"] == "killed", "killed child reads killed")
    _check(got["106"] == "running", "in-flight child reads running")
    _check(orch._child_state(bot, "nope").state == "gone", "unmapped thread reads gone")

    settled = {tid: orch._child_state(bot, tid).settled for tid in threads}
    _check(settled["101"] and settled["102"] and settled["105"],
           "completed/failed/killed all settle")
    _check(not settled["104"], "blocked child does NOT settle (wave stays open)")
    _check(not settled["103"] and not settled["106"],
           "pending/running children do NOT settle")

    # Newest instance wins when a session has several turns.
    insts.append(_inst("q-7", "s1", InstanceStatus.RUNNING,
                       created="2026-08-18T12:00:00+00:00"))
    _check(orch._child_state(bot, "101").state == "running",
           "newest instance for a session wins over an older one")


def test_wave_join() -> None:
    print("\n[wave join]")
    with tempfile.TemporaryDirectory() as td:
        r1 = Path(td) / "q-1.md"
        r1.write_text("FULL REPORT ONE\n" + ("detail " * 200), encoding="utf-8")
        r2 = Path(td) / "q-2.md"
        r2.write_text("FULL REPORT TWO\n" + ("detail " * 200), encoding="utf-8")

        threads = {
            "100": FakeThreadInfo("100", "sp", "Parent"),
            "101": FakeThreadInfo("101", "s1", "Research auth", parent="100"),
            "102": FakeThreadInfo("102", "s2", "Research db", parent="100"),
        }
        parent = _inst("q-p", "sp", InstanceStatus.COMPLETED, children=["101", "102"])
        child1 = _inst("q-1", "s1", InstanceStatus.COMPLETED, result_file=str(r1))
        child2 = _inst("q-2", "s2", InstanceStatus.RUNNING)
        bot = FakeBot(threads, [parent, child1, child2])

        # First child lands, second still running -> progress only.
        asyncio.run(orch.post_parent_callback(bot, "101", "COMPLETED", "done"))
        _check(len(bot.messenger.posts) == 1, "outstanding wave posts one progress line")
        _check("1/2" in bot.messenger.posts[0].text,
               "progress line counts children back")
        _check(not bot.resumes, "parent is NOT resumed while a child is outstanding")
        _check(not parent.spawn_wave_released, "wave stays open")

        # Second child finishes -> wave closes.
        bot._store._instances = [
            parent, child1,
            _inst("q-2", "s2", InstanceStatus.COMPLETED, result_file=str(r2)),
        ]
        asyncio.run(_run_and_drain(orch.post_parent_callback(bot, "102", "COMPLETED", "done")))
        _check(parent.spawn_wave_released, "wave marked released")
        _check(len(bot.messenger.posts) == 2, "wave closes with exactly one more post")
        body = bot.messenger.posts[1].text
        _check("2/2" in body, "closing post says 2/2 children back")
        _check(bot.messenger.posts[1].buttons is None,
               "auto-resumed wave carries no Resume button")
        _check(len(bot.resumes) == 1, "parent auto-resumed once")
        prompt = bot.resumes[0][1]
        _check(bot.resumes[0][2] == "callback_resume",
               "resume is tagged callback_resume (does not reset the wave cap)")
        _check(str(r1) in prompt and str(r2) in prompt,
               "resume prompt carries BOTH children's full report file paths")
        _check("FULL REPORT ONE" in prompt, "resume prompt includes an excerpt")
        _check(len(prompt) < 4000,
               "resume prompt stays small — reports travel as paths, not inline text")

        # Idempotence: a late duplicate callback must not re-deliver.
        before = len(bot.messenger.posts), len(bot.resumes)
        asyncio.run(_run_and_drain(orch.post_parent_callback(bot, "101", "COMPLETED", "done")))
        _check((len(bot.messenger.posts), len(bot.resumes)) == before,
               "already-released wave ignores a duplicate child callback")


async def _run_and_drain(coro):
    await coro
    await _drain()


def test_blocked_child() -> None:
    print("\n[blocked child]")
    threads = {
        "100": FakeThreadInfo("100", "sp", "Parent"),
        "101": FakeThreadInfo("101", "s1", "Needs a decision", parent="100"),
        "102": FakeThreadInfo("102", "s2", "Other", parent="100"),
    }
    parent = _inst("q-p", "sp", InstanceStatus.COMPLETED, children=["101", "102"])
    blocked = _inst("q-1", "s1", InstanceStatus.COMPLETED, needs_input=True,
                    summary="Should I use Postgres or SQLite?")
    other = _inst("q-2", "s2", InstanceStatus.COMPLETED)
    bot = FakeBot(threads, [parent, blocked, other])

    asyncio.run(_run_and_drain(orch.post_parent_callback(bot, "101", "BLOCKED", "question")))
    _check(not parent.spawn_wave_released,
           "a blocked child does NOT close the wave even if every sibling is done")
    _check(len(bot.messenger.posts) == 1, "blocked child posts one notice")
    _check("waiting on an answer" in bot.messenger.posts[0].text,
           "notice says the child is waiting")
    _check(len(bot.resumes) == 1, "parent auto-resumed to answer it")
    prompt = bot.resumes[0][1]
    _check("/reply thread=101" in prompt,
           "resume prompt tells the parent exactly how to answer the child")
    _check("Should I use Postgres" in prompt, "the child's question reaches the parent")


def test_timeout_sweep() -> None:
    print("\n[timeout sweep]")
    old = (datetime.now(timezone.utc)
           - timedelta(minutes=config.ORCH_WAVE_TIMEOUT_MIN + 5)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()

    threads = {
        "100": FakeThreadInfo("100", "sp", "Parent"),
        "101": FakeThreadInfo("101", "s1", "Came back", parent="100"),
        "102": FakeThreadInfo("102", None, "Died before starting", parent="100"),
    }
    parent = _inst("q-p", "sp", InstanceStatus.COMPLETED, children=["101", "102"],
                   created=old)
    bot = FakeBot(threads, [parent, _inst("q-1", "s1", InstanceStatus.COMPLETED)])

    n = asyncio.run(_sweep_and_drain(bot))
    _check(n == 1, "sweep releases the stale wave")
    _check(parent.spawn_wave_released, "stale wave marked released")
    body = bot.messenger.posts[0].text
    _check("released early" in body, "partial release says so")
    _check("1/2" in body, "partial release reports how many came back")
    _check(bot.messenger.posts[0].buttons is not None,
           "partial release offers a Resume button instead of auto-resuming")
    _check(not bot.resumes,
           "partial release does NOT auto-resume — proceeding without a child is a human call")
    resume_prompt = list(bot._store._platform["discord"]["orch_resume_payloads"].values())[0]
    _check("never reported back" in resume_prompt,
           "the parent is told which children contributed nothing")

    # A fresh wave is left alone.
    parent2 = _inst("q-p2", "sp", InstanceStatus.COMPLETED, children=["101", "102"],
                    created=fresh)
    bot2 = FakeBot(threads, [parent2, _inst("q-1", "s1", InstanceStatus.COMPLETED)])
    _check(asyncio.run(_sweep_and_drain(bot2)) == 0, "a wave inside its window is untouched")
    _check(not parent2.spawn_wave_released, "fresh wave stays open")

    # An already-released wave is never re-swept.
    parent3 = _inst("q-p3", "sp", InstanceStatus.COMPLETED, children=["101"],
                    created=old, released=True)
    bot3 = FakeBot(threads, [parent3])
    _check(asyncio.run(_sweep_and_drain(bot3)) == 0, "released wave is not re-released")


async def _sweep_and_drain(bot):
    n = await orch.sweep_stale_waves(bot)
    await _drain()
    return n


def test_archived_parent() -> None:
    print("\n[archived parent]")
    threads = {
        "100": FakeThreadInfo("100", "sp", "Parent"),
        "101": FakeThreadInfo("101", "s1", "Child", parent="100"),
    }
    parent = _inst("q-p", "sp", InstanceStatus.COMPLETED, children=["101"])
    bot = FakeBot(threads, [parent, _inst("q-1", "s1", InstanceStatus.COMPLETED)],
                  archived_parents=["100"])
    # orchestrator only touches discord for the isinstance(ch, Thread) archived
    # check, so pointing that name at the fake channel is the whole seam.
    real_discord = orch.discord
    orch.discord = SimpleNamespace(Thread=FakeChannel)
    try:
        asyncio.run(_run_and_drain(orch.post_parent_callback(bot, "101", "COMPLETED", "x")))
    finally:
        orch.discord = real_discord
    _check(not bot.resumes, "archived parent is not resumed")
    _check(any(p.channel == "999" for p in bot.messenger.posts),
           "undeliverable wave surfaces in The Ark instead of only the log")
    _check(parent.spawn_wave_released,
           "wave is still marked released so it can't be retried forever")


def test_reply_directive() -> None:
    print("\n[/reply directive]")
    text = (
        "Answering it now.\n\n"
        "[BOT_CMD: /reply thread=555]\n~~~reply\nUse SQLite.\n~~~\n"
        "[BOT_CMD: /reply thread=666]\n~~~reply\nSkip the cache.\n~~~\n"
    )
    pairs, no_body, over_cap = _pair_reply_directives(text)
    _check(len(pairs) == 2 and not no_body and not over_cap, "two directives pair cleanly")
    _check(pairs[0] == ("thread=555", "Use SQLite."), "first pair keeps its own body")

    shared = "[BOT_CMD: /reply thread=1]\n[BOT_CMD: /reply thread=2]\n~~~reply\nx\n~~~"
    pairs, no_body, _ = _pair_reply_directives(shared)
    _check(len(pairs) == 1 and no_body == 1, "two directives cannot share one body")

    quoted = "> [BOT_CMD: /reply thread=7]\n> ~~~reply\n> x\n> ~~~"
    _check(_pair_reply_directives(quoted)[0] == [], "quoted directive is skipped")

    many = "".join(
        f"[BOT_CMD: /reply thread={i}]\n~~~reply\nb{i}\n~~~\n" for i in range(7)
    )
    pairs, _, over_cap = _pair_reply_directives(many)
    _check(len(pairs) == _MAX_REPLIES_PER_RESPONSE and over_cap == 2,
           f"cap holds at {_MAX_REPLIES_PER_RESPONSE} with the rest reported")

    # Ownership guard.
    delivered: list[tuple[str, str]] = []
    notices: list[str] = []

    class _Msgr:
        async def send_text(self, cid, text, buttons=None, silent=False):
            notices.append(text)

    async def _reply(tid, body):
        delivered.append((tid, body))
        return True

    store = FakeStore([
        _inst("q-p", "sp", InstanceStatus.COMPLETED, children=["555"]),
        _inst("q-x", "other", InstanceStatus.COMPLETED, children=["666"]),
    ])
    ctx = SimpleNamespace(
        session_id="sp", store=store, messenger=_Msgr(), channel_id="100",
        reply_to_child=_reply,
    )
    _check(_own_child_thread_ids(ctx) == {"555"},
           "ownership is scoped to this session's own dispatched children")

    asyncio.run(_handle_reply_directives(ctx, text))
    _check(delivered == [("555", "Use SQLite.")],
           "only the child this session spawned is answered")
    _check(any("not a session this thread spawned" in n for n in notices),
           "the foreign target is refused with a visible reason")

    # A malformed id never reaches the platform callback.
    delivered.clear()
    notices.clear()
    asyncio.run(_handle_reply_directives(
        ctx, "[BOT_CMD: /reply thread=../etc]\n~~~reply\nx\n~~~"))
    _check(not delivered and any("not a thread id" in n for n in notices),
           "a non-numeric thread id is rejected before dispatch")


def test_wave_seal() -> None:
    print("\n[wave seal — the half-written-roster race]")
    threads = {
        "100": FakeThreadInfo("100", "sp", "Parent"),
        "101": FakeThreadInfo("101", "s1", "Instant failure", parent="100"),
        "102": FakeThreadInfo("102", "s2", "Slow research", parent="100"),
    }
    # Mid-dispatch: child 1 was created and died immediately; child 2's thread
    # does not exist on the roster yet, and the wave is not sealed.
    parent = _inst("q-p", "sp", InstanceStatus.COMPLETED, children=["101"],
                   sealed=False)
    bot = FakeBot(threads, [parent, _inst("q-1", "s1", InstanceStatus.FAILED)])
    asyncio.run(_run_and_drain(orch.post_parent_callback(bot, "101", "FAILED", "boom")))
    _check(not parent.spawn_wave_released,
           "an unsealed wave is NOT closed by its first child, even if that child is the whole roster so far")
    _check(not bot.resumes, "no resume off a half-written roster")

    # Dispatch finishes: child 2 is appended and the wave is sealed. Both
    # children are already terminal, so nothing else would trigger the join.
    parent.spawn_dispatched_thread_ids.append("102")
    parent.spawn_wave_sealed = True
    bot._store._instances.append(_inst("q-2", "s2", InstanceStatus.COMPLETED))
    asyncio.run(_run_and_drain(orch.evaluate_wave_now(bot, "100")))
    _check(parent.spawn_wave_released, "sealing closes a wave whose children all finished during dispatch")
    _check(len(bot.resumes) == 1, "sealed wave resumes the parent exactly once")
    _check("2/2" in bot.messenger.posts[-1].text,
           "the sealed wave reports BOTH children, not just the one that raced")

    # Sealing a wave with an outstanding child stays silent.
    parent2 = _inst("q-p2", "sp2", InstanceStatus.COMPLETED, children=["101", "106"])
    threads["100"] = FakeThreadInfo("100", "sp2", "Parent")
    threads["106"] = FakeThreadInfo("106", "s6", "Still going", parent="100")
    bot2 = FakeBot(threads, [parent2,
                             _inst("q-1", "s1", InstanceStatus.FAILED),
                             _inst("q-6", "s6", InstanceStatus.RUNNING)])
    asyncio.run(_run_and_drain(orch.evaluate_wave_now(bot2, "100")))
    _check(not parent2.spawn_wave_released and not bot2.messenger.posts,
           "sealing with a child still running says nothing and waits")


def test_two_waves_on_one_parent() -> None:
    print("\n[two waves on one parent]")
    # A parent woken by wave A can dispatch wave B while A is still filling up.
    # A child's callback must resolve the wave that LISTS that child, or wave B
    # hijacks wave A's callbacks and neither ever closes.
    threads = {
        "100": FakeThreadInfo("100", "sp", "Parent"),
        "101": FakeThreadInfo("101", "s1", "Wave A first", parent="100"),
        "102": FakeThreadInfo("102", "s2", "Wave A second", parent="100"),
        "103": FakeThreadInfo("103", "s3", "Wave B only", parent="100"),
    }
    wave_a = _inst("q-a", "sp", InstanceStatus.COMPLETED, children=["101", "102"],
                   created="2026-08-18T10:00:00+00:00")
    wave_b = _inst("q-b", "sp", InstanceStatus.COMPLETED, children=["103"],
                   created="2026-08-18T11:00:00+00:00")
    bot = FakeBot(threads, [
        wave_a, wave_b,
        _inst("q-1", "s1", InstanceStatus.COMPLETED),
        _inst("q-2", "s2", InstanceStatus.RUNNING),
        _inst("q-3", "s3", InstanceStatus.RUNNING),
    ])
    asyncio.run(_run_and_drain(orch.post_parent_callback(bot, "101", "COMPLETED", "a1")))
    _check("1/2" in bot.messenger.posts[-1].text,
           "a wave-A child is counted against wave A, not against the newer wave B")
    _check(not wave_a.spawn_wave_released and not wave_b.spawn_wave_released,
           "neither wave closes while each still has an outstanding child")

    # Wave A's last child lands: A closes, B is untouched.
    bot._store._instances[3] = _inst("q-2", "s2", InstanceStatus.COMPLETED)
    asyncio.run(_run_and_drain(orch.post_parent_callback(bot, "102", "COMPLETED", "a2")))
    _check(wave_a.spawn_wave_released, "wave A closes on its own last child")
    _check(not wave_b.spawn_wave_released, "wave B is left open by wave A closing")
    _check(len(bot.resumes) == 1, "closing wave A resumes the parent once")


def test_abandoned_wave_retired_silently() -> None:
    print("\n[abandoned wave]")
    ancient = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
    threads = {
        "100": FakeThreadInfo("100", "sp", "Parent"),
        "101": FakeThreadInfo("101", "s1", "Long done", parent="100"),
    }
    parent = _inst("q-p", "sp", InstanceStatus.COMPLETED, children=["101"],
                   created=ancient)
    bot = FakeBot(threads, [parent, _inst("q-1", "s1", InstanceStatus.COMPLETED)])
    n = asyncio.run(_sweep_and_drain(bot))
    _check(parent.spawn_wave_released, "a wave nobody can act on any more is closed out")
    _check(not bot.messenger.posts,
           "an ancient wave posts nothing — this is what absorbs waves recorded before the join existed")
    _check(not bot.resumes, "an ancient wave never resumes a days-old parent")
    _check(n == 0, "a retired wave is not counted as a release")


def test_settled_wave_closed_without_callback() -> None:
    print("\n[wave whose last child never called back]")
    # A child the user kills takes an early return in finalize, so the parent is
    # never told. Every child is settled, but nothing triggered the join — the
    # sweep has to close it on its normal tick rather than making the parent
    # wait out the full timeout for a demonstrably complete wave.
    fresh = datetime.now(timezone.utc).isoformat()
    threads = {
        "100": FakeThreadInfo("100", "sp", "Parent"),
        "101": FakeThreadInfo("101", "s1", "Finished", parent="100"),
        "102": FakeThreadInfo("102", "s2", "Killed by user", parent="100"),
    }
    parent = _inst("q-p", "sp", InstanceStatus.COMPLETED, children=["101", "102"],
                   created=fresh)
    bot = FakeBot(threads, [parent,
                            _inst("q-1", "s1", InstanceStatus.COMPLETED),
                            _inst("q-2", "s2", InstanceStatus.KILLED)])
    n = asyncio.run(_sweep_and_drain(bot))
    _check(n == 1, "the sweep closes a fully-settled wave inside its timeout window")
    _check("2/2" in bot.messenger.posts[0].text,
           "it closes as complete, not as a partial release")
    _check(len(bot.resumes) == 1, "a complete wave still auto-resumes the parent")


def test_timeout_disabled() -> None:
    print("\n[timeout disabled]")
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    threads = {
        "100": FakeThreadInfo("100", "sp", "Parent"),
        "101": FakeThreadInfo("101", "s1", "Grinding away", parent="100"),
    }
    parent = _inst("q-p", "sp", InstanceStatus.COMPLETED, children=["101"], created=old)
    bot = FakeBot(threads, [parent, _inst("q-1", "s1", InstanceStatus.RUNNING)])
    prev = config.ORCH_WAVE_TIMEOUT_MIN
    config.ORCH_WAVE_TIMEOUT_MIN = 0
    try:
        n = asyncio.run(_sweep_and_drain(bot))
    finally:
        config.ORCH_WAVE_TIMEOUT_MIN = prev
    _check(n == 0 and not parent.spawn_wave_released,
           "timeout 0 means a wave waits for its children however long they take")


def test_blocked_resume_cap() -> None:
    print("\n[blocked-child resume budget]")
    threads = {
        "100": FakeThreadInfo("100", "sp", "Parent"),
        "101": FakeThreadInfo("101", "s1", "Keeps asking", parent="100"),
    }

    def _build(blocked_resumes):
        parent = _inst("q-p", "sp", InstanceStatus.COMPLETED, children=["101"],
                       blocked_resumes=blocked_resumes)
        bot = FakeBot(threads, [
            parent,
            _inst("q-1", "s1", InstanceStatus.COMPLETED, needs_input=True),
        ])
        return parent, bot

    parent, bot = _build(0)
    asyncio.run(_run_and_drain(orch.post_parent_callback(bot, "101", "BLOCKED", "?")))
    _check(len(bot.resumes) == 1, "the first question wakes the parent to answer it")
    _check(parent.spawn_blocked_resumes == 1, "the wave is charged for that wake-up")

    parent, bot = _build(orch._MAX_BLOCKED_RESUMES)
    asyncio.run(_run_and_drain(orch.post_parent_callback(bot, "101", "BLOCKED", "?")))
    _check(not bot.resumes,
           "past the budget the parent is no longer woken — a parent/child question loop cannot run away")
    _check(bot.messenger.posts and bot.messenger.posts[-1].buttons is not None,
           "the notice still arrives, it just needs a tap")
    _check(parent.spawn_blocked_resumes == orch._MAX_BLOCKED_RESUMES,
           "a refused wake-up is not charged")

    # A blocked child of a wave that is already gone has nothing bounding it.
    parent, bot = _build(0)
    parent.spawn_wave_released = True
    asyncio.run(_run_and_drain(orch.post_parent_callback(bot, "101", "BLOCKED", "?")))
    _check(not bot.resumes,
           "a blocked child with no open wave to charge gets a button, not an auto-resume")


def main() -> int:
    print("Orchestrator spawn-wave join regression test")
    test_child_states()
    test_wave_seal()
    test_two_waves_on_one_parent()
    test_abandoned_wave_retired_silently()
    test_settled_wave_closed_without_callback()
    test_timeout_disabled()
    test_blocked_resume_cap()
    test_wave_join()
    test_blocked_child()
    test_timeout_sweep()
    test_archived_parent()
    test_reply_directive()
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
