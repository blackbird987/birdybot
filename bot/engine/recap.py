"""The end card: one compact card where a chain or a Done used to end in noise.

A finished chain used to leave a commit card, a release card (the same
changelog, cost and tokens twice), step lines, a "couldn't be verified" note,
a "not everything delivered" note, a merge line and a "chain complete" message
whose buttons repeated the release card's. Someone coming back to the thread
hours later, after seven other threads, had to scroll up to find out what it
was even about.

The card answers that in one look:

* **the headline and what was asked**, in the owner's terms;
* **before and now**, which reads as problem and fix, question and answer, or
  missing and built, whichever the work was;
* **what is still open**: what only the owner can do, and what the session
  could pick up next, each Next item a button;
* the facts the bot knows for certain (version, merge, size of the change),
  taken from git and never from the model.

The words come from a short read-only turn that resumes the session
(``InstanceOrigin.RECAP``). It is caller-read, so it posts nothing itself, and
its status line is deleted once it has answered. Anything it gets wrong or
fails to say falls back to the bot's own facts: the card always posts.

A card with anything open never closes its thread. ``close_thread`` is the one
door every bot-initiated close goes through, and it refuses while the thread's
end card still has open items. The card's Close button is the override.

Design note: ``docs/design/session-recap.md``. Harness:
``scripts/test_session_recap.py``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from bot import config
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType
from bot.platform.base import ButtonSpec, CardSpec
from bot.platform.redact import redact_secrets
from bot.textutil import mask_tilde_bodies, parse_iso_utc

log = logging.getLogger(__name__)

HEADLINE_MAX = 60
LABEL_MAX = 30
BULLETS_MAX = 3
BULLET_CHARS = 220
NEXT_MAX = 3
ASKED_CHARS = 300
FIELD_CHARS = 1000      # Discord's field ceiling is 1024
OWNER_MSG_CHARS = 280
OWNER_MSGS_MAX = 4
DO_ALL_LABEL = "Do all"
CLOSE_ACTION = "recap_close"
IMAGE_DIR_NAME = "recap_images"
# What a Next button sends. A label alone ("Fix the flaky test") reads as a
# fresh request with no context; this says where it came from.
PICK_UP_PREFIX = "Pick up this open item from the end card: "
DO_ALL_PREFIX = "Pick up these open items from the end card, in order:"

_BLOCK_RE = re.compile(r"^```recap[ \t]*\n(.*?)^```[ \t]*$", re.S | re.M)
_KEY_RE = re.compile(
    r"^\s*(HEADLINE|ASKED|BEFORE|NOW|FOR_YOU|NEXT|IMAGE)\s*:\s*(.*)$", re.I,
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_NONE_RE = re.compile(r"^\(?\s*(?:none|nothing|n/?a|-)\s*[.)]?\s*$", re.I)

# Merge notes that mean something did not happen: they belong under "For you".
_PROBLEM_NOTE_RE = re.compile(
    r"^(?:Could not push|Push to origin|Tags not pushed|Tag push|Could not sync|"
    r"Release commit \S+ was not tagged|Stashed changes not auto-restored|"
    r"⛔|⚠)",
)
# Merge notes that say nothing the owner needs.
_NOISE_NOTE_RE = re.compile(
    r"^(?:Tags pushed:|ℹ️? No remote configured|"
    r"ℹ️? Stashed changes restored)",
)


# --- The recap the session writes ------------------------------------------


@dataclass
class NextItem:
    label: str
    message: str


@dataclass
class Recap:
    headline: str = ""
    asked: str = ""
    before: list[str] = field(default_factory=list)
    now: list[str] = field(default_factory=list)
    for_you: list[str] = field(default_factory=list)
    next: list[NextItem] = field(default_factory=list)
    image: str | None = None


def _cut(text: str, n: int) -> str:
    """At most ``n`` characters, line breaks kept, an ellipsis on a cut."""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _clip(text: str, n: int) -> str:
    """One line of at most ``n`` characters: whitespace runs become a space."""
    return _cut(" ".join((text or "").split()), n)


def _is_none(text: str) -> bool:
    return not text.strip() or bool(_NONE_RE.match(text.strip()))


def _bullet(line: str) -> str:
    return _BULLET_RE.sub("", line, count=1).strip()


def _next_item(line: str) -> NextItem | None:
    text = _bullet(line)
    if _is_none(text):
        return None
    label, sep, message = text.partition("|")
    label, message = label.strip(), message.strip()
    if not sep or not message:
        message = text.replace("|", " ").strip()
        label = message
    if not label:
        return None
    return NextItem(label=_clip(label, LABEL_MAX), message=_clip(message, 400))


def parse_recap(text: str | None) -> Recap | None:
    """The last ```recap block in a reply, or None when there is none usable.

    Scanned on the masked text so a block quoted inside another session's
    ``~~~`` body is never read (masking keeps offsets, so the body is sliced
    from the original). A block with no headline, no ask and no "now" says
    nothing and counts as missing.
    """
    if not text:
        return None
    matches = list(_BLOCK_RE.finditer(mask_tilde_bodies(text)))
    if not matches:
        return None
    m = matches[-1]
    body = text[m.start(1):m.end(1)]
    rec = Recap()
    key = None
    lists = {"BEFORE": rec.before, "NOW": rec.now, "FOR_YOU": rec.for_you}
    for raw in body.splitlines():
        km = _KEY_RE.match(raw)
        if km:
            key, value = km.group(1).upper(), km.group(2).strip()
        else:
            value = raw.strip()
            if not value or key is None:
                continue
        if not value:
            continue
        if key == "HEADLINE":
            rec.headline = rec.headline or _clip(value, HEADLINE_MAX)
        elif key == "ASKED":
            rec.asked = _clip(f"{rec.asked} {value}", ASKED_CHARS)
        elif key == "IMAGE":
            if not _is_none(value) and rec.image is None:
                rec.image = value.strip().strip("`\"'")
        elif key == "NEXT":
            item = _next_item(value)
            if item and len(rec.next) < NEXT_MAX:
                rec.next.append(item)
        elif key in lists:
            b = _bullet(value)
            if not _is_none(b) and len(lists[key]) < BULLETS_MAX:
                lists[key].append(_clip(b, BULLET_CHARS))
    if not (rec.headline or rec.asked or rec.now):
        return None
    return rec


