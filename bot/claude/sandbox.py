"""Opt-in, per-repo sandbox for Claude sessions.

Everything that decides what a sandboxed session can reach lives here: the
per-repo setting, the generated CLI settings, the environment it gets, and the
private git directory a worktree session works in. The runner calls this from
``runner/sandboxing.py``, and every spawn goes through that one point.

Read docs/design/session-sandbox.md before changing anything here: it holds what
each piece defends against and what stays open.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import stat
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from bot import config
from bot.fsutil import is_env_file, is_within, read_contained_text
from bot.platform.redact import _ENV_SECRET_NAMES
from bot.procutil import run_capture

log = logging.getLogger(__name__)

# Load-bearing: parser.is_account_agnostic_error matches this, so a refused run
# is never read as an account falling over and handed to the backup account.
REFUSAL_PHRASE = "Session sandbox refused this run"

DEFAULT_HOSTS = ["github.com", "api.github.com"]

_HOOKS_DIR = Path(__file__).parent / "hooks"
_HOOK_SCRIPT = _HOOKS_DIR / "sandbox_paths.py"

# Zero-byte mount points the CLI's bwrap leaves in the working directory while
# Bash runs. Killed sessions leave them behind, and `git add -A` trips on them.
STUB_FILES = (
    ".bash_profile", ".bashrc", ".gitconfig", ".gitmodules", ".idea", ".mcp.json",
    ".profile", ".ripgreprc", ".vscode", ".zprofile", ".zshrc",
    ".claude/.cc-writes", ".claude/agents", ".claude/commands", ".claude/hooks",
    ".claude/launch.json", ".claude/loop.md", ".claude/output-styles", ".claude/routines",
    ".claude/scheduled_tasks.json", ".claude/workflows",
)
_STUB_EXCLUDES = tuple(f"/{name}" for name in STUB_FILES)
_EXCLUDE_MARKER = "# session sandbox stubs (bot)"

# Every tool the sandbox does not reach, or that talks past it. Grep is not offered
# by this CLI yet. It would run outside bwrap and search a directory by content,
# past any env file in it that the path hook can only refuse by name. Bash grep
# stays inside the sandbox and sees those files empty.
_DENIED_TOOLS = [
    "WebFetch", "WebSearch", "SendMessage", "RemoteTrigger", "CronCreate",
    "CronList", "CronDelete", "ListAgents", "Monitor", "Workflow", "Skill",
    "EnterWorktree", "ExitWorktree", "PushNotification", "DesignSync",
    "ScheduleWakeup", "Grep", "Edit(~/.*)", "Edit(~/.*/**)",
]

REQUIRED_TOOLS = ("bwrap", "socat")

# Relative to home. Named one by one: ~/.local as a whole holds keyrings and
# chat data. Only the ones that exist on this machine go in the settings.
_TOOLCHAIN_READ = (
    ".local/bin", ".local/lib", ".local/share/claude", ".local/share/uv",
    ".local/share/pipx", ".gitconfig", ".config/git", ".dotnet", ".rustup",
    ".cargo/bin", ".nvm", ".pyenv", ".bun/bin",
)
_REPO_TOOL_DIRS = (".venv", "venv", "node_modules")
_HOST_DENY = ("/mnt", "/media", "/run/media", "/var/tmp", "/dev/shm")

_SECRET_NAME_RE = re.compile(
    r"(?i)(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL|PRIVATE_KEY)"
)
# What the CLI itself needs. Stays in its env and is denied to Bash.
_CLI_NEEDS = {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CONFIG_DIR"}
# systemd-run reaches the user manager through these, so they stay in the CLI's
# env for the session scope to work. Bash does not get them.
_SCOPE_NEEDS = {"DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"}
_ALWAYS_STRIP = {"SSH_AUTH_SOCK", "GPG_AGENT_INFO", "GNOME_KEYRING_CONTROL"}

_HOST_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*\.[a-z]{2,}$")
_URL_USERINFO_RE = re.compile(r"https?://[^/@\s]+@")
_ENV_NAME_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")

_MAX_INSTRUCTIONS_BYTES = 64 * 1024

SANDBOX_NOTE = """

--- Sandbox ---
This session runs in a sandbox. Bash sees this working directory, a private temp dir ($TMPDIR) and a few toolchains. The rest of the home directory, /tmp, the bot's data and other repos are not there.
Not available here, and they will fail: email, Telegram, scripts/instances.py, gh, git push over SSH, podman, systemctl, WebFetch, WebSearch, MCP servers, skills, and any network host not on this repo's list ({hosts}). Do not try to work around this. Stop and say what you need, and the owner can do it or change the setup.
A few bot commands are off too: /repo is ignored, /spawn only reaches other sandboxed repos, /watch only reads logs inside this directory and cannot follow a pid (the sandbox has its own process numbering).
`.claude/` and `.mcp.json` are not writable, with any tool. If a step asks you to write one (for example `.claude/test.json`), do not fight it: put the contents in your final message for the owner to add.
Your memory directory can be read and not written. Temp files do not survive this run. Package caches do.
{git_note}"""

_GIT_NOTE_WORKTREE = (
    "Commit on the current branch only. Other branches, new tags and anything "
    "else you do in git stay behind when this run ends."
)
_GIT_NOTE_QUERY = (
    "`git commit` will fail here because .git is read-only in this session. "
    "`git status`, `log` and `diff` work. Edit files and leave them for the owner."
)


class SandboxRefused(Exception):
    """This run cannot start safely. Its message is shown in the thread."""


class SandboxSettingError(Exception):
    """A repo's stored setting could not be read."""


