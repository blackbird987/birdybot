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
  - the notice copy staying honest: no raw reason strings or HTTP statuses in
    the prose, and no promising that work continues on an account that is
    itself signed out
  - an alert for an account removed from CLAUDE_ACCOUNTS being dropped rather
    than nagging forever about something the user deleted on purpose
  - the /auth panel the notice links to reporting the same account as signed
    out, even though its credentials file still reads as valid

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
from bot.claude.auth_health import REASON_NO_TOKEN, REASON_RUNTIME_401, account_label
from bot.claude.auth_health import clear_cache as auth_clear_cache
from bot.discord import account_alerts as alerts_mod
from bot.services.auth_sync import collect_account_statuses
from bot.store.state import StateStore

# portability: ok - synthetic identifiers, never resolved against disk
ACCT = "/home/q/.claude-klerk"
# portability: ok - synthetic identifiers, never resolved against disk
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

    # The credentials fingerprint belongs to the verdict, not the reason.
    # If a re-open with the same reason kept the OLD fingerprint, the runner
    # would compare it against the current file, see a mismatch, read that as
    # "someone logged in", un-sideline the account, watch it fail again, and
    # repeat forever — one wasted spawn and an Ark notice per cycle.
    store.set_account_alert(ACCT, REASON_NO_TOKEN, "2026-07-27T15:00:00+00:00",
                            cred_fp="111:20")
    store.mark_account_alert_notified(ACCT)
    store.resolve_account_alert(ACCT)
    store.set_account_alert(ACCT, REASON_NO_TOKEN, "2026-07-27T16:00:00+00:00",
                            cred_fp="222:30")
    if store.get_account_alerts()[ACCT].get("cred_fp") != "222:30":
        failures.append(
            "state: re-opening an alert kept the stale credentials "
            "fingerprint — the account would flap in and out of the sideline"
        )
    # ...but a caller with no opinion must not blank it.
    store.set_account_alert(ACCT, REASON_NO_TOKEN, "2026-07-27T17:00:00+00:00")
    if store.get_account_alerts()[ACCT].get("cred_fp") != "222:30":
        failures.append(
            "state: a set_account_alert call without a fingerprint erased the "
            "recorded one — the sideline could never be retired by a /login"
        )

    store.drop_account_alert(ACCT)
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

        # "Ignore for now" means later, not never: quiet while the snooze
        # runs, one reminder once it lapses, and snoozable again after that.
        store.set_account_alert(ACCT, REASON_NO_TOKEN, "2026-07-27T14:00:00+00:00")
        if not store.snooze_account_alert(ACCT, alerts_mod.snooze_deadline()):
            failures.append("snooze: refused to mute an open alert")
        await alerts_mod._drain_once(bot)
        if len(channel.sent) != 2:
            failures.append("drain: a snoozed alert still nagged")

        store.snooze_account_alert(ACCT, "2020-01-01T00:00:00+00:00")  # lapsed
        await alerts_mod._drain_once(bot)
        if len(channel.sent) != 3:
            failures.append(
                "drain: the snooze never lapsed — 'Ignore for now' silently "
                "became 'ignore forever'"
            )
        await alerts_mod._drain_once(bot)
        if len(channel.sent) != 3:
            failures.append(
                "drain: kept nagging after the post-snooze reminder — the "
                "lapsed snooze wasn't cleared when the notice went out"
            )
        if not store.snooze_account_alert(ACCT, alerts_mod.snooze_deadline()):
            failures.append("snooze: couldn't be re-armed after the reminder")

        # Nothing to mute once the account is healthy again.
        store.resolve_account_alert(ACCT)
        if store.snooze_account_alert(ACCT, alerts_mod.snooze_deadline()):
            failures.append(
                "snooze: claimed to mute an alert that was already resolved"
            )
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
        rows: dict[int, int] = {}
        for item in view.children:
            rows[getattr(item, "row", 0)] = rows.get(getattr(item, "row", 0), 0) + 1
        if len(rows) > 5 or any(n > 5 for n in rows.values()):
            failures.append(
                f"render: breaks Discord's 5-rows-of-5 button limit: {rows}"
            )
        # The notice never expires, so its buttons can outlive an .env edit.
        # The label rides along so the handler can refuse a stale index rather
        # than logging into whichever account now sits at that position.
        if not all(
            (i or "").endswith(":klerk")
            for i in ids
            if (i or "").startswith(("auth:login:", "auth:snooze:"))
        ):
            failures.append(
                "render: account buttons don't carry the account label, so a "
                "reordered CLAUDE_ACCOUNTS would silently retarget them"
            )
        if any(len(i or "") > 100 for i in ids):
            failures.append("render: custom_id exceeds Discord's 100-char cap")

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


