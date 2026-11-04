"""The Missed marker: one tap or /missed writes a line, and the kind amends it.

Design note: docs/design/judge.md. Run: python scripts/test_missed.py
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType  # noqa: E402
from bot.engine import commands, missed  # noqa: E402
from bot.platform import formatting  # noqa: E402

failures: list[str] = []
passed = 0


def check(label: str, ok: bool, detail: object = "") -> None:
    global passed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failures.append(label)
        print(f"  FAIL  {label} {detail}")


class FakeMessenger:
    def __init__(self):
        self.sent: list[tuple[str, object]] = []
        self.edits: list[tuple[str, object]] = []

    async def send_text(self, channel_id, text, buttons=None, silent=False):
        self.sent.append((text, buttons))
        return "m-9"

    async def edit_text(self, channel_id, msg_id, text, buttons=None):
        self.edits.append((text, buttons))


class FakeStore:
    def __init__(self, inst=None):
        self.inst = inst

    def get_instance(self, iid):
        return self.inst if self.inst and self.inst.id == iid else None

    def latest_instance_for_session(self, session_id):
        return self.inst

    def list_repos(self):
        return {"bot": "/nonexistent"}


def _inst(**kw) -> Instance:
    base = dict(
        id="t-1", name="t-1", instance_type=InstanceType.TASK, prompt="p",
        repo_name="bot", repo_path="/nonexistent", status=InstanceStatus.COMPLETED,
        session_id="s-1", origin=InstanceOrigin.DIRECT,
    )
    base.update(kw)
    return Instance(**base)


def _ctx(store, *, is_owner=True, repo="bot", session="s-1"):
    return SimpleNamespace(
        messenger=FakeMessenger(), channel_id="c-1", store=store, is_owner=is_owner,
        repo_name=repo, session_id=session,
    )


def lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


def specs(inst, **kw) -> list[str]:
    return [b.callback_data for row in formatting.action_button_specs(inst, **kw) for b in row]


with tempfile.TemporaryDirectory() as tmp:
    config.DATA_DIR = Path(tmp)
    file = Path(tmp) / "missed.jsonl"

    print("the record")
    event = missed.record("bot", "c-1", "t-1", "abc", note="token sk-ant-api03-" + "x" * 40)
    row = lines(file)[0]
    check("has every field the baseline reads",
          {"ts", "repo", "thread", "instance_id", "head_sha", "kind", "note"} <= set(row))
    check("kind defaults to unspecified", row["kind"] == "unspecified")
    check("a secret in a note is not written", "sk-ant-api03" not in file.read_text())
    check("an unknown kind is kept as unspecified",
          missed.record("bot", "c", "t-2", "", kind="nonsense").kind == "unspecified")

    print("amending is append-only")
    before = file.read_text()
    check("amending works", missed.amend_kind(event.id, "too_narrow"))
    check("earlier lines are untouched", file.read_text().startswith(before))
    check("the fold applies the kind",
          next(e for e in missed.load_events() if e.id == event.id).kind == "too_narrow")
    check("an unknown event is refused", not missed.amend_kind("nope", "bug"))
    check("an unknown kind is refused", not missed.amend_kind(event.id, "nope"))
    with file.open("a") as f:
        f.write("not json\n[1]\n")
    check("bad lines cost themselves", len(missed.load_events()) == 2)
    check("repo filter", missed.load_events(repo="other") == [])
    with file.open("ab") as f:
        f.write(b"\xff\xfe not text\n")
    check("a bad byte costs its own line", len(missed.load_events()) == 2)

    print("the button")
    file.unlink()
    inst = _inst()
    ctx = _ctx(FakeStore(inst))
    asyncio.run(commands.handle_callback(ctx, "missed", "t-1", "m-1"))
    rows = lines(file)
    check("a tap records at once", len(rows) == 1 and rows[0]["instance_id"] == "t-1", rows)
    check("the record names repo and thread", rows and rows[0]["repo"] == "bot" and rows[0]["thread"] == "c-1")
    text, buttons = ctx.messenger.sent[-1]
    check("a follow-up offers the kinds",
          buttons and [b.label for b in buttons[0]] == ["Not what I meant", "Too narrow", "Bug"], buttons)
    check("kind ids fit Discord's 100 characters",
          all(len(b.callback_data) <= 100 for b in buttons[0]))

    asyncio.run(commands.handle_callback(ctx, "missed", "t-1", "m-1"))
    check("a second tap does not count twice", len(lines(file)) == 1)

    data = buttons[0][1].callback_data
    action, arg = data.split(":", 1)
    asyncio.run(commands.handle_callback(ctx, action, arg, "m-9"))
    check("picking a kind sets it", missed.load_events()[0].kind == "too_narrow")
    check("picking a kind settles the note", ctx.messenger.edits[-1] == ("Recorded as missed: Too narrow.", None))

    guest = _ctx(FakeStore(_inst(id="t-2")), is_owner=False)
    before_lines = len(lines(file))
    asyncio.run(commands.handle_callback(guest, "missed", "t-2", "m-1"))
    check("a guest's tap is not counted", len(lines(file)) == before_lines)
    asyncio.run(commands.handle_callback(guest, action, arg, "m-9"))
    check("a guest's kind changes nothing", missed.load_events()[0].kind == "too_narrow")
    check("and the guest is told", guest.messenger.sent[-1][0] == missed._OWNER_ONLY)

    print("/missed")
    ctx = _ctx(FakeStore(inst))
    asyncio.run(commands.on_missed_command(ctx, "the settings page still 500s"))
    check("records with the note", missed.load_events()[-1].note == "the settings page still 500s")
    ctx = _ctx(FakeStore(None), repo=None, session=None)
    before_lines = len(lines(file))
    asyncio.run(commands.on_missed_command(ctx, "x"))
    check("outside a repo it asks instead of guessing", len(lines(file)) == before_lines and "repo" in ctx.messenger.sent[-1][0])

print("where the button shows")
check("on a finished chat card", "missed:t-1" in specs(_inst()))
check("on a finished build card", "missed:t-1" in specs(_inst(branch="b", origin=InstanceOrigin.BUILD)))
check("on a Done card", "missed:t-1" in specs(_inst(origin=InstanceOrigin.DONE)))
check("on a card with no session", "missed:t-1" in specs(_inst(session_id=None)))
check("not while running", "missed:t-1" not in specs(_inst(status=InstanceStatus.RUNNING)))
check("not on a failed card", "missed:t-1" not in specs(_inst(status=InstanceStatus.FAILED)))
check("not on a recap", "missed:t-1" not in specs(_inst(origin=InstanceOrigin.TLDR)))
check("not on a judge's card", "missed:t-1" not in specs(_inst(origin=InstanceOrigin.JUDGE)))
check("TL;DR stays last in its row",
      formatting.action_button_specs(_inst(), show_expand=True)[-1][-1].callback_data == "tldr:t-1")
for kw in ({}, {"show_expand": True}):
    for origin in InstanceOrigin:
        for branch in (None, "b"):
            for chain in (False, True):
                r = formatting.action_button_specs(
                    _inst(origin=origin, branch=branch), has_autopilot_chain=chain, **kw)
                if len(r) > 5 or any(len(row) > 5 for row in r):
                    check(f"limits: {origin.value} {branch} {chain} {kw}", False, [[b.label for b in x] for x in r])

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print(f"All {passed} checks passed.")