# --- The bot's own facts ---------------------------------------------------


@dataclass
class Facts:
    repo: str = ""
    asked: str = ""                 # fallback ask, from the owner's own words
    owner_messages: list[str] = field(default_factory=list)
    version: str | None = None      # from the release commit's subject, never the model
    commit: str | None = None       # short hash of the newest commit
    subject: str = ""               # that commit's subject, version prefix removed
    changelog: list[str] = field(default_factory=list)
    files: int = 0
    added: int = 0
    removed: int = 0
    manual_checks: list[str] = field(default_factory=list)
    not_done: list[str] = field(default_factory=list)
    leftovers: int = 0
    branch: str | None = None
    target: str | None = None
    model: str | None = None
    started_at: float | None = None
    hold_reason: str | None = None


def lineage(store, final: Instance, extra: list[Instance] | None = None) -> list[Instance]:
    """``final`` and every step before it, newest first, each once.

    Walks ``parent_id`` (and ``chained_from``) because a resumed chain starts
    with an empty in-memory instance list; ``extra`` adds whatever the caller
    still holds.
    """
    out: list[Instance] = []
    seen: set[str] = set()
    queue: list[Instance | None] = [final]
    while queue and len(out) < 60:
        inst = queue.pop(0)
        if inst is None or inst.id in seen:
            continue
        seen.add(inst.id)
        out.append(inst)
        for ref in (inst.parent_id, inst.chained_from):
            if ref and ref not in seen:
                try:
                    queue.append(store.get_instance(ref))
                except Exception:
                    pass
    for inst in extra or []:
        if inst is not None and inst.id not in seen:
            seen.add(inst.id)
            out.append(inst)
    return out


def _git(repo: str, *args: str) -> str | None:
    from bot.procutil import run_capture
    try:
        r = run_capture(["git", *args], cwd=repo, timeout=15)
    except Exception:
        return None
    return (r.stdout or "") if r.returncode == 0 else None


_SHORTSTAT_RE = re.compile(
    r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?",
)


def _shortstat(out: str | None) -> tuple[int, int, int]:
    m = _SHORTSTAT_RE.search(out or "")
    if not m:
        return 0, 0, 0
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


def _release_version(subject: str) -> tuple[str | None, str]:
    from bot.claude.runner.git_release import _RELEASE_COMMIT_RE
    m = _RELEASE_COMMIT_RE.match(subject or "")
    if not m:
        return None, subject or ""
    return m.group(1), subject[m.end():].strip()


