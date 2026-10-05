"""A usage cooldown must not outlive the credential that earned it.

The cooldown table is keyed by *directory*, but a usage limit belongs to an
*account* — and `/login` can put a different account in the same directory.
That happened on 2026-08-14: a new subscription was signed into ``~/.claude``,
inherited the previous account's weekly-limit cooldown (set hours earlier, and
running until Aug 17), and every spawn was refused on a subscription with a
completely untouched quota.  Nothing on disk could clear it — the account was
"cooled down" for a limit it had never hit.

The fix stamps each cooldown with the credentials fingerprint it was recorded
against (``account_cooldown_fps`` in the store) and drops any cooldown at load
that cannot be tied to the credential currently in place.

The interesting part is the boundary, which is why case 5 exists: the t-3452
guarantee says a reboot must NOT forget a live cooldown, so "can't verify"
cannot simply mean "drop".  A *missing* credentials file is positive evidence
that nobody logged in — a login is what creates that file — so those cooldowns
survive.  Only a file that exists and doesn't match the stamp is proof the
credential moved on.

Run: ``python scripts/test_cooldown_credential_swap.py``  (exit 0 on pass).
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot import config
from bot.claude import auth_health
from bot.claude.runner import ClaudeRunner
from bot.store.state import StateStore


def _make_account(root: Path, name: str, token: str | None) -> str:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    if token is not None:
        (d / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshToken": token}}), encoding="utf-8",
        )
    return str(d)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cooldown-fp-"))
    saved_accounts = list(config.CLAUDE_ACCOUNTS)
    failures: list[str] = []
    try:
        future = (datetime.now(timezone.utc) + timedelta(hours=48)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

        match = _make_account(tmp, "acct-match", "tok-a")
        changed = _make_account(tmp, "acct-changed", "tok-b")
        legacy = _make_account(tmp, "acct-legacy", "tok-c")
        expired = _make_account(tmp, "acct-expired", "tok-d")
        nocred = _make_account(tmp, "acct-nocred", None)

        store = StateStore(tmp / "state.json", tmp / "results")

        # 1 + 4: stamped against the credential that is still in place.
        store.set_account_cooldown(
            match, future, auth_health.credentials_fingerprint(match),
        )
        store.set_account_cooldown(
            expired, past, auth_health.credentials_fingerprint(expired),
        )

        # 2: stamped, then the credentials file is rewritten — what /login does.
        store.set_account_cooldown(
            changed, future, auth_health.credentials_fingerprint(changed),
        )
        (Path(changed) / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshToken": "tok-b-NEW-ACCT-LONGER"}}),
            encoding="utf-8",
        )

        # 3: recorded before fingerprints were tracked.
        store.set_account_cooldown(legacy, future, None)
        # 5: cooled, but no credentials file exists at all.
        store.set_account_cooldown(nocred, future, None)

        auth_health.clear_cache()
        config.set_accounts([match, changed, legacy, expired, nocred])

        runner = ClaudeRunner(store=store)
        live = set(runner._account_cooldowns)

        if match not in live:
            failures.append(
                "stamp matches the credential on disk but the cooldown was "
                "dropped — a real limit would be walked straight back into"
            )
        if changed in live:
            failures.append(
                "credential was rewritten (re-login) and the old account's "
                "cooldown survived — the bug: a paid account benched for days"
            )
        if legacy in live:
            failures.append(
                "unstamped legacy cooldown survived, so the upgrade cannot "
                "recover an account already stuck behind one"
            )
        if expired in live:
            failures.append("an already-elapsed cooldown was loaded as live")
        if nocred not in live:
            failures.append(
                "cooldown dropped for an account with no credentials file — "
                "no login can have happened, so this breaks the t-3452 "
                "guarantee that a reboot remembers live cooldowns"
            )

        persisted = store.get_account_cooldowns()
        if changed in persisted or legacy in persisted:
            failures.append(
                "stale cooldown cleared in memory but not on disk — the next "
                "restart would bench the account all over again"
            )
        if match not in persisted:
            failures.append("a still-valid cooldown was erased from the store")

        # The point of all this: the picker must offer the recovered account
        # rather than refusing every spawn.  `match` is legitimately still
        # cooled, so `changed` — the re-logged-in one — is the expected pick.
        picked = runner._pick_account()
        if picked != changed:
            failures.append(
                f"picker returned {picked!r}; expected the re-logged-in "
                f"account {changed!r} to be available again"
            )

        # And a second restart must not resurrect what was cleared.
        if changed in ClaudeRunner(store=store)._account_cooldowns:
            failures.append("a second restart resurrected the cleared cooldown")

        # 6: the login happens while the bot is UP — the common case, and the
        # one that checking only at construction missed (2026-08-28).  `match`
        # is still legitimately cooled here; signing a new account into its
        # directory has to free it on the very next pick, with no reboot.
        if match not in runner._account_cooldowns:
            failures.append(
                "precondition broken: `match` should still be cooled before "
                "the mid-run login"
            )
        (Path(match) / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"refreshToken": "tok-a-NEW-ACCT-LONGER"}}),
            encoding="utf-8",
        )
        picked = runner._pick_account()
        if picked != match:
            failures.append(
                f"picker returned {picked!r} after a mid-run login into "
                f"{match!r}; the re-logged-in account stayed benched until a "
                f"reboot — the whole bug"
            )
        if match in store.get_account_cooldowns():
            failures.append(
                "mid-run login cleared the cooldown in memory but not on "
                "disk — the next restart would bench the account again"
            )
    finally:
        config.set_accounts(saved_accounts)
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("FAIL: cooldown credential-swap tests")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: cooldown credential-swap tests (10 checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
