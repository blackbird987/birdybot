#!/usr/bin/env python3
"""The end card: one compact card where a chain or a Done used to end in noise.

Pins the invariants in docs/design/session-recap.md:

  * parsing: the last ```recap block on the masked text, a block inside
    another session's ~~~ body is never read, "none" is nothing, caps on
    bullets, Next items, labels and the headline, a NEXT line without a pipe;
  * the card for every ending: shipped, held (with its branch row and
    reason), the verify preset, no code at all, the fallback with no recap,
    a failed tag push under "For you", the bot's own manual-check and
    not-delivered lines, leftovers and the decided line as notes only;
  * the facts come from git: the whole branch (``target...branch``), not the
    last step, the version from the release commit's subject, a commit hash
    the session named only once git confirms it;
  * the gate: a card with anything open never closes its thread, Close is the
    override, and every bot close goes through ``recap.close_thread``;
  * the record survives a StateStore reload, and month-old ones are pruned;
  * Next buttons send a full instruction (``messages``) and "Do all" one
    numbered list; the open items show in Needs you until someone writes;
  * the recap turn is read-only, on the RECAP origin, deletes its own status
    line and is skipped during a reboot drain;
  * a Done's result card and a chain release's are left to the end card, and
    their status line names the version or commit instead;
  * everything stays inside Discord's limits (5 rows, 256-char title,
    1024-char fields, 80-char button labels).

Run: python scripts/test_session_recap.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import inspect
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config  # noqa: E402
from bot.claude.types import (  # noqa: E402
    Instance, InstanceOrigin, InstanceStatus, InstanceType,
)
from bot.engine import lifecycle, recap, waiting  # noqa: E402
from bot.engine.directives import choices  # noqa: E402
from bot.platform.base import CardSpec, RequestContext  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []
PASSES = 0


def _check(name: str, cond: bool, detail: object = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(f"{name}: {detail}" if detail != "" else name)
        print(f"FAIL {name} {detail}")


def _inst(iid: str = "t-1", **kw) -> Instance:
    base = dict(
        id=iid, name=None, instance_type=InstanceType.TASK, prompt="p",
        repo_name="bot", repo_path="/tmp", status=InstanceStatus.COMPLETED,
        session_id="sess-1",
    )
    base.update(kw)
    return Instance(**base)


class _Messenger:
    def __init__(self, cards: bool = True, owner: list[str] | None = None) -> None:
        self.sent: list[tuple[str, object, dict]] = []
        self.cards: list[tuple[CardSpec, object, dict]] = []
        self.closed: list[tuple[str, bool]] = []
        self.stripped: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self._owner = owner
        self._next = 500
        if not cards:
            self.send_card = None  # type: ignore[assignment]

    def _id(self) -> str:
        self._next += 1
        return str(self._next)

    async def send_card(self, _cid, card, buttons=None, silent=False,  # noqa: ANN001
                        mention_user_id=None):
        self.cards.append((card, buttons, {"silent": silent, "mention": mention_user_id}))
        return self._id()

    async def send_text(self, _cid, text, buttons=None, silent=False, **_kw):  # noqa: ANN001
        self.sent.append((text, buttons, {"silent": silent}))
        return self._id()

    async def send_result(self, _cid, text, metadata=None, buttons=None,  # noqa: ANN001
                          silent=False, mention_user_id=None):
        self.sent.append((text, buttons, {"silent": silent, "mention": mention_user_id}))
        return self._id()

    async def close_conversation(self, cid, *, skip_mention=False):  # noqa: ANN001
        self.closed.append((cid, skip_mention))

    async def strip_button_rows(self, _cid, msg_id, prefix):  # noqa: ANN001
        self.stripped.append((str(msg_id), prefix))

    async def delete_message(self, _cid, msg_id):  # noqa: ANN001
        self.deleted.append(str(msg_id))

    async def owner_messages(self, _cid):  # noqa: ANN001
        return self._owner

    def shows_buttons(self, _cid):  # noqa: ANN001
        return True

    def markdown_to_markup(self, t):  # noqa: ANN001
        return t


class _Runner:
    def __init__(self, draining: bool = False) -> None:
        self.draining = draining

    def check_spawn_allowed(self):
        return "Reboot in progress" if self.draining else None


def _store(tmp: Path) -> StateStore:
    tmp.mkdir(parents=True, exist_ok=True)
    return StateStore(tmp / "state.json", tmp / "results")


def _ctx(store, messenger=None, runner=None, cid="chan-1") -> RequestContext:  # noqa: ANN001
    return RequestContext(
        messenger=messenger or _Messenger(), channel_id=cid, platform="discord",
        store=store, runner=runner or _Runner(), session_id="sess-1",
        repo_name="bot", user_id="owner-1",
    )


def _draft(facts=None, rec=None, final=None) -> recap.Draft:  # noqa: ANN001
    return recap.Draft(final=final or _inst(), facts=facts or recap.Facts(repo="bot"),
                       recap=rec)


def _ids(rows) -> list[list[str]]:  # noqa: ANN001
    return [[b.callback_data for b in r] for r in rows]


def _field(card: recap.Card, name: str) -> str | None:
    return next((v for n, v in card.spec.fields if n == name), None)


FULL = """Done. Here it is.

