#!/usr/bin/env python3
"""Harness: a release verifier that never ran is not a pass (E18).

``on_verify_release`` returns None when no verifier ran at all (no repo, no
entry SHA, nothing committed, a spawn that failed). The chain runner read that
as a failed step, while the Amend button's path read it as a pass and resumed
the chain straight into the release step. One meaning now, "not passed", held
in ``release_steps.release_verified``:

- the predicate: None, a failed run and a mismatch are all not passed
- after an Amend, a verifier result of None leaves the chain paused and posts
  Amend / Continue anyway instead of resuming
- a passing verifier after an Amend still resumes from the verifier's result
- a mismatch after an Amend does not resume and adds no second notice (the
  verifier already posted its own gate UI)

The chain runner's half is in ``scripts/test_autopilot_chain.py`` ("verify_release
returned None").

Run: python scripts/test_verify_release_none.py
Exit 0 = all pass.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.claude.types import (  # noqa: E402
    Instance, InstanceOrigin, InstanceStatus, InstanceType,
)
from bot.engine import chain_runner  # noqa: E402
from bot.engine.release_steps import release_verified  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def make_inst(iid: str, *, status=InstanceStatus.COMPLETED, needs_input=False,
              origin=InstanceOrigin.VERIFY_RELEASE) -> Instance:
    inst = Instance(
        id=iid, name=None, instance_type=InstanceType.QUERY, prompt="",
        repo_name="r", repo_path="/nonexistent/r", status=status,
        mode="explore", origin=origin,
    )
    inst.session_id = "sess-1"
    inst.needs_input = needs_input
    return inst


class Store:
    def __init__(self, *insts: Instance):
        self.insts = {i.id: i for i in insts}

    def get_instance(self, iid):
        return self.insts.get(iid)

    def update_instance(self, inst, **_):
        self.insts[inst.id] = inst

    def get_autopilot_chain(self, session_id):
        return ["release", "merge"]

    def get_chain_entry_sha(self, session_id):
        return "abc123"


class Messenger:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, channel_id, text, buttons=None, **kw):
        self.sent.append({"text": text, "buttons": buttons, **kw})
        return "m1"


def amend_with(verify_result) -> tuple[list[tuple], Messenger, Instance]:
    """Run on_amend_done with the verifier stubbed to return ``verify_result``."""
    vr = make_inst("vr-1", needs_input=True)
    done = make_inst("done-1", origin=InstanceOrigin.DONE)
    # A resumed step can run under a session of its own, and the chain is
    # keyed by the verifier's, so Continue anyway has to point at vr.
    done.session_id = "sess-done"
    messenger = Messenger()
    ctx = SimpleNamespace(
        store=Store(vr, done), messenger=messenger, channel_id="c1", platform="discord",
    )
    resumed: list[tuple] = []

    async def fake_done(ctx, source_id, source_msg_id=None, **kw):
        return done

    async def fake_verify(ctx, source_id, source_msg_id=None, **kw):
        return verify_result

    async def fake_resume(ctx, source_id, source_msg_id, session_id):
        resumed.append((source_id, session_id))
        return None

    saved = (chain_runner.on_done, chain_runner.on_verify_release,
             chain_runner.resume_autopilot_chain, chain_runner._last_msg_id)
    chain_runner.on_done = fake_done
    chain_runner.on_verify_release = fake_verify
    chain_runner.resume_autopilot_chain = fake_resume
    chain_runner._last_msg_id = lambda inst, platform: None
    try:
        asyncio.run(chain_runner.on_amend_done(ctx, "vr-1", None))
    finally:
        (chain_runner.on_done, chain_runner.on_verify_release,
         chain_runner.resume_autopilot_chain, chain_runner._last_msg_id) = saved
    return resumed, messenger, done


def test_predicate() -> None:
    print("\nrelease_verified")
    check("None is not passed", release_verified(None) is False)
    check("a completed run with no question is passed",
          release_verified(make_inst("a")) is True)
    check("a mismatch (needs input) is not passed",
          release_verified(make_inst("b", needs_input=True)) is False)
    check("a failed run is not passed",
          release_verified(make_inst("c", status=InstanceStatus.FAILED)) is False)


def test_amend_none_stays_paused() -> None:
    print("\nAmend, then the verifier does not run")
    resumed, messenger, done = amend_with(None)
    check("the chain is not resumed", resumed == [], str(resumed))
    check("one notice is posted", len(messenger.sent) == 1, str(messenger.sent))
    post = messenger.sent[0] if messenger.sent else {}
    check("the notice says the release is unverified and pings",
          "unverified" in post.get("text", "") and post.get("silent") is False, str(post))
    ids = [b.callback_data for row in (post.get("buttons") or []) for b in row]
    check("it offers Amend on the original verifier run",
          "amend:vr-1" in ids, str(ids))
    check("and Continue anyway on the verifier run, whose session keys the chain",
          "continue_anyway:vr-1" in ids and f"continue_anyway:{done.id}" not in ids,
          str(ids))


def test_amend_pass_resumes() -> None:
    print("\nAmend, then the verifier passes")
    passed = make_inst("vr-2")
    resumed, messenger, _ = amend_with(passed)
    check("the chain resumes from the verifier's result",
          resumed == [("vr-2", "sess-1")], str(resumed))
    check("no notice is posted", messenger.sent == [], str(messenger.sent))


def test_amend_mismatch_does_not_resume() -> None:
    print("\nAmend, then the verifier flags a mismatch again")
    resumed, messenger, _ = amend_with(make_inst("vr-3", needs_input=True))
    check("the chain is not resumed", resumed == [], str(resumed))
    check("no second notice (the verifier posts its own gate UI)",
          messenger.sent == [], str(messenger.sent))


def main() -> int:
    test_predicate()
    test_amend_none_stays_paused()
    test_amend_pass_resumes()
    test_amend_mismatch_does_not_resume()
    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed.")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
