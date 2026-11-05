#!/usr/bin/env python3
"""Regression test: what the bot does with a repo's study records.

Three places touch them, and every one has to stay inert without a study folder
and open when something is wrong, because a check that can stop work gets
switched off. What this pins:

  * **the reader handles every shape in use.** The shared tool, the
    `findings_dir`/`shut` fork and the `records_dir`/`dead` fork, on fixtures,
    and once read-only on the three real repos, so a config change there cannot
    go silently empty.
  * **the sensor warns and nothing else.** A broken record reaches the user and
    the chain advances: no fix session, no halt, under either policy, with or
    without `.claude/sensors.json`. No study folder, nothing runs.
  * **the plan reviewer gets the list.** Before the early returns, under its own
    budget, without moving the `PRIOR_ATTEMPTS:` contract or `split_attached`.
  * **the commit step asks,** with its parsed summary block still last.

Run: python scripts/test_findings.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import config  # noqa: E402
from bot.engine import findings, prior_art, sensors, step_parse, steps  # noqa: E402
from bot.procutil import install_root  # noqa: E402

TOOL = ROOT / "scripts" / "study" / "study.py"
# The repos sit beside the install, wherever this checkout is.
PROGRAMMING = install_root(ROOT).parent

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}  {detail}")


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, capture_output=True, text=True, check=True)


def record(folder, record_id, status, *, verdict="", title="A title", date="2026-11-01", body="Body."):
    folder.mkdir(parents=True, exist_ok=True)
    verdict_line = f'verdict = "{verdict}"\n' if verdict else ""
    (folder / f"{record_id}.md").write_text(
        f'+++\nid = "{record_id}"\ntitle = "{title}"\ndate = "{date}"\n'
        f'status = "{status}"\nquestion = "Q"\n{verdict_line}+++\n{body}\n',
        encoding="utf-8",
    )


def write_config(root, text):
    (root / "study").mkdir(parents=True, exist_ok=True)
    (root / "study" / "config.toml").write_text(text, encoding="utf-8")


def shared_repo(tmp, name="shared"):
    root = Path(tmp) / name
    root.mkdir()
    git(root, "init", "-q")
    subprocess.run([sys.executable, str(TOOL), "init"], cwd=root, capture_output=True, check=True)
    return root


print("\n[reader: every shape]")
with tempfile.TemporaryDirectory() as tmp:
    root = shared_repo(tmp)
    found = root / "study" / "findings"
    check("a study folder with no records is an empty list", findings.dead_ends(root) == [])
    record(found, "2026-10-01-old", "shut", verdict="Old no.", date="2026-10-01")
    record(found, "2026-11-01-new", "shut", verdict="New no.", date="2026-11-01")
    record(found, "2026-11-02-live", "stands", verdict="Holds.")
    record(found, "2026-11-03-no-verdict", "shut", title="Only a title", date="2026-09-01")
    record(found, "2026-11-04-prose", "open", body='status = "shut"\nA stray line in the prose.')
    (found / "2026-11-05-broken.md").write_text('+++\nstatus = "shut"\nid = =\n+++\n', encoding="utf-8")
    (found / "not-a-record.md").write_text("plain notes\n", encoding="utf-8")
    got = findings.dead_ends(root)
    check("the shared layout returns shut records, newest first",
          [g[0] for g in got] == ["2026-11-01-new", "2026-10-01-old", "2026-11-03-no-verdict"], str(got))
    check("a verdict is preferred", got[0][1] == "New no.", str(got))
    check("a title stands in for a missing verdict", got[2][1] == "Only a title", str(got))
    check("a stray status line in the prose is not a status",
          "2026-11-04-prose" not in [g[0] for g in got], str(got))
    check("a broken record costs itself, not the list", len(got) == 3)

    written = subprocess.run(
        [sys.executable, str(TOOL), "new", "--title", "Made by the tool", "--question", "Q",
         "--status", "shut", "--verdict", "Tool no."],
        cwd=root, capture_output=True, text=True, check=True)
    tool_id = Path(written.stdout.strip()).stem
    check("a record the tool writes reads through the bot's parser",
          (tool_id, "Tool no.") in findings.dead_ends(root), str(findings.dead_ends(root)))

    nova = Path(tmp) / "nova"
    write_config(nova, 'findings_dir = "study/findings"\nladder_path = "study/ladder.toml"\n')
    record(nova / "study" / "findings", "S170", "shut", verdict="Nova no.")
    record(nova / "study" / "findings", "S171", "dead", verdict="Not this repo's word.")
    check("findings_dir reads status shut",
          findings.dead_ends(nova) == [("S170", "Nova no.")], str(findings.dead_ends(nova)))

    lab = Path(tmp) / "lab"
    write_config(lab, 'records_dir = "hypotheses"\nstate_max_bytes = 20000\n')
    record(lab / "hypotheses", "box-1", "dead", verdict="Lab no.")
    record(lab / "hypotheses", "box-2", "shut", verdict="Not this repo's word.")
    record(lab / "hypotheses" / "common", "TEMPLATE", "dead", verdict="Not a record.")
    check("records_dir reads status dead, top level only",
          findings.dead_ends(lab) == [("box-1", "Lab no.")], str(findings.dead_ends(lab)))

    outer = Path(tmp) / "outer"
    sub = outer / "AIAgent"
    sub.mkdir(parents=True)
    git(outer, "init", "-q")
    write_config(outer, "format = 1\n")
    record(outer / "study" / "findings", "2026-11-01-x", "shut", verdict="Up one.")
    check("a repo registered as a subfolder finds the git root's study",
          findings.dead_ends(sub) == [("2026-11-01-x", "Up one.")], str(findings.dead_ends(sub)))

    nested = Path(tmp) / "above"
    write_config(nested, "format = 1\n")
    record(nested / "study" / "findings", "2026-11-01-y", "shut", verdict="Not yours.")
    inner = nested / "inner"
    inner.mkdir()
    git(inner, "init", "-q")
    check("the walk stops at the git root", findings.dead_ends(inner) == [])

    # The tool and the reader each keep the rule for what counts as a git root,
    # because neither imports the other. Same layouts through both, so changing
    # one alone fails here. `reaches` is whether the registry above is found.
    def stray_folder(d):
        (d / ".git" / "info").mkdir(parents=True)

    def empty_folder(d):
        (d / ".git").mkdir()

    def folder_with_head(d):
        (d / ".git").mkdir()
        (d / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")

    def pointer_file(d):
        (d / ".git").write_text("gitdir: /nowhere/worktrees/x\n", encoding="utf-8")

    def real_repo(d):
        git(d, "init", "-q")

    both = Path(tmp) / "both"
    write_config(both, "format = 1\n")
    record(both / "study" / "findings", "2026-11-01-z", "shut", verdict="Above.")
    for case, make, reaches in (
        ("a stray .git folder holding only info/", stray_folder, True),
        ("an empty .git folder", empty_folder, True),
        ("a .git folder with HEAD", folder_with_head, False),
        ("a .git file", pointer_file, False),
        ("a real git init", real_repo, False),
    ):
        below = both / make.__name__
        below.mkdir()
        make(below)
        verb = "walks past" if reaches else "stops"
        read = findings.dead_ends(below)
        check(f"reader, {case}: {verb}", read == ([("2026-11-01-z", "Above.")] if reaches else []),
              str(read))
        tool = subprocess.run([sys.executable, str(TOOL), "state"], cwd=below,
                              capture_output=True, text=True, encoding="utf-8")
        # By what it says, because a crash also exits non-zero.
        found = ("2026-11-01-z" in tool.stdout if reaches
                 else "no study/config.toml" in tool.stderr)
        check(f"tool, {case}: {verb}", found, tool.stdout + tool.stderr)

    bare = Path(tmp) / "bare"
    bare.mkdir()
    git(bare, "init", "-q")
    check("no study folder is an empty list", findings.dead_ends(bare) == [])
    broken = Path(tmp) / "broken"
    write_config(broken, "not = = toml")
    check("a broken config is an empty list", findings.dead_ends(broken) == [])
    check("a repo path that does not exist is an empty list",
          findings.dead_ends(Path(tmp) / "gone") == [])
    notlist = Path(tmp) / "typed"
    write_config(notlist, "records_dir = 7\n")
    check("a mistyped config is an empty list", findings.dead_ends(notlist) == [])

print("\n[reader: the three real repos, read-only]")
for name in ("Ev-nova-remake", "funny-rock-polisher", "strategy-lab"):
    repo = PROGRAMMING / name
    if not repo.is_dir():
        print(f"  [SKIP] {name} is not on this machine")
        continue
    got = findings.dead_ends(repo)
    check(f"{name} returns a non-empty list", bool(got), str(findings.layout(repo)))
    print(f"         {len(got)} dead ends")


class Spy:
    """Stands in for spawn_from: on_sensors must never reach it for a warning."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("a warn-only sensor started a fix session")