def _test_notice_copy_is_honest() -> list[str]:
    """The notice must not read like a log line, or promise a survivor that isn't.

    Two separate ways this copy went wrong. The reason strings are written for
    the log, so dropping one straight into a sentence produced "is signed out —
    logged out — the CLI rejected its OAuth token (401)": a stutter, and an
    HTTP status aimed at a user on their phone. And the "work keeps running on
    <other>" line listed every *other* configured account without checking
    whether those were signed out too — so in a both-accounts outage each of
    the two notices calmly pointed at the other one as the survivor, while
    nothing could run at all.
    """
    failures: list[str] = []
    saved = list(config.CLAUDE_ACCOUNTS)
    config.CLAUDE_ACCOUNTS[:] = [OTHER, ACCT]
    try:
        body = alerts_mod.build_alert_embed(ACCT, REASON_RUNTIME_401).description or ""
        if "401" in body or "OAuth" in body:
            failures.append(
                f"copy: leaked the raw reason string at the user: {body!r}"
            )
        if "signed out — logged out" in body:
            failures.append("copy: 'is signed out — logged out — ...' stutter")
        if alerts_mod.account_label(OTHER) not in body:
            failures.append(
                "copy: didn't name the account still carrying the work"
            )
        if "Nothing has failed" not in body:
            failures.append(
                "copy: dropped the reassurance while a healthy account remains"
            )

        # Both down: no survivor to point at, and the "nothing needs your
        # attention" line would be flatly untrue.
        both = alerts_mod.build_alert_embed(
            ACCT, REASON_RUNTIME_401, {ACCT, OTHER},
        ).description or ""
        if "Work keeps running" in both:
            failures.append(
                "copy: promised work continues on an account that is itself "
                "signed out"
            )
        if "Nothing has failed" in both:
            failures.append(
                "copy: told the user nothing needs attention while no account "
                "could take a task"
            )
        if "Every configured account is signed out" not in both:
            failures.append("copy: never says that everything is down")

        # A sidelined account that isn't ours doesn't change our story...
        one = alerts_mod.build_alert_embed(ACCT, REASON_RUNTIME_401, {ACCT}).description or ""
        if "Work keeps running" not in one:
            failures.append(
                "copy: treated the reporting account's own sideline as a "
                "second outage, hiding the healthy account"
            )

        # ...and a single-account setup has no survivor by definition.
        config.CLAUDE_ACCOUNTS[:] = [ACCT]
        solo = alerts_mod.build_alert_embed(ACCT, REASON_NO_TOKEN).description or ""
        if "only account configured" not in solo:
            failures.append("copy: single-account outage lost its wording")
    finally:
        config.CLAUDE_ACCOUNTS[:] = saved
    return failures


