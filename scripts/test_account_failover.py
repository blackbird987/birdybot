"""Tests for account-unusable failover (cancelled-subscription handling).

Covers the second cross-account failover trigger added so the bot keeps
working when one Claude subscription is cancelled (auth/subscription error,
NOT a usage limit).  See bot/claude/runner.py (the block before `return
result`) and bot/claude/parser.py classifiers.

Two layers:

1. Pure classifier unit tests — no async, no subprocess:
   - is_account_unusable_error matches auth strings, rejects transient/usage.
   - is_account_agnostic_error catches model/flag errors.

2. Integration tests against the real _run_impl failover branch, faking only
   the CLI subprocess boundary (asyncio.create_subprocess_exec +
   _stream_output), mirroring scripts/test_failover_session.py:
   - confident auth error -> cooldown set + account switched.
   - account-agnostic "currently unavailable" -> NO failover, NO cooldown.
   - both accounts dead -> each tried once, error returned, terminates
     (no infinite recursion / all-cooled deadlock).
   - run() resets a stale _accounts_tried from a prior run.
   - usage limit on primary + auth-dead backup -> result carries the
     original usage_limit_reset (auto-retry countdown, not a raw 401)
     and the backup gets the long ACCOUNT_AUTH_COOLDOWN_SECS sideline.

3. t-6614 — a signed-out account must never hard-fail a task:
   - _no_productive_work sees a 401 that the CLI wrote into result_text
     (the empty-text guard it replaces missed exactly this).
   - credential probe classifies a token-less file and self-heals on login.
   - t-6570 replay: limit + 401-in-result_text -> retry scheduled.
   - token-less backup is skipped before it costs a spawn, and is recorded
     for the Discord notice.
   - all accounts failing the probe -> safety valve still spawns.
   - nothing left -> actionable message naming the account, not a raw 401.
   - a runtime 401 sideline is not auto-cleared by re-reading the same file.
   - that sideline still retires on a login after a bot restart, when the
     in-memory record of why it was applied is gone.

Run: ``python scripts/test_account_failover.py``  (exit 0 on pass).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude.auth_health import (
    REASON_NO_TOKEN,
    REASON_RUNTIME_401,
    account_label,
    clear_cache as clear_auth_cache,
    credentials_usable,
    unusable_reason,
)
from bot.claude.parser import (
    is_account_agnostic_error,
    is_account_unusable_error,
)
from bot.claude.runner import ClaudeRunner, _no_productive_work
from bot.claude.types import Instance, InstanceStatus, InstanceType, RunResult


# ---------------------------------------------------------------------------
# Layer 1: pure classifier unit tests
# ---------------------------------------------------------------------------

def _test_classifiers() -> list[str]:
    failures: list[str] = []

    unusable_yes = [
        "Invalid API key · Please run /login",
        "OAuth token has expired",
        "Your authentication failed",
        "401 Unauthorized",
        "No active subscription found",
        "Your subscription has expired",
        "Credit balance is too low",
        "Please sign in again",
    ]
    for s in unusable_yes:
        if not is_account_unusable_error(s):
            failures.append(f"is_account_unusable_error should match: {s!r}")

    unusable_no = [
        "",
        "rate limit exceeded",          # transient
        "connection refused",            # transient
        "You've hit your usage limit · resets 5pm",  # usage cap
        "hit your weekly limit",         # usage cap
        "Some normal completion text",
    ]
    for s in unusable_no:
        if is_account_unusable_error(s):
            failures.append(f"is_account_unusable_error should NOT match: {s!r}")

    agnostic_yes = [
        "Claude Fable 5 is currently unavailable.",
        "model not found",
        "unknown model: foo",
        "unrecognized arguments: --bogus",
        "usage: claude [-h] ...",
    ]
    for s in agnostic_yes:
        if not is_account_agnostic_error(s):
            failures.append(f"is_account_agnostic_error should match: {s!r}")

    agnostic_no = ["", "Invalid API key", "rate limit"]
    for s in agnostic_no:
        if is_account_agnostic_error(s):
            failures.append(f"is_account_agnostic_error should NOT match: {s!r}")

    return failures


# ---------------------------------------------------------------------------
# Layer 2: integration harness (fakes only the subprocess boundary)
# ---------------------------------------------------------------------------

class _FakeStdin:
    def write(self, data):
        return None

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None


class _FakeProc:
    _next_pid = 95001

    def __init__(self):
        self.pid = _FakeProc._next_pid
        _FakeProc._next_pid += 1
        self.returncode = 0
        self.stdin = _FakeStdin()
        self.stdout = None
        self.stderr = None

    def kill(self):
        return None

    async def wait(self):
        return 0


class _FakeStore:
    """Just enough StateStore to exercise the account-alert state machine."""

    def __init__(self):
        self.alerts: dict[str, dict] = {}
        self.cooldowns: dict[str, str] = {}

    # --- cooldown persistence (runner reads at init, writes on sideline) ---
    def get_account_cooldowns(self):
        return dict(self.cooldowns)

    def get_model_cooldowns(self):
        return {}

    def set_account_cooldown(self, account_dir, reset_iso):
        # None means "clear" in the real store — mirroring that matters, or a
        # test asserting on the persisted table would see a cleared cooldown
        # still sitting there as a None value.
        if reset_iso is None:
            self.cooldowns.pop(account_dir, None)
        else:
            self.cooldowns[account_dir] = reset_iso

    def set_model_cooldown(self, account_dir, reset_iso):
        pass

    # --- alert state machine ---
    def get_account_alerts(self):
        return {k: dict(v) for k, v in self.alerts.items()}

    def set_account_alert(self, account_dir, reason, since_iso, cred_fp=None):
        rec = self.alerts.get(account_dir)
        if rec is not None:
            # Mirrors StateStore: the fingerprint belongs to the verdict, so a
            # re-mark that supplies one refreshes it (a stale one would flap
            # the account in and out of the sideline), and one that supplies
            # nothing leaves the record alone.
            if cred_fp is not None:
                rec["cred_fp"] = cred_fp
            rec["reason"] = reason
            rec["resolved"] = False
            return False
        self.alerts[account_dir] = {
            "reason": reason, "since": since_iso, "cred_fp": cred_fp,
            "notified": False, "resolved": False, "snooze_until": None,
        }
        return True

    def resolve_account_alert(self, account_dir):
        rec = self.alerts.get(account_dir)
        if rec is None or rec.get("resolved"):
            return
        if not rec.get("notified"):
            self.alerts.pop(account_dir, None)
        else:
            rec["resolved"] = True


def _make_instance(repo_dir: str) -> Instance:
    # No session_id -> hydration skipped (this suite tests the failover
    # trigger + cooldown, not session preservation).  No branch -> worktree
    # setup skipped.
    return Instance(
        id="t-acct",
        name=None,
        instance_type=InstanceType.QUERY,
        prompt="do a thing",
        repo_name="test-repo",
        repo_path=repo_dir,
        status=InstanceStatus.RUNNING,
        mode="explore",
    )


def _write_credentials(
    account_dir: str, *, logged_in: bool, token: str = "rt-test",
) -> None:
    """Fake a CLAUDE_CONFIG_DIR that the credential preflight accepts/rejects.

    ``logged_in=False`` reproduces the real t-6614 shape: the file exists (so
    the old existence-only check passed) but carries no refresh token.

    ``token`` varies the refresh token so a simulated ``/login`` changes the
    file's *size* — the fingerprint then differs regardless of the filesystem's
    mtime resolution, which two writes in the same millisecond would not.
    """
    payload = {"claudeAiOauth": {"accessToken": "at-test"}}
    if logged_in:
        payload["claudeAiOauth"]["refreshToken"] = token
    Path(account_dir, ".credentials.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


async def _run_with_streams(
    stream_results, *, accounts, logged_out=(), setup=None,
):
    """Run a fresh instance through runner.run(), faking _stream_output to
    yield ``stream_results`` (a list of RunResult) per spawn in order; the
    last entry is reused if more spawns happen.

    ``logged_out`` names accounts whose credentials file has no refresh token.
    ``setup(runner, instance, acct_dirs)`` runs just before the turn starts,
    for tests that need pre-existing cooldowns or already-tried accounts.

    Returns (result, instance, runner, acct_dirs, spawn_count).
    """
    tmp = tempfile.mkdtemp(prefix="acct_failover_")
    acct_dirs = [os.path.join(tmp, name) for name in accounts]
    repo_dir = os.path.join(tmp, "repo")
    for p in (*acct_dirs, repo_dir):
        os.makedirs(p, exist_ok=True)
    for name, path in zip(accounts, acct_dirs):
        _write_credentials(path, logged_in=name not in logged_out)
    clear_auth_cache()

    saved_accounts = list(config.CLAUDE_ACCOUNTS)
    saved_spawn = asyncio.create_subprocess_exec
    config.CLAUDE_ACCOUNTS[:] = acct_dirs

    spawn_calls: list[list[str]] = []

    async def fake_spawn(*args, **_kwargs):
        spawn_calls.append(list(args))
        return _FakeProc()

    asyncio.create_subprocess_exec = fake_spawn  # type: ignore[assignment]

    runner = ClaudeRunner(store=_FakeStore())
    calls = {"n": 0}

    async def fake_stream_output(proc, instance, on_progress, on_stall, **kw):
        i = min(calls["n"], len(stream_results) - 1)
        calls["n"] += 1
        return stream_results[i]

    runner._stream_output = fake_stream_output  # type: ignore[assignment]

    instance = _make_instance(repo_dir)
    if setup:
        setup(runner, instance, acct_dirs)
    try:
        result = await runner.run(instance)
    finally:
        asyncio.create_subprocess_exec = saved_spawn  # type: ignore[assignment]
        config.CLAUDE_ACCOUNTS[:] = saved_accounts
        clear_auth_cache()
        shutil.rmtree(tmp, ignore_errors=True)

    return result, instance, runner, acct_dirs, len(spawn_calls)


async def _test_confident_failover() -> list[str]:
    """Confident auth error -> cooldown on primary + switch to backup."""
    failures: list[str] = []
    results = [
        RunResult(is_error=True,
                  error_message="Invalid API key · Please run /login",
                  result_text="Invalid API key · Please run /login"),
        RunResult(is_error=False, result_text="ok"),
    ]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"]
    )
    primary, backup = accts
    if spawns < 2:
        failures.append(f"confident: expected >=2 spawns, got {spawns}")
    if primary not in instance._accounts_tried:
        failures.append("confident: primary not added to _accounts_tried")
    if primary not in runner._account_cooldowns:
        failures.append("confident: primary not put on cooldown")
    if result.is_error:
        failures.append(f"confident: final result errored: {result.error_message!r}")
    return failures


async def _test_agnostic_no_failover() -> list[str]:
    """Account-agnostic 'currently unavailable' -> NO failover, NO cooldown."""
    failures: list[str] = []
    results = [
        RunResult(is_error=True,
                  error_message="Claude model X is currently unavailable.",
                  result_text=""),
    ]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"]
    )
    primary, backup = accts
    if spawns != 1:
        failures.append(f"agnostic: expected exactly 1 spawn, got {spawns}")
    if runner._account_cooldowns:
        failures.append(
            f"agnostic: no cooldown expected, got {list(runner._account_cooldowns)}"
        )
    if not result.is_error:
        failures.append("agnostic: expected the raw error to be returned")
    return failures


async def _test_both_dead_terminates() -> list[str]:
    """Both accounts auth-dead -> each tried once, error returned, no hang."""
    failures: list[str] = []
    results = [
        RunResult(is_error=True,
                  error_message="OAuth token has expired",
                  result_text="OAuth token has expired"),
    ]  # reused for every spawn
    try:
        result, instance, runner, accts, spawns = await asyncio.wait_for(
            _run_with_streams(results, accounts=["primary", "backup"]),
            timeout=10,
        )
    except asyncio.TimeoutError:
        return ["both-dead: run did NOT terminate (possible infinite recursion)"]

    primary, backup = accts
    if spawns != 2:
        failures.append(f"both-dead: expected exactly 2 spawns, got {spawns}")
    if not (primary in runner._account_cooldowns
            and backup in runner._account_cooldowns):
        failures.append("both-dead: expected BOTH accounts on cooldown")
    if not result.is_error:
        failures.append("both-dead: expected final error result")
    # Every account is logged out, so their auth cooldowns are NOT wall-clock
    # moments when anything frees up. Scheduling a retry against them would
    # burn all three attempts on the same 401 while telling the user we're
    # "waiting for a reset" they never hit.
    if result.usage_limit_reset is not None:
        failures.append(
            "both-dead: scheduled a retry against accounts that are all "
            "logged out — that waits a day and then fails identically"
        )
    if "logged out" not in (result.error_message or ""):
        failures.append(
            f"both-dead: message isn't actionable: {result.error_message!r}"
        )
    for label in ("primary", "backup"):
        if label not in (result.error_message or ""):
            failures.append(
                f"both-dead: message doesn't name the {label} account"
            )
    return failures


async def _test_limit_then_dead_backup_keeps_reset() -> list[str]:
    """Usage limit on primary, backup auth-dead (paused subscription).

    The final result must carry the primary's usage_limit_reset so the
    caller schedules the normal auto-retry countdown instead of surfacing
    the backup's raw 401 — the exact t-5925 incident (2026-07-05).
    """
    failures: list[str] = []
    results = [
        RunResult(is_error=True,
                  error_message=(
                      "You've hit your usage limit · resets 5pm"
                  ),
                  result_text=""),
        RunResult(is_error=True,
                  error_message=(
                      "Failed to authenticate. API Error: 401 "
                      "Invalid authentication credentials"
                  ),
                  result_text=""),
    ]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"]
    )
    primary, backup = accts
    if spawns != 2:
        failures.append(f"limit+dead: expected exactly 2 spawns, got {spawns}")
    if not result.is_error:
        failures.append("limit+dead: expected an error result")
    if not result.usage_limit_reset:
        failures.append(
            "limit+dead: usage_limit_reset missing — user would see the raw "
            "401 instead of the auto-retry countdown"
        )
    elif result.usage_limit_reset != runner._account_cooldowns.get(primary):
        failures.append(
            "limit+dead: usage_limit_reset should be the PRIMARY's reset time"
        )
    backup_cd = runner._account_cooldowns.get(backup)
    if backup_cd is None:
        failures.append("limit+dead: backup not put on cooldown")
    else:
        from datetime import datetime, timezone
        secs = (backup_cd - datetime.now(timezone.utc)).total_seconds()
        if abs(secs - config.ACCOUNT_AUTH_COOLDOWN_SECS) > 60:
            failures.append(
                f"limit+dead: backup cooldown {secs:.0f}s, expected "
                f"~ACCOUNT_AUTH_COOLDOWN_SECS ({config.ACCOUNT_AUTH_COOLDOWN_SECS}s)"
            )
    return failures


async def _test_long_output_never_sidelines_account() -> list[str]:
    """A failed session that WRITES about 401s must not sideline its account.

    The classifier reads ``error_message or result_text``, which is the only
    reason the t-6570 401 was found at all — and also why a session in this
    very repo, whose output discusses expired OAuth tokens at length, could
    have cooled its own account down for 24 hours and posted a "signed out"
    notice about an account that is perfectly fine.
    """
    failures: list[str] = []
    essay = (
        "I traced the failover bug. The CLI emits 'Failed to authenticate. "
        "API Error: 401 OAuth access token has expired. Re-authenticate to "
        "continue.' into the result stream rather than the error field, so "
        "the runner counted it as a productive turn. " + "Notes. " * 40
    )
    results = [
        RunResult(is_error=True, result_text=essay, num_turns=14),
    ]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"],
    )
    primary, backup = accts
    if spawns != 1:
        failures.append(
            f"long-output: failed over on a real session failure ({spawns} spawns)"
        )
    if runner._account_cooldowns:
        failures.append(
            "long-output: cooled an account down because its session text "
            "mentioned OAuth — the account is fine"
        )
    if runner._store.alerts:
        failures.append(
            "long-output: posted a signed-out alert for a healthy account"
        )
    if result.usage_limit_reset is not None:
        failures.append(
            "long-output: replaced a genuine failure with a retry countdown"
        )
    return failures


async def _test_run_resets_accounts_tried() -> list[str]:
    """run() clears a stale _accounts_tried from a prior run."""
    failures: list[str] = []
    tmp = tempfile.mkdtemp(prefix="acct_reset_")
    primary = os.path.join(tmp, "primary")
    repo_dir = os.path.join(tmp, "repo")
    for p in (primary, repo_dir):
        os.makedirs(p, exist_ok=True)

    saved_accounts = list(config.CLAUDE_ACCOUNTS)
    saved_spawn = asyncio.create_subprocess_exec
    config.CLAUDE_ACCOUNTS[:] = [primary]

    async def fake_spawn(*args, **_kwargs):
        return _FakeProc()

    asyncio.create_subprocess_exec = fake_spawn  # type: ignore[assignment]
    runner = ClaudeRunner()

    async def fake_stream_output(proc, instance, on_progress, on_stall, **kw):
        return RunResult(is_error=False, result_text="ok")

    runner._stream_output = fake_stream_output  # type: ignore[assignment]

    instance = _make_instance(repo_dir)
    instance._accounts_tried = {"/stale/account/from/prior/run"}
    try:
        await runner.run(instance)
        if "/stale/account/from/prior/run" in instance._accounts_tried:
            failures.append("run() did not reset stale _accounts_tried")
    finally:
        asyncio.create_subprocess_exec = saved_spawn  # type: ignore[assignment]
        config.CLAUDE_ACCOUNTS[:] = saved_accounts
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


# ---------------------------------------------------------------------------
# t-6614: signed-out backup must never hard-fail a task
# ---------------------------------------------------------------------------

def _test_no_productive_work() -> list[str]:
    """The guard that decides 'did the backup actually do anything?'.

    The t-6570 bug was an empty-text test: the CLI writes its fatal 401 into
    the result text, so the bot concluded real work had happened.
    """
    failures: list[str] = []
    cases = [
        (RunResult(is_error=True, result_text=""), True, "empty output"),
        (RunResult(is_error=True,
                   result_text=("Failed to authenticate. API Error: 401 OAuth "
                                "access token has expired. Re-authenticate to "
                                "continue."),
                   ), True, "401 in result_text (the t-6570 shape)"),
        (RunResult(is_error=True, result_text="",
                   error_message="OAuth token has expired"), True,
         "401 in error_message"),
        (RunResult(is_error=False, result_text="Here is your refactor",
                   num_turns=12), False, "real output over many turns"),
        (RunResult(is_error=True, result_text="I edited three files then hit "
                   "a compile error", num_turns=9), False,
         "genuine work that ended in an error"),
        # This repo's own sessions write about OAuth and 401s constantly, so
        # the auth check must key on "this output IS a fatal error" (short),
        # not "this output mentions auth" — or a real answer gets swallowed as
        # "the backup never ran" and replaced with a retry countdown.
        (RunResult(is_error=True, num_turns=1, result_text=(
            "I traced the failure: the CLI reports 'Failed to authenticate. "
            "API Error: 401 OAuth access token has expired.' whenever the "
            "refresh token is missing from .credentials.json, and the runner "
            "was treating that as a productive turn. " + "Details follow. " * 20
        )), False, "a one-turn answer that merely discusses 401s"),
    ]
    for res, expected, label in cases:
        got = _no_productive_work(res)
        if got is not expected:
            failures.append(
                f"_no_productive_work({label}) -> {got}, expected {expected}"
            )
    return failures


def _test_credential_probe() -> list[str]:
    """Probe classification + the self-healing cache bust after a re-login."""
    failures: list[str] = []
    tmp = tempfile.mkdtemp(prefix="acct_probe_")
    try:
        missing = os.path.join(tmp, "gone")
        dead = os.path.join(tmp, ".claude-klerk")
        os.makedirs(dead, exist_ok=True)

        clear_auth_cache()
        if unusable_reason(missing) is None:
            failures.append("probe: a missing dir should be unusable")
        if unusable_reason(dead) is None:
            failures.append("probe: an empty dir should be unusable")

        # The real t-6614 shape: file present, no refresh token.
        _write_credentials(dead, logged_in=False)
        if unusable_reason(dead) != REASON_NO_TOKEN:
            failures.append(
                f"probe: no-refresh-token should report {REASON_NO_TOKEN!r}, "
                f"got {unusable_reason(dead)!r}"
            )

        # Simulated /login — must be visible with no restart and no cache clear.
        _write_credentials(dead, logged_in=True)
        if not credentials_usable(dead):
            failures.append(
                "probe: re-login did not bust the cache — the account would "
                "stay sidelined until a restart"
            )

        if account_label(dead) != "klerk":
            failures.append(
                f"probe: label for {dead!r} was {account_label(dead)!r}, "
                "expected 'klerk'"
            )
    finally:
        clear_auth_cache()
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


async def _test_dead_backup_401_in_result_text() -> list[str]:
    """The t-6570 incident, end to end.

    Primary hits its usage limit; the backup is signed out and the CLI puts
    the 401 in result_text.  The turn must come back as a scheduled retry at
    the primary's reset, not a build failure showing a raw 401.
    """
    failures: list[str] = []
    results = [
        RunResult(is_error=True,
                  error_message="You've hit your usage limit · resets 5pm",
                  result_text=""),
        RunResult(is_error=True,
                  result_text=("Failed to authenticate. API Error: 401 OAuth "
                               "access token has expired. Re-authenticate to "
                               "continue.")),
    ]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"]
    )
    primary, backup = accts
    if spawns != 2:
        failures.append(f"t-6570: expected exactly 2 spawns, got {spawns}")
    if not result.usage_limit_reset:
        failures.append(
            "t-6570: no usage_limit_reset — the user would see the raw 401 as "
            "a build failure with no retry (the reported bug)"
        )
    elif result.usage_limit_reset != runner._account_cooldowns.get(primary):
        failures.append("t-6570: retry time is not the primary's reset time")
    if result.retry_reason != "backup_logged_out":
        failures.append(
            f"t-6570: retry_reason was {result.retry_reason!r}, expected "
            "'backup_logged_out' (drives the friendlier Discord headline)"
        )
    return failures


async def _test_logged_out_backup_never_spawned() -> list[str]:
    """A backup with no refresh token is skipped before it costs a spawn."""
    failures: list[str] = []
    results = [
        RunResult(is_error=True,
                  error_message="You've hit your usage limit · resets 5pm",
                  result_text=""),
        RunResult(is_error=False, result_text="should never happen"),
    ]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"], logged_out={"backup"},
    )
    primary, backup = accts
    if spawns != 1:
        failures.append(
            f"preflight: expected 1 spawn (backup is doomed), got {spawns}"
        )
    if not result.usage_limit_reset:
        failures.append("preflight: expected a scheduled retry, not a dead end")
    alerts = runner._store.alerts
    if backup not in alerts:
        failures.append(
            "preflight: no alert recorded for the signed-out backup — it would "
            "stay invisible in Discord (the five-week blind spot)"
        )
    elif alerts[backup].get("reason") != REASON_NO_TOKEN:
        failures.append(
            f"preflight: alert reason was {alerts[backup].get('reason')!r}"
        )
    return failures


async def _test_refusal_countdown_tells_the_truth() -> list[str]:
    """The "nothing is available" retry notice must name the real cause.

    Three ways to reach it, and only one of them is a reset the user is
    genuinely waiting on. Announcing "waiting for your main account to reset"
    when every account is signed out invents a reset nobody hit — the exact
    dishonesty this branch set out to remove, one layer further down.
    """
    failures: list[str] = []
    later = datetime.now(timezone.utc) + timedelta(hours=5)
    never_run = [RunResult(is_error=False, result_text="should never happen")]

    def _cool_both(runner, _inst, accts):
        for a in accts:
            runner._set_account_cooldown(a, later)

    cases = [
        # (label, setup, expected reason, must the retry time be `later`?)
        (
            "one live account cooling down, backup rejected by the server",
            lambda r, i, a: (_cool_both(r, i, a), r._auth_dead.add(a[1])),
            "backup_logged_out", True,
        ),
        (
            "every account signed out",
            lambda r, i, a: (_cool_both(r, i, a),
                             r._auth_dead.update(a)),
            "accounts_logged_out", False,
        ),
    ]
    for label, setup, expected, expect_real_reset in cases:
        result, _inst, _runner, _accts, spawns = await _run_with_streams(
            never_run, accounts=["primary", "backup"], setup=setup,
        )
        if spawns:
            failures.append(f"refusal ({label}): spawned {spawns} time(s) anyway")
        if result.retry_reason != expected:
            failures.append(
                f"refusal ({label}): retry_reason was "
                f"{result.retry_reason!r}, expected {expected!r}"
            )
        if expect_real_reset and result.usage_limit_reset != later:
            failures.append(
                f"refusal ({label}): retry time isn't the live account's "
                "reset — the countdown would point at nothing"
            )
        if not expect_real_reset and result.usage_limit_reset == later:
            failures.append(
                f"refusal ({label}): scheduled the retry against a dead "
                "account's cooldown, which changes nothing when it expires"
            )
        if not result.usage_limit_reset:
            failures.append(
                f"refusal ({label}): no retry at all — the turn dead-ends"
            )

    # The third shape — nothing signed out, nothing free either — can only be
    # reached mid-turn, so exercise the decision itself.
    runner = ClaudeRunner(store=_FakeStore())
    when, reason = runner._refusal_retry_plan(set())
    if reason != "no_account_free":
        failures.append(
            f"refusal (nothing free): reason was {reason!r} — with no account "
            "signed out, blaming a usage limit is guesswork"
        )
    if when <= datetime.now(timezone.utc):
        failures.append("refusal (nothing free): retry time is already past")

    # And the wording each reason produces must actually differ.
    from bot.engine.lifecycle import _AUTH_RETRY_REASONS, _RETRY_HEADLINES

    if "reset" in _RETRY_HEADLINES["accounts_logged_out"]:
        failures.append(
            "refusal: the all-signed-out headline still talks about a reset"
        )
    if len(set(_RETRY_HEADLINES.values())) != len(_RETRY_HEADLINES):
        failures.append("refusal: two retry reasons render the same headline")
    if "no_account_free" in _AUTH_RETRY_REASONS:
        failures.append(
            "refusal: offered the auth panel for a wait that has nothing to "
            "do with being signed out"
        )
    return failures


async def _test_all_accounts_probe_dead_safety_valve() -> list[str]:
    """Every account fails the probe -> ignore the probe, don't go dark.

    The probe is a heuristic (a host storing credentials outside the config
    dir reads as all-logged-out), so it must never block every spawn.
    """
    failures: list[str] = []
    results = [RunResult(is_error=False, result_text="ok")]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"],
        logged_out={"primary", "backup"},
    )
    if spawns < 1:
        failures.append(
            "safety valve: nothing was spawned — a bad probe took the fleet dark"
        )
    if result.is_error:
        failures.append(
            f"safety valve: run errored: {result.error_message!r}"
        )
    return failures


async def _test_sole_account_dead_gives_actionable_message() -> list[str]:
    """One account, signed out, nothing to fall back to.

    The user must get a message naming the account and the fix, not the CLI's
    raw 401 — and it must survive humanize_failure() untouched.
    """
    failures: list[str] = []
    from bot.engine.lifecycle import humanize_failure

    results = [
        RunResult(is_error=True,
                  result_text=("Failed to authenticate. API Error: 401 OAuth "
                               "access token has expired.")),
    ]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["solo"],
    )
    msg = result.error_message or ""
    if "CLAUDE_CONFIG_DIR" not in msg:
        failures.append(
            f"dead-end: message lacks the re-auth command: {msg!r}"
        )
    # Match the CLI's raw phrasing, not the bare digits. `msg` embeds the
    # account's config dir, which here is a mkdtemp path with a random
    # suffix — one that can contain "401" by chance, failing this test at
    # random. It did: "/tmp/acct_failover_26401es7/solo". This harness blocks
    # the chain, so a one-in-a-few-hundred false failure is a real cost.
    if "API Error" in msg or "access token has expired" in msg:
        failures.append(f"dead-end: raw CLI 401 leaked into the message: {msg!r}")
    if humanize_failure(msg) != msg:
        failures.append(
            "dead-end: humanize_failure clobbered the bot's own actionable "
            "message (it should only replace raw CLI text)"
        )
    generic = humanize_failure(
        "Failed to authenticate. API Error: 401 OAuth access token has expired."
    )
    if generic and "401" in generic:
        failures.append(
            "dead-end: humanize_failure left the raw 401 as the headline"
        )
    return failures


async def _test_runtime_401_alert_not_auto_cleared() -> list[str]:
    """A server-rejected account clears on a re-login, not on a re-read.

    Two opposite failures to avoid. Re-reading the SAME file proves nothing —
    clearing on that would announce recovery and then fail on the very next
    task. But the file being REWRITTEN is a `/login`, and the notice promises
    the account rejoins the moment it's signed in, so that must clear the
    sideline (including the 24h cooldown that came with it) without waiting
    for a successful run that may never be scheduled on a backup account.
    """
    failures: list[str] = []
    tmp = tempfile.mkdtemp(prefix="acct_alert_")
    acct = os.path.join(tmp, "primary")
    other = os.path.join(tmp, "other")
    for d in (acct, other):
        os.makedirs(d, exist_ok=True)
    _write_credentials(acct, logged_in=True)
    _write_credentials(other, logged_in=True)
    clear_auth_cache()
    saved = list(config.CLAUDE_ACCOUNTS)
    config.CLAUDE_ACCOUNTS[:] = [acct, other]
    try:
        runner = ClaudeRunner(store=_FakeStore())
        runner._record_auth_alert(acct)
        runner._set_account_cooldown(
            acct, datetime.now(timezone.utc) + timedelta(hours=24),
        )
        runner._auth_cooldowns.add(acct)
        runner._store.alerts[acct]["notified"] = True
        if runner._store.alerts[acct]["reason"] != REASON_RUNTIME_401:
            failures.append("runtime-401: wrong reason recorded")
        if not runner._store.alerts[acct].get("cred_fp"):
            failures.append(
                "runtime-401: no credential fingerprint recorded, so a later "
                "login can't be told from the same rejected file"
            )
        runner._unusable_accounts()
        rec = runner._store.alerts.get(acct)
        if rec is None or rec.get("resolved"):
            failures.append(
                "runtime-401: alert was auto-cleared by re-reading the same "
                "credentials file — a false all-clear"
            )
        if acct not in runner._account_cooldowns:
            failures.append(
                "runtime-401: sideline cooldown dropped without any new "
                "evidence the account works"
            )

        # ...now the user actually signs in: the file is rewritten.
        _write_credentials(acct, logged_in=True, token="rt-fresh-and-longer")
        clear_auth_cache()
        runner._unusable_accounts()
        if acct in runner._account_cooldowns:
            failures.append(
                "runtime-401: signed back in but still sitting out the 24h "
                "auth cooldown — the Ark notice's promise is a lie"
            )
        if acct in runner._known_dead_accounts():
            failures.append(
                "runtime-401: still counted as dead after a re-login, so a "
                "retry would refuse to wait on it"
            )
        if not (runner._store.alerts.get(acct) or {}).get("resolved"):
            failures.append(
                "runtime-401: no all-clear queued after the re-login"
            )
    finally:
        config.CLAUDE_ACCOUNTS[:] = saved
        clear_auth_cache()
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


async def _test_sideline_survives_a_reboot() -> list[str]:
    """A restart must not strand a signed-out account for the rest of the day.

    The record of *why* an account was sidelined lives in memory and dies with
    the process; only the cooldown itself and the alert record are persisted.
    So after a restart every sideline reads as an ordinary usage limit — and
    signing the account back in, which is supposed to return it to rotation
    immediately, would quietly do nothing, benching it for up to 24h despite
    being fine. The credentials fingerprint on the alert record is the only
    evidence of a login that survives the restart, so this is the one recovery
    path that has to keep working across one; the sibling test above covers the
    same recovery within a single process.
    """
    failures: list[str] = []
    tmp = tempfile.mkdtemp(prefix="acct_reboot_")
    acct = os.path.join(tmp, "primary")
    other = os.path.join(tmp, "other")
    for d in (acct, other):
        os.makedirs(d, exist_ok=True)
    _write_credentials(acct, logged_in=True)
    _write_credentials(other, logged_in=True)
    clear_auth_cache()
    saved = list(config.CLAUDE_ACCOUNTS)
    config.CLAUDE_ACCOUNTS[:] = [acct, other]
    try:
        # --- The outage, before the reboot.
        store = _FakeStore()
        runner = ClaudeRunner(store=store)
        runner._record_auth_alert(acct)
        runner._set_account_cooldown(
            acct, datetime.now(timezone.utc) + timedelta(hours=24),
        )
        runner._auth_cooldowns.add(acct)
        store.alerts[acct]["notified"] = True

        # --- Reboot: same store on disk, brand-new runner. _auth_cooldowns is
        #     empty now, which is exactly what makes this case different.
        rebooted = ClaudeRunner(store=store)
        if acct not in rebooted._account_cooldowns:
            failures.append(
                "reboot: the sideline didn't survive the restart at all — "
                "the account would be picked again immediately"
            )
        if rebooted._auth_cooldowns:
            failures.append(
                "reboot: auth/usage split unexpectedly survived; this test "
                "no longer covers what it claims to"
            )

        # A `/login` rewrites the credentials file.
        _write_credentials(acct, logged_in=True, token="rt-signed-back-in")
        clear_auth_cache()
        rebooted._unusable_accounts()
        if acct in rebooted._account_cooldowns:
            failures.append(
                "reboot: signed back in after a restart but still benched — "
                "the fingerprint is the only surviving proof of a login and "
                "it was ignored"
            )
        if store.cooldowns.get(acct):
            failures.append(
                "reboot: cleared in memory but not in the store, so the next "
                "restart would bench the account all over again"
            )
        if acct in ClaudeRunner(store=store)._account_cooldowns:
            failures.append(
                "reboot: a second restart resurrected the cleared sideline"
            )

        # The all-clear record sits around for up to a minute waiting on the
        # notifier. A usage limit landing in that window must survive: nothing
        # about a stale fingerprint on an already-settled record says anything
        # about a rate limit, and clearing it would send the next task straight
        # into the limit it was meant to be waiting out.
        rebooted._set_account_cooldown(
            acct, datetime.now(timezone.utc) + timedelta(hours=5),
        )
        rebooted._unusable_accounts()
        if acct not in rebooted._account_cooldowns:
            failures.append(
                "reboot: a fresh usage limit was wiped by the already-resolved "
                "auth alert still waiting for its all-clear"
            )
    finally:
        config.CLAUDE_ACCOUNTS[:] = saved
        clear_auth_cache()
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


async def _test_boot_reconciles_account_health() -> list[str]:
    """Startup must re-check the accounts, not just trust the saved table.

    While the bot is down, both directions go stale: an account can be signed
    back in (its sideline is now a lie that benches it until some task happens
    to try it) and one can be signed out (nothing announces it until failover
    lands on it — the "nobody notices for weeks" case this feature exists to
    kill). Boot used to only seed alerts for the second case, with its own copy
    of the fingerprint logic; this covers both through the runner's own rules.
    """
    failures: list[str] = []
    tmp = tempfile.mkdtemp(prefix="acct_boot_")
    came_back = os.path.join(tmp, "came-back")
    went_out = os.path.join(tmp, "went-out")
    for d in (came_back, went_out):
        os.makedirs(d, exist_ok=True)
    _write_credentials(came_back, logged_in=True, token="rt-signed-in-while-down")
    _write_credentials(went_out, logged_in=False)
    clear_auth_cache()
    saved = list(config.CLAUDE_ACCOUNTS)
    config.CLAUDE_ACCOUNTS[:] = [came_back, went_out]
    try:
        # State as the previous process left it: came_back was sidelined and
        # announced, judged against a credentials file that has since been
        # rewritten by a /login.  went_out looked fine, so there's no record.
        store = _FakeStore()
        store.alerts[came_back] = {
            "reason": REASON_RUNTIME_401, "since": "2026-07-27T09:00:00+00:00",
            "cred_fp": "1:1", "notified": True, "resolved": False,
            "snooze_until": None,
        }
        store.cooldowns[came_back] = (
            datetime.now(timezone.utc) + timedelta(hours=24)
        ).isoformat()

        runner = ClaudeRunner(store=store)
        runner.reconcile_account_health()

        if came_back in runner._account_cooldowns:
            failures.append(
                "boot: an account signed back in while the bot was down was "
                "still benched — nothing would free it until a task tried it"
            )
        if not (store.alerts.get(came_back) or {}).get("resolved"):
            failures.append(
                "boot: no all-clear queued for the recovered account, so The "
                "Ark keeps showing a stale outage notice"
            )
        rec = store.alerts.get(went_out)
        if not rec:
            failures.append(
                "boot: an account that went out while we were down was never "
                "recorded, so nothing tells the user until failover fails"
            )
        elif rec.get("notified"):
            failures.append("boot: seeded the alert as already announced")
        elif not rec.get("cred_fp"):
            failures.append(
                "boot: seeded an alert with no credentials fingerprint, so a "
                "later /login couldn't retire it without a restart"
            )
    finally:
        config.CLAUDE_ACCOUNTS[:] = saved
        clear_auth_cache()
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


def _test_login_clears_auth_cooldown() -> list[str]:
    """Signing in must put the account straight back into rotation.

    The Ark notice promises "rejoins on its own the moment it's signed in — no
    restart needed". A 24h auth cooldown left over from the last 401 would make
    that a lie. A *usage* cooldown must survive the same event: that one really
    does end on the clock, and clearing it would just burn a doomed spawn.
    """
    failures: list[str] = []
    tmp = tempfile.mkdtemp(prefix="acct_relogin_")
    dead = os.path.join(tmp, "dead")
    limited = os.path.join(tmp, "limited")
    for d in (dead, limited):
        os.makedirs(d, exist_ok=True)
    _write_credentials(dead, logged_in=False)
    _write_credentials(limited, logged_in=True)
    clear_auth_cache()
    saved = list(config.CLAUDE_ACCOUNTS)
    config.CLAUDE_ACCOUNTS[:] = [dead, limited]
    try:
        runner = ClaudeRunner(store=_FakeStore())
        later = datetime.now(timezone.utc) + timedelta(hours=24)
        runner._set_account_cooldown(dead, later)
        runner._auth_cooldowns.add(dead)          # sidelined for auth
        runner._set_account_cooldown(limited, later)  # sidelined for usage

        runner._unusable_accounts()               # sees `dead` as logged out
        _write_credentials(dead, logged_in=True)  # ...user runs /login
        clear_auth_cache()
        runner._unusable_accounts()               # next spawn notices

        if dead in runner._account_cooldowns:
            failures.append(
                "relogin: the account is signed in again but still sitting "
                "out a stale auth cooldown"
            )
        if limited not in runner._account_cooldowns:
            failures.append(
                "relogin: cleared a usage-limit cooldown, which a login "
                "cannot fix — the next spawn would be wasted"
            )
        if runner._pick_account() != dead:
            failures.append("relogin: the recovered account wasn't picked")
    finally:
        config.CLAUDE_ACCOUNTS[:] = saved
        clear_auth_cache()
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


async def _test_success_keeps_a_siblings_usage_cooldown() -> list[str]:
    """A successful run must not wipe a usage limit a parallel task just hit.

    This bot runs many tasks at once on the same account.  Task Y hits the 5h
    limit and cools the account down while task X is still streaming; X then
    finishes fine and clears the auth sideline.  If that clear ignores the
    cooldown *kind*, it deletes Y's genuine limit and the next spawn walks
    straight back into it.  A success only ever proves the account can log in.
    """
    failures: list[str] = []
    reset = datetime.now(timezone.utc) + timedelta(hours=5)

    def setup(runner, instance, accts):
        inner = runner._stream_output

        async def wrapped(proc, inst, on_progress, on_stall, **kw):
            # The parallel sibling, landing mid-flight.
            runner._set_account_cooldown(accts[0], reset)
            return await inner(proc, inst, on_progress, on_stall, **kw)

        runner._stream_output = wrapped

    result, instance, runner, accts, _ = await _run_with_streams(
        [RunResult(is_error=False, result_text="ok")],
        accounts=["primary", "backup"], setup=setup,
    )
    if accts[0] not in runner._account_cooldowns:
        failures.append(
            "sibling-cooldown: a successful run wiped the usage-limit "
            "cooldown a parallel task had just set on the same account"
        )
    if accts[0] not in runner._store.cooldowns:
        failures.append(
            "sibling-cooldown: the cooldown was cleared from the persisted "
            "table, so a reboot wouldn't restore it either"
        )
    if result.is_error:
        failures.append(f"sibling-cooldown: run errored: {result.error_message!r}")
    return failures


def _test_success_still_retires_an_auth_sideline() -> list[str]:
    """...but the narrower clear must still end an auth sideline on success.

    The other half of the same rule: a run that completed is the only evidence
    that clears a runtime-401 sideline (the credentials file looks identical
    before and after), so narrowing it must not strand a recovered account.
    """
    failures: list[str] = []
    tmp = tempfile.mkdtemp(prefix="acct_success_")
    acct = os.path.join(tmp, "primary")
    os.makedirs(acct, exist_ok=True)
    _write_credentials(acct, logged_in=True)
    clear_auth_cache()
    saved = list(config.CLAUDE_ACCOUNTS)
    config.CLAUDE_ACCOUNTS[:] = [acct]
    try:
        runner = ClaudeRunner(store=_FakeStore())
        runner._set_account_cooldown(
            acct, datetime.now(timezone.utc) + timedelta(hours=24),
        )
        runner._auth_cooldowns.add(acct)
        runner._auth_dead.add(acct)

        runner._clear_auth_cooldown(acct)  # what the success path now calls

        if acct in runner._account_cooldowns:
            failures.append(
                "success-clears-auth: the 401 sideline outlived a run that "
                "proved the account authenticates"
            )
        if acct in runner._auth_dead:
            failures.append("success-clears-auth: still marked server-rejected")
    finally:
        config.CLAUDE_ACCOUNTS[:] = saved
        clear_auth_cache()
        shutil.rmtree(tmp, ignore_errors=True)
    return failures


async def _amain() -> int:
    all_failures: list[tuple[str, list[str]]] = []

    all_failures.append(("classifiers", _test_classifiers()))
    all_failures.append(("no-productive-work", _test_no_productive_work()))
    all_failures.append(("credential-probe", _test_credential_probe()))
    all_failures.append(("confident-failover", await _test_confident_failover()))
    all_failures.append(("agnostic-no-failover", await _test_agnostic_no_failover()))
    all_failures.append(("both-dead-terminates", await _test_both_dead_terminates()))
    all_failures.append(("limit-then-dead-backup",
                         await _test_limit_then_dead_backup_keeps_reset()))
    all_failures.append(("run-resets-tried", await _test_run_resets_accounts_tried()))
    all_failures.append(("long-output-not-a-dead-account",
                         await _test_long_output_never_sidelines_account()))
    all_failures.append(("t-6570-401-in-result-text",
                         await _test_dead_backup_401_in_result_text()))
    all_failures.append(("logged-out-backup-not-spawned",
                         await _test_logged_out_backup_never_spawned()))
    all_failures.append(("refusal-countdown-honest",
                         await _test_refusal_countdown_tells_the_truth()))
    all_failures.append(("all-probe-dead-safety-valve",
                         await _test_all_accounts_probe_dead_safety_valve()))
    all_failures.append(("dead-end-actionable-message",
                         await _test_sole_account_dead_gives_actionable_message()))
    all_failures.append(("runtime-401-not-auto-cleared",
                         await _test_runtime_401_alert_not_auto_cleared()))
    all_failures.append(("sideline-survives-a-reboot",
                         await _test_sideline_survives_a_reboot()))
    all_failures.append(("boot-reconciles-account-health",
                         await _test_boot_reconciles_account_health()))
    all_failures.append(("relogin-clears-auth-cooldown",
                         _test_login_clears_auth_cooldown()))
    all_failures.append(("success-keeps-sibling-usage-cooldown",
                         await _test_success_keeps_a_siblings_usage_cooldown()))
    all_failures.append(("success-retires-auth-sideline",
                         _test_success_still_retires_an_auth_sideline()))

    total = sum(len(f) for _, f in all_failures)
    if total:
        print("FAIL: account-failover tests")
        for name, fails in all_failures:
            for f in fails:
                print(f"  [{name}] {f}")
        return 1

    print("PASS: account-failover tests")
    for name, _ in all_failures:
        print(f"  - {name}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
