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

    def reset_daily_budget(self) -> None:
        self._daily_cost = 0.0
        self._cost_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.save()

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

    # --- Persistent Per-Repo Deferred Revisions ---

    def append_deferred(
        self, repo_name: str, items: list[str],
        thread_id: str = "", topic: str = "",
    ) -> None:
        """Append deferred revision items to the repo's persistent backlog."""
        if not repo_name or not items:
            return
        from bot.config import DEFERRED_DIR, safe_repo_slug
        slug = safe_repo_slug(repo_name)
        fpath = DEFERRED_DIR / f"{slug}.md"
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        header = f"## {date}"
        if thread_id:
            header += f" — {thread_id}"
        if topic:
            header += f" ({topic})"
        lines = [header]
        for item in items:
            lines.append(f"- {item}")
        lines.append("")
        block = "\n".join(lines)

        if fpath.exists():
            existing = fpath.read_text(encoding="utf-8")
            fpath.write_text(existing + block, encoding="utf-8")
        else:
            preamble = f"# Deferred Revisions — {repo_name}\n\n"
            fpath.write_text(preamble + block, encoding="utf-8")
        log.info("Appended %d deferred items for repo %s", len(items), repo_name)

    def get_deferred(self, repo_name: str) -> str:
        """Get the full deferred revisions markdown for a repo."""
        if not repo_name:
            return ""
        from bot.config import DEFERRED_DIR, safe_repo_slug
        fpath = DEFERRED_DIR / f"{safe_repo_slug(repo_name)}.md"
        if not fpath.exists():
            return ""
        return fpath.read_text(encoding="utf-8")

    def get_deferred_items(self, repo_name: str) -> list[str]:
        """Get just the bullet items from the deferred file."""
        text = self.get_deferred(repo_name)
        if not text:
            return []
        items = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                items.append(stripped[2:])
        return items

    def clear_deferred(self, repo_name: str) -> int:
        """Remove all deferred items for a repo. Returns count removed."""
        if not repo_name:
            return 0
        from bot.config import DEFERRED_DIR, safe_repo_slug
        fpath = DEFERRED_DIR / f"{safe_repo_slug(repo_name)}.md"
        if not fpath.exists():
            return 0
        items = self.get_deferred_items(repo_name)
        fpath.unlink()
        log.info("Cleared %d deferred items for repo %s", len(items), repo_name)
        return len(items)
