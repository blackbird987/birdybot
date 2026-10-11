"""The /spawn directive: a session opening fresh forum threads with generated prompts."""

from __future__ import annotations

import logging
import re

from bot.textutil import find_tilde_block, is_quoted_example, mask_tilde_bodies
from bot.claude.types import Instance
from bot.platform.base import RequestContext, SpawnArgs
from bot.engine.cmd_state import check_budget

log = logging.getLogger(__name__)


# --- /spawn directive (Tier-2 BOT_CMD — assistant-issued session handoff) ---

_SPAWN_DIRECTIVE_RE = re.compile(r'\[BOT_CMD:\s*/spawn\s+(.+?)\]')
# The body is a tilde-fenced ~~~spawn ... ~~~ block, located by
# textutil.find_tilde_block so a brief that itself contains a ~~~wake or
# ~~~plan block for the child is not cut off at that block's closer. Tildes
# avoid colliding with the triple-backtick code fences inside the prompt body.
# kv pair: key=value where value is bare or quoted (single or double).
_SPAWN_KV_RE = re.compile(
    r'''(\w+)=(?:"([^"]*)"|'([^']*)'|(\S+))'''
)
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
    """Pair each /spawn directive with its own adjacent ~~~spawn body.

    A directive's body must appear between it and the next directive, so a
    single body can never satisfy two directives. Directives on quoted lines
    are skipped silently (same guard as single-spawn had).

    Returns ``(pairs, no_body, over_cap)``: ``pairs`` is the ordered list of
    ``(args_str, body)`` ready for dispatch (capped at
    _MAX_SPAWNS_PER_RESPONSE), ``no_body`` counts directives that lacked
    their own body block, ``over_cap`` counts directives dropped by the cap.
    """
    # Directives are found on the masked text, so one written inside another
    # block (a /spawn inside a child's brief) is neither dispatched here nor
    # counted as the next directive that ends this one's region.
    scan = mask_tilde_bodies(result_text)
    matches = list(_SPAWN_DIRECTIVE_RE.finditer(scan))
    pairs: list[tuple[str, str]] = []
    no_body = 0
    over_cap = 0
    for i, m in enumerate(matches):
        if is_quoted_example(scan, m.start()):
            log.debug("BOT_CMD /spawn skipped: a quoted example")
            continue
        if len(pairs) >= _MAX_SPAWNS_PER_RESPONSE:
            over_cap += 1
            continue
        region_end = (
            matches[i + 1].start() if i + 1 < len(matches) else len(result_text)
        )
        span = find_tilde_block(result_text, "spawn", m.end(), region_end)
        if span is None:
            no_body += 1
            continue
        pairs.append((m.group(1).strip(), result_text[span[0]:span[1]].strip()))
    return pairs, no_body, over_cap


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


def _parse_spawn_kv(args_str: str) -> dict[str, str] | None:
    """Parse `key=val key="quoted val"` into a dict.

    Returns None if any input bytes are left unparsed (catches malformed
    input like trailing junk). No values reach a shell — `repo` is
    whitelisted against the registry, `mode`/`effort` are enum-checked,
    `title` is a Discord thread topic, and `body` is the prompt — so the
    over-broad shell-metachar check that used to live here rejected
    legitimate titles containing `()` `{}` `!`.
    """
    out: dict[str, str] = {}
    consumed_end = 0
    for m in _SPAWN_KV_RE.finditer(args_str):
        # Reject if there's non-whitespace junk between matches.
        between = args_str[consumed_end:m.start()]
        if between.strip():
            return None
        key = m.group(1)
        val = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else m.group(4)
        )
        out[key] = val
        consumed_end = m.end()
    # Reject if there's non-whitespace tail.
    if args_str[consumed_end:].strip():
        return None
    return out


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

    kv = _parse_spawn_kv(args_str)
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
    repos = ctx.store.list_repos()
    canonical = next(
        (name for name in repos if name.lower() == repo.lower()),
        None,
    )
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
