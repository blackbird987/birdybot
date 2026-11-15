#!/usr/bin/env python3
"""Harness for the "Needs you" list (bot/engine/waiting.py).

Pinned against fakes:

  * every kind shows when its record says it is waiting, and hides when the
    record is closed, too old, undatable, or a read cannot answer (git None);
  * one item per thread, the most urgent kind wins; a running session's
    thread shows nothing; hidden repos' items are marked, not dropped;
  * one kind that raises hides only itself;
  * choice offers are read through ``choices.open_offers`` and its TTL;
  * the Ark render caps at 15 lines with "and N more", stays under 4096, has
    an empty state; a Control Room render stays under 1024;
  * the Ark embed renders the same digest when only the clock moved.

``--live`` reads the real state (a copy of data/state.json, never saved) and
prints per-kind counts before and after dedup, every item, and how many of the
stored failed merges and paused chains the filter hides. Read-only: it runs
git reads against the registered repos and nothing else.

Run:  python scripts/test_waiting.py [--live]
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


NOW = 1_800_000_000.0


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# --- Fakes -----------------------------------------------------------------


class FakeGit:
    def __init__(self) -> None:
        self.branches: dict[str, bool | None] = {}
        self.anc: dict[str, bool | None] = {}
        self.ahead_by: dict[str, int | None] = {}

    def branch_exists(self, path, branch):  # noqa: ANN001, ANN201
        return self.branches.get(branch, False)

    def is_ancestor(self, path, commit, of):  # noqa: ANN001, ANN201
        return self.anc.get(commit, None)

    def ahead(self, path, base, branch):  # noqa: ANN001, ANN201
        return self.ahead_by.get(branch, 0)


class FakeStore:
    def __init__(self) -> None:
        self.instances: list = []  # newest first
        self.repos = {"a": "/repo/a", "b": "/repo/b"}
        self.dormant: set[str] = set()
        self.platform: dict[str, dict] = {"discord": {}, "auto_update": {}}
        self.peer: list[dict] = []
        self.deploy_cfg: dict = {}
        self.deploy_state: dict = {}
        self.alerts: dict = {}
        self.expiry: dict = {}
        self.offers: dict = {}
        self.chain_meta: dict = {}
        self.chains: dict = {}
        self.pending: dict = {}
        self.scheduled: dict = {}
        self.raise_pending = False

    def list_instances(self, all_=False):  # noqa: ANN001, ANN201
        return list(self.instances)

    def get_instance(self, iid):  # noqa: ANN001, ANN201
        return next((i for i in self.instances if i.id == iid), None)

    def list_repos(self):  # noqa: ANN201
        return dict(self.repos)

    def is_repo_dormant(self, name):  # noqa: ANN001, ANN201
        return name in self.dormant

    def get_platform_state(self, key):  # noqa: ANN001, ANN201
        return self.platform.get(key, {})

    def peer_received_all(self):  # noqa: ANN201
        return list(self.peer)

    def get_deploy_config(self, repo):  # noqa: ANN001, ANN201
        return self.deploy_cfg.get(repo)

    def get_deploy_state(self, repo):  # noqa: ANN001, ANN201
        return self.deploy_state.get(repo)

    def get_account_alerts(self):  # noqa: ANN201
        return dict(self.alerts)

    def get_login_expiry_warnings(self):  # noqa: ANN201
        return dict(self.expiry)

    def list_choice_offers(self):  # noqa: ANN201
        return dict(self.offers)

    def list_autopilot_chain_meta(self):  # noqa: ANN201
        return dict(self.chain_meta)

    def get_all_autopilot_chains(self):  # noqa: ANN201
        return dict(self.chains)

    def list_pending_merges(self):  # noqa: ANN201
        if self.raise_pending:
            raise RuntimeError("boom")
        return dict(self.pending)

    def list_scheduled_merges(self):  # noqa: ANN201
        return dict(self.scheduled)

    def sidelined_accounts(self):  # noqa: ANN201
        return {}

    def running_count(self):  # noqa: ANN201
        return 0


def inst(iid, sid, *, repo="a", status="completed", ago=3600.0, needs_input=False,  # noqa: ANN001, ANN201
         origin=None, error=None, branch=None, recovery=False):
    from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType
    i = Instance(id=iid, name=iid, instance_type=InstanceType.QUERY, prompt=f"prompt {iid}",
                 repo_name=repo, repo_path=f"/repo/{repo}", status=InstanceStatus(status))
    i.session_id = sid
    i.created_at = iso(NOW - ago - 60)
    i.finished_at = iso(NOW - ago) if status not in ("running", "queued") else None
    i.needs_input = needs_input
    i.origin = origin or InstanceOrigin.DIRECT
    i.error = error
    if branch:
        i.branch = branch
        i.original_branch = "master"
        i.worktree_path = f"/repo/{repo}/.worktrees/{iid}"
    if recovery:
        i.manual_recovery_needed = True
        i.manual_recovery_reason = "worktree left dirty"
    return i


def forum(threads_a: dict, threads_b: dict | None = None) -> dict:
    return {
        "a": {"repo_name": "a", "control_thread_id": "ctl-a", "threads": threads_a},
        "b": {"repo_name": "b", "control_thread_id": "ctl-b", "threads": threads_b or {}},
    }


def run(store, threads, git=None, **kw):  # noqa: ANN001, ANN003, ANN201
    from bot.engine import waiting
    return waiting.collect(store, threads, now=NOW, git=git or FakeGit(), **kw)


def kinds(items) -> list[str]:  # noqa: ANN001
    return [i.kind for i in items]


# --- Arms --------------------------------------------------------------------


def arm_session_kinds() -> None:
    print("question, failed run, manual recovery")
    from bot.claude.types import InstanceOrigin
    s = FakeStore()
    s.instances = [
        inst("q1", "s1", needs_input=True),
        inst("f1", "s2", status="failed", error="boom\nmore"),
        inst("j1", "s3", status="failed", origin=InstanceOrigin.JUDGE, ago=10),
        inst("t3", "s3", ago=4000),
        inst("old", "s4", needs_input=True, ago=4 * 86400),
        inst("m1", "s5", recovery=True),
        inst("c1", "s6", needs_input=True),
    ]
    th = {
        "t1": {"session_id": "s1", "topic": "Fix login"},
        "t2": {"session_id": "s2", "topic": "Build thing"},
        "t3": {"session_id": "s3", "topic": "Judge only"},
        "t4": {"session_id": "s4", "topic": "Old question"},
        "t5": {"session_id": "s5", "topic": "Recover me"},
        "t6": {"session_id": "s6", "topic": "Child", "parent_thread_id": "t1"},
        "ctl-a": {"session_id": "sx", "topic": "control"},
    }
    items = run(s, forum(th))
    by = {i.thread_id: i for i in items}
    check(by.get("t1") and by["t1"].kind == "question", "a question shows")
    check(by.get("t2") and by["t2"].kind == "failed_run" and by["t2"].detail == "boom",
          "a failed run shows its first error line")
    check("t3" not in by, "a judge failure is not shown and does not shadow the real turn")
    check("t4" not in by, "a question older than 3 days hides")
    check(by.get("t5") and by["t5"].kind == "manual_recovery", "manual recovery shows")
    check("t6" not in by, "a child thread's question is its parent's wave, not shown")
    check("ctl-a" not in by, "control threads are not sessions")
    line = __import__("bot.engine.waiting", fromlist=["x"]).item_line(by["t1"])
    check("<#t1>" in line and f"<t:{int(NOW - 3600)}:R>" in line,
          f"a line links the thread and uses timestamp markup (got {line!r})")


def arm_dedup_running_hidden() -> None:
    print("dedup, running, hidden, repo filter")
    s = FakeStore()
    s.dormant = {"b"}
    s.instances = [
        inst("r1", "s2", status="running"),
        inst("q1", "s1", needs_input=True),
        inst("qb", "sb", repo="b", needs_input=True),
    ]
    s.offers = {
        "t1": {"msg_id": "m1", "labels": ["Yes", "No"], "offered_at": NOW - 60},
        "t2": {"msg_id": "m2", "labels": ["A", "B"], "offered_at": NOW - 60},
        "t9": {"msg_id": "m9", "labels": ["Old"], "offered_at": NOW - 8 * 86400},
        "t8": {"msg_id": "m8", "labels": ["Bad"], "offered_at": "garbage"},
    }
    th = {"t1": {"session_id": "s1", "topic": "Both"}, "t2": {"session_id": "s2", "topic": "Busy"},
          "t8": {"session_id": "s8"}, "t9": {"session_id": "s9"}}
    items = run(s, forum(th, {"tb": {"session_id": "sb", "topic": "Hidden repo q"}}))
    by = {i.thread_id: i for i in items}
    check(by.get("t1") and by["t1"].kind == "choice_pick" and by["t1"].detail == "Yes / No",
          "a pick outranks the question on the same thread, with its labels")
    check(sum(1 for i in items if i.thread_id == "t1") == 1, "one item per thread")
    check("t2" not in by, "a running session's thread shows nothing")
    check("t9" not in by and "t8" not in by, "an expired or undatable offer is not open")
    hb = by.get("tb")
    check(hb is not None and hb.hidden, "a hidden repo's item still shows, marked hidden")
    from bot.engine import waiting
    check(hb is not None and "(hidden)" in waiting.item_line(hb), "the line says (hidden)")
    only_b = run(s, forum(th, {"tb": {"session_id": "sb", "topic": "Hidden repo q"}}), repo="b")
    check(kinds(only_b) == ["question"] and only_b[0].repo == "b", "repo= keeps one repo")


def arm_choices_reader() -> None:
    print("choices.open_offers")
    from bot.engine.directives import choices
    s = FakeStore()
    s.offers = {"1": {"offered_at": NOW - 10}, "2": {"offered_at": NOW - choices.OFFER_TTL_SECONDS - 1},
                "3": {}, "4": "not a dict"}
    got = choices.open_offers(s, now=NOW)
    check(set(got) == {"1"}, f"only the live offer is open (got {sorted(got)})")
    got["1"]["offered_at"] = 0
    check(s.offers["1"]["offered_at"] == NOW - 10, "open_offers returns copies")

    class Broken:
        def list_choice_offers(self):  # noqa: ANN201
            raise RuntimeError("x")
    check(choices.open_offers(Broken(), now=NOW) == {}, "a broken read fails open to nothing")


def arm_records() -> None:
    print("record kinds")
    from bot import config
    from bot.engine import peer_questions as pq
    from bot.store.records import DeployState
    s = FakeStore()
    git = FakeGit()
    s.instances = [
        inst("ch1", "sc"), inst("mf", "sm", branch="claude-bot/mf"),
        inst("rd", "sr", branch="claude-bot/rd"), inst("rz", "sz", branch="claude-bot/rz"),
        inst("hd", "sh", branch="claude-bot/hd"),
    ]
    th = {
        "tc": {"session_id": "sc", "topic": "Chain"},
        "tgone": {"session_id": "sgone", "topic": "Pruned chain"},
        "tm": {"session_id": "sm", "topic": "Merge"},
        "tr": {"session_id": "sr", "topic": "Ready"},
        "tz": {"session_id": "sz", "topic": "Level"},
        "th": {"session_id": "sh", "topic": "Held"},
        "tp": {"session_id": "sp", "topic": "Parent", "last_human_at": None},
        "tq": {"session_id": "sq", "topic": "Parent2", "last_human_at": NOW - 10},
        "ty": {"session_id": "sy", "topic": "Turn", "last_turn_at": NOW - 3600},
        "ty2": {"session_id": "sy2", "topic": "Replied", "last_turn_at": NOW - 3600,
                "last_human_at": NOW - 60},
        "ty3": {"session_id": "sy3", "topic": "Old turn", "last_turn_at": NOW - 3 * 86400},
        "ts": {"session_id": "ss", "topic": "Secret"},
    }
    s.platform["auto_update"] = {"carded_sha": "abc1234def", "carded_at": NOW - 100}
    git.anc["abc1234def"] = False
    s.peer = [
        {"id": "p1", "status": pq.CARDED, "created_at": NOW - 60, "repo": "a", "card_thread_id": "pc"},
        {"id": "p2", "status": pq.DONE, "created_at": NOW - 60, "repo": "a"},
        {"id": "p3", "status": pq.CARDED, "created_at": NOW - pq.MAX_AGE_SECS - 5, "repo": "a"},
    ]
    s.deploy_cfg = {"a": {"command": "x", "approved": False}, "b": {"command": "y", "approved": True}}
    s.deploy_state = {
        "a": DeployState(boot_ref="r0", current_ref="r1", pending_changes=["one", "two", "three"],
                         pending_sessions=["t-1", "t-2"]),
        "b": DeployState(boot_ref="r1", current_ref="r1", pending_changes=["one"]),
    }
    s.alerts = {"/h/.claude-x": {"reason": "signed_out", "since": iso(NOW - 50)},
                "/h/.claude-gone": {"reason": "signed_out"},
                "/h/.claude-ok": {"reason": "x", "resolved": True}}
    s.chain_meta = {
        "sc": {"status": "paused", "updated_at": iso(NOW - 60), "reason": "failed"},
        "sgone": {"status": "paused", "updated_at": iso(NOW - 60)},
        "sr": {"status": "running", "updated_at": iso(NOW - 60)},
    }
    s.platform["discord"] = {"orch_resume_payloads": {
        "k1": {"prompt": "x", "at": iso(NOW - 600), "thread_id": "tp"},
        "k2": {"prompt": "x", "at": iso(NOW - 600), "thread_id": "tq"},
        "k3": {"prompt": "x", "at": iso(NOW - 600)},
    }}
    s.pending = {
        "mf": {"created_at": iso(NOW - 600), "repo_name": "a", "branch": "claude-bot/mf",
               "target": "master", "channel_id": "tm"},
        "dead1": {"created_at": iso(NOW - 600), "repo_name": "a", "channel_id": "tx"},
        "dead2": {"created_at": iso(NOW - 600), "repo_name": "a", "branch": "claude-bot/gone",
                  "target": "master", "channel_id": "tx2"},
        "dead3": {"created_at": iso(NOW - 600), "repo_name": "a", "branch": "claude-bot/merged",
                  "target": "master", "channel_id": "tx3"},
        "dead4": {"created_at": iso(NOW - 600), "repo_name": "a", "branch": "claude-bot/unknown",
                  "target": "master", "channel_id": "tx4"},
        "undated": {"repo_name": "a", "branch": "claude-bot/mf", "target": "master"},
    }
    git.branches.update({"claude-bot/mf": True, "claude-bot/merged": True,
                         "claude-bot/unknown": None, "claude-bot/hd": True})
    git.anc.update({"claude-bot/mf": False, "claude-bot/merged": True, "claude-bot/hd": False})
    s.scheduled = {"hd": {"held": True, "repo_name": "a", "branch": "claude-bot/hd",
                          "channel_id": "th", "session_id": "sh", "created_at": iso(NOW - 30)}}
    git.ahead_by.update({"claude-bot/rd": 2, "claude-bot/rz": 0})
    secrets = {"tok1": {"name": "API_KEY", "channel_id": "ts", "repo": "a", "created_at": NOW - 60},
               "tok2": {"name": "OLD", "channel_id": "ts2", "repo": "a"}}
    with mock.patch("bot.vault.pending_requests", return_value=secrets), \
         mock.patch.object(config, "CLAUDE_ACCOUNTS", ["/h/.claude-x", "/h/.claude-ok"]), \
         mock.patch("bot.engine.chain_git._is_worktree_live", return_value=True):
        stats: dict = {}
        items = run(s, forum(th), git=git, stats=stats)
        keys = {i.key for i in items}
        byk: dict[str, list] = {}
        for i in items:
            byk.setdefault(i.kind, []).append(i)

        check(keys >= {"secret:tok1"} and "secret:tok2" not in keys,
              "a dated key request shows, an undatable one hides")
        check("update:abc1234def" in keys, "a carded update not in HEAD shows")
        check([i.thread_id for i in byk.get("peer_run_once", [])] == ["pc"],
              "only the carded, unexpired peer question shows, linked to its card")
        dep = byk.get("deploy", [])
        check(len(dep) == 1 and dep[0].detail == "approve deploy (2 merges)",
              f"an unapproved pending deploy shows, a booted one hides (got {[d.detail for d in dep]})")
        check(bool(dep) and dep[0].thread_id == "ctl-a",
              "a deploy links to its repo's Control Room, where the Deploy button is")
        acc = byk.get("account", [])
        check([a.title for a in acc] == ["x"], f"only unresolved alerts in rotation show (got {[a.title for a in acc]})")
        ch = byk.get("chain_paused", [])
        check([c.key for c in ch] == ["chain:sc"] and ch[0].detail == "paused: a step failed",
              "a paused chain shows with its reason; a pruned one and a running one hide")
        rp = byk.get("resume_parent", [])
        check([r.key for r in rp] == ["resume:k1"],
              "resume parent shows until the owner speaks there; a payload with no thread hides")
        mf = byk.get("merge_failed", [])
        check([m.key for m in mf] == ["merge:mf"],
              "only a live, unmerged branch shows; no branch, gone, merged, git None, undated hide")
        check([h.key for h in byk.get("merge_held", [])] == ["held:hd"], "a held merge shows")
        check([r.key for r in byk.get("ready_to_merge", [])] == ["ready:rd"],
              "a built branch ahead of base shows; one level with base hides")
        check([t.thread_id for t in byk.get("your_turn", [])] == ["ty"],
              "your turn: answered recently, no reply; replied or older than 48h hides")
        check(stats.get("merge_failed") == 1 and "your_turn" in stats, "stats count each reader")

        git.anc["abc1234def"] = None
        check("update:abc1234def" not in {i.key for i in run(s, forum(th), git=git)},
              "git that cannot answer hides the update")
        git.anc["abc1234def"] = True
        check("update:abc1234def" not in {i.key for i in run(s, forum(th), git=git)},
              "an applied update hides")

        s.raise_pending = True
        after = run(s, forum(th), git=git)
        check("merge_failed" not in kinds(after) and "chain_paused" in kinds(after),
              "a kind that raises hides only itself")


def _many(n: int, title: str = "Topic") -> list:
    from bot.engine.waiting import WaitingItem
    ks = ["question", "failed_run", "chain_paused", "ready_to_merge"]
    return [WaitingItem(kind=ks[i % len(ks)], key=f"k{i}", repo=f"repo{i % 5}",
                        thread_id=str(10**18 + i), title=f"{title} {i}",
                        detail="d" * 80, since=NOW - i) for i in range(n)]


def arm_render() -> None:
    print("rendering")
    from bot.engine import waiting
    from bot.engine.waiting import WaitingItem
    check(waiting.render_ark([]) == "Nothing is waiting on you.", "empty state")
    items = sorted(_many(40), key=lambda i: waiting._RANK[i.kind])
    text = waiting.render_ark(items)
    lines = text.splitlines()
    item_lines = [ln for ln in lines if not ln.startswith("**") and not ln.startswith("and ")]
    check(len(item_lines) == 15, f"15 item lines (got {len(item_lines)})")
    check(lines[-1].startswith("and 25 more: "), f"then 'and N more' by kind (got {lines[-1]!r})")
    check(len(text) <= 4000, f"inside the embed budget (len {len(text)})")
    big = waiting.render_ark(sorted(_many(40, "T" * 300), key=lambda i: waiting._RANK[i.kind]))
    check(len(big) <= 4000, f"huge titles still fit (len {len(big)})")
    turns = [WaitingItem(kind="your_turn", key=f"turn:{i}", repo="a", thread_id=str(i),
                         title="x", since=NOW, hidden=(i == 0)) for i in range(3)]
    t = waiting.render_ark(turns)
    if waiting.YOUR_TURN_AS_COUNT:
        check("3 threads answered" in t and "1 in hidden repos" in t and "<#" not in t,
              f"your turn renders as one count line (got {t!r})")
    else:
        check(t.count("<#") == 3, "your turn renders as lines")
    room = waiting.render_room(_many(30, "T" * 200))
    check(len(room) <= 1024, f"a Control Room field stays under 1024 (len {len(room)})")

    def accounted(text: str, total: int) -> bool:
        rows = text.splitlines()
        shown = sum(1 for ln in rows if "<#" in ln)
        more = next((ln for ln in rows if ln.startswith("and ")), "and 0 more")
        return shown + int(more.split()[1]) == total and not text.endswith("…")

    tight_room = waiting.render_room(_many(30, "T" * 200), limit=400)
    check(len(tight_room) <= 400 and accounted(tight_room, 30),
          f"a squeezed room folds items into 'and N more' with the right count ({tight_room!r})")
    tight_ark = waiting.render_ark(sorted(_many(40, "T" * 300), key=lambda i: waiting._RANK[i.kind]),
                                   limit=700)
    check(len(tight_ark) <= 700 and accounted(tight_ark, 40),
          f"a squeezed Ark folds items into 'and N more' with the right count ({tight_ark!r})")
    check(waiting.signature(items) == waiting.signature(list(items)), "signature is stable")


def arm_digest_stable() -> None:
    print("Ark digest holds when only the clock moves")
    from bot.discord import dashboard, forums
    s = FakeStore()
    items = _many(5)
    with mock.patch("time.time", return_value=NOW):
        a = forums.control_digest(dashboard.build_dashboard_embed(s, items, "usage", NOW - 999), None)
    with mock.patch("time.time", return_value=NOW + 3600):
        b = forums.control_digest(dashboard.build_dashboard_embed(s, items, "usage", NOW - 999), None)
    check(a == b, "same items, an hour later: same digest")
    emb = dashboard.build_dashboard_embed(s, [], "usage", NOW - 999)
    names = [f.name for f in emb.fields]
    check(emb.title == "The Ark" and emb.description == "Nothing is waiting on you.",
          "empty Ark says nothing is waiting")
    check(names[0] == "Status" and names[1].startswith("Usage") and len(names) == 2,
          f"fields are Status then Usage only (got {names})")
    check(f"<t:{int(NOW - 999)}:R>" in emb.fields[0].value, "uptime lives in the status line")
    check(emb.footer.text and "<t:" not in emb.footer.text and "Up" not in emb.footer.text,
          f"footer is version and PC only (got {emb.footer.text!r})")


# --- Live ----------------------------------------------------------------------


def live() -> int:
    from bot.claude.types import InstanceStatus
    from bot.engine import waiting
    from bot.store.state import StateStore

    main = Path("/home/quincy/Programming/claude-telegram-bot")
    src = main / "data" / "state.json"
    tmp = Path(tempfile.mkdtemp(prefix="waiting-live-"))
    try:
        shutil.copy2(src, tmp / "state.json")
        (tmp / "results").mkdir()
        store = StateStore(tmp / "state.json", tmp / "results")
        now = time.time()
        stats: dict = {}
        t0 = time.monotonic()
        items = waiting.collect(store, None, now=now, stats=stats)
        took = time.monotonic() - t0
        print(f"collect took {took:.2f}s over {len(store.list_instances(all_=True))} instances")
        print("per reader before dedup:")
        for name, n in stats.items():
            print(f"  {name:28s} {n}")
        after: dict[str, int] = {}
        for i in items:
            after[i.kind] = after.get(i.kind, 0) + 1
        print("per kind after dedup:")
        for k in waiting.KINDS:
            print(f"  {k.name:28s} {after.get(k.name, 0)}")
        print(f"total {len(items)}")
        print("items:")
        for i in items:
            age = f"{(now - i.since) / 3600:.1f}h" if i.since else "-"
            print(f"  [{i.kind}] {i.repo}{' (hidden)' if i.hidden else ''} | {i.title} | "
                  f"{i.detail} | thread {i.thread_id} | {age}")

        pend = store.list_pending_merges()
        shown_m = sum(1 for i in items if i.kind == "merge_failed")
        meta = store.list_autopilot_chain_meta()
        paused = [s for s, m in meta.items() if (m or {}).get("status") == "paused"]
        shown_c = sum(1 for i in items if i.kind == "chain_paused")
        print(f"stored failed merges: {len(pend)}, shown {shown_m}, hidden {len(pend) - shown_m}")
        print(f"stored paused chains: {len(paused)}, shown {shown_c}, hidden {len(paused) - shown_c}")

        # "Your turn" estimate: thread stamps start with this change, so count
        # threads whose latest real turn finished inside 48h, did not ask, and
        # has no newer instance (a typed message starts one).
        idx = waiting.index_from_store(store)
        latest: dict = {}
        running: set = set()
        for inst in store.list_instances(all_=True):
            if not inst.session_id:
                continue
            if inst.status in (InstanceStatus.RUNNING, InstanceStatus.QUEUED):
                running.add(inst.session_id)
            if inst.origin not in waiting._NOT_A_TURN:
                latest.setdefault(inst.session_id, inst)
        est = []
        for ref in idx.threads.values():
            if ref.parent_thread_id or not ref.session_id or ref.session_id in running:
                continue
            i = latest.get(ref.session_id)
            if i is None or i.status != InstanceStatus.COMPLETED or i.needs_input:
                continue
            at = waiting._finished(i)
            if at is not None and now - at <= waiting.YOUR_TURN_MAX_AGE:
                est.append((ref.repo, ref.topic))
        print(f"your-turn estimate: {len(est)} threads across "
              f"{len({r for r, _ in est})} repos (of {len(idx.threads)} threads)")
        for r, t in est[:60]:
            print(f"  ~ {r} | {t[:60]}")
        print("render_ark length:", len(waiting.render_ark(items)))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if "--live" in sys.argv:
        env = Path("/home/quincy/Programming/claude-telegram-bot/.env")
        if env.exists() and not (ROOT / ".env").exists():
            from dotenv import load_dotenv
            load_dotenv(env)  # values stay in the environment, never printed
        return live()
    arm_session_kinds()
    arm_dedup_running_hidden()
    arm_choices_reader()
    arm_records()
    arm_render()
    arm_digest_stable()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
