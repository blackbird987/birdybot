#!/usr/bin/env python3
"""The dollar gates record costs and refuse nothing unless asked to.

Until 2026-10-14 the parser read ``cost_usd`` while the Claude CLI emits
``total_cost_usd``, so every run was recorded at $0 and every dollar gate was
inert. Fixing the field woke them all up at once, against a ``.env`` daily
budget of $20 and a fleet that burns roughly $1K a day of *notional*
subscription cost. Without a switch the first hour after the fix would have
refused every query, every /spawn and every chain step.

``config.COST_GATES_ENFORCE`` (default off) is that switch, and
``cmd_state.within_dollar_budget`` is the only place allowed to read it.

Asserted here, on a real ``StateStore`` holding a simulated $500 day:

  * switch off: the query gate passes, /spawn is not refused, a chain whose
    steps cost $500 each runs every step, and no budget warning is posted;
  * switch on: the old behaviour returns for all four;
  * structurally: no engine module compares a dollar figure against
    ``DAILY_BUDGET_USD`` or ``cost_budget_usd`` outside the helper, so a new
    gate cannot quietly skip the switch;
  * the chain-eval "unusually expensive" flag stays off with the switch.

Run: python scripts/test_cost_gates.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config  # noqa: E402
from bot.claude.types import Instance, InstanceStatus, InstanceType  # noqa: E402
from bot.engine import chain_runner, cmd_state, eval as ev_mod  # noqa: E402
from bot.engine.directives import spawn  # noqa: E402
from bot.platform.base import RequestContext  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}" + (f"  ({detail})" if detail else ""))
        _failures.append(name)


class _Messenger:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, _cid, text, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        self.sent.append(text)
        return "m"

    async def send_result(self, *a, **kw):  # noqa: ANN002, ANN003
        return "m"

    async def is_conversation_closed(self, _cid):  # noqa: ANN001
        return False

    def format_mention(self, _uid):  # noqa: ANN001
        return None


class _Runner:
    def begin_task(self, *_a):  # noqa: ANN002
        pass

    def end_task(self, *_a):  # noqa: ANN002
        pass


def _ctx(store: StateStore) -> tuple[RequestContext, _Messenger]:
    m = _Messenger()
    ctx = RequestContext(
        messenger=m,  # type: ignore[arg-type]
        channel_id="c1", platform="test",
        store=store, runner=_Runner(),  # type: ignore[arg-type]
        repo_name="harness", is_owner=True,
    )
    return ctx, m


def _store(tmp: Path, daily: float) -> StateStore:
    store = StateStore(tmp / "state.json", tmp / "results")
    store.add_cost(daily)
    return store


def _run_chain(ctx: RequestContext, budget: float) -> tuple[int, list[str]]:
    """Run a three-step chain whose every step costs $500; return steps run and exit reasons."""
    ran: list[int] = []
    exits: list[str] = []

    async def fake_review_loop(_ctx, cur_id, _msg, chain_instances):  # noqa: ANN001
        inst = Instance(id=f"t-{len(ran) + 1}", name=None, instance_type=InstanceType.TASK,
                        status=InstanceStatus.COMPLETED, prompt="x",
                        repo_name="harness", repo_path="/nonexistent")
        inst.cost_usd = 500.0
        chain_instances.append(inst)
        ran.append(1)
        return inst, False

    async def fake_exit_needs_input(*a, **_kw):  # noqa: ANN002, ANN003
        exits.append(a[7])

    async def fake_exit(*a, **_kw):  # noqa: ANN002, ANN003
        exits.append(a[7])

    saved = {n: getattr(chain_runner, n) for n in
             ("_review_plan_loop", "_exit_chain_needs_input", "_exit_chain")}
    chain_runner._review_plan_loop = fake_review_loop
    chain_runner._exit_chain_needs_input = fake_exit_needs_input
    chain_runner._exit_chain = fake_exit
    try:
        asyncio.run(chain_runner._run_autopilot_chain(
            ctx, "t-0", None, ["review_loop", "review_loop", "review_loop"],
            "sess-cost", cost_budget_usd=budget,
        ))
    finally:
        for n, v in saved.items():
            setattr(chain_runner, n, v)
    return len(ran), exits


def _scenario(tmp: Path, enforce: bool) -> dict:
    config.COST_GATES_ENFORCE = enforce
    store = _store(tmp, 500.0)
    ctx, m = _ctx(store)
    out = {
        "check_budget": cmd_state.check_budget(ctx),
        "refused": asyncio.run(cmd_state.refuse_if_over_budget(ctx)),
        "spawn_gate": spawn.check_budget(ctx),
    }
    m.sent.clear()
    asyncio.run(cmd_state.budget_warning(ctx))
    out["warned"] = any("Budget warning" in s for s in m.sent)
    m.sent.clear()
    out["steps"], out["exits"] = _run_chain(ctx, budget=1.0)
    out["chain_msg"] = any("Cost budget exhausted" in s for s in m.sent)
    return out


def _check_structural() -> None:
    """No dollar comparison against the budgets outside within_dollar_budget."""
    pat = re.compile(
        r"(?:[<>]=?)\s*(?:config\.)?(?:DAILY_BUDGET_USD|cost_budget_usd)\b"
        r"|\b(?:config\.)?(?:DAILY_BUDGET_USD|cost_budget_usd)\b(?:\s*\*\s*[\d.]+)?\s*[<>]=?",
    )
    offenders = []
    for path in sorted((ROOT / "bot").rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if pat.search(code):
                offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    _check("no dollar-budget comparison bypasses within_dollar_budget",
           not offenders, "; ".join(offenders[:4]))

    src = (ROOT / "bot/engine/cmd_state.py").read_text(encoding="utf-8")
    _check("cmd_state.within_dollar_budget reads COST_GATES_ENFORCE",
           "config.COST_GATES_ENFORCE" in src)
    readers = [
        str(p.relative_to(ROOT)) for p in (ROOT / "bot").rglob("*.py")
        if "COST_GATES_ENFORCE" in p.read_text(encoding="utf-8")
    ]
    _check("only config, the helper and the eval flag read the switch",
           sorted(readers) == ["bot/config.py", "bot/engine/cmd_state.py", "bot/engine/eval.py"],
           str(sorted(readers)))


def _expensive_flagged(tmp: Path, enforce: bool) -> bool:
    """Does evaluate_chain flag a $12 chain as unusually expensive?"""
    config.COST_GATES_ENFORCE = enforce
    saved_enabled = config.EVAL_ENABLED
    config.EVAL_ENABLED = True
    try:
        store = StateStore(tmp / f"state-{enforce}.json", tmp / "results")
        inst = Instance(id="t-9", name=None, instance_type=InstanceType.TASK,
                        status=InstanceStatus.COMPLETED, prompt="x",
                        repo_name="harness", repo_path="/nonexistent")
        inst.cost_usd = 12.0
        ce = ev_mod.evaluate_chain(store, "t-9", ["build"], ["build"], [inst], "completed")
    finally:
        config.EVAL_ENABLED = saved_enabled
    return any("unusually expensive" in f.message for f in ce.flags)


def main() -> int:
    # The chain run and evaluate_chain both write eval files; keep them out
    # of whatever data/ dir this checkout resolves to.
    evals_tmp = tempfile.TemporaryDirectory()
    ev_mod.EVALS_DIR = Path(evals_tmp.name)
    original = config.COST_GATES_ENFORCE
    _check("COST_GATES_ENFORCE defaults off", original is False or
           __import__("os").getenv("COST_GATES_ENFORCE") is not None, str(original))
    try:
        with tempfile.TemporaryDirectory() as d:
            off = _scenario(Path(d), enforce=False)
        _check("off: a $500 day passes the query gate", off["check_budget"] is True)
        _check("off: a $500 day refuses nothing and says nothing", off["refused"] is False)
        _check("off: /spawn is not refused on a $500 day", off["spawn_gate"] is True)
        _check("off: no budget warning is posted", off["warned"] is False)
        _check("off: a chain of $500 steps runs all three", off["steps"] == 3, str(off))
        _check("off: the chain never reports its budget exhausted",
               not off["chain_msg"] and "budget_exhausted" not in off["exits"], str(off["exits"]))

        with tempfile.TemporaryDirectory() as d:
            on = _scenario(Path(d), enforce=True)
        _check("on: a $500 day fails the query gate", on["check_budget"] is False)
        _check("on: a $500 day is refused with the budget message", on["refused"] is True)
        _check("on: /spawn is refused on a $500 day", on["spawn_gate"] is False)
        _check("on: the budget warning is posted", on["warned"] is True)
        _check("on: a $1 chain budget pauses after the first $500 step", on["steps"] == 1, str(on))
        _check("on: the pause is reported as budget exhausted",
               on["chain_msg"] and "budget_exhausted" in on["exits"], str(on["exits"]))

        config.COST_GATES_ENFORCE = True
        _check("on: spending under the limit still passes",
               cmd_state.within_dollar_budget(5.0, 20.0) is True)
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "results").mkdir()
            _check("off: a $12 chain is not flagged unusually expensive",
                   not _expensive_flagged(Path(d), False))
            _check("on: a $12 chain is flagged unusually expensive",
                   _expensive_flagged(Path(d), True))
    finally:
        config.COST_GATES_ENFORCE = original

    _check_structural()

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} cost-gate check(s) failed")
        return 1
    print("PASS: costs are recorded and gate nothing unless COST_GATES_ENFORCE is on.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
