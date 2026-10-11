#!/usr/bin/env python3
"""Characterization test: the autopilot chain loop in bot/engine/workflows.py.

``_run_autopilot_chain`` drives build -> sensors -> review_code -> verify ->
done -> verify_release -> release -> merge, and every entry point (Autopilot,
Hold, Build & Ship, the conversational ``/chain`` presets) is a different step
list handed to it. Nothing tested the loop, and it is about to be reshaped into
a dispatch table, so this file pins what it does TODAY. It is a safety net, not
a statement that every behaviour here is ideal.

What is pinned:
  * the ordered step list each entry point runs when every step succeeds
    (ship, hold, verify, unknown preset, Autopilot, Autopilot hold, Build &
    Ship), plus ``start_from`` slicing;
  * what each step is handed (the previous step's instance id and message id),
    and the chain queue on the store at the moment each step runs (the
    *remaining* steps, including the one in flight);
  * the label / silent-close kwargs a launch persists, and that a terminal exit
    reaps them with the chain queue;
  * the merge path: ship reaches ``_finalize_merge`` (silent close for ship and
    Build & Ship, loud for Autopilot), hold and verify never do and post the
    Merge / Discard / Diff handoff instead;
  * failure exits: a failed step (state cleared, "blocked at <step>" ping), a
    step that asks for input (state kept, status "paused", queue starts at the
    stopped step), verify_release needing input (phantom_detected, plus the
    parse-failed wording), a release failure (recovery hint), merge failure
    (banner, pending merge, merge_failed), budget exhaustion, a review loop
    that does not converge, an empty-diff build (dirty rescue vs halt);
  * verify_release skip rules (no entry SHA, entry == HEAD);
  * multi-phase builds (spawn sequence, design gate, risk gate);
  * resume (`resume_autopilot_chain` skips the answered step, restores kwargs).

Everything external is stubbed at the ``workflows`` module attribute the loop
reads: the step functions (``on_sensors``, ``on_review_code``, ``on_verify``,
``on_done``, ``on_verify_release``, ``on_release_chain``, ``spawn_from``,
``_review_plan_loop``, ``_finalize_merge``), the git helpers (``_git_head``,
``_git_head_sha``, ``_resolve_chain_repo_path``, ``_find_mergeable_instance``,
``_find_phase_plan``, ``_extract_latest_plan_text``,
``_attempt_finishup_nudge``) and ``_eval_chain_safe``. The store is a real
``StateStore`` in a temp directory; the messenger and runner are recording
fakes.

Run: python scripts/test_autopilot_chain.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.claude.types import (  # noqa: E402
    ChainPhaseState, Instance, InstanceOrigin, InstanceStatus, InstanceType,
    Phase,
)
from bot.engine import workflows  # noqa: E402
from bot.platform.base import RequestContext  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def check_eq(label: str, got, want) -> None:
    check(label, got == want, f"got {got!r}, want {want!r}")


SESSION = "sess-1"
SOURCE_ID = "q-1"
SOURCE_MSG = "m-source"
MENTION = "<@u1>"

SHIP = ["build", "sensors", "review_code", "verify",
        "done", "verify_release", "release", "merge"]
HOLD = ["build", "sensors", "review_code", "verify",
        "done", "verify_release", "release"]
VERIFY = ["build", "sensors", "review_code", "verify"]
AUTOPILOT = ["review_loop"] + SHIP
AUTOPILOT_HOLD = ["review_loop"] + HOLD


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class FakeMessenger:
    def __init__(self) -> None:
        self.texts: list[dict] = []
        self.results: list[dict] = []
        self.closed = False

    async def send_text(self, channel_id, text, buttons=None, silent=False, **kw):
        self.texts.append({"text": text, "buttons": buttons, "silent": silent})
        return "m-sent"

    async def send_result(self, channel_id, text, metadata=None, silent=False, **kw):
        self.results.append({"text": text, "metadata": metadata, "silent": silent})
        return "m-result"

    async def is_conversation_closed(self, channel_id) -> bool:
        return self.closed

    def format_mention(self, user_id) -> str:
        return f"<@{user_id}>"

    async def close_conversation(self, *a, **kw) -> None:
        return None

    def mentions(self) -> list[str]:
        return [t["text"] for t in self.texts if t["text"].startswith(MENTION)]


class FakeRunner:
    def __init__(self) -> None:
        self.began: list[str] = []
        self.ended: list[str] = []
        self._last_merge_failure_kind: dict[str, str | None] = {}
        self.rescue_sha: str | None = None
        self.rescued: list[str] = []
        self.discarded: list[str] = []

    def begin_task(self, task_id: str) -> None:
        self.began.append(task_id)

    def end_task(self, task_id: str) -> None:
        self.ended.append(task_id)

    async def auto_commit_dirty_worktree(self, inst):
        self.rescued.append(inst.id)
        return self.rescue_sha

    async def discard_branch(self, inst, preserve_if_dirty=False):
        self.discarded.append(inst.id)
        return SimpleNamespace(preserved_branch=None)


class Scenario:
    """One isolated chain run: store, fakes, patches, and call recordings.

    ``behaviour`` maps a step name to what its stub returns:
    ``ok`` (default), ``failed``, ``needs_input``, ``none`` (spawn failure),
    ``parse_failed`` (verify_release only: needs input + parse-failed flag).
    """

    def __init__(
        self,
        behaviour: dict[str, str] | None = None,
        *,
        head_mode: str = "moves",       # "moves" | "parked" for the build guard
        merge_ok: bool = True,
        mergeable: bool = True,
        entry_sha_mode: str = "differs",  # "differs" | "same" | "none"
        phases: list[Phase] | None = None,
        plan_text: str = "",
        plan_active: bool = False,
        step_cost: float | None = None,
        converged: bool = True,
        review_deferred: list[str] | None = None,
        rescue_sha: str | None = None,
    ) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.store = StateStore(base / "state.json", base / "results")
        self.messenger = FakeMessenger()
        self.runner = FakeRunner()
        self.runner.rescue_sha = rescue_sha
        self.behaviour = behaviour or {}
        self.head_mode = head_mode
        self.merge_ok = merge_ok
        self.mergeable = mergeable
        self.entry_sha_mode = entry_sha_mode
        self.phases = phases or []
        self.plan_text = plan_text
        self.step_cost = step_cost
        self.converged = converged
        self.review_deferred = review_deferred
        self.repo_path = str(base / "repo")

        self.calls: list[dict] = []        # one entry per step stub invocation
        self.spawns: list[dict] = []       # every spawn_from call
        self.evals: list[dict] = []        # every _eval_chain_safe call
        self.finalized: list[dict] = []    # every _finalize_merge call
        self.produced: dict[str, str] = {}   # step -> instance id it returned (latest)
        self.minted: list[tuple[str, str]] = []   # every (step, instance id) returned
        self._n = 0
        self._head_n = 0
        self._sha_calls = 0
        self._undo: list[tuple[object, str, object]] = []

        self.source = self._make("q", plan_active=plan_active, iid=SOURCE_ID)
        self.source.origin = InstanceOrigin.DIRECT
        self.source.branch = None
        self.source.original_branch = None
        self.source.message_ids = {"discord": [SOURCE_MSG]}
        self.source.session_id = SESSION
        self.store.update_instance(self.source)

        self.ctx = RequestContext(
            messenger=self.messenger, channel_id="chan-1", platform="discord",
            store=self.store, runner=self.runner, session_id=SESSION,
            user_id="u1",
        )
        self._install()

    # ----- instance factory
    def _make(self, prefix: str, *, plan_active: bool = False, iid: str | None = None) -> Instance:
        self._n += 1
        iid = iid or f"{prefix}-{self._n + 100}"
        return Instance(
            id=iid, name=None, instance_type=InstanceType.TASK, prompt="x",
            repo_name="", repo_path=self.repo_path,
            status=InstanceStatus.COMPLETED, origin=InstanceOrigin.BUILD,
            session_id=SESSION, branch="claude-bot/" + iid,
            original_branch="master", plan_active=plan_active,
            message_ids={"discord": [f"m-{iid}"]},
            worktree_path=self.repo_path + "/.worktrees/" + iid,
            cost_usd=self.step_cost,
        )

    def _result_for(self, step: str) -> Instance | None:
        kind = self.behaviour.get(step, "ok")
        if kind == "none":
            return None
        inst = self._make(step)
        if kind == "failed":
            inst.status = InstanceStatus.FAILED
        elif kind in ("needs_input", "parse_failed"):
            inst.needs_input = True
            if kind == "parse_failed":
                inst._verifier_parse_failed = True
        self.store.update_instance(inst)
        self.produced[step] = inst.id
        self.minted.append((step, inst.id))
        return inst

    def _record(self, step: str, current_id: str, current_msg, **extra) -> None:
        self.calls.append({
            "step": step, "id": current_id, "msg": current_msg,
            "queue": list(self.store.get_autopilot_chain(SESSION) or []),
            "kwargs": self.store.get_chain_kwargs(SESSION),
            **extra,
        })

    # ----- patching
    def _patch(self, name: str, value) -> None:
        self._undo.append((workflows, name, getattr(workflows, name)))
        setattr(workflows, name, value)

    def _install(self) -> None:
        sc = self

        def step_stub(step: str):
            async def stub(ctx, current_id, current_msg=None, *a, **kw):
                sc._record(step, current_id, current_msg, kw=dict(kw))
                if step == "sensors" and sc.behaviour.get("sensors", "ok") == "ok":
                    # An all-pass sensors step returns the build instance unchanged.
                    sc.produced[step] = current_id
                    return ctx.store.get_instance(current_id)
                return sc._result_for(step)
            return stub

        async def spawn_from(ctx, source_id, cfg, source_msg_id=None, **kw):
            sc.spawns.append({
                "source": source_id, "origin": cfg.origin, "mode": cfg.mode,
                "prompt": cfg.prompt, "auto_branch": cfg.auto_branch,
                "copy_branch": cfg.copy_branch, "resume": cfg.resume_session,
                "msg": source_msg_id,
            })
            if cfg.origin == InstanceOrigin.BUILD:
                sc._record("build", source_id, source_msg_id)
                return sc._result_for("build")
            raise AssertionError(f"unexpected spawn origin {cfg.origin}")

        async def review_loop(ctx, current_id, current_msg, chain_instances):
            sc._record("review_loop", current_id, current_msg)
            inst = sc._result_for("review_loop")
            if inst is not None and sc.review_deferred:
                inst.deferred_revisions = list(sc.review_deferred)
            return inst, (not sc.converged)

        async def finalize_merge(ctx, target, *, close_silent=False, skip_close=False):
            sc.finalized.append({"id": target.id, "close_silent": close_silent})
            sc._record("merge", target.id, None)
            return sc.merge_ok

        async def git_head(path):
            if sc.head_mode == "parked":
                return "head-const"
            sc._head_n += 1
            return f"head-{sc._head_n}"

        def git_head_sha(repo_path):
            sc._sha_calls += 1
            if sc.entry_sha_mode == "same":
                return "sha-same"
            return "sha-entry" if sc._sha_calls == 1 else "sha-head"

        def resolve_repo(*candidates):
            return None if sc.entry_sha_mode == "none" else sc.repo_path

        def find_mergeable(store, session_id):
            if not sc.mergeable:
                return None
            return sc._make("merge-target")

        def find_phase_plan(store, inst, max_depth=24):
            return list(sc.phases)

        def extract_plan(ctx, chain_instances, source_id):
            return sc.plan_text

        async def finishup_nudge(ctx, parent, baseline, source_msg_id, phase_label=None):
            return None, False

        def eval_chain(store, root_id, expected, completed, instances, outcome, intervention=False):
            sc.evals.append({
                "root": root_id, "expected": list(expected),
                "completed": list(completed),
                "instances": [i.id for i in instances],
                "outcome": outcome, "intervention": intervention,
            })

        self._patch("spawn_from", spawn_from)
        self._patch("on_sensors", step_stub("sensors"))
        self._patch("on_review_code", step_stub("review_code"))
        self._patch("on_verify", step_stub("verify"))
        self._patch("on_done", step_stub("done"))
        self._patch("on_verify_release", step_stub("verify_release"))
        self._patch("on_release_chain", step_stub("release"))
        self._patch("_review_plan_loop", review_loop)
        self._patch("_finalize_merge", finalize_merge)
        self._patch("_git_head", git_head)
        self._patch("_git_head_sha", git_head_sha)
        self._patch("_resolve_chain_repo_path", resolve_repo)
        self._patch("_find_mergeable_instance", find_mergeable)
        self._patch("_find_phase_plan", find_phase_plan)
        self._patch("_extract_latest_plan_text", extract_plan)
        self._patch("_attempt_finishup_nudge", finishup_nudge)
        self._patch("_eval_chain_safe", eval_chain)

    def close(self) -> None:
        for mod, name, orig in reversed(self._undo):
            setattr(mod, name, orig)
        self.tmp.cleanup()

    # ----- views
    @property
    def order(self) -> list[str]:
        return [c["step"] for c in self.calls]

    @property
    def queue(self):
        return self.store.get_autopilot_chain(SESSION)

    @property
    def status(self):
        meta = self.store.get_autopilot_chain_meta(SESSION)
        return meta.get("status") if meta else None

    def final_eval(self) -> dict:
        return self.evals[-1] if self.evals else {}


def run(coro):
    return asyncio.run(coro)


def blocked_mention(mentions: list[str], label: str, step: str) -> bool:
    """Exactly one ping: "<mention> <label> blocked at <step> <dash> needs your attention."."""
    if len(mentions) != 1:
        return False
    m = mentions[0]
    return (m.startswith(f"{MENTION} {label} blocked at {step} ")
            and m.endswith(" needs your attention."))


def scenario(*a, **kw) -> Scenario:
    return Scenario(*a, **kw)


# --------------------------------------------------------------------------
# 1. happy path: step order per entry point
# --------------------------------------------------------------------------

print()
print("happy path: step order per entry point")


def _entry(label: str, launch, want_steps: list[str], *, want_label: str,
           want_silent: bool, merges: bool, **sc_kw) -> Scenario:
    sc = scenario(**sc_kw)
    run(launch(sc))
    check_eq(f"{label}: ordered steps", sc.order, want_steps)
    check_eq(f"{label}: persisted kwargs seen by every step",
             {c["kwargs"] for c in sc.calls}, {(want_label, want_silent)})
    check_eq(f"{label}: finalized merge",
             [f["close_silent"] for f in sc.finalized],
             [want_silent] if merges else [])
    ev = sc.final_eval()
    check_eq(f"{label}: eval outcome", ev.get("outcome"), "merged" if merges else "completed")
    check_eq(f"{label}: eval completed == steps", ev.get("completed"), want_steps)
    check_eq(f"{label}: eval expected is the full list", ev.get("expected"), want_steps)
    check_eq(f"{label}: eval root is the source", ev.get("root"), SOURCE_ID)
    check_eq(f"{label}: chain queue cleared at the end", sc.queue, None)
    check_eq(f"{label}: kwargs reaped with the queue",
             sc.store.get_chain_kwargs(SESSION), ("Autopilot", False))
    check_eq(f"{label}: chain task opened and closed",
             (sc.runner.began, sc.runner.ended),
             ([f"chain:{SOURCE_ID}"], [f"chain:{SOURCE_ID}"]))
    check_eq(f"{label}: no step ran twice in chain eval",
             len(set(ev.get("instances", []))), len(ev.get("instances", [])))
    return sc


def _chain(preset):
    return lambda sc: workflows.on_conversational_chain(sc.ctx, SOURCE_ID, preset, SOURCE_MSG)


ship = _entry("chain ship", _chain("ship"), SHIP, want_label="Chain (ship)",
              want_silent=True, merges=True)
hold = _entry("chain hold", _chain("hold"), HOLD, want_label="Chain (hold)",
              want_silent=False, merges=False)
ver = _entry("chain verify", _chain("verify"), VERIFY, want_label="Chain (verify)",
             want_silent=False, merges=False)
unk = _entry("chain unknown preset", _chain("bogus"), SHIP, want_label="Chain",
             want_silent=False, merges=True)
_entry("autopilot", lambda sc: workflows.on_autopilot(sc.ctx, SOURCE_ID, SOURCE_MSG),
       AUTOPILOT, want_label="Autopilot", want_silent=False, merges=True)
_entry("autopilot hold", lambda sc: workflows.on_autopilot_hold(sc.ctx, SOURCE_ID, SOURCE_MSG),
       AUTOPILOT_HOLD, want_label="Autopilot", want_silent=False, merges=False)
_entry("build and ship", lambda sc: workflows.on_build_and_ship(sc.ctx, SOURCE_ID, SOURCE_MSG),
       SHIP, want_label="Build & Ship", want_silent=True, merges=True)

print()
print("step arguments and the chain queue the store shows while each step runs")

check_eq("ship: queue at each step is the remaining steps (in-flight first)",
         [c["queue"] for c in ship.calls], [SHIP[i:] for i in range(len(SHIP))])
check_eq("ship: build is handed the source id and message",
         (ship.calls[0]["id"], ship.calls[0]["msg"]), (SOURCE_ID, SOURCE_MSG))
chain_calls = [c for c in ship.calls if c["step"] != "merge"]
handed_ok = all(
    chain_calls[i]["id"] == ship.produced[chain_calls[i - 1]["step"]]
    for i in range(1, len(chain_calls))
)
check("ship: each step is handed the instance the previous step produced", handed_ok)
sens = next(c for c in ship.calls if c["step"] == "sensors")
rc = next(c for c in ship.calls if c["step"] == "review_code")
check_eq("ship: an all-pass sensors step hands the build instance on unchanged",
         sens["id"], rc["id"])
check_eq("ship: review_code gets the build's message id",
         rc["msg"], f"m-{rc['id']}")
done_call = next(c for c in ship.calls if c["step"] == "done")
check_eq("ship: done runs with the chain prompt variant",
         done_call["kw"], {"prompt_variant": "chain"})
vr = next(c for c in ship.calls if c["step"] == "verify_release")
check_eq("ship: verify_release is handed the entry SHA snapshotted at done",
         vr["kw"], {"entry_sha": "sha-entry"})
check_eq("ship: build spawns in build mode, auto-branched, resuming, from the source",
         (ship.spawns[0]["mode"], ship.spawns[0]["auto_branch"],
          ship.spawns[0]["resume"], ship.spawns[0]["source"]),
         ("build", True, True, SOURCE_ID))
check_eq("ship: no plan text means the plain query prompt",
         ship.spawns[0]["prompt"], config.BUILD_FROM_QUERY_PROMPT)

s = scenario(plan_active=True, plan_text="PLAN-BODY")
run(_chain("hold")(s))
check("plan_active source uses the plan prompt with the plan injected first",
      s.spawns[0]["prompt"].startswith(
          config.BUILD_PLAN_INJECTION_PREFIX.replace("{plan_text}", "PLAN-BODY"))
      and s.spawns[0]["prompt"].endswith(config.BUILD_FROM_PLAN_PROMPT))
s.close()

print()
print("start_from slicing and unknown ids")

s = scenario()
run(workflows.on_autopilot(s.ctx, SOURCE_ID, SOURCE_MSG, start_from="verify"))
check_eq("autopilot start_from=verify runs verify onwards",
         s.order, ["verify", "done", "verify_release", "release", "merge"])
s.close()

s = scenario()
run(workflows.on_autopilot(s.ctx, SOURCE_ID, SOURCE_MSG, start_from="nonsense"))
check_eq("unknown start_from falls back to the first step", s.order, AUTOPILOT)
s.close()

s = scenario()
res = run(workflows.on_autopilot(s.ctx, "q-404", SOURCE_MSG))
check("unknown source: nothing runs, nothing returned",
      res is None and s.calls == [] and s.runner.began == [])
check_eq("unknown source: user told", s.messenger.texts[0]["text"], "Instance not found.")
s.close()

print()
print("hold and verify hand off to Merge / Discard / Diff, ship does not")

for label, sc in (("hold", hold), ("verify", ver)):
    handoff = [t for t in sc.messenger.texts if "Chain complete on branch" in t["text"]]
    check_eq(f"{label}: one handoff message", len(handoff), 1)
    btns = [b.callback_data.split(":")[0] for b in handoff[0]["buttons"][0]]
    check_eq(f"{label}: handoff buttons", btns, ["merge", "discard", "diff"])
    check_eq(f"{label}: handoff pings (not silent)", handoff[0]["silent"], False)
    check_eq(f"{label}: never reaches _finalize_merge", sc.finalized, [])
check("ship: no handoff message (it merged)",
      not [t for t in ship.messenger.texts if "Chain complete on branch" in t["text"]])
check_eq("ship: finalized the instance the release step produced",
         ship.finalized[0]["id"], ship.produced["release"])
for sc in (ship, hold, ver, unk):
    sc.close()

print()
print("merge step when the release produced no mergeable instance")

s = scenario(mergeable=False)
run(_chain("ship")(s))
check_eq("merge target is the release result when it carries a branch/original_branch",
         [f["id"] for f in s.finalized], [s.produced["release"]])
s.close()

s = scenario(mergeable=False)
_orig_make = s._make


def _branchless(prefix, **kw):
    inst = _orig_make(prefix, **kw)
    if prefix == "release":
        inst.branch = None
    return inst


s._make = _branchless
run(_chain("ship")(s))
check_eq("no branch on the release result and no mergeable instance: merge is a no-op",
         (s.finalized, s.final_eval().get("outcome"), s.final_eval().get("completed")),
         ([], "merged", SHIP))
s.close()


# --------------------------------------------------------------------------
# 2. failure and pause exits
# --------------------------------------------------------------------------

print()
print("a failed step ends the chain and clears its state")

for step in ("build", "sensors", "review_code", "verify", "done", "release"):
    s = scenario({step: "failed"})
    res = run(_chain("ship")(s))
    idx = SHIP.index(step)
    check_eq(f"failed {step}: ran up to and including it",
             s.order, SHIP[: idx + 1])
    check_eq(f"failed {step}: eval outcome", s.final_eval().get("outcome"), "failed")
    check_eq(f"failed {step}: eval completed steps",
             s.final_eval().get("completed"), SHIP[:idx])
    check_eq(f"failed {step}: marked as intervention", s.final_eval().get("intervention"), True)
    check_eq(f"failed {step}: queue cleared", s.queue, None)
    check_eq(f"failed {step}: kwargs cleared with it",
             s.store.get_chain_kwargs(SESSION), ("Autopilot", False))
    check(f"failed {step}: mention names label and step",
          blocked_mention(s.messenger.mentions(), "Chain (ship)", step),
          repr(s.messenger.mentions()))
    check(f"failed {step}: returns the failed instance",
          res is not None and res.status == InstanceStatus.FAILED)
    check(f"failed {step}: the failed instance is in the eval list",
          res is not None and res.id in s.final_eval().get("instances", []))
    check_eq(f"failed {step}: merge never reached", s.finalized, [])
    check_eq(f"failed {step}: chain task still released",
             s.runner.ended, [f"chain:{SOURCE_ID}"])
    if step == "release":
        check("failed release: posts the partial-release recovery hint",
              any("Release step halted mid-sequence" in t["text"] for t in s.messenger.texts))
    else:
        check(f"failed {step}: no release hint",
              not any("Release step halted" in t["text"] for t in s.messenger.texts))
    s.close()

s = scenario({"verify": "none"})
res = run(_chain("hold")(s))
check("a step returning None (spawn failure) is a failed exit",
      res is None and s.final_eval().get("outcome") == "failed"
      and blocked_mention(s.messenger.mentions(), "Chain (hold)", "verify"))
s.close()

print()
print("a step that asks for input pauses the chain and keeps its state")

for step in ("build", "review_code", "verify", "done", "release"):
    s = scenario({step: "needs_input"})
    run(_chain("ship")(s))
    idx = SHIP.index(step)
    check_eq(f"{step} needs input: ran up to it", s.order, SHIP[: idx + 1])
    check_eq(f"{step} needs input: eval outcome", s.final_eval().get("outcome"), "needs_input")
    check_eq(f"{step} needs input: queue starts at the stopped step", s.queue, SHIP[idx:])
    check_eq(f"{step} needs input: status paused", s.status, "paused")
    check_eq(f"{step} needs input: kwargs kept for resume",
             s.store.get_chain_kwargs(SESSION), ("Chain (ship)", True))
    check_eq(f"{step} needs input: generic mention",
             s.messenger.mentions(), [f"{MENTION} Needs your attention."])
    check_eq(f"{step} needs input: not counted as completed",
             s.final_eval().get("completed"), SHIP[:idx])
    s.close()

print()
print("verify_release: phantom claims and parse failures")

s = scenario({"verify_release": "needs_input"})
run(_chain("ship")(s))
check_eq("phantom: stops at verify_release", s.order, SHIP[: SHIP.index("verify_release") + 1])
check_eq("phantom: outcome", s.final_eval().get("outcome"), "phantom_detected")
check_eq("phantom: state kept, queue starts at verify_release",
         s.queue, SHIP[SHIP.index("verify_release"):])
check_eq("phantom: status paused", s.status, "paused")
check_eq("phantom: mention", s.messenger.mentions(),
         [f"{MENTION} Release verifier flagged phantom claims. "
          "Tap Amend to fix, or Continue to ship anyway."])
s.close()

s = scenario({"verify_release": "parse_failed"})
run(_chain("ship")(s))
check_eq("parse failure: outcome is still phantom_detected",
         s.final_eval().get("outcome"), "phantom_detected")
check_eq("parse failure: mention says it could not be parsed",
         s.messenger.mentions(),
         [f"{MENTION} Release verifier output couldn't be parsed. "
          "Tap Amend to retry, or Continue to ship anyway."])
s.close()

s = scenario({"verify_release": "failed"})
run(_chain("ship")(s))
check_eq("verify_release crash: plain failed exit, state cleared",
         (s.final_eval().get("outcome"), s.queue), ("failed", None))
s.close()

print()
print("verify_release is skipped when there is nothing to verify")

s = scenario(entry_sha_mode="none")
run(_chain("ship")(s))
check_eq("no entry SHA: verify_release stub never runs, rest of chain does",
         s.order, [x for x in SHIP if x != "verify_release"])
check("no entry SHA: user told the verifier was skipped",
      any("Release verifier skipped" in t["text"] for t in s.messenger.texts))
check_eq("no entry SHA: step still counts as completed",
         s.final_eval().get("completed"), SHIP)
s.close()

s = scenario(entry_sha_mode="same")
run(_chain("ship")(s))
check_eq("entry == HEAD: verify_release skipped silently",
         s.order, [x for x in SHIP if x != "verify_release"])
check("entry == HEAD: no skip notice",
      not any("Release verifier skipped" in t["text"] for t in s.messenger.texts))
check_eq("entry == HEAD: step still counts as completed",
         s.final_eval().get("completed"), SHIP)
s.close()

print()
print("merge failure")

s = scenario(merge_ok=False)
res = run(_chain("ship")(s))
check_eq("merge failed: all steps ran, merge attempted once",
         (s.order, len(s.finalized)), (SHIP, 1))
check_eq("merge failed: eval outcome", s.final_eval().get("outcome"), "merge_failed")
check_eq("merge failed: merge counts as completed",
         s.final_eval().get("completed"), SHIP)
banner = [t for t in s.messenger.texts if t["buttons"] and not t["text"].startswith(MENTION)
          and "Chain complete" not in t["text"]]
check_eq("merge failed: one banner with buttons", len(banner), 1)
check("merge failed: banner is the generic conflict copy",
      banner[0]["text"] == workflows.merge_failed_banner(None))
check_eq("merge failed: pending merge recorded for the target",
         s.store.get_pending_merge(s.finalized[0]["id"]) is not None, True)
check_eq("merge failed: mention names repo and branch",
         len(s.messenger.mentions()), 1)
check("merge failed: mention uses the chain label and Resolve / Retry / Discard",
      s.messenger.mentions()[0].startswith(f"{MENTION} Chain (ship) hit a merge conflict on ")
      and s.messenger.mentions()[0].endswith("tap Resolve / Retry / Discard."))
check_eq("merge failed: chain state is terminal (queue cleared)", s.queue, None)
check_eq("merge failed: returns the release instance",
         res.id if res else None, s.produced["release"])
s.close()


# --------------------------------------------------------------------------
# 3. review loop, budget, deferred revisions
# --------------------------------------------------------------------------

print()
print("review loop")

s = scenario(converged=False)
res = run(workflows.on_autopilot(s.ctx, SOURCE_ID, SOURCE_MSG))
check_eq("not converged: only review_loop ran", s.order, ["review_loop"])
check_eq("not converged: outcome", s.final_eval().get("outcome"), "review_did_not_converge")
check_eq("not converged: review_loop is not counted as completed",
         s.final_eval().get("completed"), [])
check_eq("not converged: state cleared", s.queue, None)
check("not converged: user gets the didn't-converge notice",
      any("converge" in t["text"] and "halting chain" in t["text"] for t in s.messenger.texts))
check("not converged: mention",
      len(s.messenger.mentions()) == 1
      and s.messenger.mentions()[0].startswith(f"{MENTION} Plan review didn't converge ")
      and s.messenger.mentions()[0].endswith(" needs your attention."))
s.close()

s = scenario(review_deferred=["tidy A", "tidy B"])
run(workflows.on_autopilot_hold(s.ctx, SOURCE_ID, SOURCE_MSG))
check_eq("deferred revisions: chain still runs in full", s.order, AUTOPILOT_HOLD)
check("deferred revisions: summary posted as a result card",
      any("Deferred Revisions" in r["text"] and "tidy A" in r["text"] for r in s.messenger.results))
check_eq("deferred revisions: store entry reaped at the end",
         s.store.get_chain_deferred(SESSION), [])
s.close()

s = scenario({"review_loop": "failed"})
run(workflows.on_autopilot(s.ctx, SOURCE_ID, SOURCE_MSG))
check_eq("review_loop failure (converged): plain failed exit at review_loop",
         (s.order, s.final_eval().get("outcome"),
          blocked_mention(s.messenger.mentions(), "Autopilot", "review_loop")),
         (["review_loop"], "failed", True))
s.close()

print()
print("cost budget")

s = scenario(step_cost=5.0)
run(workflows.on_autopilot(s.ctx, SOURCE_ID, SOURCE_MSG, start_from="build",
                           cost_budget_usd=1.0))
check_eq("budget exhausted after build: stops before the next step", s.order, ["build"])
check_eq("budget exhausted: outcome", s.final_eval().get("outcome"), "budget_exhausted")
check_eq("budget exhausted: state cleared", s.queue, None)
check("budget exhausted: notice posted",
      any("Cost budget exhausted" in t["text"] for t in s.messenger.texts))
check_eq("budget exhausted: mention", s.messenger.mentions(),
         [f"{MENTION} Cost budget reached."])
s.close()

s = scenario(step_cost=0.1)
run(workflows.on_autopilot(s.ctx, SOURCE_ID, SOURCE_MSG, start_from="build",
                           cost_budget_usd=100.0))
check_eq("budget not reached: chain completes", s.order, SHIP)
s.close()


# --------------------------------------------------------------------------
# 4. build guard: no commit
# --------------------------------------------------------------------------

print()
print("build that moved no HEAD")

s = scenario(head_mode="parked", rescue_sha="abcdef1234567890")
run(_chain("verify")(s))
check_eq("dirty worktree is auto-committed and the chain continues",
         (s.runner.rescued, s.order), ([s.produced["build"]], VERIFY))
check_eq("dirty rescue: no discard, no halt notice",
         (s.runner.discarded,
          any("Build paused" in t["text"] or "stopped before committing" in t["text"]
              for t in s.messenger.texts)),
         ([], False))
s.close()

s = scenario(head_mode="parked", rescue_sha=None)
res = run(_chain("ship")(s))
check_eq("clean worktree: chain halts right after build", s.order, ["build"])
check_eq("clean worktree: outcome needs_input", s.final_eval().get("outcome"), "needs_input")
check_eq("clean worktree: build counts as completed", s.final_eval().get("completed"), ["build"])
check_eq("clean worktree: branch discarded (preserving dirty edits)",
         len(s.runner.discarded), 1)
check("clean worktree: halt notice posted",
      any("Build stopped before committing" in t["text"] for t in s.messenger.texts))
check_eq("clean worktree: state kept for resume",
         (s.queue, s.status), (SHIP, "paused"))
check_eq("clean worktree: exactly one ping", len(s.messenger.mentions()), 1)
s.close()


# --------------------------------------------------------------------------
# 5. multi-phase builds
# --------------------------------------------------------------------------

print()
print("multi-phase build")

P1 = Phase(id="p1", title="First", gate="mechanical")
P2 = Phase(id="p2", title="Second", gate="mechanical")

s = scenario(phases=[P1, P2])
run(_chain("verify")(s))
check_eq("two mechanical phases: two builds then the normal tail",
         s.order, ["build", "build", "sensors", "review_code", "verify"])
check_eq("two phases: first branches, second copies the branch",
         [(sp["auto_branch"], sp["copy_branch"]) for sp in s.spawns],
         [(True, False), (False, True)])
check_eq("two phases: first spawns from the source", s.spawns[0]["source"], SOURCE_ID)
check_eq("two phases: second spawns from the first build's instance",
         s.spawns[1]["source"], s.minted[0][1])
check("two phases: prompts carry the phase id and title",
      "p1" in s.spawns[0]["prompt"] and "First" in s.spawns[0]["prompt"]
      and "p2" in s.spawns[1]["prompt"] and "Second" in s.spawns[1]["prompt"])
check_eq("two phases: phase state cleared when done",
         s.store.get_chain_phases(SESSION), None)
check_eq("two phases: eval completed lists build once",
         s.final_eval().get("completed"), VERIFY)
check_eq("two phases: no instance duplicated in eval",
         len(set(s.final_eval().get("instances", []))), len(s.final_eval().get("instances", [])))
s.close()

s = scenario(phases=[Phase(id="d1", title="Design", gate="design")])
run(_chain("ship")(s))
check_eq("design gate: nothing spawned", (s.order, s.spawns), ([], []))
check_eq("design gate: outcome", s.final_eval().get("outcome"), "phase_gate_design")
ps = s.store.get_chain_phases(SESSION)
check_eq("design gate: paused at pre", ps.paused_at if ps else None, "pre")
check_eq("design gate: chain queue kept and paused", (s.queue, s.status), (SHIP, "paused"))
check_eq("design gate: intervention", s.final_eval().get("intervention"), True)
check("design gate: mention names the phase",
      len(s.messenger.mentions()) == 1 and "Phase `d1` (Design)" in s.messenger.mentions()[0])
s.close()

s = scenario(phases=[Phase(id="r1", title="Risky", gate="risk"), P2])
run(_chain("ship")(s))
check_eq("risk gate: first phase built, then stop", s.order, ["build"])
check_eq("risk gate: outcome", s.final_eval().get("outcome"), "phase_gate_risk")
ps = s.store.get_chain_phases(SESSION)
check_eq("risk gate: paused at post", ps.paused_at if ps else None, "post")
check_eq("risk gate: the shipped phase is in the eval list",
         len(s.final_eval().get("instances", [])), 1)
check_eq("risk gate: chain queue kept and paused", (s.queue, s.status), (SHIP, "paused"))
s.close()

s = scenario({"build": "failed"}, phases=[P1, P2])
run(_chain("ship")(s))
check_eq("phase build failure: stops, paused at pre for re-run",
         (s.order, s.final_eval().get("outcome"),
          (s.store.get_chain_phases(SESSION) or ChainPhaseState()).paused_at),
         (["build"], "failed", "pre"))
check_eq("phase build failure: queue kept (phase failures are resumable)",
         (s.queue, s.status), (SHIP, "paused"))
s.close()


# --------------------------------------------------------------------------
# 6. resume
# --------------------------------------------------------------------------

print()
print("resume after a pause")

s = scenario({"verify": "needs_input"})
run(_chain("ship")(s))
check_eq("setup: paused at verify", s.queue, SHIP[SHIP.index("verify"):])
s.behaviour = {}
s.calls.clear()
s.evals.clear()
s.runner.began.clear()
res = run(workflows.resume_autopilot_chain(s.ctx, SOURCE_ID, SOURCE_MSG, SESSION))
check_eq("resume skips the answered step and runs the rest",
         s.order, ["done", "verify_release", "release", "merge"])
check_eq("resume restores the original label and silent close",
         (s.calls[0]["kwargs"], s.finalized[-1]["close_silent"]),
         (("Chain (ship)", True), True))
check_eq("resume finishes and clears state", (s.queue, s.final_eval().get("outcome")),
         (None, "merged"))
s.close()

s = scenario({"release": "needs_input"})
run(_chain("hold")(s))
check_eq("setup: paused at the last step", s.queue, ["release"])
s.behaviour = {}
s.calls.clear()
run(workflows.resume_autopilot_chain(s.ctx, SOURCE_ID, SOURCE_MSG, SESSION))
check_eq("a single remaining step is re-run, not skipped", s.order, ["release"])
s.close()

s = scenario()
check("resume with no stored chain returns None and runs nothing",
      run(workflows.resume_autopilot_chain(s.ctx, SOURCE_ID, SOURCE_MSG, SESSION)) is None
      and s.calls == [])
s.close()

s = scenario(phases=[P1, P2])
s.store.set_autopilot_chain(SESSION, ["build", "sensors"])
s.store.set_chain_phases(SESSION, ChainPhaseState(phases=[P1, P2], cursor=1,
                                                   first_build_id="t-first"))
s.store.set_chain_kwargs(SESSION, label="Chain (verify)", silent_close=False)
run(workflows.resume_autopilot_chain(s.ctx, SOURCE_ID, SOURCE_MSG, SESSION))
check_eq("phased resume keeps 'build' at the head (does not skip it)",
         s.order[:1], ["build"])
check_eq("phased resume: only the unfinished phase is built",
         len(s.spawns), 1)
s.close()


print()
if failures:
    print(f"FAILED {len(failures)} of {checks} checks:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS: all {checks} autopilot chain checks passed")
sys.exit(0)
