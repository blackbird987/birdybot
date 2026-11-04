"""Harness for the judge and the step-back (docs/design/judge.md).

The checks that matter: the judge's input and prompt carry no author text, its
parser fails closed, a hold holds, repair rounds stop, commits are attributed from
the session's own tool results, and an unlisted repo changes nothing at all.
"""

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import ast
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import config  # noqa: E402
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType  # noqa: E402
from bot.engine import cmd_state, judge, lifecycle, stepback  # noqa: E402
from bot.platform.buttons import action_button_specs  # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
        print(f"FAIL {name} {detail}")


def block(body):
    return f"```verdict\n{body}\n```"


def instance(**kw):
    inst = Instance(
        id=kw.pop("id", "t-1"), name="n", instance_type=InstanceType.TASK, prompt="x",
        mode="build", repo_name=kw.pop("repo_name", "aiagent"),
        repo_path=kw.pop("repo_path", "/nonexistent"),
        status=kw.pop("status", InstanceStatus.COMPLETED),
    )
    for k, v in kw.items():
        setattr(inst, k, v)
    return inst


# --- parse fails closed ---
P = judge.parse_verdict
check("pass", P(block("VERDICT: pass")).kind == judge.PASS)
check("fix", P(block("VERDICT: fix\nPROBLEMS:\n- a.py: wrong\n- b.py: wrong")).problems == ("a.py: wrong", "b.py: wrong"))
check("only_you", P(block("VERDICT: only_you\nLINE: look at the ship")).line == "look at the ship")
check("could_not_check", P(block("VERDICT: could_not_check\nLINE: no output")).kind == judge.COULD_NOT_CHECK)
for name, text in {
    "none": None,
    "empty": "",
    "prose": "I think this passes.",
    "unfenced": "VERDICT: pass",
    "other fence": "```verify\nVERDICT: pass\n```",
    "unknown": block("VERDICT: approve"),
    "two": block("VERDICT: pass\nVERDICT: fix"),
    "pass and problems": block("VERDICT: pass\nPROBLEMS:\n- a.py"),
    "fix no problems": block("VERDICT: fix"),
    "only_you no line": block("VERDICT: only_you"),
    "could_not_check no line": block("VERDICT: could_not_check"),
    "open block": "```verdict\nVERDICT: pass\n",
    "case": block("verdict: pass"),
    "two words": block("VERDICT: pass maybe"),
}.items():
    check(f"closed: {name}", P(text).kind == judge.COULD_NOT_CHECK, P(text))
check("last block wins", P(block("VERDICT: fix\nPROBLEMS:\n- a") + "\n" + block("VERDICT: pass")).kind == judge.PASS)
check("template quoted then real", P(block("VERDICT: pass") + "\n" + block("VERDICT: bad")).kind == judge.COULD_NOT_CHECK)

check("no run", judge.verdict_of_run(None).kind == judge.COULD_NOT_CHECK)
for st in (InstanceStatus.FAILED, InstanceStatus.KILLED):
    check(f"run {st.value}", judge.verdict_of_run(instance(status=st)).kind == judge.COULD_NOT_CHECK)
run = instance()
with patch.object(Instance, "read_result_text", return_value=block("VERDICT: pass")):
    check("completed reads the block", judge.verdict_of_run(run).kind == judge.PASS)
with patch.object(Instance, "read_result_text", return_value=""):
    check("completed with no text", judge.verdict_of_run(run).kind == judge.COULD_NOT_CHECK)

# --- the input carries no author text ---
AUTHOR = "AUTHOR-SUMMARY-MARKER"
verify_text = f"{AUTHOR} I am sure this works.\n```verify\nRESULT: pass\nTESTS_RUN: 3\n```\n{AUTHOR}"
with patch.object(Instance, "read_result_text", return_value=verify_text):
    vo = judge.verify_output_of(instance())
check("verify keeps the block", "RESULT: pass" in vo)
check("verify drops the prose", AUTHOR not in vo, vo)
text = judge.build_input(["owner words one", "owner words two"], "the goal", "diff body", vo)
for want in ("owner words one", "owner words two", "the goal", "diff body", "RESULT: pass"):
    check(f"input has {want}", want in text)
check("input no author", AUTHOR not in text)
fields = set(judge.build_input.__code__.co_varnames[:judge.build_input.__code__.co_argcount])
check("no way to pass a summary", fields == {"owner_messages", "goal", "diff", "verify_output", "notes"}, fields)

sys_prompt_inst = instance(origin=InstanceOrigin.JUDGE)
from bot.claude.runner.prompt_build import PromptMixin  # noqa: E402
mixin = PromptMixin.__new__(PromptMixin)
mixin.provider = MagicMock()
sp = mixin._build_system_prompt(sys_prompt_inst, context="PINNED-MARKER", sibling_context="SIBLING-MARKER")
check("system prompt is the judge's own", sp == config.JUDGE_SYSTEM_PROMPT)
check("no pinned or sibling text", "PINNED-MARKER" not in sp and "SIBLING-MARKER" not in sp)
check("shared question asked", config.SCOPE_QUESTION in sp)
for bad in ("Recent Sessions", "Sibling Sessions", "Recent master activity", "BOT_CMD"):
    check(f"system prompt lacks {bad}", bad not in sp)
