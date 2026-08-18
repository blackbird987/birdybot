"""Orchestrator loop: parent waits for its whole spawn wave, then resumes.

A parent thread that fans out N children used to receive N independent
callbacks, each carrying 400 characters of the child's report and its own
"Resume parent" button. The human was the join point (deciding when enough
children were back), the relay (pasting the reports the truncation ate), and
the watchdog (noticing children that stalled on a question). This module
removes all three jobs.

**The join is derived, never stored.** ``_child_state`` recomputes a child's
status from records that already exist and are already authoritative: the
child's ThreadInfo gives its session, the session's newest Instance gives its
terminal status and whether it parked on a question. Nothing is written down
that a reboot mid-wave could desynchronise, and a child that is killed — or
that dies without ever invoking the finalize callback — still reads as settled
(or as never-arriving, which the timeout sweep handles).

**Reports travel as file paths, not text.** Every run already writes its full
final output to ``data/results/<id>.md`` and persists the path on the instance
(``Instance.result_file``). The parent is handed those paths plus a short
excerpt, so it reads the reports it cares about with its own file tools. No
truncation ceiling, and no multi-KB blobs accumulating in ``state.json``.

The one persisted addition is ``Instance.spawn_wave_released`` — a boolean
making the release idempotent, because the per-child callbacks and the timeout
sweep can both reach it.

Blocked children (parked on a question) do not settle. They are reported to the
parent immediately, because the parent wrote the child's brief and is the right
one to answer it — see the ``/reply`` directive in ``bot/engine/commands.py``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import discord

from bot import config
from bot.claude.types import Instance, InstanceStatus
from bot.platform.base import ButtonSpec

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

_PAYLOAD_KEY = "orch_resume_payloads"
# Excerpt shown inline per child, in the human-facing post AND in the parent's
# resume prompt. Deliberately small: the full report is a file path away, and
# padding the prompt with prose the parent may not need is what the old
# truncate-then-hand-over design got wrong in the other direction.
_EXCERPT = 400
# Terminal instance states. KILLED counts: a child the user cancelled is never
# coming back, so it must settle or it would hold the wave open until timeout.
_TERMINAL = (InstanceStatus.COMPLETED, InstanceStatus.FAILED, InstanceStatus.KILLED)


class ChildState(NamedTuple):
    """Derived view of one spawned child.

    ``state`` is one of:
      ``completed`` / ``failed`` / ``killed`` — settled, wave may close
      ``blocked``  — finished a turn parked on a question; NOT settled, the
                     parent (or the user) has to answer before it moves
      ``running``  — a turn is in flight
      ``pending``  — dispatched but no session recorded yet (still starting,
                     or it died before its first turn produced one)
      ``gone``     — the thread is no longer in the forum map
    """
    thread_id: str
    state: str
    title: str
    inst: Instance | None

    @property
    def settled(self) -> bool:
        return self.state in ("completed", "failed", "killed", "gone")


# --- Resume payload store ---------------------------------------------------
# Discord caps a button's custom_id at 100 chars, so the prompt a button would
# replay is parked here under a short token and the id carries only the token.


def _get_payloads(bot: ClaudeBot) -> dict:
    state = bot._store.get_platform_state("discord")
    payloads = state.get(_PAYLOAD_KEY)
    if payloads is None:
        payloads = {}
        state[_PAYLOAD_KEY] = payloads
        bot._store.set_platform_state("discord", state, persist=False)
    return payloads


def store_resume_payload(bot: ClaudeBot, prompt: str) -> str:
    """Persist a resume prompt and return a short opaque token."""
    token = secrets.token_urlsafe(8)
    payloads = _get_payloads(bot)
    payloads[token] = prompt
    bot._store.save()
    return token


def pop_resume_payload(bot: ClaudeBot, token: str) -> str | None:
    """Look up and consume a resume prompt by token."""
    payloads = _get_payloads(bot)
    prompt = payloads.pop(token, None)
    if prompt is not None:
        bot._store.save()
    return prompt


# --- Derived child + wave state ---------------------------------------------


def _parent_is_alive(bot: ClaudeBot, parent_thread_id: str) -> bool:
    """True iff the parent thread is still present and usable.

    Treats archived/locked threads as not-alive — we never want to ping a
    thread the user has wrapped up.
    """
    lookup = bot._forums.thread_to_project(parent_thread_id)
    if lookup is None:
        return False
    try:
        ch = bot.get_channel(int(parent_thread_id))
    except Exception:
        return False
    if ch is None:
        return False
    if isinstance(ch, discord.Thread):
        if ch.archived or ch.locked:
            return False
    return True


def _latest_instance_for_session(bot: ClaudeBot, session_id: str) -> Instance | None:
    """Newest instance belonging to a session, or None.

    Scanning by session_id is how the rest of the codebase resolves
    thread → instance (tags.py, forums.py, eval.py) — there is no index.
    """
    best: Instance | None = None
    for inst in bot._store.list_instances(all_=True):
        if inst.session_id and inst.session_id == session_id:
            if best is None or (inst.created_at or "") > (best.created_at or ""):
                best = inst
    return best


def _child_state(bot: ClaudeBot, child_thread_id: str) -> ChildState:
    """Recompute one child's state from thread + instance records."""
    lookup = bot._forums.thread_to_project(child_thread_id)
    if lookup is None:
        return ChildState(child_thread_id, "gone", f"<#{child_thread_id}>", None)
    _, info = lookup
    title = (info.topic or "").strip() or f"<#{child_thread_id}>"
    if len(title) > 60:
        title = title[:60].rstrip() + "…"

    if not info.session_id:
        # No session recorded yet: either the first turn hasn't produced one,
        # or the child died before it could. Either way it hasn't reported.
        return ChildState(child_thread_id, "pending", title, None)

    inst = _latest_instance_for_session(bot, info.session_id)
    if inst is None:
        return ChildState(child_thread_id, "pending", title, None)
    if inst.status not in _TERMINAL:
        return ChildState(child_thread_id, "running", title, inst)
    if inst.needs_input:
        # finalize_run flips status to COMPLETED whenever the model paused with
        # a question, so a status-only read would call this child done while it
        # is actually waiting for an answer.
        return ChildState(child_thread_id, "blocked", title, inst)
    if inst.status == InstanceStatus.FAILED:
        return ChildState(child_thread_id, "failed", title, inst)
    if inst.status == InstanceStatus.KILLED:
        return ChildState(child_thread_id, "killed", title, inst)
    return ChildState(child_thread_id, "completed", title, inst)


