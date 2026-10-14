#!/usr/bin/env python3
"""The offline replay A/B harness (scripts/replay_ab/), without a real CLI.

Everything runs against a synthetic world in a temp dir: a git repo with a
build branch merged into master, a history.jsonl, a bot.log carrying the
build's ``Running`` line, and a fake home with an account whose projects/
holds the session transcript and whose ``.credentials.json`` must never move.

Asserted:

  * extract recovers the session, account, prompt, cut row, base, tip, merge
    and the reference harness from those sources;
  * the sandbox clone holds the base and none of the solution, and a plain
    ``git clone`` of the same repo is flagged as contaminated;
  * an arm that drops a prompt block really removes it, the session-history
    and master-activity blocks are absent from every arm, and a solution sha
    in a system prompt is caught;
  * the command line is the provider's own plus --fork-session right after
    the resumed id and a spend cap, wrapped in a scope, nice and bwrap;
  * the sandbox hides the live checkout, the run root, every other account
    and the run account's transcripts and history behind tmpfs, never the
    home itself, and binds back only the venv, the clone and the fork's own
    transcript dir, after the hiding; a tool call naming a hidden path is
    reported as a peek;
  * the replay's environment drops the parent session's identity and the
    bot's secrets;
  * the conversation fork writes only a transcript, and the credentials file
    is untouched and never copied;
  * scoring runs the reference harness fail-to-pass, tells an import
    mismatch from a wrong fix, runs the session's own harnesses, and a
    re-score never commits the reference harness as the session's work;
  * the spend cap stops new runs, counting runs in flight, and a run that
    raises is recorded without ending the round;
  * destroy removes only the transcript dir keyed on its own clone;
  * a reference harness absent at the tip is "missing", and two arms that
    differ only by name are an A/A round;
  * nothing the harness writes can land under the live data/.

Run: python scripts/test_replay_ab.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "replay_ab"))
sys.path.insert(0, str(ROOT))

import common  # noqa: E402
import extract  # noqa: E402
import main as ab_main  # noqa: E402
import prompt as ab_prompt  # noqa: E402
import run as ab_run  # noqa: E402
import sandbox  # noqa: E402
import score  # noqa: E402

_failures: list[str] = []


def _check(cond: bool, name: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        _failures.append(name)


GENV = {**os.environ, **common.GIT_ENV}


def _g(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), env=GENV, check=True,
                          capture_output=True, text=True).stdout.strip()


SID = "11111111-2222-3333-4444-555555555555"
PROMPT = "Build the widget. Make scripts/test_widget.py pass."
TEST_BODY = (
    "import sys\nfrom pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
    "import widget\nassert widget.VALUE == 42\nprint('ok')\n"
)


def make_world(tmp: Path) -> dict:
    repo = tmp / "live"
    repo.mkdir()
    _g(repo, "init", "-q", "-b", "master")
    (repo / "README.md").write_text("base\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "base")
    base = _g(repo, "rev-parse", "HEAD")
    _g(repo, "checkout", "-q", "-b", "claude-bot/t-1")
    (repo / "widget.py").write_text("VALUE = 42\n")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "test_widget.py").write_text(TEST_BODY)
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "the solution")
    tip = _g(repo, "rev-parse", "HEAD")
    _g(repo, "checkout", "-q", "master")
    (repo / "OTHER.md").write_text("parallel work\n")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-q", "-m", "parallel")
    _g(repo, "merge", "-q", "--no-ff", "-m", "Merge branch 'claude-bot/t-1'", "claude-bot/t-1")
    merge = _g(repo, "rev-parse", "HEAD")

    data = tmp / "data"
    (data / "logs").mkdir(parents=True)
    (data / "history.jsonl").write_text(json.dumps({
        "id": "t-1", "origin": "build", "mode": "build",
        "started": "2026-10-09T17:56:32+00:00", "topic": "widget",
    }) + "\n")
    (data / "logs" / "bot.log").write_text(
        f"2026-10-09 19:56:33 INFO bot.claude.runner: Running t-1 [acct=/me/.claude-x] "
        f"(prompt: {len(PROMPT)} chars via stdin): claude -p --effort high --model opus "
        f"--resume {SID} --permission-mode bypassPermissions\n"
    )

    home = tmp / "home"
    acct = home / ".claude-x"
    proj = acct / "projects" / "-live"
    proj.mkdir(parents=True)
    creds = acct / ".credentials.json"
    creds.write_text('{"claudeAiOauth": {"refreshToken": "do-not-copy"}}')
    rows = [
        {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": SID,
         "timestamp": "2026-10-09T17:00:00Z", "message": {"role": "user", "content": "plan it"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": SID,
         "timestamp": "2026-10-09T17:01:00Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "here is a plan"}]}},
        {"type": "user", "uuid": "u2", "parentUuid": "a1", "sessionId": SID,
         "timestamp": "2026-10-09T17:56:33Z", "message": {"role": "user", "content": PROMPT}},
        {"type": "assistant", "uuid": "a2", "parentUuid": "u2", "sessionId": SID,
         "timestamp": "2026-10-09T17:57:00Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "built it, see " + tip}]}},
    ]
    (proj / f"{SID}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return {"repo": repo, "data": data, "home": home, "acct": acct, "creds": creds,
            "base": base, "tip": tip, "merge": merge}


def test_extract(w: dict):
    case = extract.extract("t-1", data_dir=w["data"], repo=w["repo"], home=w["home"])
    _check(case.session_id == SID, "extract: session id from the run line")
    _check(case.account == str(w["acct"]), "extract: account is the transcript's config dir")
    _check(case.prompt == PROMPT, "extract: prompt verbatim from the transcript")
    _check(case.cut_uuid == "a1", "extract: cut at the build prompt's parent row")
    _check(case.base_sha == w["base"], "extract: base is the merge-base of the merge parents")
    _check(case.ref_tip == w["tip"] and case.merge_sha == w["merge"], "extract: tip and merge")
    _check(case.ref_tests == ["scripts/test_widget.py"], "extract: reference harness found")
    _check(case.model == "opus" and case.effort == "high", "extract: model and effort flags")
    return case


def test_sandbox(case, w: dict, tmp: Path):
    creds_before = w["creds"].read_bytes(), w["creds"].stat().st_mtime_ns
    sb = sandbox.build(case, tmp / "rep")
    _check(sandbox.check_contamination(sb.repo, [case.ref_tip, case.merge_sha]) == [],
           "sandbox: clone has no solution object, ref or remote")
    _check(_g(sb.repo, "rev-parse", "HEAD") == case.base_sha, "sandbox: clone sits on the base")
    _check(_g(sb.repo, "branch", "--show-current") == "replay", "sandbox: on the replay branch")
    _check(not (sb.repo / "widget.py").exists(), "sandbox: solution file absent")

    dirty = tmp / "dirty"
    subprocess.run(["git", "clone", "-q", str(w["repo"]), str(dirty)], check=True)
    probs = sandbox.check_contamination(dirty, [case.ref_tip, case.merge_sha])
    _check(any("present" in p for p in probs) and any("remote" in p for p in probs),
           "sandbox: a plain git clone is flagged as contaminated")

    forked = list(sb.project_dir.iterdir())
    _check([p.name for p in forked] == [f"{sb.session_id}.jsonl"], "fork: only a transcript is written")
    rows = [json.loads(line) for line in forked[0].read_text().splitlines() if line.strip()]
    uuids = [r.get("uuid") for r in rows if r.get("uuid")]
    _check(uuids == ["u1", "a1"], "fork: cut before the build prompt")
    _check(not any(case.ref_tip in json.dumps(r) for r in rows), "fork: transcript carries no solution sha")
    _check((w["creds"].read_bytes(), w["creds"].stat().st_mtime_ns) == creds_before,
           "credentials: file untouched")
    copies = [p for p in tmp.rglob(".credentials.json") if p != w["creds"]]
    _check(copies == [], "credentials: never copied")

    files = {"CLAUDE.md": str(tmp / "variant.md")}
    (tmp / "variant.md").write_text("trimmed\n")
    sb2 = sandbox.build(case, tmp / "rep2", files)
    _check((sb2.repo / "CLAUDE.md").read_text() == "trimmed\n" and sb2.start_sha != case.base_sha,
           "arm files: overlaid as a setup commit")
    _check(_g(sb2.repo, "status", "--porcelain") == "", "arm files: tree clean after setup")
    wrong = sandbox.destroy(tmp / "rep2", sb.project_dir)
    _check(wrong == {"clone": True, "transcripts": False} and sb.project_dir.is_dir(),
           "destroy: another clone's transcript dir is refused")
    _check(not (tmp / "rep2" / "repo").exists(), "destroy: clone removed")
    right = sandbox.destroy(tmp / "rep2", sb2.project_dir)
    _check(right["transcripts"] and not sb2.project_dir.exists(), "destroy: its own transcript dir removed")
    return sb


def test_prompt(case, sb):
    cur = ab_prompt.build({"name": "current"}, case=case, sandbox_repo=sb.repo,
                          session_id=sb.session_id, system_prompt_file=sb.root / "a.txt")
    drop = ab_prompt.build({"name": "nohonesty", "drop_blocks": ["HONESTY_CONSTRAINT"]}, case=case,
                           sandbox_repo=sb.repo, session_id=sb.session_id,
                           system_prompt_file=sb.root / "b.txt")
    marker = "--- Honesty & Verification ---"
    _check(marker in cur["system_prompt"], "prompt: current arm carries the block")
    _check(marker not in drop["system_prompt"], "prompt: drop_blocks removes it")
    _check((sb.root / "a.txt").read_text() == cur["system_prompt"], "prompt: written to its file")
    for arm, out in (("current", cur), ("drop", drop)):
        _check(ab_main.leaks(out["system_prompt"], case) == [],
               f"prompt: {arm} arm has no session history, master activity or solution sha")
    _check(str(sb.repo) in cur["system_prompt"], "prompt: location block names the clone")
    _check(ab_main.leaks("x " + case.ref_tip[:10], case) != [], "leaks: a solution sha is caught")

    try:
        ab_prompt.build({"name": "bad", "drop_blocks": ["NO_SUCH_BLOCK"]}, case=case,
                        sandbox_repo=sb.repo, session_id=None, system_prompt_file=sb.root / "c.txt")
        _check(False, "prompt: unknown block refused")
    except RuntimeError:
        _check(True, "prompt: unknown block refused")

    cmd = ab_run.finalize_command(cur["cmd"], budget_usd=3)
    i = cmd.index("--resume")
    _check(cmd[i + 1] == sb.session_id and cmd[i + 2] == "--fork-session",
           "command: --fork-session right after the resumed id")
    _check(cmd[-2:] == ["--max-budget-usd", "3.00"], "command: spend cap")
    for flag in ("-p", "--output-format", "--append-system-prompt-file", "--permission-mode",
                 "--model", "--effort"):
        _check(flag in cmd, f"command: provider flag {flag}")
    _check(ab_run.finalize_command(cmd, budget_usd=3).count("--fork-session") == 1,
           "command: finalize is idempotent")

    wrapped = ab_run.wrap(cmd, repo=sb.repo, unit="replay-ab-x.scope", live_root=Path("/live"))
    _check(wrapped[:3] == ["systemd-run", "--user", "--scope"] and
           "--slice=app-claudesessions.slice" in wrapped, "wrap: session scope in the sessions slice")
    j = wrapped.index("bwrap")
    seg = wrapped[j:wrapped.index("--", j + 1)]
    _check(["--bind", str(sb.repo), str(sb.repo)] == seg[seg.index("--bind"):seg.index("--bind") + 3],
           "wrap: clone writable")
    _check(seg[seg.index("--chdir") + 1] == str(sb.repo), "wrap: starts in the clone")
    _check(wrapped[wrapped.index("nice") + 2] == "10", "wrap: niced like a session")
    _check(wrapped[-len(cmd):] == cmd, "wrap: command passed through unchanged")


def test_env():
    saved = dict(os.environ)
    os.environ.update({"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x", "DISCORD_TOKEN": "t",
                       "ANTHROPIC_API_KEY": "k", "CLAUDE_EFFORT": "high"})
    try:
        env = ab_run.child_env("/acct")
    finally:
        os.environ.clear()
        os.environ.update(saved)
    _check(not any(k in env for k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "DISCORD_TOKEN",
                                       "ANTHROPIC_API_KEY")), "env: parent identity and secrets dropped")
    _check(env.get("CLAUDE_EFFORT") == "high", "env: session tuning kept")
    _check(env.get("CLAUDE_CONFIG_DIR") == "/acct", "env: account config dir set")


def test_score_and_cap(case, w: dict, tmp: Path):
    sb = sandbox.build(case, tmp / "s1")
    common.write_json(sb.root / "run.json", {"label": "t-1/a/r0", "returncode": 0, "num_turns": 3,
                                             "total_cost_usd": 1.5})
    s = score.score(sb.root, case, start_sha=sb.start_sha)
    _check(s["fail_to_pass"]["total"] == 1 and s["fail_to_pass"]["passed"] == 0,
           "score: no work, reference harness fails")

    sb2 = sandbox.build(case, tmp / "s2")
    (sb2.repo / "widget.py").write_text("VALUE = 42\n")
    common.write_json(sb2.root / "run.json", {"label": "t-1/b/r0", "returncode": 0, "num_turns": 5,
                                              "total_cost_usd": 2.0})
    s2 = score.score(sb2.root, case, start_sha=sb2.start_sha)
    _check(s2["fail_to_pass"]["all_passed"], "score: the right change passes the reference harness")
    _check(s2["diff"]["files"] == ["widget.py"], "score: diff counts the session's file only")
    _check(s2["diff"]["file_jaccard"] == 0.5, "score: overlap against the reference's files")

    s2b = score.score(sb2.root, case, start_sha=sb2.start_sha)
    _check(s2b["snapshot_sha"] == s2["snapshot_sha"] and s2b["diff"]["files"] == ["widget.py"],
           "re-score: the pinned snapshot, the reference harness never counted as work")
    _check(_g(sb2.repo, "status", "--porcelain") == "", "re-score: tree left clean")

    # Same fix under another name: the reference harness cannot import it.
    sb3 = sandbox.build(case, tmp / "s3")
    (sb3.repo / "gadget.py").write_text("VALUE = 42\n")
    (sb3.repo / "scripts").mkdir()
    (sb3.repo / "scripts" / "test_gadget.py").write_text(
        TEST_BODY.replace("import widget", "import gadget as widget"))
    common.write_json(sb3.root / "run.json", {"label": "t-1/c/r0", "returncode": 0})
    s3 = score.score(sb3.root, case, start_sha=sb3.start_sha)
    _check(s3["fail_to_pass"]["interface"] == 1 and
           s3["fail_to_pass"]["tests"]["scripts/test_widget.py"]["kind"] == "interface",
           "score: an import failure is an interface mismatch, not a wrong fix")
    _check(s3["own_tests"]["total"] == 1 and s3["own_tests"]["passed"] == 1,
           "score: the session's own harness runs on its own tree")
    _check(score.classify({"passed": False, "tail": "AssertionError"}) == "fail",
           "score: an assertion failure is a fail")

    stream = tmp / "stream.jsonl"
    stream.write_text("\n".join(json.dumps(e) for e in [
        {"type": "system", "subtype": "init", "session_id": "new-sid"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit"}]}},
        {"type": "system", "subtype": "compact_boundary"},
        {"type": "result", "num_turns": 4, "total_cost_usd": 0.75, "is_error": False,
         "usage": {"input_tokens": 10, "output_tokens": 20}, "result": "done"},
    ]) + "\n")
    st = ab_run.summarize_stream(stream)
    _check(st["session_id"] == "new-sid" and st["num_turns"] == 4 and st["total_cost_usd"] == 0.75
           and st["tools"] == {"Edit": 1} and st["compactions"] == 1, "stream: summary fields")

    ran = []

    def fake_run_one(job, log=print):
        ran.append(job.label)
        return {"total_cost_usd": 5.0}

    orig_one, orig_room = ab_run.run_one, ab_run.slice_headroom_mb
    ab_run.run_one, ab_run.slice_headroom_mb = fake_run_one, lambda: None
    try:
        jobs = [ab_run.Job(label=f"j{i}", workdir=tmp, repo=tmp, account="", cmd=[], prompt="",
                           budget_usd=1, timeout_s=1) for i in range(5)]
        ab_run.run_all(jobs, concurrency=1, total_cap_usd=9.0, log=lambda *_: None)
    finally:
        ab_run.run_one, ab_run.slice_headroom_mb = orig_one, orig_room
    _check(ran == ["j0", "j1"], "cap: no new run starts once spend reaches the cap")
    _check([j.result.get("skipped") for j in jobs[2:]] == [True] * 3, "cap: unstarted runs marked skipped")

    # Two in flight at $4 caps each under $9: the third waits for one to come
    # in, and starts only because it came in under its cap.
    ran.clear()
    scored = []

    def flaky_run_one(job, log=print):
        ran.append(job.label)
        time.sleep(0.2)
        if job.label == "k0":
            raise OSError("no bwrap")
        return {"label": job.label, "total_cost_usd": 1.0}

    ab_run.run_one, ab_run.slice_headroom_mb = flaky_run_one, lambda: None
    try:
        jobs = [ab_run.Job(label=f"k{i}", workdir=tmp, repo=tmp, account="", cmd=[], prompt="",
                           budget_usd=4, timeout_s=1) for i in range(4)]
        res = ab_run.run_all(jobs, concurrency=3, total_cap_usd=9.0, log=lambda *_: None,
                             on_done=lambda j: scored.append(j.label))
    finally:
        ab_run.run_one, ab_run.slice_headroom_mb = orig_one, orig_room
    _check(sorted(ran) == ["k0", "k1", "k2", "k3"], "cap: in-flight caps reserve room, runs resume as they land")
    _check(any(r.get("error", "").startswith("OSError") for r in res) and "k0" not in scored
           and sorted(scored) == ["k1", "k2", "k3"], "run_all: a run that raises is recorded, the round goes on")

    missing = score.fail_to_pass(sb.repo, Path(case.repo_path), case.ref_tip, ["scripts/test_nope.py"])
    _check(missing["tests"]["scripts/test_nope.py"]["kind"] == "missing" and missing["passed"] == 0,
           "score: a harness absent at the tip is missing")
    _check(ab_main.same_arm({"name": "a"}, {"name": "b"})
           and not ab_main.same_arm({"name": "a"}, {"name": "b", "model": "haiku"}),
           "aa: arms equal but for the name are an A/A round")

    run_dir = tmp / "runs" / "r"
    for arm, src in (("a", sb.root), ("b", sb2.root)):
        d = run_dir / "t-1" / arm / "r0"
        d.mkdir(parents=True)
        (d / "score.json").write_text((src / "score.json").read_text())
    common.write_json(run_dir / "run.json", {"arms": [{"name": "a"}, {"name": "b"}]})
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ab_main.cmd_report(type("A", (), {"run_dir": str(run_dir)})())
    _check(rc == 0 and "paired pass (b - a): 1.00" in buf.getvalue(), "report: paired delta printed")


def test_hiding(tmp: Path):
    home = tmp / "home"
    own, other = home / ".claude-a", home / ".claude-b"
    live, runs = tmp / "live", tmp / "runs"
    for d in (own / "projects" / "p", own / "plans", other / "projects", live / ".venv" / "bin",
              live / "data" / "results", runs / "x" / "repo"):
        d.mkdir(parents=True)
    for f in (own / "history.jsonl", own / "settings.json"):
        f.write_text("{}\n")
    repo, pd = runs / "x" / "repo", own / "projects" / "p"
    hidden = ab_run.hidden_paths(account=own, live_root=live, runs_root=runs, home=home)
    _check({live, runs, other, own / "projects", own / "plans", own / "history.jsonl"} <= set(hidden),
           "hide: live checkout, run root, other accounts, own transcripts and history")
    _check(own not in hidden and own / "settings.json" not in hidden and own / "todos" not in hidden,
           "hide: own account settings kept, absent paths skipped")
    _check(home not in ab_run.hidden_paths(account=own, live_root=live, runs_root=home, home=home),
           "hide: never the home directory")

    args = ab_run.sandbox_args(repo, live, account=own, project_dir=pd, runs_root=runs, home=home,
                               contain=True)
    last_hide = max(i for i, a in enumerate(args) if a in ("--tmpfs", "--dev-bind") and i > 3)
    binds = {args[i + 1]: (i, args[i]) for i, a in enumerate(args) if a in ("--bind", "--ro-bind")}
    _check(binds.get(str(live / ".venv"), (0, ""))[1] == "--ro-bind"
           and binds.get(str(pd), (0, ""))[1] == "--bind" and binds.get(str(repo), (0, ""))[1] == "--bind",
           "hide: venv read-only, clone and its transcript dir writable")
    _check(all(i > last_hide for i, _ in binds.values()), "hide: binds come after the hiding")
    _check(["--dev-bind", "/dev/null", str(own / "history.jsonl")] ==
           args[args.index(str(own / "history.jsonl")) - 2:args.index(str(own / "history.jsonl")) + 1],
           "hide: a file is masked with a device bind of /dev/null")
    _check("--unshare-pid" in args and "--unshare-pid" not in ab_run.sandbox_args(repo, live, home=home),
           "hide: scoring gets its own pid namespace")

    texts = [json.dumps({"command": f"cat {live}/data/results/t-1.md && {live}/.venv/bin/python x"}),
             json.dumps({"file_path": f"{repo}/a.py"}), json.dumps({"file_path": f"{pd}/s.jsonl"})]
    peeks = ab_run.find_peeks(texts, hidden, [live / ".venv", pd, repo])
    _check(peeks == [f"{live}/data/results/t-1.md"], "peek: a hidden path is caught, kept paths are not")


def test_clone_path(case, tmp: Path):
    try:
        sandbox.make_clone(Path(case.repo_path), case.base_sha, tmp / "bad_name" / "repo")
        _check(False, "clone path: an underscore is refused")
    except RuntimeError as e:
        _check("encode differently" in str(e), "clone path: an underscore is refused")
    _check(ab_prompt.load_arm({"name": "trim claude_md"})["name"] == "trim-claude-md",
           "arm name: made path-safe")


def test_live_data_guard():
    try:
        common.assert_outside_live_data(common.LIVE_DATA / "x")
        _check(False, "guard: a path under the live data/ is refused")
    except RuntimeError:
        _check(True, "guard: a path under the live data/ is refused")
    _check(not common.under(common.RUNS_ROOT, common.LIVE_DATA), "guard: run root is outside data/")
    src = "".join(p.read_text() for p in (ROOT / "scripts" / "replay_ab").glob("*.py"))
    _check("from bot import config" not in src.replace("    from bot import config", ""),
           "structure: bot.config is imported only inside the prompt child")
    _check(".credentials" not in src.replace("``.credentials.json``", ""),
           "structure: nothing names the credentials file but a docstring")


def main() -> int:
    # No underscore anywhere in the path: sandbox.check_clone_path refuses one,
    # and tempfile's random suffix can contain one.
    tmp = Path(tempfile.gettempdir()) / f"test-replay-ab-{secrets.token_hex(4)}"
    tmp.mkdir()
    try:
        w = make_world(tmp)
        case = test_extract(w)
        sb = test_sandbox(case, w, tmp)
        test_prompt(case, sb)
        test_env()
        test_score_and_cap(case, w, tmp)
        test_hiding(tmp)
        test_clone_path(case, tmp)
        test_live_data_guard()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: " + "; ".join(_failures))
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
