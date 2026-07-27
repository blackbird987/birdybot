"""Tests for the signed-out-account notice in The Ark (t-6614).

The runner records "this account can't authenticate" as state; this module's
job is to make sure that state turns into exactly ONE Discord notice per
outage, with a usable fix, and exactly one all-clear when it recovers.

Covers:
  - the persisted alert state machine in StateStore (open / notify / snooze /
    resolve / drop), including the "never announced -> nothing to un-say" case
  - the notifier drain: one embed per outage, never a second one, all-clear
    posted then dropped, snoozed alerts stay quiet
  - the embed/view actually built: plain-language body, copy-pasteable
    re-auth command, and buttons that fit Discord's row limit

No Discord connection — the channel is a stub that records what was sent.

Run: ``python scripts/test_account_alerts.py``  (exit 0 on pass).
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude.auth_health import REASON_NO_TOKEN
from bot.discord import account_alerts as alerts_mod
from bot.store.state import StateStore

ACCT = "/home/q/.claude-klerk"
OTHER = "/home/q/.claude"


class _StubChannel:
    """Stands in for The Ark — records every send instead of hitting Discord."""

    def __init__(self):
        self.sent: list[tuple[str | None, object, object]] = []

    async def send(self, content=None, *, embed=None, view=None):
        self.sent.append((content, embed, view))


class _StubBot:
    def __init__(self, store, channel):
        self._store = store
        self._lobby_channel_id = "1234"
        self._channel = channel

    def get_channel(self, _id):
        return self._channel


def _fresh_store(tmp: str) -> StateStore:
    return StateStore(
        state_file=Path(tmp) / "state.json",
        results_dir=Path(tmp) / "results",
    )


def _test_state_machine(store: StateStore) -> list[str]:
    failures: list[str] = []

    if not store.set_account_alert(ACCT, REASON_NO_TOKEN, "2026-07-27T10:00:00+00:00"):
        failures.append("state: first set_account_alert should report 'new'")
    if store.set_account_alert(ACCT, REASON_NO_TOKEN, "2026-07-27T11:00:00+00:00"):
        failures.append(
            "state: re-marking an open alert reported 'new' — a burst of "
            "failed tasks would re-notify"
        )
    if store.get_account_alerts()[ACCT]["since"] != "2026-07-27T10:00:00+00:00":
        failures.append("state: re-marking overwrote the original 'since'")

    # Never announced -> resolving drops it outright (nothing to un-say).
    store.resolve_account_alert(ACCT)
    if ACCT in store.get_account_alerts():
        failures.append(
            "state: an unannounced alert should vanish on resolve, not linger "
            "as a pending all-clear for an outage nobody saw"
        )

    # Announced -> resolving leaves the all-clear pending.
    store.set_account_alert(ACCT, REASON_NO_TOKEN, "2026-07-27T12:00:00+00:00")
    store.mark_account_alert_notified(ACCT)
    store.resolve_account_alert(ACCT)
    rec = store.get_account_alerts().get(ACCT)
    if not rec or not rec.get("resolved"):
        failures.append("state: announced alert should be marked resolved")

    # Re-breaking before the all-clear went out re-opens the same record.
    store.set_account_alert(ACCT, REASON_NO_TOKEN, "2026-07-27T13:00:00+00:00")
    rec = store.get_account_alerts()[ACCT]
    if rec.get("resolved"):
        failures.append("state: re-breaking should clear the resolved flag")
    if not rec.get("notified"):
        failures.append(
            "state: re-opening lost the notified flag — the user would be "
            "told twice about one outage"
        )

    store.drop_account_alert(ACCT)
    if store.get_account_alerts():
        failures.append("state: drop_account_alert left a record behind")
    return failures


async def _test_drain(store: StateStore) -> list[str]:
    failures: list[str] = []
    channel = _StubChannel()
    bot = _StubBot(store, channel)

    saved = list(config.CLAUDE_ACCOUNTS)
    config.CLAUDE_ACCOUNTS[:] = [OTHER, ACCT]
    try:
        store.set_account_alert(ACCT, REASON_NO_TOKEN, "2026-07-27T10:00:00+00:00")
        await alerts_mod._drain_once(bot)
        if len(channel.sent) != 1:
            failures.append(
                f"drain: expected exactly 1 notice, got {len(channel.sent)}"
            )
        elif channel.sent[0][1] is None:
            failures.append("drain: notice was posted without an embed")

        # Second tick must stay silent.
        await alerts_mod._drain_once(bot)
        if len(channel.sent) != 1:
            failures.append(
                "drain: a second tick re-posted the notice — this would spam "
                "The Ark once a minute"
            )

        # Recovery -> one all-clear, then the record is gone.
        store.resolve_account_alert(ACCT)
        await alerts_mod._drain_once(bot)
        if len(channel.sent) != 2:
            failures.append("drain: no all-clear posted after recovery")
        elif not (channel.sent[1][0] or "").startswith("✅"):
            failures.append(
                f"drain: all-clear text looked wrong: {channel.sent[1][0]!r}"
            )
        if store.get_account_alerts():
            failures.append("drain: alert record survived its all-clear")

        await alerts_mod._drain_once(bot)
        if len(channel.sent) != 2:
            failures.append("drain: posted something after the record was dropped")

        # A snoozed alert stays quiet even though it was never announced.
        store.set_account_alert(ACCT, REASON_NO_TOKEN, "2026-07-27T14:00:00+00:00")
        store.snooze_account_alert(ACCT, alerts_mod.snooze_deadline())
        await alerts_mod._drain_once(bot)
        if len(channel.sent) != 2:
            failures.append("drain: a snoozed alert still nagged")
    finally:
        config.CLAUDE_ACCOUNTS[:] = saved
    return failures


def _test_rendering() -> list[str]:
    failures: list[str] = []
    saved = list(config.CLAUDE_ACCOUNTS)
    config.CLAUDE_ACCOUNTS[:] = [OTHER, ACCT]
    try:
        embed = alerts_mod.build_alert_embed(ACCT, REASON_NO_TOKEN)
        body = (embed.description or "") + "".join(
            f.value or "" for f in embed.fields
        )
        if "klerk" not in body:
            failures.append("render: the notice never names the account")
        if "CLAUDE_CONFIG_DIR" not in body:
            failures.append("render: no copy-pasteable re-auth command")
        if "401" in body:
            failures.append("render: leaked the raw CLI error into the notice")
        if "```" not in body:
            failures.append("render: the command isn't in a code block to copy")

        view = alerts_mod.build_alert_view(ACCT, can_console=True)
        ids = [getattr(i, "custom_id", None) for i in view.children]
        if "ark:claude_login" not in ids:
            failures.append("render: no way to open the auth panel")
        if not any((i or "").startswith("auth:login:") for i in ids):
            failures.append("render: no 'log in on this PC' button")
        if not any((i or "").startswith("auth:snooze:") for i in ids):
            failures.append("render: no way to dismiss the notice")
        rows = {getattr(i, "row", 0) for i in view.children}
        if len(rows) > 5:
            failures.append("render: exceeds Discord's 5-row button limit")

        # Console-less host: the terminal button must disappear, not sit dead.
        view2 = alerts_mod.build_alert_view(ACCT, can_console=False)
        ids2 = [getattr(i, "custom_id", None) for i in view2.children]
        if any((i or "").startswith("auth:login:") for i in ids2):
            failures.append(
                "render: offered 'log in on this PC' on a host that can't "
                "open a terminal"
            )

        # An account no longer in CLAUDE_ACCOUNTS has no valid index to act on.
        config.CLAUDE_ACCOUNTS[:] = [OTHER]
        ids3 = [
            getattr(i, "custom_id", None)
            for i in alerts_mod.build_alert_view(ACCT, can_console=True).children
        ]
        if any((i or "").startswith(("auth:login:", "auth:snooze:")) for i in ids3):
            failures.append(
                "render: built index-based buttons for an account that is no "
                "longer configured — they would act on the wrong account"
            )
    finally:
        config.CLAUDE_ACCOUNTS[:] = saved
    return failures


async def _amain() -> int:
    tmp = tempfile.mkdtemp(prefix="acct_alerts_")
    try:
        store = _fresh_store(tmp)
        all_failures = [
            ("state-machine", _test_state_machine(store)),
            ("drain", await _test_drain(store)),
            ("rendering", _test_rendering()),
        ]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total = sum(len(f) for _, f in all_failures)
    if total:
        print("FAIL: account-alert tests")
        for name, fails in all_failures:
            for f in fails:
                print(f"  [{name}] {f}")
        return 1

    print("PASS: account-alert tests")
    for name, _ in all_failures:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
