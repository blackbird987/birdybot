#!/usr/bin/env python3
"""Harness for quiet hours and the bot's own merge decision.

Pinned against fakes:

  * the window: inside, outside, crossing midnight, "off", malformed;
  * presence: the owner seen 5 minutes ago stands quiet hours down, 30 minutes
    ago does not; a guest session never gets the block;
  * /chain arguments: preset and reason in either order, a quoted reason, a
    bare preset, a hold with no reason runs as ship, a confined hold stays;
  * the hold reason and the merge decision survive in the chain kwargs, and
    the old (label, silent_close) tuple keeps its shape;
  * the "merged on my own" line reaches a merge only when passed, and only the
    chain merge passes it;
  * the block lands in the turn prompt above the owner's words, and the system
    prompt bytes are identical with and without it;
  * the eval's unoffered-choice check stands down on a quiet turn;
  * Needs you shows "decided while you were away" until someone writes there;
  * /quiet shows, sets and turns off.

Run:  python scripts/test_quiet_hours.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("BOT_PATHS_DISABLED", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import config  # noqa: E402
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType  # noqa: E402
from bot.engine import quiet  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


TZ = quiet._owner_tz()


def local(h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, 20, h, m, tzinfo=TZ)


tmp = tempfile.TemporaryDirectory()
_n = [0]


def new_store() -> StateStore:
    _n[0] += 1
    d = Path(tmp.name) / f"s{_n[0]}"
    d.mkdir()
    return StateStore(d / "state.json", d / "results")


def inst(**kw) -> Instance:
    base = dict(id="q-1", name=None, instance_type=InstanceType.QUERY, prompt="do the thing",
                repo_name="", repo_path="", status=InstanceStatus.RUNNING, mode="explore")
    base.update(kw)
    return Instance(**base)


# --- Window ------------------------------------------------------------------
print("Window")
w = quiet.parse_window("03:00-09:00")
check(w == quiet.Window(180, 540), "03:00-09:00 parses")
check(quiet.in_window(local(4), w) and not quiet.in_window(local(14), w), "04:00 in, 14:00 out")
check(not quiet.in_window(local(9), w) and quiet.in_window(local(3), w), "start is inside, end is not")
night = quiet.parse_window("23:00-07:00")
check(quiet.in_window(local(23, 30), night) and quiet.in_window(local(2), night)
      and not quiet.in_window(local(12), night), "a window crossing midnight")
check(quiet.window_end(local(23, 30), night).day == 21, "its end is the next morning")
check(all(quiet.parse_window(x) is None for x in ("off", "", None, "25:00-09:00", "03:00-03:00", "3-9")),
      "off, empty, malformed and zero-length are no window")

# --- Presence ----------------------------------------------------------------
print("Presence")
t = local(4)
check(quiet.is_quiet(t, w, None), "never seen: quiet")
check(not quiet.is_quiet(t, w, t.timestamp() - 5 * 60), "seen 5 min ago: not quiet")
check(quiet.is_quiet(t, w, t.timestamp() - 30 * 60), "seen 30 min ago: quiet")
check(not quiet.is_quiet(local(14), w, None), "outside the window: never quiet")

s = new_store()
quiet.note_owner_seen(s, now=t.timestamp() - 2 * 3600)
owner = inst(origin=InstanceOrigin.DIRECT)
with mock.patch.object(quiet, "_now", return_value=t):
    block = quiet.quiet_block(s, owner, confined=False)
    check("asleep until 09:00" in block and "Decided while you were away:" in block,
          "an owner turn at 04:00, last seen 2h ago, gets the block")
    guest = inst(origin=InstanceOrigin.DIRECT, is_owner_session=False)
    check(quiet.quiet_block(s, guest, confined=False) == "", "a guest session never gets it")
    check(quiet.quiet_block(s, owner, confined=True) == "", "a confined run never gets it")
    for o in (InstanceOrigin.PEER_ANSWER, InstanceOrigin.JUDGE, InstanceOrigin.REVIEW_CODE,
              InstanceOrigin.PLAN_CHECK, InstanceOrigin.TLDR):
        check(quiet.quiet_block(s, inst(origin=o), confined=False) == "", f"{o.value} never gets it")
    quiet.note_owner_seen(s, now=t.timestamp() - 60)
    check(quiet.quiet_block(s, owner, confined=False) == "", "the owner typing a minute ago stands it down")
with mock.patch.object(quiet, "_now", return_value=local(14)):
    check(quiet.quiet_block(new_store(), owner, confined=False) == "", "14:00: no block")
check("\u2014" not in config.QUIET_HOURS_BLOCK, "the block has no em dash")

# --- /chain arguments ----------------------------------------------------------
print("/chain arguments")
from bot.engine.directives import chain as chain_mod  # noqa: E402

pa = chain_mod._parse_chain_args
check(pa('preset=hold reason="data migration on the live db"') == ("hold", "data migration on the live db"),
      "preset then quoted reason")
check(pa('reason="you asked to look first." preset=verify') == ("verify", "you asked to look first"),
      "reason first, trailing full stop dropped")
check(pa("hold") == ("hold", "") and pa("preset=ship") == ("ship", ""), "bare preset and preset=ship")
check(pa("") == (None, "") and pa("preset=bogus").preset is None, "nothing, and an unknown preset, is None")
check(chain_mod._resolve_hold("hold", "", confined=False) == ("ship", "", True),
      "a hold with no reason runs as ship")
check(chain_mod._resolve_hold("verify", "", confined=True) == ("verify", "", False),
      "a confined session keeps its hold")
check(chain_mod._resolve_hold("hold", "live db", confined=False) == ("hold", "live db", False),
      "a hold with a reason holds")
text = ('Kicking off.\n\n[BOT_CMD: /chain reason="migration" preset=hold]\n~~~plan\nDo it.\n~~~\n')
full = chain_mod._extract_chain_directive_full(text)
check(full is not None and full[0] == ("hold", "migration") and full[1].strip() == "Do it.",
      "the full extractor reads both args and the body")
check(chain_mod._extract_chain_directive(text) == ("hold", full[1]), "the old extractor keeps its shape")
up = chain_mod._QueuedChain("ship", "p", "q-1", "/r", hold_reason="", upgraded=True)
check("merge on its own" in chain_mod._start_notice(up), "the start notice says an upgrade merges")
held = chain_mod._QueuedChain("hold", "p", "q-1", "/r", hold_reason="migration")
check("Holding the merge for you: migration." in chain_mod._start_notice(held), "and names a hold's reason")

# --- Chain kwargs ------------------------------------------------------------------
print("Chain kwargs")
s = new_store()
s.set_chain_kwargs("sess", label="Chain (hold)", silent_close=False, hold_reason="migration")
check(s.get_chain_kwargs("sess") == ("Chain (hold)", False), "the tuple keeps its shape")
check(s.get_chain_hold_reason("sess") == "migration" and not s.get_chain_decided_merge("sess"),
      "the hold reason is kept")
s.set_chain_kwargs("s2", label="Chain (ship)", silent_close=True, decided_merge=True)
check(s.get_chain_decided_merge("s2") and s.get_chain_hold_reason("s2") == "", "the merge decision is kept")
s.save()
s2 = StateStore(s._file, s._results_dir)
check(s2.get_chain_hold_reason("sess") == "migration" and s2.get_chain_decided_merge("s2"),
      "both survive a restart")
check(s.get_chain_hold_reason(None) == "" and not s.get_chain_decided_merge("nope"), "unknown reads empty")

# --- Merge note ----------------------------------------------------------------------
print("Merge note")
from bot.engine import steps  # noqa: E402


class Msgr:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, _ch, text, **_kw):  # noqa: ANN001, ANN201
        self.sent.append(text)

    async def close_conversation(self, *_a, **_kw):  # noqa: ANN201
        return None


def merge(note: str) -> list[str]:
    m = Msgr()
    ctx = SimpleNamespace(messenger=m, channel_id="c", on_merged=None)
    target = inst(branch="claude-bot/t-1", original_branch="master")

    async def fake_merge(_ctx, _t):  # noqa: ANN001, ANN202
        return "Merged `claude-bot/t-1` into `master`."
    with mock.patch.object(steps, "merge_and_record", fake_merge):
        asyncio.run(steps._finalize_merge(ctx, target, note=note))
    return m.sent


from bot.engine import chain_runner  # noqa: E402

sent = merge(chain_runner.DECIDED_MERGE_NOTE)
check(sent and sent[0].endswith("\nMerged on my own: review and verify passed."), "a chain merge says so")
check(merge("") == ["✅ Merged `claude-bot/t-1` into `master`."], "a merge with no note is unchanged")
src = (ROOT / "bot").rglob("*.py")
passers = [p.name for p in src if re.search(r"_finalize_merge\([^)]*note=", p.read_text(), re.S)]
check(passers == ["chain_runner.py"], f"only the chain merge passes a note (got {passers})")
cr = (ROOT / "bot/engine/chain_runner.py").read_text()
check("Held for you: {hold_reason}." in cr, "the hold park message names the reason")

# --- Prompt placement ------------------------------------------------------------------
print("Prompt placement")
from bot.claude.runner import ClaudeRunner  # noqa: E402

s = new_store()
quiet.note_owner_seen(s, now=local(4).timestamp() - 2 * 3600)
runner = ClaudeRunner()
runner._store = s
runner._build_location_block = lambda i: ""  # type: ignore[method-assign]
runner._build_master_context_block = lambda i: ""  # type: ignore[method-assign]
repo = Path(tmp.name) / "repo"
repo.mkdir()


def build(at: datetime, **kw) -> tuple[str, bytes, Instance]:
    i = inst(prompt="OWNER WORDS", repo_path=str(repo), repo_name="r", **kw)
    with mock.patch.object(quiet, "_now", return_value=at):
        out = runner._build_command(i)
    _cmd, prompt, sp_file, key_file, rules_file = out
    sp = Path(sp_file).read_bytes() if sp_file else b""
    for f in (sp_file, key_file, rules_file):
        if f:
            try:
                os.unlink(f)
            except OSError:
                pass
    return prompt, sp, i


p_quiet, sp_quiet, i_quiet = build(local(4), origin=InstanceOrigin.DIRECT)
p_day, sp_day, i_day = build(local(14), origin=InstanceOrigin.DIRECT)
check(p_quiet.startswith("[Quiet hours:") and p_quiet.index("[Quiet hours:") < p_quiet.index("OWNER WORDS"),
      "04:00: the block sits above the owner's words")
check("Quiet hours" not in p_day, "14:00: no block")
check(i_quiet.quiet_hours and not i_day.quiet_hours, "the run is stamped quiet or not")
check(sp_quiet == sp_day and b"asleep until" not in sp_quiet, "system prompt bytes identical, block not in it")
p_guest, _sp, _i = build(local(4), origin=InstanceOrigin.DIRECT, is_owner_session=False)
check("Quiet hours" not in p_guest, "a guest turn at 04:00: no block")

# --- Eval --------------------------------------------------------------------------------
print("Eval")
from bot.engine import eval as eval_mod  # noqa: E402

q = "I can do either.\n\nShould I merge it or keep it on the branch?"
plain = inst(origin=InstanceOrigin.DIRECT)
check([f.category for f in eval_mod._check_unoffered_choice(plain, q)] == ["unoffered_choice"],
      "a normal turn ending on either/or is flagged")
qi = inst(origin=InstanceOrigin.DIRECT, quiet_hours=True)
check([f.category for f in eval_mod._check_unoffered_choice(qi, q)] == ["quiet_question"],
      "a quiet turn is not flagged for buttons, only for asking")
check(eval_mod._check_unoffered_choice(qi, "Done.\n\nDecided while you were away:\n- kept the name") == [],
      "a quiet turn that decided is not flagged")
check(eval_mod.attribute_flag("quiet_question", "x") == "QUIET_HOURS_BLOCK", "the flag is owned by the block")
check(hasattr(eval_mod, "_check_critical_pass"), "the critical pass check is still there")

# --- Needs you ------------------------------------------------------------------------------
print("Needs you")
from bot.engine import waiting  # noqa: E402

NOW = 1_800_000_000.0


class WStore:
    def __init__(self, insts) -> None:  # noqa: ANN001
        self.insts = insts

    def list_instances(self, all_=False):  # noqa: ANN001, ANN201
        return self.insts

    def list_repos(self):  # noqa: ANN201
        return {}


def iso(e: float) -> str:
    return datetime.fromtimestamp(e, tz=timezone.utc).isoformat()


def threads(human_at):  # noqa: ANN001, ANN201
    return {"r": {"repo_name": "r", "threads": {"t1": {"session_id": "s1", "topic": "night work",
                                                        "last_turn_at": NOW - 600,
                                                        "last_human_at": human_at}}}}


done = inst(id="q-9", session_id="s1", status=InstanceStatus.COMPLETED, quiet_hours=True,
            decided_away=True, finished_at=iso(NOW - 600), repo_name="r")
items = waiting.collect(WStore([done]), threads(NOW - 7200), now=NOW)
check([i.kind for i in items] == ["decided_away"], f"shows above your turn (got {[i.kind for i in items]})")
items = waiting.collect(WStore([done]), threads(NOW - 60), now=NOW)
check(all(i.kind != "decided_away" for i in items), "closes once someone writes there")
old = inst(id="q-8", session_id="s1", status=InstanceStatus.COMPLETED, quiet_hours=True,
           decided_away=True, finished_at=iso(NOW - 3 * 86400), repo_name="r")
check(all(i.kind != "decided_away" for i in waiting.collect(WStore([old]), threads(None), now=NOW)),
      "older than 48h hides")
check(waiting.KINDS[-2].name == "decided_away" and waiting.KINDS[-1].name == "your_turn",
      "its slot is just above your turn")

# --- finalize stamp -----------------------------------------------------------------------------
print("Finalize")
from bot.claude.types import RunResult  # noqa: E402
from bot.engine import lifecycle  # noqa: E402


def finalize(text: str, quiet_on: bool) -> Instance:
    i = inst(quiet_hours=quiet_on)
    ctx = SimpleNamespace(store=SimpleNamespace(update_instance=lambda *a, **k: None,
                                                add_cost=lambda *a: None, list_instances=lambda: []))
    r = RunResult(is_error=False, session_id="s", result_text=text)
    with mock.patch.object(lifecycle, "_log_history", lambda *a: None), \
         mock.patch("bot.engine.eval.evaluate_instance", lambda *a: None):
        lifecycle.finalize_run(ctx, i, r)
    return i


check(finalize("Done.\n\nDecided while you were away:\n- x", True).decided_away, "a quiet turn that decided is stamped")
check(not finalize("Decided while you were away: x", False).decided_away, "a daytime turn never is")
check(not finalize("All done.", True).decided_away, "a quiet turn that decided nothing is not")
check(Instance.from_dict(finalize("Decided while you were away:", True).to_dict()).decided_away,
      "the stamp survives a round trip")

# --- /quiet ------------------------------------------------------------------------------------
print("/quiet")
from bot.engine import settings_cmds  # noqa: E402

s = new_store()
m = Msgr()
qctx = SimpleNamespace(store=s, messenger=m, channel_id="c")
with mock.patch.object(quiet, "_now", return_value=local(4)):
    asyncio.run(settings_cmds.on_quiet(qctx, ""))
    check("03:00-09:00" in m.sent[-1] and "Active now, until 09:00" in m.sent[-1], f"status: {m.sent[-1]!r}")
    asyncio.run(settings_cmds.on_quiet(qctx, "23:30-07:00"))
    check(quiet.window_text(s) == "23:30-07:00" and "23:30-07:00" in m.sent[-1], "sets a window")
    asyncio.run(settings_cmds.on_quiet(qctx, "off"))
    check(quiet.parse_window(quiet.window_text(s)) is None and "off" in m.sent[-1], "turns off")
    asyncio.run(settings_cmds.on_quiet(qctx, "banana"))
    check(m.sent[-1].startswith("Usage"), "rejects nonsense")
    asyncio.run(settings_cmds.on_quiet(qctx, "default"))
    check(quiet.window_text(s) == config.QUIET_HOURS, "default goes back to the configured window")
slash = (ROOT / "bot/discord/slash_commands.py").read_text()
check(re.search(r'name="quiet".*?owner_only=True', slash, re.S) is not None, "/quiet is owner only")

tmp.cleanup()
print()
if FAILS:
    print(f"{len(FAILS)} check(s) failed:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("All checks passed.")