sp2 = mixin._build_system_prompt(instance(origin=InstanceOrigin.STEPBACK), context="PINNED-MARKER")
check("stepback prompt", sp2 == config.STEPBACK_SYSTEM_PROMPT and config.SCOPE_QUESTION in sp2)

# --- sizes are stated when cut ---
big = judge.build_input(["x" * 100] * 1000, "g", "d" * (judge._MAX_DIFF_CHARS + 5), "")
check("owner cut stated", "owner messages were left out" in big)
check("diff cut stated", "The diff was cut" in big)
check("no verify stated", "There is no verify output" in big)
small = judge.build_input(["a"], "g", "d", "RESULT: pass")
check("nothing cut, nothing stated", "LEFT OUT" not in small)
msgs = [f"m{i} " + "y" * 3000 for i in range(30)]
kept, dropped = judge._cap_owner_messages(msgs)
check("first and last kept", kept[0] == msgs[0] and kept[-1] == msgs[-1] and dropped > 0)
check("cap holds", sum(len(m) for m in kept) <= judge._MAX_OWNER_CHARS)

# --- wiring ---
check("no directives", {InstanceOrigin.JUDGE, InstanceOrigin.STEPBACK} <= lifecycle._NO_DIRECTIVE_ORIGINS)
for origin in (InstanceOrigin.JUDGE, InstanceOrigin.STEPBACK):
    specs = action_button_specs(instance(origin=origin), show_expand=False)
    check(f"{origin.value} has no workflow buttons", specs == [], specs)

# --- off by default, and unlisted changes nothing ---
src = (ROOT / "bot/config.py").read_text(encoding="utf-8")
check("default is empty", 'os.getenv("JUDGE_REPOS", "")' in src)
check("nothing listed", not judge.enabled_for("aiagent") or "aiagent" in os.environ.get("JUDGE_REPOS", ""))
check("no repo name", not judge.enabled_for(None) and not judge.enabled_for(""))
with patch.object(config, "JUDGE_REPOS", frozenset({"aiagent"})):
    check("listed", judge.enabled_for("aiagent"))
    check("other repo", not judge.enabled_for("bot"))
check("not read from a repo file", "workflow" not in Path(judge.__file__).read_text(encoding="utf-8").split("def enabled_for")[1].split("\n\n")[0])

chain_src = (ROOT / "bot/engine/chain_runner.py").read_text(encoding="utf-8")
hook = chain_src[chain_src.index('elif step == "verify":'):chain_src.index('elif step == "done":')]
check("chain only calls the gate behind the check", "if result and judge.enabled_for(result.repo_name):" in hook)
check("gate call is inside it", hook.index("enabled_for") < hook.index("gate_after_verify"))

idle_src = (ROOT / "bot/discord/idle.py").read_text(encoding="utf-8")
fn = idle_src[idle_src.index("def _start_idle_judge"):]
check("idle stops before anything for an unlisted repo", fn.index("enabled_for") < fn.index("_ctx(") < fn.index("create_task"))


# --- the chain gate ---
def gate_ctx(owner=("do the thing",)):
    ctx = MagicMock()
    ctx.channel_id = "99"
    ctx.platform = "discord"
    ctx.messenger.owner_messages = AsyncMock(return_value=list(owner) if owner is not None else None)
    ctx.messenger.send_text = AsyncMock()
    ctx.runner.check_spawn_allowed = MagicMock(return_value=None)
    ctx.runner.active_instance_for_channel = MagicMock(return_value=None)
    return ctx


def run_gate(verdicts, *, owner=("do the thing",), diff=("d", ["c1"], ()), more_steps=True, reverify=None):
    """Drive gate_after_verify with scripted judge runs. Returns what happened."""
    ctx = gate_ctx(owner)
    calls = {"judge": 0, "repair": 0, "verify": 0, "prompts": []}
    results = iter(verdicts)

    async def fake_spawn(c, source_id, cfg, **kw):
        if cfg.origin is InstanceOrigin.JUDGE:
            calls["judge"] += 1
            calls["prompts"].append(cfg)
            r = instance(id=f"j-{calls['judge']}")
            r._text = next(results)
            return r
        calls["repair"] += 1
        calls["repair_cfg"] = cfg
        return instance(id=f"r-{calls['repair']}")

    async def fake_verify(c, source_id, msg=None):
        calls["verify"] += 1
        if reverify is not None:
            return reverify
        return instance(id=f"v-{calls['verify']}")

    with tempfile.TemporaryDirectory() as td, \
            patch.object(config, "DATA_DIR", Path(td)), \
            patch.object(judge, "spawn_from", fake_spawn), \
            patch.object(judge, "on_verify", fake_verify), \
            patch.object(judge, "_branch_diff", AsyncMock(return_value=diff)), \
            patch.object(judge, "repo_goal", lambda p: "goal"), \
            patch.object(judge, "verify_output_of", lambda i: ""), \
            patch.object(Instance, "read_result_text", lambda self: getattr(self, "_text", "")):
        src = instance(id="v-0")
        out = asyncio.run(judge.gate_after_verify(ctx, src, more_steps=more_steps))
        rows = [json.loads(ln) for ln in (Path(td) / "judge.jsonl").read_text().splitlines()]
    return out, ctx, calls, rows


