"""Discord surfacing for signed-out Claude accounts.

A backup account can sit signed out for weeks without anyone noticing —
nothing fails until the primary hits a usage limit and failover lands on a
dead account. The runner and startup validation record that condition in the
store (``set_account_alert`` / ``resolve_account_alert``); this module is the
drain: it posts exactly one notice per outage to The Ark, with buttons to fix
it, and a one-line all-clear when the account comes back.

Deliberately quiet by design: one notice per outage, not one per failed run.

It also posts the one warning that comes *before* an outage: a login has a
hard end date about four weeks after sign-in that using the account does not
push back, so a few days ahead of it The Ark gets a single heads-up to sign in
again (``_check_login_expiry``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord

from bot.textutil import parse_iso_utc
from bot import config
from bot.claude.auth_health import (
    REASON_NO_DIR,
    REASON_NO_FILE,
    REASON_LOGIN_EXPIRED,
    REASON_NO_TOKEN,
    REASON_ORG_DISABLED,
    REASON_RUNTIME_401,
    REASON_UNREADABLE,
    account_label,
    login_expiry,
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
# How far ahead of a login's end date The Ark warns. Long enough to catch it
# across a weekend away, short enough that it is still news when it arrives.
LOGIN_EXPIRY_WARN_DAYS = 3
# A fresh login moves the end date by weeks. Anything smaller is the server
# re-stating the same date with jitter, not the user signing in again.
LOGIN_RENEWED_MIN_SHIFT = timedelta(hours=1)


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
    REASON_ORG_DISABLED: (
        "its organization has switched Claude Code off for the whole account"
    ),
    REASON_NO_TOKEN: "there's no saved login for it",
    REASON_NO_FILE: "it has never been signed in on this machine",
    REASON_NO_DIR: "its config folder is missing",
    REASON_UNREADABLE: "its saved login can't be read",
    REASON_LOGIN_EXPIRED: (
        "its login reached its end date (logins last about four weeks from "
        "sign-in, however much the account is used)"
    ),
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

    # An org-disabled account is signed in perfectly well, so every sentence
    # this notice normally ends on is wrong for it: it is not "signed out", a
    # re-login provably will not fix it, and it cannot "rejoin the moment it's
    # signed in" because it never left that state. Sending the user to run
    # /login here is sending them to do something that cannot work.
    org_disabled = reason == REASON_ORG_DISABLED
    # What the account is waiting for, which is the one word the "nothing can
    # run until ..." sentences below differ by.
    recovery = "access is restored" if org_disabled else "it's signed in again"

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
            "Every configured account is sidelined, so nothing can run until "
            "one of them is usable again."
            if others else
            f"This is the only account configured, so nothing can run until "
            f"{recovery}."
        )
        closer = ("New tasks can't run: they'll auto-retry a few times in "
                  "case it recovers, then give up. ")

    opener = "is out of rotation" if org_disabled else "is signed out"
    if org_disabled:
        remedy = (
            "Signing in again won't help: it's signed in fine. An admin has "
            "to re-enable Claude Code for that organization. The bot retries "
            "the account about once a day and takes it back the moment access "
            "returns; `/auth` has a **Try now** button if you don't want to "
            "wait for that."
        )
    else:
        remedy = (
            "The account rejoins rotation on its own the moment it's signed "
            "in — no restart needed."
        )

    embed.description = (
        f"**`{label}`** {opener} — {describe_reason(reason)}.\n\n"
        f"{impact}\n\n"
        f"{closer}"
        f"{remedy}"
    )
    if org_disabled:
        embed.add_field(
            name="What unblocks it",
            value=(
                f"An organization admin re-enabling Claude Code for "
                f"`{label}`. Nothing on {config.PC_NAME} needs changing, and "
                f"the account can stay in `CLAUDE_ACCOUNTS` while it's off."
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name=f"Sign in on {config.PC_NAME}",
            value=(
                f"```\n{relogin_command(account_dir)}\n```\n"
                f"then `/login` inside."
            ),
            inline=False,
        )
    return embed


def build_alert_view(
    account_dir: str, can_console: bool, reason: str = "",
) -> discord.ui.View:
    """Buttons: open the auth panel, pop a login terminal, or snooze.

    *reason* swaps the middle button. For an org-disabled account a login
    terminal is the one thing that cannot help, so it offers **Try now**
    instead: the only action the user can take from here once an admin has
    flipped access back on.
    """
    view = discord.ui.View(timeout=None)
    idx = _account_index(account_dir)

    view.add_item(discord.ui.Button(
        label="Auth panel",
        style=discord.ButtonStyle.primary,
        custom_id="ark:claude_login",
        row=0,
    ))
    if reason == REASON_ORG_DISABLED and idx is not None:
        view.add_item(discord.ui.Button(
            label=f"Try {account_label(account_dir)} now"[:80],
            style=discord.ButtonStyle.secondary,
            custom_id=button_id("retry", account_dir, idx),
            row=0,
        ))
    elif can_console and idx is not None:
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


def discord_time(dt: datetime, style: str) -> str:
    """Discord timestamp markup, rendered in the reader's own timezone."""
    return f"<t:{int(dt.timestamp())}:{style}>"


