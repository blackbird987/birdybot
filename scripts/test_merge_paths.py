#!/usr/bin/env python3
"""Harness for the bookkeeping every merge path shares.

/merge, the Merge button, both resolver exits and the chain's
``_finalize_merge`` all merge through ``steps.merge_and_record``, and every
unattended merge that fails records through ``steps.record_merge_failure``.
Five hand-kept copies had drifted, so this pins what the one copy must do:

- a landed merge clears the branch from sibling instances by the name it had
  *before* the merge (two resolver copies read it afterwards, got None, and
  cleared nothing), and drops the pending merge
- a merge onto a tree missing a release, or left broken, skips deploy
  bookkeeping on every path, not only the chain's
- a failed merge refreshes the pending merge's failure kind and leaves it open
- the Merge button holds a post-Done thread open on a tree that must not ship
- a failed unattended merge is recorded even when its banner cannot be posted
- every resolver exit and manual path goes through the shared helper
- ``_finalize_merge`` reports a merge onto a tree that must not ship as
  STOPPED, not MERGED: the chain used to log "complete" for one (E17)

Run: python scripts/test_merge_paths.py
Exit 0 = all pass.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.claude.types import (  # noqa: E402
    RELEASE_ORPHANED_MARKER, REPO_UNUSABLE_MARKER, Instance, InstanceOrigin,
    InstanceStatus, InstanceType,
)
from bot.engine import branch_actions, deploy, resolver, steps  # noqa: E402
from bot.store import history as history_mod  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def make_inst(iid: str, branch: str | None = "claude-bot/t-1") -> Instance:
    return Instance(
        id=iid, name=None, instance_type=InstanceType.TASK, prompt="",
        repo_name="r", repo_path="/nonexistent/r", status=InstanceStatus.COMPLETED,
        mode="build", branch=branch, original_branch="master",
        worktree_path="/nonexistent/wt" if branch else None, origin=InstanceOrigin.DONE,
    )


class Store:
    def __init__(self, *insts: Instance):
        self.insts = list(insts)
        self.pending: dict[str, dict] = {}

    def list_instances(self, all_=False):
        return list(self.insts)

    def update_instance(self, inst, **_):
        pass

    def clear_pending_merge(self, iid):
        self.pending.pop(iid, None)

    def set_pending_merge_failure_kind(self, iid, kind):
        if iid in self.pending:
            self.pending[iid]["failure_kind"] = kind or ""

    def set_pending_merge(self, iid, **kw):
        self.pending[iid] = kw


class Messenger:
    def __init__(self, fail_send: bool = False):
        self.sent: list[dict] = []
        self.edits: list[str] = []
        self.closed = 0
        self.fail_send = fail_send

    def escape(self, text):
        return text

    async def send_text(self, channel_id, text, buttons=None, **kw):
        if self.fail_send:
            raise RuntimeError("gateway down")
        self.sent.append({"channel": channel_id, "text": text, "buttons": buttons, **kw})
        return "m1"

    async def edit_text(self, channel_id, msg_id, text, buttons=None, **kw):
        self.edits.append(text)

    async def close_conversation(self, channel_id, **kw):
        self.closed += 1


class Runner:
    def __init__(self, msg: str, kind: str | None = None):
        self.msg = msg
        self._last_merge_failure_kind = {}
        self.kind = kind

    async def merge_branch(self, inst):
        if self.msg.startswith("Merge failed"):
            if self.kind:
                self._last_merge_failure_kind[inst.id] = self.kind
            return self.msg
        inst.branch = None  # what the real merge does to the instance
        inst.worktree_path = None
        return self.msg


def make_ctx(runner, store, messenger=None):
    tagged: list[bool] = []

    async def on_merged():
        tagged.append(True)

    ctx = SimpleNamespace(
        runner=runner, store=store, messenger=messenger or Messenger(),
        channel_id="c1", on_merged=on_merged,
    )
    return ctx, tagged


def run(coro):
    return asyncio.run(coro)


def patched(fn):
    """Run fn with deploy bookkeeping and history writes stubbed out."""
    deploys: list[str] = []
    cleared: list[str] = []

    async def fake_deploy(messenger, store, inst):
        deploys.append(inst.id)

    saved = (deploy.apply_post_merge_deploy, history_mod.clear_branch,
             history_mod.get_branch_for_instance)
    deploy.apply_post_merge_deploy = fake_deploy
    history_mod.clear_branch = cleared.append
    history_mod.get_branch_for_instance = lambda iid: None
    try:
        fn(deploys, cleared)
    finally:
        (deploy.apply_post_merge_deploy, history_mod.clear_branch,
         history_mod.get_branch_for_instance) = saved


def test_landed_merge_clears_siblings() -> None:
    def body(deploys, cleared):
        inst, sibling = make_inst("t-1"), make_inst("t-0")
        store = Store(inst, sibling)
        store.pending["t-1"] = {"failure_kind": "conflict"}
        ctx, _ = make_ctx(Runner("Merged into master."), store)
        msg = run(steps.merge_and_record(ctx, inst))
        check("a landed merge returns the runner's message", msg == "Merged into master.", msg)
        check("the sibling sharing the branch is cleared by its pre-merge name",
              sibling.branch is None and sibling.worktree_path is None, str(sibling.branch))
        check("history is cleared for that branch", cleared == ["claude-bot/t-1"], str(cleared))
        check("the pending merge is dropped", "t-1" not in store.pending)
        check("deploy bookkeeping runs for a tree that may ship", deploys == ["t-1"], str(deploys))
    patched(body)


def test_blocked_tree_skips_deploy() -> None:
    for marker, label in ((RELEASE_ORPHANED_MARKER, "missing a release"),
                          (REPO_UNUSABLE_MARKER, "left broken")):
        def body(deploys, cleared, marker=marker, label=label):
            inst, sibling = make_inst("t-1"), make_inst("t-0")
            ctx, _ = make_ctx(Runner(f"Merged into master.\n{marker}: x"), Store(inst, sibling))
            msg = run(steps.merge_and_record(ctx, inst))
            check(f"a tree {label} is reported as blocking ship", steps.merge_blocks_ship(msg))
            check(f"a tree {label} gets no deploy bookkeeping", deploys == [], str(deploys))
            check(f"a tree {label} still clears the landed branch", sibling.branch is None)
        patched(body)


def test_failure_refreshes_kind() -> None:
    def body(deploys, cleared):
        inst, sibling = make_inst("t-1"), make_inst("t-0")
        store = Store(inst, sibling)
        store.pending["t-1"] = {"failure_kind": "conflict"}
        ctx, tagged = make_ctx(Runner("Merge failed: diverged", kind="diverged"), store)
        text, msg = run(steps.merge_from_user(ctx, inst))
        check("a failed merge keeps the pending merge", "t-1" in store.pending)
        check("and refreshes its failure kind", store.pending["t-1"]["failure_kind"] == "diverged",
              str(store.pending))
        check("the card carries the banner for this attempt's kind",
              text.startswith("Merge failed: diverged\n\n") and len(text) > len(msg), text)
        check("nothing is cleared, tagged or deployed",
              sibling.branch and not tagged and not deploys and not cleared)
    patched(body)


def test_button_holds_thread_on_blocked_tree() -> None:
    def body(deploys, cleared):
        for marker, expect_closed in (("", 1), (RELEASE_ORPHANED_MARKER, 0)):
            inst = make_inst("t-1")
            messenger = Messenger()
            ctx, tagged = make_ctx(Runner(f"Merged into master.\n{marker}"), Store(inst), messenger)
            saved = branch_actions._strip_post_merge_buttons

            async def no_strip(*a, **kw):
                pass

            branch_actions._strip_post_merge_buttons = no_strip
            try:
                text, msg = run(steps.merge_from_user(ctx, inst))
                run(branch_actions._finish_branch_action(
                    ctx, inst, text, None, keep_open=steps.merge_blocks_ship(msg)))
            finally:
                branch_actions._strip_post_merge_buttons = saved
            what = "a release-missing tree" if marker else "a clean tree"
            check(f"Merge on {what}: thread closed {expect_closed} time(s)",
                  messenger.closed == expect_closed, str(messenger.closed))
            check(f"Merge on {what}: merged tag applied", tagged == [True])
    patched(body)


def test_unattended_failure_recorded_when_post_fails() -> None:
    inst = make_inst("t-1")
    store = Store(inst)
    ctx, _ = make_ctx(Runner("Merge failed: x", kind="conflict"), store, Messenger(fail_send=True))
    ctx.runner._last_merge_failure_kind["t-1"] = "conflict"
    run(steps.record_merge_failure(ctx, inst, session_id="s1", channel_id="thread-9"))
    meta = store.pending.get("t-1") or {}
    check("a failed unattended merge is recorded even if the post fails",
          meta.get("channel_id") == "thread-9" and meta.get("session_id") == "s1"
          and meta.get("failure_kind") == "conflict" and meta.get("repo_name") == "r", str(meta))

    messenger = Messenger()
    ctx, _ = make_ctx(ctx.runner, Store(inst), messenger)
    run(steps.record_merge_failure(ctx, inst, session_id="s1", lead="Lead.\n\n", silent=False))
    post = messenger.sent[0] if messenger.sent else {}
    check("the banner posts to the ctx channel with the lead, buttons and ping",
          post.get("channel") == "c1" and post.get("text", "").startswith("Lead.\n\n")
          and post.get("buttons") and post.get("silent") is False, str(post))


def test_finalize_reports_stopped_on_blocked_tree() -> None:
    """E17: both ship-blocking paths are STOPPED, never MERGED or FAILED.

    MERGED is what made the chain log "complete" for a chain that halted
    before deploy and close. FAILED would be as wrong the other way: the
    branch landed, so the conflict banner and its Resolve / Retry buttons
    would offer to redo a merge that already happened.
    """
    def body(deploys, cleared):
        cases = (
            ("", steps.MergeOutcome.MERGED, 1, "a clean tree"),
            (f"\n{RELEASE_ORPHANED_MARKER}: x", steps.MergeOutcome.STOPPED, 0,
             "a tree missing a release"),
            (f"\n{REPO_UNUSABLE_MARKER}: x", steps.MergeOutcome.STOPPED, 0,
             "a broken tree"),
        )
        for suffix, want, want_closed, what in cases:
            deploys.clear()
            inst = make_inst("t-1")
            store = Store(inst)
            store.pending["t-1"] = {"failure_kind": "conflict"}
            messenger = Messenger()
            ctx, tagged = make_ctx(Runner(f"Merged into master.{suffix}"), store, messenger)
            got = run(steps._finalize_merge(ctx, inst, close_silent=True))
            check(f"finalize on {what} returns {want.name}", got is want, str(got))
            check(f"finalize on {what}: thread closed {want_closed} time(s)",
                  messenger.closed == want_closed, str(messenger.closed))
            check(f"finalize on {what}: deploy bookkeeping only when merged",
                  deploys == (["t-1"] if want is steps.MergeOutcome.MERGED else []),
                  str(deploys))
            check(f"finalize on {what}: the landed branch drops its pending merge",
                  "t-1" not in store.pending, str(store.pending))
            check(f"finalize on {what}: merged tag applied", tagged == [True])
            if want is steps.MergeOutcome.STOPPED:
                check(f"finalize on {what}: the warning pings (not silent)",
                      any(m["text"].startswith("\u26d4") and not m.get("silent")
                          for m in messenger.sent), str(messenger.sent))
                # The chain pings about the stop itself, so it asks for the
                # warning quietly: one event, one notification.
                messenger = Messenger()
                inst = make_inst("t-1")
                ctx, _ = make_ctx(Runner(f"Merged into master.{suffix}"), Store(inst), messenger)
                quiet = run(steps._finalize_merge(ctx, inst, stop_silent=True))
                check(f"finalize on {what} with stop_silent: still STOPPED", quiet is want)
                check(f"finalize on {what} with stop_silent: the warning posts silently",
                      any(m["text"].startswith("\u26d4") and m.get("silent") is True
                          for m in messenger.sent), str(messenger.sent))

        inst = make_inst("t-1")
        ctx, tagged = make_ctx(Runner("Merge failed: conflict"), Store(inst))
        got = run(steps._finalize_merge(ctx, inst))
        check("finalize on a failed merge returns FAILED",
              got is steps.MergeOutcome.FAILED, str(got))
        check("a failed merge is not tagged merged", tagged == [])
        check("a branchless target is FAILED, not a crash",
              run(steps._finalize_merge(ctx, make_inst("t-2", branch=None)))
              is steps.MergeOutcome.FAILED)
    patched(body)

    # The stop has to come before the deploy and the close, in that function's
    # own text, or a later edit can reorder it while the behaviour checks above
    # still pass on the fakes.
    src = inspect.getsource(steps._finalize_merge)
    close = src.find("close_conversation")
    for name in ("merge_msg_repo_unusable", "merge_msg_release_orphaned"):
        at = src.find(name)
        check(f"{name} is checked before the thread is closed",
              at != -1 and close != -1 and at < close)
    check("STOPPED is returned on both blocking paths",
          src.count("return MergeOutcome.STOPPED") == 2)


def test_every_path_uses_the_helper() -> None:
    src = inspect.getsource(resolver._on_resolve_merge)
    check("both resolver exits merge through merge_from_user",
          src.count("merge_from_user(") == 2 and "merge_branch(" not in src)
    check("/merge merges through merge_from_user",
          "merge_from_user(" in inspect.getsource(branch_actions.on_merge))
    for mod in ("bot.engine.chain_runner", "bot.discord.fleet", "bot.autonomy"):
        text = Path(sys.modules["bot.engine.steps"].__file__).parents[2].joinpath(
            *mod.split(".")).with_suffix(".py").read_text(encoding="utf-8")
        check(f"{mod} records merge failures through the shared helper",
              "record_merge_failure(" in text and "set_pending_merge(" not in text)


def main() -> int:
    test_landed_merge_clears_siblings()
    test_blocked_tree_skips_deploy()
    test_failure_refreshes_kind()
    test_button_holds_thread_on_blocked_tree()
    test_unattended_failure_recorded_when_post_fails()
    test_finalize_reports_stopped_on_blocked_tree()
    test_every_path_uses_the_helper()
    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
