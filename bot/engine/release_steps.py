"""The release half of a chain: verify-release, amend, continue-anyway and the release step itself."""

from __future__ import annotations

import logging
from pathlib import Path

from bot.textutil import hard_clip
from bot import config
from bot.claude.types import (
    Instance, InstanceOrigin,
    InstanceStatus, InstanceType,
)
from bot.platform.base import ButtonSpec, RequestContext

from bot.engine.chain_git import (
    _DIFF_PAYLOAD_CAP,
    _git_diff_payload,
    _git_diff_stat,
    _git_head_sha,
    _git_log_messages,
    _read_unreleased_block,
    _resolve_chain_repo_path,
)
from bot.engine.step_parse import _parse_verifier_json
from bot.engine.step_spawn import (
    SpawnConfig,
    spawn_from,
)

log = logging.getLogger(__name__)


def release_verified(result: Instance | None) -> bool:
    """Did the release verifier run and pass? None is not passed.

    The post-amend path decides whether to resume into the release step from
    an ``on_verify_release`` result, and it used to read None as a pass while
    the chain runner's post-step guard read it as a failure. An unverified
    release treated as verified is the unsafe direction, so this states the
    guard's meaning for the amend path. The chain keeps its generic guard
    (same answer for every step); ``scripts/test_autopilot_chain.py`` pins
    that half and ``scripts/test_verify_release_none.py`` this one.
    """
    return (
        result is not None
        and result.status == InstanceStatus.COMPLETED
        and not result.needs_input
    )


