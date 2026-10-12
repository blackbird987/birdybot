"""Instance metadata store: persistence, instances, costs, settings and queries.

The other domains are mixins: repos, accounts, schedules and chains.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.textutil import parse_iso_utc
from bot import paths
from bot.claude.types import (
    Instance,
    InstanceStatus,
    InstanceType,
    Schedule,
    Watch,
)
from bot.store.accounts import AccountsMixin
from bot.store.chains import ChainsMixin
from bot.store.records import AutoFixState, DeployState
from bot.store.repos import ReposMixin
from bot.store.schedules import SchedulesMixin

log = logging.getLogger(__name__)

# Absolute paths recorded per instance that are later OPENED — the working
# tree, the build worktree, the account dir that owns the session, and the two
# artefact files behind /log and /diff (also read back when a forum thread
# rebuilds its history). Untranslated, those two are the quiet failure: the
# instance list renders fine and the button just returns nothing.
#
# `prompt` and `path_poisoning` also contain absolute paths
# and are deliberately left alone — they are a record of what was said and
# done, not a handle to open, and rewriting them would falsify history rather
# than repair it.
_INSTANCE_PATH_FIELDS = (
    "repo_path", "worktree_path", "session_account", "result_file", "diff_file",
)
# Maps whose KEYS are account directories.
_ACCOUNT_KEYED = (
    "account_cooldowns", "account_cooldown_fps", "model_cooldowns", "account_alerts",
)


def _localise_paths(data: dict) -> None:
    """Rewrite stored absolute paths into this machine's spelling, in place.

    The state file is shared verbatim between machines that reach the same
    directories under different names (a mounted Windows drive read from
    Linux, and the same drive read from Windows). Every path in it is correct
    for whichever machine wrote it last, so it is translated on the way in
    rather than migrated on disk — that keeps both machines writing freely and
    means neither has to run a conversion before it can start.

    Unknown paths pass through untouched, so a machine with no path map behaves
    exactly as it did before this existed.
    """
    repos = data.get("repos")
    if isinstance(repos, dict):
        for name, path in list(repos.items()):
            if isinstance(path, str):
                repos[name] = paths.translate(path)

    # The blurb cache records the path it was derived from and re-derives when
    # it differs. Untranslated, the other machine's spelling never matches and
    # every refresh on this machine re-reads the files the cache exists to skip.
    descs = data.get("repo_descriptions")
    if isinstance(descs, dict):
        for entry in descs.values():
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                entry["path"] = paths.translate(entry["path"])

    for inst in data.get("instances") or []:
        if not isinstance(inst, dict):
            continue
        for field in _INSTANCE_PATH_FIELDS:
            value = inst.get(field)
            if isinstance(value, str) and value:
                inst[field] = paths.translate(value)

    # A schedule fires unattended, so a repo path spelled for the other machine
    # fails with nobody watching.
    for sched in data.get("schedules") or []:
        if isinstance(sched, dict) and isinstance(sched.get("repo_path"), str):
            sched["repo_path"] = paths.translate(sched["repo_path"])

    # Cooldowns and outage records are keyed by account directory. Left
    # untranslated, a cooldown set under the other machine's spelling is
    # invisible here — the account reads as free, gets spawned, and fails
    # again on the same limit.
    for key in _ACCOUNT_KEYED:
        table = data.get(key)
        if not isinstance(table, dict):
            continue
        merged: dict = {}
        for k, v in table.items():
            local = paths.translate(k) if isinstance(k, str) else k
            if local in merged and merged[local] != v:
                # Two spellings of one account carrying different records.
                # Normal saves can't produce this (the whole table is rewritten
                # in the local spelling every time), so it means a hand-edited
                # file — worth a line in the log rather than a value that
                # vanishes without explanation.
                log.warning(
                    "%s: %r and %r are the same account with different records "
                    "— keeping the first", key, k, local,
                )
                continue
            merged[local] = v
        data[key] = merged


class StateStore(ReposMixin, AccountsMixin, SchedulesMixin, ChainsMixin):
    """In-memory state with atomic JSON persistence."""

    def __init__(self, state_file: Path, results_dir: Path, retention_days: int = 7,
                 max_retained: int = 250):
        self._file = state_file
        self._results_dir = results_dir
        self._retention_days = retention_days
        self._max_retained = max_retained

        self._instances: dict[str, Instance] = {}
        self._repos: dict[str, str] = {}       # name -> path
        # name -> {"text", "source", "sig", "mtime", "path"} — the derived
        # Control Room blurb, cached so a refresh is stat-only.
        # See bot/engine/repo_desc.py.
        self._repo_descriptions: dict[str, dict] = {}
        # Repos hidden from Discord's sidebar and every picker. Display-only:
        # a dormant repo stays registered and its path stays resolvable, so
        # old threads still resume. See list_active_repos().
        self._dormant_repos: set[str] = set()
        self._active_repo: str | None = None
        self._task_counter: int = 0
        self._query_counter: int = 0
        self._schedule_counter: int = 0
        self._daily_cost: float = 0.0
        self._cost_date: str = ""               # YYYY-MM-DD
        self._total_cost: float = 0.0
        self._mode: str = "explore"
        self._context: str | None = None
        self._aliases: dict[str, str] = {}      # name -> prompt
        self._schedules: dict[str, Schedule] = {}
        # Event-triggered self-wakes (see bot/engine/watches.py). Kept apart
        # from _schedules because they are polled on a condition, not a
        # clock — a watch only becomes a Schedule at the moment it trips.
        self._watches: dict[str, Watch] = {}
        self._watch_counter: int = 0
        self._active_session_id: str | None = None  # Current conversation session
        self._verbose_level: int = 1  # 0=silent, 1=normal, 2=detailed
        self._effort: str = "high"  # reasoning effort: low/medium/high/max
        self._platform_state: dict[str, dict] = {}  # platform -> arbitrary state
        self._autopilot_chains: dict[str, list[str]] = {}  # session_id -> remaining steps
        self._autopilot_chain_meta: dict[str, dict] = {}  # session_id -> {status, updated_at}
        # Chain entry-point kwargs (label + silent_close), persisted so a
        # resumed chain (Continue button or reboot recovery) restores the
        # originating identity rather than defaulting back to "Autopilot".
        self._chain_kwargs: dict[str, dict] = {}  # session_id -> {label, silent_close}
        self._pending_merges: dict[str, dict] = {}  # instance_id -> pending merge metadata
        # Auto-merge veto window: instance_id -> {channel_id, session_id,
        # repo_name, branch, merge_at, held, created_at}. Distinct from
        # _pending_merges (which is FAILED-merge recovery) — this is a
        # SUCCESS-path countdown: a completed build branch scheduled to
        # auto-merge unless the user taps Hold before merge_at.
        self._scheduled_merges: dict[str, dict] = {}
        # Conversational-chain plan handoff: session_id -> plan text the agent
        # wrote from the conversation and handed to a /chain directive. Read as
        # the highest-priority source by _extract_latest_plan_text; cleared when
        # the chain finishes (queue empties).
        self._chain_plan_overrides: dict[str, str] = {}
        self._chain_deferred: dict[str, list[str]] = {}  # session_id -> deferred revisions
        self._chain_phases: dict[str, dict] = {}  # session_id -> ChainPhaseState dict
        self._chain_entry_sha: dict[str, str] = {}  # session_id -> HEAD sha snapshot
        self._deploy_state: dict[str, DeployState] = {}  # repo_name -> deploy state
        self._deploy_configs: dict[str, dict] = {}  # repo_name -> deploy config
        self._auto_fix_state: dict[str, AutoFixState] = {}  # "repo:trigger" -> state
        self._active_provider: str | None = None  # Runtime provider override
        self._fallback_cost: float = 0.0     # Rolling daily API fallback spend
        self._fallback_cost_date: str = ""   # YYYY-MM-DD for fallback cost reset
        # account_dir -> ISO timestamp of usage-limit reset.  Persisted across
        # reboots so a build triggered immediately after restart doesn't waste
        # a CLI invocation on a known-cooldown account (the t-3452 dementia).
        self._account_cooldowns: dict[str, str] = {}
        # account_dir -> credentials fingerprint captured when that cooldown was
        # recorded.  A usage limit belongs to the *account*, but the cooldown is
        # keyed by *directory* — and a `/login` can swap which account lives in a
        # directory (2026-08-14: a new subscription was signed into ~/.claude,
        # inheriting the old account's weekly-limit cooldown and refusing every
        # spawn on a subscription with full quota).  Comparing fingerprints at
        # load makes that recoverable without an .env edit.
        self._account_cooldown_fps: dict[str, str] = {}
        # account_dir -> ISO timestamp of the PRIMARY model's own limit reset
        # (e.g. Fable 5 quota).  Separate from _account_cooldowns: the account
        # stays usable for other models, so this only downgrades — never
        # refuses spawns.
        self._model_cooldowns: dict[str, str] = {}
        # account_dir -> {"reason": str, "since": ISO, "notified": bool,
        #                 "snooze_until": ISO|None, "resolved": bool,
        #                 "cred_fp": str|None}.  Tracks accounts sidelined for
        # auth (logged out / OAuth dead) so the Ark notice fires exactly once
        # per outage instead of once per failed task, and survives reboot.
        # "resolved" marks an outage that's over but still owes an all-clear
        # post; "cred_fp" fingerprints the credentials file the verdict was
        # reached against, so a later login is recognisable across a restart.
        self._account_alerts: dict[str, dict] = {}
        self._dirty: bool = False  # Dirty flag — mark_dirty() defers save to auto-save loop
        self._last_mtime: float = 0.0  # Track file mtime for external change detection

        self._load()
        self._update_mtime()

    # --- Persistence ---

    def _load(self) -> None:
        if not self._file.exists():
            log.info("No state file found, starting fresh")
            return
        try:
            self._apply(json.loads(self._file.read_text(encoding="utf-8")))
        except Exception:
            log.exception("Failed to load state file, starting fresh")

    def _apply(self, data: dict) -> None:
        """Load a parsed state file into this store. Raises on a bad payload.

        Fills the record maps (instances, schedules, watches) rather than
        replacing them, so ``reload_if_changed`` empties them first.
        """
        # Deliberately isolated from the load below.  Everything after this
        # point raises to a caller whose recovery at startup is "start fresh",
        # which for this file means every repo, instance and thread mapping is
        # dropped and the next auto-save overwrites the real one.  A path
        # rewrite failing is a degradation (paths stay in the other
        # machine's spelling); it must never escalate to losing the state.
        try:
            _localise_paths(data)
        except Exception:
            log.exception("Path localisation failed, loading paths as stored")
        for d in data.get("instances", []):
            inst = Instance.from_dict(d)
            self._instances[inst.id] = inst
        self._repos = data.get("repos", {})
        self._repo_descriptions = data.get("repo_descriptions", {})
        self._dormant_repos = set(data.get("dormant_repos", []) or [])
        self._active_repo = data.get("active_repo")
        self._task_counter = data.get("task_counter", 0)
        self._query_counter = data.get("query_counter", 0)
        self._schedule_counter = data.get("schedule_counter", 0)
        self._daily_cost = data.get("daily_cost", 0.0)
        self._cost_date = data.get("cost_date", "")
        self._total_cost = data.get("total_cost", 0.0)
        self._mode = data.get("mode", "explore")
        self._context = data.get("context")
        self._aliases = data.get("aliases", {})
        self._active_session_id = data.get("active_session_id")
        self._verbose_level = data.get("verbose_level", 1)
        self._effort = data.get("effort", "high")
        self._platform_state = data.get("platform_state", {})
        self._autopilot_chains = data.get("autopilot_chains", {})
        self._autopilot_chain_meta = data.get("autopilot_chain_meta", {})
        self._chain_kwargs = data.get("chain_kwargs", {})
        # Migration: pre-meta chain queues have no status record. Drop them
        # rather than guess: a stale unmetered chain that resumes after
        # reboot is exactly the bug this field exists to prevent.
        unmetered = [
            sid for sid in list(self._autopilot_chains.keys())
            if sid not in self._autopilot_chain_meta
        ]
        for sid in unmetered:
            log.warning(
                "Dropping unmetered autopilot chain on load: session=%s steps=%s",
                sid, self._autopilot_chains.get(sid),
            )
            self._autopilot_chains.pop(sid, None)
        if unmetered:
            self.mark_dirty()
        self._pending_merges = data.get("pending_merges", {})
        self._scheduled_merges = data.get("scheduled_merges", {})
        self._chain_plan_overrides = data.get("chain_plan_overrides", {})
        self._chain_deferred = data.get("chain_deferred", {})
        self._chain_phases = data.get("chain_phases", {})
        self._chain_entry_sha = data.get("chain_entry_sha", {})
        self._deploy_state = {
            k: DeployState.from_dict(v)
            for k, v in data.get("deploy_state", {}).items()
        }
        self._deploy_configs = data.get("deploy_configs", {})
        self._auto_fix_state = {
            k: AutoFixState.from_dict(v)
            for k, v in data.get("auto_fix_state", {}).items()
        }
        self._active_provider = data.get("active_provider")
        self._fallback_cost = data.get("fallback_cost", 0.0)
        self._fallback_cost_date = data.get("fallback_cost_date", "")
        self._account_cooldowns = data.get("account_cooldowns", {})
        self._account_cooldown_fps = data.get("account_cooldown_fps", {})
        self._model_cooldowns = data.get("model_cooldowns", {})
        self._account_alerts = data.get("account_alerts", {})
        for d in data.get("schedules", []):
            sched = Schedule.from_dict(d)
            self._schedules[sched.id] = sched
        self._watch_counter = data.get("watch_counter", 0)
        for d in data.get("watches", []):
            try:
                w = Watch.from_dict(d)
            except Exception:
                log.warning("Dropping malformed watch record", exc_info=True)
                continue
            self._watches[w.id] = w
        log.info("Loaded state: %d instances, %d repos, %d schedules",
                 len(self._instances), len(self._repos), len(self._schedules))

    def _update_mtime(self) -> None:
        """Record current file mtime after we read or write."""
        try:
            self._last_mtime = self._file.stat().st_mtime if self._file.exists() else 0.0
        except OSError:
            self._last_mtime = 0.0

    def reload_if_changed(self) -> bool:
        """Re-read state from disk if the file was modified externally.
        Returns True if a reload occurred."""
        try:
            current_mtime = self._file.stat().st_mtime if self._file.exists() else 0.0
        except OSError:
            return False
        if current_mtime <= self._last_mtime:
            return False
        # Unsaved changes win: this process owns the file, and the next
        # auto-save overwrites the outside edit either way. Reloading over
        # them would silently drop records created since the last save.
        if self._dirty:
            log.warning("State file changed externally with unsaved changes "
                        "pending; keeping memory, the next save overwrites it")
            self._last_mtime = current_mtime
            return False
        # Parse before touching anything: a half-written or corrupt file must
        # leave memory alone, or the next save would write the damage back.
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except Exception:
            log.exception("State file changed externally but will not parse; "
                          "keeping memory")
            self._last_mtime = current_mtime
            return False
        log.info("State file changed externally, reloading")
        # Replace the record maps rather than merging into them, or a record
        # deleted on disk comes back from memory. Restored whole on failure.
        saved = (self._instances, self._schedules, self._watches)
        self._instances, self._schedules, self._watches = {}, {}, {}
        try:
            self._apply(data)
        except Exception:
            log.exception("State reload failed; keeping the previous records")
            self._instances, self._schedules, self._watches = saved
        self._last_mtime = current_mtime
        return True

    def mark_dirty(self) -> None:
        """Mark state as changed — actual write deferred to auto-save loop."""
        self._dirty = True

    def save_if_dirty(self, backup: bool = False) -> None:
        """Write to disk only if state has changed since last save."""
        if self._dirty:
            self.save(backup=backup)

    def _serialize(self) -> str:
        """Build a JSON snapshot of all state. Compact separators — the file is
        machine-read, so pretty-printing several MB is pure CPU waste (cuts
        serialize time ~4x vs indent=2)."""
        data = {
            "instances": [i.to_dict() for i in self._instances.values()],
            "repos": self._repos,
            "repo_descriptions": self._repo_descriptions,
            "dormant_repos": sorted(self._dormant_repos),
            "active_repo": self._active_repo,
            "task_counter": self._task_counter,
            "query_counter": self._query_counter,
            "schedule_counter": self._schedule_counter,
            "daily_cost": self._daily_cost,
            "cost_date": self._cost_date,
            "total_cost": self._total_cost,
            "mode": self._mode,
            "context": self._context,
            "aliases": self._aliases,
            "active_session_id": self._active_session_id,
            "verbose_level": self._verbose_level,
            "effort": self._effort,
            "platform_state": self._platform_state,
            "autopilot_chains": self._autopilot_chains,
            "autopilot_chain_meta": self._autopilot_chain_meta,
            "chain_kwargs": self._chain_kwargs,
            "pending_merges": self._pending_merges,
            "scheduled_merges": self._scheduled_merges,
            "chain_plan_overrides": self._chain_plan_overrides,
            "chain_deferred": self._chain_deferred,
            "chain_phases": self._chain_phases,
            "chain_entry_sha": self._chain_entry_sha,
            "deploy_state": {k: v.to_dict() for k, v in self._deploy_state.items()},
            "deploy_configs": self._deploy_configs,
            "auto_fix_state": {k: v.to_dict() for k, v in self._auto_fix_state.items()},
            "active_provider": self._active_provider,
            "fallback_cost": self._fallback_cost,
            "fallback_cost_date": self._fallback_cost_date,
            "account_cooldowns": self._account_cooldowns,
            "account_cooldown_fps": self._account_cooldown_fps,
            "model_cooldowns": self._model_cooldowns,
            "account_alerts": self._account_alerts,
            "schedules": [s.to_dict() for s in self._schedules.values()],
            "watches": [w.to_dict() for w in self._watches.values()],
            "watch_counter": self._watch_counter,
        }
        return json.dumps(data, separators=(",", ":"))

    def _write_payload(self, payload: str, backup: bool) -> None:
        """Atomically write a pre-serialized snapshot to disk (temp + replace)."""
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._file.parent), suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                # Refresh the last-known-good backup only when asked (periodic).
                if backup and self._file.exists():
                    self._rotate_backup()
                os.replace(tmp_path, str(self._file))  # atomic overwrite
                self._update_mtime()
            except Exception:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        except Exception:
            log.exception("Failed to save state")

    def _rotate_backup(self) -> None:
        """Make the current file the .bak without copying it (best effort).

        A hard link names the same bytes twice, so the replace that follows
        repoints state.json at the new snapshot and leaves .bak holding the
        old one: no 4 MB copy, and state.json never stops existing in between,
        which a rename of the live file would cost. A filesystem without hard
        links falls back to the copy.
        """
        bak = self._file.with_suffix(".bak")
        tmp_bak = bak.with_name(bak.name + ".tmp")
        try:
            tmp_bak.unlink(missing_ok=True)
            os.link(self._file, tmp_bak)
            os.replace(tmp_bak, bak)
            return
        except OSError:
            tmp_bak.unlink(missing_ok=True)
        try:
            shutil.copy2(str(self._file), str(bak))
        except Exception:
            pass  # best-effort backup

    def save(self, backup: bool = False) -> None:
        """Atomic save: serialize then write via temp + replace. Cheap enough to
        stay on the event loop now that state.json is pruned and JSON is compact
        (~12ms, no per-call 4 MB backup copy); pass ``backup=True`` to also
        refresh the periodic .bak."""
        self._dirty = False
        self._write_payload(self._serialize(), backup)

    # --- Instance Management ---

    def create_instance(
        self,
        instance_type: InstanceType,
        prompt: str,
        name: str | None = None,
        mode: str | None = None,
        schedule_id: str | None = None,
    ) -> Instance:
        if instance_type == InstanceType.TASK:
            self._task_counter += 1
            iid = f"t-{self._task_counter:03d}"
        elif instance_type == InstanceType.SCHEDULED:
            self._task_counter += 1
            iid = f"s-{self._task_counter:03d}"
        else:
            self._query_counter += 1
            iid = f"q-{self._query_counter:03d}"

        repo_name = self._active_repo or ""
        repo_path = self._repos.get(repo_name, "") if repo_name else ""

        inst = Instance(
            id=iid,
            name=name,
            instance_type=instance_type,
            prompt=prompt,
            repo_name=repo_name,
            repo_path=repo_path,
            status=InstanceStatus.QUEUED,
            mode=mode or self._mode,
            created_at=datetime.now(timezone.utc).isoformat(),
            schedule_id=schedule_id,
        )
        self._instances[iid] = inst
        self.save()
        return inst

    def clone_instance_for_rerun(
        self,
        inst: Instance,
        *,
        name_suffix: str | None = None,
        origin_platform: str,
        effort: str,
        model: str | None,
    ) -> Instance:
        """Clone *inst* into a fresh QUEUED instance for a re-run.

        Shared by /retry and the Retry button, the cooldown auto-retry, and
        continue-on-pay-per-use. Each caller still sets what it alone owns
        (``api_fallback``, ``cooldown_retries``, a prompt suffix), still calls
        ``update_instance``, and still backfills the thread's session itself:
        see "A thread must always know its session".

        ``model`` is a parameter, not a copy. Both retry paths pass
        ``inst.model`` for faithful replay, so a re-run build stays on
        BUILD_MODEL. Pay-per-use passes None deliberately: the provider
        ignores it under ``api_fallback``, and a later plain retry must not
        inherit a model the pay-per-use turn never ran on.
        """
        new_inst = self.create_instance(
            instance_type=inst.instance_type,
            prompt=inst.prompt,
            name=f"{inst.name}-{name_suffix}" if inst.name and name_suffix else None,
            mode=inst.mode,
        )
        new_inst.origin = inst.origin
        new_inst.model = model
        new_inst.origin_platform = origin_platform
        new_inst.effort = effort
        new_inst.parent_id = inst.id
        new_inst.repo_name = inst.repo_name
        new_inst.repo_path = inst.repo_path
        if inst.session_id:
            new_inst.session_id = inst.session_id
        if inst.branch:
            new_inst.branch = inst.branch
            new_inst.original_branch = inst.original_branch
            new_inst.worktree_path = inst.worktree_path
        return new_inst

    def get_instance(self, id_or_name: str) -> Instance | None:
        # Try by ID first
        inst = self._instances.get(id_or_name)
        if inst:
            return inst
        # Try by name
        return self.find_by_name(id_or_name)

    def update_instance(self, inst: Instance, *, critical: bool = False) -> None:
        self._instances[inst.id] = inst
        if critical:
            self.save()
        else:
            self.mark_dirty()

    def find_by_name(self, name: str) -> Instance | None:
        name_lower = name.lower()
        for inst in self._instances.values():
            if inst.name and inst.name.lower() == name_lower:
                return inst
        return None

    def list_instances(self, all_: bool = False) -> list[Instance]:
        """Return instances, most recent first. Default: last 24h only."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=24)
        result = []
        for inst in self._instances.values():
            if all_:
                result.append(inst)
            else:
                created = parse_iso_utc(inst.created_at)
                if created is None or created > cutoff:
                    result.append(inst)
        result.sort(key=lambda i: i.created_at, reverse=True)
        return result

    def latest_instance_for_session(self, session_id: str | None) -> Instance | None:
        """Newest instance belonging to a session, or None.

        Scanning by session_id is how the codebase resolves thread -> instance
        (tags.py, forums.py, eval.py, the spawn-wave join): there is no index,
        and this is the one implementation of that scan so a caller cannot
        drift onto "first match" and pick a stale turn.
        """
        if not session_id:
            return None
        best: Instance | None = None
        for inst in self._instances.values():
            if inst.session_id and inst.session_id == session_id:
                if best is None or (inst.created_at or "") > (best.created_at or ""):
                    best = inst
        return best

    def instance_count(self) -> int:
        """Total number of instances (all time)."""
        return len(self._instances)

    def mark_orphans(self) -> list["Instance"]:
        """Mark running/queued instances as failed (for startup recovery).

        Returns the list of orphaned instances so callers can update their
        thinking messages after platform connections are established.
        """
        orphans: list["Instance"] = []
        for inst in self._instances.values():
            if inst.status in (InstanceStatus.RUNNING, InstanceStatus.QUEUED):
                inst.status = InstanceStatus.FAILED
                inst.error = "Bot restarted, instance interrupted"
                inst.finished_at = datetime.now(timezone.utc).isoformat()
                orphans.append(inst)
        if orphans:
            self.save()
        return orphans

    def archive_old(self) -> int:
        """Prune terminal instances: first by age (older than retention), then
        enforce a hard cap so only the most-recent N survive. The cap bounds
        state.json size regardless of churn — without it, a busy week leaves
        hundreds of instances and every save stalls the event loop."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self._retention_days)
        terminal = (InstanceStatus.COMPLETED, InstanceStatus.FAILED,
                    InstanceStatus.KILLED)
        to_remove = []
        for inst in self._instances.values():
            if inst.status in terminal:
                created = parse_iso_utc(inst.created_at)
                if created is not None and created < cutoff:
                    to_remove.append(inst)
        # Hard cap: of the terminal instances that survived the age pass, keep
        # only the most-recent _max_retained (by created_at); drop the oldest.
        removing = {i.id for i in to_remove}
        survivors = [i for i in self._instances.values()
                     if i.status in terminal and i.id not in removing]
        if len(survivors) > self._max_retained:
            survivors.sort(key=lambda i: i.created_at or "")  # oldest first
            to_remove.extend(survivors[: len(survivors) - self._max_retained])
        for inst in to_remove:
            # Clean up result files
            for fpath in (inst.result_file, inst.diff_file):
                if fpath:
                    Path(fpath).unlink(missing_ok=True)
            del self._instances[inst.id]
        if to_remove:
            self.save()
        return len(to_remove)

    # --- Cost Tracking ---

    def add_cost(self, amount: float) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._cost_date != today:
            self._daily_cost = 0.0
            self._cost_date = today
        self._daily_cost += amount
        self._total_cost += amount
        self.save()

    def get_daily_cost(self) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._cost_date != today:
            return 0.0
        return self._daily_cost

    def get_repo_daily_cost(self, repo_name: str) -> float:
        """Sum today's costs for instances of a specific repo.

        Uses instance-level cost fields filtered to today, not the global
        accumulator (which can't be split by repo).
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total = 0.0
        for inst in self.list_by_repo(repo_name):
            if inst.cost_usd and inst.created_at:
                try:
                    if inst.created_at[:10] == today:
                        total += inst.cost_usd
                except (ValueError, TypeError):
                    pass
        return total

    def reset_daily_budget(self) -> None:
        self._daily_cost = 0.0
        self._cost_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.save()

    def add_fallback_cost(self, amount: float) -> None:
        """Track API fallback (pay-per-use) spending separately."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._fallback_cost_date != today:
            self._fallback_cost = 0.0
            self._fallback_cost_date = today
        self._fallback_cost += amount
        self.save()

    def get_fallback_spend_today(self) -> float:
        """Return total API fallback spend in the last 24h."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._fallback_cost_date != today:
            return 0.0
        return self._fallback_cost

    # --- Mode ---

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        self._mode = value
        self.save()

    # --- Context ---

    @property
    def context(self) -> str | None:
        return self._context

    @context.setter
    def context(self, value: str | None) -> None:
        self._context = value
        self.save()

    # --- Active Session ---

    @property
    def active_session_id(self) -> str | None:
        return self._active_session_id

    @active_session_id.setter
    def active_session_id(self, value: str | None) -> None:
        self._active_session_id = value
        self.save()

    # --- Verbose Level ---

    @property
    def verbose_level(self) -> int:
        return self._verbose_level

    @verbose_level.setter
    def verbose_level(self, value: int) -> None:
        self._verbose_level = max(0, min(2, value))
        self.save()

    # --- Effort ---

    @property
    def effort(self) -> str:
        return self._effort

    @effort.setter
    def effort(self, value: str) -> None:
        self._effort = value
        self.save()

    # --- Active Provider ---

    @property
    def active_provider(self) -> str | None:
        return self._active_provider

    @active_provider.setter
    def active_provider(self, value: str | None) -> None:
        self._active_provider = value
        self.mark_dirty()

    # --- Platform State ---

    def get_platform_state(self, platform: str) -> dict:
        return self._platform_state.get(platform, {})

    def set_platform_state(self, platform: str, data: dict, *, persist: bool = True) -> None:
        self._platform_state[platform] = data
        if persist:
            self.save()

    # --- Deploy State ---

    def get_deploy_state(self, repo_name: str) -> DeployState | None:
        return self._deploy_state.get(repo_name)

    def set_deploy_state(self, repo_name: str, state: DeployState) -> None:
        self._deploy_state[repo_name] = state
        self.mark_dirty()

    # --- Deploy Config ---

    def get_deploy_config(self, repo_name: str) -> dict | None:
        return self._deploy_configs.get(repo_name)

    def set_deploy_config(self, repo_name: str, config: dict) -> None:
        self._deploy_configs[repo_name] = config
        self.mark_dirty()

    def remove_deploy_config(self, repo_name: str) -> None:
        self._deploy_configs.pop(repo_name, None)
        self.mark_dirty()

    # --- Auto-Fix State ---

    def get_auto_fix_state(self, repo_name: str, trigger: str) -> AutoFixState:
        key = f"{repo_name}:{trigger}"
        return self._auto_fix_state.get(key, AutoFixState())

    def set_auto_fix_state(self, repo_name: str, trigger: str, state: AutoFixState) -> None:
        key = f"{repo_name}:{trigger}"
        self._auto_fix_state[key] = state
        self.mark_dirty()

    # --- Aliases ---

    def set_alias(self, name: str, prompt: str) -> None:
        self._aliases[name] = prompt
        self.save()

    def get_alias(self, name: str) -> str | None:
        return self._aliases.get(name)

    def delete_alias(self, name: str) -> bool:
        if name in self._aliases:
            del self._aliases[name]
            self.save()
            return True
        return False

    def list_aliases(self) -> dict[str, str]:
        return dict(self._aliases)

    # --- Stats ---

    def instance_count_today(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return sum(1 for i in self._instances.values()
                   if i.created_at.startswith(today))

    def failure_count_today(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return sum(1 for i in self._instances.values()
                   if i.status == InstanceStatus.FAILED
                   and i.created_at.startswith(today))

    def running_count(self) -> int:
        return sum(1 for i in self._instances.values()
                   if i.status == InstanceStatus.RUNNING)

    # --- Query Helpers ---

    def list_by_repo(self, repo_name: str) -> list[Instance]:
        """Return recent instances for a specific repo, newest first."""
        return [i for i in self.list_instances() if i.repo_name == repo_name]

    def list_by_status(self, *statuses: InstanceStatus) -> list[Instance]:
        """Return recent instances matching any of the given statuses."""
        status_set = set(statuses)
        return [i for i in self.list_instances() if i.status in status_set]

    def needs_attention(self, recent_fail_minutes: int = 30) -> list[Instance]:
        """Return instances actively waiting on the user, newest first.

        Includes any ``needs_input`` instance (Claude is paused on a question)
        and FAILED instances whose ``finished_at`` falls within the last
        ``recent_fail_minutes``. Stale failures fall through to
        :meth:`recent_failures` so the dashboard doesn't accumulate them.

        Inherits the 24h window from :meth:`list_instances`: a session the
        user hasn't touched in over a day is treated as abandoned and will
        not reappear here even if it was paused on a question.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=recent_fail_minutes)
        ).isoformat()
        return [
            i for i in self.list_instances()
            if i.needs_input
            or (
                i.status == InstanceStatus.FAILED
                and i.finished_at and i.finished_at >= cutoff
            )
        ]

    def idle_sessions(self, max_age_hours: int = 2) -> list[Instance]:
        """Return recently completed instances with sessions not currently running.

        These represent threads the user can tap to resume — idle but alive.
        The *max_age_hours* window keeps the list fresh; older completed
        sessions are presumed done with.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        running_sessions = {
            i.session_id for i in self._instances.values()
            if i.status == InstanceStatus.RUNNING and i.session_id
        }
        seen: set[str] = set()
        idle: list[Instance] = []
        for i in self.list_instances():
            if (i.session_id
                    and i.session_id not in running_sessions
                    and i.session_id not in seen
                    and i.status == InstanceStatus.COMPLETED
                    and not i.needs_input
                    and i.finished_at and i.finished_at >= cutoff):
                seen.add(i.session_id)
                idle.append(i)
        return idle

    def recent_failures(self, hours: int = 6) -> list[Instance]:
        """Return instances that failed in the last *hours* hours."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return [
            i for i in self.list_instances()
            if i.status == InstanceStatus.FAILED
            and i.finished_at and i.finished_at >= cutoff
        ]

    def last_activity(self) -> Instance | None:
        """Return the most recently created instance."""
        instances = self.list_instances()
        return instances[0] if instances else None
