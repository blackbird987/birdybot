#!/usr/bin/env python3
"""Regression test: corrections are proposed from the eval record, never applied.

Every session is scored, every recurring flag is attributed to the prompt block
that was supposed to prevent it, and until now nobody read them in aggregate.
The weekly prompt review closes that loop. It is only safe because of a handful
of properties that are easy to lose in a refactor:

  * a **retired** check's flags stop counting. `tool_hygiene` flagged the very
    Bash reads and greps that bypass-permissions mode instructs a session to
    do, so it fired ~73,000 times in 30 days and buried every real finding. The
    check is gone; its output is still on disk forever.
  * the reviewing agent runs behind the **read-only floor**, so it cannot edit
    the constraints it is reviewing. Asserted on the created Instance, not on
    the prose of a prompt.
  * only a `contradicted` or `obsolete` finding may become an edit. A rule that
    is merely **disobeyed** is reported and left alone -- deleting a rule
    because it is hard to follow is exactly backwards.
  * the weekly gate runs off a **persisted timestamp**, so a reboot neither
    re-fires it nor skips the week.
  * a **rejected** proposal is recognised on its (file, flag) targets, not on
    its diff text, so a reworded rerun of the same idea is still suppressed.
  * a run gets **all the way through**, from the real handle `send_thinking`
    returns to the report, the buttons and both clicks. All 7 scheduled runs
    after launch died on that handle, because nothing here ever touched it.

Run: python scripts/test_prompt_review.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import inspect
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.engine import eval as eval_mod  # noqa: E402
from bot.engine import prompt_review as pr  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" · {detail}" if detail else ""))
    if not ok:
        failures.append(label)


tmpdir = tempfile.TemporaryDirectory()
EVALS = Path(tmpdir.name) / "evals"
EVALS.mkdir(parents=True)
eval_mod.EVALS_DIR = EVALS


def write_eval(iid: str, flags: list[tuple[str, str]]) -> None:
    """One eval file with (category, message) flags."""
    (EVALS / f"{iid}.json").write_text(json.dumps({
        "instance_id": iid,
        "repo": "bot",
        "origin": "direct",
        "mode": "build",
        "flags": [
            {"category": c, "severity": "warning", "message": m, "evidence": ""}
            for c, m in flags
        ],
        "metrics": {"cache_hit_rate": 0.5, "resumed_session": True},
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")


# --------------------------------------------- 1. the retired check is retired

print()
print("a withdrawn check stops counting, but its files stay readable")

check("the tool-hygiene check itself is gone",
      not hasattr(eval_mod, "_check_tool_hygiene"))
check("its regexes are gone too",
      not hasattr(eval_mod, "_READ_CMD_RE") and not hasattr(eval_mod, "_SEARCH_CMD_RE"))
check("tool_hygiene is marked retired", eval_mod.is_retired_flag("tool_hygiene"))
check("a live category is not", not eval_mod.is_retired_flag("efficiency"))

# Five sessions, each carrying a mountain of retired flags and one real one.
for n in range(5):
    write_eval(f"q-{n:03d}", [("tool_hygiene", "Bash used for search")] * 40
               + [("constraint_violation", "Over-long response (5,000 chars)")])

digest = eval_mod.build_digest(days=7, min_count=3)
messages = [r.message for r in digest.rows]
check("the digest reads all five sessions", digest.sessions == 5, str(digest.sessions))
check("no retired flag reaches the digest",
      not any("Bash used" in m for m in messages), str(messages))
check("the real finding survives",
      any("Over-long response" in m for m in messages), str(messages))
check("attribution no longer claims an owner for the retired category",
      eval_mod.attribute_flag("tool_hygiene", "Bash used for search") == "unattributed")

# The files themselves must still be readable as recorded -- a per-instance view
# rewriting history is how an old session becomes unexplainable.
raw = eval_mod.load_evals(since_hours=24)
check("load_evals still returns the flags as they were recorded",
      any(f.category == "tool_hygiene" for e in raw for f in e.flags))


# ------------------------------------------------- 2. the aggregation is bounded

print()
print("the agent sees a bounded table, never the eval directory")

for n in range(40):
    write_eval(f"z-{n:03d}", [("efficiency", f"Query took {n} turns (expected <20)"),
                              ("claim_grounding", "Response contains 3 URL(s) but WebFetch/WebSearch not used")])

text = pr.build_review_input(days=7)
check("the rendered input respects its char budget",
      len(text) <= pr._MAX_INPUT_CHARS + 200, f"{len(text)} chars")
check("rows are capped", text.count("\n- [") <= pr._MAX_ROWS, str(text.count("\n- [")))
check("owner attribution is carried through to the agent", "owner=" in text)
check("session counts are carried through", "sessions=" in text)
check("no retired flag leaks into the review input", "Bash used" not in text)

prompt = pr.build_review_prompt(text, rejected=["bot/config.py :: some old flag"])
for word in (pr.CLASS_CONTRADICTED, pr.CLASS_DISOBEYED, pr.CLASS_OBSOLETE):
    check(f"the brief defines '{word}'", word in prompt)
check("the brief forbids editing a merely-disobeyed rule",
      "REPORT ONLY" in prompt)
check("the brief biases toward deletion", "Prefer deletion" in prompt)
check("the brief states the proposal cap",
      str(config.PROMPT_REVIEW_MAX_PROPOSALS) in prompt)
check("a previous rejection is named in the brief",
      "some old flag" in prompt)
check("a no-change answer is an explicit option", pr.NO_PROPOSALS in prompt)


# ------------------------------------------ 3. only editable classes get through

print()
print("a rule that is merely disobeyed is reported, never proposed")

report = pr.parse_review(f"{pr.NO_PROPOSALS}")
check("the no-change sentinel parses as empty", report.empty)

sample = """Summary of the week.

