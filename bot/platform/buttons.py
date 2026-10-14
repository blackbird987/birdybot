"""Platform-agnostic button specs for result, running and queued cards, split out of formatting.py.

The TL;DR button placement rules live in ``action_button_specs``; see
docs/design/tldr.md, "TL;DR has two shapes".
"""

from __future__ import annotations

from bot import config
from bot.claude.types import CODE_CHANGE_TOOLS, PLAN_ORIGINS, Instance, InstanceOrigin, InstanceStatus
from bot.platform.base import ButtonSpec
from bot.platform.modes import _NEXT_MODE, _WORKFLOW_ORIGINS, mode_name

# --- Button Specs (platform-agnostic) ---

def merge_failed_button_specs(instance_id: str) -> list[list[ButtonSpec]]:
    """Buttons posted after an auto-merge failure — retry or discard.

    Posting actual buttons (instead of plain text) means the user's tap is
    routed through the existing merge/discard handlers rather than typing
    text into the thread, which Claude would otherwise reinterpret as a
    fresh review/build prompt.
    """
    return [[
        ButtonSpec("Resolve with Claude", f"resolve_merge:{instance_id}"),
        ButtonSpec("Try Merge Again", f"merge:{instance_id}"),
        ButtonSpec("Discard", f"discard:{instance_id}"),
    ]]


def merge_failed_banner(failure_kind: str | None) -> str:
    """Compose the user-facing banner for a failed auto-merge.

    Branches on the ``MERGE_FAIL_*`` taxonomy from ``bot/claude/runner/git_merge.py``
    (kept as string constants so this module can stay platform-agnostic
    without importing runner). Empty / unknown kinds fall back to the
    generic conflict copy — same as before the t-4114 work landed.
    """
    if failure_kind == "orphaned_index":
        return (
            "Auto-merge failed because the main repo has an orphaned "
            "merge in its index. Tap **Try Merge Again** — the next "
            "attempt will detect and auto-recover the leftover state "
            "(any local changes are preserved in a stash). **Resolve "
            "with Claude** also works. **Discard** drops the branch."
        )
    if failure_kind == "recovery_failed":
        return (
            "Auto-merge failed and automatic recovery couldn't unstick "
            "the main repo. **Manual intervention needed** — open a "
            "terminal in the repo and run `git reset --merge` or "
            "`git status` to inspect. Any work the bot preserved is "
            "recorded in a labeled stash (see the failure detail above). "
            "Once unstuck, tap **Try Merge Again**."
        )
    if failure_kind == "diverged":
        # Deliberately does NOT suggest Resolve with Claude — the resolver
        # fixes branch-vs-master conflict markers inside the worktree and
        # never touches local-vs-origin divergence, so it can't help here.
        return (
            "Auto-merge stopped before merging: local master and "
            "origin have diverged (commits exist on both sides — see the "
            "counts above). Nothing was changed. Run `git pull --rebase` "
            "in the main repo to reconcile, then tap **Try Merge Again**."
        )
    return (
        "Auto-merge failed. Tap **Try Merge Again** to retry "
        "(useful if a parallel build just completed) or **Discard** "
        "to drop the branch. Or describe the situation here to talk "
        "it through with Claude."
    )


def resolver_running_button_specs(instance_id: str) -> list[list[ButtonSpec]]:
    """Buttons shown while the merge-conflict resolver is in flight.

    Only Cancel — both Merge and Discard are blocked until the resolver
    finishes or is cancelled, since either action would race the worktree
    state Claude is actively editing.
    """
    return [[
        ButtonSpec("Cancel", f"resolve_cancel:{instance_id}"),
    ]]


def _plan_action_rows(iid: str, has_autopilot_chain: bool) -> list[list[ButtonSpec]]:
    """The button rows offered when a plan is on the table.

    Hide the Autopilot starters when a chain is already paused — Continue
    Autopilot elsewhere on the card is the correct resumption path.
    """
    rows: list[list[ButtonSpec]] = []
    if not has_autopilot_chain:
        rows.append([
            ButtonSpec("Autopilot", f"autopilot:{iid}"),
            ButtonSpec("Autopilot (Hold)", f"autopilot_hold:{iid}"),
        ])
    rows.append([
        ButtonSpec("Review Plan", f"review_plan:{iid}"),
        ButtonSpec("Build & Ship", f"build_and_ship:{iid}"),
        ButtonSpec("Done", f"done:{iid}"),
    ])
    return rows