# --- The setting ------------------------------------------------------------

def _key(repo_path: str) -> str:
    return os.path.realpath(repo_path)


def setting_for(store, repo_path: str | None) -> dict | None:
    """The stored setting for this repo, or None when it is off.

    Raises SandboxSettingError for an entry it cannot read. A store with no
    sandbox accessors (a test double) has no sandboxed repos.
    """
    if not repo_path or store is None:
        return None
    getter = getattr(store, "sandbox_entries", None)
    entries = getter() if callable(getter) else None
    if not isinstance(entries, dict):
        return None
    if "__corrupt__" in entries:
        raise SandboxSettingError("the stored sandbox settings are unreadable")
    entry = entries.get(_key(repo_path))
    if entry is None:
        return None
    if not isinstance(entry, dict) or not isinstance(entry.get("hosts"), list):
        raise SandboxSettingError(f"unreadable sandbox setting for {repo_path}")
    return entry


def is_sandboxed(store, repo_path: str | None) -> bool:
    """Is this repo sandboxed? An answer that cannot be read counts as yes."""
    try:
        return setting_for(store, repo_path) is not None
    except Exception:
        log.warning("Sandbox setting unreadable for %s", repo_path, exc_info=True)
        return True


def origin_is_sandboxed(store, instance) -> bool:
    """Did this text come from a sandboxed session?

    With no instance to ask, any sandboxed repo means yes: the bot-side gates
    this feeds should not open because a lookup came back empty.
    """
    if instance is not None:
        return is_sandboxed(store, getattr(instance, "repo_path", None))
    try:
        getter = getattr(store, "sandbox_entries", None)
        entries = getter() if callable(getter) else None
    except Exception:
        return True
    return bool(entries) if isinstance(entries, dict) else False


def validate_hosts(hosts: list[str]) -> list[str]:
    """Normalized hosts. Raises ValueError naming the first one it refuses."""
    cleaned: list[str] = []
    for raw in hosts:
        host = raw.strip().lower()
        if not _HOST_RE.match(host):
            raise ValueError(f"`{raw}` is not a host name (no schemes, paths, IPs or a bare `*`)")
        if host not in cleaned:
            cleaned.append(host)
    return cleaned


def _bot_roots() -> list[str]:
    roots = {str(config._PROJECT_ROOT), str(config._ENV_ROOT), str(config.DATA_DIR)}
    return sorted(roots)


def missing_tools() -> list[str]:
    return [tool for tool in REQUIRED_TOOLS if not shutil.which(tool)]


def _hooks_path_problem(repo_path: str) -> str | None:
    """A hooks path the session could write would run its code outside."""
    r = run_capture(["git", "config", "--get", "core.hooksPath"], cwd=repo_path, timeout=30)
    value = (r.stdout or "").strip()
    if not value:
        return None
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        return f"its core.hooksPath (`{value}`) is relative, so a session could supply the hooks"
    if is_within(expanded, repo_path):
        return f"its core.hooksPath (`{value}`) is inside the repo, so a session could supply the hooks"
    return None


def _git_credentials_problem(repo_path: str) -> str | None:
    """A credential in git's config, which a session here can read. Never printed."""
    r = run_capture(
        ["git", "config", "--get-regexp",
         r"^(remote\..*\.(url|pushurl)|url\..*\.insteadof|http\..*extraheader)$"],
        cwd=repo_path, timeout=30,
    )
    # 1 is git's "no such key".
    if r.returncode not in (0, 1):
        return "git could not read its config"
    for line in (r.stdout or "").splitlines():
        key = line.partition(" ")[0]
        if key.startswith("http.") or _URL_USERINFO_RE.search(line):
            return (
                f"its git config has a credential in it ({key.rsplit('.', 1)[-1]}), and a "
                "session can read that. Use a credential helper instead"
            )
    return None


def _git_config_problem(repo_path: str) -> str | None:
    return _hooks_path_problem(repo_path) or _git_credentials_problem(repo_path)


def check_repo_allowed(repo_path: str, provider_name: str = "claude") -> str | None:
    """Why this repo cannot be sandboxed, or None when it can."""
    real = os.path.realpath(repo_path)
    for root in _bot_roots():
        if is_within(real, root) or is_within(root, real):
            return (
                "this path overlaps the bot itself. A session here could edit "
                "code that later runs outside any sandbox"
            )
    home = str(Path.home())
    if not is_within(real, home):
        return "only repos under the home directory have been tested"
    if os.path.relpath(real, os.path.realpath(home)).startswith("."):
        return "it sits in a dot directory in home, where the `~/.*` deny would stop every edit"
    if not os.path.isdir(real):
        return "the path is not a directory"
    if not os.path.isdir(os.path.join(real, ".git")):
        return "it has no .git directory (a worktree or a non-git folder cannot be sandboxed)"
    if provider_name != "claude":
        return f"provider `{provider_name}` has no sandbox settings. It works with Claude only"
    return _git_config_problem(real)


def harden_repo_git(repo_path: str) -> None:
    """Stop the bot's own git from running code out of a session's files.

    `git status` run by the bot would otherwise descend into a nested repo the
    session built and fire the core.fsmonitor in its config. The repo's real
    config is out of a session's reach. Raises SandboxRefused if it won't stick.
    """
    for key, value in (("core.fsmonitor", "false"), ("diff.ignoreSubmodules", "all")):
        r = run_capture(["git", "config", key, value], cwd=repo_path, timeout=30)
        if r.returncode != 0:
            raise SandboxRefused(f"could not set {key} in the repo's git config")


