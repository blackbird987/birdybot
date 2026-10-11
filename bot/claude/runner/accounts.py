"""Account rotation, cooldowns and the auth sideline."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from bot import config
from bot.claude.auth_health import (
    REASON_RUNTIME_401,
    RUNTIME_REJECTION_REASONS,
    account_label,
    credentials_fingerprint,
    relogin_command,
    unusable_reason,
)
from bot.claude.parser import RunResult
from bot.claude.types import Instance

log = logging.getLogger(__name__)


# How long to wait before re-trying a turn that had nowhere to run and no real
# reset to wait for. Short on purpose: the only thing that can change is the
# user signing an account back in.
REFUSAL_GRACE_MINUTES = 15


class AccountsMixin:
    """Account rotation, cooldowns and the auth sideline. Mixed into ClaudeRunner."""

    def _drop_relogged_cooldowns(
        self, stamped_fps: dict[str, str] | None = None,
    ) -> None:
        """Retire usage cooldowns that belong to a credential no longer in place.

        A usage limit belongs to an *account*, but the cooldown is keyed by
        *directory* — and `/login` can put a DIFFERENT account in the same
        directory.  When that happens the new subscription inherits the old
        one's exhausted quota and every spawn is refused until a reset it never
        hit (2026-08-14).

        A rewritten credentials file is the evidence.  Dropping the cooldown on
        that signal costs at most one spawn — the limit response re-arms it
        immediately if it was still real — while keeping it strands a paid
        account for days.  Same trade ``_clear_auth_cooldown(force=True)``
        already makes for auth sidelines.

        An unstamped entry is treated the same as a mismatched one: it predates
        fingerprinting, so it cannot be shown to still apply, and
        "unverifiable" must not outrank a paid account sitting idle.  Every
        cooldown written from here on carries a stamp, so that branch only
        fires once per upgrade.

        No credentials file at all is the opposite case and must NOT drop: a
        login is what *creates* that file, so its absence is positive evidence
        that nothing was signed in — and the t-3452 guarantee (a reboot must not
        forget a live cooldown) is real.

        Called on every ``_pick_account``, not just at construction: the login
        that rescues a stranded account overwhelmingly happens while the bot is
        *running*, and checking only at startup meant the fix didn't land until
        someone thought to reboot (2026-08-28 — a fresh subscription sat idle
        behind the previous account's limit, "Refusing spawn ... all 1
        account(s) on cooldown", with nothing left to wait for).
        """
        if not self._account_cooldowns:
            return
        if stamped_fps is None:
            if self._store is None:
                return
            try:
                stamped_fps = self._store.get_account_cooldown_fps()
            except Exception:
                log.debug("Failed to read cooldown fingerprints", exc_info=True)
                return
        for acct in list(self._account_cooldowns):
            current = credentials_fingerprint(acct)
            if current is None:
                continue
            stamped = stamped_fps.get(acct)
            if stamped == current:
                continue
            log.info(
                "Dropping usage cooldown for %s — cannot confirm it belongs to "
                "the credential in place now (recorded=%s, current=%s)",
                account_label(acct), stamped or "unstamped", current,
            )
            self._account_cooldowns.pop(acct, None)
            if self._store is not None:
                try:
                    self._store.set_account_cooldown(acct, None)
                except Exception:
                    log.debug("Failed to clear stale cooldown", exc_info=True)

    def _pick_account(
        self,
        exclude: set[str] | None = None,
        prefer: str | None = None,
        avoid_model_cooldown: bool = False,
    ) -> str | None:
        """Return the first configured account not on cooldown/excluded, or None.

        ``prefer`` (if set) is returned first when it is in CLAUDE_ACCOUNTS,
        not on cooldown, and not in ``exclude``.  Falls through to normal
        rotation otherwise — never blocks waiting for the preferred account.
        Used by the dementia fix to honor per-instance session ownership.

        ``avoid_model_cooldown`` prefers accounts whose primary model (e.g.
        Fable) is not on a model-specific cooldown, so runs land where they
        can still use the best model — even ahead of ``prefer`` (session
        hydration copies the JSONL cross-account, so model quality wins over
        session-home convenience).  When every candidate is model-cooled it
        falls back to normal rotation; the caller then applies the
        fallback-model override instead of blocking.
        """
        if not config.CLAUDE_ACCOUNTS:
            return None
        now = datetime.now(timezone.utc)
        # Purge expired cooldowns
        self._account_cooldowns = {
            k: v for k, v in self._account_cooldowns.items() if v > now
        }
        # ...and any left holding a limit that a since-replaced credential hit,
        # so a `/login` takes effect on the next spawn instead of the next boot.
        self._drop_relogged_cooldowns()
        exclude = exclude or set()

        # Credential preflight: an account with no usable refresh token can
        # only ever return a 401, so spawning it wastes a task and (before
        # t-6614) could surface a raw auth error as a build failure.  Skipping
        # it here is free and self-healing — a `/login` rewrites the
        # credentials file, which busts the cached probe on the next pick.
        unusable = self._unusable_accounts()
        # A clean sweep is what engages the safety valve below.  Tracked here,
        # next to the condition itself, rather than at one of the several
        # return paths — otherwise an early return leaves the flag stuck and
        # the next real outage goes unlogged.
        sweep = len(unusable) >= len(config.CLAUDE_ACCOUNTS)
        valve_was_open = self._preflight_valve_open
        if valve_was_open and not sweep:
            log.info("Credential preflight is discriminating again")
        self._preflight_valve_open = sweep

        def _first(skip_model_cooled: bool, skip_unusable: bool) -> str | None:
            if (
                prefer
                and prefer in config.CLAUDE_ACCOUNTS
                and prefer not in exclude
                and prefer not in self._account_cooldowns
                and not (skip_unusable and prefer in unusable)
                and not (skip_model_cooled and prefer in self._model_cooldowns)
            ):
                return prefer
            for acct in config.CLAUDE_ACCOUNTS:
                if acct in exclude:
                    continue
                if acct in self._account_cooldowns:
                    continue
                if skip_unusable and acct in unusable:
                    continue
                if skip_model_cooled and acct in self._model_cooldowns:
                    continue
                return acct
            return None

        if avoid_model_cooldown:
            self._purge_model_cooldowns()
            candidate = _first(skip_model_cooled=True, skip_unusable=True)
            if candidate:
                return candidate
        candidate = _first(skip_model_cooled=False, skip_unusable=True)
        if candidate or not sweep:
            return candidate

        # Safety valve: EVERY configured account failed the credentials probe.
        # The probe is a heuristic (a host that keeps credentials outside the
        # config dir, e.g. macOS Keychain, reads as "logged out" for every
        # account), and a clean sweep is exactly what a wrong heuristic looks
        # like — so it must never be able to take the fleet dark.  Better to
        # spawn and get a real error than to refuse everything.
        #
        # Deliberately NOT fired when only some accounts fail the probe: there
        # the probe is discriminating (one real dead account), and spawning it
        # anyway is the doomed-401 spawn this whole change exists to prevent.
        #
        # Logged on the transition only. The host-keeps-credentials-elsewhere
        # case above never resolves, so a per-spawn warning would be a
        # permanent line in the log that says nothing new.
        if not valve_was_open:
            log.warning(
                "Credential preflight would exclude every account (%s) — "
                "ignoring it and picking normally",
                ", ".join(sorted(account_label(a) for a in unusable)),
            )
        if avoid_model_cooldown:
            candidate = _first(skip_model_cooled=True, skip_unusable=False)
            if candidate:
                return candidate
        return _first(skip_model_cooled=False, skip_unusable=False)

    def _unusable_accounts(self) -> dict[str, str]:
        """{account_dir -> reason} for configured accounts that can't authenticate.

        Logs only when the set changes, so a busy fleet doesn't repeat the
        same line on every spawn.  Also records/clears the persisted alert
        marker that drives the one-time Discord notice.
        """
        found: dict[str, str] = {}
        for acct in config.CLAUDE_ACCOUNTS:
            reason = unusable_reason(acct)
            if reason:
                found[acct] = reason
        if found != self._unusable_seen:
            for acct, reason in found.items():
                if acct not in self._unusable_seen:
                    log.warning(
                        "Account %s skipped: %s", account_label(acct), reason,
                    )
            for acct in self._unusable_seen:
                if acct not in found:
                    log.info("Account %s is usable again", account_label(acct))
                    # Credentials were rewritten with a working token, so an
                    # auth cooldown we set earlier is now stale — drop it or
                    # the account would sit out for the rest of the day
                    # despite being signed in, contradicting what the Ark
                    # notice promises ("rejoins the moment it's signed in").
                    # Usage cooldowns are untouched: those really do end on
                    # the clock.
                    self._clear_auth_cooldown(acct)
            self._unusable_seen = dict(found)
        # Persist alert state so the notifier can announce (and un-announce)
        # this exactly once.  Never notifies from inside the runner — the
        # engine layer owns Discord.
        if self._store:
            now_iso = datetime.now(timezone.utc).isoformat()
            for acct, reason in found.items():
                try:
                    self._store.set_account_alert(
                        acct, reason, now_iso,
                        # Every open alert remembers the file it was judged
                        # against, whatever the reason — that is what lets the
                        # recovery check below be one rule instead of one per
                        # reason string.
                        cred_fp=credentials_fingerprint(acct),
                    )
                except Exception:
                    log.debug("Failed to record account alert", exc_info=True)
            try:
                open_alerts = self._store.get_account_alerts()
            except Exception:
                open_alerts = {}
            for acct in config.CLAUDE_ACCOUNTS:
                if acct in found:
                    continue
                # The credentials read fine now — but "fine" is exactly how a
                # server-rejected token reads too, so the probe alone can't
                # retire a sideline.  What can: the file having been rewritten
                # since the verdict was reached, which is what a `/login` does.
                # Comparing fingerprints is the only recovery signal that
                # survives a reboot; the in-memory record of which cooldowns
                # were auth-kind does not.
                #
                # Only an *open* record is considered.  A resolved one has
                # already been acted on and is merely waiting for the notifier
                # to post its all-clear and drop it — up to a minute later.
                # Re-firing on it during that window would log the same
                # retirement on every spawn, and worse: if the account picked
                # up a genuine usage limit in the meantime, the force-clear
                # below would wipe that cooldown and send the next task
                # straight into the limit it was waiting out.
                existing = open_alerts.get(acct)
                if existing and not existing.get("resolved"):
                    fp = existing.get("cred_fp")
                    same_file = bool(fp) and fp == credentials_fingerprint(acct)
                    if (
                        same_file
                        and existing.get("reason") in RUNTIME_REJECTION_REASONS
                    ):
                        # Same file the server rejected. Only a successful run
                        # clears this one (see _stream_output).
                        continue
                    if not same_file:
                        log.info(
                            "Retiring the sideline on %s (%s): %s",
                            account_label(acct),
                            existing.get("reason") or "unknown",
                            "credentials were rewritten" if fp
                            # No fingerprint means the record predates this
                            # check, so there is nothing to compare against.
                            # Give it the benefit of the doubt rather than
                            # stranding it: if the account is still broken the
                            # next run re-opens the sideline.
                            else "nothing on record to compare against",
                        )
                        # Durable evidence of a fresh login, so this may
                        # override the in-memory auth/usage split — which a
                        # reboot loses entirely.  Worst case it drops a real
                        # usage cooldown that had been hiding behind the
                        # sideline; the next run re-arms it from the limit
                        # response, which is far cheaper than stranding a
                        # working account for the rest of the day.
                        self._clear_auth_cooldown(acct, force=True)
                if acct in self._account_cooldowns:
                    continue
                # Credentials look fine and the account isn't sidelined —
                # resolve rather than delete so the notifier can post the
                # matching all-clear for an outage it announced.
                try:
                    self._store.resolve_account_alert(acct)
                except Exception:
                    log.debug("Failed to resolve account alert", exc_info=True)
        return found

    def reconcile_account_health(self) -> dict[str, str]:
        """Re-probe every account and sync the alert table.  Call once at boot.

        Two things go stale while the bot is down, in opposite directions: an
        account signed out at shutdown may have been signed back in since, and
        one that was fine may be signed out now.  Left alone, the first sits
        sidelined until some task happens to try it, and the second is
        announced only once failover lands on it — which is precisely the
        "nobody notices for weeks" case this whole feature exists to kill.

        Deliberately a thin wrapper: the per-spawn path already knows how to
        open, retire and resolve these records, and a second copy of those
        rules in the startup path is exactly how the two drift apart.
        """
        return self._unusable_accounts()

    def _record_auth_alert(
        self, account_dir: str, reason: str = REASON_RUNTIME_401,
    ) -> None:
        """Persist that an account was sidelined for auth at runtime.

        Only writes state — the engine layer drains it and posts to Discord,
        so the runner keeps no platform dependency.

        *reason* carries which runtime rejection it was, because the notice
        The Ark posts has to tell the user what to actually do about it, and
        "sign in again" is the wrong answer for an org-disabled account.
        """
        self._auth_dead.add(account_dir)
        if not self._store:
            return
        try:
            self._store.set_account_alert(
                account_dir, reason,
                datetime.now(timezone.utc).isoformat(),
                # Which file the server rejected: the only way to recognise a
                # `/login` later, since the rejected file parses fine too.
                cred_fp=credentials_fingerprint(account_dir),
            )
        except Exception:
            log.debug("Failed to record auth alert", exc_info=True)

    def _clear_auth_cooldown(
        self, account_dir: str, *, force: bool = False,
    ) -> None:
        """Drop a cooldown that was applied for auth (not usage), if any.

        ``force`` skips the auth-kind check for callers holding durable proof
        that a fresh login happened (a changed credential fingerprint).  After
        a reboot the in-memory auth/usage split is gone, and without this an
        account would sit out the rest of its 24h sideline despite being signed
        back in — exactly what the Ark notice promises won't happen.
        """
        self._auth_dead.discard(account_dir)
        if not force and account_dir not in self._auth_cooldowns:
            return
        self._auth_cooldowns.discard(account_dir)
        self._account_cooldowns.pop(account_dir, None)
        if self._store is not None:
            try:
                self._store.set_account_cooldown(account_dir, None)
            except Exception:
                log.debug("Failed to clear auth cooldown", exc_info=True)

    def _auth_sideline_is_open(self, account_dir: str) -> bool:
        """Is this account sitting out because auth failed (not a usage cap)?

        ``_auth_cooldowns`` is the in-memory answer and a reboot loses it, so
        an open alert record is consulted as the durable second source, the
        same asymmetry ``_clear_auth_cooldown(force=True)`` exists to paper
        over.  Either one is enough; they can only disagree by being stale in
        the safe direction.

        Only a *runtime* rejection counts as durable evidence, because only
        those are written by the path that also arms the cooldown.  The probe
        reasons (no token, no file, unreadable) open an alert without ever
        arming one, so an account holding a real usage limit while its
        credentials went missing would otherwise read as auth-sidelined here
        and have that limit force-cleared out from under it.
        """
        if account_dir in self._auth_cooldowns or account_dir in self._auth_dead:
            return True
        if self._store is None:
            return False
        try:
            record = self._store.get_account_alerts().get(account_dir)
        except Exception:
            log.debug("Failed to read account alerts", exc_info=True)
            return False
        if not record or record.get("resolved"):
            return False
        return record.get("reason") in RUNTIME_REJECTION_REASONS

    def has_spawnable_account(self) -> bool:
        """Would a spawn find an account to run on, right now?

        Asked by the cooldown-retry loop, which parks an instance on a
        *predicted* reset time and otherwise has no way to learn that the
        prediction went stale.  It is the same `_pick_account()` call the
        refusal branch in `_run_impl` makes, deliberately: a predicate that
        could disagree with the spawn would either fire retries into a refusal
        or leave them parked while the fleet ran.

        Non-failover setups (no CLAUDE_ACCOUNTS) never refuse a spawn for want
        of an account, so there is nothing for them to wait on.
        """
        if not config.CLAUDE_ACCOUNTS:
            return True
        return self._pick_account() is not None

    def retry_account_now(self, account_dir: str) -> str:
        """Put an auth-sidelined account back in rotation immediately.

        The auth cooldown is deliberately a whole day
        (``ACCOUNT_AUTH_COOLDOWN_SECS``) so a dead account costs one wasted
        run per day rather than one per limit-hit.  That is the right default
        and the wrong wait when the user has *just* had access switched back
        on, which is what this is for: it drops the cooldown so the next task
        tries the account, and nothing else.  If it is still dead the next run
        re-sidelines it exactly as before.

        Returns ``"cleared"``, ``"usage"`` (the cooldown is a real usage limit
        with a real reset time, so clearing it would only send the next task
        into the limit it is waiting out), or ``"not_cooled"`` when the
        account was not sitting out in the first place.
        """
        from bot.claude.auth_health import clear_cache as _clear_auth_cache

        # Re-probe the credentials file too: "try now" means "look again at
        # everything about this account", and the on-disk probe is cached.
        # Every account is re-probed, not just this one: the probe is a small
        # cached file read, and a picker that disagrees with the panel the
        # user is looking at costs more than re-reading two files.
        _clear_auth_cache()
        was_sidelined = self._auth_sideline_is_open(account_dir)
        if account_dir not in self._account_cooldowns:
            # Nothing to un-cool.  That is the *sole account* case rather than
            # a healthy one: sidelining the only account we have would stop
            # everything, so that path records the alert and skips the
            # cooldown.  The in-memory dead mark is then the only thing
            # holding it out of rotation (_known_dead_accounts), so dropping
            # it is the whole retry.
            self._auth_dead.discard(account_dir)
            self._auth_cooldowns.discard(account_dir)
            if was_sidelined:
                log.info(
                    "Account %s put back in rotation by hand (no cooldown to "
                    "clear)", account_label(account_dir),
                )
                return "cleared"
            return "not_cooled"
        if not was_sidelined:
            return "usage"
        self._clear_auth_cooldown(account_dir, force=True)
        log.info(
            "Account %s put back in rotation by hand (auth cooldown cleared)",
            account_label(account_dir),
        )
        return "cleared"

    def _known_dead_accounts(self) -> set[str]:
        """Accounts that certainly cannot authenticate right now.

        The union of "the credentials file says so" and "the server said so
        this run" — the two ways an account can be dead while still sitting in
        ``_account_cooldowns`` looking exactly like one that is merely waiting
        out a usage limit.
        """
        dead = set(self._unusable_seen) | set(self._auth_dead)
        for acct in config.CLAUDE_ACCOUNTS:
            if acct not in dead and unusable_reason(acct):
                dead.add(acct)
        return dead

    def _refusal_retry_plan(
        self, logged_out: set[str],
    ) -> tuple[datetime, str | None]:
        """When to come back after refusing to spawn, and what to call it.

        Only a LIVE account's cooldown is a real wall-clock moment: a dead
        account's cooldown expiring changes nothing, and retrying then just
        burns an attempt on the same 401 (t-6614).  With no live cooldown
        there is nothing to wait *for*, so the retry becomes a short grace
        window in case someone signs an account back in — bounded to roughly
        45 minutes by MAX_COOLDOWN_RETRIES.

        The reason it returns is display-only, but it has to be honest: calling
        the grace window "waiting for your main account to reset" invents a
        reset nobody hit, which is the same lie in a different place.
        """
        live = [
            reset for acct, reset in self._account_cooldowns.items()
            if acct not in logged_out
        ]
        if live:
            return min(live), ("backup_logged_out" if logged_out else None)
        grace = datetime.now(timezone.utc) + timedelta(
            minutes=REFUSAL_GRACE_MINUTES
        )
        # "Every account is signed out" has to mean every account.  Any logged
        # -out account at all used to be enough to print it, so a single dead
        # backup made a healthy primary — the one answering at that very
        # moment — read as signed out, and pointed the user at re-authenticating
        # something that was never broken.
        #
        # Three states, not two, because the middle one is the common one and
        # collapsing it either way loses something: called a clean sweep it
        # lies, called "nothing free" it drops the auth panel that would in
        # fact unblock this.  Signing the dead account back in is still the
        # fastest way out, so say so and offer the button.
        if bool(config.CLAUDE_ACCOUNTS) and logged_out >= set(config.CLAUDE_ACCOUNTS):
            return grace, "accounts_logged_out"
        if logged_out:
            return grace, "some_accounts_logged_out"
        return grace, "no_account_free"

    def _soften_auth_dead_end(
        self, result: RunResult, instance: Instance, dead_account: str,
        *, org_disabled: bool = False,
    ) -> None:
        """Turn a nowhere-to-go auth failure into a retry or a useful message.

        A raw ``401 OAuth access token has expired`` as the headline of a
        build failure tells the user nothing they can act on, and throws away
        a turn that would have succeeded twenty minutes later.  So:

        * another account merely cooling down -> schedule the turn to come
          back when the earliest one frees up (a real wall-clock moment), and
        * genuinely nothing left -> say which accounts are logged out and how
          to fix them, instead of echoing the CLI.

        *org_disabled* swaps that last message, for the same reason The Ark
        notice branches on it: an org-disabled account is signed in fine, so
        "logged out (OAuth expired), re-auth with ..." names the wrong fault
        and prescribes the one fix that provably cannot work.

        Accounts that are themselves logged out are excluded from the first
        branch even though they hold a cooldown entry.  Waiting on one is not a
        real wall-clock moment — it burns all three retries and then fails with
        the same 401, while telling the user we're "waiting for a reset" they
        never hit.
        """
        now = datetime.now(timezone.utc)
        dead = self._known_dead_accounts() | {dead_account}
        others = {
            acct: reset
            for acct, reset in self._account_cooldowns.items()
            if acct not in dead
            and acct in config.CLAUDE_ACCOUNTS
            and reset > now
        }
        if others:
            earliest = min(others.values())
            result.usage_limit_reset = earliest
            result.session_id = result.session_id or instance.session_id
            result.retry_reason = "backup_logged_out"
            log.warning(
                "Account %s is logged out and no failover target is free for "
                "%s — scheduling retry at %s instead of failing",
                account_label(dead_account), instance.id, earliest.isoformat(),
            )
            return

        ordered = [a for a in config.CLAUDE_ACCOUNTS if a in dead] or [dead_account]
        labels = ", ".join(f"'{account_label(a)}'" for a in ordered)
        verb = "is" if len(ordered) == 1 else "are"
        if org_disabled:
            result.error_message = (
                f"Account '{account_label(dead_account)}' can't run: its "
                f"organization has Claude Code switched off, and there is no "
                f"other account to fall back to.\n"
                f"Signing in again won't fix this one, it is signed in fine. "
                f"An admin has to re-enable access; the bot retries the "
                f"account about once a day and takes it back by itself, or "
                f"use Try now in /auth."
            )
        else:
            result.error_message = (
                f"Account {labels} {verb} logged out (OAuth expired) and there "
                f"is no other account to fall back to.\n"
                f"Re-auth with: {relogin_command(ordered[0])}  then /login "
                f"inside the CLI, or drop it from CLAUDE_ACCOUNTS."
            )
        result.result_text = ""
        log.error(
            "Accounts %s unusable (%s) and nothing is left to fall back to (%s)",
            labels,
            "org-disabled" if org_disabled else "logged out",
            instance.id,
        )

    def _set_account_cooldown(self, account_dir: str, reset_at: datetime) -> None:
        """Record cooldown both in memory and on disk via the StateStore.

        Routing every cooldown write through here is what gives the
        persistence layer its single source of truth — direct dict writes
        would skip the save() and reintroduce the t-3452 reboot gap.
        """
        self._account_cooldowns[account_dir] = reset_at
        # Any new cooldown is a usage cooldown unless the caller says otherwise
        # (it re-adds straight after), so a stale auth mark can't make a real
        # rate-limit wait look clearable by logging in.
        self._auth_cooldowns.discard(account_dir)
        if self._store is not None:
            try:
                self._store.set_account_cooldown(
                    account_dir,
                    reset_at.isoformat(),
                    # Which credential hit the limit. Lets a later login into
                    # this same directory retire the cooldown instead of
                    # inheriting it (see __init__).
                    credentials_fingerprint(account_dir),
                )
            except Exception:
                log.exception(
                    "Failed to persist account cooldown for %s",
                    account_dir[-20:] if account_dir else "?",
                )

    def _set_model_cooldown(self, account_dir: str, reset_at: datetime) -> None:
        """Record a primary-model (e.g. Fable) cooldown in memory + on disk.

        Same single-source-of-truth routing as _set_account_cooldown.
        """
        self._model_cooldowns[account_dir] = reset_at
        if self._store is not None:
            try:
                self._store.set_model_cooldown(account_dir, reset_at.isoformat())
            except Exception:
                log.exception(
                    "Failed to persist model cooldown for %s",
                    account_dir[-20:] if account_dir else "?",
                )

    def _purge_model_cooldowns(self) -> None:
        """Drop elapsed model cooldowns (memory + disk) — this is what makes
        the switch BACK to the primary model automatic once its limit resets.
        """
        now = datetime.now(timezone.utc)
        expired = [k for k, v in self._model_cooldowns.items() if v <= now]
        for k in expired:
            self._model_cooldowns.pop(k, None)
            if self._store is not None:
                try:
                    self._store.set_model_cooldown(k, None)
                except Exception:
                    log.exception("Failed to clear model cooldown for %s", k[-20:])