def git_facts(repo: str | None, branch: str | None, target: str | None,
              commit_hint: str | None = None) -> dict:
    """Version, newest commit and size of the change, read from git only.

    A build branch is measured over the whole branch (``target...branch``),
    not from the last step's entry point, so a release step's two-line diff
    never stands for a build of hundreds. Without a branch only the commit the
    session named is used, and only once git confirms it exists. Any read
    that fails leaves its fact empty.
    """
    out: dict = {}
    if not repo or not Path(repo).is_dir():
        return out
    if branch and target:
        subjects = _git(repo, "log", "--format=%h%x00%s", f"{target}..{branch}") or ""
        rows = [r.split("\0", 1) for r in subjects.splitlines() if "\0" in r]
        if rows:
            out["commit"], out["subject"] = rows[0][0], rows[0][1]
        for _h, subj in rows:
            ver, _rest = _release_version(subj)
            if ver:
                out["version"] = ver
                break
        files, added, removed = _shortstat(
            _git(repo, "diff", "--shortstat", f"{target}...{branch}"))
        out.update(files=files, added=added, removed=removed)
        return out
    if commit_hint and re.fullmatch(r"[0-9a-fA-F]{6,40}", commit_hint):
        row = _git(repo, "log", "-1", "--format=%h%x00%s", commit_hint)
        if row and "\0" in row:
            h, subj = row.strip().split("\0", 1)
            out["commit"], out["subject"] = h, subj
            ver, _rest = _release_version(subj)
            if ver:
                out["version"] = ver
    return out


def _approval_only(text: str) -> bool:
    words = re.sub(r"[^a-z ]", " ", text.lower()).split()
    return len(" ".join(words)) < 16


async def gather_facts(
    ctx, final: Instance, *, chain_instances: list[Instance] | None = None,
    leftovers: int = 0, hold_reason: str | None = None,
) -> Facts:
    """Everything the card states as fact. Never raises."""
    from bot.platform.formatting import parse_finalize_output
    facts = Facts(repo=final.repo_name or "", leftovers=leftovers,
                  hold_reason=hold_reason, branch=final.branch,
                  target=final.original_branch)
    try:
        insts = lineage(ctx.store, final, chain_instances)
        seen_checks: set[str] = set()
        for inst in insts:
            reason = (inst.manual_verify_reason or "").strip()
            if inst.needs_manual_verification and reason and reason not in seen_checks:
                seen_checks.add(reason)
                facts.manual_checks.append(_clip(redact_secrets(reason), BULLET_CHARS))
            nd = (inst.verify_not_done or "").strip()
            if nd and nd not in facts.not_done:
                facts.not_done.append(_clip(redact_secrets(nd), BULLET_CHARS))
            if not facts.model and inst.origin == InstanceOrigin.BUILD:
                facts.model = inst.context_model or inst.model
        commit_hint = None
        for inst in insts:  # newest first: the release beats the commit before it
            if inst.origin not in (InstanceOrigin.DONE, InstanceOrigin.RELEASE,
                                   InstanceOrigin.COMMIT):
                continue
            if inst.status != InstanceStatus.COMPLETED:
                continue
            info = parse_finalize_output(inst.read_result_text() or "")
            if not info:
                continue
            if not facts.changelog and info.changelog_entries:
                facts.changelog = [_clip(redact_secrets(e), BULLET_CHARS)
                                   for e in info.changelog_entries[:BULLETS_MAX]]
            commit_hint = commit_hint or info.commit_hash
        starts = [parse_iso_utc(i.created_at) for i in insts if i.created_at]
        starts = [s.timestamp() for s in starts if s is not None]
        facts.started_at = min(starts) if starts else None
        if not facts.model:
            facts.model = final.context_model or final.model
        g = await asyncio.to_thread(
            git_facts, final.repo_path, final.branch, final.original_branch, commit_hint,
        )
        facts.version = g.get("version")
        facts.commit = g.get("commit")
        facts.subject = redact_secrets(_release_version(g.get("subject") or "")[1])
        facts.files, facts.added, facts.removed = (
            g.get("files", 0), g.get("added", 0), g.get("removed", 0))
    except Exception:
        log.warning("End card: facts for %s were partly unreadable", final.id, exc_info=True)
    try:
        msgs = await ctx.messenger.owner_messages(ctx.channel_id)
    except Exception:
        msgs = None
    if msgs:
        facts.owner_messages = pick_owner_messages(msgs)
        first = next((m for m in msgs if not _approval_only(m)), None)
        if first:
            facts.asked = _clip(redact_secrets(first), ASKED_CHARS)
    return facts