def unharden_repo_git(repo_path: str) -> None:
    for key in ("core.fsmonitor", "diff.ignoreSubmodules"):
        run_capture(["git", "config", "--unset", key], cwd=repo_path, timeout=30)


# --- A run's directories and settings ---------------------------------------

def sandbox_root() -> Path:
    return Path(config.DATA_DIR) / "sandbox"


def _digest(path: str) -> str:
    return hashlib.sha1(os.path.realpath(path).encode()).hexdigest()[:16]


def state_dir_for(worktree: str) -> Path:
    return sandbox_root() / "worktrees" / _digest(worktree)


def private_gitdir(worktree: str) -> str:
    return str(state_dir_for(worktree) / "gitdir")


def cache_dir_for(repo_path: str) -> Path:
    return sandbox_root() / "cache" / _digest(repo_path)


@dataclass
class SandboxRun:
    """What one sandboxed spawn was given. ``run_dir`` is removed afterwards."""
    instance_id: str
    repo_path: str
    working_dir: str
    is_worktree: bool
    run_dir: str
    tmp_dir: str
    private_dir: str
    cache_dir: str
    settings_file: str
    hosts: list[str]
    strip_names: list[str] = field(default_factory=list)
    attachments: dict[str, str] = field(default_factory=dict)
    checked_out: bool = False

    def localize_prompt(self, prompt: str) -> str:
        """Point the prompt's upload paths at this run's own copies."""
        for original, copy in self.attachments.items():
            prompt = prompt.replace(original, copy)
        return prompt

    def system_prompt_note(self) -> str:
        git_note = _GIT_NOTE_WORKTREE if self.is_worktree else _GIT_NOTE_QUERY
        note = SANDBOX_NOTE.format(hosts=", ".join(self.hosts), git_note=git_note)
        return note + project_instructions(
            self.working_dir, skip_claude_dir=not self.is_worktree,
        )


