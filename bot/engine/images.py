"""Outbound image sharing — the [BOT_CMD: /image] directive.

Inbound images already worked: a photo attached in Discord is saved to
``data/pending_images/`` and its path handed to the session. This module is the
missing reverse direction — a turn can push a picture back into the thread
(a diagram checked into the repo, a screenshot taken while running the app, a
chart generated during a build).

Same control channel as /spawn, /chain and /wake: a directive line in the
turn's output, parsed post-turn. Deliberately dispatched BEFORE the result
embed is sent (lifecycle.run_instance / commands.on_query) so the workflow
buttons stay the last thing in the thread — on a phone that's where the thumb
expects them.

Path safety is the whole risk surface here: an unchecked path turns "share a
picture" into "read any file on the host and publish it to Discord". So a
candidate must resolve — after ``..`` collapsing and symlink resolution — to a
real file under one of this run's own roots (its worktree, its repo, or the
bot's data dir), carry an image extension, and fit the per-file size cap.
Anything else is refused with a visible notice: a message promising a diagram
with no diagram attached is worse than no feature at all.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bot import config
from bot.claude.gitpaths import git_toplevel
from bot.claude.sandbox import instance_confined
from bot.procutil import run_capture
from bot.textutil import DIRECTIVE_ARGS, DIRECTIVE_KV_RE, directive_kv, is_quoted_example, mask_tilde_bodies

if TYPE_CHECKING:
    from bot.claude.types import Instance
    from bot.platform.base import RequestContext

log = logging.getLogger(__name__)

# Extensions Discord renders inline. SVG is deliberately absent — Discord
# serves it as a download, so it would arrive as a file, not a picture.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Discord's per-message upload cap is 10 MiB on an unboosted guild. Stay under
# it per file AND per message. A turn's images are packed into as many
# messages as it takes, so the per-message budget only decides where one
# message ends and the next begins: it is never a reason to drop an image.
# (It used to be: everything went up as ONE message and whatever did not fit
# under 9 MB was refused with "batch upload limit reached", which a turn
# sharing a few GIFs hit every time.)
MAX_IMAGE_BYTES = 8_000_000
MAX_MESSAGE_BYTES = 9_000_000
MAX_FILES_PER_MESSAGE = 10  # Discord's attachment cap per message
MAX_IMAGES_PER_RESPONSE = 10
_MAX_CAPTION_CHARS = 2000   # Discord's message content limit

# Directive must own its whole line — same anchoring as the display collapser
# in platform.formatting, so the set of directives that ACT is exactly the set
# that renders as handled. A mid-sentence match is left alone.
_IMAGE_DIRECTIVE_RE = re.compile(
    rf"(?m)^[ \t]*\[BOT_CMD:\s*/image({DIRECTIVE_ARGS}*)\][ \t]*$"
)


def parse_image_directives(text: str) -> list[tuple[str, str | None]]:
    """Extract ``(raw_path, caption)`` pairs from a turn's output.

    Accepts both ``path="x.png" caption="…"`` and the bare ``[BOT_CMD:
    /image x.png]`` shorthand. Order of appearance is preserved; no cap is
    applied here (the caller reports what it drops).
    """
    if not text or "[BOT_CMD:" not in text:
        return []
    out: list[tuple[str, str | None]] = []
    # A /image written into another directive's tilde body (a /spawn brief)
    # is the other session's to post.
    text = mask_tilde_bodies(text)
    for m in _IMAGE_DIRECTIVE_RE.finditer(text):
        if is_quoted_example(text, m.start()):
            log.debug("/image skipped: a quoted example")
            continue
        args = m.group(1) or ""
        kv = directive_kv(args)
        raw_path = kv.get("path", "").strip()
        if not raw_path:
            # Bare form: everything that isn't a key=value pair is the path.
            raw_path = DIRECTIVE_KV_RE.sub("", args).strip().strip("\"'")
        if not raw_path:
            log.warning("/image directive has no path: %r", args)
            continue
        caption = kv.get("caption") or None
        out.append((raw_path, caption))
    return out


def _allowed_roots(inst: "Instance", sandboxed: bool = False) -> list[Path]:
    """Directories this run is allowed to publish a file from.

    Deliberately NOT the system temp dir. That was the first cut, and the test
    harness immediately showed why it's wrong: temp holds every process's
    scratch files, so allowing it both leaks other programs' images and (on a
    machine where the repo itself sits under temp) silently defeats the
    ``..``-escape check. A session that generates a picture writes it into its
    own worktree or ``data/`` instead.

    A sandboxed session gets its own working directory only. The bot's data dir
    holds images from every other thread.
    """
    if sandboxed:
        raw = [getattr(inst, "worktree_path", None) or getattr(inst, "repo_path", None)]
    else:
        raw = [
            getattr(inst, "worktree_path", None),
            getattr(inst, "repo_path", None),
            str(config.DATA_DIR),
        ]
    roots: list[Path] = []
    for p in raw:
        if not p:
            continue
        try:
            roots.append(Path(p).resolve())
        except OSError:
            continue
    return roots


def resolve_image(
    raw: str, inst: "Instance", *, sandboxed: bool = False,
) -> tuple[Path | None, str]:
    """Validate one directive path. Returns ``(path, reason_if_rejected)``.

    Relative paths resolve against the run's own working root — the worktree
    for a build, the repo otherwise — so a build can share a file it just
    created without knowing its absolute location. Outside a build, a miss
    also checks the repo's other git worktrees.
    """
    try:
        cand = Path(raw.strip().strip("\"'")).expanduser()
    except (OSError, ValueError):
        return None, "unreadable path"
    missing = "file not found"

    if not cand.is_absolute():
        # Worktree first (a build's own files win), then the main repo — a
        # build sharing a diagram that's checked in but not touched by this
        # branch shouldn't have to know it's running in a worktree.
        bases = [
            b for b in (
                getattr(inst, "worktree_path", None),
                getattr(inst, "repo_path", None),
            ) if b
        ]
        if not bases:
            return None, "no repo root to resolve against"
        rel = cand
        cand = Path(bases[0]) / rel
        for base in bases[1:]:
            if cand.is_file():
                break
            cand = Path(base) / rel
        if (
            not cand.is_file()
            and cand.suffix.lower() in IMAGE_EXTS
            and not getattr(inst, "worktree_path", None)
            and not sandboxed
        ):
            # A session working in the repo may have made its own git worktree
            # (or cd'd into one) and written the path relative to THAT tree.
            # Not for a bot build: its own worktree is where its relative
            # paths live, and a sibling build's same-named file is not its
            # result.
            other = _find_in_other_worktrees(rel, inst)
            if other is not None:
                cand = other
            else:
                missing = (
                    "file not found in the repo or its worktrees, "
                    "give the absolute path"
                )

    try:
        # resolve() collapses `..` and follows symlinks — both checks below
        # are meaningless without it.
        cand = cand.resolve()
    except OSError:
        return None, "unreadable path"

    roots = _allowed_roots(inst, sandboxed)
    if not any(_under(cand, r) for r in roots):
        if sandboxed:
            return None, "outside this session's working directory"
        return None, "outside this repo, the worktree, and the bot's data dir"
    if cand.suffix.lower() not in IMAGE_EXTS:
        return None, f"not a shareable image type ({cand.suffix or 'no extension'})"
    if not cand.is_file():
        return None, missing
    try:
        size = cand.stat().st_size
    except OSError:
        return None, "file not readable"
    if size == 0:
        return None, "file is empty"
    if size > MAX_IMAGE_BYTES:
        return None, f"too large ({size / 1_000_000:.1f} MB, limit 8 MB)"
    return cand, ""


@dataclass(frozen=True)
class ImageFailure:
    """One image a turn asked to share that did not reach the thread.

    ``path`` is what the turn wrote (so the session can find the file again),
    ``reason`` is why it was not posted.
    """
    path: str
    reason: str

    @property
    def name(self) -> str:
        return Path(self.path).name or self.path


@dataclass
class ImageDelivery:
    """What happened to a turn's /image directives."""
    sent: int = 0
    failures: list[ImageFailure] = field(default_factory=list)


