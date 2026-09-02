#!/usr/bin/env python3
"""Regression test: the Control Room says what the repo is about.

A repo forum used to identify each project by its filesystem path and nothing
else, so ten repos read as ten paths and the user had to remember which was
which. Every repo already states its purpose somewhere — CLAUDE.md, README.md,
package metadata — so the blurb is derived rather than typed in.

Two things have to hold or the feature is worse than not having it:

  * the derived line is the *prose* one, not the title, a badge row, a bullet
    or a line of fenced code — a wrong blurb is worse than no blurb
  * the derive is cached against a stat-only signature of the source files,
    because ``refresh_control_room`` runs on every instance start and
    completion; a six-file read per refresh to render one sentence that
    changes once a month is exactly the cost the cache exists to avoid

Asserted here: source precedence, prose extraction, markdown cleanup,
truncation, the cache's hit and miss conditions, the symlink refusal, and that
the embed itself leads with the blurb and demotes the path.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.engine import repo_desc as rd  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


class FakeStore:
    """Just the four methods repo_desc touches, plus a save counter."""

    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}
        self.writes = 0

    def get_repo_description_entry(self, name: str) -> dict | None:
        return self.entries.get(name)

    def set_repo_description(self, name, text, source, sig, mtime, path) -> None:
        self.writes += 1
        self.entries[name] = {"text": text, "source": source, "sig": sig,
                              "mtime": mtime, "path": path}

    def clear_repo_description(self, name: str) -> None:
        self.entries.pop(name, None)


def make_repo(root: Path, files: dict[str, str]) -> str:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return str(root)


tmp = tempfile.TemporaryDirectory()
BASE = Path(tmp.name)
n = 0


def repo(files: dict[str, str]) -> str:
    global n
    n += 1
    root = BASE / f"r{n}"
    root.mkdir()
    return make_repo(root, files)


# --- Source precedence ----------------------------------------------------

print("Source precedence")

path = repo({
    ".claude/repo.json": '{"description": "The override wins."}',
    "CLAUDE.md": "# Thing\n\nThe derived line.\n",
})
check("repo.json beats CLAUDE.md",
      rd.resolve_repo_description_with_source(path) == ("The override wins.", "repo.json"),
      str(rd.resolve_repo_description_with_source(path)))

path = repo({
    "CLAUDE.md": "# Thing\n\nFrom CLAUDE.md.\n",
    "README.md": "# Thing\n\nFrom the README.\n",
})
check("CLAUDE.md beats README.md",
      rd.resolve_repo_description(path) == "From CLAUDE.md.")

path = repo({
    "README.md": "# Thing\n\nFrom the README.\n",
    "pyproject.toml": '[project]\nname = "t"\ndescription = "From pyproject."\n',
})
check("README.md beats pyproject.toml",
      rd.resolve_repo_description(path) == "From the README.")

path = repo({"pyproject.toml": '[project]\nname = "t"\ndescription = "A packaged thing."\n'})
check("pyproject [project] description is read",
      rd.resolve_repo_description_with_source(path) == ("A packaged thing.", "pyproject.toml"))

path = repo({"pyproject.toml": '[tool.poetry]\nname = "t"\ndescription = "A poetry thing."\n'})
check("pyproject [tool.poetry] description is read",
      rd.resolve_repo_description(path) == "A poetry thing.")

path = repo({"package.json": '{"name": "t", "description": "A node thing."}'})
check("package.json description is read",
      rd.resolve_repo_description_with_source(path) == ("A node thing.", "package.json"))

path = repo({"Cargo.toml": '[package]\nname = "t"\ndescription = "A rust thing."\n'})
check("Cargo.toml description is read",
      rd.resolve_repo_description(path) == "A rust thing.")

check("an empty repo yields nothing", rd.resolve_repo_description(repo({})) is None)

# A repo.json that a human hand-edited into invalid JSON must not hide the
# perfectly good CLAUDE.md behind it, and must not take the refresh down.
path = repo({
    ".claude/repo.json": "{not json at all",
    "CLAUDE.md": "# Thing\n\nStill found this.\n",
})
check("malformed repo.json falls through to the next source",
      rd.resolve_repo_description(path) == "Still found this.")

path = repo({
    ".claude/repo.json": '{"deploy": {"command": "x"}}',
    "CLAUDE.md": "# Thing\n\nNo description key.\n",
})
check("repo.json without a description key falls through",
      rd.resolve_repo_description(path) == "No description key.")


# --- Prose extraction -----------------------------------------------------

print("\nProse extraction")

path = repo({"CLAUDE.md": "# Claude Code Bot\n\nDiscord bot for managing instances.\n"})
check("the H1 title is skipped",
      rd.resolve_repo_description(path) == "Discord bot for managing instances.")

path = repo({"README.md": (
    "# Thing\n\n"
    "[![CI](https://img.shields.io/x.svg)](https://ci.example)\n"
    "[![Cov](https://img.shields.io/y.svg)](https://cov.example)\n\n"
    "Actual sentence about the thing.\n"
)})
check("badge rows are skipped",
      rd.resolve_repo_description(path) == "Actual sentence about the thing.")

path = repo({"README.md": "# Thing\n\n```bash\npip install thing\n```\n\nWhat it does.\n"})
check("fenced code is skipped",
      rd.resolve_repo_description(path) == "What it does.")

path = repo({"README.md": "# Thing\n\n- a bullet\n- another\n\nThe prose line.\n"})
check("bullets are skipped",
      rd.resolve_repo_description(path) == "The prose line.")

path = repo({"README.md": "# Thing\n\n> a quote\n\nThe prose line.\n"})
check("block quotes are skipped",
      rd.resolve_repo_description(path) == "The prose line.")

path = repo({"README.md": "# Thing\n\n<!-- a note\nspanning lines -->\n\nThe prose line.\n"})
check("multi-line HTML comments are skipped",
      rd.resolve_repo_description(path) == "The prose line.")

path = repo({"README.md": "# Thing\n\n<!-- note --> The prose line.\n"})
check("prose after a closed comment on the same line is kept",
      rd.resolve_repo_description(path) == "The prose line.",
      str(rd.resolve_repo_description(path)))

# An unterminated comment must not leak its body as the blurb.
path = repo({"README.md": "# Thing\n\n<!-- unterminated\nsecret internal note\n"})
check("an unterminated HTML comment yields nothing",
      rd.resolve_repo_description(path) is None,
      str(rd.resolve_repo_description(path)))

path = repo({"README.md": "Thing\n=====\n\n---\n\nThe prose line.\n"})
check("horizontal rules are skipped",
      rd.resolve_repo_description(path) == "The prose line.")

path = repo({"README.md": "# Thing\n\n| a | b |\n|---|---|\n\nThe prose line.\n"})
check("table rows are skipped",
      rd.resolve_repo_description(path) == "The prose line.")

path = repo({"README.md": "# Thing\n\n## Install\n\n### Also\n"})
check("a file of nothing but headings yields nothing",
      rd.resolve_repo_description(path) is None)


# --- Cleanup and truncation -----------------------------------------------

print("\nCleanup and truncation")

path = repo({"CLAUDE.md": "# T\n\n**Bold** and *italic* and `code` and [a link](http://x).\n"})
check("markdown is reduced to plain text",
      rd.resolve_repo_description(path) == "Bold and italic and code and a link.",
      rd.resolve_repo_description(path) or "")

# snake_case is the exact thing a blunt underscore strip would eat, and it is
# all over the first line of a developer README.
check("snake_case survives the emphasis strip",
      rd.clean_text("State lives in data/state.json via repo_desc and _internal.")
      == "State lives in data/state.json via repo_desc and _internal.",
      rd.clean_text("State lives in data/state.json via repo_desc and _internal."))

check("underscore italics are still unwrapped",
      rd.clean_text("It is _derived_, not typed.") == "It is derived, not typed.")

long_line = ("This project is a very long winded description of something that "
             "goes on and on well past the limit that the control room embed "
             "is willing to show for one repo.")
path = repo({"CLAUDE.md": f"# T\n\n{long_line}\n"})
got = rd.resolve_repo_description(path) or ""
check("an over-long line is truncated to the cap",
      len(got) <= rd.MAX_LEN, f"{len(got)} chars")
check("it truncates on a word boundary with an ellipsis",
      got.endswith("…") and not got[:-1].endswith(" ") and " " in got, got)
check("the truncated text is a prefix of the source",
      long_line.startswith(got[:-1].rstrip()), got)

path = repo({".claude/repo.json": '{"description": "ab"}', "CLAUDE.md": "# T\n\nok\n"})
check("a sub-3-char blurb is rejected rather than shown",
      rd.resolve_repo_description(path) is None)

path = repo({"CLAUDE.md": "# T\n\nOne line\nthat wrapped.\n"})
check("the result is always a single line",
      "\n" not in (rd.resolve_repo_description(path) or ""))

# A hard-wrapped README is the common case, and its first *line* ends
# mid-sentence — a blurb that stops at "a plain-language request like" reads as
# truncation with no ellipsis to admit it.
path = repo({"README.md": (
    "# Media fetcher\n\n"
    "Agentic media downloader. You take a plain-language request like\n"
    "\"grab that reel\" and work out the rest yourself.\n\n"
    "## Install\n"
)})
check("a hard-wrapped paragraph is joined, not cut at the line break",
      rd.resolve_repo_description(path)
      == 'Agentic media downloader. You take a plain-language request like '
         '"grab that reel" and work out the rest yourself.',
      rd.resolve_repo_description(path) or "")

path = repo({"README.md": "# T\n\nFirst para.\n\nSecond para.\n"})
check("the paragraph stops at the blank line",
      rd.resolve_repo_description(path) == "First para.")

path = repo({"README.md": "# T\n\nThe opener.\n- a bullet\n"})
check("the paragraph stops at a bullet",
      rd.resolve_repo_description(path) == "The opener.")

path = repo({"README.md": "# T\n\nThe opener.\n## Next\n"})
check("the paragraph stops at a heading",
      rd.resolve_repo_description(path) == "The opener.")

# Two whole sentences fit inside the cap, so both are kept; a third would not.
two = ("A short name for the thing. It does the second thing as well. "
       "And here is a third sentence that pushes the whole paragraph past the "
       "hundred and twenty character cap entirely.")
path = repo({"README.md": f"# T\n\n{two}\n"})
got = rd.resolve_repo_description(path) or ""
check("truncation keeps as many whole sentences as fit",
      got == "A short name for the thing. It does the second thing as well.", got)
check("and does not append an ellipsis to a clean sentence cut",
      not got.endswith("…"), got)

# When the first sentence is too terse to describe anything, the ellipsis cut
# is the better of the two.
terse = ("Hi. This is a much longer explanation of what the project actually is "
         "and it runs well past the cap so it has to be cut somewhere sensible.")
path = repo({"README.md": f"# T\n\n{terse}\n"})
got = rd.resolve_repo_description(path) or ""
check("a too-short first sentence falls back to the ellipsis cut",
      got.endswith("…") and len(got) > 24, got)


# --- Symlink refusal ------------------------------------------------------

print("\nSymlink refusal")

outside = BASE / "outside.md"
outside.write_text("# X\n\nA file the repo does not own.\n", encoding="utf-8")
path = repo({})
try:
    os.symlink(outside, Path(path) / "CLAUDE.md")
    check("a symlink pointing out of the repo is refused",
          rd.resolve_repo_description(path) is None,
          str(rd.resolve_repo_description(path)))
except OSError:
    check("a symlink pointing out of the repo is refused", True, "symlinks unsupported, skipped")


# --- Cache ----------------------------------------------------------------

print("\nCache")

store = FakeStore()
path = repo({"CLAUDE.md": "# T\n\nFirst wording.\n"})
check("first call derives and records",
      rd.refresh_repo_description_sync(store, "r", path) == "First wording."
      and store.writes == 1)

check("a second call with nothing changed is a cache hit",
      rd.refresh_repo_description_sync(store, "r", path) == "First wording."
      and store.writes == 1, f"{store.writes} write(s)")

# Rewrite the body but restore the mtime: the cache must be believed, which is
# what proves the hot path is not reading the file.
claude_md = Path(path) / "CLAUDE.md"
st = os.stat(claude_md)
claude_md.write_text("# T\n\nSecond wording.\n", encoding="utf-8")
os.utime(claude_md, (st.st_atime, st.st_mtime))
check("unchanged mtime does not re-read the body",
      rd.refresh_repo_description_sync(store, "r", path) == "First wording."
      and store.writes == 1, f"{store.writes} write(s)")

os.utime(claude_md, (st.st_atime, st.st_mtime + 10))
check("a moved mtime re-derives",
      rd.refresh_repo_description_sync(store, "r", path) == "Second wording."
      and store.writes == 2, f"{store.writes} write(s)")

# The manual override is a brand new file, so the newest mtime moves forward.
rd.write_manual_description(path, "Hand written.")
check("writing repo.json invalidates the cache",
      rd.refresh_repo_description_sync(store, "r", path) == "Hand written.")
check("and records where it came from",
      (store.entries["r"] or {}).get("source") == "repo.json")

rd.write_manual_description(path, None)
check("clearing repo.json falls back to CLAUDE.md",
      rd.refresh_repo_description_sync(store, "r", path) == "Second wording.")
check("clearing keeps the file for its other keys",
      rd.repo_json_path(path).is_file())

# A registration whose directory is gone (moved, deleted) must be refused,
# not silently re-created by mkdir(parents=True).
missing = str(BASE / "not-a-repo" / "nested")
try:
    rd.write_manual_description(missing, "Should not land.")
    refused = False
except OSError:
    refused = True
check("a missing repo directory is refused, not conjured",
      refused and not (BASE / "not-a-repo").exists())

other = repo({"CLAUDE.md": "# T\n\nA different repo entirely.\n"})
check("the same name at a different path re-derives",
      rd.refresh_repo_description_sync(store, "r", other) == "A different repo entirely.")

# A repo that says nothing must cache the miss, or every refresh pays six
# failed opens for a line that will never exist.
empty = repo({})
store2 = FakeStore()
check("a repo with no sources caches the miss",
      rd.refresh_repo_description_sync(store2, "e", empty) is None and store2.writes == 1)
check("and the miss is a cache hit next time",
      rd.refresh_repo_description_sync(store2, "e", empty) is None and store2.writes == 1,
      f"{store2.writes} write(s)")

check("an empty repo path is a no-op",
      rd.refresh_repo_description_sync(store2, "x", "") is None)


# --- The async twin -------------------------------------------------------

# Production calls the async one; the sync twin exists for harnesses and
# non-async callers. They must not drift.

print("\nAsync twin")

import asyncio  # noqa: E402

astore = FakeStore()
apath = repo({"CLAUDE.md": "# T\n\nAsync wording.\n"})
check("the async twin derives",
      asyncio.run(rd.refresh_repo_description(astore, "a", apath)) == "Async wording."
      and astore.writes == 1)
check("and hits the same cache",
      asyncio.run(rd.refresh_repo_description(astore, "a", apath)) == "Async wording."
      and astore.writes == 1, f"{astore.writes} write(s)")
check("an empty repo path is a no-op",
      asyncio.run(rd.refresh_repo_description(astore, "a", "")) is None)


# --- State round-trip ------------------------------------------------------

print("\nState round-trip")

from bot.store.state import StateStore  # noqa: E402

sfile = BASE / "state.json"
store3 = StateStore(sfile, BASE / "results")
store3.add_repo("thing", apath)
store3.set_repo_description("thing", "Round tripped.", "CLAUDE.md", "sig", 1.5, apath)
check("recording a blurb defers the write instead of rewriting state.json",
      store3._dirty is True)
store3.save()
reloaded = StateStore(sfile, BASE / "results")
check("the cache survives a save/load",
      reloaded.get_repo_description("thing") == "Round tripped.",
      str(reloaded.get_repo_description("thing")))
check("the whole entry survives",
      (reloaded.get_repo_description_entry("thing") or {}).get("sig") == "sig")

# An older state file predates the key entirely and must load, not raise.
import json as _json  # noqa: E402
raw = _json.loads(sfile.read_text())
raw.pop("repo_descriptions")
sfile.write_text(_json.dumps(raw))
old_store = StateStore(sfile, BASE / "results")
check("a state file without the key still loads",
      old_store.get_repo_description("thing") is None
      and "thing" in old_store.list_repos())

old_store.set_repo_description("thing", "x" * 5, "CLAUDE.md", "s", 1.0, apath)
check("removing the repo drops its cached blurb",
      old_store.remove_repo("thing")
      and old_store.get_repo_description_entry("thing") is None)


# --- The embed ------------------------------------------------------------

print("\nControl Room embed")

from bot.discord import channels  # noqa: E402

# Derived, not hardcoded — a literal /home/... path fails check_portability.
REPO_PATH = str(BASE / "thing")
embed = channels.build_control_embed("thing", REPO_PATH, "master",
                                     description="A thing that does things.")
desc = embed.description or ""
check("the blurb leads the embed description",
      desc.startswith("A thing that does things."), desc)
check("the path is demoted to subtext beneath it",
      desc.endswith(f"-# {REPO_PATH}"), desc)

bare = channels.build_control_embed("thing", REPO_PATH, "master")
check("with no blurb the embed is unchanged from before",
      bare.description == REPO_PATH, str(bare.description))

blank = channels.build_control_embed("thing", REPO_PATH, "master", description="   ")
check("a whitespace-only blurb is treated as absent",
      blank.description == REPO_PATH, str(blank.description))

nopath = channels.build_control_embed("thing", "", None, description="A thing.")
check("a blurb with no path renders alone",
      nopath.description == "A thing.", str(nopath.description))


# --- /repo desc targets the thread's repo ---------------------------------
#
# The bug this pins: the handler defaulted to the globally *active* repo, so
# typing it in one repo's forum after a /repo switch wrote the sentence into
# a different repo's .claude/repo.json. The Discord half is the other half of
# the same bug — slash commands build a ctx with no repo at all, so cmd_repo
# has to resolve the channel's repo before the engine ever sees it.

print("\n/repo desc targeting")

import asyncio as _asyncio  # noqa: E402

from bot.engine import commands as _cmds  # noqa: E402


class _Msgr:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.meta_changed: list[str] = []

    async def send_text(self, channel_id, text, **kw):
        self.sent.append(text)

    async def on_repo_meta_changed(self, repo_name):
        self.meta_changed.append(repo_name)


class _Ctx:
    def __init__(self, store, repo_name=None) -> None:
        self.store = store
        self.repo_name = repo_name
        self.messenger = _Msgr()
        self.channel_id = "1"


here_store = StateStore(BASE / "desc-state.json", BASE / "results")
thread_repo = repo({"CLAUDE.md": "# A\n\nThread repo.\n"})
active_repo = repo({"CLAUDE.md": "# B\n\nActive repo.\n"})
here_store.add_repo("threadrepo", thread_repo)
here_store.add_repo("activerepo", active_repo)
here_store.switch_repo("activerepo")

ctx = _Ctx(here_store, repo_name="threadrepo")
_asyncio.run(_cmds._repo_desc(ctx, "Typed in the thread."))
check("the blurb lands in the thread's repo, not the active one",
      (rd.repo_json_path(thread_repo).is_file()
       and not rd.repo_json_path(active_repo).exists()),
      f"thread={rd.repo_json_path(thread_repo).is_file()} "
      f"active={rd.repo_json_path(active_repo).exists()}")
check("and the control room for that repo is redrawn",
      ctx.messenger.meta_changed == ["threadrepo"], str(ctx.messenger.meta_changed))

# With no thread repo it still falls back to the active one.
ctx2 = _Ctx(here_store, repo_name=None)
_asyncio.run(_cmds._repo_desc(ctx2, "Typed with no thread repo."))
check("with no thread repo it falls back to the active one",
      rd.repo_json_path(active_repo).is_file())

# An explicit name still wins over both.
ctx3 = _Ctx(here_store, repo_name="activerepo")
_asyncio.run(_cmds._repo_desc(ctx3, "threadrepo Named explicitly."))
check("an explicit name beats the thread's repo",
      _json.loads(rd.repo_json_path(thread_repo).read_text())["description"]
      == "Named explicitly.")

# The Discord half: cmd_repo must resolve the channel's repo, because
# _run_slash builds its ctx without one.
slash_src = (Path(__file__).resolve().parent.parent
             / "bot" / "discord" / "slash_commands.py").read_text()
check("the /repo slash command resolves the channel's repo into ctx",
      "repo_for_channel" in slash_src)
from bot.discord.forums import ForumManager  # noqa: E402
check("ForumManager exposes that resolver",
      callable(getattr(ForumManager, "repo_for_channel", None)))


# --- This repo, for real --------------------------------------------------

print("\nThis repo")

here = str(Path(__file__).resolve().parent.parent)
found = rd.resolve_repo_description_with_source(here)
check("this repo describes itself out of CLAUDE.md",
      found is not None and found[1] == "CLAUDE.md", str(found))
check("and the line is its actual first sentence",
      found is not None
      and found[0] == "Discord bot for managing Claude Code instances remotely.",
      str(found))

tmp.cleanup()

print()
if failures:
    print("FAIL: repo description")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print(f"PASS: the Control Room says what the repo is about ({checks} checks).")
print("      The blurb comes from repo.json, CLAUDE.md, README.md or package")
print("      metadata in that order, skips titles/badges/code/bullets, is")
print("      reduced to one plain line under 120 chars, and is re-derived only")
print("      when the signature over the source files moves — so the refresh")
print("      that runs on every instance event stays stat-only.")
