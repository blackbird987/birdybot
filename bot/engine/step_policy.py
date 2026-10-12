"""Workflow policy: model routing per step, verify and autonomy policy, and the chain presets."""

from __future__ import annotations

import logging

from bot.fsutil import read_repo_config
from bot import config
from bot.claude.types import (
    BUILD_ORIGINS, Instance, InstanceOrigin,
)

log = logging.getLogger(__name__)


# Legacy per-step routing: EXPLORE_MODEL covers the mechanical plan-review
# steps for deployments that set it. BUILD_ORIGINS (the plan-vs-build split)
# supersedes it for build-family origins.
_EXPLORE_MODEL_ORIGINS = frozenset(
    {InstanceOrigin.REVIEW_PLAN, InstanceOrigin.APPLY_REVISIONS}
)


def resolve_spawn_model(
    origin: InstanceOrigin, override: str | None = None,
) -> str | None:
    """Pick the ``--model`` a spawned workflow step should carry, or None.

    Precedence, highest first:
      0. *override* — the thread's own ``/model`` pin. An explicit human choice
         beats configured routing: if the user pinned a model and then tapped
         Build, silently running something else would be the surprising outcome.
      1. An explicit ``MODEL_ROUTING`` entry pins one specific origin.
      2. ``BUILD_ORIGINS`` (everything that touches real code) → ``BUILD_MODEL``.
      3. Legacy ``EXPLORE_MODEL`` for the mechanical plan-review steps.
      4. None — unrouted (direct chat, plan, review_plan, apply_revisions).
         The caller leaves ``instance.model`` unset so it falls through to
         ``DEFAULT_SESSION_MODEL`` at command-build time.

    All of this loses to the model-limit failover downgrade, applied later in
    the runner (``model_override`` beats ``instance.model``) — so a thread
    pinned to a model whose own quota is exhausted still runs, on the fallback.
    """
    if override:
        return override
    routed = config.MODEL_ROUTING.get(origin.value)
    if routed:
        return routed
    if origin in BUILD_ORIGINS:
        return config.BUILD_MODEL
    if origin in _EXPLORE_MODEL_ORIGINS and config.EXPLORE_MODEL:
        return config.EXPLORE_MODEL
    return None


def _load_verify_policy(source_inst: Instance | None) -> str:
    """Read verify_policy from .claude/test.json. Default: 'block_fail'.

    Three-value model:
      warn        — never halt; flag+advance on manual/crashed/fail (opt-in,
                    for repos where autopilot should always push through).
      block_fail  — DEFAULT. A reported `fail` (verify ran, feature is broken)
                    halts the chain; `manual`/`crashed` (couldn't verify) still
                    warn+advance so unconfigured repos aren't hard-stopped.
      block       — strictest; halt on any of fail/manual/crashed.
    """
    cfg = read_repo_config(source_inst.repo_path if source_inst else None, "test.json")
    return (cfg or {}).get("verify_policy", "block_fail")


_WORKFLOW_POLICY_DEFAULT = {
    "autonomy": "hold",
    "merge_veto_minutes": 10,
    "ship_grace_minutes": 30,
}


def _safe_pos_int(value, default: int) -> int:
    try:
        n = int(value)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def load_workflow_policy(repo_path: str | None) -> dict:
    """Read per-repo autonomy policy from ``.claude/workflow.json``.

    Returns a dict with keys ``autonomy`` ("hold" | "merge" | "ship"),
    ``merge_veto_minutes``, ``ship_grace_minutes``. Absent/malformed file →
    the ``hold`` default (today's behavior — nothing auto-ships until a repo
    explicitly opts in). Mirrors ``_load_verify_policy``'s read guardrails.

      hold  — DEFAULT. Manual Merge/Discard; nothing fires on its own.
      merge — completed build branches auto-merge after a veto window
              (Hold to stop). Deploy rides along as it does today.
      ship  — like merge, plus the autonomy sweep fleet-ships any stragglers
              (deploy + verify-back) for this repo.
    """
    cfg = read_repo_config(repo_path, "workflow.json")
    if cfg is None:
        return dict(_WORKFLOW_POLICY_DEFAULT)
    autonomy = cfg.get("autonomy", "hold")
    if autonomy not in ("hold", "merge", "ship"):
        autonomy = "hold"
    return {
        "autonomy": autonomy,
        "merge_veto_minutes": _safe_pos_int(cfg.get("merge_veto_minutes"), 10),
        "ship_grace_minutes": _safe_pos_int(cfg.get("ship_grace_minutes"), 30),
    }


# --- Autopilot chains ---

_AUTOPILOT_STEPS = [
    "review_loop", "build", "sensors", "review_code", "verify",
    "done", "verify_release", "release", "merge",
]
_BUILD_AND_SHIP_STEPS = [
    "build", "sensors", "review_code", "verify",
    "done", "verify_release", "release", "merge",
]
_AUTOPILOT_HOLD_STEPS = [
    "review_loop", "build", "sensors", "review_code", "verify",
    "done", "verify_release", "release",
]

# Conversational-chain presets. The plan already emerged in the chat, so
# these all START at "build" (no review_loop) and take their plan from the
# stored override (see _extract_latest_plan_text). Keyed by the `preset=`
# value on a /chain directive.
_CHAIN_PRESETS: dict[str, list[str]] = {
    # Full ship: build → review → verify → done → release → merge (quiet close).
    "ship": [
        "build", "sensors", "review_code", "verify",
        "done", "verify_release", "release", "merge",
    ],
    # Build + release but stop before merge — parks on Merge/Discard (or the
    # auto-merge veto window if the repo opted into merge/ship autonomy).
    "hold": [
        "build", "sensors", "review_code", "verify",
        "done", "verify_release", "release",
    ],
    # Build + verify loop only — the "build and verify" flow. Stops after
    # verify with the branch open.
    "verify": ["build", "sensors", "review_code", "verify"],
}