def env_file_names() -> set[str]:
    """The variable names in the bot's `.env`. Values are never read into memory."""
    names: set[str] = set()
    try:
        with open(Path(config._ENV_ROOT) / ".env", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _ENV_NAME_RE.match(line)
                if m:
                    names.add(m.group(1))
    except OSError:
        pass
    return names


def names_to_strip(environ: dict | None = None) -> tuple[list[str], list[str]]:
    """``(strip, deny)``: names removed from the env, and names hidden from Bash.

    Strip is what the CLI does not need. Deny is everything that could hold a
    secret, because the CLI hands its own env to Bash unless told otherwise.
    """
    environ = os.environ if environ is None else environ
    candidates = set(env_file_names()) | set(_ENV_SECRET_NAMES) | _ALWAYS_STRIP
    candidates |= {name for name in environ if _SECRET_NAME_RE.search(name)}
    keep = _CLI_NEEDS | _SCOPE_NEEDS
    strip = sorted(candidates - keep)
    deny = sorted(candidates | keep)
    return strip, deny


def _env_files(working_dir: str) -> list[str]:
    """`.env` files under the working directory, by exact path.

    A `~/**/.env` deny took 108 seconds against 2 in the trial, so these are
    listed one by one.
    """
    skip = {"node_modules", ".git", ".worktrees", ".venv", "venv", "__pycache__", "dist", "build"}
    found: list[str] = []
    base_depth = working_dir.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(working_dir):
        dirs[:] = [d for d in dirs if d not in skip]
        if root.count(os.sep) - base_depth >= 4:
            dirs[:] = []
        found += [os.path.join(root, name) for name in files if is_env_file(name)]
        if len(found) >= 100:
            break
    return found


def attachment_paths(prompt: str | None) -> list[str]:
    """This run's own uploads, named in its prompt."""
    if not prompt:
        return []
    base = re.escape(str(config.PENDING_IMAGES_DIR))
    return sorted(set(re.findall(rf"{base}/[0-9a-f]{{32}}\.[A-Za-z0-9]+", prompt)))


def copy_attachments(prompt: str | None, target: Path) -> dict[str, str]:
    """Copy this run's uploads out of the shared directory. Original path -> copy."""
    copies: dict[str, str] = {}
    for original in attachment_paths(prompt):
        if not os.path.isfile(original) or os.path.islink(original):
            # Reaped already, and the session gets the plain "no such file".
            continue
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        copy = target / os.path.basename(original)
        shutil.copyfile(original, copy)
        copies[original] = str(copy)
    return copies


def _hook_entries(hook_cfg: str, working_dir: str, repo_path: str, is_worktree: bool) -> dict:
    python_exe = sys.executable or "python"
    hooks: dict[str, list] = {"PreToolUse": [{
        "matcher": "Read|Edit|Write|MultiEdit|NotebookEdit|Grep|Glob",
        "hooks": [{
            "type": "command",
            "command": f'"{python_exe}" -I "{_HOOK_SCRIPT}" "{hook_cfg}" || exit 2',
            "timeout": 10,
        }],
    }]}
    # worktree_guard and the test mutex live in settings.local.json, which a
    # sandboxed session does not load, so they move here.
    if not is_worktree:
        return hooks
    if config.WORKTREE_HOOK_ENABLED and (_HOOKS_DIR / "worktree_guard.py").is_file():
        hooks["PreToolUse"].append({
            "matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
            "hooks": [{
                "type": "command",
                "command": (
                    f'"{python_exe}" "{_HOOKS_DIR / "worktree_guard.py"}" '
                    f'"{working_dir}" "{repo_path}"'
                ),
                "timeout": 10,
            }],
        })
    if config.TEST_MUTEX_ENABLED and (_HOOKS_DIR / "test_mutex.py").is_file():
        for event, phase, timeout in (("PreToolUse", "pre", 90), ("PostToolUse", "post", 15)):
            hooks.setdefault(event, []).append({
                "matcher": "Bash",
                "hooks": [{
                    "type": "command",
                    "command": (
                        f'"{python_exe}" "{_HOOKS_DIR / "test_mutex.py"}" {phase} '
                        f'"{working_dir}" "{repo_path}"'
                    ),
                    "timeout": timeout,
                }],
            })
    return hooks


def build_settings(
    *,
    working_dir: str,
    repo_path: str,
    is_worktree: bool,
    tmp_dir: str,
    cache_dir: str,
    hosts: list[str],
    deny_env: list[str],
    hook_cfg: str,
    attachments_dir: str | None,
    env_files: list[str],
    other_repos: list[str],
    memory_dir: str | None = None,
) -> dict:
    """The one settings file a sandboxed run gets. Pure, apart from `exists` checks."""
    home = str(Path.home())
    deny_read = ["~/", "/tmp", f"/tmp/claude-{os.getuid()}", f"/run/user/{os.getuid()}"]
    deny_read += [p for p in _HOST_DENY if os.path.exists(p)]
    deny_read += env_files
    # The deny on home covers a repo under it, unless the working dir (which is
    # allowed) holds it. A repo that holds the working dir would hide it if denied.
    for other in other_repos:
        if not is_within(other, home) or is_within(other, working_dir):
            if not is_within(working_dir, other):
                deny_read.append(other)
    if not is_within(str(config.DATA_DIR), home):
        deny_read.append(str(config.DATA_DIR))

    allow_read = [working_dir, tmp_dir, cache_dir]
    allow_write = [tmp_dir, cache_dir]
    # Later sessions load these, and the merge gate never sees a query run's edit.
    deny_write: list[str] = [os.path.join(working_dir, ".claude")]
    # The CLI's read block only opens for whole directories, and it opens them to
    # Bash too: for reads over any deny, and for writes unless denied here.
    extra_dirs: list[str] = []
    if memory_dir and os.path.isdir(memory_dir):
        extra_dirs.append(memory_dir)
    if attachments_dir:
        extra_dirs.append(attachments_dir)
    deny_write += extra_dirs
    common = os.path.join(repo_path, ".git")
    if is_worktree:
        gitdir = private_gitdir(working_dir)
        allow_read += [gitdir, os.path.join(common, "objects")]
        allow_write.append(gitdir)
        # commondir holds "." and is pinned: a session that could point it
        # elsewhere would hand the bot's own `git status` a config of its choosing.
        deny_write += [
            os.path.join(gitdir, "config"),
            os.path.join(gitdir, "hooks"),
            os.path.join(gitdir, "commondir"),
            os.path.join(gitdir, "objects", "info", "alternates"),
            os.path.join(working_dir, ".git"),
        ]
    else:
        allow_read.append(common)
        deny_write += [common, os.path.join(repo_path, ".worktrees")]
    for rel in _TOOLCHAIN_READ:
        path = os.path.join(home, rel)
        if os.path.exists(path):
            allow_read.append(path)
    # A query session can write here, so `.venv -> ~/.ssh` would open that to its
    # next run, and `.venv -> .env` would reopen the file over its deny.
    for rel in _REPO_TOOL_DIRS:
        path = os.path.join(repo_path, rel)
        if os.path.isdir(path) and is_within(path, repo_path):
            allow_read.append(path)
    if attachments_dir:
        allow_read.append(attachments_dir)

    return {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "denyRead": deny_read,
                "allowRead": allow_read,
                "allowWrite": allow_write,
                "denyWrite": deny_write,
            },
            "credentials": {"envVars": [{"name": n, "mode": "deny"} for n in deny_env]},
            "network": {"allowedDomains": list(hosts), "strictAllowlist": True},
        },
        "permissions": {
            "blockReadsOutsideWorkingDirectories": True,
            "additionalDirectories": extra_dirs,
            "deny": list(_DENIED_TOOLS),
        },
        "hooks": _hook_entries(hook_cfg, working_dir, repo_path, is_worktree),
    }


def preflight(provider_name: str, api_fallback: bool, repo_path: str) -> None:
    """Refuse a run that could not be contained, before any file is written."""
    if provider_name != "claude":
        raise SandboxRefused(
            f"this repo is sandboxed and provider `{provider_name}` has no sandbox settings"
        )
    if api_fallback:
        raise SandboxRefused(
            "API fallback is off in sandboxed repos: `--bare` skips the hooks and "
            "the key stays in the environment"
        )
    if not _HOOK_SCRIPT.is_file():
        raise SandboxRefused("the path check hook is missing")
    missing = missing_tools()
    if missing:
        raise SandboxRefused(f"`{missing[0]}` is not installed")
    problem = _git_config_problem(repo_path)
    if problem:
        raise SandboxRefused(problem)


