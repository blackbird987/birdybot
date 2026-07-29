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
bot's data dir), carry an image extension, and fit the upload budget. Anything else is refused with a visible notice: a message
promising a diagram with no diagram attached is worse than no feature at all.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from bot import config

if TYPE_CHECKING:
    from bot.claude.types import Instance
    from bot.platform.base import RequestContext

log = logging.getLogger(__name__)

# Extensions Discord renders inline. SVG is deliberately absent — Discord
# serves it as a download, so it would arrive as a file, not a picture.
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Discord's per-message upload cap is 10 MiB on an unboosted guild. Stay under
# it per file AND per batch, since a batch posts as one message.
MAX_IMAGE_BYTES = 8_000_000
MAX_BATCH_BYTES = 9_000_000
MAX_IMAGES_PER_RESPONSE = 4

# Directive must own its whole line — same anchoring as the display collapser
# in platform.formatting, so the set of directives that ACT is exactly the set
# that renders as handled. A mid-sentence match is left alone.
_IMAGE_DIRECTIVE_RE = re.compile(
    r"(?m)^[ \t]*\[BOT_CMD:\s*/image([^\]\n]*)\][ \t]*$"
)
# key=value, bare or quoted — mirrors the /spawn and display parsers.
_KV_RE = re.compile(r'''(\w+)=(?:"([^"]*)"|'([^']*)'|(\S+))''')
# Quoted/code/heading lines never dispatch — a directive shown as an example
# must stay an example (parity with commands._QUOTED_LINE_PREFIX).
_QUOTED_LINE_PREFIX = re.compile(r'^\s*(?:>|`|```|#{1,3}\s)')


def parse_image_directives(text: str) -> list[tuple[str, str | None]]:
    """Extract ``(raw_path, caption)`` pairs from a turn's output.

    Accepts both ``path="x.png" caption="…"`` and the bare ``[BOT_CMD:
    /image x.png]`` shorthand. Order of appearance is preserved; no cap is
    applied here (the caller reports what it drops).
    """
    if not text or "[BOT_CMD:" not in text:
        return []
    out: list[tuple[str, str | None]] = []
    for m in _IMAGE_DIRECTIVE_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        if _QUOTED_LINE_PREFIX.match(text[line_start:m.start()]):
            log.debug("/image skipped — inside quoted content")
            continue
        args = m.group(1) or ""
        kv = {
            k: (d or s or b or "")
            for k, d, s, b in _KV_RE.findall(args)
        }
        raw_path = kv.get("path", "").strip()
        if not raw_path:
            # Bare form: everything that isn't a key=value pair is the path.
            raw_path = _KV_RE.sub("", args).strip().strip("\"'")
        if not raw_path:
            log.warning("/image directive has no path: %r", args)
            continue
        caption = kv.get("caption") or None
        out.append((raw_path, caption))
    return out


def _allowed_roots(inst: "Instance") -> list[Path]:
    """Directories this run is allowed to publish a file from.

    Deliberately NOT the system temp dir. That was the first cut, and the test
    harness immediately showed why it's wrong: temp holds every process's
    scratch files, so allowing it both leaks other programs' images and (on a
    machine where the repo itself sits under temp) silently defeats the
    ``..``-escape check. A session that generates a picture writes it into its
    own worktree or ``data/`` instead.
    """
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


def resolve_image(raw: str, inst: "Instance") -> tuple[Path | None, str]:
    """Validate one directive path. Returns ``(path, reason_if_rejected)``.

    Relative paths resolve against the run's own working root — the worktree
    for a build, the repo otherwise — so a build can share a file it just
    created without knowing its absolute location.
    """
    try:
        cand = Path(raw.strip().strip("\"'")).expanduser()
    except (OSError, ValueError):
        return None, "unreadable path"

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

    try:
        # resolve() collapses `..` and follows symlinks — both checks below
        # are meaningless without it.
        cand = cand.resolve()
    except OSError:
        return None, "unreadable path"

    roots = _allowed_roots(inst)
    if not any(_under(cand, r) for r in roots):
        return None, "outside this repo, the worktree, and the bot's data dir"
    if cand.suffix.lower() not in IMAGE_EXTS:
        return None, f"not a shareable image type ({cand.suffix or 'no extension'})"
    if not cand.is_file():
        return None, "file not found"
    try:
        size = cand.stat().st_size
    except OSError:
        return None, "file not readable"
    if size == 0:
        return None, "file is empty"
    if size > MAX_IMAGE_BYTES:
        return None, f"too large ({size / 1_000_000:.1f} MB, limit 8 MB)"
    return cand, ""


async def deliver_images(
    ctx: "RequestContext", result_text: str, inst: "Instance",
) -> int:
    """Post every valid /image directive in ``result_text``. Returns the count.

    Never raises into the caller: a Discord upload failure must not lose the
    result embed that follows it.
    """
    try:
        directives = parse_image_directives(result_text)
    except Exception:
        log.exception("/image — directive parsing failed")
        return 0
    if not directives:
        return 0

    over_cap = max(0, len(directives) - MAX_IMAGES_PER_RESPONSE)
    directives = directives[:MAX_IMAGES_PER_RESPONSE]

    paths: list[str] = []
    captions: list[str] = []
    problems: list[str] = []
    batch_bytes = 0
    for raw, caption in directives:
        resolved, reason = resolve_image(raw, inst)
        if resolved is None:
            log.warning("/image refused %r — %s", raw, reason)
            problems.append(f"`{Path(raw).name or raw}` — {reason}")
            continue
        size = resolved.stat().st_size
        if batch_bytes + size > MAX_BATCH_BYTES:
            problems.append(f"`{resolved.name}` — batch upload limit reached")
            continue
        batch_bytes += size
        paths.append(str(resolved))
        if caption:
            captions.append(caption)

    if over_cap:
        problems.append(
            f"{over_cap} more image(s) skipped — "
            f"max {MAX_IMAGES_PER_RESPONSE} per response"
        )

    sent = 0
    if paths:
        caption = " · ".join(captions) if captions else None
        try:
            await ctx.messenger.send_files(ctx.channel_id, paths, caption=caption)
            sent = len(paths)
            log.info("/image — delivered %d image(s) to %s", sent, ctx.channel_id)
        except Exception:
            log.exception("/image — upload failed")
            problems.append(f"{len(paths)} image(s) — upload failed")

    if problems:
        # Visible, quiet: without this the turn's prose promises a picture that
        # never shows up and the user has no idea why.
        try:
            lines = "\n".join(f"-# couldn't share {p}" for p in problems)
            await ctx.messenger.send_text(ctx.channel_id, lines, silent=True)
        except Exception:
            log.debug("/image — failed to post refusal notice", exc_info=True)

    return sent


def _under(path: Path, root: Path) -> bool:
    """True if *path* sits inside *root* (both already resolved)."""
    try:
        return path == root or path.is_relative_to(root)
    except (ValueError, OSError):
        return False
