"""The /spawn directive: a session opening fresh forum threads with generated prompts."""

from __future__ import annotations

import logging
import re

from bot.textutil import directive_re, match_name
from bot.claude.types import Instance
from bot.platform.base import RequestContext, SpawnArgs
from bot.engine.cmd_state import check_budget
from bot.engine.directives.common import pair_directives, parse_kv

log = logging.getLogger(__name__)


# --- /spawn directive (Tier-2 BOT_CMD — assistant-issued session handoff) ---

_SPAWN_DIRECTIVE_RE = directive_re("spawn")
# The body is a tilde-fenced ~~~spawn ... ~~~ block, located by
# textutil.find_tilde_block so a brief that itself contains a ~~~wake or
# ~~~plan block for the child is not cut off at that block's closer. Tildes
# avoid colliding with the triple-backtick code fences inside the prompt body.
_SPAWN_ALLOWED_KEYS = {"repo", "title", "mode", "effort"}
_SPAWN_ALLOWED_MODES = {"explore", "plan", "build"}
_SPAWN_ALLOWED_EFFORTS = {"low", "medium", "high", "max"}
_SPAWN_PROMPT_MAX_BYTES = 32 * 1024  # 32 KiB hard cap on body
_SPAWN_TITLE_MAX = 80
_TITLE_CONTROL_CHARS = re.compile(r"[\x00-\x1f]")
# Per-response fan-out cap: max /spawn directives honored in one assistant
# response. Bounds a single confused turn without blocking legitimate
# parallel fan-out (the common case is 2-5 subsessions at once).
_MAX_SPAWNS_PER_RESPONSE = 5
# Orchestrator run cap: max child sessions a parent may spawn within a single
# orchestration run (across turns, surviving callback-resume replays). Reset on
# genuine user-typed messages. Sized so a full per-response wave of
# _MAX_SPAWNS_PER_RESPONSE still leaves room for follow-up waves after the
# children report back, while a runaway parent can't fan out unbounded.
_MAX_SPAWN_WAVES = 12


def _pair_spawn_directives(
    result_text: str,
) -> tuple[list[tuple[str, str]], int, int]:
    """Pair each /spawn directive with its own adjacent ~~~spawn body (see pair_directives)."""
    return pair_directives(
        result_text, _SPAWN_DIRECTIVE_RE, "spawn", _MAX_SPAWNS_PER_RESPONSE,
    )


def _resolve_parent_instance(ctx: RequestContext) -> "Instance | None":
    """The instance whose response is being scanned for directives.

    Directives are scanned inside ``run_instance``, before ``end_task``, so the
    run is still registered as active. Shared by the /spawn dispatcher (which
    appends to its child roster) and the wave seal (which closes that roster),
    so the two can never disagree about which instance owns the wave.
    """
    parent_iid = (
        ctx.runner.active_instance_for_session(ctx.session_id)
        or ctx.runner.active_instance_for_channel(ctx.channel_id)
    )
    return ctx.store.get_instance(parent_iid) if parent_iid else None


