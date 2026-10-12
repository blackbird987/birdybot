"""The post-turn [BOT_CMD: ...] scan: routes /repo, /chain, /spawn and /reply to their handlers."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from bot.textutil import directive_re, is_quoted_example, mask_tilde_bodies
from bot.claude.types import Instance
from bot.platform.base import RequestContext
from bot.engine.directives.chain import _handle_chain_directive
from bot.engine.directives.reply import _handle_reply_directives
from bot.engine.directives.spawn import (
    _MAX_SPAWNS_PER_RESPONSE,
    _handle_spawn_directive,
    _pair_spawn_directives,
    _resolve_parent_instance,
)
from bot.engine.repo_cmds import on_repo

log = logging.getLogger(__name__)


# --- Natural language repo detection (Tier 2 — Claude-assisted BOT_CMD) ---

_BOT_CMD_RE = directive_re("repo")
_ALLOWED_BOT_CMD_ACTIONS = {"add", "create", "switch"}
_DANGEROUS_PATH_CHARS = re.compile(r'[;&|`$(){}!<>]')


async def _execute_bot_commands(
    ctx: RequestContext, result_text: str,
    source_inst: "Instance | None" = None,
) -> None:
    """Scan final assistant output for [BOT_CMD: /repo], /spawn, /chain directives.

    ``source_inst`` is the instance that produced ``result_text`` — required
    for the /chain directive (it launches a chain rooted on that instance's
    session). None disables /chain handling (callers with no instance context).
    """
    if not result_text:
        return
    # Scan with every tilde body blanked: a /repo directive written into a
    # /spawn brief or a ~~~plan is the other session's to run, not ours.
    scan = mask_tilde_bodies(result_text)
    for m in _BOT_CMD_RE.finditer(scan):
        if is_quoted_example(scan, m.start()):
            log.debug("BOT_CMD /repo skipped: a quoted example")
            continue

        repo_args = m.group(1).strip()
        action = repo_args.split()[0] if repo_args else ""
        if action not in _ALLOWED_BOT_CMD_ACTIONS:
            log.warning("BOT_CMD blocked — disallowed action: %s", action)
            continue
        if _DANGEROUS_PATH_CHARS.search(repo_args):
            log.warning("BOT_CMD blocked — dangerous characters: %s", repo_args)
            continue
        # For add commands, validate the path exists
        if action == "add":
            parts = repo_args.split(None, 2)  # "add <name> <path>"
            if len(parts) >= 3:
                candidate = Path(parts[2].strip("\"'"))
                if not candidate.is_dir():
                    log.warning("BOT_CMD blocked — path not found: %s", parts[2])
                    continue
        try:
            await ctx.messenger.send_text(
                ctx.channel_id, f"Auto-executing: `/repo {repo_args}`",
            )
            await on_repo(ctx, repo_args)
        except Exception:
            log.warning("Failed to execute bot command: /repo %s", repo_args)

    # /spawn — up to _MAX_SPAWNS_PER_RESPONSE directives per response run, in
    # order of appearance. Each directive is paired with the ~~~spawn body that
    # sits between it and the next directive, so bodies can't be shared or
    # stolen across directives. Directives are dispatched sequentially (thread
    # creation + state writes stay race-free); the child sessions themselves
    # run in parallel once created.
    pairs, no_body, over_cap = _pair_spawn_directives(result_text)
    if no_body:
        log.warning(
            "BOT_CMD /spawn — %d directive(s) had no adjacent ~~~spawn body",
            no_body,
        )
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"{no_body} /spawn directive(s) ignored — each directive "
                "must be immediately followed by its own `~~~spawn ... ~~~` "
                "body block.",
                silent=True,
            )
        except Exception:
            pass
    if over_cap:
        log.warning(
            "BOT_CMD /spawn — %d directive(s) beyond the per-response cap "
            "(%d) ignored", over_cap, _MAX_SPAWNS_PER_RESPONSE,
        )
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"Only the first {_MAX_SPAWNS_PER_RESPONSE} /spawn "
                f"directives per response run — {over_cap} ignored.",
                silent=True,
            )
        except Exception:
            pass
    for args_str, body in pairs:
        try:
            keep_going = await _handle_spawn_directive(ctx, args_str, body)
        except Exception:
            log.exception("Failed to dispatch /spawn directive")
            continue
        if not keep_going:
            # Thread-level gate (autopilot/depth/wave-cap/budget) — it would
            # reject every remaining directive with the same notice, so stop
            # after the first one instead of spamming duplicates.
            log.info(
                "BOT_CMD /spawn — thread-level gate rejected; skipping "
                "remaining directives in this response",
            )
            break

    # Seal the wave: the dispatch loop is done handing out children, so the
    # roster on the parent instance is now final and the join may act on it.
    # Until this point a child that failed instantly could have finalized
    # against a half-written roster and closed the wave without its siblings.
    #
    # Sealing also has to RE-CHECK the wave, because if every child finished
    # while this loop was still running, their finalize callbacks already came
    # and went against an unsealed wave — nothing else would be left to close
    # it before the timeout sweep noticed 45 minutes later.
    if pairs:
        sealed_inst = _resolve_parent_instance(ctx)
        if sealed_inst is not None and sealed_inst.spawn_dispatched_thread_ids:
            sealed_inst.spawn_wave_sealed = True
            ctx.store.update_instance(sealed_inst, critical=True)
            if ctx.on_spawn_wave_sealed is not None:
                try:
                    await ctx.on_spawn_wave_sealed()
                except Exception:
                    log.exception("on_spawn_wave_sealed failed")

    # /reply — answer a child session this thread spawned. Sits after /spawn so
    # a response that both replies to one child and spawns another keeps its
    # written order.
    await _handle_reply_directives(ctx, result_text)

    # /chain — assistant-issued handoff into the build→ship chain, carrying a
    # plan the agent wrote from the conversation. Only one per response.
    if source_inst is not None:
        await _handle_chain_directive(ctx, result_text, source_inst)