def build_expiry_embed(account_dir: str, expires: datetime) -> discord.Embed:
    """Yellow 'sign in again before this date' heads-up for The Ark."""
    label = account_label(account_dir)
    # With nothing to fail over to, "drops out of rotation" undersells it.
    lapse = (
        "nothing can run until you do"
        if len(config.CLAUDE_ACCOUNTS) <= 1
        else f"`{label}` drops out of rotation until you do"
    )
    embed = discord.Embed(
        title="Login expiring soon",
        color=discord.Color.gold(),
    )
    embed.description = (
        f"**`{label}`** stays signed in until "
        f"**{discord_time(expires, 'f')}** ({discord_time(expires, 'R')}).\n\n"
        "Logins last about four weeks from sign-in, and using the account "
        "doesn't extend that, so there's no way to keep it alive. Sign in "
        f"again before then and it carries on without a gap. Let it lapse and "
        f"{lapse}."
    )
    embed.add_field(
        name=f"Sign in on {config.PC_NAME}",
        value=f"```\n{relogin_command(account_dir)}\n```\nthen `/login` inside.",
        inline=False,
    )
    return embed


def build_expiry_view(account_dir: str, can_console: bool) -> discord.ui.View:
    """Auth panel, plus a login terminal when this host can show one."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label="Auth panel",
        style=discord.ButtonStyle.primary,
        custom_id="ark:claude_login",
        row=0,
    ))
    idx = _account_index(account_dir)
    if can_console and idx is not None:
        view.add_item(discord.ui.Button(
            label=f"Log in {account_label(account_dir)} on this PC"[:80],
            style=discord.ButtonStyle.secondary,
            custom_id=button_id("login", account_dir, idx),
            row=0,
        ))
    return view


def build_renewed_message(account_dir: str, expires: datetime) -> str:
    """One-line confirmation that the warned-about login was renewed."""
    return (
        f"✅ `{account_label(account_dir)}` is signed in again, good until "
        f"{discord_time(expires, 'D')}."
    )


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
                until = parse_iso_utc(snooze_until)
                if until is None:
                    snooze_until = None
                elif until > now:
                    continue
            if meta.get("notified") and not snooze_until:
                continue

            if can_console is None:
                can_console = host_can_show_console()

            reason = meta.get("reason") or "not logged in"
            await channel.send(
                embed=build_alert_embed(account_dir, reason, down),
                view=build_alert_view(account_dir, can_console, reason),
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


async def _check_login_expiry(bot: ClaudeBot) -> None:
    """Warn once per login before it runs out; confirm once when renewed.

    Keyed on the end date itself: the warned date is stored, so the warning
    fires once per login, and a later date on the file is the evidence of a
    fresh sign-in. Accounts already carrying an open sideline are skipped,
    because they have a notice of their own and a login reminder on top of
    "this account is down" is noise. Once the date has passed the probe
    sidelines the account (``REASON_LOGIN_EXPIRED``) and that notice takes
    over, so this never says "expired" itself.
    """
    accounts = list(config.CLAUDE_ACCOUNTS)
    # Same guard as the outage drain: with no rotation configured there is
    # nothing to check, and a misread env must not wipe what was already said.
    if not accounts:
        return
    store = bot._store
    warned = store.get_login_expiry_warnings()

    channel = None
    can_console: bool | None = None
    now = datetime.now(timezone.utc)
    try:
        open_alerts = store.sidelined_accounts()
    except Exception:
        open_alerts = set()

    for account_dir in list(warned):
        if account_dir not in accounts:
            store.drop_login_expiry_warning(account_dir)
            warned.pop(account_dir, None)

    for account_dir in accounts:
        try:
            expires = login_expiry(account_dir)
            prior_iso = warned.get(account_dir)
            prior = parse_iso_utc(prior_iso) if prior_iso else None

            if prior_iso:
                if prior is not None and prior <= now and (
                    expires is None or expires > now
                ):
                    # The login lapsed before it was renewed. If the account
                    # was sidelined in that gap, the outage notice's all-clear
                    # covers this; if it never was, the user just signed in
                    # and needs no telling. Either way, nothing to add here.
                    store.drop_login_expiry_warning(account_dir)
                elif expires is not None and prior is not None and (
                    expires - prior >= LOGIN_RENEWED_MIN_SHIFT
                ):
                    channel = channel or await _ark_channel(bot)
                    if channel is None:
                        return
                    await channel.send(build_renewed_message(account_dir, expires))
                    store.drop_login_expiry_warning(account_dir)
                    log.info("Login for %s renewed, now ends %s",
                             account_label(account_dir), expires.isoformat())
                elif prior is None:
                    # Unparseable record: forget it rather than stay mute.
                    store.drop_login_expiry_warning(account_dir)
                continue

            if expires is None or account_dir in open_alerts:
                continue
            if not (now < expires <= now + timedelta(days=LOGIN_EXPIRY_WARN_DAYS)):
                continue

            channel = channel or await _ark_channel(bot)
            if channel is None:
                return
            if can_console is None:
                from bot.services.auth_sync import host_can_show_console
                can_console = host_can_show_console()
            await channel.send(
                embed=build_expiry_embed(account_dir, expires),
                view=build_expiry_view(account_dir, can_console),
            )
            store.set_login_expiry_warned(account_dir, expires.isoformat())
            log.info("Posted login-expiry warning for %s (ends %s)",
                     account_label(account_dir), expires.isoformat())
        except discord.HTTPException:
            log.warning("Login expiry post failed for %s", account_dir,
                        exc_info=True)
        except Exception:
            log.exception("Login expiry check error for %s", account_dir)


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
        try:
            await _check_login_expiry(bot)
        except Exception:
            log.exception("Login expiry check error")