def pick_owner_messages(msgs: list[str]) -> list[str]:
    """The owner's first real request and their latest messages, oldest first.

    The first is what the thread was about, the latest are where it went; a
    bare "go" or "sgo" in between says nothing about either.
    """
    real = [m for m in msgs if m and m.strip()]
    first = next((i for i, m in enumerate(real) if not _approval_only(m)), None)
    keep = set(range(max(0, len(real) - (OWNER_MSGS_MAX - 1)), len(real)))
    if first is not None:
        keep.add(first)
    return [_clip(redact_secrets(real[i]), OWNER_MSG_CHARS) for i in sorted(keep)]


def facts_text(facts: Facts) -> str:
    """The facts as the recap turn sees them: what it must not repeat, and
    the owner's own words to name the ask from."""
    lines = [f"- Repo: {facts.repo or 'unknown'}"]
    if facts.version:
        lines.append(f"- Release: {facts.version}")
    if facts.commit:
        lines.append(f"- Newest commit: {facts.commit} {facts.subject}".rstrip())
    if facts.files:
        lines.append(f"- Size: {facts.files} files, +{facts.added} -{facts.removed}")
    if facts.branch and facts.target:
        lines.append(f"- Built on its own branch, to land on {facts.target}")
    if facts.changelog:
        lines.append("- Changelog: " + "; ".join(facts.changelog))
    for c in facts.manual_checks:
        lines.append(f"- Needs a check by hand (shown to the user already): {c}")
    for n in facts.not_done:
        lines.append(f"- Verify said not delivered (shown already): {n}")
    if facts.leftovers:
        lines.append(f"- {facts.leftovers} review leftovers were filed in TODO.md")
    if facts.hold_reason:
        lines.append(f"- Merge held for the user: {facts.hold_reason}")
    if facts.owner_messages:
        lines.append("- What the user typed in this thread, oldest first:")
        lines.extend(f"  {i}. {m}" for i, m in enumerate(facts.owner_messages, 1))
    return "\n".join(lines)


# --- The recap turn --------------------------------------------------------


@dataclass
class Draft:
    """What ``prepare`` gathered before the merge, for ``post`` after it."""
    final: Instance
    facts: Facts
    recap: Recap | None = None
    image_path: str | None = None


async def _drop_status_line(ctx, inst: Instance) -> None:
    """Delete the recap turn's own status line: the card is its output."""
    msgs = list((inst.message_ids or {}).get(ctx.platform, []))
    delete = getattr(ctx.messenger, "delete_message", None)
    if not msgs or delete is None:
        return
    try:
        await delete(ctx.channel_id, msgs[-1])
    except Exception:
        log.debug("Could not delete the recap status line", exc_info=True)


async def run_recap_turn(ctx, final: Instance, facts: Facts) -> Recap | None:
    """Ask the session for the card's words. None on any failure. Never raises.

    Resumes the session, read-only at the floor that also closes Bash, on the
    light model, and posts nothing of its own.
    """
    if not final.session_id:
        return None
    try:
        if ctx.runner.check_spawn_allowed():
            # A drain would queue it as a button replay with no handler.
            return None
    except Exception:
        return None
    from bot.engine.step_spawn import SpawnConfig, spawn_from
    prompt = config.RECAP_PROMPT.replace(config.RECAP_SLOT, facts_text(facts))
    try:
        inst = await spawn_from(ctx, final.id, SpawnConfig(
            instance_type=InstanceType.QUERY, prompt=prompt, mode="explore",
            origin=InstanceOrigin.RECAP, status_text="Writing the recap...",
            resume_session=True, copy_branch=True, silent=True,
            permission_mode="explore",
        ), strip_source_buttons=False)
    except Exception:
        log.warning("End card: the recap turn for %s failed to start", final.id,
                    exc_info=True)
        return None
    if inst is None:
        return None
    await _drop_status_line(ctx, inst)
    if inst.status != InstanceStatus.COMPLETED or inst.needs_input:
        return None
    return parse_recap(inst.read_result_text())


