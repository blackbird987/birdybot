"""/repo: add, create, switch, describe, hide and unhide repos, plus the plain-text fast path."""

from __future__ import annotations

import asyncio
import difflib
import logging
import re
import subprocess
from pathlib import Path

from bot import config
from bot.procutil import run_capture
from bot.claude.gitpaths import git_toplevel
from bot.engine import repo_desc
from bot.platform.base import RequestContext

from bot.claude.runner import git_fail_reason

log = logging.getLogger(__name__)


# --- Natural language repo detection (Tier 1 — fast regex) ---

_FAST_REPO_PATTERNS = [
    # "add repo <name> <path>" / "register repo <name> <path>"
    re.compile(
        r"(?:add|register)\s+(?:a\s+)?(?:repo|repository|project)\s+"
        r"['\"]?(\w[\w-]{2,31})['\"]?\s+(?:at\s+)?['\"]?"
        r"([A-Za-z]:\\[^\s'\"]+|/[^\s'\"]+)['\"]?",
        re.IGNORECASE,
    ),
    # "create repo <name>" / "create a new repo <name>" [+ optional path]
    re.compile(
        r"(?:create|init)\s+(?:a\s+)?(?:new\s+)?(?:repo|repository|project)\s+"
        r"(?:called\s+|named\s+)?['\"]?(\w[\w-]{2,31})['\"]?"
        r"(?:\s+(?:at\s+)?['\"]?([A-Za-z]:\\[^\s'\"]+|/[^\s'\"]+)['\"]?)?$",
        re.IGNORECASE,
    ),
]


async def _try_fast_repo_command(ctx: RequestContext, text: str) -> bool:
    """Catch very explicit natural-language repo commands. Returns True if handled."""
    stripped = text.strip()
    for i, pat in enumerate(_FAST_REPO_PATTERNS):
        m = pat.search(stripped)
        if not m:
            continue
        if i == 0:  # add/register pattern
            name, path = m.group(1), m.group(2)
            await ctx.messenger.send_text(
                ctx.channel_id, f"Registering repo **{name}** at `{path}`…",
            )
            await on_repo(ctx, f"add {name} {path}")
            return True
        elif i == 1:  # create pattern
            name = m.group(1)
            path = m.group(2) or ""
            await ctx.messenger.send_text(
                ctx.channel_id, f"Creating repo **{name}**…",
            )
            await on_repo(ctx, f"create {name} {path}".strip())
            return True
    return False


# --- /repo ---

# "clear" is reserved for the same reason the subcommand names are: it is
# an argument `/repo desc [name] clear` has to be able to tell apart from
# a repo name, and a repo actually called "clear" would make that command
# print a blurb instead of clearing one.
_RESERVED_REPO_NAMES = {"add", "switch", "list", "create", "remove", "delete",
                       "desc", "deploy", "clear", "hide", "unhide"}


def _validate_repo_name(name: str) -> str | None:
    """Return error message if invalid, None if ok."""
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        return "Repo name must be alphanumeric (hyphens/underscores ok)."
    if name.lower() in _RESERVED_REPO_NAMES:
        return f"'{name}' is a reserved word."
    if len(name) > 64:
        return "Repo name too long (max 64 chars)."
    return None


def _resolve_default_path(name: str, store) -> tuple[Path, str] | None:
    """Resolve default path for a new repo. Returns (path, source) or None."""
    from bot.config import REPOS_BASE_DIR
    if REPOS_BASE_DIR:
        return REPOS_BASE_DIR / name, "REPOS_BASE_DIR"
    _, active_path = store.get_active_repo()
    if active_path:
        return Path(active_path).parent / name, "sibling of active repo"
    repos = store.list_repos()
    if repos:
        return Path(next(iter(repos.values()))).parent / name, "sibling of first repo"
    return None


def _normalize_repo_name(name: str) -> str:
    return re.sub(r"[-_. ]", "", name.lower())


def _similar_repo_names(name: str, repos: dict[str, str]) -> list[str]:
    """Registered repo names close enough to ``name`` to be the same project.

    Case-insensitive equality, one name containing the other once separators
    are dropped (`smart-home` vs `smarthome`, shorter name 4+ characters), or
    a difflib ratio of 0.8+.
    Hidden repos count: forgetting one is the reason this check exists.
    """
    want = _normalize_repo_name(name)
    out = []
    for existing in repos:
        have = _normalize_repo_name(existing)
        if not want or not have:
            continue
        # Containment needs a few letters to mean anything: "ai" sits inside
        # half the names on any machine.
        contains = min(len(want), len(have)) >= 4 and (want in have or have in want)
        if (want == have or contains
                or difflib.SequenceMatcher(None, want, have).ratio() >= 0.8):
            out.append(existing)
    return out