PASS_B = block("VERDICT: pass")
FIX_B = block("VERDICT: fix\nPROBLEMS:\n- a.py: wrong")
ONLY_B = block("VERDICT: only_you\nLINE: look at the ship")
NOPE_B = block("VERDICT: could_not_check\nLINE: no screenshot")

out, ctx, calls, rows = run_gate([PASS_B])
check("pass carries on", not out.needs_input and out.id == "v-0")
check("pass says nothing", ctx.messenger.send_text.await_count == 0)
check("pass is recorded", rows[0]["verdict"] == "pass" and rows[0]["commits"] == ["c1"])
cfg = calls["prompts"][0]
check("judge is fresh", cfg.resume_session is False)
check("judge is on the floor", cfg.permission_mode == "explore" and cfg.mode == "explore")
check("judge prompt has the owner's words", "do the thing" in cfg.prompt)

for name, text in (("only_you", ONLY_B), ("could_not_check", NOPE_B), ("garbage", "no block")):
    out, ctx, calls, rows = run_gate([text])
    check(f"{name} holds", out.needs_input)
    check(f"{name} flags the hand-off", out.needs_manual_verification and out.manual_verify_reason)
    check(f"{name} shows its line", ctx.messenger.send_text.await_count == 1)
    check(f"{name} offers Continue", ctx.messenger.send_text.await_args.kwargs["buttons"][0][0].callback_data == "continue_anyway:v-0")
    check(f"{name} no repair", calls["repair"] == 0)
out, ctx, *_ = run_gate([ONLY_B], more_steps=False)
check("nothing after verify, no Continue", ctx.messenger.send_text.await_args.kwargs["buttons"] is None)
check("line reaches the owner", "look at the ship" in run_gate([ONLY_B])[1].messenger.send_text.await_args.args[1])

out, ctx, calls, rows = run_gate([FIX_B, PASS_B])
check("fix then pass", not out.needs_input and out.id == "v-1")
check("fix goes back to the author", calls["repair"] == 1 and calls["repair_cfg"].resume_session and calls["repair_cfg"].mode == "build")
check("repair is told the problems", "a.py: wrong" in calls["repair_cfg"].prompt)
check("repaired work is re-verified and re-judged", calls["verify"] == 1 and calls["judge"] == 2)

out, ctx, calls, rows = run_gate([FIX_B, FIX_B, FIX_B])
check("round limit: three judgements", calls["judge"] == 3)
check("round limit: two repairs", calls["repair"] == judge.MAX_REPAIR_ROUNDS == 2)
check("round limit: holds", out.needs_input)
check("round limit: says so", "repair rounds" in ctx.messenger.send_text.await_args.args[1])
check("every round recorded", [r["round"] for r in rows] == [0, 1, 2])

blocked = instance(id="v-x", needs_input=True)
out, ctx, calls, rows = run_gate([FIX_B], reverify=blocked)
check("a blocked re-verify stops the loop", out is blocked and calls["judge"] == 1)

out, ctx, calls, rows = run_gate([], owner=None)
check("unreadable thread holds without a run", out.needs_input and calls["judge"] == 0)
out, ctx, calls, rows = run_gate([], owner=())
check("no owner message holds without a run", out.needs_input and calls["judge"] == 0)
out, ctx, calls, rows = run_gate([], diff=(None, [], ()))
check("no diff holds without a run", out.needs_input and calls["judge"] == 0)


async def _spawn_none(*a, **k):
    return None

ctx = gate_ctx()
with tempfile.TemporaryDirectory() as td, patch.object(config, "DATA_DIR", Path(td)), \
        patch.object(judge, "spawn_from", _spawn_none), \
        patch.object(judge, "_branch_diff", AsyncMock(return_value=("d", [], ()))), \
        patch.object(judge, "repo_goal", lambda p: ""), patch.object(judge, "verify_output_of", lambda i: ""):
    out = asyncio.run(judge.gate_after_verify(ctx, instance(id="v-0"), more_steps=True))
check("a judge that cannot start holds", out.needs_input)


async def _boom(*a, **k):
    raise RuntimeError("boom")

ctx = gate_ctx()
with tempfile.TemporaryDirectory() as td, patch.object(config, "DATA_DIR", Path(td)), \
        patch.object(judge, "_branch_diff", _boom):
    out = asyncio.run(judge.gate_after_verify(ctx, instance(id="v-0"), more_steps=True))
check("an internal error holds", out.needs_input)

