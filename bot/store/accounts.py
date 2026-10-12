"""Account cooldowns and the account alert table (multi-account failover memory).

A mixin of ``StateStore`` (bot/store/state.py), which owns the dicts these
methods read and the persistence that saves them.
"""

from __future__ import annotations


class AccountsMixin:
    # --- Account Cooldowns (multi-account failover memory) ---

    def get_account_cooldowns(self) -> dict[str, str]:
        """Return a copy of {account_dir -> ISO reset timestamp}.

        Caller is responsible for parsing the timestamps and purging entries
        that have already elapsed; this store keeps strings to avoid coupling
        the persistence layer to datetime semantics.
        """
        return dict(self._account_cooldowns)

    def get_account_cooldown_fps(self) -> dict[str, str]:
        """Return {account_dir -> credentials fingerprint at cooldown time}.

        Pairs with :meth:`get_account_cooldowns`.  An entry missing here for a
        live cooldown means "recorded before fingerprints were tracked" — the
        caller decides what to do with that, since only it knows how expensive
        being wrong is.
        """
        return dict(self._account_cooldown_fps)

    def set_account_cooldown(
        self, account_dir: str, reset_iso: str | None, cred_fp: str | None = None,
    ) -> None:
        """Record (or clear with None) a usage-limit reset for an account.

        Persists immediately so a reboot between cooldown-set and the next
        spawn cannot lose the memory — that gap is exactly what allowed the
        t-3452 dementia (fresh runner had empty cooldowns, picked the
        already-exhausted primary, hit "No conversation found", fell through
        to Layer 3 which dropped the session).

        ``cred_fp`` stamps which credential the limit was hit on, so a later
        login into the same directory can be told apart from a restart.
        """
        if reset_iso is None:
            self._account_cooldowns.pop(account_dir, None)
            self._account_cooldown_fps.pop(account_dir, None)
        else:
            self._account_cooldowns[account_dir] = reset_iso
            if cred_fp is None:
                self._account_cooldown_fps.pop(account_dir, None)
            else:
                self._account_cooldown_fps[account_dir] = cred_fp
        self.save()

    def get_model_cooldowns(self) -> dict[str, str]:
        """Return a copy of {account_dir -> ISO reset} for the primary model.

        Same string-based contract as get_account_cooldowns — caller parses
        and purges elapsed entries.
        """
        return dict(self._model_cooldowns)

    def set_model_cooldown(self, account_dir: str, reset_iso: str | None) -> None:
        """Record (or clear with None) a model-specific limit reset.

        Persists immediately for the same reason as account cooldowns: a
        reboot must not forget the primary model is limited, or the first
        post-restart spawn burns a doomed CLI invocation on it.
        """
        if reset_iso is None:
            self._model_cooldowns.pop(account_dir, None)
        else:
            self._model_cooldowns[account_dir] = reset_iso
        self.save()

    # --- Account Alerts (auth sideline notices) ---

    def get_account_alerts(self) -> dict[str, dict]:
        """Return a copy of {account_dir -> alert record}.

        Record shape: ``{"reason": str, "since": ISO, "notified": bool,
        "resolved": bool, "snooze_until": ISO|None, "cred_fp": str|None}``.

        The record is a small state machine drained by the notifier loop:
        open+unnotified -> announce; resolved+notified -> all-clear then drop;
        resolved+unannounced -> drop silently (nothing to un-say).  Writers
        (the runner) only ever open or resolve records — they never post, so
        the engine layer keeps sole ownership of Discord.
        """
        return {k: dict(v) for k, v in self._account_alerts.items()}

    def sidelined_account_reasons(self) -> dict[str, str]:
        """{account_dir -> reason} for every open (unresolved) auth alert.

        The only record of an account the *server* rejected — its credentials
        file still parses fine, so no on-disk check can see it.  Read by the
        "how healthy is the fleet?" surfaces so they agree with The Ark.

        The reason rides along because the advice differs by it: an account
        with no saved login is answered by signing in, an org-disabled one
        provably is not.  A surface that only needs the names uses
        ``sidelined_accounts``, which is this with the reasons dropped.
        """
        return {
            k: (v.get("reason") or "")
            for k, v in self._account_alerts.items()
            if not v.get("resolved")
        }

    def sidelined_accounts(self) -> set[str]:
        """The names of the accounts with an open auth alert."""
        return set(self.sidelined_account_reasons())

    def set_account_alert(
        self,
        account_dir: str,
        reason: str,
        since_iso: str,
        cred_fp: str | None = None,
    ) -> bool:
        """Open (or re-open) an auth sideline for an account. True if new.

        Re-marking an already-open alert is a no-op that keeps the original
        ``since`` and the ``notified``/``snooze_until`` flags — that is what
        stops a burst of failed tasks from re-notifying.  An account that
        breaks again before its all-clear went out simply re-opens.

        ``cred_fp`` fingerprints the credentials file the verdict was reached
        against, so a later reader can tell "still the same rejected file" from
        "someone logged in since".  It belongs to the verdict, not to the
        reason: it is refreshed on every re-mark that supplies one, including a
        re-open with the reason unchanged.  Keeping the old value there would
        strand the account in a loop — the reader would see a fingerprint that
        no longer matches the file, conclude a login had happened, un-sideline
        it, watch it fail again, and repeat on every spawn.  None means "no
        opinion" — which is what both writers pass for an account with no
        credentials file at all — and leaves whatever is on record alone
        rather than blanking it.
        """
        existing = self._account_alerts.get(account_dir)
        if existing is not None:
            changed = False
            if existing.get("reason") != reason:
                existing["reason"] = reason
                changed = True
            if cred_fp is not None and existing.get("cred_fp") != cred_fp:
                existing["cred_fp"] = cred_fp
                changed = True
            if existing.get("resolved"):
                existing["resolved"] = False
                changed = True
            if changed:
                self.save()
            return False
        self._account_alerts[account_dir] = {
            "reason": reason,
            "since": since_iso,
            "notified": False,
            "resolved": False,
            "snooze_until": None,
            "cred_fp": cred_fp,
        }
        self.save()
        return True

    def resolve_account_alert(self, account_dir: str) -> None:
        """Mark an account healthy again, leaving the all-clear for the notifier.

        An alert that was never announced is dropped outright — there is no
        point telling the user an account recovered from an outage they were
        never told about.
        """
        record = self._account_alerts.get(account_dir)
        if record is None or record.get("resolved"):
            return
        if not record.get("notified"):
            self._account_alerts.pop(account_dir, None)
        else:
            record["resolved"] = True
        self.save()

    def drop_account_alert(self, account_dir: str) -> None:
        """Remove an alert record entirely (notifier, after the all-clear)."""
        if self._account_alerts.pop(account_dir, None) is not None:
            self.save()

    def mark_account_alert_notified(self, account_dir: str) -> None:
        """Record that the sideline notice was delivered for this account.

        Clears any elapsed snooze at the same time — the reminder the user
        asked for has now been delivered, so the next "Ignore for now" starts
        a fresh window instead of finding a stale one already in the past.
        """
        record = self._account_alerts.get(account_dir)
        if record is None:
            return
        record["notified"] = True
        record["snooze_until"] = None
        self.save()

    def snooze_account_alert(self, account_dir: str, until_iso: str) -> bool:
        """Suppress further notices for this account until *until_iso*.

        False when there is no open alert to snooze — the account recovered
        between the notice being posted and the button being pressed, so the
        caller should say that rather than claim it muted something.
        """
        record = self._account_alerts.get(account_dir)
        if record is None or record.get("resolved"):
            return False
        record["snooze_until"] = until_iso
        record["notified"] = True
        self.save()
        return True