def keep_image(store, final: Instance, raw: str | None, *, ctx=None) -> str | None:
    """Copy the picture the recap named somewhere the merge will not delete.

    Same path rules as ``/image`` (and the same sandbox answer), then a copy
    under the data dir, because a build's worktree is gone once it merges.
    None when anything about it is off: no picture is better than no card.
    """
    if not raw:
        return None
    try:
        from bot.claude.sandbox import instance_confined
        from bot.engine.images import resolve_image
        path, reason = resolve_image(
            raw, final, sandboxed=instance_confined(store, final, ctx=ctx))
        if path is None:
            log.info("End card: picture %r refused (%s)", raw, reason)
            return None
        dest_dir = config.DATA_DIR / IMAGE_DIR_NAME
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{final.id}{path.suffix.lower()}"
        shutil.copyfile(path, dest)
        return str(dest)
    except Exception:
        log.info("End card: picture %r could not be kept", raw, exc_info=True)
        return None


async def prepare(
    ctx, final: Instance, *, chain_instances: list[Instance] | None = None,
    leftovers: int = 0, hold_reason: str | None = None,
) -> Draft:
    """Gather the facts and the recap while the branch and worktree exist.

    Runs before a merge, which deletes the worktree the session's cwd and any
    picture live in. Never raises; a failure leaves a draft with facts only.
    """
    try:
        facts = await gather_facts(ctx, final, chain_instances=chain_instances,
                                   leftovers=leftovers, hold_reason=hold_reason)
    except Exception:
        log.warning("End card: no facts for %s", final.id, exc_info=True)
        facts = Facts(repo=final.repo_name or "")
    draft = Draft(final=final, facts=facts)
    draft.recap = await run_recap_turn(ctx, final, facts)
    if draft.recap and draft.recap.image:
        draft.image_path = keep_image(ctx.store, final, draft.recap.image, ctx=ctx)
    return draft


# --- The card --------------------------------------------------------------


@dataclass
class EndState:
    """How the work ended, which only the caller knows.

    ``kind``: ``merged``, ``held`` (a branch waiting for the owner),
    ``merge_failed``, ``merge_stopped`` (it landed on a tree that must not
    ship, so deploy and close were skipped), or ``local`` (no branch:
    committed in place or nothing).
    """
    kind: str = "local"
    merge_notes: list[str] = field(default_factory=list)
    decided_note: str = ""
    branch_instance_id: str | None = None


# Endings where the merge went wrong. Every caller follows the card with a
# message that pings on its own (the conflict banner, the stop warning, the
# chain's exit mention), so the card itself stays silent: one event, one ping.
_TROUBLE_KINDS = frozenset({"merge_failed", "merge_stopped"})


@dataclass
class Card:
    spec: CardSpec
    open_items: list[str]
    next_items: list[NextItem]
    held: bool


def split_merge_notes(notes: list[str]) -> tuple[list[str], list[str]]:
    """Merge messages to (problems, notes), dropping the first line and noise.

    A line in backticks after a problem is git's own reason for it, so it
    rides on that problem.
    """
    problems: list[str] = []
    other: list[str] = []
    for msg in notes:
        lines = [ln.strip() for ln in (msg or "").splitlines()]
        last: list[str] | None = None
        for ln in lines[1:]:
            if not ln:
                continue
            if ln.startswith("`") and last is not None and last:
                last[-1] = f"{last[-1]} ({ln.strip('`')})"
                continue
            if _NOISE_NOTE_RE.match(ln):
                last = None
                continue
            target = problems if _PROBLEM_NOTE_RE.match(ln) else other
            target.append(_clip(ln, BULLET_CHARS))
            last = target
    return problems, other


def _bullets(items: list[str]) -> str:
    return _cut("\n".join(f"• {i}" for i in items if i), FIELD_CHARS)


EMBED_BUDGET = 5800     # Discord rejects an embed over 6000 characters in total