for bad in (None, instance(id="v-q", needs_input=True), instance(id="v-f", status=InstanceStatus.FAILED)):
    ctx = gate_ctx()
    with patch.object(judge, "spawn_from", _boom):
        out = asyncio.run(judge.gate_after_verify(ctx, bad, more_steps=True))
    check("the chain's own guard handles an unfinished verify", out is bad)


# --- attribution ---
from bot.engine.commit_calls import commit_subject, match_commits, read_commit_calls  # noqa: E402


def session(tmp, *turns, ts="2026-11-04T10:00:00Z", name="s.jsonl"):
    """turns: (command, output), in the transcript's real shape."""
    path = Path(tmp) / name
    with path.open("w") as f:
        for i, (cmd, output) in enumerate(turns):
            f.write(json.dumps({"type": "assistant", "timestamp": ts, "message": {"content": [
                {"type": "tool_use", "id": f"u{i}", "name": "Bash", "input": {"command": cmd}},
            ]}}) + "\n")
            # The same blocks come round again under another row type.
            f.write(json.dumps({"type": "api-request-blob", "message": {"content": [
                {"type": "tool_use", "id": f"u{i}", "name": "Bash", "input": {"command": cmd}},
            ]}}) + "\n")
            if output is not None:
                f.write(json.dumps({"type": "user", "timestamp": ts, "message": {"content": [
                    {"type": "tool_result", "tool_use_id": f"u{i}", "content": output},
                ]}}) + "\n")
    return path


for cmd, want in {
    'git commit -m "plain one"': "plain one",
    'git add -A && git commit -qm "quiet one\n\nbody"': "quiet one",
    "git commit -q -m 'single quoted'": "single quoted",
    'git commit -q -m "esc \\"aped\\" \\$x"': 'esc "aped" $x',
    "git add -A && git commit -q -F - <<'EOF'\nfrom a heredoc\n\nbody\nEOF": "from a heredoc",
    "git commit -q -m \"$(cat <<'EOF'\nfrom cat\n\nbody\nEOF\n)\"": "from cat",
    "git -C /r commit -qm 'with dash c'": "with dash c",
    "GIT_AUTHOR_NAME=x git commit -qm 'with env'": "with env",
    "git commit --amend --no-edit": None,
    "git commit -F msg.txt": None,
    "cat > s.py <<'PY'\nrun('git add . && git commit -qm \"inside\"')\nPY\ngit commit -qm 'after a script'": "after a script",
    "cat > s.sh <<'SH'\ngit commit -qm 'inside'\nSH": None,
}.items():
    check(f"subject of {cmd[:30]!r}", commit_subject(cmd) == want, commit_subject(cmd))

with tempfile.TemporaryDirectory() as td:
    f = session(td,
                ('git add -A && git commit -m "x"', "[master abc1234] x\n 1 file changed"),
                ('git -C /r commit -qm "quiet one"', ""),
                ("git log --grep commit", "commit deadbee"),
                ("echo git commit", "git commit"),
                ('git commit -m "nope"', "nothing to commit, working tree clean"),
                ("git commit --dry-run", "[master 1111111] no"),
                ('git commit -qm "hook said no"', "error: pre-commit hook failed"),
                ('git commit -qm "never answered"', None))
    calls = read_commit_calls(f)
    check("calls found, duplicates dropped", [c.tool_id for c in calls] == ["u0", "u1", "u4", "u6", "u7"], calls)
    now = calls[0].ts
    log = [("abc1234" + "0" * 33, "x", now), ("b" * 40, "quiet one", now + 5), ("c" * 40, "other session", now)]
    shas, un = match_commits(calls, log)
    check("hash and quiet subject both pinned", shas == [log[0][0], log[1][0]], shas)
    check("a refusal is no commit, an unanswered call is unknown", un == ["u7"], un)
    check("another session's commit is not taken", log[2][0] not in shas)
    quiet = [c for c in calls if c.tool_id == "u1"]
    check("same subject twice is not guessed", match_commits(quiet, [("a" * 40, "quiet one", now), ("b" * 40, "quiet one", now)]) == ([], ["u1"]))
    check("same subject long ago does not count", match_commits(quiet, [("a" * 40, "quiet one", now - 86400)]) == ([], ["u1"]))
    check("nothing matching is unattributed", match_commits(quiet, []) == ([], ["u1"]))
    f = session(td, ("git commit -q -F /tmp/m.txt && git log --oneline -3", "M  a.py\nb" + "b" * 6 + " file subject\nc" + "c" * 6 + " older work"), name="printed.jsonl")
    printed = read_commit_calls(f)
    rows = [("b" * 40, "file subject", now), ("c" * 40, "older work", now)]
    check("a printed line pins a quiet commit, and only the newest", match_commits(printed, rows) == ([rows[0][0]], []), printed)
    check("a printed line naming no real commit does not count", match_commits(printed, [("d" * 40, "x", now)]) == ([], ["u0"]))
    f = session(td, ("git commit-graph write", ""), ("git commit-tree abc", ""), name="plumbing.jsonl")
    check("plumbing is not a commit", read_commit_calls(f) == [])
    f = session(td, ('git commit -qm "a" && git add . && git commit -qm "b"', "[master abc1234] a\n[master def5678] b"), name="two.jsonl")
    two = read_commit_calls(f)
    check("two commits in one call are not guessed", match_commits(two, [("abc1234" + "0" * 33, "a", now), ("def5678" + "0" * 33, "b", now)]) == ([], ["u0"]), two)
    f = session(td, ("cat > s.sh <<'SH'\ngit add . && git commit --dry-run\ngit commit -qm 'inside'\nSH", ""), name="script.jsonl")
    check("a script that mentions a commit is no commit call", read_commit_calls(f) == [])
    f = session(td, ("cat > s.sh <<'SH'\ngit commit -qm 'inside'\nSH\ngit commit -qm 'real one'", ""), name="script2.jsonl")
    after = read_commit_calls(f)
    check("a real commit after a script is one call, under its own subject",
          [c.subject for c in after] == ["real one"], after)
    f = session(td, ("git commit --amend --no-edit", ""), name="amend.jsonl")
    check("no subject and no hash is unattributed", match_commits(read_commit_calls(f), []) == ([], ["u0"]))