def prepare_run(
    *,
    instance_id: str,
    repo_path: str,
    working_dir: str,
    hosts: list[str],
    prompt: str | None,
    other_repos: list[str],
    memory_dir: str | None,
    keep_names: set[str],
) -> SandboxRun:
    """Create a run's private directories and write its settings."""
    is_worktree = os.path.realpath(working_dir) != os.path.realpath(repo_path)
    run_dir = sandbox_root() / "runs" / f"{instance_id}-{uuid.uuid4().hex[:8]}"
    tmp_dir = run_dir / "tmp"
    private_dir = run_dir / "private"
    cache_dir = cache_dir_for(repo_path)
    for path in (tmp_dir, private_dir, cache_dir):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A write deny on a missing path leaves a file in its place, which would then
    # stop anything from creating the directory.
    os.makedirs(os.path.join(working_dir, ".claude"), exist_ok=True)

    strip, deny = names_to_strip()
    strip = [n for n in strip if n not in keep_names]
    deny = [n for n in deny if n not in keep_names]

    attachments = copy_attachments(prompt, run_dir / "attachments")
    attachments_dir = str(run_dir / "attachments") if attachments else None
    env_files = _env_files(working_dir)
    hook_cfg = private_dir / "paths.json"
    hook_cfg.write_text(json.dumps({
        "cwd": working_dir,
        "read": [
            working_dir, str(tmp_dir),
            *([attachments_dir] if attachments_dir else []),
            *([memory_dir] if memory_dir else []),
        ],
        "write": [working_dir, str(tmp_dir)],
        "deny_read": env_files,
    }), encoding="utf-8")

    settings = build_settings(
        working_dir=working_dir, repo_path=repo_path, is_worktree=is_worktree,
        tmp_dir=str(tmp_dir), cache_dir=str(cache_dir), hosts=hosts,
        deny_env=deny, hook_cfg=str(hook_cfg), attachments_dir=attachments_dir,
        env_files=env_files, other_repos=other_repos, memory_dir=memory_dir,
    )
    settings_file = private_dir / "settings.json"
    settings_file.write_text(json.dumps(settings, indent=1), encoding="utf-8")
    return SandboxRun(
        instance_id=instance_id, repo_path=repo_path, working_dir=working_dir,
        is_worktree=is_worktree, run_dir=str(run_dir), tmp_dir=str(tmp_dir),
        private_dir=str(private_dir), cache_dir=str(cache_dir),
        settings_file=str(settings_file), hosts=list(hosts), strip_names=strip,
        attachments=attachments,
    )


def apply_env(env: dict, run: SandboxRun) -> None:
    """Take the bot's secrets out of the session's env and point it at its dirs."""
    for name in run.strip_names:
        env.pop(name, None)
    env["CLAUDE_CODE_TMPDIR"] = run.tmp_dir
    env["TMPDIR"] = run.tmp_dir
    cache = run.cache_dir
    env.update({
        "NPM_CONFIG_CACHE": os.path.join(cache, "npm"),
        "npm_config_cache": os.path.join(cache, "npm"),
        "PIP_CACHE_DIR": os.path.join(cache, "pip"),
        "UV_CACHE_DIR": os.path.join(cache, "uv"),
        "XDG_CACHE_HOME": os.path.join(cache, "xdg"),
        "NUGET_PACKAGES": os.path.join(cache, "nuget"),
        "DOTNET_CLI_HOME": os.path.join(cache, "dotnet"),
        "CARGO_HOME": os.path.join(cache, "cargo"),
        "GOCACHE": os.path.join(cache, "go-build"),
        "GOMODCACHE": os.path.join(cache, "go-mod"),
        # MSBuild reads these as properties: source link walks .git by itself
        # and falls over on a private git directory.
        "EnableSourceLink": "false",
        "EnableSourceControlManagerQueries": "false",
    })


def project_instructions(working_dir: str, *, skip_claude_dir: bool = False) -> str:
    """Project CLAUDE.md files, which `--setting-sources user` no longer loads.

    Read without following a link out of the directory: a planted
    `CLAUDE.md -> ~/.ssh/id_rsa` would otherwise put that file in the prompt.
    """
    names = ["CLAUDE.md", "CLAUDE.local.md"]
    # The regular prompt builder already reads .claude/CLAUDE.md from the repo
    # root, which on a query run is this same file.
    if not skip_claude_dir:
        names.insert(1, os.path.join(".claude", "CLAUDE.md"))
    parts: list[str] = []
    for name in names:
        text = read_contained_text(working_dir, name, max_bytes=_MAX_INSTRUCTIONS_BYTES)
        if text:
            parts.append(f"\n\n--- Project instructions (from {name}) ---\n{text}")
    return "".join(parts)


def add_stub_excludes(common_git_dir: str) -> None:
    """Keep the CLI's stub files out of `git add -A`, without touching the tree."""
    exclude = Path(common_git_dir) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = exclude.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        current = ""
    if _EXCLUDE_MARKER in current:
        return
    lines = "\n".join((_EXCLUDE_MARKER, *_STUB_EXCLUDES))
    joiner = "" if not current or current.endswith("\n") else "\n"
    exclude.write_text(f"{current}{joiner}{lines}\n", encoding="utf-8")


def clean_stubs(working_dir: str) -> int:
    """Remove stubs a killed session left. Only zero-byte and untracked."""
    removed = 0
    for name in STUB_FILES:
        path = os.path.join(working_dir, name)
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size != 0:
            continue
        tracked = run_capture(
            ["git", *GIT_SAFE, "ls-files", "--error-unmatch", "--", name],
            cwd=working_dir, timeout=30,
        )
        if tracked.returncode == 0:
            continue
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            log.debug("Could not remove stub %s", path, exc_info=True)
    # The CLI's own write journal, then what prepare_run made for the write deny
    # to hold on to. rmdir only ever takes an empty directory.
    for name in (os.path.join(".claude", ".cc-writes"), ".claude"):
        try:
            os.rmdir(os.path.join(working_dir, name))
        except OSError:
            pass
    return removed