def find_wave_instance(bot: ClaudeBot, parent_thread_id: str) -> Instance | None:
    """The parent instance owning the current, unreleased spawn wave.

    A parent thread accumulates one instance per turn; the wave belongs to the
    newest one that actually dispatched children. Returns None when that wave
    has already been released, so a late callback can't fire it twice.
    """
    lookup = bot._forums.thread_to_project(parent_thread_id)
    if lookup is None:
        return None
    _, info = lookup
    if not info.session_id:
        return None
    best: Instance | None = None
    for inst in bot._store.list_instances(all_=True):
        if inst.session_id != info.session_id:
            continue
        if not inst.spawn_dispatched_thread_ids:
            continue
        if best is None or (inst.created_at or "") > (best.created_at or ""):
            best = inst
    if best is None or best.spawn_wave_released:
        return None
    if not best.spawn_wave_sealed:
        # The dispatch loop is still handing out children — the roster is
        # incomplete, so joining on it now would drop the siblings that have
        # not been created yet. The seal callback re-checks the moment the
        # loop finishes.
        return None
    return best


def _wave_deadline_passed(inst: Instance) -> bool:
    """True once a wave has been open longer than ORCH_WAVE_TIMEOUT_MIN."""
    stamp = inst.finished_at or inst.created_at
    if not stamp:
        return False
    try:
        started = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=config.ORCH_WAVE_TIMEOUT_MIN)
    return started < cutoff


# --- Rendering --------------------------------------------------------------

_STATE_LABEL = {
    "completed": "COMPLETED",
    "failed": "FAILED",
    "killed": "KILLED (cancelled)",
    "blocked": "WAITING ON A QUESTION",
    "running": "still running",
    "pending": "never started",
    "gone": "thread gone",
}


