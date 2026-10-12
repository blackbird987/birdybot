"""Coding CLI subprocess management: streaming, kill, semaphore, stall detection, git worktrees.

The runner is a package of mixins (see ``core.ClaudeRunner``). This module only
re-exports, so every ``from bot.claude.runner import X`` written before the
split keeps working. Patch a name in the module that reads it, never here:
a patch on this package succeeds and changes nothing.
"""

from bot.claude.runner.common import (  # noqa: F401
    ContextResetCallback,
    ProgressCallback,
    RecoveryCallback,
    SESSION_STRIPPED_ENV_VARS,
    StallCallback,
    _NOWND,
    _notify,
)
from bot.claude.runner.reaping import (  # noqa: F401
    StallSnapshot,
    _KILL_SIGNALS,
    _OOM_JOURNAL_LAG_SECS,
    _SIGKILL_EXIT_CODES,
    _SIGNAL_EXIT_CODES,
    _SIGTERM_EXIT_CODES,
    _capture_process_snapshot,
    _classify_oom_kill,
    _describe_last_tool,
    _fleet_kill_detail,
    _fmt_duration,
    _last_tool_name,
    _lifetime_kill_detail,
    _lifetime_kill_result,
    _memory_kill_detail,
    _memory_kill_result,
    _memory_warning_detail,
    _no_productive_work,
    _reaped_result_base,
    _turn_completed_successfully,
    is_kill_shape,
)
from bot.claude.runner.git_release import (  # noqa: F401
    _GIT_PROGRESS,
    _RELEASE_COMMIT_RE,
    _TAG_WALKBACK_CAP,
    _VERSION_TAG_RE,
    _is_ancestor,
    _is_git_progress,
    _parse_version_tag,
    git_fail_reason,
    missing_predecessor_release,
    orphaned_releases,
    version_tags,
)
from bot.claude.runner.git_merge import (  # noqa: F401
    DiscardOutcome,
    MERGE_FAIL_DIVERGED,
    MERGE_FAIL_GENERIC,
    MERGE_FAIL_ORPHANED_INDEX,
    MERGE_FAIL_RECOVERY_FAILED,
    PrecheckResult,
)
from bot.claude.runner.worktrees import (  # noqa: F401
    ReleaseRecoveryEvent,
    WorktreeRecoveryEvent,
)
from bot.claude.runner.session_files import (  # noqa: F401
    HYDRATE_CACHE_TTL_SECS,
    REBUILD_CACHE_TTL_SECS,
    REBUILD_TIMEOUT_SECS,
    _UUID_RE,
)
from bot.claude.runner.accounts import (  # noqa: F401
    REFUSAL_GRACE_MINUTES,
)
from bot.claude.runner.admission import (  # noqa: F401
    AdmissionBlock,
)
from bot.claude.runner.recovery import (  # noqa: F401
    _carry_forward_work_record,
    _is_primary_model,
)
from bot.claude.runner.core import (  # noqa: F401
    ClaudeRunner,
    RebootResult,
    _lower_priority,
    _save_result_file,
    _wait_or_kill,
)