def drive(cwd, repo_path=None):
    """Run the real on_sensors step: (returned instance, notices, spawns)."""
    inst = SimpleNamespace(id="t-1", worktree_path=str(cwd), repo_path=str(repo_path or cwd),
                           needs_input=False)
    notices = []

    async def send_text(channel_id, text, silent=False):
        notices.append(text)

    ctx = SimpleNamespace(
        store=SimpleNamespace(get_instance=lambda _id: inst),
        messenger=SimpleNamespace(send_text=send_text),
        channel_id="c",
    )
    spy = Spy()
    real = steps.spawn_from
    steps.spawn_from = spy
    try:
        result = asyncio.run(steps.on_sensors(ctx, "t-1"))
    finally:
        steps.spawn_from = real
    return result, inst, notices, spy.calls


print("\n[sensor]")
with tempfile.TemporaryDirectory() as tmp:
    bare = Path(tmp) / "bare"
    bare.mkdir()
    git(bare, "init", "-q")
    check("no study folder: no sensor", sensors._study_sensor(bare) is None)
    result, inst, notices, spawns = drive(bare)
    check("no study folder: nothing runs and nothing is said",
          result is inst and notices == [] and spawns == 0, str(notices))

    root = shared_repo(tmp)
    record(root / "study" / "findings", "2026-11-01-fine", "stands", verdict="Holds.")
    spec = sensors._study_sensor(root)
    check("a study folder gets a sensor, non-blocking",
          spec is not None and spec["blocking"] is False and spec["auto"] is True, str(spec))
    check("the shared tool runs when the repo has no copy", str(TOOL) in spec["command"], str(spec))
    result, inst, notices, spawns = drive(root)
    check("a clean registry: one quiet line",
          result is inst and notices == ["Sensors: study check ✓"], str(notices))

    record(root / "study" / "findings", "2026-11-02-bad", "shut")
    result, inst, notices, spawns = drive(root)
    check("a broken record: the chain advances on the same instance",
          result is inst and not inst.needs_input, str(result))
    check("a broken record: no fix session", spawns == 0)
    check("a broken record: the warning reaches the user",
          len(notices) == 1 and "study check ✗" in notices[0]
          and "2026-11-02-bad.md: status 'shut' needs a verdict" in notices[0], str(notices))

    fork = Path(tmp) / "fork"
    (fork / "tools" / "study").mkdir(parents=True)
    write_config(fork, 'findings_dir = "study/findings"\n')
    (fork / "tools" / "study" / "check.py").write_text(
        'print("the fork\'s own checker"); raise SystemExit(1)\n', encoding="utf-8")
    git(fork, "init", "-q")
    spec = sensors._study_sensor(fork)
    check("a repo with its own copy runs that copy",
          spec["command"] == "python3 -B tools/study/check.py", str(spec))
    result, inst, notices, spawns = drive(fork)
    check("the fork's failure warns too",
          result is inst and spawns == 0 and "the fork's own checker" in notices[0], str(notices))
    check("the check left no bytecode in the repo", not list(fork.rglob("__pycache__")))
    (fork / "tools" / "study" / "check.py").unlink()
    check("a fork with no checker: no sensor, and not the shared one",
          sensors._study_sensor(fork) is None)

    custom = shared_repo(tmp, "custom")
    record(custom / "study" / "findings", "2026-11-02-bad", "shut")
    (custom / ".claude").mkdir()
    (custom / ".claude" / "sensors.json").write_text(json.dumps({
        "sensors": [{"name": "fine", "command": "true"}],
    }), encoding="utf-8")
    result, inst, notices, spawns = drive(custom)
    check("a repo with sensors.json still gets the check",
          "fine ✓" in notices[0] and "study check ✗" in notices[0] and spawns == 0, str(notices))

    main = shared_repo(tmp, "main")
    (main / "readme").write_text("x", encoding="utf-8")
    early = Path(tmp) / "early"
    early.mkdir()
    git(early, "init", "-q")
    report = asyncio.run(sensors.run_sensors(str(early), str(main)))
    check("cwd decides, not the repo path: a worktree forked before the study "
          "folder skips quietly", not any(r.name == "study check" for r in report.results))

    report = sensors.SensorReport(results=[sensors.SensorResult(
        name="study check", command="x", status="timeout", output="timed out after 60s",
        blocking=False)])
    check("a timeout warns and does not fail the report",
          report.passed and report.failures() == [] and "timed out" in report.notice())
    long_report = sensors.SensorReport(results=[sensors.SensorResult(
        name="study check", command="x", status="fail", output="line\n" * 2000, blocking=False)])
    check("a long warning is capped for the message", len(long_report.notice()) < 1100,
          str(len(long_report.notice())))
    check("a passing non-blocking sensor adds nothing",
          sensors.SensorReport(results=[sensors.SensorResult(
              name="s", command="x", status="pass", blocking=False)]).warning_text() == "")
    check("failure_text stays blocking only: a warning never becomes a fix prompt",
          long_report.failure_text() == "")

    # A blocking failure elsewhere still runs its fix loop, without the warning in the prompt.
    mixed = sensors.SensorReport(results=[
        sensors.SensorResult(name="ruff", command="r", status="fail", output="E9", blocking=True),
        sensors.SensorResult(name="study check", command="c", status="fail", output="FAIL rec", blocking=False),
    ])
    check("a mixed report keeps the warning out of the fix prompt",
          "E9" in mixed.failure_text() and "FAIL rec" not in mixed.failure_text())
    check("and keeps it in the notice", "FAIL rec" in mixed.notice())