async def on_verify_release(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None = None,
    *,
    entry_sha: str | None = None,
) -> Instance | None:
    """Cross-check `done`-step claims (commit messages + CHANGELOG) vs. real diff.

    Spawns a verifier Claude with a bounded payload. On `mismatch` (phantom
    claims), sets `result.needs_input = True` so the chain pauses and the
    user gets Amend / Continue buttons.

    Fail-closed: any of {non-zero exit, empty result, JSON parse error,
    missing required keys} is treated as `mismatch` so the user gets a
    chance to inspect.

    Returns None when no verifier ran at all: an unresolvable repo, no entry
    SHA, nothing committed since the entry SHA, or a spawn that failed. None
    is "not passed" to every caller (the chain's post-step guard, and
    ``release_verified`` after an amend); the chain's own
    "nothing to verify" skips are decided before it calls, where they can be
    told apart from a verifier that never ran.
    """
    source_inst = ctx.store.get_instance(source_id)
    repo_path = _resolve_chain_repo_path(source_inst)
    if not repo_path:
        log.warning("verify_release: cannot resolve repo path for %s", source_id)
        return None

    # Defensive guards — the dispatch loop also pre-skips on these conditions,
    # but on_verify_release is also called directly by on_amend_done.
    if not entry_sha:
        log.warning("verify_release: no entry SHA — skipping gate")
        return None
    head_sha = _git_head_sha(repo_path)
    if head_sha and head_sha == entry_sha:
        log.info("verify_release: entry == HEAD, no commits to verify")
        return None

    commit_messages = _git_log_messages(repo_path, entry_sha) or "(no commit messages)"
    diff_stat = _git_diff_stat(repo_path, entry_sha) or "(empty)"
    diff_payload, truncated, files = _git_diff_payload(repo_path, entry_sha)
    if not diff_payload:
        diff_payload = "(empty diff)"

    truncation_note = ""
    if truncated:
        files_blob = "\n".join(files) if files else "(unknown)"
        truncation_note = (
            f" — diff was truncated at {_DIFF_PAYLOAD_CAP} bytes. "
            f"Full file list:\n{files_blob}\n"
            f"If a claim references a file outside the window, list it under "
            f"`needs_inspection` instead of marking phantom."
        )

    unreleased = _read_unreleased_block(repo_path)
    changelog_block = unreleased or "(parser failure: [Unreleased] missing or malformed)"

    prompt = config.build_release_verify_prompt(
        commit_messages=commit_messages,
        changelog_unreleased=changelog_block,
        diff_stat=diff_stat,
        diff_payload=diff_payload,
        truncation_note=truncation_note,
    )

    result = await spawn_from(ctx, source_id, SpawnConfig(
        instance_type=InstanceType.QUERY,
        prompt=prompt,
        mode="explore",
        origin=InstanceOrigin.VERIFY_RELEASE,
        status_text="Verifying release claims...",
        resume_session=True,
        copy_branch=True,
        silent=True,
    ), source_msg_id=source_msg_id)

    if not result:
        return None
    if result.status != InstanceStatus.COMPLETED:
        return result

    # The LLM sees the full diff and is the authoritative judge. Only fail
    # closed when its output itself is unreadable — a local CHANGELOG regex
    # miss is a tooling artifact, not a release signal.
    verifier_parse_failed = False

    verdict_data: dict | None = None
    if result.result_file and Path(result.result_file).exists():
        try:
            text = Path(result.result_file).read_text(encoding="utf-8")
        except Exception:
            text = ""
        verdict_data = _parse_verifier_json(text)
    if verdict_data is None:
        verifier_parse_failed = True
        verdict_data = {
            "verdict": "mismatch",
            "phantom_bullets": [],
            "missing_bullets": [],
            "needs_inspection": [],
            "rationale": "verifier output was unparseable — failing closed",
        }
    elif unreleased is None:
        # Local parser regressed but LLM verdict is intact — keep diagnostic
        # signal so a real regex bug doesn't hide forever.
        log.warning(
            "verify_release: local CHANGELOG parser returned None; "
            "trusting LLM verdict %s",
            verdict_data.get("verdict"),
        )

    raw_phantoms = verdict_data.get("phantom_bullets")
    # Be tolerant of a verifier that returns a non-list (e.g. a string) —
    # iterating a string would otherwise treat each character as a phantom.
    if not isinstance(raw_phantoms, list):
        raw_phantoms = []
    phantoms = [str(x) for x in raw_phantoms]
    rationale = str(verdict_data.get("rationale") or "")

    real_mismatch = (
        verdict_data.get("verdict") == "mismatch" and bool(phantoms)
    )
    is_mismatch = verifier_parse_failed or real_mismatch

    if is_mismatch:
        result.needs_input = True
        # Mark which sub-cause tripped the gate so the chain dispatcher can
        # pick a ping headline that matches the body — "couldn't be parsed"
        # vs "flagged phantom claims". Ephemeral field on Instance; see the
        # _verifier_parse_failed declaration in bot/claude/types.py.
        result._verifier_parse_failed = verifier_parse_failed
        ctx.store.update_instance(result)

        # Discord's text-message ceiling is 2000 chars. Cap each bullet
        # individually so one runaway claim doesn't push the whole body
        # past the limit and break the gate UI.
        capped_phantoms = [hard_clip(b, 200) for b in phantoms[:10]]
        more = len(phantoms) - len(capped_phantoms)

        if capped_phantoms:
            bullets_text = "\n".join(f"• {b}" for b in capped_phantoms)
            if more > 0:
                bullets_text += f"\n• …and {more} more"
            body = (
                f"**Release verifier flagged a mismatch.**\n\n"
                f"**Phantom claims:**\n{bullets_text}\n\n"
                f"**Rationale:** {hard_clip(rationale, 400)}\n\n"
                f"Tap **Amend** to re-run the wrap-up step with this feedback, "
                f"or **Continue anyway** to proceed to release."
            )
        else:
            # verifier_parse_failed path: no phantoms to enumerate, just
            # tell the user the gate is fail-closed and why.
            body = (
                f"**Release verifier output couldn't be parsed — failing closed.**\n\n"
                f"**Rationale:** {hard_clip(rationale, 400)}\n\n"
                f"Tap **Amend** to retry the wrap-up step, "
                f"or **Continue anyway** to proceed to release."
            )
        try:
            await ctx.messenger.send_text(
                ctx.channel_id, body,
                buttons=[[
                    ButtonSpec("Amend", f"amend:{result.id}"),
                    ButtonSpec("Continue anyway", f"continue_anyway:{result.id}"),
                ]],
                silent=False,
            )
        except Exception:
            log.exception("verify_release: failed to post Amend/Continue UI")

    return result


async def on_release_chain(
    ctx: RequestContext,
    source_id: str,
    source_msg_id: str | None = None,
) -> Instance | None:
    """Run the autopilot `release` chain step — cuts version, updates files, tags.

    Reuses RELEASE_PROMPT (the same prompt /release uses) so behavior matches
    the manual command. Defaults to `patch` bump.
    """
    prompt = config.RELEASE_PROMPT.format(version_hint="patch")
    cfg = SpawnConfig(
        instance_type=InstanceType.TASK,
        prompt=prompt,
        mode="build",
        origin=InstanceOrigin.RELEASE,
        status_text="Cutting release...",
        resume_session=True,
        copy_branch=True,
        silent=True,
    )
    source = ctx.store.get_instance(source_id)
    repo_path_for_lock = source.repo_path if source else None
    if repo_path_for_lock:
        async with ctx.runner.release_lock_scope(repo_path_for_lock, "release-chain"):
            return await spawn_from(ctx, source_id, cfg, source_msg_id=source_msg_id)
    return await spawn_from(ctx, source_id, cfg, source_msg_id=source_msg_id)