def finish_run(run: SandboxRun) -> None:
    shutil.rmtree(run.run_dir, ignore_errors=True)


# --- The private git directory ----------------------------------------------
#
# A worktree's gitdir sits inside the main repo's .git, and the CLI opens that
# common directory for writes, so a session could `git update-ref` master. So a
# sandboxed worktree points at a private gitdir for the length of the run, and
# the one branch comes back through a checked fetch when it ends. See the doc.

GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "diff.ignoreSubmodules=all"]
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_MERGE_STATE = ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE")


def _git(args: list[str], cwd: str, timeout: int = 120):
    return run_capture(["git", *GIT_SAFE, *args], cwd=cwd, timeout=timeout)


def _replace_file(path: str, text: str) -> None:
    """Atomic. Replaces whatever is there, a planted symlink included."""
    tmp = f"{path}.bot-{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _strip_userinfo(url: str) -> str:
    return re.sub(r"^(https?://)[^/@]+@", r"\1", url)


def _real_gitdir(worktree: str, repo_path: str) -> str:
    """The worktree's real gitdir, checked to be the one git made for it."""
    gitfile = os.path.join(worktree, ".git")
    st = os.lstat(gitfile)
    if not stat.S_ISREG(st.st_mode):
        raise SandboxRefused("the worktree's .git is not a plain file")
    with open(gitfile, encoding="utf-8") as fh:
        text = fh.read(4096).strip()
    if not text.startswith("gitdir: "):
        raise SandboxRefused("the worktree's .git file is not a gitdir pointer")
    real = os.path.realpath(text[len("gitdir: "):])
    if not is_within(real, os.path.join(repo_path, ".git", "worktrees")):
        raise SandboxRefused("the worktree's .git points outside this repo")
    with open(os.path.join(real, "gitdir"), encoding="utf-8") as fh:
        back = fh.read(4096).strip()
    if os.path.realpath(back) != os.path.realpath(gitfile):
        raise SandboxRefused("the worktree's gitdir points back somewhere else")
    return real


def checkout_worktree(worktree: str, repo_path: str, instance_id: str) -> None:
    """Build a fresh private gitdir and point the worktree at it.

    Raises SandboxRefused if a previous run's import failed, or on anything else
    that means this run should not start.
    """
    state = state_dir_for(worktree)
    meta_path = state / "meta.json"
    if meta_path.exists():
        # Going on would wipe the only copy of commits that did not come home.
        problem = checkin_worktree(worktree)
        if problem:
            raise SandboxRefused(_failed_import_message(problem))
    real = _real_gitdir(worktree, repo_path)
    with open(os.path.join(real, "HEAD"), encoding="utf-8") as fh:
        head = fh.read(4096).strip()
    if not head.startswith("ref: refs/heads/"):
        raise SandboxRefused("the worktree is not on a branch")
    branch = head[len("ref: refs/heads/"):]

    common = os.path.join(repo_path, ".git")
    gitdir = private_gitdir(worktree)
    # Fresh every time. A job that outlived the last run could have kept writing
    # to the old one, so nothing in it is reused.
    shutil.rmtree(gitdir, ignore_errors=True)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    r = run_capture(["git", "init", "-q", "--bare", "--template=", gitdir], timeout=60)
    if r.returncode != 0:
        raise SandboxRefused("could not create the private git directory")
    for sub in ("hooks", "info", os.path.join("objects", "info")):
        os.makedirs(os.path.join(gitdir, sub), exist_ok=True)
    alternates = os.path.join(gitdir, "objects", "info", "alternates")
    _replace_file(alternates, os.path.join(common, "objects") + "\n")
    # A commondir of "." changes nothing for git. It exists so the sandbox can
    # hold it read-only: the file cannot be planted if it is already there.
    _replace_file(os.path.join(gitdir, "commondir"), ".\n")

    settings = [
        ("core.bare", "false"), ("core.fsmonitor", "false"),
        ("diff.ignoreSubmodules", "all"),
    ]
    for key in ("user.name", "user.email"):
        value = run_capture(["git", "config", "--local", "--get", key], cwd=repo_path, timeout=30)
        if value.returncode == 0 and value.stdout.strip():
            settings.append((key, value.stdout.strip()))
    origin = run_capture(["git", "remote", "get-url", "origin"], cwd=repo_path, timeout=30)
    if origin.returncode == 0 and origin.stdout.strip():
        settings.append(("remote.origin.url", _strip_userinfo(origin.stdout.strip())))
        settings.append(("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"))
    for key, value in settings:
        r = _git(["config", "--file", os.path.join(gitdir, "config"), key, value], cwd=gitdir)
        if r.returncode != 0:
            raise SandboxRefused(f"could not set {key} in the private git directory")

    r = _git([
        "fetch", "-q", "--update-head-ok", "--no-write-fetch-head", repo_path,
        "+refs/heads/*:refs/heads/*", "+refs/remotes/*:refs/remotes/*",
        "+refs/tags/*:refs/tags/*",
    ], cwd=gitdir, timeout=300)
    if r.returncode != 0:
        raise SandboxRefused(f"could not fill the private git directory: {r.stderr.strip()[:200]}")
    r = _git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=gitdir)
    if r.returncode != 0:
        raise SandboxRefused("could not set the private git directory's HEAD")

    for name in ("index", *_MERGE_STATE):
        src = os.path.join(real, name)
        if os.path.isfile(src):
            shutil.copyfile(src, os.path.join(gitdir, name))
    exclude = ""
    real_exclude = os.path.join(common, "info", "exclude")
    if os.path.isfile(real_exclude):
        with open(real_exclude, encoding="utf-8", errors="replace") as fh:
            exclude = fh.read()
    _replace_file(os.path.join(gitdir, "info", "exclude"), exclude)

    with open(os.path.join(gitdir, "config"), "rb") as fh:
        config_hash = hashlib.sha256(fh.read()).hexdigest()
    # The marker goes first: a crash between these two lines finds a worktree
    # that is already right, and a crash after them finds the marker.
    _replace_file(str(meta_path), json.dumps({
        "worktree": worktree, "repo": repo_path, "real_gitdir": real, "branch": branch,
        "instance_id": instance_id, "config_sha256": config_hash,
    }))
    _replace_file(os.path.join(worktree, ".git"), f"gitdir: {gitdir}\n")