async def _test_unconfigured_account_is_dropped(store: StateStore) -> list[str]:
    """Removing an account from .env must not leave it nagging from The Ark.

    The runner only ever looks at configured accounts, so an alert for one the
    user has since deleted from CLAUDE_ACCOUNTS can never be resolved — it
    would sit in the table forever and, if the outage was never announced,
    announce itself on the next tick about an account that no longer exists.
    """
    failures: list[str] = []
    channel = _StubChannel()
    bot = _StubBot(store, channel)
    saved = list(config.CLAUDE_ACCOUNTS)
    try:
        config.CLAUDE_ACCOUNTS[:] = [OTHER]  # ACCT was removed from .env
        # Start from no record at all: re-opening the one the drain test left
        # behind would carry its "already announced" flag, and the silence
        # asserted below would prove nothing.
        store.drop_account_alert(ACCT)
        store.set_account_alert(ACCT, REASON_NO_TOKEN, "2026-07-27T10:00:00+00:00")
        await alerts_mod._drain_once(bot)
        if channel.sent:
            failures.append(
                "prune: announced an outage on an account the user had "
                "already removed from CLAUDE_ACCOUNTS"
            )
        if ACCT in store.get_account_alerts():
            failures.append(
                "prune: the record for an unconfigured account survived, so it "
                "would count against the usable-accounts total forever"
            )

        # With no rotation configured at all, CLAUDE_ACCOUNTS is empty for
        # reasons that have nothing to do with the alert — don't wipe the table.
        config.CLAUDE_ACCOUNTS[:] = []
        store.set_account_alert(ACCT, REASON_NO_TOKEN, "2026-07-27T11:00:00+00:00")
        await alerts_mod._drain_once(bot)
        if ACCT not in store.get_account_alerts():
            failures.append(
                "prune: an unset CLAUDE_ACCOUNTS wiped a live alert"
            )
        store.drop_account_alert(ACCT)
    finally:
        config.CLAUDE_ACCOUNTS[:] = saved
    return failures


async def _test_auth_panel_agrees_with_the_ark() -> list[str]:
    """The panel the outage notice links to must not contradict it.

    A server-rejected account leaves a perfectly valid-looking credentials
    file, so every on-disk check calls it signed in. The Ark says "signed out",
    the user taps through to the auth panel, and — before this — saw a green
    tick and a "Re-login" button next to the very account that just failed.

    The names it must agree on matter too, hence the realistic `.claude-x`
    directories: with plain names the panel's raw-directory label and the
    shared short label happen to coincide, and the disagreement this asserts
    on would be invisible.
    """
    failures: list[str] = []
    tmp = tempfile.mkdtemp(prefix="acct_panel_")
    try:
        good = Path(tmp, ".claude-good")
        rejected = Path(tmp, ".claude-rejected")
        for d in (good, rejected):
            d.mkdir()
            (d / ".credentials.json").write_text(
                '{"claudeAiOauth": {"refreshToken": "rt"}}', encoding="utf-8",
            )
        auth_clear_cache()
        dirs = [str(good), str(rejected)]

        baseline = await collect_account_statuses(dirs)
        if not all(s.logged_in for s in baseline):
            failures.append(
                "panel: the on-disk check alone should call both accounts "
                "signed in — this test no longer proves anything"
            )

        statuses = await collect_account_statuses(
            dirs, None, {str(rejected)},
        )
        by_label = {s.label: s for s in statuses}
        expected = {account_label(d) for d in dirs}
        if set(by_label) != expected:
            failures.append(
                f"panel: names accounts {sorted(by_label)} where every other "
                f"surface (Ark notice, /status, dashboard, login terminal) "
                f"says {sorted(expected)} — the user has to work out that "
                f"they're the same account"
            )
            return failures
        if by_label["rejected"].logged_in:
            failures.append(
                "panel: showed a signed-out account as signed in, "
                "contradicting the Ark notice that links here"
            )
        if not by_label["good"].logged_in:
            failures.append(
                "panel: marked a healthy account signed out — the sideline "
                "leaked across accounts"
            )
    finally:
        auth_clear_cache()
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


async def _amain() -> int:
    tmp = tempfile.mkdtemp(prefix="acct_alerts_")
    try:
        store = _fresh_store(tmp)
        all_failures = [
            ("state-machine", _test_state_machine(store)),
            ("drain", await _test_drain(store)),
            ("rendering", _test_rendering()),
            ("notice-copy", _test_notice_copy_is_honest()),
            ("unconfigured-dropped", await _test_unconfigured_account_is_dropped(store)),
            ("auth-panel-agrees", await _test_auth_panel_agrees_with_the_ark()),
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
