"""Regression test for the opt-in session sandbox.

Pins the setting, the generated settings, the bot-side gates and the private git
directory with temp dirs and a fake gateway. No session is started and nothing
here touches real keys. The attack run with the real CLI, which is what shows the
settings hold, is `scripts/attack_session_sandbox.py`. Read
docs/design/session-sandbox.md first.

Run: python scripts/test_session_sandbox.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TMP = Path(tempfile.mkdtemp(prefix="sbx-harness-")).resolve()
# Before anything asks where home is: only repos under it can be sandboxed.
os.environ["HOME"] = str(TMP)
for key, value in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"),
                   ("GIT_COMMITTER_NAME", "t"), ("GIT_COMMITTER_EMAIL", "t@t")):
    os.environ[key] = value

from bot import config  # noqa: E402

config.DATA_DIR = TMP / "data"
config.RESULTS_DIR = config.DATA_DIR / "results"
config.PENDING_IMAGES_DIR = config.DATA_DIR / "pending_images"
config.PENDING_IMAGES_DIR.mkdir(parents=True)

from bot.claude import parser, sandbox  # noqa: E402
from bot.claude.hooks import sandbox_paths  # noqa: E402
from bot.claude.provider import get_provider  # noqa: E402
from bot.claude.runner import ClaudeRunner  # noqa: E402
from bot.claude.runner.sandboxing import refused_result  # noqa: E402
from bot.claude.types import Instance, InstanceStatus, InstanceType  # noqa: E402
from bot.discord import deploy_flow  # noqa: E402
from bot.engine import images, repo_desc, sandbox_cmds, watches  # noqa: E402
from bot.engine.directives.dispatch import _execute_bot_commands  # noqa: E402
from bot.engine.step_policy import _load_verify_policy, load_workflow_policy  # noqa: E402
from bot.fsutil import is_env_file, read_contained_text  # noqa: E402
from bot.platform.base import RequestContext  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: object, detail: object = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        print(f"  FAIL: {name} {detail}")


def git(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
    ).stdout.strip()


def new_repo(name: str) -> Path:
    path = TMP / name
    path.mkdir()
    git(path, "init", "-q", "-b", "master")
    (path / "a.txt").write_text("a\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "first")
    return path


def new_store(name: str) -> StateStore:
    return StateStore(TMP / f"{name}-state.json", TMP / f"{name}-results")


def new_instance(repo: Path, inst_id: str = "t-1", **kw) -> Instance:
    defaults = dict(
        id=inst_id, name=None, instance_type=InstanceType.QUERY, prompt="hello",
        repo_name=repo.name, repo_path=str(repo), status=InstanceStatus.RUNNING,
        mode="build", effort="low", is_owner_session=True,
    )
    defaults.update(kw)
    return Instance(**defaults)


def sandbox_on(store: StateStore, repo: Path) -> None:
    store.add_repo(repo.name, str(repo))
    store.set_sandbox_entry(os.path.realpath(repo), {"hosts": ["github.com"]})


def prepare(repo: Path, working_dir: Path | None = None, *, prompt: str | None = None,
            hosts=("github.com",), memory_dir: str | None = None, keep=frozenset()):
    return sandbox.prepare_run(
        instance_id="t-1", repo_path=str(repo), working_dir=str(working_dir or repo),
        hosts=list(hosts), prompt=prompt, other_repos=[], memory_dir=memory_dir,
        keep_names=set(keep),
    )


def add_worktree(repo: Path, name: str, branch: str) -> Path:
    wt = repo / ".worktrees" / name
    git(repo, "worktree", "add", "-q", "-b", branch, str(wt))
    return wt


class FakeMessenger:
    def __init__(self) -> None:
        self.posts: list[str] = []

    async def send_text(self, channel_id, text, buttons=None, silent=False):
        self.posts.append(text)
        return f"m-{len(self.posts)}"

    async def edit_text(self, *a, **k):
        return None

    def escape(self, text):
        return text


# --- The setting ------------------------------------------------------------

def test_setting() -> None:
    repo = new_repo("setting")
    store = new_store("setting")
    store.add_repo("setting", str(repo))
    check("off by default", sandbox.setting_for(store, str(repo)) is None)
    check("off reads as not sandboxed", not sandbox.is_sandboxed(store, str(repo)))

    link = TMP / "setting-link"
    link.symlink_to(repo)
    store.set_sandbox_entry(str(repo.resolve()), {"hosts": ["x.dev"]})
    check("keyed by real path, a link finds it", sandbox.is_sandboxed(store, str(link)))
    check("stored outside the repo", not (repo / ".claude").exists())
    store.save()
    data = json.loads((TMP / "setting-state.json").read_text())
    check("persisted in the state file", str(repo.resolve()) in data["sandboxed_repos"])
    reloaded = new_store("setting")
    check("survives a reload", reloaded.sandbox_entries().get(str(repo.resolve())) == {"hosts": ["x.dev"]})

    store.set_sandbox_entry(str(repo.resolve()), {"hosts": "not a list"})
    try:
        sandbox.setting_for(store, str(repo))
        check("a bad entry raises", False)
    except sandbox.SandboxSettingError:
        check("a bad entry raises", True)
    check("a bad entry counts as sandboxed", sandbox.is_sandboxed(store, str(repo)))

    state = TMP / "corrupt-state.json"
    state.write_text(json.dumps({"sandboxed_repos": ["nope"]}))
    corrupt = StateStore(state, TMP / "corrupt-results")
    check("corrupt setting is not read as off", sandbox.is_sandboxed(corrupt, str(repo)))
    check("corrupt setting keeps refusing after a save", "__corrupt__" in corrupt.sandbox_entries())

    check("no store means off", sandbox.setting_for(None, str(repo)) is None)
    check("a double with no accessors means off", sandbox.setting_for(SimpleNamespace(), str(repo)) is None)

    check("an empty lookup with a sandboxed repo is yes", sandbox.origin_is_sandboxed(store, None))
    check("an empty lookup with none sandboxed is no", not sandbox.origin_is_sandboxed(new_store("none"), None))


def test_repo_allowed() -> None:
    repo = new_repo("allowed")
    check("a plain repo under home is fine", sandbox.check_repo_allowed(str(repo)) is None)
    root = config._PROJECT_ROOT
    check("the bot repo is refused", sandbox.check_repo_allowed(str(root)))
    check("inside the bot repo is refused", sandbox.check_repo_allowed(str(Path(root) / "bot")))
    check("a parent of the bot repo is refused", sandbox.check_repo_allowed(str(Path(root).parent)))
    check("cursor is refused", "cursor" in (sandbox.check_repo_allowed(str(repo), "cursor") or ""))
    plain = TMP / "no-git"
    plain.mkdir()
    check("no .git is refused", sandbox.check_repo_allowed(str(plain)))
    outside = Path(tempfile.mkdtemp(prefix="sbx-outside-", dir="/var/tmp"))
    try:
        check("outside home is refused", sandbox.check_repo_allowed(str(outside)))
    finally:
        shutil.rmtree(outside, ignore_errors=True)

    hooked = new_repo("hooked")
    git(hooked, "config", "core.hooksPath", ".githooks")
    check("a relative hooks path is refused", sandbox.check_repo_allowed(str(hooked)))
    git(hooked, "config", "core.hooksPath", str(hooked / "hooks"))
    check("a hooks path inside the repo is refused", sandbox.check_repo_allowed(str(hooked)))
    git(hooked, "config", "core.hooksPath", "/usr/share/hooks")
    check("a hooks path outside the repo is fine", sandbox.check_repo_allowed(str(hooked)) is None)

    dotted = TMP / ".config" / "dotted"
    dotted.mkdir(parents=True)
    git(dotted, "init", "-q")
    check("a dot directory in home is refused", "dot directory" in (sandbox.check_repo_allowed(str(dotted)) or ""))
    deep = TMP / "work" / ".hidden" / "deep"
    deep.mkdir(parents=True)
    git(deep, "init", "-q")
    check("a dot directory further down is fine", sandbox.check_repo_allowed(str(deep)) is None)

    creds = new_repo("creds")
    git(creds, "remote", "add", "origin", "https://github.com/a/b.git")
    check("a plain remote is fine", sandbox.check_repo_allowed(str(creds)) is None)
    git(creds, "remote", "set-url", "origin", "git@github.com:a/b.git")
    check("an ssh remote is fine", sandbox.check_repo_allowed(str(creds)) is None)
    git(creds, "remote", "set-url", "origin", "https://canarytoken@github.com/a/b.git")
    problem = sandbox.check_repo_allowed(str(creds))
    check("a token in the remote refuses", "credential" in (problem or ""), problem)
    check("the refusal does not print it", "canarytoken" not in (problem or ""), problem)
    with mock.patch.object(shutil, "which", lambda name: f"/usr/bin/{name}"):
        try:
            sandbox.preflight("claude", False, str(creds))
            check("a token added later refuses the run", False)
        except sandbox.SandboxRefused as exc:
            check("a token added later refuses the run", "canarytoken" not in str(exc))
    git(creds, "remote", "set-url", "origin", "https://github.com/a/b.git")
    git(creds, "remote", "set-url", "--add", "--push", "origin", "https://canarytoken@github.com/a/b.git")
    check("a token in a push url refuses", sandbox.check_repo_allowed(str(creds)))
    git(creds, "config", "--unset", "remote.origin.pushurl")
    git(creds, "config", "http.https://github.com/.extraHeader", "AUTHORIZATION: basic canary")
    check("an auth header refuses", "credential" in (sandbox.check_repo_allowed(str(creds)) or ""))
    git(creds, "config", "--unset", "http.https://github.com/.extraHeader")
    git(creds, "config", "url.https://canarytoken@github.com/.insteadOf", "https://github.com/")
    check("a token in insteadOf refuses", sandbox.check_repo_allowed(str(creds)))
    git(creds, "config", "--remove-section", "url.https://canarytoken@github.com/")
    check("clean again", sandbox.check_repo_allowed(str(creds)) is None)


def test_hosts() -> None:
    check("hosts are normalized", sandbox.validate_hosts([" GitHub.com ", "*.npmjs.org", "github.com"])
          == ["github.com", "*.npmjs.org"])
    for bad in ("*", "https://x.dev", "x.dev/path", "1.2.3.4", "localhost", "x.dev:443", "a b.dev"):
        try:
            sandbox.validate_hosts([bad])
            check(f"refuses host {bad}", False)
        except ValueError:
            check(f"refuses host {bad}", True)


def test_commands() -> None:
    repo = new_repo("cmds")
    store = new_store("cmds")
    store.add_repo("cmds", str(repo))
    store.add_repo("bot", str(config._PROJECT_ROOT))
    with mock.patch.object(shutil, "which", lambda name: f"/usr/bin/{name}"):
        check("unknown repo", "No repo" in sandbox_cmds.turn_on(store, "nope", "claude"))
        check("bot repo refused", "overlaps" in sandbox_cmds.turn_on(store, "bot", "claude"))
        check("nothing stored for it", not store.sandbox_entries())
        check("cursor refused", "Not sandboxing" in sandbox_cmds.turn_on(store, "cmds", "cursor"))
        check("nothing stored for cursor", not store.sandbox_entries())
        check("hosts need it on", "not sandboxed" in sandbox_cmds.set_hosts(store, "cmds", ["x.dev"]))
        check("on works", "sandboxed from its next run" in sandbox_cmds.turn_on(store, "cmds", "claude"))
    real = str(repo.resolve())
    check("on stores the default hosts", store.sandbox_entries()[real]["hosts"] == sandbox.DEFAULT_HOSTS)
    check("on hardens git", git(repo, "config", "core.fsmonitor") == "false")
    sandbox_cmds.set_hosts(store, "cmds", ["pypi.org"])
    check("hosts stick", store.sandbox_entries()[real]["hosts"] == ["pypi.org"])
    check("a bad host is refused", "not a host" in sandbox_cmds.set_hosts(store, "cmds", ["*"]))
    check("status lists it", "pypi.org" in sandbox_cmds.status(store, "claude"))
    with mock.patch.object(shutil, "which", lambda name: f"/usr/bin/{name}"):
        sandbox_cmds.turn_on(store, "cmds", "claude")
    check("on again keeps the hosts", store.sandbox_entries()[real]["hosts"] == ["pypi.org"])
    with mock.patch.object(shutil, "which", lambda name: None if name == "bwrap" else f"/usr/bin/{name}"):
        store2 = new_store("cmds2")
        store2.add_repo("cmds", str(repo))
        check("a missing tool refuses", "bwrap" in sandbox_cmds.turn_on(store2, "cmds", "claude"))
    check("off works", "no longer" in sandbox_cmds.turn_off(store, "cmds"))
    check("off clears it", real not in store.sandbox_entries())
    r = subprocess.run(["git", "config", "--get", "core.fsmonitor"], cwd=repo, capture_output=True)
    check("off undoes the hardening", r.returncode != 0)

    broken = new_store("cmds3")
    broken.add_repo("cmds", str(repo))
    broken.set_sandbox_entry("__corrupt__", "junk")
    before = dict(broken.sandbox_entries())
    for reply in (sandbox_cmds.turn_on(broken, "cmds", "claude"), sandbox_cmds.turn_off(broken, "cmds"),
                  sandbox_cmds.set_hosts(broken, "cmds", ["x.dev"]), sandbox_cmds.status(broken, "claude")):
        check("unreadable settings say so", "state.json" in reply, reply)
    check("and nothing is written over them", broken.sandbox_entries() == before)


# --- The generated settings -------------------------------------------------

def test_settings() -> None:
    repo = new_repo("settings")
    (repo / ".env").write_text("X=1\n")
    run = prepare(repo, hosts=("pypi.org",))
    settings = json.loads(Path(run.settings_file).read_text())
    box = settings["sandbox"]
    check("enabled", box["enabled"] is True)
    check("no way out when it cannot start", box["failIfUnavailable"] is True)
    check("no unsandboxed escape hatch", box["allowUnsandboxedCommands"] is False)
    check("hosts are the repo's own", box["network"]["allowedDomains"] == ["pypi.org"])
    check("strict host list", box["network"]["strictAllowlist"] is True)
    fs = box["filesystem"]
    check("home is denied", "~/" in fs["denyRead"])
    check("/tmp is denied", "/tmp" in fs["denyRead"])
    check("the repo's .env is denied by path", str(repo / ".env") in fs["denyRead"])
    check("Grep is denied", "Grep" in settings["permissions"]["deny"])
    check("the working dir can be read", str(repo) in fs["allowRead"])
    check(".claude is denied to Bash", str(repo / ".claude") in fs["denyWrite"])
    check("a query run has .git read-only", str(repo / ".git") in fs["denyWrite"])
    check("a query run cannot reach worktrees", str(repo / ".worktrees") in fs["denyWrite"])
    check(".claude exists for the deny to hold", (repo / ".claude").is_dir())
    perms = settings["permissions"]
    check("reads outside are blocked", perms["blockReadsOutsideWorkingDirectories"] is True)
    for tool in ("WebFetch", "WebSearch", "SendMessage", "RemoteTrigger", "CronCreate", "Skill", "Monitor"):
        check(f"{tool} denied", tool in perms["deny"])

    hooks = settings["hooks"]["PreToolUse"]
    text = json.dumps(hooks)
    check("the path hook is in the settings", "sandbox_paths.py" in text)
    check("a crash in the hook blocks", "|| exit 2" in text)
    check("a query run has no worktree guard to bring", "worktree_guard" not in text)
    check("run dir is outside the repo", not Path(run.run_dir).is_relative_to(repo))
    check("settings file is private", Path(run.settings_file).parent.name == "private")
    sandbox.finish_run(run)
    check("run dir removed after", not Path(run.run_dir).exists())

    wt = add_worktree(repo, "t-s", "s/branch")
    run = prepare(repo, wt)
    settings = json.loads(Path(run.settings_file).read_text())
    fs = settings["sandbox"]["filesystem"]
    # They live in settings.local.json, which a sandboxed run does not load.
    with mock.patch.object(config, "WORKTREE_HOOK_ENABLED", True), mock.patch.object(config, "TEST_MUTEX_ENABLED", True):
        hooks = json.dumps(sandbox._hook_entries("/cfg", str(wt), str(repo), True))
    check("the worktree guard came along", "worktree_guard.py" in hooks)
    check("the test mutex came along", "test_mutex.py" in hooks)
    gitdir = sandbox.private_gitdir(str(wt))
    for name in ("config", "hooks", "commondir"):
        check(f"private gitdir {name} is pinned", os.path.join(gitdir, name) in fs["denyWrite"])
    check("alternates are pinned", os.path.join(gitdir, "objects", "info", "alternates") in fs["denyWrite"])
    check("the gitfile is pinned", str(wt / ".git") in fs["denyWrite"])
    check("the private gitdir is writable", gitdir in fs["allowWrite"])
    check("the real common dir is not", str(repo / ".git") not in fs["allowWrite"])
    sandbox.finish_run(run)


def test_planted_tool_dirs() -> None:
    repo = new_repo("tooldirs")
    (repo / ".env").write_text("X=1\n")
    outside = TMP / "tooldirs-outside"
    outside.mkdir()
    (repo / ".venv").symlink_to(outside)
    (repo / "venv").symlink_to(repo / ".env")
    (repo / "node_modules").mkdir()
    run = prepare(repo)
    allow = json.loads(Path(run.settings_file).read_text())["sandbox"]["filesystem"]["allowRead"]
    check("a real tool dir is readable", str(repo / "node_modules") in allow)
    check("a link out of the repo opens nothing", str(repo / ".venv") not in allow)
    check("a link onto .env does not reopen it", str(repo / "venv") not in allow)
    sandbox.finish_run(run)


def test_attachments_and_memory() -> None:
    repo = new_repo("attach")
    mine = config.PENDING_IMAGES_DIR / ("a" * 32 + ".png")
    other = config.PENDING_IMAGES_DIR / ("b" * 32 + ".png")
    mine.write_bytes(b"mine")
    other.write_bytes(b"other")
    memory = TMP / "memory"
    memory.mkdir()
    run = prepare(repo, prompt=f"look at {mine}", memory_dir=str(memory))
    settings = json.loads(Path(run.settings_file).read_text())
    copy = run.attachments[str(mine)]
    check("the upload is copied", Path(copy).read_bytes() == b"mine")
    check("only this run's upload", len(list(Path(copy).parent.iterdir())) == 1)
    check("the prompt points at the copy",
          run.localize_prompt(f"look at {mine}") == f"look at {copy}")
    extra = settings["permissions"]["additionalDirectories"]
    pending = str(config.PENDING_IMAGES_DIR)
    # A directory grant reaches Bash over any deny: the shared one must never be in it.
    check("the shared uploads dir is not granted", pending not in extra)
    check("the shared uploads dir is not readable",
          pending not in settings["sandbox"]["filesystem"]["allowRead"])
    check("the copies are granted", str(Path(copy).parent) in extra)
    check("the copies are read-only", str(Path(copy).parent) in settings["sandbox"]["filesystem"]["denyWrite"])
    check("memory can be read", str(memory) in extra)
    check("memory is read-only to Bash", str(memory) in settings["sandbox"]["filesystem"]["denyWrite"])
    cfg = json.loads((Path(run.private_dir) / "paths.json").read_text())
    check("the hook allows the copies", str(Path(copy).parent) in cfg["read"])
    check("the hook allows memory", str(memory) in cfg["read"])
    check("the hook does not write memory", str(memory) not in cfg["write"])
    sandbox.finish_run(run)

    run = prepare(repo, prompt="nothing attached", memory_dir=None)
    check("no attachments, no dir", not (Path(run.run_dir) / "attachments").exists())
    sandbox.finish_run(run)
    link = config.PENDING_IMAGES_DIR / ("c" * 32 + ".png")
    link.symlink_to(TMP / "nowhere")
    run = prepare(repo, prompt=f"{link} and {config.PENDING_IMAGES_DIR / ('d' * 32 + '.png')}")
    check("a link or a reaped upload is not copied", run.attachments == {})
    sandbox.finish_run(run)
    for path in (mine, other, link):
        path.unlink()


def test_env() -> None:
    fake_env = {"FOO_API_KEY": "x", "GH_TOKEN": "x", "PATH": "/bin", "HOME": "/h",
                "CLAUDE_CODE_OAUTH_TOKEN": "x", "DBUS_SESSION_BUS_ADDRESS": "x",
                "SSH_AUTH_SOCK": "/s", "EDITOR": "vi"}
    with mock.patch.object(sandbox, "env_file_names", lambda: {"BOT_CONFIG_VALUE", "DISCORD_TOKEN"}):
        strip, deny = sandbox.names_to_strip(fake_env)
    for name in ("FOO_API_KEY", "GH_TOKEN", "SSH_AUTH_SOCK", "BOT_CONFIG_VALUE", "DISCORD_TOKEN"):
        check(f"{name} is stripped", name in strip)
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "DBUS_SESSION_BUS_ADDRESS"):
        check(f"{name} stays for the CLI", name not in strip)
        check(f"{name} is hidden from Bash", name in deny)
    check("harmless names are left", "EDITOR" not in strip and "PATH" not in strip)

    repo = new_repo("env")
    with mock.patch.object(sandbox, "names_to_strip", lambda: (["VAULT_KEY", "GH_TOKEN"], ["VAULT_KEY", "GH_TOKEN"])):
        run = prepare(repo, keep={"VAULT_KEY"})
    check("a granted vault key stays", "VAULT_KEY" not in run.strip_names and "GH_TOKEN" in run.strip_names)
    denied = json.loads(Path(run.settings_file).read_text())["sandbox"]["credentials"]["envVars"]
    check("a granted vault key reaches Bash", "VAULT_KEY" not in [e["name"] for e in denied])
    env = {"GH_TOKEN": "x", "PATH": "/bin"}
    sandbox.apply_env(env, run)
    check("apply_env strips", "GH_TOKEN" not in env)
    check("apply_env points temp at the run", env["CLAUDE_CODE_TMPDIR"] == run.tmp_dir == env["TMPDIR"])
    check("caches are per repo", env["PIP_CACHE_DIR"].startswith(run.cache_dir))
    sandbox.finish_run(run)
    run2 = prepare(repo)
    check("the same cache on the next run", run2.cache_dir == run.cache_dir)
    check("a different run dir on the next run", run2.run_dir != run.run_dir)
    sandbox.finish_run(run2)


def test_instructions() -> None:
    repo = new_repo("instr")
    (repo / "CLAUDE.md").write_text("project rule")
    run = prepare(repo)
    check("project rules reach the prompt", "project rule" in run.system_prompt_note())
    check("the note says it is sandboxed", "This session runs in a sandbox" in run.system_prompt_note())
    check("the note lists the hosts", "github.com" in run.system_prompt_note())
    check("no em dash in the note", "\u2014" not in run.system_prompt_note())
    sandbox.finish_run(run)
    canary = TMP / "instr-canary.txt"
    canary.write_text("canary text")
    (repo / "CLAUDE.md").unlink()
    (repo / "CLAUDE.md").symlink_to(canary)
    check("a planted link is not followed", "canary text" not in sandbox.project_instructions(str(repo)))


def test_env_files_never_read() -> None:
    for name in (".env", ".env.local", ".env.production"):
        check(f"{name} is an env file", is_env_file(name))
    for name in (".env.example", ".env.sample", ".env.template", "env", ".envrc", ".environment"):
        check(f"{name} is not", not is_env_file(name))

    repo = new_repo("envreads")
    (repo / ".env").write_text("CANARY=envcanary\n")
    (repo / ".env.example").write_text("CANARY=shown\n")
    (repo / "CLAUDE.md").symlink_to(repo / ".env")
    (repo / "README.md").symlink_to(repo / ".env")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "test.json").symlink_to(repo / ".env")
    check("not as project instructions", "envcanary" not in sandbox.project_instructions(str(repo)))
    check("not straight", read_contained_text(repo, ".env") is None)
    check("not through a link", read_contained_text(repo, ".claude/test.json") is None)
    check("an example is fine", "shown" in (read_contained_text(repo, ".env.example") or ""))
    found = repo_desc.resolve_repo_description_with_source(str(repo))
    check("not as the blurb", "envcanary" not in str(found), found)
    (repo / "README.md").unlink()
    (repo / "README.md").write_text("# Readme\n\nA real blurb for this repo.\n")
    found = repo_desc.resolve_repo_description_with_source(str(repo))
    check("a real readme still is", "real blurb" in str(found), found)
    (repo / "fifo").unlink(missing_ok=True)
    os.mkfifo(repo / "fifo")
    check("a FIFO does not hang the reader", read_contained_text(repo, "fifo") is None)


# --- Provider and preflight -------------------------------------------------

def test_provider_flags() -> None:
    provider = get_provider("claude")
    inst = new_instance(new_repo("flags"))
    kwargs = dict(
        binary="claude", system_prompt_file=None, system_prompt_inline=None,
        api_fallback=False, api_key_file=None,
    )
    plain = provider.build_command(inst, **kwargs)
    check("a normal run has its usual sources", "user,project,local" in plain)
    check("a normal run has no strict MCP flag", "--strict-mcp-config" not in plain)
    boxed = provider.build_command(inst, **kwargs, sandbox_settings_file="/x/settings.json")
    check("a sandboxed run reads user settings only",
          boxed[boxed.index("--setting-sources") + 1] == "user")
    check("a sandboxed run ignores repo MCP", "--strict-mcp-config" in boxed)
    check("one settings file", boxed.count("--settings") == 1 and "/x/settings.json" in boxed)


def test_preflight() -> None:
    repo = new_repo("pre")

    def refused(*args) -> str:
        try:
            sandbox.preflight(*args)
        except sandbox.SandboxRefused as exc:
            return str(exc)
        return ""

    with mock.patch.object(shutil, "which", lambda name: f"/usr/bin/{name}"):
        check("a good repo passes", refused("claude", False, str(repo)) == "")
        check("another provider refuses", "cursor" in refused("cursor", False, str(repo)))
        check("API fallback refuses", "API fallback" in refused("claude", True, str(repo)))
        with mock.patch.object(sandbox, "_HOOK_SCRIPT", TMP / "no-hook.py"):
            check("a missing hook refuses", "hook" in refused("claude", False, str(repo)))
    for tool in ("bwrap", "socat"):
        with mock.patch.object(shutil, "which", lambda name, t=tool: None if name == t else f"/usr/bin/{name}"):
            check(f"missing {tool} refuses", tool in refused("claude", False, str(repo)))


def test_failure_wording() -> None:
    # Load-bearing: without these a refused run has the shape of an account falling
    # over, and recovery hands it to the backup subscription.
    check("the refusal phrase did not drift", sandbox.REFUSAL_PHRASE == "Session sandbox refused this run")
    check("a refusal is account-agnostic",
          parser.is_account_agnostic_error(f"{sandbox.REFUSAL_PHRASE}: bwrap is not installed"))
    check("the CLI's wording is account-agnostic", parser.is_account_agnostic_error(
        "Error: sandbox required but unavailable: sandbox is enabled but dependencies are missing"))
    inst = new_instance(new_repo("wording"), session_id="s-1")
    result = refused_result(inst, "because")
    check("a refused result is an error", result.is_error)
    check("a refused result carries the phrase", sandbox.REFUSAL_PHRASE in result.error_message)
    check("a refused result keeps the session", result.session_id == "s-1")


def test_run_refused() -> None:
    repo = new_repo("refused")
    store = new_store("refused")
    runner = ClaudeRunner(store=store)
    inst = new_instance(repo)
    store._instances[inst.id] = inst

    async def prep(provider, api_fallback, wd=str(repo)):
        return await runner._sandbox_prepare(inst, provider, api_fallback, wd)

    check("an unsandboxed repo is untouched", asyncio.run(prep(runner.provider, False)) is None)
    store.add_repo("refused", str(repo))
    store.set_sandbox_entry(str(repo.resolve()), {"hosts": "bad"})
    result = asyncio.run(prep(runner.provider, False))
    check("an unreadable setting refuses", result.is_error and sandbox.REFUSAL_PHRASE in result.error_message)
    store.set_sandbox_entry(str(repo.resolve()), {"hosts": []})
    with mock.patch.object(shutil, "which", lambda name: f"/usr/bin/{name}"):
        result = asyncio.run(prep(runner.provider, True))
        check("API fallback refuses the run", result.is_error and "API fallback" in result.error_message)
        result = asyncio.run(prep(get_provider("cursor"), False))
        check("a provider switch refuses the run", result.is_error and "cursor" in result.error_message)
        result = asyncio.run(prep(runner.provider, False, None))
        check("no working directory refuses", result.is_error)
        result = asyncio.run(prep(runner.provider, False))
        check("a good run prepares", isinstance(result, sandbox.SandboxRun))
        sandbox.finish_run(result)


# --- The path hook ----------------------------------------------------------

def test_hook() -> None:
    wd = TMP / "hook-wd"
    wd.mkdir()
    (wd / ".env").write_text("X=1")
    tmp = TMP / "hook-tmp"
    tmp.mkdir()
    memory = TMP / "hook-memory"
    memory.mkdir()
    secret = TMP / "hook-secret.txt"
    secret.write_text("s")
    (wd / "link").symlink_to(secret)
    cfg = {"cwd": str(wd), "read": [str(wd), str(tmp), str(memory)], "write": [str(wd), str(tmp)],
           "deny_read": [str(wd / ".env")]}
    allow = sandbox_paths.check

    check("read inside", allow("Read", {"file_path": str(wd / "a")}, cfg) is None)
    check("read relative", allow("Read", {"file_path": "a"}, cfg) is None)
    check("read memory", allow("Read", {"file_path": str(memory / "m.md")}, cfg) is None)
    check("read outside", allow("Read", {"file_path": str(secret)}, cfg))
    check("read through a link", allow("Read", {"file_path": str(wd / "link")}, cfg))
    check("read climbing", allow("Read", {"file_path": str(wd / ".." / "hook-secret.txt")}, cfg))
    check("read home", allow("Read", {"file_path": "~/.ssh/id_rsa"}, cfg))
    check("read .env", allow("Read", {"file_path": str(wd / ".env")}, cfg))
    check("edit .env", allow("Edit", {"file_path": str(wd / ".env")}, cfg))
    check("write .env", allow("Write", {"file_path": str(wd / ".env")}, cfg))
    (wd / "envlink").symlink_to(wd / ".env")
    check("edit .env through a link", allow("Edit", {"file_path": str(wd / "envlink")}, cfg))
    check("grep with no path", allow("Grep", {"pattern": "x"}, cfg) is None)
    check("grep outside", allow("Grep", {"pattern": "x", "path": "/etc"}, cfg))
    check("grep glob climbing", allow("Grep", {"pattern": "x", "glob": "../../**"}, cfg))
    check("glob relative", allow("Glob", {"pattern": "**/*.py"}, cfg) is None)
    check("glob absolute", allow("Glob", {"pattern": "/etc/*"}, cfg))
    check("glob home", allow("Glob", {"pattern": "~/.ssh/*"}, cfg))
    check("glob climbing", allow("Glob", {"pattern": "../*"}, cfg))
    check("write inside", allow("Write", {"file_path": str(wd / "b")}, cfg) is None)
    check("write temp", allow("Write", {"file_path": str(tmp / "b")}, cfg) is None)
    check("write memory", allow("Write", {"file_path": str(memory / "m.md")}, cfg))
    check("write outside", allow("Write", {"file_path": str(secret)}, cfg))
    check("write through a link", allow("Edit", {"file_path": str(wd / "link")}, cfg))
    for rel in (".git/hooks/pre-commit", ".claude/settings.json", ".claude/workflow.json",
                "sub/.claude/x", ".mcp.json", "sub/.mcp.json"):
        check(f"write {rel}", allow("Write", {"file_path": str(wd / rel)}, cfg))
    check("a notebook outside", allow("NotebookEdit", {"notebook_path": str(secret)}, cfg))
    check("a write tool with no path", allow("Write", {}, cfg))

    # The whole script: any failure has to come out as exit 2.
    script = Path(sandbox_paths.__file__)
    cfg_file = TMP / "hook-cfg.json"
    cfg_file.write_text(json.dumps(cfg))

    def run_hook(config_arg: str, stdin: str) -> int:
        return subprocess.run(
            [sys.executable, "-I", str(script), config_arg], input=stdin, text=True, capture_output=True,
        ).returncode

    good = json.dumps({"tool_name": "Read", "tool_input": {"file_path": str(wd / "a")}})
    bad = json.dumps({"tool_name": "Read", "tool_input": {"file_path": str(secret)}})
    check("hook allows with 0", run_hook(str(cfg_file), good) == 0)
    check("hook blocks with 2", run_hook(str(cfg_file), bad) == 2)
    check("a missing config blocks", run_hook(str(TMP / "none.json"), good) == 2)
    check("bad stdin blocks", run_hook(str(cfg_file), "not json") == 2)


# --- Stubs ------------------------------------------------------------------

def test_stubs() -> None:
    repo = new_repo("stubs")
    git_dir = repo / ".git"
    sandbox.add_stub_excludes(str(git_dir))
    once = (git_dir / "info" / "exclude").read_text()
    sandbox.add_stub_excludes(str(git_dir))
    check("excludes go in once", (git_dir / "info" / "exclude").read_text() == once)
    check("excludes cover the stubs", all(f"/{n}" in once for n in sandbox.STUB_FILES))

    (repo / ".bashrc").write_text("")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "agents").write_text("")
    (repo / ".claude" / ".cc-writes").mkdir()
    status = git(repo, "status", "--short")
    check("stubs do not show in git status", ".bashrc" not in status and ".claude" not in status, status)
    (repo / ".profile").write_text("mine")
    (repo / ".zshrc").write_text("")
    git(repo, "add", "-f", ".zshrc")
    git(repo, "commit", "-qm", "tracked empty")
    removed = sandbox.clean_stubs(str(repo))
    check("empty untracked go", not (repo / ".bashrc").exists() and not (repo / ".claude" / "agents").exists())
    check("the write journal goes", not (repo / ".claude" / ".cc-writes").exists())
    check("the empty .claude goes", not (repo / ".claude").exists())
    check("removal is counted", removed == 2)
    check("one with content stays", (repo / ".profile").read_text() == "mine")
    check("a tracked empty one stays", (repo / ".zshrc").exists())

    (repo / ".claude").mkdir()
    (repo / ".claude" / "keep.json").write_text("{}")
    sandbox.clean_stubs(str(repo))
    check("a real .claude stays", (repo / ".claude" / "keep.json").exists())


def test_cleanup_waits_for_other_sessions() -> None:
    repo = new_repo("shared")
    store = new_store("shared")
    runner = ClaudeRunner(store=store)
    mine = new_instance(repo, "q-mine")
    other = new_instance(repo, "q-other")
    elsewhere = new_instance(new_repo("elsewhere"), "q-else")
    store._instances.update({i.id: i for i in (mine, other, elsewhere)})
    run = SimpleNamespace(instance_id="q-mine", repo_path=str(repo))
    runner._processes = {"q-mine": object()}
    check("alone: stubs can go", not runner._sandbox_other_live(run))
    runner._processes = {"q-mine": object(), "q-else": object()}
    check("a session elsewhere does not matter", not runner._sandbox_other_live(run))
    runner._processes = {"q-mine": object(), "q-other": object()}
    check("a session in this repo holds it", runner._sandbox_other_live(run))
    runner._processes = {"q-mine": object(), "q-gone": object()}
    check("an unknown session holds it", runner._sandbox_other_live(run))


# --- The private git directory ----------------------------------------------

def test_private_gitdir() -> None:
    repo = new_repo("gitdir")
    git(repo, "branch", "bystander")
    master_before = git(repo, "rev-parse", "master")
    wt = add_worktree(repo, "t-g", "g/branch")
    gitfile = (wt / ".git").read_text()

    sandbox.checkout_worktree(str(wt), str(repo), "t-g")
    private = sandbox.private_gitdir(str(wt))
    check("the worktree points at the private dir", private in (wt / ".git").read_text())
    check("on its branch", git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "g/branch")
    (wt / "n.txt").write_text("n\n")
    git(wt, "add", "n.txt")
    git(wt, "commit", "-qm", "work")
    work = git(wt, "rev-parse", "HEAD")
    # What a session could try with everything it can write.
    git(wt, "update-ref", "refs/heads/master", work)
    git(wt, "update-ref", "refs/heads/planted", work)
    git(wt, "tag", "planted-tag")
    check("nothing has moved yet", git(repo, "rev-parse", "master") == master_before)

    problem = sandbox.checkin_worktree(str(wt))
    check("check-in is clean", problem is None, problem)
    check("the gitfile comes back", (wt / ".git").read_text() == gitfile)
    check("the branch comes home", git(repo, "rev-parse", "g/branch") == work)
    check("master did not move", git(repo, "rev-parse", "master") == master_before)
    refs = git(repo, "for-each-ref", "--format=%(refname)")
    check("a planted branch stays behind", "planted" not in refs, refs)
    check("the private dir is gone", not Path(private).exists())
    check("status works after", git(wt, "status", "--short") is not None)
    check("the second check-in has nothing to do", sandbox.checkin_worktree(str(wt)) is None)


def test_private_gitdir_attacks() -> None:
    repo = new_repo("gitattack")
    canary = TMP / "gitattack-canary.txt"
    canary.write_text("canary")

    def attacked(name: str, attack) -> str | None:
        wt = add_worktree(repo, name, f"x/{name}")
        sandbox.checkout_worktree(str(wt), str(repo), name)
        attack(wt, Path(sandbox.private_gitdir(str(wt))))
        problem = sandbox.checkin_worktree(str(wt))
        check(f"{name}: gitfile restored", str(repo) in (wt / ".git").read_text())
        if problem:
            try:
                sandbox.checkout_worktree(str(wt), str(repo), name)
                check(f"{name}: next run refused", False)
            except sandbox.SandboxRefused:
                check(f"{name}: next run refused", True)
        return problem

    check("a planted link refuses", attacked(
        "link", lambda wt, g: (g / "planted").symlink_to(canary)))
    check("a planted FIFO refuses", attacked(
        "fifo", lambda wt, g: os.mkfifo(g / "planted")))
    check("a changed config refuses", attacked(
        "cfg", lambda wt, g: (g / "config").open("a").write("[core]\n\tfsmonitor = /bin/false\n")))

    def lie_in_merge_head(wt: Path, g: Path) -> None:
        (g / "MERGE_HEAD").write_text("../../etc/passwd\n")

    wt_clean = attacked("mergehead", lie_in_merge_head)
    check("a junk MERGE_HEAD is no failure, only dropped", wt_clean is None)
    real = repo / ".git" / "worktrees" / "mergehead" / "MERGE_HEAD"
    check("a junk MERGE_HEAD does not come back", not real.exists())

    def rewrite_alternates(wt: Path, g: Path) -> None:
        (g / "objects" / "info" / "alternates").write_text(str(TMP / "elsewhere") + "\n")

    check("rewritten alternates are overwritten, not trusted", attacked("alt", rewrite_alternates) is None)

    wt = add_worktree(repo, "redirect", "x/redirect")
    (wt / ".git").write_text(f"gitdir: {canary}\n")
    try:
        sandbox.checkout_worktree(str(wt), str(repo), "redirect")
        check("a redirected gitfile refuses", False)
    except sandbox.SandboxRefused:
        check("a redirected gitfile refuses", True)

    wt = add_worktree(repo, "linkfile", "x/linkfile")
    text = (wt / ".git").read_text()
    (wt / ".git").unlink()
    (wt / ".git").symlink_to(canary)
    try:
        sandbox.checkout_worktree(str(wt), str(repo), "linkfile")
        check("a gitfile link refuses", False)
    except sandbox.SandboxRefused:
        check("a gitfile link refuses", True)
    (wt / ".git").unlink()
    (wt / ".git").write_text(text)


def test_failed_checkin_keeps_the_commits() -> None:
    repo = new_repo("keep")
    wt = add_worktree(repo, "t-k", "k/branch")
    sandbox.checkout_worktree(str(wt), str(repo), "t-k")
    (wt / "k.txt").write_text("k\n")
    git(wt, "add", "k.txt")
    git(wt, "commit", "-qm", "only copy")
    work = git(wt, "rev-parse", "HEAD")
    gitdir = Path(sandbox.private_gitdir(str(wt)))
    (gitdir / "planted").symlink_to("/etc/passwd")
    # What a crash leaves: nothing checked it in, so the next run is the first to try.
    try:
        sandbox.checkout_worktree(str(wt), str(repo), "t-k")
        check("a next run on a bad gitdir refuses", False)
    except sandbox.SandboxRefused as exc:
        check("a next run on a bad gitdir refuses", "planted" in str(exc), exc)
    check("the private gitdir is not wiped", git(gitdir, "cat-file", "-t", work) == "commit")
    state = json.loads((sandbox.state_dir_for(str(wt)) / "meta.json").read_text())
    check("the failure stays recorded", state.get("import_failed"), state)
    check("/sandbox off clears it", sandbox.clear_failed_imports(str(repo)) == 1)


def test_recovery_and_clearing() -> None:
    repo = new_repo("recover")
    wt = add_worktree(repo, "t-r", "r/branch")
    sandbox.checkout_worktree(str(wt), str(repo), "t-r")
    (wt / "n.txt").write_text("n\n")
    git(wt, "add", "n.txt")
    git(wt, "commit", "-qm", "before the crash")
    work = git(wt, "rev-parse", "HEAD")
    runs = sandbox.sandbox_root() / "runs" / "dead-run"
    runs.mkdir(parents=True)
    check("no problem after a crash", sandbox.recover_after_restart() == [])
    check("the branch comes home after a crash", git(repo, "rev-parse", "r/branch") == work)
    check("the worktree is put back", str(repo) in (wt / ".git").read_text())
    check("dead run dirs are removed", not runs.exists())

    wt2 = add_worktree(repo, "t-r2", "r/branch2")
    sandbox.checkout_worktree(str(wt2), str(repo), "t-r2")
    (Path(sandbox.private_gitdir(str(wt2))) / "planted").symlink_to("/etc/passwd")
    check("a failed import reports", sandbox.checkin_worktree(str(wt2)))
    check("a failed import is not re-tried on boot", sandbox.recover_after_restart() == [])
    try:
        sandbox.checkout_worktree(str(wt2), str(repo), "t-r2")
        check("a failed import blocks the next run", False)
    except sandbox.SandboxRefused:
        check("a failed import blocks the next run", True)
    check("/sandbox off clears it", sandbox.clear_failed_imports(str(repo)) == 1)
    sandbox.checkout_worktree(str(wt2), str(repo), "t-r2")
    check("then it runs again", sandbox.private_gitdir(str(wt2)) in (wt2 / ".git").read_text())
    sandbox.checkin_worktree(str(wt2))

    # The setting's key is the real path, and a registered path need not be.
    linked = TMP / "recover-link"
    linked.symlink_to(repo)
    wt3 = add_worktree(repo, "t-r3", "r/branch3")
    sandbox.checkout_worktree(str(wt3), str(linked), "t-r3")
    (Path(sandbox.private_gitdir(str(wt3))) / "planted").symlink_to("/etc/passwd")
    check("a linked repo's import fails too", sandbox.checkin_worktree(str(wt3)))
    check("/sandbox off finds it by real path", sandbox.clear_failed_imports(os.path.realpath(linked)) == 1)

    # An exception in the import is recorded too, or nothing could clear it.
    wt4 = add_worktree(repo, "t-r4", "r/branch4")
    sandbox.checkout_worktree(str(wt4), str(repo), "t-r4")
    (Path(sandbox.private_gitdir(str(wt4))) / "config").unlink()
    check("an exception comes back as a problem", sandbox.checkin_worktree(str(wt4)))
    check("the gitfile is back", str(repo) in (wt4 / ".git").read_text())
    try:
        sandbox.checkout_worktree(str(wt4), str(repo), "t-r4")
        check("it blocks the next run", False)
    except sandbox.SandboxRefused:
        check("it blocks the next run", True)
    check("/sandbox off clears that as well", sandbox.clear_failed_imports(str(repo)) == 1)


def test_boot_kills_leftover_sessions() -> None:
    runs = sandbox.sandbox_root() / "runs"
    (runs / "alive" / "private").mkdir(parents=True)
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    alive = subprocess.Popen([*sleeper, str(runs / "alive" / "private" / "settings.json")])
    bystander = subprocess.Popen([*sleeper, str(runs) + "-other/x", "mentions data/sandbox/runs"])
    try:
        check("no problem", sandbox.recover_after_restart() == [])
        check("a leftover session is killed", alive.wait(timeout=10) == -9)
        check("nothing else is", bystander.poll() is None)
        check("its run dir is gone", not (runs / "alive").exists())
    finally:
        for proc in (alive, bystander):
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    # A run started during startup would have its directory deleted under it.
    source = (Path(config._PROJECT_ROOT) / "bot" / "app.py").read_text()
    recover = source.index("boot_recovery.recover_sandboxed_worktrees()")
    check("recovery runs before Discord starts", recover < source.index("await _start_discord("))
    check("right after orphans are marked", source.index("store.mark_orphans()") < recover)


# --- Bot-side gates ---------------------------------------------------------

def test_merge_gate() -> None:
    for path in (".claude/x", "sub/.claude/x", ".github/workflows/a.yml", ".mcp.json", "sub/.mcp.json",
                 ".gitattributes", ".gitmodules", ".envrc", ".env", "sub/.env", ".env.local"):
        check(f"{path} needs the owner", sandbox.is_protected_path(path))
    for path in ("src/a.py", "README.md", "env.txt", "claude/x", ".environment"):
        check(f"{path} does not", not sandbox.is_protected_path(path))

    repo = new_repo("mergegate")
    git(repo, "checkout", "-qb", "feature")
    (repo / "src.txt").write_text("x")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "plain change")
    check("an ordinary branch is free", sandbox.protected_changes(str(repo), "master", "feature") == [])
    (repo / ".claude").mkdir()
    (repo / ".claude" / "workflow.json").write_text("{}")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "self grant")
    check("a self-grant is listed", sandbox.protected_changes(str(repo), "master", "feature") == [".claude/workflow.json"])
    git(repo, "mv", ".claude/workflow.json", "plain.json")
    git(repo, "commit", "-qm", "rename out")
    git(repo, "checkout", "-q", "master")
    check("git failing is not read as clean", sandbox.protected_changes(str(repo), "master", "no-such-branch") is None)

    store = new_store("mergegate")
    runner = ClaudeRunner(store=store)
    inst = new_instance(repo, branch="feature", original_branch="master")
    check("an unsandboxed repo has no gate", runner._sandbox_merge_block(inst) is None)
    sandbox_on(store, repo)
    git(repo, "checkout", "-qb", "gated")
    (repo / ".mcp.json").write_text("{}")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "mcp")
    git(repo, "checkout", "-q", "master")
    inst = new_instance(repo, branch="gated", original_branch="master")
    block = runner._sandbox_merge_block(inst)
    check("a sandboxed repo's branch is held", block and ".mcp.json" in block, block)
    check("the failure kind reaches the buttons", runner._last_merge_failure_kind.get(inst.id) == "sandbox_protected")
    with mock.patch.object(runner, "_merge_branch_sync", lambda instance: "merged"):
        check("an unattended merge is held", ".mcp.json" in asyncio.run(runner.merge_branch(inst)))
        check("the owner's own merge goes through",
              asyncio.run(runner.merge_branch(inst, owner_approved=True)) == "merged")


def test_policy() -> None:
    repo = new_repo("policy")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "workflow.json").write_text('{"autonomy": "ship"}')
    (repo / ".claude" / "test.json").write_text('{"verify_policy": "warn"}')
    check("off: the repo's policy is read", load_workflow_policy(str(repo))["autonomy"] == "ship")
    check("on: a self-grant is ignored", load_workflow_policy(str(repo), sandboxed=True)["autonomy"] == "hold")
    inst = new_instance(repo)
    check("off: the verify policy is read", _load_verify_policy(inst) == "warn")
    check("on: a self-written verify policy is ignored", _load_verify_policy(inst, sandboxed=True) == "block_fail")


def test_deploy_fingerprint() -> None:
    repo = new_repo("deploy")
    (repo / "deploy.sh").write_text("echo one\n")
    cfg = {"command": "./deploy.sh", "approved": True}
    first = sandbox.deploy_fingerprint(str(repo), cfg)
    check("stable", first == sandbox.deploy_fingerprint(str(repo), dict(cfg)))
    check("the approval itself is not part of it",
          first == sandbox.deploy_fingerprint(str(repo), {**cfg, "approved": False, "approved_fingerprint": "x"}))
    check("a changed command changes it", first != sandbox.deploy_fingerprint(str(repo), {**cfg, "command": "./deploy.sh --x"}))
    check("a changed health check changes it", first != sandbox.deploy_fingerprint(str(repo), {**cfg, "health": "curl x"}))
    (repo / "deploy.sh").write_text("echo two\n")
    check("a changed script changes it", first != sandbox.deploy_fingerprint(str(repo), cfg))
    (repo / "deploy.sh").write_text("echo one\n")
    (repo / "Makefile").write_text("all:\n")
    check("a changed well-known file changes it", first != sandbox.deploy_fingerprint(str(repo), cfg))

    linked = TMP / "deploy-link"
    linked.symlink_to(repo)
    check("the same through a link", sandbox.deploy_fingerprint(str(linked), cfg) == sandbox.deploy_fingerprint(str(repo), cfg))
    (repo / "sub").mkdir()
    (repo / "sub" / "deploy.sh").write_text("echo sub\n")
    with_cwd = {**cfg, "cwd": "sub"}
    check("cwd through a link", sandbox.deploy_fingerprint(str(linked), with_cwd) == sandbox.deploy_fingerprint(str(repo), with_cwd))
    # Compared across restarts, and each process hashes strings differently.
    code = (
        "import json, sys; from bot.claude import sandbox; "
        "print(sandbox.deploy_fingerprint(sys.argv[1], json.loads(sys.argv[2])))"
    )
    seen = {
        subprocess.run(
            [sys.executable, "-c", code, str(repo), json.dumps(with_cwd)],
            cwd=config._PROJECT_ROOT, capture_output=True, text=True, check=True,
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
        ).stdout.strip()
        for seed in range(1, 7)
    }
    check("stable across restarts", len(seen) == 1, seen)


def test_deploy_refusal() -> None:
    repo = new_repo("deployrun")
    (repo / "deploy.sh").write_text("echo one\n")
    store = new_store("deployrun")
    sandbox_on(store, repo)
    cfg = {"command": "./deploy.sh", "approved": True}
    cfg["approved_fingerprint"] = sandbox.deploy_fingerprint(str(repo), cfg)
    store.set_deploy_config(repo.name, cfg)

    async def refresh(name) -> None:
        return None

    bot = SimpleNamespace(_store=store, _forums=SimpleNamespace(refresh_control_room=refresh))
    (repo / "deploy.sh").write_text("echo changed\n")
    ok, _, message = asyncio.run(deploy_flow.execute_deploy(bot, repo.name, store.get_deploy_config(repo.name)))
    check("a changed script is refused", not ok and "refused" in message, message)
    stored = store.get_deploy_config(repo.name)
    check("the approval is gone", not stored.get("approved") and "approved_fingerprint" not in stored, stored)


def test_directives() -> None:
    repo = new_repo("direct")
    twin = new_repo("direct-twin")
    plain = new_repo("direct-plain")
    store = new_store("direct")
    sandbox_on(store, repo)
    sandbox_on(store, twin)
    store.add_repo("direct-plain", str(plain))
    inst = new_instance(repo, "q-d")
    store._instances[inst.id] = inst
    runner = SimpleNamespace(
        active_instance_for_session=lambda *a, **k: inst.id,
        active_instance_for_channel=lambda *a, **k: inst.id,
    )
    spawned: list = []

    async def fake_spawn(args):
        spawned.append(args)
        return SimpleNamespace(ok=True, error=None, thread_id="th-1", thread_url="u", instance_id="q-new")

    def ctx_for(messenger):
        return RequestContext(
            messenger=messenger, channel_id="c-1", platform="test", store=store, runner=runner,
            spawn_session=fake_spawn, thread_has_guest=False,
        )

    async def run(text: str, source=inst) -> FakeMessenger:
        messenger = FakeMessenger()
        await _execute_bot_commands(ctx_for(messenger), text, source_inst=source)
        return messenger

    for action in (f"add evil {TMP}", "create evil", "switch direct-plain", "hide direct", "unhide direct"):
        before = (dict(store.list_repos()), store.get_active_repo())
        m = asyncio.run(run(f"[BOT_CMD: /repo {action}]"))
        after = (dict(store.list_repos()), store.get_active_repo())
        check(f"/repo {action} changes nothing", before == after)
        check(f"/repo {action} says so", any("sandboxed" in p for p in m.posts), m.posts)

    body = "\n~~~spawn\nA child's brief.\n~~~\n"
    m = asyncio.run(run(f'[BOT_CMD: /spawn repo=direct-plain title="Out"]{body}'))
    check("/spawn to an unsandboxed repo is refused", not spawned and any("/spawn refused" in p for p in m.posts), m.posts)
    m = asyncio.run(run(f'[BOT_CMD: /spawn repo=direct-twin title="In"]{body}'))
    check("/spawn to a sandboxed repo goes", len(spawned) == 1, m.posts)

    spawned.clear()
    plain_inst = new_instance(plain, "q-p")
    store._instances[plain_inst.id] = plain_inst
    runner.active_instance_for_session = lambda *a, **k: plain_inst.id
    runner.active_instance_for_channel = lambda *a, **k: plain_inst.id
    m = asyncio.run(run(f'[BOT_CMD: /spawn repo=direct-twin title="Fine"]{body}', plain_inst))
    check("an unsandboxed session can still spawn", len(spawned) == 1, m.posts)

    # No sandboxed repo at all: /repo goes on as it always did.
    calm = new_store("direct-calm")
    calm.add_repo("calm", str(plain))
    messenger = FakeMessenger()
    ctx = RequestContext(messenger=messenger, channel_id="c-1", platform="test", store=calm, thread_has_guest=False,
                         runner=SimpleNamespace(active_instance_for_session=lambda *a, **k: None,
                                                active_instance_for_channel=lambda *a, **k: None))
    asyncio.run(_execute_bot_commands(ctx, f"[BOT_CMD: /repo add added {plain}]", source_inst=plain_inst))
    check("/repo works with nothing sandboxed", "added" in calm.list_repos(), messenger.posts)


def test_sensors_skipped() -> None:
    from bot.engine.steps import on_sensors

    repo = new_repo("sense")
    (repo / ".claude").mkdir()
    canary = repo / "sensor-ran"
    (repo / ".claude" / "sensors.json").write_text(json.dumps(
        {"sensors": [{"name": "planted", "command": f"touch {canary}"}]}))
    store = new_store("sense")
    sandbox_on(store, repo)
    inst = new_instance(repo, "t-sense")
    store._instances[inst.id] = inst
    messenger = FakeMessenger()
    ctx = RequestContext(messenger=messenger, channel_id="c-1", platform="test", store=store, runner=None)
    result = asyncio.run(on_sensors(ctx, inst.id))
    check("the chain carries on", result is inst)
    check("a repo's sensor command does not run", not canary.exists())
    check("the thread is told", any("Sensors skipped" in p for p in messenger.posts), messenger.posts)


def test_watch() -> None:
    repo = new_repo("watch")
    canary = TMP / "watch-canary.txt"
    canary.write_text("canary")
    check("a pid is refused", watches.sandbox_refusal({"pid": 1}, str(repo)))
    check("a log outside is refused", watches.sandbox_refusal({"log": str(canary)}, str(repo)))
    check("a climbing log is refused", watches.sandbox_refusal({"log": "../watch-canary.txt"}, str(repo)))
    check("a log inside is fine", watches.sandbox_refusal({"log": "out.log"}, str(repo)) is None)
    check("no log and no pid is fine", watches.sandbox_refusal({}, str(repo)) is None)
    (repo / ".env").write_text("CANARY=envcanary\n")
    (repo / "env.log").symlink_to(repo / ".env")
    check("an env file is refused", watches.sandbox_refusal({"log": ".env"}, str(repo)))
    check("a link onto one is refused", watches.sandbox_refusal({"log": "env.log"}, str(repo)))

    watch = SimpleNamespace(log_path="out.log", repo_path=str(repo), log_root=str(repo))
    (repo / "out.log").write_text("line\n")
    check("a contained log is read", watches.read_log_tail(watch) == "line\n")
    (repo / "out.log").unlink()
    (repo / "out.log").symlink_to(canary)
    check("a log swapped for a link is not read", watches.read_log_tail(watch) == "")
    (repo / "out.log").unlink()
    (repo / "out.log").symlink_to(repo / ".env")
    check("a log swapped for a link to .env is not read", watches.read_log_tail(watch) == "")
    (repo / "out.log").unlink()
    os.mkfifo(repo / "out.log")
    check("a FIFO does not hang", watches.read_log_tail(watch) == "")
    (repo / "out.log").unlink()

    open_watch = SimpleNamespace(log_path=str(canary), repo_path=str(repo), log_root="")
    check("an unsandboxed watch reads as before", watches.read_log_tail(open_watch) == "canary")


def test_image() -> None:
    repo = new_repo("image")
    (repo / "shot.png").write_bytes(b"\x89PNG")
    inst = new_instance(repo)
    uploaded = config.DATA_DIR / "theirs.png"
    uploaded.write_bytes(b"\x89PNG")
    path, _ = images.resolve_image("shot.png", inst, sandboxed=True)
    check("its own image goes", path is not None)
    path, reason = images.resolve_image(str(uploaded), inst, sandboxed=True)
    check("the data dir is refused when sandboxed", path is None, reason)
    check("and the reason does not offer it", "data dir" not in (reason or ""), reason)
    path, _ = images.resolve_image(str(uploaded), inst)
    check("the data dir is allowed otherwise", path is not None)
    uploaded.unlink()


def test_todo() -> None:
    repo = new_repo("todo")
    store = new_store("todo")
    sandbox_on(store, repo)
    canary = TMP / "todo-canary.txt"
    canary.write_text("## Deferred Revisions\n- [ ] canary item\n")
    before = canary.read_text()
    (repo / "TODO.md").symlink_to(canary)
    check("a planted link is not read", store.get_deferred_items("todo") == [])
    store.append_deferred("todo", ["new item"])
    check("a planted link is not written through", canary.read_text() == before)
    store.clear_deferred("todo")
    check("nor cleared through", canary.read_text() == before)
    (repo / "TODO.md").unlink()
    os.mkfifo(repo / "TODO.md")
    check("a planted FIFO does not hang", store.get_deferred_items("todo") == [])
    (repo / "TODO.md").unlink()
    store.append_deferred("todo", ["real item"])
    check("a plain file still works", "real item" in (repo / "TODO.md").read_text())


def main() -> int:
    tests = [
        test_setting, test_repo_allowed, test_hosts, test_commands, test_settings,
        test_planted_tool_dirs, test_attachments_and_memory, test_env, test_instructions,
        test_env_files_never_read, test_provider_flags,
        test_preflight, test_failure_wording, test_run_refused, test_hook, test_stubs,
        test_cleanup_waits_for_other_sessions, test_private_gitdir, test_private_gitdir_attacks,
        test_failed_checkin_keeps_the_commits, test_recovery_and_clearing,
        test_boot_kills_leftover_sessions, test_merge_gate, test_policy, test_deploy_fingerprint,
        test_deploy_refusal, test_directives, test_sensors_skipped, test_watch, test_image, test_todo,
    ]
    global FAILED
    try:
        for test in tests:
            print(test.__name__)
            try:
                test()
            except Exception:
                FAILED += 1
                print(f"  FAIL: {test.__name__} raised")
                traceback.print_exc()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