def _gitdir_problem(gitdir: str, config_hash: str) -> str | None:
    """Anything about a used private gitdir the bot's git should not trust."""
    for root, dirs, files in os.walk(gitdir, followlinks=False):
        for name in dirs + files:
            path = os.path.join(root, name)
            mode = os.lstat(path).st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                return f"a link or special file was planted at {os.path.relpath(path, gitdir)}"
    with open(os.path.join(gitdir, "config"), "rb") as fh:
        if hashlib.sha256(fh.read()).hexdigest() != config_hash:
            return "its config changed during the run"
    return None


def _failed_import_message(reason: str) -> str:
    return (
        f"an earlier run's commits could not be brought back ({reason}). "
        "Discard this branch, or turn the sandbox off and on again, to clear it"
    )


def checkin_worktree(worktree: str) -> str | None:
    """Put the worktree back on its real gitdir and bring its branch home.

    Returns the reason the branch could not be imported, or None. The worktree is
    always put back first, whatever else happens. A failed import stays recorded,
    so the next run is refused until ``forget_worktree`` clears it.
    """
    state = state_dir_for(worktree)
    meta_path = state / "meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if meta.get("import_failed"):
        raise SandboxRefused(_failed_import_message(meta["import_failed"]))
    if not os.path.isdir(worktree):
        shutil.rmtree(state, ignore_errors=True)
        return None
    real, repo, branch = meta["real_gitdir"], meta["repo"], meta["branch"]
    gitdir = private_gitdir(worktree)
    _replace_file(os.path.join(worktree, ".git"), f"gitdir: {real}\n")

    try:
        problem = _import_branch(worktree, gitdir, repo, real, branch, meta)
    except Exception as exc:
        # Recorded like any other, or `/sandbox off` could not clear the worktree.
        log.exception("Sandbox: import of %s failed", worktree)
        problem = f"the import failed ({type(exc).__name__})"
    if problem:
        meta["import_failed"] = problem
        _replace_file(str(meta_path), json.dumps(meta))
        log.error("Sandbox: branch %s of %s not imported: %s", branch, repo, problem)
        return problem
    meta_path.unlink(missing_ok=True)
    shutil.rmtree(gitdir, ignore_errors=True)
    return None


def _import_branch(
    worktree: str, gitdir: str, repo: str, real: str, branch: str, meta: dict,
) -> str | None:
    if not os.path.isdir(gitdir):
        return "the private git directory is gone"
    problem = _gitdir_problem(gitdir, meta["config_sha256"])
    if problem:
        return problem
    # Rewritten before git reads them: objects from other repos must not come
    # in through alternates, and shallow or grafts would change what a fetch sees.
    _replace_file(
        os.path.join(gitdir, "objects", "info", "alternates"),
        os.path.join(repo, ".git", "objects") + "\n",
    )
    _replace_file(os.path.join(gitdir, "commondir"), ".\n")
    for name in ("shallow", os.path.join("info", "grafts")):
        try:
            os.unlink(os.path.join(gitdir, name))
        except OSError:
            pass

    has_ref = _git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], cwd=gitdir)
    if has_ref.returncode == 0:
        temp_ref = f"refs/sandbox-import/{uuid.uuid4().hex[:12]}"
        fetched = _git([
            "-c", "fetch.fsckObjects=true", "fetch", "-q", "--no-tags",
            "--no-write-fetch-head", gitdir, f"+refs/heads/{branch}:{temp_ref}",
        ], cwd=repo, timeout=300)
        if fetched.returncode != 0:
            return f"git refused the commits: {fetched.stderr.strip()[:200]}"
        sha = _git(["rev-parse", "--verify", temp_ref], cwd=repo).stdout.strip()
        moved = _git(["update-ref", f"refs/heads/{branch}", sha], cwd=repo)
        _git(["update-ref", "-d", temp_ref], cwd=repo)
        if moved.returncode != 0:
            return f"could not move the branch: {moved.stderr.strip()[:200]}"
    else:
        log.warning("Sandbox: %s left no refs/heads/%s; nothing imported", worktree, branch)

    # The private index is never trusted: rebuild from what was just imported.
    reset = _git(["reset", "-q"], cwd=worktree)
    if reset.returncode != 0:
        log.warning("Sandbox: could not rebuild the index of %s: %s", worktree, reset.stderr.strip()[:200])
    for name in _MERGE_STATE:
        target = os.path.join(real, name)
        src = os.path.join(gitdir, name)
        if name == "MERGE_HEAD":
            kept = _merge_head_lines(src)
            if kept:
                _replace_file(target, kept)
                continue
        elif os.path.isfile(src) and _merge_head_lines(os.path.join(gitdir, "MERGE_HEAD")):
            with open(src, encoding="utf-8", errors="replace") as fh:
                _replace_file(target, fh.read(64 * 1024))
            continue
        try:
            os.unlink(target)
        except OSError:
            pass
    return None