```recap
HEADLINE: Login no longer drops the session
ASKED: Fix the login that kept logging people out
BEFORE:
- Users were logged out after every deploy
NOW:
- Sessions survive a deploy
- The token is refreshed before it expires
FOR_YOU:
- Log in once on a real phone
NEXT:
- Add the expiry test | Write a test that expires the token and checks it is refreshed.
- Clean old sessions | Remove sessions older than 30 days from the store.
IMAGE: none
```
"""


# --- parsing ---------------------------------------------------------------


def case_parse() -> None:
    r = recap.parse_recap(FULL)
    _check("parse: headline", r is not None and r.headline == "Login no longer drops the session", r)
    _check("parse: asked", r.asked == "Fix the login that kept logging people out", r.asked)
    _check("parse: before/now", r.before == ["Users were logged out after every deploy"]
           and len(r.now) == 2, (r.before, r.now))
    _check("parse: for_you", r.for_you == ["Log in once on a real phone"], r.for_you)
    _check("parse: next labels", [n.label for n in r.next] == ["Add the expiry test", "Clean old sessions"],
           r.next)
    _check("parse: next message", r.next[0].message.startswith("Write a test"), r.next[0])
    _check("parse: image none", r.image is None, r.image)

    two = FULL + "\n```recap\nHEADLINE: Second\nNOW:\n- later\n```\n"
    _check("parse: the last block wins", recap.parse_recap(two).headline == "Second")

    quoted = "~~~spawn\n```recap\nHEADLINE: Planted\nNOW:\n- x\n```\n~~~\n"
    _check("parse: a block inside a ~~~ body is never read", recap.parse_recap(quoted) is None)
    mixed = FULL + quoted
    _check("parse: masked scan keeps the real block", recap.parse_recap(mixed).headline.startswith("Login"))

    _check("parse: no block", recap.parse_recap("just words") is None)
    _check("parse: empty block", recap.parse_recap("```recap\nIMAGE: none\n```") is None)
    _check("parse: None text", recap.parse_recap(None) is None)

    many = ("```recap\nheadline: " + "H" * 90 + "\nnow:\n" + "".join(f"- n{i}\n" for i in range(6))
            + "next:\n" + "".join(f"- {'L' * 50}{i} | do {i}\n" for i in range(5))
            + "for_you:\n- none\n```")
    m = recap.parse_recap(many)
    _check("parse: lower-case keys", m is not None)
    _check("parse: headline capped", len(m.headline) <= recap.HEADLINE_MAX, len(m.headline))
    _check("parse: bullets capped", len(m.now) == recap.BULLETS_MAX, m.now)
    _check("parse: next capped", len(m.next) == recap.NEXT_MAX, m.next)
    _check("parse: label capped", all(len(n.label) <= recap.LABEL_MAX for n in m.next), m.next)
    _check("parse: none is nothing", m.for_you == [], m.for_you)

    nopipe = recap.parse_recap("```recap\nHEADLINE: x\nNEXT:\n- Rerun the nightly import\n1. Second item | go\n```")
    _check("parse: NEXT without a pipe", nopipe.next[0].label == "Rerun the nightly import"
           and nopipe.next[0].message == "Rerun the nightly import", nopipe.next)
    _check("parse: numbered bullets", nopipe.next[1].label == "Second item", nopipe.next)

    img = recap.parse_recap("```recap\nHEADLINE: x\nIMAGE: `docs/shot.png`\n```")
    _check("parse: image path unquoted", img.image == "docs/shot.png", img.image)


# --- the card --------------------------------------------------------------


def case_cards() -> None:
    now = 1_000_000.0
    facts = recap.Facts(repo="bot", version="v1.2.3", commit="abc1234", subject="Login fix",
                        changelog=["Sessions survive deploys"], files=4, added=120, removed=8,
                        branch="claude-bot/t-1", target="master", model="claude-opus-5-5",
                        started_at=now - 3600)

    # Shipped, nothing open.
    clean = recap.Recap(headline="Login fixed", asked="Fix login", now=["Works"])
    c = recap.build_card(_draft(facts, clean), recap.EndState(kind="merged"), now=now)
    _check("shipped: status", c.spec.description.startswith("Shipped v1.2.3, merged into master"),
           c.spec.description)
    _check("shipped: size", "4 files, +120 -8" in c.spec.description, c.spec.description)
    _check("shipped: green", c.spec.color == "green", c.spec.color)
    _check("shipped: nothing open", c.open_items == [] and "open" not in c.spec.description)
    _check("shipped: footer", c.spec.footer.startswith("bot · 1h") and "abc1234" in c.spec.footer,
           c.spec.footer)
    rows = recap.card_buttons(c, "t-1", recap.EndState(kind="merged"))
    _check("shipped: only Missed", _ids(rows) == [["missed:t-1"]], _ids(rows))

    # Merged with open items: gold, counted, buttons.
    rec = recap.parse_recap(FULL)
    c = recap.build_card(_draft(facts, rec), recap.EndState(kind="merged"), now=now)
    _check("open: gold", c.spec.color == "gold")
    _check("open: count", "3 open" in c.spec.description, c.spec.description)
    _check("open: for you field", _field(c, "For you") == "• Log in once on a real phone",
           _field(c, "For you"))
    _check("open: next field numbered", (_field(c, "Next") or "").startswith("1. **Add the expiry test**"),
           _field(c, "Next"))
    rows = recap.card_buttons(c, "t-1", recap.EndState(kind="merged"))
    _check("open: rows", _ids(rows) == [["choice:t-1:0", "choice:t-1:1", "choice:t-1:2"],
                                       ["recap_close:t-1", "missed:t-1"]], _ids(rows))
    _check("open: Do all label", rows[0][-1].label == recap.DO_ALL_LABEL)
    _check("open: field order", [n for n, _ in c.spec.fields] ==
           ["You asked", "Before", "Now", "For you", "Next"], c.spec.fields)

    # Held: branch row on the branch instance, reason line, Close.
    hf = recap.Facts(repo="bot", version="v1.2.3", branch="b", target="master",
                     hold_reason="it touches the release lock")
    c = recap.build_card(_draft(hf, clean), recap.EndState(kind="held", branch_instance_id="t-9"))
    _check("held: status", c.spec.description.startswith("v1.2.3 built, waiting for your merge"),
           c.spec.description)
    _check("held: reason", "Held: it touches the release lock" in c.spec.description)
    _check("held: reason on its own line", "\nHeld: " in c.spec.description,
           c.spec.description)
    _check("held: gold", c.spec.color == "gold")
    rows = recap.card_buttons(c, "t-1", recap.EndState(kind="held", branch_instance_id="t-9"))
    _check("held: rows", _ids(rows) == [["merge:t-9", "discard:t-9", "diff:t-9"],
                                       ["recap_close:t-1", "missed:t-1"]], _ids(rows))

    # A merge that went wrong: gold, and the status says what happened.
    sf = recap.Facts(repo="bot", version="v1.2.3", branch="b", target="master")
    c = recap.build_card(_draft(sf, clean), recap.EndState(kind="merge_stopped"))
    _check("stopped: status", c.spec.description.startswith(
        "Merged into master, stopped before deploy"), c.spec.description)
    _check("stopped: gold", c.spec.color == "gold", c.spec.color)
    c = recap.build_card(_draft(sf, clean), recap.EndState(kind="merge_failed"))
    _check("failed: status", c.spec.description.startswith("The merge did not land"),
           c.spec.description)
    _check("failed: gold", c.spec.color == "gold", c.spec.color)

    # Verify preset: held, no release.
    vf = recap.Facts(repo="bot", branch="b", target="master")
    c = recap.build_card(_draft(vf, clean), recap.EndState(kind="held", branch_instance_id="t-9"))
    _check("verify preset: status", c.spec.description.startswith("Built on its branch, waiting for your merge"),
           c.spec.description)

    # No code at all: research or a decision.
    research = recap.Recap(headline="Why the cache misses", asked="Why is it slow?",
                           before=["Nobody knew"], now=["The key includes a timestamp"])
    c = recap.build_card(_draft(recap.Facts(repo="bot"), research), recap.EndState(kind="local"))
    _check("no code: status", c.spec.description == "Nothing committed", c.spec.description)
    _check("no code: blurple", c.spec.color == "blurple", c.spec.color)
    _check("no code: no Close", _ids(recap.card_buttons(c, "q-1", recap.EndState())) == [["missed:q-1"]])

    # Committed in place.
    c = recap.build_card(_draft(recap.Facts(repo="bot", commit="deadbee"), clean),
                         recap.EndState(kind="local"))
    _check("local commit: status", c.spec.description == "Committed `deadbee`", c.spec.description)

    # Fallback: no recap at all.
    ff = recap.Facts(repo="bot", subject="Retry the webhook on 502", asked="the webhook keeps failing",
                     changelog=["Webhook retries on 502"], commit="abc1234")
    c = recap.build_card(_draft(ff, None), recap.EndState(kind="merged"))
    _check("fallback: title from subject", c.spec.title == "Retry the webhook on 502", c.spec.title)
    _check("fallback: asked from the owner", _field(c, "You asked") == "the webhook keeps failing")
    _check("fallback: now from changelog", _field(c, "Now") == "• Webhook retries on 502")
    c = recap.build_card(_draft(recap.Facts(repo="bot"), None), recap.EndState(kind="local"))
    _check("fallback: bare title", c.spec.title == "Work finished", c.spec.title)

    # Merge notes: problems under For you, noise dropped, the rest as notes.
    notes = ["Merged into master (3 commits)\nTags pushed: v1.2.3\n"
             "Tags not pushed: v1.2.3\n`remote: permission denied`\n"
             "ℹ️ No remote configured\nDeploy queued for bot"]
    c = recap.build_card(_draft(facts, clean), recap.EndState(kind="merged", merge_notes=notes))
    fy = _field(c, "For you") or ""
    _check("tag push: problem shown", "Tags not pushed: v1.2.3 (remote: permission denied)" in fy, fy)
    _check("tag push: counts as open", "1 open" in c.spec.description and c.spec.color == "gold",
           c.spec.description)
    nf = _field(c, "Notes") or ""
    _check("tag push: noise dropped", "Tags pushed" not in nf and "No remote" not in nf
           and "Merged into" not in nf, nf)
    _check("tag push: other note kept", "Deploy queued for bot" in nf, nf)

    # The bot's own lines, leftovers and the decided line.
    bf = recap.Facts(repo="bot", commit="abc1234", manual_checks=["the phone layout"],
                     not_done=["the export button"], leftovers=2)
    c = recap.build_card(_draft(bf, clean), recap.EndState(kind="merged", decided_note="Merged on my own."))
    fy = _field(c, "For you") or ""
    _check("bot lines: manual check", "Check by hand: the phone layout" in fy, fy)
    _check("bot lines: not delivered", "Not delivered: the export button" in fy, fy)
    nf = _field(c, "Notes") or ""
    _check("notes: leftovers", "2 review leftovers filed in TODO.md" in nf, nf)
    _check("notes: decided", "Merged on my own." in nf, nf)
    _check("notes: not counted as open", "2 open" in c.spec.description, c.spec.description)

    # A merge that did not land.
    c = recap.build_card(_draft(facts, clean), recap.EndState(kind="merge_failed"))
    _check("merge failed: status", c.spec.description.startswith("The merge did not land"), c.spec.description)
    _check("merge failed: gold", c.spec.color == "gold")


def case_limits() -> None:
    long = "x" * 5000
    rec = recap.Recap(headline=long, asked=long, before=[long] * 3, now=[long] * 3,
                      for_you=[long] * 3,
                      next=[recap.NextItem("L" * 30, long) for _ in range(3)])
    facts = recap.Facts(repo="bot", manual_checks=[long] * 5, not_done=[long] * 5,
                        hold_reason=long, subject=long)
    c = recap.build_card(_draft(facts, rec), recap.EndState(
        kind="held", branch_instance_id="t-9", merge_notes=["Merged\n" + "\n".join([long] * 5)],
        decided_note=long))
    _check("limits: title", len(c.spec.title) <= 256, len(c.spec.title))
    _check("limits: fields", all(len(v) <= 1024 for _, v in c.spec.fields),
           [len(v) for _, v in c.spec.fields])
    _check("limits: field count", len(c.spec.fields) <= 25)
    _check("limits: description", len(c.spec.description) <= 4096, len(c.spec.description))
    total = len(c.spec.title) + len(c.spec.description) + len(c.spec.footer) + sum(
        len(n) + len(v) for n, v in c.spec.fields)
    _check("limits: whole embed under 6000", total <= 6000, total)
    rows = recap.card_buttons(c, "t-1", recap.EndState(kind="held", branch_instance_id="t-9"))
    _check("limits: rows", len(rows) <= 5, len(rows))
    _check("limits: labels", all(len(b.label) <= 80 for r in rows for b in r))
    _check("limits: row width", all(len(r) <= 5 for r in rows))


# --- facts from git --------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def case_git_facts(tmp: Path) -> None:
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "feature")
    for name in ("b.txt", "c.txt", "d.txt"):
        (repo / name).write_text("one\ntwo\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "Build the thing")
    (repo / "CHANGELOG.md").write_text("v1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "v1.4.0: The thing")
    _git(repo, "checkout", "-q", "master")
    # Master moves on: its own change must not count as the branch's.
    (repo / "e.txt").write_text("1\n2\n3\n4\n5\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "unrelated")

    g = recap.git_facts(str(repo), "feature", "master")
    _check("git: whole branch, three dots", g.get("files") == 4 and g.get("added") == 7, g)
    _check("git: version from the release subject", g.get("version") == "v1.4.0", g)
    _check("git: newest commit", g.get("subject") == "v1.4.0: The thing", g)
    newest = _git(repo, "rev-parse", "--short", "feature")
    _check("git: newest hash", g.get("commit") == newest, (g.get("commit"), newest))

    hint = _git(repo, "rev-parse", "master")
    g = recap.git_facts(str(repo), None, None, hint)
    _check("git: no branch uses the confirmed hint", g.get("subject") == "unrelated"
           and "files" not in g, g)
    g = recap.git_facts(str(repo), None, None, "0123456789abcdef")
    _check("git: unknown hint is nothing", g == {}, g)
    g = recap.git_facts(str(repo), None, None, "--all")
    _check("git: a hint that is not a hash is never passed", g == {}, g)
    _check("git: missing repo", recap.git_facts(str(tmp / "nope"), "a", "b") == {})

    # gather_facts reads the release step's own result, not just git.
    store = _store(tmp / "gf")
    build = _inst("t-b", origin=InstanceOrigin.BUILD, repo_path=str(repo), branch="feature",
                  original_branch="master", needs_manual_verification=True,
                  manual_verify_reason="the phone layout", created_at="2026-11-23T10:00:00+00:00",
                  model="opus")
    ver = _inst("t-v", origin=InstanceOrigin.VERIFY, parent_id="t-b", repo_path=str(repo),
                verify_not_done="the export button", created_at="2026-11-23T10:30:00+00:00")
    rel = _inst("t-r", origin=InstanceOrigin.RELEASE, parent_id="t-v", repo_path=str(repo),
                branch="feature", original_branch="master",
                created_at="2026-11-23T11:00:00+00:00")
    for i in (build, ver, rel):
        store.update_instance(i)
    rel.result_file = str(tmp / "rel.md")
    Path(rel.result_file).write_text(
        "```summary\nCOMMIT: abc1234 v1.4.0: The thing\nCHANGELOG:\n- Added the thing\nVERSION: v1.4.0\n```")
    store.update_instance(rel)
    msgs = ["the dashboard is missing the export, can you add it", "sgo"]
    ctx = _ctx(store, _Messenger(owner=msgs))
    f = asyncio.run(recap.gather_facts(ctx, rel))
    _check("facts: lineage manual check", f.manual_checks == ["the phone layout"], f.manual_checks)
    _check("facts: lineage not done", f.not_done == ["the export button"], f.not_done)
    _check("facts: changelog from the release", f.changelog == ["Added the thing"], f.changelog)
    _check("facts: version from git", f.version == "v1.4.0", f.version)
    _check("facts: subject without version", f.subject == "The thing", f.subject)
    _check("facts: asked skips the approval", f.asked.startswith("the dashboard"), f.asked)
    _check("facts: build model", f.model == "opus", f.model)
    _check("facts: started at the build", f.started_at is not None
           and abs(f.started_at - 1795428000.0) < 1, f.started_at)
    txt = recap.facts_text(f)
    _check("facts text: carries the owner's words", "the dashboard is missing" in txt, txt)
    _check("facts text: release", "Release: v1.4.0" in txt, txt)


# --- the recap turn and the image ------------------------------------------


def case_recap_turn(tmp: Path) -> None:
    from bot.engine import step_spawn
    store = _store(tmp / "rt")
    final = _inst("t-r", origin=InstanceOrigin.RELEASE)
    store.update_instance(final)
    seen: dict = {}

    async def fake_spawn(ctx, source_id, cfg, strip_source_buttons=True, source_msg_id=None):  # noqa: ANN001
        seen.update(source=source_id, cfg=cfg, strip=strip_source_buttons)
        inst = _inst("q-recap", origin=InstanceOrigin.RECAP, instance_type=InstanceType.QUERY)
        inst.message_ids = {"discord": ["777"]}
        inst.result_file = str(tmp / "recap.md")
        Path(inst.result_file).write_text(FULL)
        return inst

    orig = step_spawn.spawn_from
    step_spawn.spawn_from = fake_spawn
    try:
        m = _Messenger()
        rec = asyncio.run(recap.run_recap_turn(_ctx(store, m), final, recap.Facts(repo="bot")))
        cfg = seen.get("cfg")
        _check("turn: parsed", rec is not None and rec.headline.startswith("Login"), rec)
        _check("turn: RECAP origin", cfg.origin == InstanceOrigin.RECAP, cfg.origin)
        _check("turn: read-only", cfg.mode == "explore" and cfg.permission_mode == "explore", cfg)
        _check("turn: resumes, posts nothing", cfg.resume_session and cfg.silent, cfg)
        _check("turn: leaves the source card", seen.get("strip") is False)
        _check("turn: facts in the prompt", config.RECAP_SLOT not in cfg.prompt
               and "Repo: bot" in cfg.prompt)
        _check("turn: status line deleted", m.deleted == ["777"], m.deleted)

        seen.clear()
        rec = asyncio.run(recap.run_recap_turn(_ctx(store, runner=_Runner(draining=True)),
                                               final, recap.Facts()))
        _check("turn: skipped in a drain", rec is None and not seen, seen)
        rec = asyncio.run(recap.run_recap_turn(_ctx(store), _inst(session_id=None), recap.Facts()))
        _check("turn: no session, no turn", rec is None and not seen)
    finally:
        step_spawn.spawn_from = orig

    async def boom(*_a, **_kw):
        raise RuntimeError("spawn failed")
    step_spawn.spawn_from = boom
    try:
        d = asyncio.run(recap.prepare(_ctx(store, _Messenger()), final))
        _check("prepare: never raises", d.recap is None and d.facts.repo == "bot", d)
    finally:
        step_spawn.spawn_from = orig

    # Pinned by source: the floor and the origin sets.
    _check("origin: caller-read", InstanceOrigin.RECAP in lifecycle._CALLER_READS_ORIGINS)
    _check("origin: no directives", InstanceOrigin.RECAP in lifecycle._NO_DIRECTIVE_ORIGINS)
    _check("origin: keeps the offer", InstanceOrigin.RECAP in lifecycle._KEEPS_CHOICE_OFFER_ORIGINS)
    _check("origin: not a turn", InstanceOrigin.RECAP in waiting._NOT_A_TURN)


def case_image(tmp: Path) -> None:
    from bot.claude import sandbox
    repo = tmp / "imgrepo"
    repo.mkdir()
    (repo / "shot.png").write_bytes(b"\x89PNG fake")
    outside = tmp / "elsewhere"
    outside.mkdir()
    (outside / "x.png").write_bytes(b"\x89PNG fake")
    final = _inst("t-img", repo_path=str(repo))
    store = _store(tmp / "img")

    kept = recap.keep_image(store, final, "shot.png")
    _check("image: kept outside the worktree", kept is not None
           and Path(kept).parent == config.DATA_DIR / recap.IMAGE_DIR_NAME
           and Path(kept).read_bytes() == b"\x89PNG fake", kept)
    _check("image: outside the repo refused", recap.keep_image(store, final, str(outside / "x.png")) is None)
    _check("image: not an image refused", recap.keep_image(store, final, str(ROOT / "README.md")) is None)
    _check("image: none", recap.keep_image(store, final, None) is None)

    orig = sandbox.instance_confined
    sandbox.instance_confined = lambda *_a, **_kw: True
    try:
        data_png = config.DATA_DIR / "inside.png"
        data_png.write_bytes(b"\x89PNG fake")
        _check("image: a confined session cannot reach the data dir",
               recap.keep_image(store, final, str(data_png)) is None)
    finally:
        sandbox.instance_confined = orig
    _check("image: an unconfined one can", recap.keep_image(store, final, str(config.DATA_DIR / "inside.png"))
           is not None)


# --- posting, the gate, the record -----------------------------------------


def case_post_and_gate(tmp: Path) -> None:
    store = _store(tmp / "pg")
    final = _inst("t-1")
    store.update_instance(final)

    # Nothing open: posted, recorded, closed quietly.
    m = _Messenger()
    ctx = _ctx(store, m)
    clean = recap.Recap(headline="Done", asked="x", now=["y"])
    mid = asyncio.run(recap.post(ctx, _draft(final=final, rec=clean),
                                 recap.EndState(kind="merged"), close=True))
    _check("post: card sent", len(m.cards) == 1 and mid is not None, m.cards)
    _check("post: quiet when clean", m.cards[0][2] == {"silent": True, "mention": None}, m.cards[0][2])
    _check("post: closed", m.closed == [("chan-1", True)], m.closed)
    rec = store.get_recap("chan-1")
    _check("post: recorded", rec and rec["msg_id"] == mid and rec["open_items"] == [], rec)
    _check("post: card id not on the instance", mid not in (final.message_ids or {}).get("discord", []))

    # Loud (Autopilot) pings even when clean.
    m = _Messenger()
    asyncio.run(recap.post(_ctx(store, m), _draft(final=final, rec=clean),
                           recap.EndState(kind="merged"), close=True, loud=True))
    _check("post: loud pings", m.cards[0][2]["mention"] == "owner-1", m.cards[0][2])

    # Open items: pings, offer recorded with messages, never closes.
    m = _Messenger()
    ctx = _ctx(store, m)
    rec_full = recap.parse_recap(FULL)
    mid = asyncio.run(recap.post(ctx, _draft(final=final, rec=rec_full),
                                 recap.EndState(kind="merged"), close=True))
    _check("open: pings", m.cards[0][2]["mention"] == "owner-1", m.cards[0][2])
    _check("open: not closed", m.closed == [], m.closed)
    offer = store.get_choice_offer("chan-1")
    _check("open: offer on the card", offer and offer["msg_id"] == mid
           and offer["instance_id"] == "t-1", offer)
    _check("open: tap sends the full item",
           choices.tap_text(offer, 0, "Add the expiry test").startswith(recap.PICK_UP_PREFIX + "Add the expiry test.")
           and "Write a test that expires" in choices.tap_text(offer, 0, "Add the expiry test"),
           offer.get("messages"))
    allmsg = choices.tap_text(offer, 2, recap.DO_ALL_LABEL)
    _check("open: Do all is one numbered list", allmsg.startswith(recap.DO_ALL_PREFIX)
           and "1. Add the expiry test" in allmsg and "2. Clean old sessions" in allmsg, allmsg)

    # Every bot close goes through the gate.
    closed = asyncio.run(recap.close_thread(store, m, "chan-1", skip_mention=True))
    _check("gate: refuses while open", closed is False and m.closed == [], m.closed)
    # The Close button is the override.
    asyncio.run(recap.on_close_tap(ctx, "t-1", mid))
    _check("close tap: record dropped", store.get_recap("chan-1") is None)
    _check("close tap: offer closed", store.get_choice_offer("chan-1") is None)
    _check("close tap: rows stripped", (mid, "recap_close:") in m.stripped
           and (mid, "merge:") in m.stripped, m.stripped)
    _check("close tap: closed", m.closed == [("chan-1", True)], m.closed)
    closed = asyncio.run(recap.close_thread(store, _Messenger(), "chan-1"))
    _check("gate: closes once nothing is open", closed is True)

    # No send_card: the text fallback still posts.
    m = _Messenger(cards=False)
    asyncio.run(recap.post(_ctx(store, m, cid="chan-2"), _draft(final=final, rec=clean),
                           recap.EndState(kind="local"), close=False))
    _check("fallback post: as text", m.sent and m.sent[0][0].startswith("**Done**"), m.sent)

    # Held: never closes even with close=True, and a branch tap resolves it.
    m = _Messenger()
    ctx = _ctx(store, m, cid="chan-3")
    mid = asyncio.run(recap.post(ctx, _draft(final=final, rec=clean),
                                 recap.EndState(kind="held", branch_instance_id="t-1"), close=False))
    _check("held: pings", m.cards[0][2]["mention"] == "owner-1")
    _check("held: card found by id", recap.card_record(store, "chan-3", mid) is not None
           and recap.card_record(store, "chan-3", "other") is None)
    new_id = asyncio.run(recap.move_branch_action_off_card(ctx, mid, "Merging..."))
    _check("held: progress off the card", new_id and new_id != mid
           and (mid, "merge:") in m.stripped, (new_id, m.stripped))
    asyncio.run(recap.branch_resolved(ctx))
    _check("held: resolved closes", store.get_recap("chan-3")["held"] is False
           and m.closed == [("chan-3", True)], m.closed)

    # A held branch keeps the thread open while the branch exists, whoever
    # releases it: the gate reads the branch instance, not the record alone.
    branch_inst = _inst("t-7", branch="claude-bot/t-7")
    store.update_instance(branch_inst)
    m = _Messenger()
    ctx = _ctx(store, m, cid="chan-4")
    asyncio.run(recap.post(ctx, _draft(final=branch_inst, rec=clean),
                           recap.EndState(kind="held", branch_instance_id="t-7"), close=True))
    _check("held gate: post with close=True stays open", m.closed == [], m.closed)
    _check("held gate: record names the branch",
           store.get_recap("chan-4")["branch_instance_id"] == "t-7", store.get_recap("chan-4"))
    closed = asyncio.run(recap.close_thread(store, m, "chan-4", skip_mention=True))
    _check("held gate: refuses while the branch exists", closed is False and m.closed == [],
           m.closed)
    branch_inst.branch = None          # a typed /merge or fleet ship
    store.update_instance(branch_inst)
    closed = asyncio.run(recap.close_thread(store, m, "chan-4", skip_mention=True))
    _check("held gate: closes once the branch is gone", closed is True
           and m.closed == [("chan-4", True)], m.closed)
    store.put_recap("chan-5", {"msg_id": "5", "posted_at": time.time(), "open_items": [],
                               "held": True, "instance_id": "gone", "branch_instance_id": "gone"})
    closed = asyncio.run(recap.close_thread(store, _Messenger(), "chan-5"))
    _check("held gate: a pruned instance holds nothing", closed is True)

    # A merge that went wrong never pings from the card, open items or not:
    # the banner or warning beside it pings.
    for kind in ("merge_failed", "merge_stopped"):
        m = _Messenger()
        asyncio.run(recap.post(_ctx(store, m, cid="chan-6"), _draft(final=final, rec=rec_full),
                               recap.EndState(kind=kind), close=False, loud=True))
        _check(f"{kind}: card silent", m.cards[0][2] == {"silent": True, "mention": None},
               m.cards[0][2])
        _check(f"{kind}: not closed", m.closed == [], m.closed)

    # The record survives a reload, and month-old ones are pruned.
    store.put_recap("old", {"posted_at": time.time() - 40 * 86400, "open_items": ["x"]})
    store.put_recap("chan-9", {"posted_at": time.time(), "open_items": ["a"], "msg_id": "9"})
    store.save()
    again = _store(tmp / "pg")
    _check("record: survives a reload", again.get_recap("chan-9") == store.get_recap("chan-9"),
           again.get_recap("chan-9"))
    store.put_recap("chan-10", {"posted_at": time.time(), "open_items": []})
    _check("record: month-old pruned", store.get_recap("old") is None)


def case_needs_you(tmp: Path) -> None:
    store = _store(tmp / "ny")
    now = time.time()
    store.put_recap("th-1", {"posted_at": now - 60, "open_items": ["Log in on a phone", "b"],
                             "repo": "bot", "headline": "Login fixed"})
    store.put_recap("th-2", {"posted_at": now - 60, "open_items": [], "repo": "bot"})
    store.put_recap("th-3", {"posted_at": now - 8 * 86400, "open_items": ["x"], "repo": "bot"})
    store.put_recap("th-4", {"posted_at": now - 600, "open_items": ["y"], "repo": "bot"})
    idx = waiting.ThreadIndex()
    idx.threads["th-4"] = waiting.ThreadRef("th-4", "bot", None, "t", None, None, now - 10)
    items = [i for i in waiting.collect(store, idx, now=now) if i.kind == "open_items"]
    keys = {i.thread_id for i in items}
    _check("needs you: listed", keys == {"th-1"}, keys)
    if items:
        _check("needs you: detail", items[0].detail == "Log in on a phone (+1 more)", items[0].detail)
        _check("needs you: title", items[0].title == "Login fixed", items[0].title)
    _check("needs you: kind exists", any(k.name == "open_items" for k in waiting.KINDS))


# --- the wiring ------------------------------------------------------------


def case_wiring() -> None:
    done = _inst(origin=InstanceOrigin.DONE)
    rel = _inst(origin=InstanceOrigin.RELEASE)
    com = _inst(origin=InstanceOrigin.COMMIT)
    asking = _inst(origin=InstanceOrigin.DONE, needs_input=True)
    _check("suppress: Done", lifecycle._end_card_follows(done, False))
    _check("suppress: chain release", lifecycle._end_card_follows(rel, True))
    _check("suppress: not a lone release", not lifecycle._end_card_follows(rel, False))
    _check("suppress: not a commit", not lifecycle._end_card_follows(com, True))
    _check("suppress: not a question", not lifecycle._end_card_follows(asking, False))
    s = "```summary\nCOMMIT: abc1234567 msg\nCHANGELOG:\n- x\nVERSION: v2.0.0\n```"
    _check("status: version", lifecycle.finalize_status(s) == "done: v2.0.0")
    _check("status: commit", lifecycle.finalize_status(s.replace("v2.0.0", "none"))
           == "done: committed abc1234")
    _check("status: nothing", lifecycle.finalize_status("no block") is None)

    # Every bot-initiated close goes through the gate.
    offenders = []
    for path in (ROOT / "bot").rglob("*.py"):
        if path.name == "recap.py" or "telegram" in path.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\.close_conversation\(", line) and "def close_conversation" not in line:
                offenders.append(f"{path.relative_to(ROOT)}:{n}")
    _check("gate: no direct close outside recap", not offenders, offenders)

    from bot.engine import callbacks, chain_runner, steps
    cr = inspect.getsource(chain_runner._run_autopilot_chain)
    _check("chain: old notes gone", "Couldn't be verified automatically" not in cr
           and "Chain complete on branch" not in cr
           and "not everything asked for was delivered" not in cr)
    _check("chain: card prepared before the merge",
           cr.index("recap.prepare(") < cr.index("_finalize_merge("))
    _check("chain: merge reports, does not close", "skip_close=True" in cr and "report=merge_report" in cr)
    od = inspect.getsource(steps.on_done)
    _check("done: card prepared before the merge", od.index("recap.prepare(") < od.index("_finalize_merge("))
    _check("done: no close on a question", "if result.needs_input:" in od)
    _check("done: a stopped merge gets its card", 'kind="merge_stopped"' in od)
    _check("chain: a stopped merge gets its card",
           cr.index('kind="merge_stopped"') < cr.index('"merge_stopped",'))
    cb = inspect.getsource(callbacks.handle_callback)
    _check("callbacks: Close wired", "recap.CLOSE_ACTION" in cb)


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        orig = config.DATA_DIR
        config.DATA_DIR = tmp / "data"
        config.DATA_DIR.mkdir()
        try:
            case_parse()
            case_cards()
            case_limits()
            case_git_facts(tmp)
            case_recap_turn(tmp)
            case_image(tmp)
            case_post_and_gate(tmp)
            case_needs_you(tmp)
            case_wiring()
        finally:
            config.DATA_DIR = orig
    print(f"{PASSES} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
