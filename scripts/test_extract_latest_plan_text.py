"""Regression tests for ``bot.engine.workflows._extract_latest_plan_text``.

Locks in the broken-chain fallback added to fix the compaction bug observed
on Discord thread 1502626771843944569: when ``chain_instances`` is empty
(autopilot chain cleared after a no-commits halt, or user clicked Build via
the manual on_build path), the helper must scan the store for a same-session
APPLY_REVISIONS / PLAN whose ``created_at <= source.created_at`` and inject
that as the verbatim plan, instead of silently no-opping and letting the
resumed Claude session implement its compacted-summary recollection.

Run: python scripts/test_extract_latest_plan_text.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.claude.types import InstanceOrigin
from bot.engine.workflows import _extract_latest_plan_text


_failures: list[str] = []


def _check(actual, expected, label: str) -> None:
    if actual != expected:
        _failures.append(label)
        print(f"  FAIL: {label}\n        got      {actual!r}\n        expected {expected!r}")
    else:
        print(f"  ok:   {label}")


# Duck-typed fakes. The helper touches only origin, session_id, created_at,
# and read_result_text() on Instance, plus get_instance/list_instances on
# the store, plus ctx.store. Everything else is irrelevant.
@dataclass
class _FakeInst:
    id: str
    origin: InstanceOrigin
    session_id: str | None = None
    created_at: str | None = None
    _result_text: str = ""

    def read_result_text(self) -> str:
        return self._result_text


@dataclass
class _FakeStore:
    instances: dict[str, _FakeInst] = field(default_factory=dict)
    listed: list[_FakeInst] = field(default_factory=list)

    def get_instance(self, inst_id: str) -> _FakeInst | None:
        return self.instances.get(inst_id)

    def list_instances(self, all_: bool = False) -> list[_FakeInst]:
        # The helper relies on most-recent-first ordering, which matches
        # the real StateStore.list_instances() at bot/store/state.py:373.
        return sorted(
            (i for i in self.listed if i.created_at),
            key=lambda i: i.created_at or "",
            reverse=True,
        )


@dataclass
class _FakeCtx:
    store: _FakeStore


def _ts(offset_seconds: int) -> str:
    """Build an ISO-8601 timestamp at a deterministic offset from a fixed base."""
    base = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_seconds)).isoformat()


# ---- Case 1: chain_instances has APPLY_REVISIONS — existing behavior ----
print("Case 1: chain_instances has APPLY_REVISIONS")
ar = _FakeInst("ar1", InstanceOrigin.APPLY_REVISIONS, _result_text="REVISED PLAN BODY")
ctx = _FakeCtx(_FakeStore())
_check(
    _extract_latest_plan_text(ctx, [ar], "missing"),
    "REVISED PLAN BODY",
    "returns chain APPLY_REVISIONS text",
)

# ---- Case 2: chain empty, source is PLAN — existing behavior ----
print("Case 2: chain empty, source is PLAN")
plan_src = _FakeInst(
    "p1", InstanceOrigin.PLAN, session_id="s1", created_at=_ts(0),
    _result_text="ORIGINAL PLAN",
)
store = _FakeStore(instances={"p1": plan_src})
ctx = _FakeCtx(store)
_check(
    _extract_latest_plan_text(ctx, [], "p1"),
    "ORIGINAL PLAN",
    "falls back to PLAN-origin source",
)

# ---- Case 3: chain empty, non-PLAN source, store has same-session APPLY_REVISIONS ----
print("Case 3: chain empty, store fallback finds APPLY_REVISIONS")
qsrc = _FakeInst("q1", InstanceOrigin.DIRECT, session_id="s1", created_at=_ts(100))
ar_old = _FakeInst(
    "ar_old", InstanceOrigin.APPLY_REVISIONS,
    session_id="s1", created_at=_ts(50), _result_text="STALE PLAN",
)
ar_new = _FakeInst(
    "ar_new", InstanceOrigin.APPLY_REVISIONS,
    session_id="s1", created_at=_ts(80), _result_text="LATEST PLAN",
)
store = _FakeStore(
    instances={"q1": qsrc, "ar_old": ar_old, "ar_new": ar_new},
    listed=[qsrc, ar_old, ar_new],
)
ctx = _FakeCtx(store)
_check(
    _extract_latest_plan_text(ctx, [], "q1"),
    "LATEST PLAN",
    "store fallback picks most recent APPLY_REVISIONS for session",
)

# ---- Case 4: store fallback rejects different-session candidates ----
print("Case 4: store fallback rejects cross-session leakage")
qsrc = _FakeInst("q1", InstanceOrigin.DIRECT, session_id="s1", created_at=_ts(100))
ar_other = _FakeInst(
    "ar_other", InstanceOrigin.APPLY_REVISIONS,
    session_id="s2", created_at=_ts(80), _result_text="OTHER SESSION PLAN",
)
store = _FakeStore(
    instances={"q1": qsrc, "ar_other": ar_other}, listed=[qsrc, ar_other],
)
ctx = _FakeCtx(store)
_check(
    _extract_latest_plan_text(ctx, [], "q1"),
    "",
    "different-session APPLY_REVISIONS is filtered out",
)

# ---- Case 5: store fallback rejects future-dated candidates ----
print("Case 5: store fallback rejects future-dated candidates")
qsrc = _FakeInst("q1", InstanceOrigin.DIRECT, session_id="s1", created_at=_ts(100))
ar_future = _FakeInst(
    "ar_future", InstanceOrigin.APPLY_REVISIONS,
    session_id="s1", created_at=_ts(200), _result_text="FUTURE PLAN",
)
store = _FakeStore(
    instances={"q1": qsrc, "ar_future": ar_future}, listed=[qsrc, ar_future],
)
ctx = _FakeCtx(store)
_check(
    _extract_latest_plan_text(ctx, [], "q1"),
    "",
    "future-dated APPLY_REVISIONS (created after source) is filtered out",
)

# ---- Case 6: no plan-bearing instances anywhere ----
print("Case 6: no plan candidates")
qsrc = _FakeInst("q1", InstanceOrigin.DIRECT, session_id="s1", created_at=_ts(100))
unrelated = _FakeInst("b1", InstanceOrigin.BUILD, session_id="s1", created_at=_ts(50))
store = _FakeStore(instances={"q1": qsrc, "b1": unrelated}, listed=[qsrc, unrelated])
ctx = _FakeCtx(store)
_check(
    _extract_latest_plan_text(ctx, [], "q1"),
    "",
    "BUILD-origin candidates aren't treated as plans",
)

# ---- Case 7: source has no created_at (defensive guard) ----
print("Case 7: source missing created_at — no TypeError")
qsrc = _FakeInst("q1", InstanceOrigin.DIRECT, session_id="s1", created_at=None)
ar = _FakeInst(
    "ar1", InstanceOrigin.APPLY_REVISIONS,
    session_id="s1", created_at=_ts(50), _result_text="PLAN",
)
store = _FakeStore(instances={"q1": qsrc, "ar1": ar}, listed=[qsrc, ar])
ctx = _FakeCtx(store)
_check(
    _extract_latest_plan_text(ctx, [], "q1"),
    "",
    "source missing created_at returns '' instead of crashing",
)

# ---- Case 8: source has malformed created_at ----
print("Case 8: source malformed created_at — no crash")
qsrc = _FakeInst("q1", InstanceOrigin.DIRECT, session_id="s1", created_at="not-a-date")
ar = _FakeInst(
    "ar1", InstanceOrigin.APPLY_REVISIONS,
    session_id="s1", created_at=_ts(50), _result_text="PLAN",
)
store = _FakeStore(instances={"q1": qsrc, "ar1": ar}, listed=[qsrc, ar])
ctx = _FakeCtx(store)
_check(
    _extract_latest_plan_text(ctx, [], "q1"),
    "",
    "source malformed created_at returns '' instead of crashing",
)

# ---- Case 9: candidate has malformed created_at — skipped, next candidate considered ----
print("Case 9: candidate malformed created_at is skipped")
qsrc = _FakeInst("q1", InstanceOrigin.DIRECT, session_id="s1", created_at=_ts(100))
ar_bad = _FakeInst(
    "ar_bad", InstanceOrigin.APPLY_REVISIONS,
    session_id="s1", created_at="garbage", _result_text="BAD PLAN",
)
ar_ok = _FakeInst(
    "ar_ok", InstanceOrigin.APPLY_REVISIONS,
    session_id="s1", created_at=_ts(50), _result_text="GOOD PLAN",
)
store = _FakeStore(
    instances={"q1": qsrc, "ar_bad": ar_bad, "ar_ok": ar_ok},
    listed=[qsrc, ar_bad, ar_ok],
)
ctx = _FakeCtx(store)
_check(
    _extract_latest_plan_text(ctx, [], "q1"),
    "GOOD PLAN",
    "malformed-timestamp candidate skipped, next valid candidate returned",
)

# ---- Case 10: APPLY_REVISIONS preferred over PLAN even when PLAN is more recent ----
print("Case 10: APPLY_REVISIONS preferred over PLAN")
qsrc = _FakeInst("q1", InstanceOrigin.DIRECT, session_id="s1", created_at=_ts(100))
plan_inst = _FakeInst(
    "p1", InstanceOrigin.PLAN,
    session_id="s1", created_at=_ts(20), _result_text="ORIG PLAN",
)
ar = _FakeInst(
    "ar1", InstanceOrigin.APPLY_REVISIONS,
    session_id="s1", created_at=_ts(10), _result_text="REVISED PLAN",
)
store = _FakeStore(
    instances={"q1": qsrc, "p1": plan_inst, "ar1": ar},
    listed=[qsrc, plan_inst, ar],
)
ctx = _FakeCtx(store)
_check(
    _extract_latest_plan_text(ctx, [], "q1"),
    "REVISED PLAN",
    "APPLY_REVISIONS preferred over (more-recent) PLAN",
)

# ---- Case 11: PLAN-only fallback when no APPLY_REVISIONS exists ----
print("Case 11: PLAN-only store fallback")
qsrc = _FakeInst("q1", InstanceOrigin.DIRECT, session_id="s1", created_at=_ts(100))
plan_inst = _FakeInst(
    "p1", InstanceOrigin.PLAN,
    session_id="s1", created_at=_ts(20), _result_text="ORIG PLAN",
)
store = _FakeStore(
    instances={"q1": qsrc, "p1": plan_inst}, listed=[qsrc, plan_inst],
)
ctx = _FakeCtx(store)
_check(
    _extract_latest_plan_text(ctx, [], "q1"),
    "ORIG PLAN",
    "PLAN-origin candidate returned when no APPLY_REVISIONS available",
)

# ---- Case 12: tz-naive candidate vs tz-aware source — no TypeError ----
print("Case 12: tz-naive candidate compared against tz-aware source")
qsrc = _FakeInst("q1", InstanceOrigin.DIRECT, session_id="s1", created_at=_ts(100))
# Naive timestamp (no tzinfo) — would have crashed `inst_dt > source_dt`
# before the tz-normalization fix. The codebase intent is UTC everywhere,
# so the helper now coerces naive → UTC before comparing.
naive_iso = datetime(2026, 5, 9, 12, 0, 50).isoformat()  # no tzinfo
ar_naive = _FakeInst(
    "ar_naive", InstanceOrigin.APPLY_REVISIONS,
    session_id="s1", created_at=naive_iso, _result_text="NAIVE PLAN",
)
store = _FakeStore(
    instances={"q1": qsrc, "ar_naive": ar_naive},
    listed=[qsrc, ar_naive],
)
ctx = _FakeCtx(store)
_check(
    _extract_latest_plan_text(ctx, [], "q1"),
    "NAIVE PLAN",
    "tz-naive candidate normalized to UTC and accepted (no TypeError)",
)

# ---- Case 13: tz-naive source vs tz-aware candidate — no TypeError ----
print("Case 13: tz-naive source compared against tz-aware candidate")
naive_src_iso = datetime(2026, 5, 9, 12, 1, 40).isoformat()  # no tzinfo
qsrc = _FakeInst(
    "q1", InstanceOrigin.DIRECT, session_id="s1", created_at=naive_src_iso,
)
ar_aware = _FakeInst(
    "ar_aware", InstanceOrigin.APPLY_REVISIONS,
    session_id="s1", created_at=_ts(50), _result_text="AWARE PLAN",
)
store = _FakeStore(
    instances={"q1": qsrc, "ar_aware": ar_aware},
    listed=[qsrc, ar_aware],
)
ctx = _FakeCtx(store)
_check(
    _extract_latest_plan_text(ctx, [], "q1"),
    "AWARE PLAN",
    "tz-naive source normalized to UTC and compared without TypeError",
)

# ---- Case 14: trailing "### Applied" metadata stripped ----
print("Case 14: trailing review metadata stripped")
ar = _FakeInst(
    "ar1", InstanceOrigin.APPLY_REVISIONS,
    _result_text="PLAN BODY\n\n### Applied\n- [TAG] Title — applied",
)
ctx = _FakeCtx(_FakeStore())
_check(
    _extract_latest_plan_text(ctx, [ar], "missing"),
    "PLAN BODY",
    "trailing '### Applied' block stripped",
)


# ---- Summary ----
print()
if _failures:
    print(f"FAILED ({len(_failures)}):")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("All cases passed.")
sys.exit(0)