async def _handle_spawn_directive(
    ctx: RequestContext, args_str: str, body: str,
) -> bool:
    """Dispatch a [BOT_CMD: /spawn ...] directive in the parent thread.

    Validates inputs, gates on autopilot/recursion/budget, calls the
    platform-supplied ``ctx.spawn_session`` callback, and writes the
    parent → child audit marker on the parent instance.

    Returns True if later directives in the same response may still be
    dispatched (success, or a per-directive rejection like a bad repo).
    Returns False on a thread-level gate (missing platform seam, autopilot,
    depth cap, wave cap, budget) — those reject every remaining directive
    identically, so the caller stops instead of spamming duplicate notices.
    """
    # Platform must support the seam — engine never imports bot.discord.
    if ctx.spawn_session is None:
        log.warning("BOT_CMD /spawn — platform has no spawn_session callback; ignoring")
        return False

    # Block dispatch when the parent thread is mid-autopilot (running OR paused).
    # Spawning a child while the chain holds the wheel is confusing UX and risks
    # split-brain reviews on the same code.
    chain_meta = ctx.store.get_autopilot_chain_meta(ctx.session_id)
    if chain_meta and chain_meta.get("status") in ("running", "paused"):
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                "/spawn refused — autopilot is "
                f"{chain_meta.get('status')} on this thread. Stop or finish "
                "the chain before handing off a fresh session.",
                silent=True,
            )
        except Exception:
            pass
        return False

    async def _reject(log_msg: str, user_msg: str) -> None:
        log.warning("BOT_CMD /spawn blocked — %s", log_msg)
        try:
            await ctx.messenger.send_text(ctx.channel_id, user_msg, silent=True)
        except Exception:
            pass

    kv = parse_kv(args_str)
    if kv is None:
        await _reject(
            f"args failed to parse: {args_str}",
            "/spawn directive ignored — could not parse args. "
            "Expected `key=value` pairs, with values containing spaces "
            "wrapped in double quotes.",
        )
        return True
    extra = set(kv) - _SPAWN_ALLOWED_KEYS
    if extra:
        await _reject(
            f"unknown keys: {sorted(extra)}",
            f"/spawn directive ignored — unknown keys: "
            f"`{', '.join(sorted(extra))}`. Allowed: "
            f"`{', '.join(sorted(_SPAWN_ALLOWED_KEYS))}`.",
        )
        return True
    repo = kv.get("repo", "").strip()
    title = kv.get("title", "").strip()
    if not repo or not title:
        await _reject(
            "repo and title are required",
            "/spawn directive ignored — `repo` and `title` are both required.",
        )
        return True
    if _TITLE_CONTROL_CHARS.search(title):
        await _reject(
            "title contains control characters",
            "/spawn directive ignored — `title` cannot contain control "
            "characters or newlines.",
        )
        return True
    if len(title) > _SPAWN_TITLE_MAX:
        title = title[:_SPAWN_TITLE_MAX].rstrip()
    mode = kv.get("mode", "build").strip().lower()
    if mode not in _SPAWN_ALLOWED_MODES:
        await _reject(
            f"invalid mode: {mode}",
            f"/spawn directive ignored — invalid mode `{mode}`. "
            f"Allowed: `{', '.join(sorted(_SPAWN_ALLOWED_MODES))}`.",
        )
        return True
    effort = kv.get("effort")
    if effort is not None:
        effort = effort.strip().lower()
        if effort not in _SPAWN_ALLOWED_EFFORTS:
            await _reject(
                f"invalid effort: {effort}",
                f"/spawn directive ignored — invalid effort `{effort}`. "
                f"Allowed: `{', '.join(sorted(_SPAWN_ALLOWED_EFFORTS))}`.",
            )
            return True

    # Repo must be registered. Match case-insensitively and resolve to the
    # canonical registered name — registrations are lowercase by convention
    # but the model often emits CamelCase based on directory names.
    canonical = match_name(repo, ctx.store.list_repos())
    if canonical is None:
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"/spawn refused — repo `{repo}` is not registered. "
                "Use `/repo list` to see registered repos.",
                silent=True,
            )
        except Exception:
            pass
        return True
    repo = canonical

    # Body checks.
    if not body:
        await _reject(
            "empty prompt body",
            "/spawn directive ignored — the `~~~spawn ... ~~~` body "
            "block is empty.",
        )
        return True
    if len(body.encode("utf-8")) > _SPAWN_PROMPT_MAX_BYTES:
        log.warning(
            "BOT_CMD /spawn blocked — body exceeds %d bytes",
            _SPAWN_PROMPT_MAX_BYTES,
        )
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                "/spawn refused — prompt body exceeds the 32 KiB limit.",
                silent=True,
            )
        except Exception:
            pass
        return True

    # Resolve the parent instance for recursion cap + audit marker.
    parent_inst = _resolve_parent_instance(ctx)
    parent_depth = parent_inst.spawn_depth if parent_inst else 0

    # Recursion cap — depth 1 max. A spawned thread cannot itself spawn.
    if parent_depth >= 1:
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                "/spawn refused — this thread was itself created by a "
                "spawn directive, so it cannot spawn further. Switch to a "
                "top-level thread (one you opened directly) and try again.",
                silent=True,
            )
        except Exception:
            pass
        return False

    # Wave cap — bound the orchestrator loop. Counter lives on the parent
    # thread's ThreadInfo (turn-stable: survives callback-resume replays;
    # resets only when the user types a fresh message). Reads via the
    # platform-supplied accessor so the engine stays platform-agnostic.
    if ctx.read_spawn_wave_count is not None:
        try:
            wave_count = ctx.read_spawn_wave_count()
        except Exception:
            log.exception("read_spawn_wave_count failed; defaulting to 0")
            wave_count = 0
        if wave_count >= _MAX_SPAWN_WAVES:
            try:
                await ctx.messenger.send_text(
                    ctx.channel_id,
                    f"/spawn refused — orchestrator wave cap reached "
                    f"({_MAX_SPAWN_WAVES}). Send a fresh message in this "
                    "thread to start a new orchestration run.",
                    silent=True,
                )
            except Exception:
                pass
            return False

    # Budget gate.
    if not check_budget(ctx):
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                "/spawn refused — daily budget exhausted.",
                silent=True,
            )
        except Exception:
            pass
        return False

    spawn_args = SpawnArgs(
        repo=repo,
        title=title,
        prompt=body,
        mode=mode,
        effort=effort,
        parent_depth=parent_depth,
    )
    try:
        result = await ctx.spawn_session(spawn_args)
    except Exception:
        log.exception("BOT_CMD /spawn — platform callback raised")
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                "/spawn failed — platform could not create the new session.",
                silent=True,
            )
        except Exception:
            pass
        # Per-directive: one failed thread creation (e.g. a transient
        # Discord error) shouldn't abandon the rest of the wave.
        return True

    # Audit marker on the parent instance — write-only; surfaces the
    # parent → child links in state.json for post-hoc inspection.
    if parent_inst:
        parent_inst.spawn_dispatched_thread_ids.append(result.thread_id)
        ctx.store.update_instance(parent_inst, critical=True)

    # Confirm in the parent thread.
    link = result.thread_url or result.thread_mention
    try:
        await ctx.messenger.send_text(
            ctx.channel_id,
            f"Spawned new session: {link}",
        )
    except Exception:
        log.debug("Failed to post spawn confirmation", exc_info=True)
    return True