NOW_TS = datetime.now(timezone.utc).isoformat()


def idle_repo(td):
    repo = Path(td) / "r"
    repo.mkdir()
    def g(*a):
        return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (repo / "CLAUDE.md").write_text("A goal.\n")
    g("add", ".")
    g("commit", "-qm", "AUTHOR-SUBJECT-MARKER one")
    (repo / "a.py").write_text("x = 1\n")
    g("add", ".")
    g("commit", "-qm", "AUTHOR-SUBJECT-MARKER two")
    return repo, g("rev-parse", "--short", "HEAD"), g("rev-parse", "HEAD")


def run_idle(td, repo, sess, *, verdict=PASS_B, idle=True, chain=False, closed=False, owner=("ask",), repair_ok=True, draining=None, budget=True):
    ctx = gate_ctx(owner)
    ctx.runner.check_spawn_allowed = MagicMock(return_value=draining)
    ctx.store.get_autopilot_chain = MagicMock(return_value=["verify"] if chain else None)
    ctx.messenger.is_conversation_closed = AsyncMock(return_value=closed)
    seen = {"prompts": [], "repairs": []}

    async def fake_run(c, name, path, prompt):
        seen["prompts"].append(prompt)
        r = instance(id=f"j-{len(seen['prompts'])}")
        r._text = verdict
        return r

    async def start_repair(prompt):
        seen["repairs"].append(prompt)
        # What must already be true while the whole repair turn is running.
        seen["said_first"] = ctx.messenger.send_text.await_count
        seen["rows_first"] = judge._load_records("99")
        if isinstance(repair_ok, Exception):
            raise repair_ok
        return repair_ok

    with patch.object(config, "DATA_DIR", Path(td)), \
            patch.object(judge, "find_session_file", lambda s: sess), \
            patch.object(judge, "_run_idle_judge", fake_run), \
            patch.object(cmd_state, "check_budget", lambda c: budget), \
            patch.object(Instance, "read_result_text", lambda self: getattr(self, "_text", "")):
        out = asyncio.run(judge.judge_idle_thread(
            ctx, session_id="s", repo_name="aiagent", repo_path=str(repo),
            still_idle=lambda: idle, start_repair=start_repair,
        ))
    return out, ctx, seen


with tempfile.TemporaryDirectory() as td:
    repo, short, full = idle_repo(td)
    sess = session(td, ('git commit -qm "AUTHOR-SUBJECT-MARKER two"', ""), ts=NOW_TS)
    out, ctx, seen = run_idle(td, repo, sess)
    check("a quiet commit is found by its subject", len(seen["prompts"]) == 1)
    check("idle pass", out.kind == judge.PASS and len(seen["prompts"]) == 1)
    check("idle pass says nothing", ctx.messenger.send_text.await_count == 0)
    check("idle input has the change", "x = 1" in seen["prompts"][0])
    check("idle input has no commit subject", "AUTHOR-SUBJECT-MARKER" not in seen["prompts"][0])
    out, ctx, seen = run_idle(td, repo, sess)
    check("judged once", out is None and seen["prompts"] == [])

with tempfile.TemporaryDirectory() as td:
    repo, short, full = idle_repo(td)
    sess = session(td, ('git commit -m "two"', f"[master {short}] two"), ts=NOW_TS)
    out, _, seen = run_idle(td, repo, sess, chain=True)
    check("a live chain owns its thread", out is None and seen["prompts"] == [])
    out, _, seen = run_idle(td, repo, sess, closed=True)
    check("a closed thread is left alone", out is None and seen["prompts"] == [])
    (Path(td) / "judge.jsonl").write_text(json.dumps({"thread": "99", "commits": [full]}) + "\n")
    out, _, seen = run_idle(td, repo, sess)
    check("what the chain judged is not judged again", out is None and seen["prompts"] == [])

