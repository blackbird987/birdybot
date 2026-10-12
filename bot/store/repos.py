"""The repo registry, hidden (dormant) repos, Control Room descriptions and
the per-repo deferred revisions kept in each repo's TODO.md.

A mixin of ``StateStore`` (bot/store/state.py), which owns the dicts these
methods read and the persistence that saves them.
"""

from __future__ import annotations

import logging
from pathlib import Path


log = logging.getLogger(__name__)


class ReposMixin:
    # --- Repo Registry ---

    def add_repo(self, name: str, path: str) -> None:
        self._repos[name] = path
        # Registering a repo is the opposite of parking it. remove_repo
        # already clears the flag, but a hidden repo re-pointed at a new
        # path with /repo add (no remove first) would otherwise come back
        # invisible, with nothing on screen to explain why.
        self._dormant_repos.discard(name)
        if not self._active_repo:
            self._active_repo = name
        self.save()

    def remove_repo(self, name: str) -> bool:
        if name not in self._repos:
            return False
        del self._repos[name]
        # A name re-registered at a different path must re-derive its blurb,
        # and must not come back hidden.
        self._repo_descriptions.pop(name, None)
        self._dormant_repos.discard(name)
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

    # --- Dormant (hidden) repos ---
    #
    # Hiding is a *display* state, never a registration state. `list_repos()`
    # is deliberately unfiltered: dozens of callers use it to resolve a repo
    # path for resume, merge, worktree and deploy work, and a hidden repo has
    # to keep working for all of them. Only the surfaces that draw a list for
    # a human read `list_active_repos()`.

    def set_repo_dormant(self, name: str, dormant: bool) -> bool:
        """Hide or unhide a registered repo. False if it is not registered."""
        if name not in self._repos:
            return False
        if dormant:
            if name in self._dormant_repos:
                return True
            self._dormant_repos.add(name)
        else:
            if name not in self._dormant_repos:
                return True
            self._dormant_repos.discard(name)
        self.save()
        return True

    def is_repo_dormant(self, name: str) -> bool:
        return name in self._dormant_repos

    def list_dormant_repos(self) -> list[str]:
        """Registered repos that are currently hidden, in registration order."""
        return [n for n in self._repos if n in self._dormant_repos]

    def list_active_repos(self) -> dict[str, str]:
        """`list_repos()` minus the hidden ones, for pickers and dashboards."""
        return {n: p for n, p in self._repos.items()
                if n not in self._dormant_repos}

    # --- Repo descriptions (Control Room blurb, see bot/engine/repo_desc.py) ---

    def get_repo_description_entry(self, name: str) -> dict | None:
        entry = self._repo_descriptions.get(name)
        return entry if isinstance(entry, dict) else None

    def set_repo_description(self, name: str, text: str, source: str,
                             sig: str, mtime: float, path: str) -> None:
        """Record a derived blurb. An empty ``text`` is a cached *miss* — a repo
        that says nothing about itself must not be re-read on every refresh.

        ``sig`` is the freshness key (see ``repo_desc.source_signature``);
        ``mtime`` is the newest source mtime, carried only so this record is
        legible when read out of ``data/state.json`` by hand.

        Deferred to the auto-save loop, not written through. Control rooms
        refresh on every instance start and completion, and a cache miss that
        forced a full multi-megabyte rewrite of state.json would make the
        cache cost more on the miss path than the reads it exists to avoid.
        Losing an entry to a crash costs one re-derivation, nothing more.
        """
        self._repo_descriptions[name] = {
            "text": text, "source": source,
            "sig": sig, "mtime": mtime, "path": path,
        }
        self.mark_dirty()

    def clear_repo_description(self, name: str) -> None:
        if self._repo_descriptions.pop(name, None) is not None:
            self.mark_dirty()

    # --- Persistent Per-Repo Deferred Revisions (stored in repo TODO.md) ---

    @staticmethod
    def deferred_dedup_key(item: str) -> str:
        """Normalize a deferred item to a dedup key.

        Strips severity tag, leading dash, lowercases, then uses
        [Tag] + first 40 chars of description as the comparison key.
        Public so engine code (e.g. plan-review injection) can dedup
        with the same normalization used on append.
        """
        import re
        text = re.sub(r'\s*\((Critical|High|Medium|Low)\)\s*$', '', item)
        text = re.sub(r'^-\s*', '', text).strip().lower()
        m = re.match(r'(\[[^\]]+\])\s*(.*)', text)
        if m:
            return f"{m.group(1)} {m.group(2)[:40]}"
        return text[:50]

    def append_deferred(self, repo_name: str, items: list[str]) -> None:
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
        existing_keys = {self.deferred_dedup_key(i) for i in existing_items}
        new_items = [
            item for item in items
            if self.deferred_dedup_key(item) not in existing_keys
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
