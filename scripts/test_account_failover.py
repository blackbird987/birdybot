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

Run: ``python scripts/test_account_failover.py``  (exit 0 on pass).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
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
        self.cooldowns[account_dir] = reset_iso

    def set_model_cooldown(self, account_dir, reset_iso):
        pass

    # --- alert state machine ---
    def get_account_alerts(self):
        return {k: dict(v) for k, v in self.alerts.items()}

    def set_account_alert(self, account_dir, reason, since_iso):
        rec = self.alerts.get(account_dir)
        if rec is not None:
            rec["reason"] = reason
            rec["resolved"] = False
            return False
        self.alerts[account_dir] = {
            "reason": reason, "since": since_iso,
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


def _write_credentials(account_dir: str, *, logged_in: bool) -> None:
    """Fake a CLAUDE_CONFIG_DIR that the credential preflight accepts/rejects.

    ``logged_in=False`` reproduces the real t-6614 shape: the file exists (so
    the old existence-only check passed) but carries no refresh token.
    """
    payload = {"claudeAiOauth": {"accessToken": "at-test"}}
    if logged_in:
        payload["claudeAiOauth"]["refreshToken"] = "rt-test"
    Path(account_dir, ".credentials.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )


async def _run_with_streams(stream_results, *, accounts, logged_out=()):
    """Run a fresh instance through runner.run(), faking _stream_output to
    yield ``stream_results`` (a list of RunResult) per spawn in order; the
    last entry is reused if more spawns happen.

    ``logged_out`` names accounts whose credentials file has no refresh token.

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
    if "401" in msg:
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
    """A runtime 401 sideline survives a credentials file that looks fine.

    Re-reading the same file proves nothing — only a successful run may post
    the all-clear, otherwise the bot cheerfully announces recovery and then
    fails on the very next task.
    """
    failures: list[str] = []
    tmp = tempfile.mkdtemp(prefix="acct_alert_")
    acct = os.path.join(tmp, "primary")
    os.makedirs(acct, exist_ok=True)
    _write_credentials(acct, logged_in=True)
    clear_auth_cache()
    saved = list(config.CLAUDE_ACCOUNTS)
    config.CLAUDE_ACCOUNTS[:] = [acct]
    try:
        runner = ClaudeRunner(store=_FakeStore())
        runner._record_auth_alert(acct)
        runner._store.alerts[acct]["notified"] = True
        if runner._store.alerts[acct]["reason"] != REASON_RUNTIME_401:
            failures.append("runtime-401: wrong reason recorded")
        runner._unusable_accounts()
        rec = runner._store.alerts.get(acct)
        if rec is None or rec.get("resolved"):
            failures.append(
                "runtime-401: alert was auto-cleared by re-reading the same "
                "credentials file — a false all-clear"
            )
        # A successful run is the real all-clear.
        runner._store.resolve_account_alert(acct)
        if not runner._store.alerts[acct].get("resolved"):
            failures.append("runtime-401: a successful run failed to resolve it")
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
    all_failures.append(("t-6570-401-in-result-text",
                         await _test_dead_backup_401_in_result_text()))
    all_failures.append(("logged-out-backup-not-spawned",
                         await _test_logged_out_backup_never_spawned()))
    all_failures.append(("all-probe-dead-safety-valve",
                         await _test_all_accounts_probe_dead_safety_valve()))
    all_failures.append(("dead-end-actionable-message",
                         await _test_sole_account_dead_gives_actionable_message()))
    all_failures.append(("runtime-401-not-auto-cleared",
                         await _test_runtime_401_alert_not_auto_cleared()))

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