@dataclass(frozen=True)
class Upload:
    """One accepted image, ready to post.

    ``raw`` is the path as the turn wrote it, kept so a failure is reported in
    the session's own terms; ``path`` is the resolved file that gets uploaded.
    """
    raw: str
    path: str
    size: int
    caption: str | None = None


def pack_messages(items: list[Upload]) -> list[list[Upload]]:
    """Split accepted uploads into per-message groups.

    Order is kept. A group closes when the next file would push it past
    ``MAX_MESSAGE_BYTES`` or ``MAX_FILES_PER_MESSAGE``. Every item fits a group
    on its own because ``resolve_image`` already refused anything over
    ``MAX_IMAGE_BYTES``.
    """
    groups: list[list[Upload]] = []
    current: list[Upload] = []
    current_bytes = 0
    for item in items:
        if current and (
            current_bytes + item.size > MAX_MESSAGE_BYTES
            or len(current) >= MAX_FILES_PER_MESSAGE
        ):
            groups.append(current)
            current, current_bytes = [], 0
        current.append(item)
        current_bytes += item.size
    if current:
        groups.append(current)
    return groups


def failure_note(failures: list[ImageFailure]) -> str:
    """The message that tells the session its images did not post.

    The turn has already ended by the time an upload fails, and its reply
    usually says the pictures are there. Without this the session never learns
    otherwise: the user sees a small "couldn't share" line, the session sees
    nothing, and the next turn carries on as if they were shared. Handed to
    ``lifecycle.check_wake_request``, which re-invokes the session with it (or
    folds it into whatever already resumes the thread).
    """
    lines = "\n".join(f"- `{f.path}`: {f.reason}" for f in failures)
    return (
        "SYSTEM NOTICE: these images from your previous reply did NOT reach "
        "the user. The user only saw a short \"couldn't share\" line for each:\n"
        f"{lines}\n"
        "If your reply said they were shared, it was wrong. Fix what you can "
        "and share those images again with [BOT_CMD: /image] directives in "
        f"this reply: a file over {MAX_IMAGE_BYTES // 1_000_000} MB can be "
        "shrunk or re-encoded (fewer frames, smaller dimensions, more "
        "compression), a file outside the repo can be copied into it, and an "
        f"image past the {MAX_IMAGES_PER_RESPONSE}-per-reply limit can simply "
        "be sent now. The bot splits a reply's images across several messages "
        "on its own, so do not hold any back for size. For an image that "
        "cannot be fixed, tell the user plainly which one and why. Do not "
        "redo any other work."
    )