async def _create_repo(ctx: RequestContext, text: str) -> None:
    """Handle /repo create <name> [path] [--github] [--public] [--force]."""
    # --- Parse flags and positional args ---
    tokens = text.split()
    flags = {t for t in tokens if t.startswith("--")}
    positional = [t for t in tokens if not t.startswith("--")]
    github = bool(flags & {"--github", "--gh"})
    public = "--public" in flags
    force = "--force" in flags

    if not positional:
        await ctx.messenger.send_text(
            ctx.channel_id,
            "Usage: /repo create <name> [path] [--github] [--public] [--force]",
        )
        return
    name = positional[0]
    path_str = " ".join(positional[1:]) if len(positional) > 1 else None

    # --- Validate name ---
    if err := _validate_repo_name(name):
        await ctx.messenger.send_text(ctx.channel_id, err)
        return
    if name in ctx.store.list_repos():
        await ctx.messenger.send_text(
            ctx.channel_id, f"Repo '{name}' already exists. Use /repo switch {name}"
        )
        return
    if not force and (similar := _similar_repo_names(name, ctx.store.list_repos())):
        labels = ", ".join(
            f"{n} (hidden)" if ctx.store.is_repo_dormant(n) else n
            for n in similar
        )
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Similar repo(s) already registered: {labels}. "
            f"Use `/repo switch {similar[0]}`, or re-run with --force to "
            f"create '{name}' anyway.",
        )
        return

    # --- Resolve path ---
    path_source: str | None = None
    if path_str:
        repo_path = Path(path_str.strip("\"'"))
    else:
        resolved = _resolve_default_path(name, ctx.store)
        if resolved is None:
            await ctx.messenger.send_text(
                ctx.channel_id,
                "No repos registered — provide a path: /repo create <name> <path>",
            )
            return
        repo_path, path_source = resolved

    # --- Handle existing directory already under version control ---
    # rev-parse (not a `.git` stat) so a path registered at a subdirectory
    # of a larger repo is recognised instead of getting a nested `git init`,
    # which would break the parent repo's view of those files.
    toplevel = git_toplevel(str(repo_path)) if repo_path.exists() else None
    if toplevel is not None:
        ctx.store.add_repo(name, str(repo_path.resolve()))
        ctx.store.switch_repo(name)
        # Be explicit when the path is a subdirectory of a larger repo —
        # silent registration of e.g. a dir under an umbrella repo would
        # surprise the user.
        if Path(toplevel).resolve() == repo_path.resolve():
            note = f"'{name}' already a git repo — registered and switched: {repo_path}"
        else:
            note = (
                f"'{name}' is inside the git repo at {toplevel} — registered "
                f"this subdirectory and switched: {repo_path}"
            )
        await ctx.messenger.send_text(ctx.channel_id, note)
        await repo_desc.refresh_repo_description(ctx.store, name, str(repo_path.resolve()))
        await ctx.messenger.on_repo_added(name)
        return

    # --- Create directory + git init ---
    created_dir = False
    try:
        def _init():
            nonlocal created_dir
            if not repo_path.exists():
                repo_path.mkdir(parents=True)
                created_dir = True
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=str(repo_path),
                capture_output=True, check=True, **config.NOWND,
            )
        await asyncio.to_thread(_init)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if created_dir:
            try:
                repo_path.rmdir()  # best-effort cleanup (empty dir we just created)
            except OSError:
                pass
        await ctx.messenger.send_text(ctx.channel_id, f"git init failed: {e}")
        return
    except OSError as e:
        await ctx.messenger.send_text(ctx.channel_id, f"Failed to create directory: {e}")
        return

    # --- Register ---
    ctx.store.add_repo(name, str(repo_path.resolve()))
    ctx.store.switch_repo(name)
    source_hint = f", default: {path_source}" if path_source else ""
    msg = f"Created '{name}' at {repo_path} (git initialized{source_hint})."

    # --- Optional GitHub remote ---
    if github:
        visibility = "--public" if public else "--private"
        try:
            def _gh_create():
                return run_capture(["gh", "repo", "create", name, visibility,
                     "--source", str(repo_path), "--push"],
                    cwd=str(repo_path),
                )
            result = await asyncio.to_thread(_gh_create)
            if result.returncode == 0:
                msg += f" Pushed to GitHub ({visibility[2:]})."
            else:
                # `gh repo create --push` shells out to git, so this is git's
                # stderr — remote-contacting, and it went out unbounded and
                # unredacted. Same picker as everywhere else.
                reason = git_fail_reason(result.stderr.strip()) or "unknown error"
                msg += f"\nGitHub create failed: {reason}"
        except FileNotFoundError:
            msg += "\nGitHub push skipped: `gh` CLI not installed."

    await ctx.messenger.send_text(ctx.channel_id, msg)
    await repo_desc.refresh_repo_description(ctx.store, name, str(repo_path.resolve()))
    await ctx.messenger.on_repo_added(name)


