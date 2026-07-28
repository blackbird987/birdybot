"""Discord surfacing for signed-out Claude accounts.

A backup account can sit signed out for weeks without anyone noticing —
nothing fails until the primary hits a usage limit and failover lands on a
dead account. The runner and startup validation record that condition in the
store (``set_account_alert`` / ``resolve_account_alert``); this module is the
drain: it posts exactly one notice per outage to The Ark, with buttons to fix
it, and a one-line all-clear when the account comes back.

Deliberately quiet by design: one notice per outage, not one per failed run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord

from bot import config
from bot.claude.auth_health import (
    REASON_NO_DIR,
    REASON_NO_FILE,
    REASON_NO_TOKEN,
    REASON_RUNTIME_401,
    REASON_UNREADABLE,
    account_label,
    relogin_command,
)

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)

POLL_SECS = 60
SNOOZE_DAYS = 7
# Discord caps custom_id at 100 chars; the label suffix is a safety check, not
# data, so truncating it on both sides keeps the comparison honest.
LABEL_ID_MAX = 40


def snooze_deadline() -> str:
    """ISO timestamp for the 'Ignore for now' button."""
    return (datetime.now(timezone.utc) + timedelta(days=SNOOZE_DAYS)).isoformat()


def _account_index(account_dir: str) -> int | None:
    """Position of *account_dir* in the configured rotation, if still there."""
    for i, acct in enumerate(config.CLAUDE_ACCOUNTS):
        if acct == account_dir:
            return i
    return None


def button_id(action: str, account_dir: str, idx: int) -> str:
    """``auth:<action>:<idx>:<label>`` — index to act on, label to verify it.

    This notice lives in The Ark forever (no view timeout), so its buttons can
    outlive an edit to CLAUDE_ACCOUNTS. Carrying the label lets the handler
    notice that position 1 is no longer the account the button names, instead
    of silently opening a login terminal for the wrong one.
    """
    return f"auth:{action}:{idx}:{account_label(account_dir)[:LABEL_ID_MAX]}"


# The reason strings are written for the log, where a filename or an HTTP
# status is the useful detail. Dropped into a sentence in The Ark they read as
# a stutter ("is signed out — logged out — ... (401)") and leak internals at
# the user. Each one gets a plain-English cause here; anything unmapped falls
# back to the raw string, which is still readable, just less polished.
_REASON_COPY = {
    REASON_RUNTIME_401: "the login expired and Claude turned it away",
    REASON_NO_TOKEN: "there's no saved login for it",
    REASON_NO_FILE: "it has never been signed in on this machine",
    REASON_NO_DIR: "its config folder is missing",
    REASON_UNREADABLE: "its saved login can't be read",
}


def describe_reason(reason: str) -> str:
    """Plain-English cause for a sidelined account, for user-facing copy."""
    return _REASON_COPY.get(reason, reason)


def build_alert_embed(
    account_dir: str, reason: str, also_down: set[str] | None = None,
) -> discord.Embed:
    """Orange 'this account is sidelined' notice for The Ark.

    *also_down* is the rest of the currently sidelined set. Without it the
    notice happily promises that "work keeps running on `<other>`" while that
    other account is signed out too — during a both-accounts outage each of the
    two notices would point at the other as the survivor.
    """
    label = account_label(account_dir)
    embed = discord.Embed(
        title="Account sidelined — failover degraded",
        color=discord.Color.orange(),
    )

    down = set(also_down or ())
    others = [a for a in config.CLAUDE_ACCOUNTS if a != account_dir]
    healthy = [a for a in others if a not in down]
    # The closing reassurance only holds while something is left to run on;
    # telling the user nothing needs their attention when no account can take
    # a task is the one place this notice could actively mislead.
    if healthy:
        still = ", ".join(f"`{account_label(a)}`" for a in healthy)
        impact = (
            f"Work keeps running on {still}. What you lose is the safety net: "
            "when that account hits its usage limit there's nothing to fail "
            "over to, so tasks wait for the reset instead of continuing."
        )
        closer = "Nothing has failed and nothing needs your attention right now. "
    else:
        impact = (
            "Every configured account is signed out, so nothing can run until "
            "one of them is signed in again."
            if others else
            "This is the only account configured, so nothing can run until "
            "it's signed in again."
        )
        closer = ("New tasks can't run — they'll auto-retry a few times in "
                  "case you sign in, then give up. ")

    embed.description = (
        f"**`{label}`** is signed out — {describe_reason(reason)}.\n\n"
        f"{impact}\n\n"
        f"{closer}"
        "The account rejoins rotation on its own the moment it's signed in "
        "— no restart needed."
    )
    embed.add_field(
        name=f"Sign in on {config.PC_NAME}",
        value=f"```\n{relogin_command(account_dir)}\n```\nthen `/login` inside.",
        inline=False,
    )
    return embed


def build_alert_view(account_dir: str, can_console: bool) -> discord.ui.View:
    """Buttons: open the auth panel, pop a login terminal, or snooze."""
    view = discord.ui.View(timeout=None)
    idx = _account_index(account_dir)

    view.add_item(discord.ui.Button(
        label="Auth panel",
        style=discord.ButtonStyle.primary,
        custom_id="ark:claude_login",
        row=0,
    ))
    if can_console and idx is not None:
        view.add_item(discord.ui.Button(
            label=f"Log in {account_label(account_dir)} on this PC"[:80],
            style=discord.ButtonStyle.secondary,
            custom_id=button_id("login", account_dir, idx),
            row=0,
        ))
    if idx is not None:
        view.add_item(discord.ui.Button(
            label=f"Ignore for {SNOOZE_DAYS}d",
            style=discord.ButtonStyle.secondary,
            custom_id=button_id("snooze", account_dir, idx),
            row=0,
        ))
    return view


def build_clear_message(account_dir: str) -> str:
    """One-line all-clear — no embed, no buttons, no ceremony."""
    return f"✅ Account `{account_label(account_dir)}` is back in rotation."


async def _ark_channel(bot: ClaudeBot):
    """The Ark, or None if it isn't resolvable yet (cache cold, bot starting).

    Duck-typed on ``send`` rather than ``isinstance(TextChannel)`` so a Thread
    works too — and so the harness can hand in a recording stub.
    """
    if not bot._lobby_channel_id:
        return None
    ch = bot.get_channel(int(bot._lobby_channel_id))
    return ch if ch is not None and hasattr(ch, "send") else None


async def _drain_once(bot: ClaudeBot) -> None:
    """Post pending notices / all-clears for the current alert table."""
    store = bot._store
    alerts = store.get_account_alerts()
    if not alerts:
        return

    channel = await _ark_channel(bot)
    if channel is None:
        return

    from bot.services.auth_sync import host_can_show_console

    now = datetime.now(timezone.utc)
    can_console: bool | None = None
    # Every account currently sidelined, so a notice can't promise that work
    # continues on an account that is itself down.
    down = {a for a, m in alerts.items() if not m.get("resolved")}

    for account_dir, meta in list(alerts.items()):
        try:
            # An account dropped from CLAUDE_ACCOUNTS is nothing the runner
            # touches again, so its record would sit here forever — and if it
            # was never notified, we'd announce an outage on an account the
            # user deliberately removed.  Guarded on a non-empty rotation so an
            # unconfigured/misread env can't wipe live alerts.
            if config.CLAUDE_ACCOUNTS and account_dir not in config.CLAUDE_ACCOUNTS:
                log.info("Dropping account alert for unconfigured %s", account_dir)
                store.drop_account_alert(account_dir)
                continue

            if meta.get("resolved"):
                if meta.get("notified"):
                    await channel.send(build_clear_message(account_dir))
                store.drop_account_alert(account_dir)
                continue

            # "notified" means the user has seen this outage; "snooze_until"
            # means they asked for it back later. So: stay quiet while a snooze
            # is running, speak once when it lapses, and otherwise speak only
            # if they've never been told. mark_account_alert_notified clears
            # the snooze, which is what makes "Ignore" repeatable rather than
            # a one-shot that silences the account forever.
            snooze_until = meta.get("snooze_until")
            if snooze_until:
                try:
                    if datetime.fromisoformat(snooze_until) > now:
                        continue
                except (TypeError, ValueError):
                    snooze_until = None
            if meta.get("notified") and not snooze_until:
                continue

            if can_console is None:
                can_console = host_can_show_console()

            reason = meta.get("reason") or "not logged in"
            await channel.send(
                embed=build_alert_embed(account_dir, reason, down),
                view=build_alert_view(account_dir, can_console),
            )
            store.mark_account_alert_notified(account_dir)
            log.info("Posted account-sidelined notice for %s (%s)",
                     account_dir, reason)
        except discord.HTTPException:
            # Transient — leave the alert unnotified and retry next tick.
            log.warning("Account alert post failed for %s", account_dir,
                        exc_info=True)
        except Exception:
            log.exception("Account alert drain error for %s", account_dir)


async def run_account_alert_notifier(
    bot: ClaudeBot, stop_event: asyncio.Event,
) -> None:
    """Background task: surface signed-out accounts in The Ark."""
    if not await bot._wait_for_ready("account_alerts"):
        return
    while not stop_event.is_set():
        # Wait on the stop event rather than sleeping blind, so shutdown isn't
        # held up for a minute by a loop that has nothing to do.
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_SECS)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await _drain_once(bot)
        except Exception:
            log.exception("Account alert notifier error")