async def deliver_images(
    ctx: "RequestContext", result_text: str, inst: "Instance",
) -> ImageDelivery:
    """Post every valid /image directive in ``result_text``.

    Images are packed into as many messages as their sizes need. Returns how
    many were posted and which were not, with why; the caller hands the
    failures to ``check_wake_request`` so the session finds out.

    Never raises into the caller: a Discord upload failure must not lose the
    result embed that follows it.
    """
    outcome = ImageDelivery()
    try:
        directives = parse_image_directives(result_text)
    except Exception:
        log.exception("/image — directive parsing failed")
        return outcome
    if not directives:
        return outcome

    over_cap = directives[MAX_IMAGES_PER_RESPONSE:]
    directives = directives[:MAX_IMAGES_PER_RESPONSE]

    accepted: list[Upload] = []
    failures = outcome.failures
    sandboxed = instance_confined(ctx.store, inst, ctx=ctx)
    for raw, caption in directives:
        # Off the event loop: the worktree fallback shells out to git.
        resolved, reason = await asyncio.to_thread(
            resolve_image, raw, inst, sandboxed=sandboxed,
        )
        if resolved is None:
            log.warning("/image refused %r — %s", raw, reason)
            failures.append(ImageFailure(raw, reason))
            continue
        try:
            size = resolved.stat().st_size
        except OSError:
            failures.append(ImageFailure(raw, "file not readable"))
            continue
        accepted.append(Upload(raw, str(resolved), size, caption))

    for raw, _caption in over_cap:
        failures.append(ImageFailure(
            raw,
            f"over the {MAX_IMAGES_PER_RESPONSE}-images-per-reply limit, "
            "not sent",
        ))

    for group in pack_messages(accepted):
        captions = [u.caption for u in group if u.caption]
        caption = " · ".join(captions)[:_MAX_CAPTION_CHARS] if captions else None
        try:
            ids = await ctx.messenger.send_files(
                ctx.channel_id, [u.path for u in group], caption=caption,
            )
        except Exception as exc:
            log.exception("/image: upload failed")
            reason = f"Discord upload failed ({type(exc).__name__})"
            failures.extend(ImageFailure(u.raw, reason) for u in group)
            continue
        if not ids:
            # The adapter returns no message id when the channel could not be
            # resolved: nothing was posted, and saying otherwise is the bug.
            log.warning("/image: upload to %s returned no message", ctx.channel_id)
            failures.extend(
                ImageFailure(u.raw, "Discord upload posted nothing") for u in group
            )
            continue
        outcome.sent += len(group)
        log.info("/image: delivered %d image(s) to %s", len(group), ctx.channel_id)

    if failures:
        # Visible, quiet: without this the turn's prose promises a picture that
        # never shows up and the user has no idea why. The session is told
        # separately, by check_wake_request.
        try:
            lines = "\n".join(
                f"-# couldn't share `{f.name}`: {f.reason}" for f in failures
            )
            await ctx.messenger.send_text(ctx.channel_id, lines, silent=True)
        except Exception:
            log.debug("/image — failed to post refusal notice", exc_info=True)

    return outcome