def _excerpt_of(child: ChildState) -> str:
    """First few hundred characters of a child's report, or "" if none."""
    if child.inst is None:
        return ""
    text = (child.inst.read_result_text() or "").strip()
    if not text:
        text = (child.inst.summary or "").strip()
    if not text:
        return ""
    if len(text) > _EXCERPT:
        text = text[:_EXCERPT].rstrip() + "…"
    return text


def _report_path(child: ChildState) -> str | None:
    """Absolute path to the child's full report file, if it still exists."""
    if child.inst is None or not child.inst.result_file:
        return None
    try:
        p = Path(child.inst.result_file)
        return str(p) if p.exists() else None
    except Exception:
        return None


def _render_wave(children: list[ChildState], *, partial: bool) -> tuple[str, str]:
    """Build (human-facing post, parent resume prompt) for a closing wave.

    The human copy stays short — statuses plus a short excerpt each. The
    parent's copy carries the same statuses but points at the full report
    FILES, which it can open itself. That asymmetry is the whole point: the
    parent is no longer limited to what fits in a chat message.
    """
    done = sum(1 for c in children if c.settled)
    total = len(children)
    head = (
        f"Spawn wave released early — {done}/{total} children back "
        f"(waited {config.ORCH_WAVE_TIMEOUT_MIN}m)"
        if partial
        else f"Spawn wave complete — {total}/{total} children back"
    )

    post_lines = [f"**{head}**"]
    prompt_lines = [
        f"{head}.",
        "",
        "Children you dispatched, and where their full reports are:",
    ]
    for c in children:
        label = _STATE_LABEL.get(c.state, c.state)
        post_lines.append("")
        post_lines.append(f"**{c.title}** — {label} · <#{c.thread_id}>")
        prompt_lines.append("")
        prompt_lines.append(f'- "{c.title}" — {label} (thread <#{c.thread_id}>)')

        path = _report_path(c)
        if path:
            prompt_lines.append(f"  Full report: {path}")
        excerpt = _excerpt_of(c)
        if excerpt:
            post_lines.append(excerpt)
            prompt_lines.append(f"  Opens with: {excerpt}")
        elif c.state in ("pending", "running", "gone"):
            prompt_lines.append("  No report — this child never reported back.")

    prompt_lines += [
        "",
        "Read the report files above with your file tools before drawing any "
        "conclusion — the excerpts are openings, not summaries. Then continue "
        "the work you spawned these children for.",
    ]
    if partial:
        prompt_lines.append(
            "This wave was released before every child returned. Anything "
            "marked still running / never started contributed nothing, so say "
            "plainly what is missing rather than filling the gap by guessing.",
        )
    return "\n".join(post_lines), "\n".join(prompt_lines)


def _render_blocked(child: ChildState) -> tuple[str, str]:
    """Build (post, resume prompt) for a child parked on a question."""
    excerpt = _excerpt_of(child)
    post_lines = [
        f'**Child "{child.title}" is waiting on an answer** · <#{child.thread_id}>',
    ]
    if excerpt:
        post_lines += ["", excerpt]

    prompt_lines = [
        f'The child session "{child.title}" you spawned has stopped and is '
        "waiting for an answer before it can continue.",
        f"Child thread id: {child.thread_id}",
    ]
    path = _report_path(child)
    if path:
        prompt_lines.append(f"Its full message: {path}")
    if excerpt:
        prompt_lines += ["", "It said:", excerpt]
    prompt_lines += [
        "",
        "You wrote this child's brief, so you are the one who can answer it. "
        "Read its full message, then reply to it directly with a "
        "[BOT_CMD: /reply thread=" + child.thread_id + "] directive and a "
        "~~~reply body — do NOT ask the user unless the answer genuinely "
        "depends on something only they know. Your wave stays open until this "
        "child finishes.",
    ]
    return "\n".join(post_lines), "\n".join(prompt_lines)


# --- Delivery ---------------------------------------------------------------