async def _repo_desc(ctx: RequestContext, rest: str) -> None:
    """`/repo desc [name] [<text>|clear]` — the Control Room blurb.

    Setting one writes `<repo>/.claude/repo.json`, not bot state: the sentence
    describes the repo, so it belongs to the repo and travels with a clone.
    """
    repos = ctx.store.list_repos()
    # The thread's own repo wins over the globally active one — /repo desc is
    # typed inside a repo's forum, and defaulting to whatever was switched to
    # last would write the sentence into the wrong repo's .claude/repo.json.
    if ctx.repo_name and ctx.repo_name in repos:
        default, default_path = ctx.repo_name, repos[ctx.repo_name]
    else:
        default, default_path = ctx.store.get_active_repo()
    target, target_path, body = default, default_path, rest

    # `/repo desc <name> <text>` only when the first word really names a
    # registered repo — otherwise the whole of `rest` is the blurb, and a
    # sentence starting with a repo's name would otherwise lose that word.
    first, _, remainder = rest.partition(" ")
    if rest in repos:
        target, target_path, body = rest, repos[rest], ""
    elif first in repos and remainder.strip():
        target, target_path, body = first, repos[first], remainder.strip()

    if not target or not target_path:
        await ctx.messenger.send_text(
            ctx.channel_id, "No repo set. Use /repo add <name> <path>")
        return

    if body:
        text = None if body == "clear" else body.strip('"\'')
        try:
            await asyncio.to_thread(repo_desc.write_manual_description, target_path, text)
        except OSError as e:
            await ctx.messenger.send_text(ctx.channel_id, f"Could not set the description \u2014 {e}")
            return
        ctx.store.clear_repo_description(target)

    blurb = await repo_desc.refresh_repo_description(ctx.store, target, target_path)
    entry = ctx.store.get_repo_description_entry(target) or {}
    source = entry.get("source") or ""

    if body:
        await ctx.messenger.on_repo_meta_changed(target)
    if blurb:
        note = f" (from {source})" if source else ""
        await ctx.messenger.send_text(ctx.channel_id, f"**{target}** — {blurb}{note}")
    else:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"No description for `{target}`. Set one: `/repo desc <text>`\n"
            f"Otherwise it is read from CLAUDE.md, README.md or package metadata.",
        )


async def _repo_set_visibility(ctx: RequestContext, rest: str, *, hidden: bool) -> None:
    """Handle /repo hide <name...> and /repo unhide <name...>.

    Hiding parks the repo's forum out of the way and drops it from every
    picker; it never unregisters the repo or deletes a thread. Several names
    at once, because a sidebar is decluttered in one go, not one repo per
    command.
    """
    names = [n for n in rest.replace(",", " ").split() if n]
    if not names:
        verb = "hide" if hidden else "unhide"
        await ctx.messenger.send_text(
            ctx.channel_id, f"Usage: /repo {verb} <name> [name ...]")
        return

    done: list[str] = []
    lines: list[str] = []
    for name in names:
        if not ctx.store.set_repo_dormant(name, hidden):
            lines.append(f"❌ `{name}`: not found")
            continue
        done.append(name)
        try:
            await ctx.messenger.on_repo_visibility_changed(name, hidden)
        except Exception:
            log.warning("on_repo_visibility_changed failed for %s", name, exc_info=True)

    if done:
        if hidden:
            lines.append(
                f"🙈 Hidden: {', '.join(f'`{n}`' for n in done)}\n"
                f"Nothing was deleted: the repo, its forum and every thread "
                f"in it are intact. Bring it back with "
                f"`/repo unhide {done[0]}`, or it comes back on its own the "
                f"moment work starts in it.")
        else:
            lines.append(f"👁 Visible again: {', '.join(f'`{n}`' for n in done)}")

    await ctx.messenger.send_text(ctx.channel_id, "\n".join(lines))