with tempfile.TemporaryDirectory() as td:
    repo, short, full = idle_repo(td)
    sess = session(td)
    out, _, seen = run_idle(td, repo, sess)
    check("no commits, nothing happens", out is None and seen["prompts"] == [])

with tempfile.TemporaryDirectory() as td:
    repo, short, full = idle_repo(td)
    sess = session(td, ('git commit -qm "no such subject"', ""), ts=NOW_TS)
    out, ctx, seen = run_idle(td, repo, sess)
    check("unattributed reports could_not_check and does not guess", out.kind == judge.COULD_NOT_CHECK and seen["prompts"] == [])
    check("and says so", ctx.messenger.send_text.await_count == 1)
    out, ctx, seen = run_idle(td, repo, sess)
    check("and only once", out is None and ctx.messenger.send_text.await_count == 0)

with tempfile.TemporaryDirectory() as td:
    repo, short, full = idle_repo(td)
    sess = session(td, ('git commit -m "x"', "[master 0badc0d] gone"), ts=NOW_TS)
    out, _, seen = run_idle(td, repo, sess)
    check("a hash that resolves to nothing is could_not_check", out.kind == judge.COULD_NOT_CHECK and seen["prompts"] == [])

with tempfile.TemporaryDirectory() as td:
    repo, short, full = idle_repo(td)
    sess = session(td, ('git commit -m "two"', f"[master {short}] two"), ts=NOW_TS)
    out, ctx, seen = run_idle(td, repo, sess, owner=())
    check("no owner words, no run", out.kind == judge.COULD_NOT_CHECK and seen["prompts"] == [])

with tempfile.TemporaryDirectory() as td:
    repo, short, full = idle_repo(td)
    sess = session(td, ('git commit -m "two"', f"[master {short}] two"), ts=NOW_TS)
    out, ctx, seen = run_idle(td, repo, sess, verdict=FIX_B)
    check("idle fix starts one repair", len(seen["repairs"]) == 1 and "a.py: wrong" in seen["repairs"][0])
    check("idle fix is surfaced", "a.py: wrong" in ctx.messenger.send_text.await_args.args[1])
    (repo / "b.py").write_text("y = 2\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fix"], cwd=repo, check=True)
    short2 = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    sess = session(td, ('git commit -m "two"', f"[master {short}] two"), ('git commit -m "fix"', f"[master {short2}] fix"), ts=NOW_TS)
    out, ctx, seen = run_idle(td, repo, sess, verdict=FIX_B)
    check("the repair's commits close the round", out is None and seen["prompts"] == [] and seen["repairs"] == [])


def commit_more(repo, name):
    (repo / name).write_text("z = 3\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", name], cwd=repo, check=True)
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()


with tempfile.TemporaryDirectory() as td:
    repo, short, full = idle_repo(td)
    first = ('git commit -m "two"', f"[master {short}] two")
    sess = session(td, first, ts=NOW_TS)
    out, ctx, seen = run_idle(td, repo, sess, verdict=FIX_B)
    check("the problems are said before the repair runs", seen["said_first"] == 1, seen)
    check("said while it is starting, not after", "repair turn is starting" in ctx.messenger.send_text.await_args_list[0].args[1])
    check("recorded before the repair runs", seen["rows_first"][-1].get("repair_started") is True, seen["rows_first"])
    # The repair turn committed nothing: its idle closes the round anyway.
    out, ctx, seen = run_idle(td, repo, sess, verdict=FIX_B)
    check("a repair with no commits still closes", out is None and seen["prompts"] == [] and ctx.messenger.send_text.await_count == 0)
    short2 = commit_more(repo, "later.py")
    sess = session(td, first, ('git commit -m "later.py"', f"[master {short2}] later.py"), ts=NOW_TS)
    out, ctx, seen = run_idle(td, repo, sess)
    check("a later turn's commits are not swallowed", out is not None and out.kind == judge.PASS and len(seen["prompts"]) == 1)
    check("and only those are judged", "z = 3" in seen["prompts"][0] and "x = 1" not in seen["prompts"][0])

for name, outcome in (("a refused start", False), ("a raising start", RuntimeError("boom"))):
    with tempfile.TemporaryDirectory() as td:
        repo, short, full = idle_repo(td)
        first = ('git commit -m "two"', f"[master {short}] two")
        sess = session(td, first, ts=NOW_TS)
        out, ctx, seen = run_idle(td, repo, sess, verdict=FIX_B, repair_ok=outcome)
        check(f"{name} returns the verdict", out is not None and out.kind == judge.FIX)
        check(f"{name} says so", "No repair turn could start" in ctx.messenger.send_text.await_args.args[1])
        with patch.object(config, "DATA_DIR", Path(td)):
            check(f"{name} closes the round", not judge._load_records("99")[-1].get("repair_started"))
        short2 = commit_more(repo, "later.py")
        sess = session(td, first, ('git commit -m "later.py"', f"[master {short2}] later.py"), ts=NOW_TS)
        out, _, seen = run_idle(td, repo, sess)
        check(f"after {name} the next turn is judged", len(seen["prompts"]) == 1 and out.kind == judge.PASS)