async def _deliver(
    bot: ClaudeBot,
    parent_id: str,
    post_body: str,
    resume_prompt: str,
    *,
    auto_resume: bool,
) -> None:
    """Post into the parent thread and either auto-resume it or offer a button."""
    buttons = None
    if not auto_resume:
        try:
            token = store_resume_payload(bot, resume_prompt)
            buttons = [[ButtonSpec(label="Resume parent", callback_data=f"orch_resume:{token}")]]
        except Exception:
            log.exception("failed to store orch resume payload")

    try:
        await bot.messenger.send_text(parent_id, post_body, buttons=buttons)
    except Exception:
        log.exception("failed to post orchestrator callback to parent %s", parent_id)

    if auto_resume:
        # Dispatched detached: _replay_to_thread awaits the parent's whole run,
        # and this is called from a child's finalize path (or the autonomy
        # loop's tick) — neither may block on it.
        async def _resume() -> None:
            try:
                await bot._replay_to_thread(
                    parent_id, resume_prompt, source="callback_resume",
                )
            except Exception:
                log.exception("orchestrator auto-resume failed for %s", parent_id)

        asyncio.create_task(_resume())


async def _notify_ark_undelivered(bot: ClaudeBot, parent_id: str, what: str) -> None:
    """Surface a callback we could not deliver, instead of only logging it.

    A parent thread archived mid-wave used to swallow its children's reports
    with nothing but a log line — invisible from a phone.
    """
    ark_id = getattr(bot, "_lobby_channel_id", None)
    if not ark_id:
        return
    try:
        await bot.messenger.send_text(
            str(ark_id),
            f"Orchestrator: {what} could not be delivered to <#{parent_id}> — "
            "that thread is archived, locked or gone. Reopen it to pick the "
            "work back up.",
            silent=True,
        )
    except Exception:
        log.debug("Ark notice for undelivered orch callback failed", exc_info=True)


# --- Entry points -----------------------------------------------------------


async def release_wave(
    bot: ClaudeBot, parent_id: str, wave_inst: Instance, *, partial: bool,
) -> bool:
    """Close a wave: mark it released, post once, resume the parent.

    Returns True if the wave was released by this call. The released flag is
    written BEFORE the post so a concurrent finalize callback and the timeout
    sweep can't both deliver it.
    """
    if wave_inst.spawn_wave_released:
        return False
    wave_inst.spawn_wave_released = True
    bot._store.update_instance(wave_inst, critical=True)

    children = [
        _child_state(bot, tid) for tid in wave_inst.spawn_dispatched_thread_ids
    ]
    post_body, resume_prompt = _render_wave(children, partial=partial)

    if not _parent_is_alive(bot, parent_id):
        log.info("orchestrator wave release skipped — parent %s archived/missing", parent_id)
        await _notify_ark_undelivered(bot, parent_id, "a completed spawn wave")
        return True

    # A partial release is a judgment call about proceeding without a
    # straggler, so it always waits for a human tap.
    await _deliver(
        bot, parent_id, post_body, resume_prompt,
        auto_resume=config.ORCH_AUTO_RESUME and not partial,
    )
    return True