def action_button_specs(
    instance: Instance, show_expand: bool = False,
    has_autopilot_chain: bool = False,
) -> list[list[ButtonSpec]]:
    """Return button row specs based on instance status and origin.

    has_autopilot_chain: if True, this session has a paused autopilot chain
    that can be resumed via Continue Autopilot.
    """
    rows: list[list[ButtonSpec]] = []
    iid = instance.id

    # Resolver origin: no buttons on the resolver's own result. The resolve_merge
    # handler posts a follow-up message with the appropriate Merge/Retry/Discard
    # row once verification + post-merge has run.
    if instance.origin == InstanceOrigin.RESOLVE_MERGE:
        return rows

    # Prompt review: its result is a report posted in The Ark, not a turn in a
    # repo conversation. The Ark is not a repo forum, so Retry / Plan / Build &
    # Ship / Branch would all resolve against whichever repo happens to be
    # globally active, and the review's own Approve / Reject buttons are posted
    # separately by bot/discord/prompt_review.py. Keep only the controls that
    # mean something here: stopping it, reading its log, and expanding a report
    # too long to render inline.
    if instance.origin == InstanceOrigin.PROMPT_REVIEW:
        if instance.status in (InstanceStatus.RUNNING, InstanceStatus.QUEUED):
            rows.append([ButtonSpec("Kill", f"kill:{iid}")])
        elif instance.status == InstanceStatus.FAILED:
            rows.append([ButtonSpec("Log", f"log:{iid}")])
        if show_expand:
            rows.append([
                ButtonSpec("Expand \u25bc", f"expand:{iid}"),
                ButtonSpec("Full Log", f"log:{iid}"),
            ])
        return rows

    # Done origin: if branch is pending merge and no autopilot, show Merge/Discard
    if instance.origin == InstanceOrigin.DONE and instance.status == InstanceStatus.COMPLETED:
        if instance.branch and not has_autopilot_chain:
            rows.append([
                ButtonSpec("Merge", f"merge:{iid}"),
                ButtonSpec("Discard", f"discard:{iid}"),
            ])
        return rows

    if instance.status == InstanceStatus.COMPLETED:
        tools = set(instance.tools_used or [])
        made_code_changes = bool(tools & CODE_CHANGE_TOOLS)
        # this_planned: THIS instance produced/dealt with a plan
        this_planned = bool(
            {"EnterPlanMode", "ExitPlanMode"} & tools
            or instance.origin in PLAN_ORIGINS
            or instance.mode == "plan"
        )
        # session_has_plan: inherited plan_active from any sibling
        session_has_plan = instance.plan_active

        if instance.branch:
            if has_autopilot_chain:
                # Autopilot handles merge — show review/action buttons plus a
                # Discard escape hatch (user may inspect the Diff and decide to
                # bail out of the branch even mid-chain).
                rows.append([
                    ButtonSpec("Diff", f"diff:{iid}"),
                    ButtonSpec("Review Code", f"review_code:{iid}"),
                    ButtonSpec("Commit", f"commit:{iid}"),
                    ButtonSpec("Done", f"done:{iid}"),
                    ButtonSpec("Discard", f"discard:{iid}"),
                ])
            else:
                # Manual build — keep full merge workflow
                rows.append([
                    ButtonSpec("Diff", f"diff:{iid}"),
                    ButtonSpec("Merge", f"merge:{iid}"),
                    ButtonSpec("Discard", f"discard:{iid}"),
                ])
                rows.append([
                    ButtonSpec("Review Code", f"review_code:{iid}"),
                    ButtonSpec("Commit", f"commit:{iid}"),
                    ButtonSpec("Done", f"done:{iid}"),
                ])
        elif this_planned:
            # This instance directly produced or reviewed a plan
            if instance.origin == InstanceOrigin.REVIEW_PLAN:
                # Just reviewed — offer to apply or ship
                rows.append([
                    ButtonSpec("Apply Revisions", f"apply_revisions:{iid}"),
                    ButtonSpec("Build & Ship", f"build_and_ship:{iid}"),
                    ButtonSpec("Done", f"done:{iid}"),
                ])
            else:
                # Plan created or revisions applied
                rows.extend(_plan_action_rows(iid, has_autopilot_chain))
        elif made_code_changes:
            # Edited/wrote files in-place (no branch)
            rows.append([
                ButtonSpec("Review Code", f"review_code:{iid}"),
                ButtonSpec("Retry", f"retry:{iid}"),
                ButtonSpec("Done", f"done:{iid}"),
            ])
        elif instance.code_active:
            # Session has uncommitted code changes — offer commit/review
            rows.append([
                ButtonSpec("Commit", f"commit:{iid}"),
                ButtonSpec("Review Code", f"review_code:{iid}"),
                ButtonSpec("Done", f"done:{iid}"),
            ])
        elif session_has_plan:
            # Fallback: session has a plan from a prior instance, and this
            # instance didn't do anything code-related — offer plan actions.
            rows.extend(_plan_action_rows(iid, has_autopilot_chain))
        else:
            # Default buttons + workflow row when session exists
            rows.append([
                ButtonSpec("New", f"new:{iid}"),
                ButtonSpec("Retry", f"retry:{iid}"),
            ])
            if instance.session_id:
                rows.append([
                    ButtonSpec("Plan", f"plan:{iid}"),
                    ButtonSpec("Build & Ship", f"build_and_ship:{iid}"),
                    ButtonSpec("Done", f"done:{iid}"),
                ])

    elif instance.status in (InstanceStatus.RUNNING, InstanceStatus.QUEUED):
        rows.append([ButtonSpec("Kill", f"kill:{iid}")])

    elif instance.status == InstanceStatus.FAILED:
        if instance.cooldown_retry_at:
            row = [ButtonSpec("Cancel Auto-Retry", f"cancel_cooldown:{iid}")]
            if config.API_FALLBACK_ENABLED:
                cap = config.API_FALLBACK_MAX_USD
                row.insert(0, ButtonSpec(
                    f"Continue with {config.API_FALLBACK_MODEL} (≤${cap:.2f})",
                    f"continue_ppu:{iid}",
                ))
            rows.append(row)
        else:
            rows.append([
                ButtonSpec("Retry", f"retry:{iid}"),
                ButtonSpec("Log", f"log:{iid}"),
            ])

    elif instance.status == InstanceStatus.KILLED:
        rows.append([ButtonSpec("Retry", f"retry:{iid}")])

    # Continue Autopilot — shown when session has a paused chain
    if has_autopilot_chain and instance.status == InstanceStatus.COMPLETED:
        rows.append([ButtonSpec("Continue Autopilot", f"continue_autopilot:{iid}")])

    # Mode toggle — only on non-workflow completions
    if (instance.status == InstanceStatus.COMPLETED
            and instance.origin not in _WORKFLOW_ORIGINS):
        target = _NEXT_MODE.get(instance.mode, "explore")
        label = mode_name(target)
        rows.append([ButtonSpec(f"Mode: {label}", f"mode_{target}:{iid}")])

    # Branch from here — fork the session at the message this button is on.
    # Gated on session_id only; the click handler falls back to the last
    # assistant uuid in the JSONL when the per-message map has no entry
    # (e.g. the first render, before send_result stamps message ids).
    # Reserve a row for Expand when show_expand=True so long-result messages
    # keep their Expand button even with a crowded set of action buttons.
    # Branch+Share row takes priority; when it fires, omit Share from the
    # Expand row below to avoid showing two Share buttons on the same message.
    branch_cap = 4 if show_expand else 5
    share_added = False
    branch_row: list[ButtonSpec] | None = None
    if (instance.status == InstanceStatus.COMPLETED
            and instance.session_id
            and len(rows) < branch_cap):
        branch_row = [
            ButtonSpec("Branch", f"branch:{iid}"),
            ButtonSpec("Share", f"share:{iid}"),
        ]
        rows.append(branch_row)
        share_added = True

    expand_row: list[ButtonSpec] | None = None
    if show_expand:
        expand_row = [
            ButtonSpec("Expand \u25bc", f"expand:{iid}"),
            ButtonSpec("Full Log", f"log:{iid}"),
        ]
        if instance.session_id and not share_added:
            expand_row.append(ButtonSpec("Share", f"share:{iid}"))
        rows.append(expand_row)

    # TL;DR: a plain-language recap of whatever this turn was about.
    #
    # It rides an existing row instead of claiming one of the five Discord
    # allows: the Expand row when a long result built one, otherwise the
    # Branch/Share row. Both are gated on session_id, which TL;DR needs
    # anyway (it resumes the session). When neither row exists the button is
    # dropped rather than displacing a Merge or plan row, since /tldr covers
    # the same ground from the keyboard.
    #
    # Never on a TL;DR's own card: re-summarising a summary says nothing, and
    # the recursion has no natural floor.
    if (instance.status == InstanceStatus.COMPLETED
            and instance.session_id
            and instance.origin != InstanceOrigin.TLDR):
        host = expand_row if expand_row is not None else branch_row
        if host is not None:
            host.append(ButtonSpec("TL;DR", f"tldr:{iid}"))

    return rows


