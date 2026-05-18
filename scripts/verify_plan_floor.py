"""Deterministic checks for the read-only triage floor + build-child restore.

Two assertions, both built from synthetic Instances and the real provider:

1. triage-clamp: ``_enforce_readonly_floor(permission_mode="explore", ...)``
   produces an instance whose provider command line strips Bash + the
   code-change tools via ``--disallowed-tools``.

2. build-restore: ``_enforce_readonly_floor(permission_mode=None, "build",
   baseline="full")`` returns ``("build", "full")``, and a build-mode
   instance carrying that result (simulating a build child spawned from a
   triage-clamped parent whose baseline was preserved) does NOT have
   ``Bash`` in its provider ``--disallowed-tools``. This is the regression
   that caused t-4696.

Exit 0 = both checks pass. Exit 1 = regression.

Run: ``python scripts/verify_plan_floor.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.claude.provider import get_provider
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType
from bot.engine.workflows import _enforce_readonly_floor


def _build_synthetic_instance() -> Instance:
    spawn_mode, bash_policy = _enforce_readonly_floor(
        permission_mode="explore",
        spawn_mode="explore",
        bash_policy="full",
    )
    if spawn_mode != "explore" or bash_policy != "none":
        raise AssertionError(
            f"floor regression: expected ('explore','none'), "
            f"got ({spawn_mode!r},{bash_policy!r})"
        )

    return Instance(
        id="verify-floor",
        name=None,
        instance_type=InstanceType.QUERY,
        prompt="(synthetic)",
        repo_name="(synthetic)",
        repo_path=str(ROOT),
        status=InstanceStatus.RUNNING,
        mode=spawn_mode,
        origin=InstanceOrigin.REVIEW_PLAN,
        bash_policy=bash_policy,
        effort="high",
    )


def _disallowed_tokens(cmd: list[str]) -> list[str]:
    try:
        idx = cmd.index("--disallowed-tools")
    except ValueError:
        return []
    if idx + 1 >= len(cmd):
        return []
    return [t.strip() for t in cmd[idx + 1].split(",") if t.strip()]


def _build_synthetic_build_child() -> Instance:
    """Simulate a build child spawned from a triage-clamped parent.

    The parent was clamped (bash_policy="none") but its baseline remained
    "full". The chained spawn passes baseline (not the clamp) through the
    floor with permission_mode=None, so the new build child must end up
    with bash_policy="full" — restoring Bash.
    """
    parent_baseline = "full"
    parent_bash_policy = "none"  # simulating a triage-clamped parent

    spawn_mode, child_bash_policy = _enforce_readonly_floor(
        permission_mode=None,
        spawn_mode="build",
        bash_policy=parent_baseline,
    )
    if spawn_mode != "build" or child_bash_policy != "full":
        raise AssertionError(
            f"build-restore regression: expected ('build','full'), "
            f"got ({spawn_mode!r},{child_bash_policy!r}); "
            f"parent had bash_policy={parent_bash_policy!r}, "
            f"baseline={parent_baseline!r}"
        )

    return Instance(
        id="verify-build-restore",
        name=None,
        instance_type=InstanceType.TASK,
        prompt="(synthetic)",
        repo_name="(synthetic)",
        repo_path=str(ROOT),
        status=InstanceStatus.RUNNING,
        mode=spawn_mode,
        origin=InstanceOrigin.DIRECT,
        bash_policy=child_bash_policy,
        bash_policy_baseline=parent_baseline,
        effort="high",
    )


def main() -> int:
    provider = get_provider("claude")

    # Assertion 1: triage subagent still gets Bash (and code-change tools) stripped.
    instance = _build_synthetic_instance()
    cmd = provider.build_command(
        instance,
        binary="claude",
        system_prompt_file=None,
        system_prompt_inline=None,
        api_fallback=False,
        api_key_file=None,
    )
    tokens = _disallowed_tokens(cmd)
    # Derive expected set from the provider so future code_change_tools
    # additions (e.g. MultiEdit) are automatically required, not silently
    # missed. Bash is the floor's specific contribution.
    required = set(provider.code_change_tools) | {"Bash"}
    missing = required - set(tokens)
    if missing:
        print("FAIL [triage-clamp]: --disallowed-tools missing tokens:", sorted(missing))
        print("  full disallowed value:", tokens)
        print("  full cmd:", cmd)
        return 1
    print(f"PASS [triage-clamp]: --disallowed-tools contains {sorted(required)}")
    print(f"  observed: {tokens}")

    # Assertion 2: build child spawned from a triage-clamped parent restores Bash.
    # This is the regression that caused the t-4696 incident.
    build_child = _build_synthetic_build_child()
    build_cmd = provider.build_command(
        build_child,
        binary="claude",
        system_prompt_file=None,
        system_prompt_inline=None,
        api_fallback=False,
        api_key_file=None,
    )
    build_tokens = _disallowed_tokens(build_cmd)
    if "Bash" in build_tokens:
        print("FAIL [build-restore]: build child still has Bash disallowed")
        print("  disallowed:", build_tokens)
        print("  full cmd:", build_cmd)
        return 1
    print("PASS [build-restore]: build child spawned from clamped parent has Bash")
    print(f"  disallowed: {build_tokens or '(none)'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