async def post_parent_callback(
    bot: ClaudeBot,
    child_thread_id: str,
    status: str,
    summary: str,
) -> None:
    """A child finalized — re-evaluate its parent's wave.

    Called from ``engine.lifecycle`` for every terminal child outcome,
    including one that parked on a question (``status == "BLOCKED"``). The
    status/summary arguments describe the child that just landed; everything
    the parent is told is re-derived, so a callback that never fires (crash,
    kill) only costs the wave its timeout, not its correctness.

    Errors are logged but never raised — child finalize must complete.
    """
    child_lookup = bot._forums.thread_to_project(child_thread_id)
    if child_lookup is None:
        return
    _, child_info = child_lookup
    parent_id = child_info.parent_thread_id
    if not parent_id:
        return

    # A blocked child never settles, so it can't close a wave. Report it on its
    # own so the parent can unblock it rather than the wave silently stalling
    # until the timeout.
    if status == "BLOCKED":
        child = _child_state(bot, child_thread_id)
        post_body, resume_prompt = _render_blocked(child)
        if not _parent_is_alive(bot, parent_id):
            log.info("orchestrator blocked-notice skipped — parent %s archived", parent_id)
            await _notify_ark_undelivered(bot, parent_id, "a blocked child session")
            return
        await _deliver(
            bot, parent_id, post_body, resume_prompt,
            auto_resume=config.ORCH_AUTO_RESUME,
        )
        return

    wave_inst = find_wave_instance(bot, parent_id)
    if wave_inst is None:
        # No open wave for this parent — either it was already released or the
        # dispatch record is gone. Nothing to join; stay quiet rather than
        # posting a second report.
        log.debug("no open spawn wave for parent %s; child %s finalize ignored",
                  parent_id, child_thread_id)
        return

    children = [_child_state(bot, tid) for tid in wave_inst.spawn_dispatched_thread_ids]
    outstanding = [c for c in children if not c.settled]
    if outstanding:
        # Progress line only — no button, no resume. The user still sees the
        # wave filling up; the parent isn't woken on partial information.
        done = len(children) - len(outstanding)
        waiting = ", ".join(f"{c.title} ({_STATE_LABEL.get(c.state, c.state)})"
                            for c in outstanding[:4])
        if len(outstanding) > 4:
            waiting += f", +{len(outstanding) - 4} more"
        try:
            await bot.messenger.send_text(
                parent_id,
                f"Spawn wave: {done}/{len(children)} children back. "
                f"Still waiting on {waiting}.",
                silent=True,
            )
        except Exception:
            log.debug("failed to post wave progress to %s", parent_id, exc_info=True)
        return

    await release_wave(bot, parent_id, wave_inst, partial=False)


async def evaluate_wave_now(bot: ClaudeBot, parent_thread_id: str) -> None:
    """Re-check a freshly sealed wave and close it if every child is already in.

    Called when the /spawn dispatch loop finishes. The normal trigger is the
    last child's finalize callback, but a wave whose children all finished
    while the loop was still dispatching would have had those callbacks arrive
    against an unsealed (not-yet-joinable) wave. Without this the wave would
    hang until the timeout sweep. Silent when children are still outstanding —
    nothing has landed, so there is no progress to report.
    """
    wave_inst = find_wave_instance(bot, parent_thread_id)
    if wave_inst is None:
        return
    children = [_child_state(bot, tid) for tid in wave_inst.spawn_dispatched_thread_ids]
    if any(not c.settled for c in children):
        return
    await release_wave(bot, parent_thread_id, wave_inst, partial=False)


async def sweep_stale_waves(bot: ClaudeBot) -> int:
    """Release waves whose children never all came back. Returns count released.

    Driven by the autonomy loop's tick. Without it, a child that is killed
    during a reboot — or that dies before recording a session — would hold its
    parent open forever, which is strictly worse than the per-child callbacks
    this replaced.
    """
    released = 0
    # Unsealed waves are included on purpose: a bot that died mid-dispatch
    # leaves a roster that will never be sealed, and skipping those here would
    # strand the parent permanently instead of merely late.
    for inst in bot._store.list_instances(all_=True):
        if not inst.spawn_dispatched_thread_ids or inst.spawn_wave_released:
            continue
        if not _wave_deadline_passed(inst):
            continue
        parent_id = _thread_for_instance(bot, inst)
        if parent_id is None:
            # Can't locate the thread that owns this wave — mark it released so
            # the scan doesn't re-examine it on every tick forever.
            inst.spawn_wave_released = True
            bot._store.update_instance(inst, critical=True)
            continue
        children = [_child_state(bot, tid) for tid in inst.spawn_dispatched_thread_ids]
        partial = any(not c.settled for c in children)
        try:
            if await release_wave(bot, parent_id, inst, partial=partial):
                released += 1
        except Exception:
            log.exception("stale wave release failed for instance %s", inst.id)
    return released


def _thread_for_instance(bot: ClaudeBot, inst: Instance) -> str | None:
    """Reverse-resolve which forum thread an instance ran in, via its session."""
    if not inst.session_id:
        return None
    for proj in bot._forums.forum_projects.values():
        for tid, info in proj.threads.items():
            if info.session_id == inst.session_id:
                return tid
    return None