def expanded_button_specs(instance: Instance) -> list[list[ButtonSpec]]:
    """Action buttons + Collapse for expanded view."""
    rows = action_button_specs(instance)
    collapse_row = [
        ButtonSpec("Collapse \u25b2", f"collapse:{instance.id}"),
        ButtonSpec("Full Log", f"log:{instance.id}"),
    ]
    # Share fallback: skip if action_button_specs already placed it (Branch row)
    already_has_share = any(
        b.callback_data.startswith("share:") for row in rows for b in row
    )
    if instance.session_id and not already_has_share:
        collapse_row.append(ButtonSpec("Share", f"share:{instance.id}"))
    rows.append(collapse_row)
    return rows


def running_button_specs(instance_id: str) -> list[list[ButtonSpec]]:
    """Stop button shown on progress messages while an instance is running."""
    return [[ButtonSpec("Stop", f"kill:{instance_id}")]]


def stall_button_specs(instance_id: str) -> list[list[ButtonSpec]]:
    return [[
        ButtonSpec("Kill", f"kill:{instance_id}"),
        ButtonSpec("Wait", f"wait:{instance_id}"),
    ]]


def queued_button_specs(
    pending_id: str, supports_steer: bool,
) -> list[list[ButtonSpec]]:
    """Buttons on a 'Queued' message while a run holds the channel lock.

    Steer button is omitted when the provider can't resume a killed session.
    """
    row: list[ButtonSpec] = []
    if supports_steer:
        row.append(ButtonSpec("Steer Now", f"steer:{pending_id}"))
    row.append(ButtonSpec("Cancel", f"cancel_pending:{pending_id}"))
    return [row]