with tempfile.TemporaryDirectory() as td:
    repo, short, full = idle_repo(td)
    sess = session(td, ('git commit -m "two"', f"[master {short}] two"), ts=NOW_TS)
    for name, kw in (("draining", {"draining": "Reboot in progress"}), ("over budget", {"budget": False})):
        out, ctx, seen = run_idle(td, repo, sess, **kw)
        check(f"{name}: no run", out is None and seen["prompts"] == [])
        check(f"{name}: nothing recorded", not (Path(td) / "judge.jsonl").exists())
    out, _, seen = run_idle(td, repo, sess)
    check("a later idle tries again", len(seen["prompts"]) == 1)
    (Path(td) / "judge.jsonl").open("a").write("[1, 2]\nnot json\n")
    out, _, seen = run_idle(td, repo, sess)
    check("a bad line in the record costs itself", out is None and seen["prompts"] == [])

# A detached task: nothing may escape it, and the thread is free again afterwards.
ctx = gate_ctx()
ctx.store.get_autopilot_chain = MagicMock(side_effect=RuntimeError("boom"))
out = asyncio.run(judge.judge_idle_thread(
    ctx, session_id="s", repo_name="aiagent", repo_path="/nonexistent",
    still_idle=lambda: True, start_repair=AsyncMock(),
))
check("an internal error stays in the log", out is None and "99" not in judge._idle_running)
ctx = gate_ctx()
ctx.messenger.send_text = AsyncMock(side_effect=RuntimeError("down"))
asyncio.run(judge._report_idle(ctx, judge._could_not_check("x")))
check("a line that cannot be posted costs only the line", ctx.messenger.send_text.await_count == 1)

with tempfile.TemporaryDirectory() as td:
    repo, short, full = idle_repo(td)
    sess = session(td, ('git commit -m "two"', f"[master {short}] two"), ts=NOW_TS)
    out, ctx, seen = run_idle(td, repo, sess, verdict=FIX_B, idle=False)
    check("a busy thread gets no repair", seen["repairs"] == [] and out.kind == judge.FIX)
    check("but still hears about it", "a.py: wrong" in ctx.messenger.send_text.await_args.args[1])

with tempfile.TemporaryDirectory() as td:
    repo, short, full = idle_repo(td)
    sess = session(td, ('git commit -m "two"', f"[master {short}] two"), ts=NOW_TS)
    out, ctx, seen = run_idle(td, repo, sess, verdict=ONLY_B)
    check("idle only_you shows its line", "look at the ship" in ctx.messenger.send_text.await_args.args[1])

# --- goal ---
with tempfile.TemporaryDirectory() as td:
    r = Path(td)
    check("no goal", judge.repo_goal(td) == "")
    (r / ".claude").mkdir()
    (r / ".claude" / "CLAUDE.md").write_text("dot goal")
    check("dot claude goal", judge.repo_goal(td) == "dot goal")
    (r / "CLAUDE.md").write_text("root goal " + "z" * 9000)
    (r / "study").mkdir()
    (r / "study" / "STATE.md").write_text("the state")
    g = judge.repo_goal(td)
    check("root goal first, capped", g.startswith("root goal") and len(g) < 4500)
    check("study state added", "the state" in g)

# --- the run stays out of the thread ---
life = ast.parse((ROOT / "bot/engine/judge.py").read_text(encoding="utf-8"))
names = {n.attr for n in ast.walk(life) if isinstance(n, ast.Attribute)}
check("judge never binds a session", "backfill_thread_session" not in names and "bind_session" not in names)
check("no directive parsing", "_execute_bot_commands" not in names)
ctx = gate_ctx()
ctx.store.create_instance = MagicMock(return_value=instance(id="f-1"))
floor = judge.readonly_instance(ctx, InstanceOrigin.JUDGE, "p", "aiagent", "/r")
check("floor: explore", ctx.store.create_instance.call_args.kwargs["mode"] == "explore")
check("floor: bash closed", floor.bash_policy == "none" and floor.bash_policy_baseline == "none")
check("floor: fresh session", floor.session_id is None)


def judge_run_instance(origin):
    """The real run_instance, up to what it does with a finished run."""
    ctx = gate_ctx()
    ctx.channel_id = "99"
    result = MagicMock(killed_intentionally=False, kill_owns_card=False, is_error=False, needs_input=False, result_text="done")
    ctx.runner.run = AsyncMock(return_value=result)
    ctx.runner.begin_task = MagicMock()
    inst = instance(id="j-9", origin=origin)
    with patch.object(lifecycle, "_try_apply_near_limit", AsyncMock()) as tag, \
            patch.object(lifecycle, "get_sibling_summary", lambda *a, **k: ""), \
            patch.object(lifecycle, "finalize_run", MagicMock()), \
            patch.object(lifecycle, "schedule_cooldown_retry", AsyncMock(return_value=True)) as retry, \
            patch.object(lifecycle, "send_result", AsyncMock()) as sent:
        async def go():
            await lifecycle.run_instance(ctx, inst, silent=True)
            await asyncio.sleep(0)
        asyncio.run(go())
    return ctx, tag, retry, sent


