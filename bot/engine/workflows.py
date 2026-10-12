"""Workflow steps and chains, re-exported from the modules that define them.

This module used to hold all of them. It re-exports every old name so
callers keep working, and nothing more. A patch here reaches only callers
that look the name up through this module; anything else must be patched
in the module that reads it. scripts/test_facades.py enforces both.
"""

from __future__ import annotations

from bot.claude.branch_utils import clear_stale_branches  # noqa: F401  (re-exported for callers)
from bot.platform.formatting import merge_failed_banner  # noqa: F401
from bot.engine.chain_git import (  # noqa: F401
    _DIFF_PAYLOAD_CAP,
    _UNRELEASED_RE,
    _find_mergeable_instance,
    _find_prior_build_for_chain,
    _find_recoverable_session_predecessor,
    _find_session_branch_instance,
    _git_diff_payload,
    _git_diff_stat,
    _git_head,
    _git_head_sha,
    _git_log_messages,
    _is_worktree_live,
    _read_unreleased_block,
    _resolve_chain_repo_path,
)
from bot.engine.chain_runner import (  # noqa: F401
    _CHAIN_EXIT_MESSAGES,
    _attempt_finishup_nudge,
    _build_halted_text,
    _eval_chain_safe,
    _exit_chain,
    _exit_chain_needs_input,
    _launch_chain,
    _review_plan_loop,
    _run_autopilot_chain,
    on_amend_done,
    on_autopilot,
    on_autopilot_hold,
    on_build_and_ship,
    on_continue_anyway,
    on_conversational_chain,
    resume_autopilot_chain,
)
from bot.engine.release_steps import (  # noqa: F401
    on_release_chain,
    on_verify_release,
)
from bot.engine.step_parse import (  # noqa: F401
    VerifyOutcome,
    _BUILD_PLAN_INJECT_MAX,
    _FENCE_LINE_RE,
    _HIGH_PRIO_RE,
    _JSON_BLOCK_RE,
    _PHASE_PLAN_RE,
    _PLAN_METADATA_MARKERS,
    _PRIOR_ART_INJECT_MAX,
    _REVIEW_LOOP_MAX_ROUNDS,
    _REVIEW_STATUS_RE,
    _TRIAGE_RESULT_RE,
    _VERIFY_ACTIONS_RE,
    _VERIFY_BLOCK_RE,
    _VERIFY_ENDPOINTS_RE,
    _VERIFY_RESULT_RE,
    _VERIFY_SUMMARY_RE,
    _VERIFY_WHY_RE,
    _extract_deferred,
    _extract_latest_plan_text,
    _extract_phase_plan,
    _extract_triage_deferred,
    _find_phase_plan,
    _needs_revision,
    _parse_deferred_block,
    _parse_verifier_json,
    _phase_gate_suffix,
    _stop_reason_snippet,
    _verify_outcome,
    _verify_why,
)
from bot.engine.step_policy import (  # noqa: F401
    _AUTOPILOT_HOLD_STEPS,
    _AUTOPILOT_STEPS,
    _BUILD_AND_SHIP_STEPS,
    _CHAIN_PRESETS,
    _EXPLORE_MODEL_ORIGINS,
    _WORKFLOW_POLICY_DEFAULT,
    _load_verify_policy,
    _safe_pos_int,
    load_workflow_policy,
    resolve_spawn_model,
)
from bot.engine.step_spawn import (  # noqa: F401
    SpawnConfig,
    _enforce_readonly_floor,
    _last_msg_id,
    _notify_user,
    spawn_from,
    spawn_resolver_detached,
)
from bot.engine.steps import (  # noqa: F401
    _attempt_inline_worktree_recovery,
    MergeOutcome,
    _finalize_merge,
    _prior_art_prefix,
    merge_and_record,
    merge_blocks_ship,
    merge_from_user,
    record_merge_failure,
    on_apply_revisions,
    on_build,
    on_commit,
    on_done,
    on_plan,
    on_review_code,
    on_review_plan,
    on_sensors,
    on_sess_resume,
    on_tldr,
    on_verify,
)