def _merge_head_lines(path: str) -> str:
    """MERGE_HEAD's text if every line is a commit id, or ``""``."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read(4096).split()
    except OSError:
        return ""
    if lines and all(_HEX40.match(line) for line in lines):
        return "\n".join(lines) + "\n"
    return ""


def forget_worktree(worktree: str) -> None:
    """Drop a worktree's sandbox state, once it is gone or being cleared."""
    shutil.rmtree(state_dir_for(worktree), ignore_errors=True)


def clear_failed_imports(repo_path: str) -> int:
    """`/sandbox off` clears a stuck import, so a repo is never wedged for good."""
    cleared = 0
    root = sandbox_root() / "worktrees"
    if not root.is_dir():
        return 0
    for state in root.iterdir():
        try:
            meta = json.loads((state / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if os.path.realpath(meta.get("repo", "")) == os.path.realpath(repo_path) and meta.get("import_failed"):
            shutil.rmtree(state, ignore_errors=True)
            cleared += 1
    return cleared


def _run_process_ids(runs: Path) -> list[int]:
    """Processes started with a path in this run directory, which every session is."""
    prefix = os.fsencode(str(runs) + os.sep)
    pids: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                args = fh.read().split(b"\0")
        except OSError:
            continue
        if any(arg.startswith(prefix) for arg in args):
            pids.append(int(entry))
    return pids


def _kill_leftover_runs(runs: Path, wait_secs: float = 5.0) -> int:
    """Kill a session a hard crash left running. Its session scope outlives the bot,
    and it must be dead before its git directory is trusted."""
    pids = _run_process_ids(runs)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            log.debug("Could not kill leftover sandbox process %s", pid, exc_info=True)
    deadline = time.monotonic() + wait_secs
    while pids and _run_process_ids(runs) and time.monotonic() < deadline:
        time.sleep(0.1)
    return len(pids)


def recover_after_restart() -> list[str]:
    """Check in whatever a dead bot left checked out. Returns one line per problem."""
    problems: list[str] = []
    root = sandbox_root()
    runs = root / "runs"
    killed = _kill_leftover_runs(runs)
    if killed:
        log.warning("Sandbox: killed %d process(es) left over from before the restart", killed)
    if runs.is_dir():
        shutil.rmtree(runs, ignore_errors=True)
    worktrees = root / "worktrees"
    if not worktrees.is_dir():
        return problems
    for state in list(worktrees.iterdir()):
        meta_path = state / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            shutil.rmtree(state, ignore_errors=True)
            continue
        except (OSError, ValueError):
            problems.append(f"{state.name}: unreadable state")
            continue
        worktree = meta.get("worktree", "")
        if meta.get("import_failed"):
            if not os.path.isdir(worktree):
                shutil.rmtree(state, ignore_errors=True)
            continue
        try:
            problem = checkin_worktree(worktree)
            if problem:
                problems.append(f"{worktree}: {problem}")
            else:
                clean_stubs(worktree)
        except Exception as exc:
            problems.append(f"{worktree}: {exc}")
    return problems


# --- Bot-side gates ---------------------------------------------------------

_PROTECTED_DIR_PARTS = {".claude", ".github"}
_PROTECTED_NAMES = {".mcp.json", ".gitattributes", ".gitmodules", ".envrc"}


def is_protected_path(path: str) -> bool:
    parts = path.split("/")
    if _PROTECTED_DIR_PARTS.intersection(parts) or parts[-1] in _PROTECTED_NAMES:
        return True
    # A tracked .env reads as empty inside the sandbox, so `git add -A` blanks it.
    return parts[-1] == ".env" or parts[-1].startswith(".env.")


def protected_changes(repo_path: str, target: str, branch: str) -> list[str] | None:
    """Protected paths this branch changes. None when git could not say."""
    r = _git(
        ["diff", "--no-renames", "--name-only", "-z", f"{target}...{branch}"],
        cwd=repo_path,
    )
    if r.returncode != 0:
        return None
    return sorted(p for p in r.stdout.split("\0") if p and is_protected_path(p))


_DEPLOY_WELL_KNOWN = (
    "Makefile", "makefile", "justfile", "Justfile", "package.json", "Taskfile.yml",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml", "Dockerfile",
)


def deploy_fingerprint(repo_path: str, deploy_config: dict) -> str:
    """A hash of a deploy command and the files it names in the repo.

    Taken when the owner approves. A session in a sandboxed repo can land a change
    to a script the deploy runs outside the sandbox, so a mismatch refuses.
    """
    command = str(deploy_config.get("command") or "")
    cwd = deploy_config.get("cwd") or ""
    # Every setting but the approval itself: the health check runs in a shell too.
    settings = {k: v for k, v in deploy_config.items()
                if k not in ("approved", "approved_fingerprint")}
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True, default=str).encode())
    repo_real = os.path.realpath(repo_path)
    base = os.path.realpath(os.path.join(repo_real, cwd))
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    names = sorted({*tokens, *_DEPLOY_WELL_KNOWN})
    for token in names:
        # Sorted: this hash is compared across restarts, so no set order in it.
        for root in sorted({base, repo_real}):
            path = os.path.realpath(os.path.join(root, token))
            if not is_within(path, repo_real) or not os.path.isfile(path):
                continue
            with open(path, "rb") as fh:
                digest.update(f"{os.path.relpath(path, repo_real)}\0".encode())
                digest.update(hashlib.sha256(fh.read()).digest())
    return digest.hexdigest()
