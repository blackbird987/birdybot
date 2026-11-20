"""The repo registry, hidden (dormant) repos, Control Room descriptions and
the per-repo deferred revisions kept in each repo's TODO.md.

A mixin of ``StateStore`` (bot/store/state.py), which owns the dicts these
methods read and the persistence that saves them.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from pathlib import Path

from bot.fsutil import atomic_write_text

# A done item an older writer boxed a second time ("- [ ] [x] ...").
_BOXED_TWICE = "[ ] [x] "
# Any markdown heading ends the section's top-level list.
_HEADING_RE = re.compile(r'#{1,6}\s')


log = logging.getLogger(__name__)


class ReposMixin:
    # --- Session sandbox (read through bot/claude/sandbox.py) ---

    def sandbox_entries(self) -> dict:
        return self._sandboxed_repos

    def set_sandbox_entry(self, real_path: str, entry: dict | None) -> None:
        if entry is None:
            self._sandboxed_repos.pop(real_path, None)
        else:
            self._sandboxed_repos[real_path] = entry
        self.save()

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
    #
    # The section is edited in place, never regenerated. Its items are the
    # unindented "- " lines of its top-level list: from the header to the first
    # heading of any level after it. Anything under a sub-heading (an owner's
    # "### Owner questions" block, say) is the owner's and is never read as an
    # item, cleared or rewritten. A ticked item ("- [x]") is done: it is not
    # returned, not cleared, and still blocks a re-append of the same finding.

    @staticmethod
    def deferred_dedup_key(item: str) -> str:
        """Normalize a deferred item to a dedup key.

        Strips severity tag, leading dash, lowercases, then uses
        [Tag] + first 40 chars of description as the comparison key.
        A note an owner put after the severity ("(stale, ...)", "(queued
        ...)") goes with it, so a triaged item still matches its finding.
        Public so engine code (e.g. plan-review injection) can dedup
        with the same normalization used on append.
        """
        text = re.sub(r'\s*\((Critical|High|Medium|Low)\)(\s*\(.*)?\s*$', '', item)
        text = re.sub(r'^-\s*', '', text).strip().lower()
        m = re.match(r'(\[[^\]]+\])\s*(.*)', text)
        if m:
            return f"{m.group(1)} {m.group(2)[:40]}"
        return text[:50]

    def append_deferred(self, repo_name: str, items: list[str]) -> None:
        """Append deferred revision items to the repo's TODO.md (deduplicated).

        Uses normalized key matching against every top-level item, open or
        done, so a finding already ticked off is not filed again. New lines go
        in after the last top-level item; nothing else in the file moves.
        """
        if not repo_name or not items:
            return
        repo_path = self._repos.get(repo_name)
        if not repo_path or not Path(repo_path).is_dir():
            log.warning("Cannot append deferred items: repo dir missing for %s", repo_name)
            return
        todo_path = self._todo_path(repo_path)
        if todo_path is None:
            return

        if todo_path.exists():
            content = todo_path.read_text(encoding="utf-8")
        else:
            content = "# TODO\n"

        entries = self._deferred_entries(content)
        existing_keys = {self.deferred_dedup_key(text) for _, _, text in entries}
        new_items = []
        for item in items:
            key = self.deferred_dedup_key(item)
            if key in existing_keys:
                continue
            existing_keys.add(key)
            new_items.append(item)
        if not new_items:
            log.debug("All %d deferred items already tracked for %s", len(items), repo_name)
            return

        content = self._insert_deferred(content, new_items)
        self._write_todo(todo_path, content)
        log.info("Appended %d deferred items to %s TODO.md (skipped %d dupes)",
                 len(new_items), repo_name, len(items) - len(new_items))

    def _todo_path(self, repo_path: str) -> Path | None:
        """The repo's TODO.md, or None when a session could have planted it.

        The bot reads and rewrites this file outside any sandbox, so a link or FIFO
        there is a way to read, clobber or hang on a host file. Asked of every repo:
        no run is at hand here, and a guest can be confined in a repo with no setting.
        """
        path = Path(repo_path) / "TODO.md"
        try:
            if stat.S_ISREG(os.lstat(path).st_mode):
                return path
        except FileNotFoundError:
            return path
        except OSError:
            pass
        log.warning("Skipping %s: not a plain file", path)
        return None

    def _write_todo(self, path: Path, content: str) -> None:
        # A rename replaces a link swapped in since the check, and does not follow it.
        atomic_write_text(path, content)

    def get_deferred(self, repo_name: str) -> str:
        """Get the deferred revisions section from the repo's TODO.md."""
        items = self.get_deferred_items(repo_name)
        if not items:
            return ""
        return "\n".join(f"- {item}" for item in items)

    def get_deferred_items(self, repo_name: str) -> list[str]:
        """Get the open deferred revision items from the repo's TODO.md."""
        if not repo_name:
            return []
        repo_path = self._repos.get(repo_name)
        if not repo_path:
            return []
        todo_path = self._todo_path(repo_path)
        if todo_path is None or not todo_path.exists():
            return []
        content = todo_path.read_text(encoding="utf-8")
        return self._parse_deferred_section(content)

    def clear_deferred(self, repo_name: str) -> int:
        """Remove the open top-level deferred items. Returns count removed.

        Done items and everything under a sub-heading stay where they are.
        """
        if not repo_name:
            return 0
        repo_path = self._repos.get(repo_name)
        if not repo_path:
            return 0
        todo_path = self._todo_path(repo_path)
        if todo_path is None or not todo_path.exists():
            return 0
        content = todo_path.read_text(encoding="utf-8")
        open_lines = {i for i, done, _ in self._deferred_entries(content) if not done}
        if not open_lines:
            return 0
        lines = content.splitlines(keepends=True)
        kept = [line for i, line in enumerate(lines) if i not in open_lines]
        self._write_todo(todo_path, self._repair_deferred("".join(kept)))
        log.info("Cleared %d deferred items from %s TODO.md", len(open_lines), repo_name)
        return len(open_lines)

    @staticmethod
    def _deferred_scope(lines: list[str]) -> tuple[int | None, int]:
        """(header index, end index) of the section's top-level list.

        The end is the first heading of any level after the header (a "### "
        sub-heading or the next "## " section), or the end of the file.
        """
        start = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if start is None:
                if stripped == "## Deferred Revisions":
                    start = i
                continue
            if _HEADING_RE.match(stripped):
                return start, i
        return start, len(lines)

    @staticmethod
    def _split_deferred_line(line: str) -> tuple[bool, str] | None:
        """(done, text) for a top-level item line, or None when it is not one."""
        body = line.rstrip("\r\n")
        if not body.startswith("- "):
            return None
        text = body[2:].strip()
        low = text.lower()
        if low.startswith(_BOXED_TWICE):
            return True, text[len(_BOXED_TWICE):]
        if low.startswith("[x] "):
            return True, text[4:]
        if low in ("[x]", _BOXED_TWICE.strip()):
            return True, ""
        if text.startswith("[ ] "):
            return False, text[4:]
        if text == "[ ]":
            return False, ""
        return False, text

    @classmethod
    def _deferred_entries(cls, content: str) -> list[tuple[int, bool, str]]:
        """(line index, done, text) of each top-level item in the section."""
        lines = content.splitlines(keepends=True)
        start, end = cls._deferred_scope(lines)
        if start is None:
            return []
        entries = []
        for i in range(start + 1, end):
            parsed = cls._split_deferred_line(lines[i])
            if parsed is not None:
                entries.append((i, parsed[0], parsed[1]))
        return entries

    @classmethod
    def _parse_deferred_section(cls, content: str) -> list[str]:
        """The open top-level items of the ## Deferred Revisions section."""
        return [text for _, done, text in cls._deferred_entries(content) if not done]

    @classmethod
    def _repair_deferred(cls, content: str) -> str:
        """Turn each "- [ ] [x] " top-level line back into "- [x] "."""
        lines = content.splitlines(keepends=True)
        start, end = cls._deferred_scope(lines)
        if start is None:
            return content
        for i in range(start + 1, end):
            line = lines[i]
            if line.lower().startswith("- " + _BOXED_TWICE):
                lines[i] = "- [x] " + line[2 + len(_BOXED_TWICE):]
        return "".join(lines)

    @classmethod
    def _insert_deferred(cls, content: str, items: list[str]) -> str:
        """Add "- [ ] item" lines to the section, creating it when missing."""
        new_lines = [f"- [ ] {item}\n" for item in items]
        lines = content.splitlines(keepends=True)
        start, end = cls._deferred_scope(lines)
        if start is None:
            while lines and lines[-1].strip() == "":
                lines.pop()
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            section = [
                "## Deferred Revisions\n",
                "<!-- Auto-managed by code review. Remove items when addressed. -->\n",
                *new_lines,
            ]
            return "".join(lines + (["\n"] if lines else []) + section)

        item_lines = [
            i for i in range(start + 1, end)
            if cls._split_deferred_line(lines[i]) is not None
        ]
        if item_lines:
            at = item_lines[-1] + 1
        else:
            at = start + 1
            if at < end and lines[at].strip().startswith("<!--"):
                at += 1
            # Keep the blank line above a following heading or text below the items.
            if at < len(lines) and lines[at].strip():
                new_lines.append("\n")
        if at > 0 and not lines[at - 1].endswith("\n"):
            lines[at - 1] += "\n"
        lines[at:at] = new_lines
        return cls._repair_deferred("".join(lines))
