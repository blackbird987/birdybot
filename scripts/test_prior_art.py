#!/usr/bin/env python3
"""Regression test: a plan is checked against what was already tried.

An idea that was built, shipped and removed on purpose kept coming back as a
fresh proposal, because the session judging the plan only knew its own
context. The auto-armed wake is the worked example: added in cdc54c8, removed
in d5f8aa8, proposed again by a session that had never seen either. The bot
now reads git for the files a plan touches and hands the result to whichever
step is judging it. What this pins:

  * **retrieval finds the removal.** On a fixture repo and on this repo's own
    history: a plan re-proposing the auto-armed wake must surface d5f8aa8,
    tagged [reversal], ahead of everything else.
  * **it fails open.** Not a repo, a timeout, a spent budget, the switch off:
    each is "" and never an exception, because a history check that can stop
    work gets switched off.
  * **every consumer gets it.** Both plan-review paths, the /chain build brief
    (surviving the 8000-char plan cap), and the weekly prompt review.
  * **the two evals catch a judge that ignored it**, and stay silent on a
    quoted /chain the bot would never have dispatched.

Run: python scripts/test_prior_art.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import config  # noqa: E402
from bot.claude.types import InstanceOrigin  # noqa: E402
from bot.engine import commands, prior_art  # noqa: E402
from bot.engine import eval as eval_mod  # noqa: E402
from bot.engine import prompt_review as pr  # noqa: E402
from bot.engine import workflows  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" · {detail}" if detail else ""))
    if not ok:
        failures.append(label)


tmp = tempfile.TemporaryDirectory()
TMP = Path(tmp.name)


def git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=env).stdout


def commit(repo: Path, files: dict[str, str | None], message: str) -> None:
    for rel, body in files.items():
        p = repo / rel
        if body is None:
            git(repo, "rm", "-q", rel)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", message)


# ------------------------------------------------------------ 1. fixture repo

REPO = TMP / "repo"
REPO.mkdir()
git(REPO, "init", "-q")
commit(REPO, {"a.py": "x = 1\n", "pkg/b.py": "y = 1\n"}, "Initial import")
commit(REPO, {"a.py": "x = 1\nauto_arm = True\n"},
       "Auto-arm a wake when a reply promises to keep watching")
commit(REPO, {"pkg/b.py": "y = 2\n"}, "Tweak b")
commit(REPO, {"a.py": "x = 1\n"},
       "Remove the auto-arm wake heuristic\n\nIt fired on prose that merely discussed a job.")
commit(REPO, {"gone.py": "SPECIAL_THING = 1\n"}, "Add gone.py holding SPECIAL_THING")
commit(REPO, {"gone.py": None}, "Delete gone.py")
commit(REPO, {"a.py": "x = 2\n"},
       "Tidy a.py\n\nRemove a stray blank line while here.")
commit(REPO, {"a.py": "x = 3\n"}, "Add a helper to a.py")

print()
print("retrieval on a fixture repo")

plan = ("Re-add auto-arming in a.py (see `a.py:12`) and reuse `SPECIAL_THING`. "
        "Also touch nope/missing.py, which does not exist.")
block = prior_art.collect(str(REPO), plan)
lines = block.splitlines()
check("the block opens with the marker", block.startswith(prior_art.PRIOR_ART_MARKER))
check("the removal commit is found and tagged",
      any("[reversal] Remove the auto-arm wake heuristic" in ln for ln in lines),
      block)
first_commit = next((ln for ln in lines if ln.startswith("- ")), "")
check("the reversal is listed before any other commit",
      "[reversal]" in first_commit, first_commit)
check("a body-only 'Remove' line is context, not a reversal",
      any("Tidy a.py" in ln and "[reversal]" not in ln for ln in lines), block)
check("an identifier from a deleted file is found through its commit message",
      "Add gone.py holding SPECIAL_THING" in block)
check("a path that does not exist is ignored", "nope/missing.py" not in block)
check("each sha appears once",
      len([ln for ln in lines if ln.startswith("- ")])
      == len({ln.split()[1] for ln in lines if ln.startswith("- ")}))

bare = prior_art.collect(str(REPO), "Only change b.py here.")
check("a bare filename resolves when exactly one tracked file has it",
      "### pkg/b.py" in bare, bare)

small = prior_art.collect(str(REPO), plan, max_chars=330)
check("the char cap holds", 0 < len(small) <= 330, f"{len(small)} chars")
check("under a tight cap the reversal is what survives",
      "[reversal] Remove the auto-arm" in small, small)

check("a plan naming no file and no identifier gives nothing",
      prior_art.collect(str(REPO), "Make the bot nicer to use.") == "")
check("an empty plan gives nothing", prior_art.collect(str(REPO), "") == "")

NOT_GIT = TMP / "plain"
NOT_GIT.mkdir()
(NOT_GIT / "a.py").write_text("x\n")
check("a directory that is not a repo gives nothing",
      prior_art.collect(str(NOT_GIT), "change a.py") == "")
check("a path that does not exist gives nothing",
      prior_art.collect(str(TMP / "nowhere"), "change a.py") == "")

_orig_run = prior_art.run_capture


def _timeout_on_log(cmd, **kw):
    if "log" in cmd:
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))
    return _orig_run(cmd, **kw)


prior_art.run_capture = _timeout_on_log
try:
    out = prior_art.collect(str(REPO), plan)
    check("a git timeout is swallowed, not raised", out == "", repr(out[:80]))
except Exception as e:  # pragma: no cover - the failure being tested
    check("a git timeout is swallowed, not raised", False, repr(e))
finally:
    prior_art.run_capture = _orig_run

_orig_budget = prior_art._TOTAL_BUDGET_SECS
prior_art._TOTAL_BUDGET_SECS = 0
try:
    check("a spent time budget gives nothing, without raising",
          prior_art.collect(str(REPO), plan) == "")
finally:
    prior_art._TOTAL_BUDGET_SECS = _orig_budget

config.PRIOR_ART_ENABLED = False
check("switched off, collect gives nothing", prior_art.collect(str(REPO), plan) == "")
check("switched off, block_history gives nothing",
      prior_art.block_history(str(ROOT), ["PLAN_REVIEW_PROMPT"]) == "")
config.PRIOR_ART_ENABLED = True

# attach / split_attached
plan_quoting = f"Build it.\n\n{prior_art.PRIOR_ART_MARKER}\nquoted, not attached"
check("a plan quoting the marker is not split at its own quotation",
      prior_art.split_attached(plan_quoting) == (plan_quoting, ""))
joined = prior_art.attach("The plan.", block)
p_half, tail = prior_art.split_attached(joined)
check("attach then split round-trips",
      p_half == "The plan." and tail.startswith(prior_art.PRIOR_ART_MARKER)
      and tail.endswith(prior_art.CHAIN_INSTRUCTION))
check("attaching nothing leaves the plan alone", prior_art.attach("p", "") == "p")


# ---------------------------------------------- 2. this repo's real history

print()
print("replay against this repo's own history")


def has_commit(repo: Path, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "cat-file", "-e", sha + "^{commit}"],
                          capture_output=True).returncode == 0


if has_commit(ROOT, "d5f8aa8"):
    replay = prior_art.collect(str(ROOT), (
        "Re-add auto-arming a self-wake when a reply says it will keep "
        "watching: in bot/engine/lifecycle.py, detect the promise with a "
        "`WAKE_PROMISE_RE` regex and schedule a 3-minute wake."
    ))
    check("the auto-arm plan surfaces the commit that removed it",
          any("d5f8aa8" in ln and "[reversal]" in ln for ln in replay.splitlines()),
          replay[:300])
    check("the real block respects the default cap",
          len(replay) <= config.PRIOR_ART_MAX_CHARS, f"{len(replay)} chars")

    # Negative control: a file whose history holds no removal at all.
    quiet = None
    for cand in subprocess.run(["git", "-C", str(ROOT), "ls-files", "scripts/"],
                               capture_output=True, text=True).stdout.split():
        if not cand.endswith(".py"):
            continue
        hits = subprocess.run(
            ["git", "-C", str(ROOT), "log", "--no-merges", "-i", "-E",
             f"--grep={prior_art._REVERSAL_GREP}", "--format=%s", "--", cand],
            capture_output=True, text=True).stdout
        subjects = [s for s in hits.splitlines()
                    if prior_art._REVERSAL_SUBJECT_RE.match(s)]
        recent = subprocess.run(
            ["git", "-C", str(ROOT), "log", "--no-merges", "-n", "5",
             "--format=%s", "--", cand], capture_output=True, text=True).stdout
        if not subjects and not any(prior_art._REVERSAL_SUBJECT_RE.match(s)
                                    for s in recent.splitlines()):
            quiet = cand
            break
    if quiet:
        control = prior_art.collect(str(ROOT), f"Adjust {quiet} slightly.")
        check(f"negative control ({quiet}) shows history but no reversal",
              bool(control) and not any(
                  ln.startswith("- ") and "[reversal]" in ln
                  for ln in control.splitlines()),
              control[:200])
    else:
        print("  SKIP  no reversal-free file found for the negative control")
else:
    print("  SKIP  d5f8aa8 is not reachable from this checkout")

# A large real history to time against (6,087 commits when this was written).
AIAGENT = Path(os.environ.get(
    "PRIOR_ART_BENCH_REPO", str(Path.home() / "Programming/DegenAI/AIAgent/AIAgent")))
if AIAGENT.is_dir() and has_commit(AIAGENT, "HEAD"):
    tracked = subprocess.run(["git", "-C", str(AIAGENT), "ls-files"],
                             capture_output=True, text=True).stdout.split()
    files = [f for f in tracked if f.endswith(".cs")][:8]
    big_plan = " ".join(files) + " `ExecuteTradeAsync` `PositionManager` " \
        "`RiskGuard_Limit` `BacktestRunner` `DecisionLog` `ExitWatchCloids`"
    t0 = time.monotonic()
    prior_art.collect(str(AIAGENT), big_plan)
    elapsed = time.monotonic() - t0
    check("8 files and 6 identifiers on AIAgent stay under 2s",
          elapsed < 2.0, f"{elapsed:.2f}s")
else:
    print("  SKIP  AIAgent checkout not present")


# ------------------------------------------------------------ 3. wiring

print()
print("every judging step receives it")


class FakeStore:
    def __init__(self, instances=None, override=None, deferred=None):
        self.instances = instances or {}
        self.override = override
        self.deferred = deferred or []
        self.stored_override = None

    def get_instance(self, iid):
        return self.instances.get(iid)

    def get_deferred_items(self, repo):
        return list(self.deferred)

    def deferred_dedup_key(self, item):
        return item.lower()

    def get_chain_plan_override(self, sid):
        return self.override

    def set_chain_plan_override(self, sid, text):
        self.stored_override = text

    def get_autopilot_chain(self, sid):
        return None


def source_inst(plan_text: str, repo: Path = REPO, **kw):
    return SimpleNamespace(
        id="p-1", session_id="s-1", repo_path=str(repo), worktree_path=None,
        repo_name="fixture", origin=InstanceOrigin.PLAN,
        read_result_text=lambda: plan_text, **kw,
    )


captured: list = []


async def fake_spawn_from(ctx, source_id, cfg, source_msg_id=None):
    captured.append(cfg)
    return None


_orig_spawn = workflows.spawn_from
workflows.spawn_from = fake_spawn_from
try:
    ctx = SimpleNamespace(store=FakeStore({"p-1": source_inst(plan)}))
    asyncio.run(workflows.on_review_plan(ctx, "p-1"))
    prompt = captured[-1].prompt
    check("the Review Plan button prepends the history",
          prompt.startswith(prior_art.PRIOR_ART_MARKER)
          and prompt.endswith(config.PLAN_REVIEW_PROMPT))
    check("the review stays behind the read-only floor",
          captured[-1].permission_mode == "explore")

    ctx = SimpleNamespace(store=FakeStore({"p-1": source_inst("Make it nicer.")}))
    asyncio.run(workflows.on_review_plan(ctx, "p-1"))
    check("a plan with no history gets the bare review prompt",
          captured[-1].prompt == config.PLAN_REVIEW_PROMPT)

    ctx = SimpleNamespace(store=FakeStore({"p-1": source_inst(plan)},
                                          deferred=["[UX] Old item (Low)"]))
    asyncio.run(workflows._review_plan_loop(ctx, "p-1", None, []))
    prompt = captured[-1].prompt
    check("the autopilot review loop prepends it too",
          prompt.startswith(prior_art.PRIOR_ART_MARKER)
          and "Previously deferred review items" in prompt
          and prompt.endswith(config.PLAN_REVIEW_PROMPT), prompt[:120])
finally:
    workflows.spawn_from = _orig_spawn

# /chain: the override carries plan, block and instruction.
chain_text = (
    "Kicking it off.\n\n[BOT_CMD: /chain preset=verify]\n~~~plan\n"
    f"{plan}\nPrior attempts: none found\n~~~\n"
)
store = FakeStore()
sent: list[str] = []


async def _send(ch, text, **kw):
    sent.append(text)


async def _noop_chain(*a, **kw):
    return None


_orig_conv = workflows.on_conversational_chain
workflows.on_conversational_chain = _noop_chain
try:
    ctx = SimpleNamespace(store=store, channel_id="c-1",
                          messenger=SimpleNamespace(send_text=_send))

    async def _run_chain():
        await commands._handle_chain_directive(ctx, chain_text, source_inst(plan))
        await asyncio.sleep(0)

    asyncio.run(_run_chain())
finally:
    workflows.on_conversational_chain = _orig_conv
stored = store.stored_override or ""
check("the /chain override keeps the plan", stored.startswith(plan))
check("...carries the history block", prior_art.PRIOR_ART_MARKER in stored)
check("...and ends with the ask-before-re-adding instruction",
      stored.endswith(prior_art.CHAIN_INSTRUCTION))

# The 8000-char cap must not truncate the history off the brief.
long_plan = ("Build step. " * 700)[:7900]
ctx = SimpleNamespace(store=FakeStore(
    {"p-1": source_inst(long_plan)}, override=prior_art.attach(long_plan, block)))
text = workflows._extract_latest_plan_text(ctx, [], "p-1")
check("a 7,900-char plan keeps all of itself", text.startswith(long_plan))
check("...and its history survives the plan cap",
      prior_art.PRIOR_ART_MARKER in text and text.endswith(prior_art.CHAIN_INSTRUCTION))

huge = "Z" * 12000
ctx = SimpleNamespace(store=FakeStore(
    {"p-1": source_inst(huge)}, override=prior_art.attach(huge, block)))
text = workflows._extract_latest_plan_text(ctx, [], "p-1")
check("an oversized plan is still capped at the plan limit",
      text.count("Z") == workflows._BUILD_PLAN_INJECT_MAX, str(text.count("Z")))
check("...while the history rides on top",
      text.endswith(prior_art.CHAIN_INSTRUCTION))

meta_plan = "Do the thing.\n### Applied\n- [UX] x — applied"
ctx = SimpleNamespace(store=FakeStore(
    {"p-1": source_inst(meta_plan)}, override=prior_art.attach(meta_plan, block)))
text = workflows._extract_latest_plan_text(ctx, [], "p-1")
check("review metadata is still stripped from the plan half",
      "### Applied" not in text and text.startswith("Do the thing.")
      and prior_art.PRIOR_ART_MARKER in text, text[:80])

ctx = SimpleNamespace(store=FakeStore({"p-1": source_inst("plain")}, override="plain plan"))
check("an override with nothing attached is returned as before",
      workflows._extract_latest_plan_text(ctx, [], "p-1") == "plain plan")

# The review-status parser must still find DEFERRED after the new line.
status = TMP / "review.md"
status.write_text(
    "Review.\n```review-status\nNEEDS_REVISION: no\n"
    "PRIOR_ATTEMPTS: d5f8aa8 removed the auto-arm wake\n"
    "DEFERRED:\n- [UX] One (Low)\n- [Bug Risk] Two (Medium)\n```\n",
    encoding="utf-8",
)
fake_review = SimpleNamespace(result_file=str(status))
check("DEFERRED still parses with a PRIOR_ATTEMPTS line above it",
      workflows._extract_deferred(fake_review) == ["[UX] One (Low)", "[Bug Risk] Two (Medium)"],
      str(workflows._extract_deferred(fake_review)))
check("NEEDS_REVISION still parses", workflows._needs_revision(fake_review) is False)

# Prompts ask for the lines the evals check for.
prp = config.PLAN_REVIEW_PROMPT
check("the review prompt requires PRIOR_ATTEMPTS, before DEFERRED",
      0 <= prp.find("PRIOR_ATTEMPTS:") < prp.find("DEFERRED:"))
check("History is an available tag", "Bug Risk, History" in prp)
check("CHAIN_CONTEXT asks for a 'Prior attempts:' line",
      "Prior attempts:" in config.CHAIN_CONTEXT)

# Weekly prompt review: the owning blocks' edit history is appended.
EVALS = TMP / "evals"
EVALS.mkdir()
eval_mod.EVALS_DIR = EVALS
for n in range(4):
    (EVALS / f"q-{n}.json").write_text(json.dumps({
        "instance_id": f"q-{n}", "repo": "bot", "origin": "direct", "mode": "build",
        "flags": [{"category": "constraint_violation", "severity": "issue",
                   "message": "Plan review got prior history but reported no "
                              "PRIOR_ATTEMPTS line", "evidence": ""}],
        "metrics": {}, "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
review_input = pr.build_review_input(days=7)
check("the prompt review is handed the owning block's edit history",
      "EDIT HISTORY OF THE OWNING BLOCKS" in review_input
      and "### PLAN_REVIEW_PROMPT" in review_input, review_input[-400:])
hist = review_input[review_input.find("EDIT HISTORY"):]
check("...under its own cap", len(hist) <= pr._MAX_HISTORY_CHARS, f"{len(hist)} chars")
check("the brief tells it to answer a listed removal",
      "Check the block's history" in pr.build_review_prompt(review_input))


# ------------------------------------------------------------ 4. evals

print()
print("the evals catch a judge that ignored the history")


def fake(origin, prompt="", branch=None):
    return SimpleNamespace(origin=origin, prompt=prompt, branch=branch)


with_marker = f"{block}\n\n{config.PLAN_REVIEW_PROMPT}"
no_line = "Fine.\n```review-status\nNEEDS_REVISION: no\nDEFERRED:\n```\n"
with_line = "Fine.\n```review-status\nNEEDS_REVISION: no\nPRIOR_ATTEMPTS: none\nDEFERRED:\n```\n"
r = eval_mod._check_review_prior_attempts
flags = r(fake(InstanceOrigin.REVIEW_PLAN, with_marker), no_line)
check("a review given history and silent about it is flagged", len(flags) == 1)
check("...and the flag is owned by PLAN_REVIEW_PROMPT",
      bool(flags) and eval_mod.attribute_flag(flags[0].category, flags[0].message)
      == "PLAN_REVIEW_PROMPT")
check("a review with the line is not flagged",
      r(fake(InstanceOrigin.REVIEW_PLAN, with_marker), with_line) == [])
check("a review that got no history is not flagged",
      r(fake(InstanceOrigin.REVIEW_PLAN, config.PLAN_REVIEW_PROMPT), no_line) == [])
check("a review with no status block at all is left to other handling",
      r(fake(InstanceOrigin.REVIEW_PLAN, with_marker), "A question for you?") == [])
check("another origin is not judged",
      r(fake(InstanceOrigin.DIRECT, with_marker), no_line) == [])

c = eval_mod._check_chain_prior_attempts
bare_chain = "Go.\n\n[BOT_CMD: /chain preset=ship]\n~~~plan\nEdit a.py.\n~~~\n"
flags = c(fake(InstanceOrigin.DIRECT), bare_chain)
check("a /chain plan with no 'Prior attempts:' line is flagged", len(flags) == 1)
check("...and the flag is owned by CHAIN_CONTEXT",
      bool(flags) and eval_mod.attribute_flag(flags[0].category, flags[0].message)
      == "CHAIN_CONTEXT")
for label, body in (
    ("plain", "Prior attempts: none found"),
    ("bulleted and bold", "- **Prior attempts:** d5f8aa8 removed it; this differs"),
):
    ok_chain = f"Go.\n\n[BOT_CMD: /chain preset=ship]\n~~~plan\nEdit a.py.\n{body}\n~~~\n"
    check(f"a /chain with the line ({label}) is not flagged",
          c(fake(InstanceOrigin.DIRECT), ok_chain) == [])
quoted = "The format is `[BOT_CMD: /chain preset=ship]` with a ~~~plan body.\n"
check("a quoted /chain is not flagged", c(fake(InstanceOrigin.DIRECT), quoted) == [])
nested = ("[BOT_CMD: /spawn repo=bot title=\"x\"]\n~~~spawn\nEnd with:\n"
          "[BOT_CMD: /chain preset=ship]\n~~~plan\nEdit a.py.\n~~~\n~~~\n")
check("a /chain inside another block's body is not flagged",
      c(fake(InstanceOrigin.DIRECT), nested) == [])
check("an origin whose directives are never dispatched is not flagged",
      c(fake(InstanceOrigin.TLDR), bare_chain) == [])

src = inspect.getsource(eval_mod.evaluate_instance)
check("both checks run in evaluate_instance",
      "_check_review_prior_attempts" in src and "_check_chain_prior_attempts" in src)

# House style: no em dashes in what this change wrote.
check("prior_art.py has no em dashes",
      "—" not in (ROOT / "bot/engine/prior_art.py").read_text(encoding="utf-8"))

print()
if failures:
    print(f"FAILED {len(failures)}/{checks}:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"All {checks} checks passed.")
