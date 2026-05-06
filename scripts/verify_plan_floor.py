"""Deterministic check: plan-text spawns clamp Bash via permission_mode=explore.

Builds a synthetic Instance with the same shape spawn_from would produce after
``_enforce_readonly_floor`` runs (mode="explore", bash_policy="none"), calls
``provider.build_command``, and asserts ``Bash`` appears in the value of
``--disallowed-tools``. Independent of log infrastructure or live runtime.

Exit 0 = floor is wired correctly. Exit 1 = regression.

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


def main() -> int:
    provider = get_provider("claude")
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
        print("FAIL: --disallowed-tools missing tokens:", sorted(missing))
        print("  full disallowed value:", tokens)
        print("  full cmd:", cmd)
        return 1

    print(f"PASS: --disallowed-tools contains {sorted(required)}")
    print(f"  observed: {tokens}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
