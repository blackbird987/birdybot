"""Tests for plan-vs-build spawn model routing (resolve_spawn_model).

Workflow steps split by nature:
  - Plan/brainstorm-family origins (direct, plan, review_plan, apply_revisions)
    stay unrouted (None) so they fall through to DEFAULT_SESSION_MODEL — the
    lighter "thinking" model.
  - Build-family origins (BUILD_ORIGINS: build, verify, commit, review_code,
    release, sensor_fix, resolve_merge, ...) route to BUILD_MODEL (default opus).

Precedence: explicit MODEL_ROUTING entry > BUILD_ORIGINS category default >
legacy EXPLORE_MODEL > None (DEFAULT_SESSION_MODEL fall-through).

Run: ``python scripts/test_model_routing.py``  (exit 0 on pass).
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude.types import BUILD_ORIGINS, PLAN_ORIGINS, InstanceOrigin
from bot.engine.workflows import resolve_spawn_model


# Origins that must stay unrouted (None → DEFAULT_SESSION_MODEL thinking model).
_THINKING_ORIGINS = frozenset(
    {InstanceOrigin.DIRECT} | set(PLAN_ORIGINS)
)

# Button/preset actions that never become a spawned instance origin, so they
# never reach resolve_spawn_model: Build & Ship fans out into per-step spawns,
# and a retry preserves its source instance's origin + model.
_NON_INSTANCE_ORIGINS = frozenset(
    {InstanceOrigin.BUILD_AND_SHIP, InstanceOrigin.RETRY}
)


@contextmanager
def _cfg(*, build_model="opus", model_routing=None, explore_model=None):
    """Temporarily set the routing-relevant config knobs, then restore."""
    saved = (config.BUILD_MODEL, dict(config.MODEL_ROUTING), config.EXPLORE_MODEL)
    config.BUILD_MODEL = build_model
    config.MODEL_ROUTING = dict(model_routing or {})
    config.EXPLORE_MODEL = explore_model
    try:
        yield
    finally:
        config.BUILD_MODEL, config.MODEL_ROUTING, config.EXPLORE_MODEL = (
            saved[0], saved[1], saved[2],
        )


def _test_build_origins_route_to_build_model() -> list[str]:
    failures: list[str] = []
    with _cfg(build_model="opus"):
        for origin in BUILD_ORIGINS:
            got = resolve_spawn_model(origin)
            if got != "opus":
                failures.append(
                    f"build origin {origin.value!r} should route to BUILD_MODEL "
                    f"'opus', got {got!r}"
                )
    return failures


def _test_thinking_origins_unrouted() -> list[str]:
    """Plan/brainstorm origins stay None so they fall through to the default."""
    failures: list[str] = []
    with _cfg(build_model="opus"):
        for origin in _THINKING_ORIGINS:
            got = resolve_spawn_model(origin)
            if got is not None:
                failures.append(
                    f"thinking origin {origin.value!r} should be unrouted "
                    f"(None → DEFAULT_SESSION_MODEL), got {got!r}"
                )
    return failures


def _test_explicit_routing_overrides_category() -> list[str]:
    """A MODEL_ROUTING entry wins over the BUILD_ORIGINS category default AND
    over an otherwise-unrouted thinking origin."""
    failures: list[str] = []
    with _cfg(build_model="opus", model_routing={"build": "sonnet", "plan": "haiku"}):
        if resolve_spawn_model(InstanceOrigin.BUILD) != "sonnet":
            failures.append("MODEL_ROUTING should override BUILD_ORIGINS default")
        if resolve_spawn_model(InstanceOrigin.PLAN) != "haiku":
            failures.append("MODEL_ROUTING should route an unrouted thinking origin")
        # A build origin NOT named in MODEL_ROUTING still gets the category default.
        if resolve_spawn_model(InstanceOrigin.COMMIT) != "opus":
            failures.append("unnamed build origin should keep the category default")
    return failures


def _test_explore_model_legacy_fallback() -> list[str]:
    """EXPLORE_MODEL still covers the plan-review steps when set, but only for
    origins not already claimed by BUILD_ORIGINS or MODEL_ROUTING."""
    failures: list[str] = []
    with _cfg(build_model="opus", explore_model="sonnet"):
        if resolve_spawn_model(InstanceOrigin.REVIEW_PLAN) != "sonnet":
            failures.append("EXPLORE_MODEL should route review_plan when set")
        if resolve_spawn_model(InstanceOrigin.APPLY_REVISIONS) != "sonnet":
            failures.append("EXPLORE_MODEL should route apply_revisions when set")
        # Plain DIRECT chat is untouched by EXPLORE_MODEL.
        if resolve_spawn_model(InstanceOrigin.DIRECT) is not None:
            failures.append("EXPLORE_MODEL must not touch DIRECT chat")
    return failures


def _test_every_origin_classified() -> list[str]:
    """Every InstanceOrigin is deliberately placed: a build origin, a thinking
    origin, or a button/meta action. A new enum member trips this until it is
    classified — the guard against silently defaulting a build-like origin to
    the thinking model."""
    failures: list[str] = []
    buckets = (BUILD_ORIGINS, _THINKING_ORIGINS, _NON_INSTANCE_ORIGINS)
    classified = BUILD_ORIGINS | _THINKING_ORIGINS | _NON_INSTANCE_ORIGINS
    missing = set(InstanceOrigin) - classified
    if missing:
        failures.append(
            "unclassified origins (place in BUILD_ORIGINS, thinking, or meta): "
            f"{sorted(o.value for o in missing)}"
        )
    # No origin may sit in two buckets.
    for i, a in enumerate(buckets):
        for b in buckets[i + 1:]:
            overlap = a & b
            if overlap:
                failures.append(
                    f"origin in multiple buckets: {sorted(o.value for o in overlap)}"
                )
    return failures


def main() -> int:
    checks = [
        ("build-origins-route", _test_build_origins_route_to_build_model()),
        ("thinking-origins-unrouted", _test_thinking_origins_unrouted()),
        ("explicit-overrides-category", _test_explicit_routing_overrides_category()),
        ("explore-model-legacy", _test_explore_model_legacy_fallback()),
        ("every-origin-classified", _test_every_origin_classified()),
    ]
    total = sum(len(f) for _, f in checks)
    if total:
        print("FAIL: model-routing tests")
        for name, fails in checks:
            for f in fails:
                print(f"  [{name}] {f}")
        return 1
    print("PASS: model-routing tests")
    for name, _ in checks:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