async def on_repo(ctx: RequestContext, text: str) -> None:
    text = text.strip()

    if text.startswith("add "):
        parts = text[4:].strip().split(None, 1)
        if len(parts) < 2:
            await ctx.messenger.send_text(ctx.channel_id, "Usage: /repo add <name> <path>")
            return
        name, path = parts
        if err := _validate_repo_name(name):
            await ctx.messenger.send_text(ctx.channel_id, err)
            return
        path = str(Path(path.strip("\"'")).resolve())
        if not Path(path).is_dir():
            await ctx.messenger.send_text(ctx.channel_id, f"Directory not found: {path}")
            return
        ctx.store.add_repo(name, path)
        await ctx.messenger.send_text(ctx.channel_id, f"Repo '{name}' added: {path}")
        await repo_desc.refresh_repo_description(ctx.store, name, path)
        await ctx.messenger.on_repo_added(name)

    elif text.startswith("create "):
        await _create_repo(ctx, text[7:].strip())

    elif text.startswith("remove "):
        name = text[7:].strip()
        if ctx.store.remove_repo(name):
            await ctx.messenger.send_text(ctx.channel_id, f"Repo '{name}' removed from registry.")
        else:
            await ctx.messenger.send_text(ctx.channel_id, f"Repo '{name}' not found.")

    elif text == "hide" or text.startswith("hide "):
        await _repo_set_visibility(ctx, text[4:].strip(), hidden=True)

    elif text == "unhide" or text.startswith("unhide "):
        await _repo_set_visibility(ctx, text[6:].strip(), hidden=False)

    elif text.startswith("switch "):
        name = text[7:].strip()
        if ctx.store.switch_repo(name):
            _, path = ctx.store.get_active_repo()
            await ctx.messenger.send_text(ctx.channel_id, f"Switched to '{name}': {path}")
        else:
            await ctx.messenger.send_text(ctx.channel_id, f"Repo '{name}' not found.")

    elif text == "desc" or text.startswith("desc "):
        await _repo_desc(ctx, text[4:].strip())

    elif text == "list":
        repos = ctx.store.list_repos()
        active, _ = ctx.store.get_active_repo()
        if repos:
            # Hidden repos are still listed: hiding takes them out of the
            # sidebar and the pickers, not out of the registry, and this is
            # where you go to find one again.
            lines, hidden_lines = [], []
            for name, path in repos.items():
                marker = " *" if name == active else ""
                entry = f"  {name}{marker} → {path}"
                (hidden_lines if ctx.store.is_repo_dormant(name)
                 else lines).append(entry)
            if hidden_lines:
                lines.append("\nHidden (`/repo unhide <name>`):")
                lines.extend(hidden_lines)
            await ctx.messenger.send_text(ctx.channel_id, "\n".join(lines))
        else:
            await ctx.messenger.send_text(ctx.channel_id, "No repos registered.")

    elif text.startswith("deploy"):
        rest = text[6:].strip()
        if rest.startswith("remove "):
            rname = rest[7:].strip()
            ctx.store.remove_deploy_config(rname)
            await ctx.messenger.send_text(ctx.channel_id, f"Deploy config removed for `{rname}`.")
        elif rest == "set" or rest.startswith("set "):
            parts = rest[4:].strip().split(None, 1) if len(rest) > 3 else []
            if len(parts) < 2:
                await ctx.messenger.send_text(ctx.channel_id, "Usage: /repo deploy set <name> <command>")
                return
            rname, command = parts
            if rname not in ctx.store.list_repos():
                await ctx.messenger.send_text(ctx.channel_id, f"Repo `{rname}` not found.")
                return
            from bot.engine.deploy import make_deploy_config
            ctx.store.set_deploy_config(rname, make_deploy_config(
                "command", command=command, label="Deploy",
                source="manual", approved=True,
            ))
            await ctx.messenger.send_text(ctx.channel_id, f"Deploy set for `{rname}`: `{command}`")
            await ctx.messenger.on_deploy_state_changed(rname)
        else:
            # Show current deploy configs
            configs = {n: ctx.store.get_deploy_config(n)
                       for n in ctx.store.list_repos() if ctx.store.get_deploy_config(n)}
            if not configs:
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    "No deploy configs.\nUsage: `/repo deploy set <name> <command>`",
                )
            else:
                lines = []
                for n, c in configs.items():
                    status = "approved" if c.get("approved") else "pending approval"
                    cmd = c.get("command", "self")
                    lines.append(f"  **{n}**: `{cmd}` ({status})")
                await ctx.messenger.send_text(ctx.channel_id, "\n".join(lines))

    elif not text:
        name, path = ctx.store.get_active_repo()
        # The Discord switch menu only appears with two or more *visible*
        # repos, so hiding all but one lands here. Say how many are parked,
        # or the repos look lost rather than hidden.
        hidden = ctx.store.list_dormant_repos()
        suffix = (f"\n-# {len(hidden)} hidden - `/repo list` to see them"
                  if hidden else "")
        if name:
            await ctx.messenger.send_text(
                ctx.channel_id, f"Active repo: {name} ({path}){suffix}")
        else:
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"No repo set. Use /repo add <name> <path>{suffix}")

    else:
        await ctx.messenger.send_text(
            ctx.channel_id,
            "Usage: /repo add|remove|create|switch|list|hide|unhide|desc|deploy")
