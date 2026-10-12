"""Per-session chain state: autopilot steps and metadata, pending and scheduled
merges, the plan handoff, deferred revisions, entry SHA and phase state.

A mixin of ``StateStore`` (bot/store/state.py), which owns the dicts these
methods read and the persistence that saves them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bot.claude.types import ChainPhaseState

log = logging.getLogger(__name__)


class ChainsMixin:
    # --- Autopilot Chain State ---

    def get_all_autopilot_chains(self) -> dict[str, list[str]]:
        """Return all active autopilot chains (session_id -> remaining steps)."""
        return dict(self._autopilot_chains)

    def get_autopilot_chain(self, session_id: str | None) -> list[str] | None:
        """Get remaining autopilot steps for a session."""
        if not session_id:
            return None
        return self._autopilot_chains.get(session_id)

    def set_autopilot_chain(self, session_id: str | None, steps: list[str]) -> None:
        """Store remaining autopilot steps for a session."""
        if not session_id:
            return
        self._autopilot_chains[session_id] = steps
        self._autopilot_chain_meta[session_id] = {
            "status": "running",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.mark_dirty()

    def clear_autopilot_chain(self, session_id: str | None) -> None:
        """Remove autopilot chain state for a session.

        Reaps every per-session chain field together (queue, meta, kwargs, and
        the conversational-chain plan override) so an abandoned or reset chain
        can't leave a stale override behind for a later chain to inject. The
        override's lifetime is exactly the chain's; clear points are terminal
        only (steps use set_autopilot_chain to update the queue).
        """
        if not session_id:
            return
        self._autopilot_chains.pop(session_id, None)
        self._autopilot_chain_meta.pop(session_id, None)
        self._chain_kwargs.pop(session_id, None)
        self._chain_plan_overrides.pop(session_id, None)
        self.mark_dirty()

    def set_chain_kwargs(
        self,
        session_id: str | None,
        *,
        label: str,
        silent_close: bool,
    ) -> None:
        """Persist chain entry-point kwargs so resume can restore them.

        Written once at chain launch (see `_launch_chain` in chain_runner.py).
        Resume paths read via `get_chain_kwargs` so a paused Build & Ship that
        the user taps Continue on doesn't revert to "Autopilot" labeling and
        loud close. Unconditional write — no chain-queue guard — because the
        launch path writes kwargs before the chain queue exists, and
        clear_autopilot_chain reaps both fields together so orphan kwargs
        without a queue can't accumulate.
        """
        if not session_id:
            return
        self._chain_kwargs[session_id] = {
            "label": label,
            "silent_close": silent_close,
        }
        self.mark_dirty()

    def get_chain_kwargs(
        self, session_id: str | None,
    ) -> tuple[str, bool]:
        """Return (label, silent_close); falls back to Autopilot defaults."""
        if not session_id:
            return ("Autopilot", False)
        data = self._chain_kwargs.get(session_id)
        if not data:
            return ("Autopilot", False)
        return (
            data.get("label", "Autopilot"),
            bool(data.get("silent_close", False)),
        )

    def get_autopilot_chain_meta(self, session_id: str | None) -> dict | None:
        """Return {status, updated_at} for a session's chain, or None."""
        if not session_id:
            return None
        return self._autopilot_chain_meta.get(session_id)

    def set_autopilot_chain_status(
        self, session_id: str | None, status: str
    ) -> None:
        """Update chain status to 'running' or 'paused' with a timestamp."""
        if not session_id or status not in ("running", "paused"):
            return
        # Only stamp meta if the chain queue still exists — clearing the chain
        # already drops meta, and we don't want zombie meta with no queue.
        if session_id not in self._autopilot_chains:
            return
        self._autopilot_chain_meta[session_id] = {
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.mark_dirty()

    # --- Pending Merges (auto-merge failed; awaiting user resolution) ---

    def set_pending_merge(
        self,
        instance_id: str,
        *,
        session_id: str | None,
        channel_id: str,
        repo_name: str,
        message: str,
        failure_kind: str | None = None,
    ) -> None:
        """Record that an auto-merge failed and is awaiting user action.

        Set when autopilot or standalone-Done auto-merge fails. While present,
        any plain-text message in the channel is prefixed with a system note
        in on_text so Claude doesn't reinterpret it as a fresh task.

        ``failure_kind`` is one of the ``MERGE_FAIL_*`` constants in
        ``bot/claude/runner/git_merge.py``. When supplied, the platform layer can
        render a more accurate banner (e.g. orphaned-index recovery
        guidance vs a generic conflict resolver). ``None`` means
        "unclassified" and callers fall back to the generic banner copy.
        """
        if not instance_id:
            return
        # Preserve resolver_instance_id and deferred_text across re-set so a
        # verify-fail re-post doesn't drop active resolver state or the
        # user's accumulated typed guidance.
        existing = self._pending_merges.get(instance_id, {})
        self._pending_merges[instance_id] = {
            "session_id": session_id or "",
            "channel_id": channel_id,
            "repo_name": repo_name,
            "message": message,
            "created_at": existing.get("created_at")
            or datetime.now(timezone.utc).isoformat(),
            "resolver_instance_id": existing.get("resolver_instance_id", ""),
            "deferred_text": existing.get("deferred_text", ""),
            "failure_kind": failure_kind or existing.get("failure_kind", ""),
        }
        self.save()

    def set_pending_merge_failure_kind(
        self, instance_id: str, failure_kind: str | None,
    ) -> None:
        """Update the failure_kind on an existing pending_merge entry.

        Used by Try-Merge-Again flows: the original set_pending_merge call
        already captured channel/session context; a follow-up merge attempt
        may classify differently (e.g. generic conflict on retry resolves to
        an orphaned-index leak) and needs to refresh the banner copy without
        re-running the full setter.
        """
        meta = self._pending_merges.get(instance_id)
        if not meta:
            return
        meta["failure_kind"] = failure_kind or ""
        self.save()

    def set_pending_merge_resolver(
        self, instance_id: str, resolver_instance_id: str | None,
    ) -> None:
        """Set or clear the in-flight resolver instance id for a pending merge."""
        meta = self._pending_merges.get(instance_id)
        if not meta:
            return
        meta["resolver_instance_id"] = resolver_instance_id or ""
        self.save()

    def append_pending_merge_deferred_text(
        self, instance_id: str, text: str, cap: int = 2000,
    ) -> str:
        """Append user-typed guidance to a pending merge, capped at *cap* chars.

        Appends with a newline separator so successive refinements ("fix the
        conflicts", "preserve both [Unreleased] sections") both reach Claude
        on the next Resolve spawn. Returns the new accumulated text.
        """
        meta = self._pending_merges.get(instance_id)
        if not meta:
            return ""
        existing = (meta.get("deferred_text") or "").strip()
        addition = (text or "").strip()
        if not addition:
            return existing
        joined = f"{existing}\n{addition}".strip() if existing else addition
        if len(joined) > cap:
            joined = joined[-cap:]
        meta["deferred_text"] = joined
        self.save()
        return joined

    def clear_pending_merge_deferred_text(self, instance_id: str) -> None:
        """Drop accumulated deferred_text after it's been consumed by a spawn."""
        meta = self._pending_merges.get(instance_id)
        if not meta:
            return
        if meta.get("deferred_text"):
            meta["deferred_text"] = ""
            self.save()

    def get_pending_merge(self, instance_id: str) -> dict | None:
        """Return pending-merge metadata for an instance, or None."""
        return self._pending_merges.get(instance_id)

    def get_pending_merge_by_session(self, session_id: str | None) -> tuple[str, dict] | None:
        """Find a pending merge by session_id. Returns (instance_id, meta) or None."""
        if not session_id:
            return None
        for iid, meta in self._pending_merges.items():
            if meta.get("session_id") == session_id:
                return iid, meta
        return None

    def get_pending_merge_by_channel(self, channel_id: str) -> tuple[str, dict] | None:
        """Find a pending merge by channel_id. Returns (instance_id, meta) or None."""
        if not channel_id:
            return None
        for iid, meta in self._pending_merges.items():
            if meta.get("channel_id") == channel_id:
                return iid, meta
        return None

    def clear_pending_merge(self, instance_id: str) -> None:
        """Remove pending-merge state once the user resolves the merge."""
        if not instance_id:
            return
        if self._pending_merges.pop(instance_id, None) is not None:
            self.save()

    # --- Scheduled Merges (auto-merge veto window) ---

    def set_scheduled_merge(
        self,
        instance_id: str,
        *,
        session_id: str | None,
        channel_id: str,
        repo_name: str | None,
        branch: str | None,
        merge_at: str,
    ) -> None:
        """Schedule a completed build branch to auto-merge at *merge_at* (ISO).

        The autonomy loop in bot/autonomy.py fires due, un-held entries through
        ``_finalize_merge`` — the same path the Merge button uses. Persisted so
        a reboot mid-window resumes the countdown instead of dropping it.
        """
        if not instance_id:
            return
        self._scheduled_merges[instance_id] = {
            "session_id": session_id or "",
            "channel_id": channel_id,
            "repo_name": repo_name or "",
            "branch": branch or "",
            "merge_at": merge_at,
            "held": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save()

    def get_scheduled_merge_by_session(
        self, session_id: str | None,
    ) -> tuple[str, dict] | None:
        """Return (instance_id, meta) for a session's scheduled merge, or None."""
        if not session_id:
            return None
        for iid, meta in self._scheduled_merges.items():
            if meta.get("session_id") == session_id:
                return iid, meta
        return None

    def hold_scheduled_merge(self, instance_id: str) -> bool:
        """Mark a scheduled merge as held (user vetoed). Returns True if found."""
        meta = self._scheduled_merges.get(instance_id)
        if not meta:
            return False
        meta["held"] = True
        self.save()
        return True

    def list_due_scheduled_merges(self, now_iso: str) -> list[tuple[str, dict]]:
        """Return (instance_id, meta) for un-held merges whose time has come."""
        due: list[tuple[str, dict]] = []
        for iid, meta in list(self._scheduled_merges.items()):
            if meta.get("held"):
                continue
            merge_at = meta.get("merge_at")
            if merge_at and now_iso >= merge_at:
                due.append((iid, meta))
        return due

    def clear_scheduled_merge(self, instance_id: str) -> None:
        if not instance_id:
            return
        if self._scheduled_merges.pop(instance_id, None) is not None:
            self.save()

    # --- Conversational-chain plan handoff ---

    def set_chain_plan_override(self, session_id: str | None, plan_text: str) -> None:
        """Store the plan a /chain directive handed off from the conversation."""
        if not session_id or not plan_text:
            return
        self._chain_plan_overrides[session_id] = plan_text
        self.save()

    def get_chain_plan_override(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        return self._chain_plan_overrides.get(session_id)

    # Note: no standalone clear — the override is reaped by
    # clear_autopilot_chain (its lifetime is exactly the chain's), so that is
    # the single canonical teardown point.

    # --- Chain Deferred Revisions ---

    def get_chain_deferred(self, session_id: str | None) -> list[str]:
        """Get deferred revisions persisted for an autopilot chain."""
        if not session_id:
            return []
        return self._chain_deferred.get(session_id, [])

    def set_chain_deferred(self, session_id: str | None, revisions: list[str]) -> None:
        """Persist deferred revisions for an autopilot chain."""
        if not session_id or not revisions:
            return
        self._chain_deferred[session_id] = revisions
        self.mark_dirty()

    def clear_chain_deferred(self, session_id: str | None) -> None:
        """Remove deferred revisions for an autopilot chain."""
        if not session_id:
            return
        self._chain_deferred.pop(session_id, None)
        self.mark_dirty()

    def clear_chain_state(self, session_id: str | None) -> None:
        """End a chain: drop its queue, deferred items, phases and entry SHA.

        One call for every terminal exit, so a failure exit cannot leave a
        field behind that only the success exit remembered to clear.
        """
        self.clear_autopilot_chain(session_id)
        self.clear_chain_deferred(session_id)
        self.clear_chain_phases(session_id)
        self.clear_chain_entry_sha(session_id)

    # --- Chain Entry SHA (verify_release diff anchor) ---

    def get_chain_entry_sha(self, session_id: str | None) -> str | None:
        """Return the HEAD SHA snapshotted at the chain `done` step, or None."""
        if not session_id:
            return None
        return self._chain_entry_sha.get(session_id)

    def set_chain_entry_sha(self, session_id: str | None, sha: str | None) -> None:
        """Persist the HEAD SHA so verify_release survives reboots."""
        if not session_id or not sha:
            return
        self._chain_entry_sha[session_id] = sha
        self.save()

    def clear_chain_entry_sha(self, session_id: str | None) -> None:
        """Remove the chain entry SHA when the chain ends or is abandoned."""
        if not session_id:
            return
        if self._chain_entry_sha.pop(session_id, None) is not None:
            self.save()

    # --- Chain Phase State (multi-phase build chains) ---

    def get_chain_phases(self, session_id: str | None) -> ChainPhaseState | None:
        """Load phase state for an autopilot chain, or None if not phased."""
        if not session_id:
            return None
        d = self._chain_phases.get(session_id)
        if not d:
            return None
        try:
            return ChainPhaseState.from_dict(d)
        except Exception:
            log.exception("Failed to deserialize chain_phases for %s", session_id)
            return None

    def set_chain_phases(self, session_id: str | None, state: ChainPhaseState) -> None:
        """Persist the full phase state for an autopilot chain."""
        if not session_id:
            return
        self._chain_phases[session_id] = state.to_dict()
        self.save()

    def clear_chain_phases(self, session_id: str | None) -> None:
        """Remove phase state for an autopilot chain."""
        if not session_id:
            return
        if self._chain_phases.pop(session_id, None) is not None:
            self.save()

    def advance_chain_phase(self, session_id: str | None) -> ChainPhaseState | None:
        """Atomically advance the cursor and clear pre-phase fields.

        Bumps cursor by one and clears `paused_at` + `pre_phase_head` in a
        single save so a reboot between writes can never observe a partial
        transition. Returns the updated state, or None if no phases tracked.
        """
        if not session_id:
            return None
        state = self.get_chain_phases(session_id)
        if state is None:
            return None
        state.cursor += 1
        state.paused_at = None
        state.pre_phase_head = None
        self._chain_phases[session_id] = state.to_dict()
        self.save()
        return state

    def set_phase_pause(self, session_id: str | None, where: str | None) -> None:
        """Mark a phase as paused at 'pre' or 'post' (or None to clear)."""
        if not session_id:
            return
        state = self.get_chain_phases(session_id)
        if state is None:
            return
        state.paused_at = where
        self._chain_phases[session_id] = state.to_dict()
        self.save()

    def set_pre_phase_head(self, session_id: str | None, sha: str | None) -> None:
        """Record the git HEAD captured before the current phase spawned."""
        if not session_id:
            return
        state = self.get_chain_phases(session_id)
        if state is None:
            return
        state.pre_phase_head = sha
        self._chain_phases[session_id] = state.to_dict()
        self.save()

    def set_phase_spawn_metadata(
        self,
        session_id: str | None,
        worktree_path: str | None,
        first_build_id: str | None,
    ) -> None:
        """Atomically record both worktree_path and first_build_id.

        These two fields must move together: a crash with one set but not the
        other would make the resume's `is_first` check disagree with reality
        (e.g. first_build_id=None + worktree_path=set would re-trigger
        auto_branch over an existing worktree).
        """
        if not session_id:
            return
        state = self.get_chain_phases(session_id)
        if state is None:
            return
        state.worktree_path = worktree_path
        state.first_build_id = first_build_id
        self._chain_phases[session_id] = state.to_dict()
        self.save()