print("\n[plan review]")
with tempfile.TemporaryDirectory() as tmp:
    root = shared_repo(tmp)
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "Remove the old loader")
    found = root / "study" / "findings"
    record(found, "2026-11-01-no-go", "shut", verdict="The loader cannot be cached.")

    vague = prior_art.collect(str(root), "Make things better in general.")
    check("a plan naming nothing still gets the dead ends",
          "2026-11-01-no-go: The loader cannot be cached." in vague, vague)
    check("under the block's marker, so the evals and split_attached see it",
          vague.startswith(prior_art.PRIOR_ART_MARKER + "\n"), vague)
    check("the block tells the reader what to do with them", "tagged History" in vague, vague)

    both = prior_art.collect(str(root), "Change app.py")
    check("a plan naming a file gets the history and the dead ends",
          "### app.py" in both and "2026-11-01-no-go" in both, both)
    check("the history comes first", both.index("### app.py") < both.index("Dead ends"), both)

    tight = prior_art.collect(str(root), "Change app.py", max_chars=200)
    check("a tight history cap does not touch the dead ends", "2026-11-01-no-go" in tight, tight)

    bare = Path(tmp) / "bare"
    bare.mkdir()
    git(bare, "init", "-q")
    check("no study folder: inert", prior_art.collect(str(bare), "Make things better.") == "")
    check("no plan: inert", prior_art.collect(str(root), "") == "")

    notgit = Path(tmp) / "notgit"
    write_config(notgit, "format = 1\n")
    record(notgit / "study" / "findings", "2026-11-01-z", "shut", verdict="No.")
    check("outside git the list still comes", "2026-11-01-z" in prior_art.collect(str(notgit), "A plan."))

    was = config.PRIOR_ART_ENABLED
    config.PRIOR_ART_ENABLED = False
    try:
        check("the switch turns it off too", prior_art.collect(str(root), "A plan.") == "")
    finally:
        config.PRIOR_ART_ENABLED = was

    real_dead_ends = findings.dead_ends
    def boom(*_args):
        raise RuntimeError("wedged")
    findings.dead_ends = boom
    try:
        check("a reader that raises costs the list, not the history",
              "### app.py" in prior_art.collect(str(root), "Change app.py"))
    finally:
        findings.dead_ends = real_dead_ends
    real_read = prior_art._read_history
    prior_art._read_history = boom
    try:
        check("a history that raises costs the history, not the list",
              "2026-11-01-no-go" in prior_art.collect(str(root), "Change app.py"))
    finally:
        prior_art._read_history = real_read

    # The budget: its own, and clipped from the old end with a way to the rest.
    many = shared_repo(tmp, "many")
    for i in range(200):
        record(many / "study" / "findings", f"2026-{i // 28 + 1:02d}-{i % 28 + 1:02d}-dead-{i:03d}",
               "shut", verdict="x" * 400, date=f"2026-{i // 28 + 1:02d}-{i % 28 + 1:02d}")
    block = prior_art.collect(str(many), "A plan.")
    check("the list stays near its own budget, clip note included",
          len(block) <= config.PRIOR_ART_DEAD_ENDS_MAX_CHARS + 150, str(len(block)))
    check("each line is capped", all(len(line) <= prior_art._DEAD_END_LINE_MAX for line in block.splitlines()[3:-1]))
    check("the newest survive and the oldest go", "dead-199" in block and "dead-000" not in block)
    check("the rest are counted, with where to grep",
          'older not shown. Grep `status = "shut"` in study/findings' in block, block[-200:])

    plan = "Quoting the marker in a plan:\n\n" + prior_art.PRIOR_ART_MARKER + "\nsee?"
    attached = prior_art.attach(plan, prior_art.collect(str(root), "A plan naming nothing."))
    split_plan, tail = prior_art.split_attached(attached)
    check("split_attached gives back a dead-ends-only block", split_plan == plan, split_plan)
    check("and the block keeps its list and its instruction",
          "2026-11-01-no-go" in tail and tail.endswith(prior_art.CHAIN_INSTRUCTION), tail)
    both_attached = prior_art.attach("The plan.", both)
    split_plan, tail = prior_art.split_attached(both_attached)
    check("split_attached gives back history plus dead ends",
          split_plan == "The plan." and "### app.py" in tail and "2026-11-01-no-go" in tail, tail)

    full = prior_art.collect(str(many), "A plan.")
    joined = (full + "\n\n" + prior_art.CHAIN_INSTRUCTION)
    padded = joined + "h" * config.PRIOR_ART_MAX_CHARS
    check("the build brief's allowance holds both caps and the instruction",
          len(padded) <= step_parse._PRIOR_ART_INJECT_MAX, str(len(padded)))

    source = SimpleNamespace(read_result_text=lambda: "Make things better.")
    real_resolve = steps._resolve_chain_repo_path
    steps._resolve_chain_repo_path = lambda _src: str(root)
    try:
        prefix = asyncio.run(steps._prior_art_prefix(source))
    finally:
        steps._resolve_chain_repo_path = real_resolve
    check("the review prefix carries it", "2026-11-01-no-go" in prefix, prefix)

print("\n[what must not move]")
prompt = config.PLAN_REVIEW_PROMPT
check("PRIOR_ATTEMPTS: stays above DEFERRED:",
      prompt.index("PRIOR_ATTEMPTS:") < prompt.index("DEFERRED:"))
check("the commit prompt asks about findings", "study/config.toml" in config.COMMIT_PROMPT)
check("it names a tool that exists", str(TOOL.resolve()) in config.COMMIT_PROMPT)
check("it asks for the same commit", "same commit" in config.COMMIT_PROMPT)
check("the parsed summary block stays last",
      config.COMMIT_PROMPT.rstrip().endswith("```")
      and config.COMMIT_PROMPT.index("study.py") < config.COMMIT_PROMPT.index("```summary"))

print(f"\nresults: {_passed} passed, {_failed} failed")
raise SystemExit(1 if _failed else 0)
