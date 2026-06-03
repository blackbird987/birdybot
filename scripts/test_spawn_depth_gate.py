"""Regression test for the depth-gated /spawn instructions in the system prompt.

Background: a spawned (depth>=1) thread cannot itself spawn — the recursion cap
in bot/engine/commands.py refuses any /spawn directive it emits. Before this fix
the full /spawn instructions (config.SPAWN_CONTEXT) were injected into EVERY
session unconditionally, so spawned threads kept proposing directives that got
rejected. The fix in ClaudeRunner._build_system_prompt now picks the variant by
instance.spawn_depth: depth-0 gets SPAWN_CONTEXT, depth>=1 gets
SPAWN_CAPPED_NOTICE.

This locks that gate so a future edit to BOT_CONTEXT or _build_system_prompt
can't silently revert it.

Run: python scripts/test_spawn_depth_gate.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot import config
from bot.claude.runner import ClaudeRunner
from bot.claude.types import Instance, InstanceStatus, InstanceType

_SPAWN_HEADER = "Spawning a fresh session with a generated prompt:"
_CAPPED_MARKER = "Spawning (DISABLED here)"

_failures: list[str] = []


def _check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


def _make_instance(spawn_depth: int) -> Instance:
    # repo_name / repo_path left empty so _build_system_prompt skips the
    # history-load and repo-instruction branches (no filesystem/git access).
    # branch set truthy so the wake-guidance block is skipped. mode != "plan".
    return Instance(
        id="t-test",
        name=None,
        instance_type=InstanceType.TASK,
        prompt="x",
        repo_name="",
        repo_path="",
        status=InstanceStatus.RUNNING,
        mode="build",
        branch="claude-bot/t-test",
        spawn_depth=spawn_depth,
    )


def _build_prompt(spawn_depth: int) -> str:
    runner = ClaudeRunner()
    # Stub the git-touching preamble helpers so the test stays hermetic — we
    # only care about which spawn block lands in the prompt.
    runner._build_location_block = lambda inst: ""  # type: ignore[method-assign]
    runner._build_master_context_block = lambda inst: ""  # type: ignore[method-assign]
    return runner._build_system_prompt(_make_instance(spawn_depth))


# ---- Constant-level invariants: the split itself ----
print("Constant invariants")
_check(_SPAWN_HEADER not in config.BOT_CONTEXT,
       "BOT_CONTEXT no longer contains the spawn header")
_check(_SPAWN_HEADER in config.SPAWN_CONTEXT,
       "SPAWN_CONTEXT carries the full spawn instructions")
_check(_CAPPED_MARKER in config.SPAWN_CAPPED_NOTICE,
       "SPAWN_CAPPED_NOTICE states spawning is disabled")
_check("Rebooting the management bot:" in config.BOT_CONTEXT_TAIL,
       "BOT_CONTEXT_TAIL carries the reboot/wake guidance")

# ---- Depth-0: full spawn capability present, capped notice absent ----
print("Depth 0 (top-level thread)")
p0 = _build_prompt(0)
_check(_SPAWN_HEADER in p0, "depth-0 prompt includes full /spawn instructions")
_check(_CAPPED_MARKER not in p0, "depth-0 prompt omits the capped notice")
_check("Rebooting the management bot:" in p0, "depth-0 prompt keeps reboot tail")

# ---- Depth-1: capped notice present, full capability absent ----
print("Depth 1 (spawned thread)")
p1 = _build_prompt(1)
_check(_CAPPED_MARKER in p1, "depth-1 prompt includes the capped notice")
_check(_SPAWN_HEADER not in p1, "depth-1 prompt omits the full /spawn instructions")
_check("Rebooting the management bot:" in p1, "depth-1 prompt keeps reboot tail")

# ---- Depth 2 behaves like depth 1 (cap is >=1, not ==1) ----
print("Depth 2 (defensive — same as depth 1)")
p2 = _build_prompt(2)
_check(_CAPPED_MARKER in p2 and _SPAWN_HEADER not in p2,
       "depth-2 prompt is capped like depth-1")


# ---- Summary ----
print()
if _failures:
    print(f"FAILED ({len(_failures)}):")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("All cases passed.")
sys.exit(0)
