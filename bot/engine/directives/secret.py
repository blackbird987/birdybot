"""The [BOT_CMD: /secret request NAME reason="..."] directive: a session asks for a key.

It never gets the value this way. The owner sees the name and the reason with
Allow and Deny buttons, and Allow grants the repo and resumes the thread.
"""

from __future__ import annotations

import logging

from bot import secret_detect, vault
from bot.claude.types import Instance
from bot.engine.directives.common import parse_kv
from bot.platform.base import ButtonSpec, RequestContext
from bot.textutil import directive_re, is_quoted_example, mask_tilde_bodies

log = logging.getLogger(__name__)

_SECRET_RE = directive_re("secret")
_MAX_REQUESTS_PER_RESPONSE = 3
_REASON_MAX = 200


def _eligible(inst: Instance | None) -> bool:
    """Only a session that could use the key may ask for it. Nothing else can."""
    # Local import: lifecycle reaches this module through commands.
    from bot.engine.lifecycle import _NO_DIRECTIVE_ORIGINS

    return bool(
        inst is not None
        and inst.is_owner_session
        and inst.vault_eligible
        and inst.repo_name
        and inst.origin not in _NO_DIRECTIVE_ORIGINS
    )


async def _handle_secret_directives(
    ctx: RequestContext, result_text: str, source_inst: Instance | None,
) -> None:
    scan = mask_tilde_bodies(result_text)
    matches = [m for m in _SECRET_RE.finditer(scan) if not is_quoted_example(scan, m.start())]
    if not matches or not _eligible(source_inst):
        return
    posted = 0
    for m in matches:
        if posted >= _MAX_REQUESTS_PER_RESPONSE:
            log.info("BOT_CMD /secret: over the per-response cap, the rest ignored")
            break
        action, _, rest = m.group(1).strip().partition(" ")
        name, _, kv = rest.strip().partition(" ")
        if action != "request" or not vault.valid_name(name) or not vault.has_name(name):
            log.info("BOT_CMD /secret skipped: not a request for a stored name")
            continue
        if name in vault.names_for_repo(source_inst.repo_name):
            continue
        reason = (parse_kv(kv) or {}).get("reason", "") if kv.strip() else ""
        reason = secret_detect.scrub(vault.strip_literals(reason))[:_REASON_MAX]
        try:
            token = vault.add_request(
                name, source_inst.repo_name, ctx.channel_id, source_inst.session_id,
            )
        except vault.VaultUnavailable:
            log.warning("BOT_CMD /secret: the vault file is unreadable")
            return
        if token is None:
            continue
        posted += 1
        why = f" Reason: {reason}" if reason else ""
        try:
            await ctx.messenger.send_text(
                ctx.channel_id,
                f"This session asks for `{name}` in {source_inst.repo_name}.{why}",
                buttons=[[
                    ButtonSpec("Allow", f"secret_allow:{token}"),
                    ButtonSpec("Deny", f"secret_deny:{token}"),
                ]],
            )
        except Exception:
            log.warning("Could not post a /secret request", exc_info=True)