def _fit_embed(title: str, description: str, footer: str,
               fields: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Shrink the longest fields until the whole embed fits Discord's total.

    Each field is already under its own 1024 ceiling, but six full ones plus
    the status line add up past 6000, and Discord then refuses the whole card.
    """
    fields = list(fields)
    fixed = len(title) + len(description) + len(footer) + sum(len(n) for n, _ in fields)
    while fields and fixed + sum(len(v) for _, v in fields) > EMBED_BUDGET:
        over = fixed + sum(len(v) for _, v in fields) - EMBED_BUDGET
        i = max(range(len(fields)), key=lambda k: len(fields[k][1]))
        name, value = fields[i]
        keep = max(80, len(value) - over - 1)
        if keep >= len(value):
            break
        fields[i] = (name, value[:keep].rstrip() + "…")
    return fields


def _elapsed(facts: Facts, now: float) -> str | None:
    if facts.started_at is None:
        return None
    from bot.platform.formatting import format_elapsed
    return format_elapsed(max(0.0, now - facts.started_at))


def build_card(draft: Draft, state: EndState, *, now: float | None = None) -> Card:
    """The card for one ending. Pure: no I/O, so the harness can pin it."""
    now = time.time() if now is None else now
    facts, rec = draft.facts, draft.recap or Recap()

    # Status line: what the bot knows for certain.
    ver = facts.version
    held = state.kind == "held"
    if state.kind == "merged":
        status = (f"Shipped {ver}, merged into {facts.target or 'the main branch'}"
                  if ver else f"Merged into {facts.target or 'the main branch'}")
    elif held:
        status = (f"{ver} built, waiting for your merge" if ver
                  else "Built on its branch, waiting for your merge")
    elif state.kind == "merge_failed":
        status = "The merge did not land: see the message below"
    elif state.kind == "merge_stopped":
        status = (f"Merged into {facts.target or 'the main branch'}, stopped "
                  f"before deploy: see the warning above")
    elif ver:
        status = f"Released {ver}"
    elif facts.commit:
        status = f"Committed `{facts.commit}`"
    else:
        status = "Nothing committed"
    bits = [status]
    if facts.files:
        bits.append(f"{facts.files} file{'s' if facts.files != 1 else ''}, "
                    f"+{facts.added} -{facts.removed}")

    problems, notes = split_merge_notes(state.merge_notes)
    for_you = ([f"Check by hand: {c}" for c in facts.manual_checks]
               + [f"Not delivered: {n}" for n in facts.not_done]
               + problems
               + [redact_secrets(x) for x in rec.for_you])
    next_items = [NextItem(redact_secrets(n.label), redact_secrets(n.message))
                  for n in rec.next[:NEXT_MAX]]
    open_items = for_you + [n.label for n in next_items]
    if open_items:
        bits.append(f"{len(open_items)} open")
    description = " · ".join(bits)
    if held and facts.hold_reason:
        description += f"\nHeld: {_clip(redact_secrets(facts.hold_reason), BULLET_CHARS)}"

    title = rec.headline or _clip(facts.subject, HEADLINE_MAX) or "Work finished"
    asked = rec.asked or facts.asked
    now_lines = rec.now or facts.changelog[:BULLETS_MAX]

    fields: list[tuple[str, str]] = []
    if asked:
        fields.append(("You asked", _cut(redact_secrets(asked), FIELD_CHARS)))
    if rec.before:
        fields.append(("Before", _bullets([redact_secrets(b) for b in rec.before])))
    if now_lines:
        fields.append(("Now", _bullets([redact_secrets(n) for n in now_lines])))
    if for_you:
        fields.append(("For you", _bullets(for_you)))
    if next_items:
        fields.append(("Next", _cut("\n".join(
            f"{i}. **{n.label}**: {n.message}" for i, n in enumerate(next_items, 1)),
            FIELD_CHARS)))
    extra = list(notes)
    if state.decided_note:
        extra.append(state.decided_note)
    if facts.leftovers:
        extra.append(f"{facts.leftovers} review leftover"
                     f"{'s' if facts.leftovers != 1 else ''} filed in TODO.md")
    if extra:
        fields.append(("Notes", _bullets(extra)))

    if state.kind in _TROUBLE_KINDS or open_items or held:
        color = "gold"
    elif state.kind == "merged" or facts.commit:
        color = "green"
    else:
        color = "blurple"

    from bot.platform.formatting import short_model_label
    footer_bits = [facts.repo, _elapsed(facts, now),
                   short_model_label(facts.model) if facts.model else None,
                   facts.commit]
    footer = " · ".join(b for b in footer_bits if b)
    title = _clip(redact_secrets(title), 256)
    # _cut, not _clip: the "Held:" line must stay on its own line.
    description = _cut(description, 4000)
    fields = _fit_embed(title, description, footer, fields)
    spec = CardSpec(title=title, description=description,
                    fields=fields, color=color, footer=footer,
                    image_path=draft.image_path)
    return Card(spec=spec, open_items=open_items, next_items=next_items, held=held)


def pick_up_messages(items: list[NextItem]) -> tuple[list[str], list[str]]:
    """Labels and the message each one sends; "Do all" when there are two or more."""
    labels = [n.label for n in items]
    msgs = [f"{PICK_UP_PREFIX}{n.label}. {n.message}" for n in items]
    if len(items) >= 2:
        labels.append(DO_ALL_LABEL)
        msgs.append(DO_ALL_PREFIX + "\n" + "\n".join(
            f"{i}. {n.label}: {n.message}" for i, n in enumerate(items, 1)))
    return labels, msgs


def card_buttons(card: Card, iid: str, state: EndState) -> list[list[ButtonSpec]]:
    """Next row, the held branch's row, then Close and Missed. At most 3 rows."""
    from bot.platform.buttons import choice_row_specs
    rows: list[list[ButtonSpec]] = []
    if card.next_items:
        labels, _msgs = pick_up_messages(card.next_items)
        rows.append(choice_row_specs(iid, labels))
    if card.held and state.branch_instance_id:
        bid = state.branch_instance_id
        rows.append([ButtonSpec("Merge", f"merge:{bid}"),
                     ButtonSpec("Discard", f"discard:{bid}"),
                     ButtonSpec("Diff", f"diff:{bid}")])
    last: list[ButtonSpec] = []
    if card.open_items or card.held:
        last.append(ButtonSpec("Close", f"{CLOSE_ACTION}:{iid}"))
    last.append(ButtonSpec("Missed", f"missed:{iid}"))
    rows.append(last)
    return rows


# --- Posting and the close gate --------------------------------------------


async def post(
    ctx, draft: Draft | None, state: EndState, *, close: bool, loud: bool = False,
) -> str | None:
    """Post the end card, record it, and close the thread when nothing is open.

    ``loud`` pings even when nothing is open (Autopilot's completion ping).
    Open items or a held branch ping too, except on a merge that went wrong,
    whose own warning pings (``_TROUBLE_KINDS``). Never raises; returns the
    card's message id.
    """
    if draft is None:
        return None
    try:
        card = build_card(draft, state)
    except Exception:
        log.exception("End card: could not build the card for %s", draft.final.id)
        return None
    iid = draft.final.id
    buttons = card_buttons(card, iid, state)
    pings = (loud or card.open_items or card.held) and state.kind not in _TROUBLE_KINDS
    mention = ctx.user_id if pings else None
    msg_id: str | None = None
    try:
        send_card = getattr(ctx.messenger, "send_card", None)
        if send_card is not None:
            msg_id = await send_card(ctx.channel_id, card.spec, buttons=buttons,
                                     silent=mention is None, mention_user_id=mention)
        else:
            msg_id = await ctx.messenger.send_result(
                ctx.channel_id, ctx.messenger.markdown_to_markup(card.spec.as_text()),
                buttons=buttons, silent=mention is None, mention_user_id=mention)
    except Exception:
        log.exception("End card: posting failed for %s", iid)
        return None
    if msg_id and card.next_items:
        from bot.engine.directives import choices
        labels, msgs = pick_up_messages(card.next_items)
        choices.open_offer(ctx, draft.final, labels, str(msg_id), messages=msgs)
    record = {
        "msg_id": str(msg_id) if msg_id else None,
        "posted_at": time.time(),
        "repo": draft.final.repo_name,
        "headline": card.spec.title,
        "open_items": card.open_items,
        "held": card.held,
        "instance_id": iid,
        "branch_instance_id": state.branch_instance_id if card.held else None,
    }
    try:
        ctx.store.put_recap(ctx.channel_id, record)
    except Exception:
        log.warning("End card: could not record the card for %s", ctx.channel_id,
                    exc_info=True)
    from bot.engine import waiting
    waiting.notify_changed()
    log.info("End card posted in %s for %s (%s, %d open)", ctx.channel_id, iid,
             state.kind, len(card.open_items))
    if close:
        await close_thread(ctx.store, ctx.messenger, ctx.channel_id, skip_mention=True)
    return str(msg_id) if msg_id else None


def _record(store, channel_id: str | None) -> dict | None:
    """The thread's end card record, or None. Never raises."""
    if not channel_id:
        return None
    get = getattr(store, "get_recap", None)
    if get is None:
        return None
    try:
        rec = get(str(channel_id))
    except Exception:
        return None
    return rec if isinstance(rec, dict) else None


def open_items_for(store, channel_id: str | None) -> list[str]:
    """The thread's end card's open items; empty when it has none or no card."""
    rec = _record(store, channel_id) or {}
    return [str(x) for x in (rec.get("open_items") or []) if str(x).strip()]


def _holds_branch(store, rec: dict | None) -> bool:
    """The card's held branch is still waiting for Merge or Discard.

    Read from the instance, not the record alone: a branch merged or
    discarded some other way (a typed /merge, fleet ship) clears
    ``instance.branch`` without touching the card. A gone instance holds
    nothing.
    """
    if not rec or not rec.get("held"):
        return False
    bid = rec.get("branch_instance_id") or rec.get("instance_id")
    if not bid:
        return False
    try:
        inst = store.get_instance(str(bid))
    except Exception:
        return False
    return bool(inst is not None and getattr(inst, "branch", None))


async def close_thread(store, messenger, channel_id: str | None, *,
                       skip_mention: bool = False) -> bool:
    """The one door for a bot-initiated close. Refuses while anything is open:
    an open item on the end card, or a held branch still waiting.

    Returns True when the close was asked for. The card's Close button does
    not come through here: it is the owner overriding exactly this.
    """
    if not channel_id:
        return False
    rec = _record(store, channel_id)
    items = open_items_for(store, channel_id)
    if items:
        log.info("Not closing %s: its end card has %d open item(s)", channel_id,
                 len(items))
        return False
    if _holds_branch(store, rec):
        log.info("Not closing %s: its end card's branch is still held", channel_id)
        return False
    try:
        await messenger.close_conversation(channel_id, skip_mention=skip_mention)
    except Exception:
        log.warning("Close of %s failed", channel_id, exc_info=True)
        return False
    return True


def card_record(store, channel_id: str | None, msg_id: str | None) -> dict | None:
    """The end card's record when ``msg_id`` is that card, else None."""
    rec = _record(store, channel_id)
    if not rec or not msg_id or str(rec.get("msg_id")) != str(msg_id):
        return None
    return rec


async def _strip(ctx, msg_id: str | None, prefix: str) -> None:
    """Take the button rows starting with ``prefix`` off a message. Best effort."""
    strip = getattr(ctx.messenger, "strip_button_rows", None)
    if not msg_id or strip is None:
        return
    try:
        await strip(ctx.channel_id, msg_id, prefix)
    except Exception:
        log.debug("Could not strip %s rows from %s", prefix, msg_id, exc_info=True)


async def move_branch_action_off_card(ctx, msg_id: str, progress: str) -> str | None:
    """A Merge or Discard tapped on the card: take that row off the card
    (its other rows stay, it is never redrawn from specs) and post the
    progress line as its own message, which the outcome then replaces."""
    await _strip(ctx, msg_id, "merge:")
    try:
        return await ctx.messenger.send_text(ctx.channel_id, progress, silent=True)
    except Exception:
        return None


async def branch_resolved(ctx, *, keep_open: bool = False) -> None:
    """The card's held branch was merged or discarded: it is no longer held,
    and the thread closes when nothing else on the card is open."""
    rec = _record(ctx.store, ctx.channel_id)
    if rec:
        rec = dict(rec)
        rec["held"] = False
        try:
            ctx.store.put_recap(ctx.channel_id, rec)
        except Exception:
            log.warning("End card: could not update the record in %s",
                        ctx.channel_id, exc_info=True)
    if not keep_open:
        await close_thread(ctx.store, ctx.messenger, ctx.channel_id, skip_mention=True)


async def on_close_tap(ctx, instance_id: str, source_msg_id: str | None) -> None:
    """Close: the owner says the open items can wait. Drops the record, the
    offer and the card's rows, then archives the thread past the gate."""
    try:
        ctx.store.pop_recap(ctx.channel_id)
    except Exception:
        log.debug("No end card record to drop in %s", ctx.channel_id, exc_info=True)
    from bot.engine.directives import choices
    await choices.close_offer(ctx, ctx.channel_id, "closed")
    for prefix in (f"{CLOSE_ACTION}:", "merge:"):
        await _strip(ctx, source_msg_id, prefix)
    from bot.engine import waiting
    waiting.notify_changed()
    try:
        await ctx.messenger.close_conversation(ctx.channel_id, skip_mention=True)
    except Exception:
        log.debug("Close of %s failed", ctx.channel_id, exc_info=True)