ctx, tag, retry, sent = judge_run_instance(InstanceOrigin.JUDGE)
check("judge run: no channel taken", ctx.runner.begin_task.call_args.kwargs["channel_id"] is None)
check("judge run: thread's tag left alone", tag.call_count == 0)
check("judge run: no timer retry", retry.await_count == 0)
check("judge run: no result card", sent.await_count == 0)
ctx, tag, retry, sent = judge_run_instance(InstanceOrigin.DIRECT)
check("any other run: takes its channel and clears the tag", ctx.runner.begin_task.call_args.kwargs["channel_id"] == "99" and tag.call_count == 1)
check("any other run: reaches the retry", retry.await_count == 1)

# --- the step-back ---
ev = SimpleNamespace(ts="2026-11-01T10:00", kind="too_narrow", head_sha="abc", note="one ship only")
text = stepback.build_input("goal", ["a1 fix ships", "a2 fix ships"], Counter({"bot": 2}), [ev])
for want in ("goal", "a1 fix ships", "bot", "too_narrow", "one ship only"):
    check(f"stepback input has {want}", want in text)
check("no events said", "(none marked)" in stepback.build_input("g", [], Counter(), []))
cut = stepback.build_input("g", ["s" * 100] * 1000, Counter(), [])
check("subject cut stated", "commit subjects are shown" in cut)
with tempfile.TemporaryDirectory() as td:
    repo, short, full = idle_repo(td)
    subjects, areas = asyncio.run(stepback._read_log(str(repo)))
    check("stepback reads the log", len(subjects) == 2 and areas["a.py"] == 1, (subjects, areas))
    ctx = gate_ctx()
    ctx.store.list_repos = MagicMock(return_value={"r": str(repo)})
    made = instance(id="s-1")
    ctx.store.create_instance = MagicMock(return_value=made)
    ctx.messenger.send_thinking = AsyncMock(return_value={})
    ctx.messenger.escape = lambda t: t
    with patch.object(stepback.lifecycle, "run_instance", AsyncMock()) as ran, \
            patch.object(stepback.lifecycle, "backfill_thread_session", AsyncMock()), \
            patch.object(cmd_state, "check_budget", lambda c: True), \
            patch.object(stepback.missed, "load_events", lambda **k: []):
        asyncio.run(stepback.on_stepback_command(ctx, "r"))
    check("stepback runs read-only", ctx.store.create_instance.call_args.kwargs["mode"] == "explore")
    check("stepback closes bash", made.bash_policy == "none" and made.origin is InstanceOrigin.STEPBACK)
    check("stepback ran once", ran.await_count == 1)
    ctx = gate_ctx()
    ctx.store.list_repos = MagicMock(return_value={})
    asyncio.run(stepback.on_stepback_command(ctx, "nope"))
    check("unknown repo refused", "No repo" in ctx.messenger.send_text.await_args.args[1])

    def refused(setup, budget=True, log=(["a1 x"], Counter()), events=()):
        ctx = gate_ctx()
        ctx.store.list_repos = MagicMock(return_value={"r": str(repo)})
        setup(ctx)
        with patch.object(stepback.lifecycle, "run_instance", AsyncMock()) as ran, \
                patch.object(cmd_state, "check_budget", lambda c: budget), \
                patch.object(stepback, "_read_log", AsyncMock(return_value=log)), \
                patch.object(stepback.missed, "load_events", lambda **k: list(events)):
            asyncio.run(stepback.on_stepback_command(ctx, "r"))
        return ran.await_count == 0 and ctx.store.create_instance.call_count == 0, ctx.messenger.send_text.await_args.args[1]

    def draining(c):
        c.runner.check_spawn_allowed = MagicMock(return_value="Reboot in progress")

    def busy(c):
        c.runner.active_instance_for_channel = MagicMock(return_value="t-9")

    ok, said = refused(draining)
    check("stepback refused while draining", ok and "Reboot" in said, said)
    ok, said = refused(busy)
    check("stepback refused on a busy thread", ok and "already running" in said, said)
    ok, said = refused(lambda c: None, budget=False)
    check("stepback refused over budget", ok and "budget" in said, said)
    ok, said = refused(lambda c: None, log=None)
    check("stepback says when it cannot read", ok and "Could not read" in said, said)
    ok, said = refused(lambda c: None, log=([], Counter()))
    check("nothing to look at costs no run", ok and "Nothing to report" in said, said)

help_src = (ROOT / "bot/engine/admin_cmds.py").read_text(encoding="utf-8")
for cmd in ("/missed", "/stepback"):
    check(f"{cmd} is in /help", f"`{cmd}`" in help_src)
    check(f"{cmd} is in the bot context", f"- {cmd}" in src)

check("no em dashes", all(
    chr(0x2014) not in (ROOT / p).read_text(encoding="utf-8")
    for p in ("bot/engine/judge.py", "bot/engine/stepback.py", "scripts/test_judge.py")
))

print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
