"""Instance metadata store with repo registry, cost tracking, aliases, schedules."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.claude.types import Instance, InstanceStatus, InstanceType, Schedule
from bot.engine.auto_fix import AutoFixState
from bot.engine.deploy import DeployState

log = logging.getLogger(__name__)


class StateStore:
    """In-memory state with atomic JSON persistence."""

    def __init__(self, state_file: Path, results_dir: Path, retention_days: int = 7):
        self._file = state_file
        self._results_dir = results_dir
        self._retention_days = retention_days

        self._instances: dict[str, Instance] = {}
        self._repos: dict[str, str] = {}       # name -> path
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
        self._active_session_id: str | None = None  # Current conversation session
        self._verbose_level: int = 1  # 0=silent, 1=normal, 2=detailed
        self._effort: str = "high"  # reasoning effort: low/medium/high/max
        self._platform_state: dict[str, dict] = {}  # platform -> arbitrary state
        self._autopilot_chains: dict[str, list[str]] = {}  # session_id -> remaining steps
        self._chain_deferred: dict[str, list[str]] = {}  # session_id -> deferred revisions
        self._deploy_state: dict[str, DeployState] = {}  # repo_name -> deploy state
        self._deploy_configs: dict[str, dict] = {}  # repo_name -> deploy config
        self._auto_fix_state: dict[str, AutoFixState] = {}  # "repo:trigger" -> state
        self._active_provider: str | None = None  # Runtime provider override
        self._fallback_cost: float = 0.0     # Rolling daily API fallback spend
        self._fallback_cost_date: str = ""   # YYYY-MM-DD for fallback cost reset
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
            data = json.loads(self._file.read_text(encoding="utf-8"))
            for d in data.get("instances", []):
                inst = Instance.from_dict(d)
                self._instances[inst.id] = inst
            self._repos = data.get("repos", {})
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
            self._chain_deferred = data.get("chain_deferred", {})
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
            for d in data.get("schedules", []):
                sched = Schedule.from_dict(d)
                self._schedules[sched.id] = sched
            log.info("Loaded state: %d instances, %d repos, %d schedules",
                     len(self._instances), len(self._repos), len(self._schedules))
        except Exception:
            log.exception("Failed to load state file, starting fresh")

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
        if current_mtime > self._last_mtime:
            log.info("State file changed externally, reloading")
            self._load()
            self._last_mtime = current_mtime
            return True
        return False

    def mark_dirty(self) -> None:
        """Mark state as changed — actual write deferred to auto-save loop."""
        self._dirty = True

    def save_if_dirty(self) -> None:
        """Write to disk only if state has changed since last save."""
        if self._dirty:
            self.save()

    def save(self) -> None:
        """Atomic save: write to temp then rename."""
        self._dirty = False
        data = {
            "instances": [i.to_dict() for i in self._instances.values()],
            "repos": self._repos,
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
            "chain_deferred": self._chain_deferred,
            "deploy_state": {k: v.to_dict() for k, v in self._deploy_state.items()},
            "deploy_configs": self._deploy_configs,
            "auto_fix_state": {k: v.to_dict() for k, v in self._auto_fix_state.items()},
            "active_provider": self._active_provider,
            "fallback_cost": self._fallback_cost,
            "fallback_cost_date": self._fallback_cost_date,
            "schedules": [s.to_dict() for s in self._schedules.values()],
        }
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._file.parent), suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                # Backup the current (last-known-good) file before replacing
                if self._file.exists():
                    backup = self._file.with_suffix(".bak")
                    try:
                        shutil.copy2(str(self._file), str(backup))
                    except Exception:
                        pass  # best-effort backup
                    self._file.unlink()
                Path(tmp_path).rename(self._file)
                self._update_mtime()
            except Exception:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        except Exception:
            log.exception("Failed to save state")

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

    def find_by_message(self, platform: str, message_id: str) -> Instance | None:
        """Find instance by platform message ID."""
        for inst in self._instances.values():
            if message_id in inst.message_ids.get(platform, []):
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
                try:
                    created = datetime.fromisoformat(inst.created_at)
                    if created > cutoff:
                        result.append(inst)
                except (ValueError, TypeError):
                    result.append(inst)
        result.sort(key=lambda i: i.created_at, reverse=True)
        return result

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
                inst.error = "Bot restarted — instance interrupted"
                inst.finished_at = datetime.now(timezone.utc).isoformat()
                orphans.append(inst)
        if orphans:
            self.save()
        return orphans

    def archive_old(self) -> int:
        """Delete instances older than retention period."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self._retention_days)
        to_remove = []
        for inst in self._instances.values():
            if inst.status in (InstanceStatus.COMPLETED, InstanceStatus.FAILED,
                               InstanceStatus.KILLED):
                try:
                    created = datetime.fromisoformat(inst.created_at)
                    if created < cutoff:
                        to_remove.append(inst)
                except (ValueError, TypeError):
                    pass
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

    def get_total_cost(self) -> float:
        return self._total_cost

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

    def get_top_spenders(self, limit: int = 5) -> list[Instance]:
        """Return top-spending instances today."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        instances = [
            i for i in self._instances.values()
            if i.cost_usd and i.created_at.startswith(today)
        ]
        instances.sort(key=lambda i: i.cost_usd or 0, reverse=True)
        return instances[:limit]

    # --- Repo Registry ---

    def add_repo(self, name: str, path: str) -> None:
        self._repos[name] = path
        if not self._active_repo:
            self._active_repo = name
        self.save()

    def remove_repo(self, name: str) -> bool:
        if name not in self._repos:
            return False
        del self._repos[name]
        if self._active_repo == name:
            self._active_repo = next(iter(self._repos), None)
        # Clean up any persisted deploy status msg IDs for this repo
        discord_state = self._platform_state.get("discord", {})
        pending = discord_state.get("deploy_status_msgs", {})
        if name in pending:
            del pending[name]
            if not pending:
                discord_state.pop("deploy_status_msgs", None)
        self.save()
        return True

    def switch_repo(self, name: str) -> bool:
        if name not in self._repos:
            return False
        self._active_repo = name
        self.save()
        return True

    def get_active_repo(self) -> tuple[str | None, str | None]:
        """Returns (name, path) or (None, None)."""
        if self._active_repo and self._active_repo in self._repos:
            return self._active_repo, self._repos[self._active_repo]
        return None, None

    def list_repos(self) -> dict[str, str]:
        return dict(self._repos)

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

    # --- Schedules ---

    def add_schedule(self, prompt: str, interval_secs: int | None = None,
                     run_at: str | None = None, mode: str = "explore") -> Schedule:
        self._schedule_counter += 1
        sid = f"sch-{self._schedule_counter:03d}"
        repo_name, repo_path = self.get_active_repo()
        sched = Schedule(
            id=sid,
            prompt=prompt,
            repo_name=repo_name or "",
            repo_path=repo_path or "",
            mode=mode,
            interval_secs=interval_secs,
            run_at=run_at,
            is_recurring=interval_secs is not None,
        )
        # Calculate next_run_at
        now = datetime.now(timezone.utc)
        if interval_secs:
            sched.next_run_at = (now + timedelta(seconds=interval_secs)).isoformat()
        elif run_at:
            sched.next_run_at = run_at
        self._schedules[sid] = sched
        self.save()
        return sched

    def get_schedule(self, sid: str) -> Schedule | None:
        return self._schedules.get(sid)

    def delete_schedule(self, sid: str) -> bool:
        if sid in self._schedules:
            del self._schedules[sid]
            self.save()
            return True
        return False

    def list_schedules(self) -> list[Schedule]:
        return [s for s in self._schedules.values() if s.enabled]

    def update_schedule(self, sched: Schedule) -> None:
        self._schedules[sched.id] = sched
        self.save()

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

    def needs_attention(self) -> list[Instance]:
        """Return instances that need user attention (failed + needs_input)."""
        return [
            i for i in self.list_instances()
            if i.status == InstanceStatus.FAILED or i.needs_input
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

    # --- Autopilot Chain State ---

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
        self.mark_dirty()

    def clear_autopilot_chain(self, session_id: str | None) -> None:
        """Remove autopilot chain state for a session."""
        if not session_id:
            return
        self._autopilot_chains.pop(session_id, None)
        self.mark_dirty()

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

    # --- Persistent Per-Repo Deferred Revisions (stored in repo TODO.md) ---

    @staticmethod
    def _dedup_key(item: str) -> str:
        """Normalize a deferred item to a dedup key.

        Strips severity tag, leading dash, lowercases, then uses
        [Tag] + first 40 chars of description as the comparison key.
        """
        import re
        text = re.sub(r'\s*\((Critical|High|Medium|Low)\)\s*$', '', item)
        text = re.sub(r'^-\s*', '', text).strip().lower()
        m = re.match(r'(\[[^\]]+\])\s*(.*)', text)
        if m:
            return f"{m.group(1)} {m.group(2)[:40]}"
        return text[:50]

    def append_deferred(
        self, repo_name: str, items: list[str],
        thread_id: str = "", topic: str = "",
    ) -> None:
        """Append deferred revision items to the repo's TODO.md (deduplicated).

        Uses normalized key matching to prevent the same item from
        accumulating across sessions.
        """
        if not repo_name or not items:
            return
        repo_path = self._repos.get(repo_name)
        if not repo_path or not Path(repo_path).is_dir():
            log.warning("Cannot append deferred items: repo dir missing for %s", repo_name)
            return
        todo_path = Path(repo_path) / "TODO.md"

        if todo_path.exists():
            content = todo_path.read_text(encoding="utf-8")
        else:
            content = "# TODO\n"

        # Deduplicate against existing items using normalized keys
        existing_items = self._parse_deferred_section(content)
        existing_keys = {self._dedup_key(i) for i in existing_items}
        new_items = [
            item for item in items
            if self._dedup_key(item) not in existing_keys
        ]
        if not new_items:
            log.debug("All %d deferred items already tracked for %s", len(items), repo_name)
            return

        content = self._update_deferred_section(content, existing_items + new_items)
        todo_path.write_text(content, encoding="utf-8")
        log.info("Appended %d deferred items to %s TODO.md (skipped %d dupes)",
                 len(new_items), repo_name, len(items) - len(new_items))

    def get_deferred(self, repo_name: str) -> str:
        """Get the deferred revisions section from the repo's TODO.md."""
        items = self.get_deferred_items(repo_name)
        if not items:
            return ""
        return "\n".join(f"- {item}" for item in items)

    def get_deferred_items(self, repo_name: str) -> list[str]:
        """Get deferred revision items from the repo's TODO.md."""
        if not repo_name:
            return []
        repo_path = self._repos.get(repo_name)
        if not repo_path:
            return []
        todo_path = Path(repo_path) / "TODO.md"
        if not todo_path.exists():
            return []
        content = todo_path.read_text(encoding="utf-8")
        return self._parse_deferred_section(content)

    def clear_deferred(self, repo_name: str) -> int:
        """Remove all deferred items from the repo's TODO.md. Returns count removed."""
        if not repo_name:
            return 0
        repo_path = self._repos.get(repo_name)
        if not repo_path:
            return 0
        todo_path = Path(repo_path) / "TODO.md"
        if not todo_path.exists():
            return 0
        content = todo_path.read_text(encoding="utf-8")
        items = self._parse_deferred_section(content)
        if not items:
            return 0
        content = self._update_deferred_section(content, [])
        todo_path.write_text(content, encoding="utf-8")
        log.info("Cleared %d deferred items from %s TODO.md", len(items), repo_name)
        return len(items)

    @staticmethod
    def _parse_deferred_section(content: str) -> list[str]:
        """Extract items from ## Deferred Revisions section of a TODO.md."""
        lines = content.splitlines()
        in_section = False
        items = []
        for line in lines:
            stripped = line.strip()
            if stripped == "## Deferred Revisions":
                in_section = True
                continue
            if in_section and stripped.startswith("## "):
                break
            if in_section and stripped.startswith("- "):
                text = stripped[2:]
                if text.startswith("[ ] "):
                    text = text[4:]
                items.append(text)
        return items

    @staticmethod
    def _update_deferred_section(content: str, items: list[str]) -> str:
        """Replace or insert the ## Deferred Revisions section in TODO.md."""
        lines = content.splitlines()
        section_start = None
        section_end = None
        for i, line in enumerate(lines):
            if line.strip() == "## Deferred Revisions":
                section_start = i
                continue
            if section_start is not None and section_end is None:
                if line.strip().startswith("## "):
                    section_end = i
                    break

        if items:
            new_section = [
                "## Deferred Revisions",
                "<!-- Auto-managed by code review. Remove items when addressed. -->",
            ]
            for item in items:
                new_section.append(f"- [ ] {item}")
            new_section.append("")
        else:
            new_section = []

        if section_start is not None:
            if section_end is None:
                section_end = len(lines)
            result = lines[:section_start] + new_section + lines[section_end:]
        elif items:
            # Strip trailing blank lines to avoid double-spacing before new section
            while lines and lines[-1].strip() == "":
                lines.pop()
            result = lines + [""] + new_section
        else:
            result = lines

        text = "\n".join(result)
        if not text.endswith("\n"):
            text += "\n"
        return text