def _find_in_other_worktrees(rel: Path, inst: "Instance") -> Path | None:
    """Find a relative path in one of the repo's other git worktrees.

    Sessions in a direct thread often create their own worktree (under the
    repo's ``.worktrees/``) and render images there, then hand back a path
    relative to that tree. Each worktree is tried at the repo's offset inside
    its git root (a repo registered as a subfolder of its git root has the
    same subfolder in every worktree) and at the worktree root. When several
    match, the most recently written one wins: it is the image the turn just
    made.

    Only paths inside this run's allowed roots count, so this widens where a
    relative path is looked for, never what may be published.
    """
    repo = getattr(inst, "repo_path", None)
    if not repo:
        return None
    top = git_toplevel(repo)
    if not top:
        return None
    try:
        offset = Path(repo).resolve().relative_to(Path(top).resolve())
    except (OSError, ValueError):
        offset = Path()
    try:
        r = run_capture(
            ["git", "-C", repo, "worktree", "list", "--porcelain"],
            timeout=10, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        log.debug("/image: git worktree list failed in %s", repo, exc_info=True)
        return None
    if r.returncode != 0:
        return None

    roots = _allowed_roots(inst)
    best: Path | None = None
    best_mtime = -1.0
    for line in (r.stdout or "").splitlines():
        if not line.startswith("worktree "):
            continue
        tree = Path(line[len("worktree "):])
        for base in dict.fromkeys((tree / offset, tree)):
            try:
                cand = (base / rel).resolve()
                if not any(_under(cand, root) for root in roots) or not cand.is_file():
                    continue
                mtime = cand.stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best, best_mtime = cand, mtime
    if best is not None:
        log.info("/image: found %s in another worktree (%s)", rel, best)
    return best


def _under(path: Path, root: Path) -> bool:
    """True if *path* sits inside *root* (both already resolved)."""
    try:
        return path == root or path.is_relative_to(root)
    except (ValueError, OSError):
        return False