NET LINES: +2 -19

### PROPOSAL 1
FILE: bot/config.py
FLAG: Bash used for search
CLASS: contradicted
WHY: the harness instructs this.
```diff
- old
```

### PROPOSAL 2
FILE: CLAUDE.md
FLAG: Over-long response
CLASS: disobeyed
WHY: should never become an edit.
```diff
- delete the mobile rule
```

### PROPOSAL 3
FILE: bot/config.py
CLASS: obsolete
WHY: no FLAG line, so it cannot be identified later.
"""
report = pr.parse_review(sample)
files = [p.file for p in report.proposals]
check("a contradicted finding becomes a proposal", "bot/config.py" in files)
check("a disobeyed finding does NOT", "CLAUDE.md" not in files, str(files))
check("a block missing FLAG is dropped, not guessed at", len(report.proposals) == 1,
      str(len(report.proposals)))
check("both drops are reported rather than silent", len(report.ignored) == 2,
      str(report.ignored))
check("the net line delta is carried", report.net_lines == "+2 -19",
      str(report.net_lines))

many = "\n".join(
    f"### PROPOSAL {i}\nFILE: f{i}.py\nFLAG: flag {i}\nCLASS: obsolete\nWHY: x\n"
    for i in range(config.PROMPT_REVIEW_MAX_PROPOSALS + 3)
)
capped = pr.parse_review(many)
check("the proposal cap is enforced on parse",
      len(capped.proposals) == config.PROMPT_REVIEW_MAX_PROPOSALS,
      str(len(capped.proposals)))


# ------------------------------------------------ 4. rejection survives rewording

print()
print("a rejected idea is recognised again even when reworded")

a = pr.parse_review(
    "### PROPOSAL 1\nFILE: bot/config.py\nFLAG: Over-long response\n"
    "CLASS: obsolete\nWHY: first wording\n```diff\n- a\n```\n")
b = pr.parse_review(
    "### PROPOSAL 1\nFILE: bot/config.py\nFLAG: Over-long response\n"
    "CLASS: contradicted\nWHY: completely different prose and a different diff\n"
    "```diff\n- b\n- c\n```\n")
c = pr.parse_review(
    "### PROPOSAL 1\nFILE: bot/config.py\nFLAG: a different flag entirely\n"
    "CLASS: obsolete\nWHY: x\n")
check("the same (file, flag) target hashes the same despite new prose",
      pr.fingerprint(a.proposals) == pr.fingerprint(b.proposals))
check("a different target hashes differently",
      pr.fingerprint(a.proposals) != pr.fingerprint(c.proposals))
check("the readable target names both halves",
      a.proposals[0].target() == "bot/config.py :: Over-long response")


# ------------------------------------------------- 5. the weekly gate is a clock

print()
print("the weekly gate runs off a timestamp, so a reboot cannot shift it")

now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
check("nothing is due before the interval elapses",
      not pr.should_run_now((now - timedelta(days=6)).isoformat(), now=now, interval_days=7))
check("it is due once the interval has elapsed",
      pr.should_run_now((now - timedelta(days=7, minutes=1)).isoformat(), now=now, interval_days=7))
check("a week is not skipped when the check happens late",
      pr.should_run_now((now - timedelta(days=30)).isoformat(), now=now, interval_days=7))
check("a naive timestamp is read as UTC rather than crashing",
      pr.should_run_now("2026-08-01T00:00:00", now=now, interval_days=7))
check("an unparseable timestamp is treated as due, not as forever-not-due",
      pr.should_run_now("not a date", now=now, interval_days=7))
check("no timestamp at all seeds instead of firing",
      not pr.should_run_now(None, now=now, interval_days=7))

# A reboot re-runs the same gate against the same stored value; it must give
# the same answer both times rather than depending on process uptime.
stamp = (now - timedelta(days=3)).isoformat()
check("the gate is a pure function of (stored stamp, now)",
      pr.should_run_now(stamp, now=now) == pr.should_run_now(stamp, now=now))

# Asserted on the signature and the arithmetic, not on the prose: the
# docstring says the word "tick" precisely to explain why there is no counter.
_gate_src = inspect.getsource(pr.should_run_now)
check("the gate takes the stored stamp as its input",
      "last_run_at: str | None" in _gate_src)
check("and decides by parsing it, not by counting invocations",
      "parse_iso_utc(last_run_at)" in _gate_src and "timedelta(days=" in _gate_src)


# ------------------------------- 6. the reviewer cannot edit what it is reviewing

print()
print("the reviewing agent runs behind the read-only floor")

from bot.discord import prompt_review as dpr  # noqa: E402
from bot.engine import lifecycle  # noqa: E402
from bot.platform import formatting  # noqa: E402
from bot.claude.types import (  # noqa: E402
    Instance, InstanceOrigin, InstanceStatus, InstanceType,
)

_run_src = inspect.getsource(dpr._run_review_locked)
check("the review instance is created in explore mode",
      'mode="explore"' in _run_src)
check("Bash is disabled on it, not just the edit tools",
      'bash_policy = "none"' in _run_src)
check("the baseline is clamped too, so nothing inherits a writable policy",
      'bash_policy_baseline = "none"' in _run_src)
check("the review instance never enters a worktree",
      "worktree" not in _run_src.lower())

_approve_src = inspect.getsource(dpr._open_build)
check("approve opens a build thread rather than writing files itself",
      "get_or_create_session_thread" in _approve_src
      and "git apply" not in _approve_src)
check("the approve brief tells the build not to weaken a disobeyed rule",
      "disobeyed" in dpr._APPROVE_BRIEF)

_post_src = inspect.getsource(dpr._post_outcome)
check("a re-proposed rejection is suppressed by the bot, not merely discouraged",
      "rejected_fingerprints" in _post_src)

_weekly_src = inspect.getsource(dpr.maybe_run_weekly)
check("the weekly run stamps the clock BEFORE running, never after",
      _weekly_src.rindex("_STATE_LAST_RUN") < _weekly_src.index("await run_review("))

_own_src = inspect.getsource(dpr.own_repo)
check("the target repo is resolved by path identity, never guessed",
      "list_repos()" in _own_src and "resolve()" in _own_src)


# --------------------------------- 7. the new run_instance caller does not bind

print()
print("the review's session belongs to the review, not to a thread")

_root = Path(__file__).resolve().parent.parent
_dpr_text = (_root / "bot" / "discord" / "prompt_review.py").read_text(encoding="utf-8")
check("the review runs through lifecycle.run_instance",
      "lifecycle.run_instance(" in _dpr_text)
check("and deliberately does NOT backfill a thread session",
      "backfill_thread_session" not in _dpr_text)
check("the reason is written down where the next reader will look",
      "A thread must always know its session" in _dpr_text)

_app_text = (_root / "bot" / "app.py").read_text(encoding="utf-8")
check("app.py still has exactly one direct run_instance caller",
      _app_text.count("lifecycle.run_instance(") == 1,
      str(_app_text.count("lifecycle.run_instance(")))

# ------------------------- 8. the report is inert: no directives, no repo buttons

print()
print("a report is not a turn: nothing it emits is dispatched")

_life_text = (_root / "bot" / "engine" / "lifecycle.py").read_text(encoding="utf-8")
check("lifecycle names the origins whose output is never dispatched",
      "_NO_DIRECTIVE_ORIGINS" in _life_text
      and "InstanceOrigin.PROMPT_REVIEW," in _life_text)

_run_src = inspect.getsource(lifecycle.run_instance)
check("the gate is computed once from the instance origin",
      "dispatch_directives = inst.origin not in _NO_DIRECTIVE_ORIGINS" in _run_src)
for _dispatcher in ("deliver_images", "_execute_bot_commands", "check_wake_request"):
    # Each dispatcher must sit under the gate. The proposals quote the literal
    # /watch, /spawn, /image and /repo examples out of the very files being
    # reviewed, so an ungated one arms them for real.
    _idx = _run_src.index(f"{_dispatcher}(ctx")
    _before = _run_src[:_idx]
    check(f"{_dispatcher} is gated on it",
          "dispatch_directives" in _before.rsplit("\n\n", 1)[-1])

_specs = formatting.action_button_specs
_review_inst = Instance(
    id="pr-1", name=None, instance_type=InstanceType.QUERY, prompt="x",
    repo_name="bot", repo_path="/tmp/bot",
    status=InstanceStatus.COMPLETED, origin=InstanceOrigin.PROMPT_REVIEW,
    session_id="sess-1",
)
_ids = [b.callback_data for row in _specs(_review_inst, show_expand=True) for b in row]
check("a finished review offers no repo-scoped buttons",
      not any(i.split(":")[0] in
              {"retry", "new", "plan", "build_and_ship", "done", "branch",
               "share", "merge", "discard", "commit", "review_code"}
              for i in _ids),
      str(_ids))
check("but a long report can still be expanded and its log read",
      any(i.startswith("expand:") for i in _ids)
      and any(i.startswith("log:") for i in _ids),
      str(_ids))

_review_inst.status = InstanceStatus.RUNNING
_running_ids = [b.callback_data for row in _specs(_review_inst) for b in row]
check("a running review can still be killed",
      _running_ids == ["kill:pr-1"], str(_running_ids))


# ------------------------------- 9. a pending proposal is recorded, then bounded

print()
print("the buttons exist before the state that validates them")

_post_full = inspect.getsource(dpr._post_outcome)
check("the pending entry is written only after the send succeeded",
      _post_full.index("await thread.send(embed=embed") < _post_full.index("_record_pending"))
check("a failed send records nothing",
      "log.exception(\"Prompt review: failed to post proposal" in _post_full
      and "return" in _post_full.split("failed to post proposal")[1][:120])

_pending: dict = {}
for _i in range(dpr._MAX_PENDING + 5):
    dpr._record_pending(_pending, f"i-{_i:03d}", f"fp{_i}", ["a :: b"])
    # Distinct timestamps: created_at has second resolution in the real path,
    # so the eviction order is asserted on values we control.
    _pending[dpr._STATE_PENDING][f"i-{_i:03d}"]["created_at"] = f"2026-01-01T00:00:{_i:02d}"
_keys = sorted(_pending[dpr._STATE_PENDING])
check("the pending map is capped", len(_keys) == dpr._MAX_PENDING, str(len(_keys)))
check("and it is the oldest that is evicted",
      _keys[0] == f"i-{5:03d}", _keys[0])

_body_src = inspect.getsource(dpr._embed_body)
check("the embed indexes the report instead of pasting it again",
      "report.raw" not in _body_src)


# ------------------------------------------------- 10. a real run, end to end

print()
print("a run reaches its outcome, and the buttons on it work")

# Everything above reads source or calls pure helpers, which is how the handle
# bug survived. This drives the real path instead: only the CLI run itself, the
# Discord objects and the build's thread creation and dispatch are fake.

from bot.claude.runner import ClaudeRunner  # noqa: E402
from bot.claude.runner.core import _save_result_file  # noqa: E402
from bot.claude.types import RunResult  # noqa: E402
from bot.discord.forums import ThreadInfo  # noqa: E402
from bot.discord.thread_ctx import ThreadContextMixin  # noqa: E402
from bot.engine import commands  # noqa: E402
from bot.engine.repo_cmds import _FAST_REPO_PATTERNS  # noqa: E402
from bot.platform.base import MessageHandle  # noqa: E402
from bot.store import history as history_mod  # noqa: E402
from bot.store.state import StateStore  # noqa: E402


class _Messenger:
    platform_name = "discord"

    def __init__(self) -> None:
        self.reports: list[str] = []

    def escape(self, text):
        return text

    def markdown_to_markup(self, md):
        return md

    def chunk_message(self, text):
        return [text]

    def format_mention(self, user_id):
        return None

    async def send_thinking(self, channel_id, text, buttons=None, **kw):
        # The real type, which is the whole point: a dict here would hide the bug.
        return MessageHandle(platform="discord", _data={"message_id": "123"})

    async def edit_thinking(self, *a, **kw):
        return True

    async def send_text(self, channel_id, text, *a, **kw):
        self.reports.append(text)
        return "msg-text"

    async def send_result(self, channel_id, markup, *a, **kw):
        self.reports.append(markup)
        return "msg-result"

    async def edit_text(self, *a, **kw):
        return True

    async def delete_message(self, *a, **kw):
        return None

    async def set_near_limit(self, *a, **kw):
        return None


class _Thread:
    id = 700

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, content=None, **kw):
        self.sent.append({"content": content, **kw})


class _Forums:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.info = ThreadInfo(thread_id="900")

    async def get_or_create_session_thread(self, repo_name, **kw):
        self.created.append({"repo": repo_name, **kw})
        return SimpleNamespace(id=900)

    def thread_to_project(self, channel_id):
        return (None, self.info)

    def attach_session_callbacks(self, ctx, info, channel_id):
        return None

    def persist_ctx_settings(self, ctx):
        return None


class _Bot(ThreadContextMixin):
    _discord_user_id = None

    def __init__(self, store, runner, messenger, forums) -> None:
        self._store = store
        self._runner = runner
        self.messenger = messenger
        self._forums = forums

    async def _offer_usage_limit_choice(self, *a, **kw):
        return False


class _Interaction:
    def __init__(self) -> None:
        self.followups: list[str] = []

        async def _defer(**kw):
            return None

        async def _followup(text, **kw):
            self.followups.append(text)

        self.response = SimpleNamespace(defer=_defer)
        self.followup = SimpleNamespace(send=_followup)


def _proposal(flag: str) -> str:
    """A report in the shape the review prompt asks for."""
    return (
        "NET LINES: +0 -1\n\n"
        f"### PROPOSAL 1\nFILE: bot/config.py\nFLAG: {flag}\nCLASS: obsolete\n"
        "WHY: the rule names a command that is gone.\n"
        "```diff\n- old rule\n```\n\n"
        "Reported, not proposed\n- Over-long response: disobeyed\n"
    )


async def _end_to_end() -> None:
    root = Path(tmpdir.name)
    store = StateStore(root / "state.json", config.RESULTS_DIR)
    store.add_repo("bot", str(config._PROJECT_ROOT))
    runner = ClaudeRunner(store)
    messenger = _Messenger()
    forums = _Forums()
    bot = _Bot(store, runner, messenger, forums)
    thread = _Thread()

    answer = {"text": ""}
    seen: list = []

    async def _fake_cli(inst, **kw):
        seen.append((inst.mode, inst.bash_policy, inst.origin))
        _save_result_file(inst, answer["text"])
        return RunResult(session_id="sess-review", result_text=answer["text"])

    runner.run = _fake_cli

    async def _review(text: str):
        answer["text"] = text
        thread.sent.clear()
        before = {i.id for i in store.list_instances()}
        outcome = await dpr._run_review_locked(
            bot, 7, "bot", str(config._PROJECT_ROOT), thread)
        inst = next(i for i in store.list_instances() if i.id not in before)
        return outcome, inst

    def _pending() -> dict:
        return dpr._state(bot).get(dpr._STATE_PENDING) or {}

    # --- nothing to propose
    outcome, inst = await _review(pr.NO_PROPOSALS)
    check("a run gets past the thinking card and completes",
          inst.status == InstanceStatus.COMPLETED, inst.status.value)
    check("the card's message id is recorded on the instance",
          inst.message_ids.get("discord", [None])[0] == "123", str(inst.message_ids))
    check("the CLI run really happened behind the read-only floor",
          seen == [("explore", "none", InstanceOrigin.PROMPT_REVIEW)], str(seen))
    check("the result file is in the temporary results dir",
          Path(inst.result_file).parent == config.RESULTS_DIR, str(inst.result_file))
    check("the review's report is posted into the thread",
          any(pr.NO_PROPOSALS in r for r in messenger.reports), str(messenger.reports))
    check("NO PROPOSALS posts the quiet no-change line, no buttons",
          len(thread.sent) == 1 and "no changes proposed" in thread.sent[0]["content"]
          and "view" not in thread.sent[0], str(thread.sent))
    check("and leaves nothing pending", not _pending(), str(_pending()))
    check("the caller is told zero proposals", "0 proposal(s)" in outcome, outcome)

    # The bot's end-of-turn marker has been seen on real results. It must not
    # turn a clean answer into a broken one.
    await _review(f"{pr.NO_PROPOSALS}\n\n{config.TURN_COMPLETE_SENTINEL}")
    check("a trailing turn marker still parses as no proposals",
          "no changes proposed" in (thread.sent[0]["content"] or ""), str(thread.sent))

    # --- a proposal, then Reject
    outcome, first = await _review(
        f"{_proposal('Over-long response')}\n{config.TURN_COMPLETE_SENTINEL}")
    check("a valid proposal is counted", "1 proposal(s)" in outcome, outcome)
    sent = thread.sent[0] if thread.sent else {}
    embed, view = sent.get("embed"), sent.get("view")
    check("a valid proposal posts the embed", embed is not None
          and "bot/config.py" in (embed.description or ""), str(sent))
    ids = [c.custom_id for c in view.children] if view else []
    check("with Approve and Reject buttons",
          ids == [f"ark:promptreview:approve:{first.id}",
                  f"ark:promptreview:reject:{first.id}"], str(ids))
    check("and the pending entry is recorded", first.id in _pending()
          and _pending()[first.id]["targets"] == ["bot/config.py :: Over-long response"],
          str(_pending()))

    tap = _Interaction()
    await dpr.handle_button(bot, tap, ids[1])
    check("Reject answers the owner", tap.followups[-1:] ==
          ["Rejected. It will not be proposed again."], str(tap.followups))
    check("Reject clears pending", first.id not in _pending(), str(_pending()))
    check("Reject records the fingerprint",
          pr.fingerprint(pr.parse_review(_proposal("Over-long response")).proposals)
          in dpr.rejected_fingerprints(bot))

    await _review(_proposal("Over-long response"))
    check("the same idea next week is suppressed, without buttons",
          len(thread.sent) == 1 and "suppressed" in thread.sent[0]["content"],
          str(thread.sent))

    # --- a proposal, then Approve
    report_text = _proposal("Stale command name")
    _, second = await _review(report_text)
    approve_id = thread.sent[0]["view"].children[0].custom_id
    dispatched: list = []

    async def _fake_on_text(ctx, prompt):
        dispatched.append((ctx, prompt))

    real_on_text = commands.on_text
    commands.on_text = _fake_on_text
    try:
        tap = _Interaction()
        await dpr.handle_button(bot, tap, approve_id)
        for _ in range(3):
            await asyncio.sleep(0)
    finally:
        commands.on_text = real_on_text

    check("Approve opens one build thread on the bot's own repo",
          [c["repo"] for c in forums.created] == ["bot"], str(forums.created))
    check("the build thread is a build", forums.info.mode == "build"
          and len(dispatched) == 1 and dispatched[0][0].mode == "build",
          str(forums.info.mode))
    prompt = dispatched[0][1] if dispatched else ""
    check("the brief carries the rules and the full report",
          prompt.startswith(dpr._APPROVE_BRIEF) and report_text in prompt)
    check("Approve clears pending", second.id not in _pending(), str(_pending()))
    check("Approve links the build", tap.followups[-1:] == ["Build opened: <#900>"],
          str(tap.followups))

    tap = _Interaction()
    await dpr.handle_button(bot, tap, approve_id)
    check("a later tap on a resolved proposal opens nothing",
          len(forums.created) == 1 and "no longer pending" in tap.followups[-1],
          str(tap.followups))

    check("no fast repo command can capture the approve brief",
          not any(p.search(dpr._APPROVE_BRIEF.strip()) for p in _FAST_REPO_PATTERNS))
    check("run history went to the temporary log",
          first.id in history_mod.HISTORY_FILE.read_text(encoding="utf-8"))


# Every other path a run writes to (the store is built on the temporary dir and
# the eval dir was redirected at the top), so this never reaches the real data.
_saved_paths = (
    config.RESULTS_DIR, config.REBOOT_REQUEST_FILE,
    config.REBOOT_REQUEST_DEFERRED_FILE, history_mod.HISTORY_FILE,
)
config.RESULTS_DIR = Path(tmpdir.name) / "results"
config.RESULTS_DIR.mkdir()
config.REBOOT_REQUEST_FILE = Path(tmpdir.name) / "reboot_request.json"
config.REBOOT_REQUEST_DEFERRED_FILE = Path(tmpdir.name) / "reboot_request.deferred.json"
history_mod.HISTORY_FILE = Path(tmpdir.name) / "history.jsonl"
try:
    asyncio.run(_end_to_end())
except Exception as exc:
    check("the end-to-end run finished", False, f"{type(exc).__name__}: {exc}")
finally:
    (
        config.RESULTS_DIR, config.REBOOT_REQUEST_FILE,
        config.REBOOT_REQUEST_DEFERRED_FILE, history_mod.HISTORY_FILE,
    ) = _saved_paths

tmpdir.cleanup()

print()
if failures:
    print("FAIL: prompt review")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print(f"PASS: corrections are proposed, never applied ({checks} checks).")
print("      A retired check's flags stop counting but stay readable, the")
print("      agent sees a bounded table behind a read-only floor, a rule that")
print("      is merely disobeyed is reported and left alone, the weekly gate")
print("      is a persisted clock, a rejected idea stays rejected even when")
print("      it comes back reworded, and a real run reaches its buttons.")
