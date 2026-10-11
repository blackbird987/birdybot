"""Command handlers, re-exported from the modules that define them.

This module used to hold all of them. It re-exports every old name so
callers keep working, and nothing more. A patch here reaches only callers
that look the name up through this module (the Discord layer calls
``commands.on_text`` and ``commands.handle_callback`` that way); anything
else must be patched in the module that reads it.
"""

from __future__ import annotations

from bot.engine.admin_cmds import (  # noqa: F401
    _accounts_status_line,
    on_budget,
    on_clear,
    on_cost,
    on_deferred,
    on_evals,
    on_help,
    on_logs,
    on_reboot,
    on_session,
    on_shutdown,
    on_status,
    on_usage,
)
from bot.engine.branch_actions import (  # noqa: F401
    _finish_branch_action,
    _finish_resolved_branch,
    _strip_post_merge_buttons,
    on_branches,
    on_discard,
    on_merge,
    perform_discard,
)
from bot.engine.callbacks import (  # noqa: F401
    _clear_expand_overflow,
    handle_callback,
)
from bot.engine.cmd_state import (  # noqa: F401
    _channel_locks,
    _clear_chain_on_manual_finalize,
    _cli_version,
    _get_channel_lock,
    _shutdown_fn,
    _start_time,
    budget_warning,
    check_budget,
    get_start_time,
    init,
)
from bot.engine.directives.chain import (  # noqa: F401
    _CHAIN_ALLOWED_PRESETS,
    _CHAIN_DIRECTIVE_RE,
    _CHAIN_PLAN_MAX_BYTES,
    _extract_chain_directive,
    _handle_chain_directive,
    _parse_chain_preset,
)
from bot.engine.directives.dispatch import (  # noqa: F401
    _ALLOWED_BOT_CMD_ACTIONS,
    _BOT_CMD_RE,
    _DANGEROUS_PATH_CHARS,
    _execute_bot_commands,
)
from bot.engine.directives.reply import (  # noqa: F401
    _MAX_REPLIES_PER_RESPONSE,
    _REPLY_ALLOWED_KEYS,
    _REPLY_BODY_MAX_BYTES,
    _REPLY_DIRECTIVE_RE,
    _handle_reply_directives,
    _own_child_thread_ids,
    _pair_reply_directives,
)
from bot.engine.directives.spawn import (  # noqa: F401
    _MAX_SPAWNS_PER_RESPONSE,
    _MAX_SPAWN_WAVES,
    _SPAWN_ALLOWED_EFFORTS,
    _SPAWN_ALLOWED_KEYS,
    _SPAWN_ALLOWED_MODES,
    _SPAWN_DIRECTIVE_RE,
    _SPAWN_KV_RE,
    _SPAWN_PROMPT_MAX_BYTES,
    _SPAWN_TITLE_MAX,
    _TITLE_CONTROL_CHARS,
    _handle_spawn_directive,
    _pair_spawn_directives,
    _parse_spawn_kv,
    _resolve_parent_instance,
)
from bot.engine.instance_cmds import (  # noqa: F401
    _DISCORD_UPLOAD_MAX,
    _SAFE_UPLOAD,
    _run_bg_task,
    _start_retry,
    kill_instance,
    on_bg,
    on_diff,
    on_kill,
    on_list,
    on_log,
    on_new,
    on_release,
    on_retry,
    on_share,
    perform_kill,
    stop_instances,
)
from bot.engine.query import (  # noqa: F401
    _enqueue_with_pending_ui,
    _execute_query,
    _finish_pending_on_acquire,
    _run_query,
    on_text,
)
from bot.engine.repo_cmds import (  # noqa: F401
    _FAST_REPO_PATTERNS,
    _RESERVED_REPO_NAMES,
    _create_repo,
    _repo_desc,
    _repo_set_visibility,
    _resolve_default_path,
    _try_fast_repo_command,
    _validate_repo_name,
    on_repo,
)
from bot.engine.resolver import (  # noqa: F401
    _RESOLVE_TIMEOUT_SECONDS,
    _build_resolver_prompt,
    _merge_context_prefix,
    _on_resolve_cancel,
    _on_resolve_merge,
    _post_resolver_failure,
    _resolver_in_flight,
    _resolver_prep_worktree,
    _resolver_verify,
)
from bot.engine.settings_cmds import (  # noqa: F401
    _VALID_EFFORT,
    on_alias,
    on_context,
    on_effort,
    on_mode,
    on_model,
    on_provider,
    on_schedule,
    on_verbose,
)
