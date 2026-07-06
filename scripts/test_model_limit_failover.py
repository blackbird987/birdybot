"""Tests for model-specific limit failover (Fable 5 quota handling).

Fable 5 has its own usage limit on top of the account-wide 5h/weekly caps.
The CLI reports it as "You've reached your Fable 5 limit. Run /usage-credits
to continue or switch models with /model." — the subscription keeps working
for other models, so the bot must NOT sideline the whole account. Instead:

  1. Try the other account's quota for the same model (each account has its
     own Fable quota).
  2. When every account is model-limited, downgrade to MODEL_FALLBACK
     (subscription-only — never pay-per-use) and keep going.
  3. New runs while the cooldown is active go straight to the fallback model
     (no doomed spawn), routed to a model-free account when one exists.
  4. When the cooldown lapses the override stops applying — automatically
     back on the primary model.

Two layers, mirroring scripts/test_account_failover.py:

1. Pure parser unit tests: parse_model_limit matches model-specific wording,
   rejects account-wide caps and transient rate limits.
2. Integration tests against the real _run_impl branch, faking only the
   subprocess boundary.

Run: ``python scripts/test_model_limit_failover.py``  (exit 0 on pass).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude.parser import parse_model_limit, parse_usage_limit
from bot.claude.runner import ClaudeRunner
from bot.claude.types import Instance, InstanceStatus, InstanceType, RunResult


FABLE_MSG = (
    "You've reached your Fable 5 limit. Run /usage-credits to continue "
    "or switch models with /model."
)


# ---------------------------------------------------------------------------
# Layer 1: pure parser unit tests
# ---------------------------------------------------------------------------

def _test_parser() -> list[str]:
    failures: list[str] = []

    hit = parse_model_limit(FABLE_MSG)
    if not hit:
        failures.append("parse_model_limit should match the real Fable message")
    else:
        label, reset = hit
        if "fable" not in label.lower():
            failures.append(f"label should name the model, got {label!r}")
        secs = (reset - datetime.now(timezone.utc)).total_seconds()
        if not (4 * 3600 < secs <= 5 * 3600 + 60):
            failures.append(f"no-reset-time fallback should be ~5h, got {secs:.0f}s")

    timed = parse_model_limit("You've reached your Fable 5 limit · resets 7pm")
    if not timed:
        failures.append("parse_model_limit should match timed Fable message")
    elif timed[1].minute != 0:
        failures.append("timed variant should parse the explicit reset time")

    # A model name in the label must win over generic cap words: a future
    # "Fable 5 usage limit" wording is still a MODEL limit — misreading it
    # as account-wide would sideline the whole account.
    if not parse_model_limit("You've reached your Fable 5 usage limit · resets 8pm"):
        failures.append(
            "parse_model_limit should match 'Fable 5 usage limit' "
            "(model name beats generic words)"
        )

    model_no = [
        "",
        "You've hit your usage limit · resets 5pm",   # account-wide
        "hit your weekly limit",                        # account-wide
        "You've hit your 5-hour limit · resets 3pm",   # account-wide
        "hit your plan limit",                          # account-wide
        "rate limit exceeded",                          # transient
        "Some normal completion text",
    ]
    for s in model_no:
        if parse_model_limit(s):
            failures.append(f"parse_model_limit should NOT match: {s!r}")

    # Ordering contract: the generic parser ALSO matches model wording (so
    # is_account_unusable_error keeps treating it as a cap) — the runner
    # must therefore check parse_model_limit first.
    if not parse_usage_limit(FABLE_MSG):
        failures.append(
            "parse_usage_limit should still match Fable wording "
            "(guards depend on it) — runner ordering handles the split"
        )

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
    _next_pid = 96001

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


def _make_instance(repo_dir: str, model: str | None = None) -> Instance:
    inst = Instance(
        id="t-model",
        name=None,
        instance_type=InstanceType.QUERY,
        prompt="do a thing",
        repo_name="test-repo",
        repo_path=repo_dir,
        status=InstanceStatus.RUNNING,
        mode="explore",
    )
    inst.model = model
    return inst


async def _run_with_streams(
    stream_results,
    *,
    accounts,
    model_cooldowns: dict[int, datetime] | None = None,
    instance_model: str | None = None,
):
    """Run a fresh instance through runner.run(), faking _stream_output.

    ``model_cooldowns`` maps account INDEX -> reset datetime, applied to the
    fresh runner before the run (simulates persisted state).

    Returns (result, instance, runner, acct_dirs, spawn_calls) where
    spawn_calls is a list of (cmd_args, env) per spawn.
    """
    tmp = tempfile.mkdtemp(prefix="model_failover_")
    acct_dirs = [os.path.join(tmp, name) for name in accounts]
    repo_dir = os.path.join(tmp, "repo")
    for p in (*acct_dirs, repo_dir):
        os.makedirs(p, exist_ok=True)

    saved_accounts = list(config.CLAUDE_ACCOUNTS)
    saved_spawn = asyncio.create_subprocess_exec
    config.CLAUDE_ACCOUNTS[:] = acct_dirs

    spawn_calls: list[tuple[list[str], dict]] = []

    async def fake_spawn(*args, **kwargs):
        spawn_calls.append((list(args), dict(kwargs.get("env") or {})))
        return _FakeProc()

    asyncio.create_subprocess_exec = fake_spawn  # type: ignore[assignment]

    runner = ClaudeRunner()
    if model_cooldowns:
        for idx, reset in model_cooldowns.items():
            runner._model_cooldowns[acct_dirs[idx]] = reset

    calls = {"n": 0}

    async def fake_stream_output(proc, instance, on_progress, on_stall, **kw):
        i = min(calls["n"], len(stream_results) - 1)
        calls["n"] += 1
        return stream_results[i]

    runner._stream_output = fake_stream_output  # type: ignore[assignment]

    instance = _make_instance(repo_dir, model=instance_model)
    try:
        result = await runner.run(instance)
    finally:
        asyncio.create_subprocess_exec = saved_spawn  # type: ignore[assignment]
        config.CLAUDE_ACCOUNTS[:] = saved_accounts
        shutil.rmtree(tmp, ignore_errors=True)

    return result, instance, runner, acct_dirs, spawn_calls


def _model_flag(cmd: list[str]) -> str | None:
    """Extract the --model value from a spawned command, or None."""
    for i, tok in enumerate(cmd):
        if tok == "--model" and i + 1 < len(cmd):
            return cmd[i + 1]
    return None


def _fable_err() -> RunResult:
    return RunResult(is_error=True, error_message=FABLE_MSG, result_text="")


async def _test_model_limit_hops_account() -> list[str]:
    """Fable limit on primary -> model cooldown (NOT account), hop to backup
    still on the primary model."""
    failures: list[str] = []
    results = [_fable_err(), RunResult(is_error=False, result_text="ok")]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"]
    )
    primary, backup = accts
    if len(spawns) != 2:
        failures.append(f"hop: expected 2 spawns, got {len(spawns)}")
    if primary not in runner._model_cooldowns:
        failures.append("hop: primary should get a MODEL cooldown")
    if runner._account_cooldowns:
        failures.append(
            "hop: account cooldowns must stay EMPTY for a model limit "
            f"(got {list(runner._account_cooldowns)})"
        )
    if len(spawns) == 2:
        if spawns[1][1].get("CLAUDE_CONFIG_DIR") != backup:
            failures.append("hop: second spawn should run on the backup account")
        if _model_flag(spawns[1][0]) is not None:
            failures.append(
                "hop: backup spawn should stay on the primary model "
                f"(got --model {_model_flag(spawns[1][0])})"
            )
    if result.is_error:
        failures.append(f"hop: final result errored: {result.error_message!r}")
    return failures


async def _test_all_limited_downgrades() -> list[str]:
    """Fable limit on BOTH accounts -> third spawn runs the fallback model,
    no account cooldowns, run succeeds."""
    failures: list[str] = []
    results = [_fable_err(), _fable_err(), RunResult(is_error=False, result_text="ok")]
    try:
        result, instance, runner, accts, spawns = await asyncio.wait_for(
            _run_with_streams(results, accounts=["primary", "backup"]),
            timeout=10,
        )
    except asyncio.TimeoutError:
        return ["downgrade: run did NOT terminate (possible infinite recursion)"]

    primary, backup = accts
    if len(spawns) != 3:
        failures.append(f"downgrade: expected 3 spawns, got {len(spawns)}")
    if not (primary in runner._model_cooldowns and backup in runner._model_cooldowns):
        failures.append("downgrade: BOTH accounts should carry a model cooldown")
    if runner._account_cooldowns:
        failures.append("downgrade: account cooldowns must stay empty")
    if len(spawns) == 3:
        if _model_flag(spawns[0][0]) is not None:
            failures.append("downgrade: first spawn should have no --model flag")
        if _model_flag(spawns[2][0]) != config.MODEL_FALLBACK:
            failures.append(
                f"downgrade: third spawn should use --model {config.MODEL_FALLBACK}, "
                f"got {_model_flag(spawns[2][0])!r}"
            )
    if result.is_error:
        failures.append(f"downgrade: final result errored: {result.error_message!r}")
    return failures


async def _test_dead_backup_downgrades() -> list[str]:
    """Fable limit on primary + auth-dead backup -> instead of a raw 401 or
    a stall, rerun on the fallback model on the primary account."""
    failures: list[str] = []
    results = [
        _fable_err(),
        RunResult(is_error=True,
                  error_message=(
                      "Failed to authenticate. API Error: 401 "
                      "Invalid authentication credentials"
                  ),
                  result_text=""),
        RunResult(is_error=False, result_text="ok"),
    ]
    try:
        result, instance, runner, accts, spawns = await asyncio.wait_for(
            _run_with_streams(results, accounts=["primary", "backup"]),
            timeout=10,
        )
    except asyncio.TimeoutError:
        return ["dead-backup: run did NOT terminate"]

    primary, backup = accts
    if len(spawns) != 3:
        failures.append(f"dead-backup: expected 3 spawns, got {len(spawns)}")
    else:
        if spawns[2][1].get("CLAUDE_CONFIG_DIR") != primary:
            failures.append(
                "dead-backup: third spawn should return to the primary account"
            )
        if _model_flag(spawns[2][0]) != config.MODEL_FALLBACK:
            failures.append(
                f"dead-backup: third spawn should use --model "
                f"{config.MODEL_FALLBACK}, got {_model_flag(spawns[2][0])!r}"
            )
    if primary not in runner._model_cooldowns:
        failures.append("dead-backup: primary should carry a model cooldown")
    if backup not in runner._account_cooldowns:
        failures.append("dead-backup: auth-dead backup should be account-cooled")
    if result.is_error:
        failures.append(f"dead-backup: final result errored: {result.error_message!r}")
    return failures


async def _test_single_account_downgrades() -> list[str]:
    """Only one account configured, Fable-limited -> downgrade in place."""
    failures: list[str] = []
    results = [_fable_err(), RunResult(is_error=False, result_text="ok")]
    try:
        result, instance, runner, accts, spawns = await asyncio.wait_for(
            _run_with_streams(results, accounts=["primary"]),
            timeout=10,
        )
    except asyncio.TimeoutError:
        return ["single: run did NOT terminate"]

    if len(spawns) != 2:
        failures.append(f"single: expected 2 spawns, got {len(spawns)}")
    elif _model_flag(spawns[1][0]) != config.MODEL_FALLBACK:
        failures.append(
            f"single: second spawn should use --model {config.MODEL_FALLBACK}, "
            f"got {_model_flag(spawns[1][0])!r}"
        )
    if runner._account_cooldowns:
        failures.append("single: account cooldowns must stay empty")
    if result.is_error:
        failures.append(f"single: final result errored: {result.error_message!r}")
    return failures


async def _test_preemptive_downgrade() -> list[str]:
    """Both accounts already model-cooled (e.g. after reboot) -> a new run
    spawns ONCE, directly on the fallback model."""
    failures: list[str] = []
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    results = [RunResult(is_error=False, result_text="ok")]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"],
        model_cooldowns={0: future, 1: future},
    )
    if len(spawns) != 1:
        failures.append(f"preemptive: expected 1 spawn, got {len(spawns)}")
    elif _model_flag(spawns[0][0]) != config.MODEL_FALLBACK:
        failures.append(
            f"preemptive: expected --model {config.MODEL_FALLBACK}, "
            f"got {_model_flag(spawns[0][0])!r}"
        )
    if result.is_error:
        failures.append(f"preemptive: final result errored: {result.error_message!r}")
    return failures


async def _test_preemptive_routes_to_free_account() -> list[str]:
    """Only the first account is model-cooled -> a new run routes to the
    model-free account and stays on the primary model."""
    failures: list[str] = []
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    results = [RunResult(is_error=False, result_text="ok")]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"],
        model_cooldowns={0: future},
    )
    primary, backup = accts
    if len(spawns) != 1:
        failures.append(f"route: expected 1 spawn, got {len(spawns)}")
    else:
        if spawns[0][1].get("CLAUDE_CONFIG_DIR") != backup:
            failures.append("route: spawn should land on the model-free backup")
        if _model_flag(spawns[0][0]) is not None:
            failures.append(
                "route: model-free account should stay on the primary model"
            )
    return failures


async def _test_expired_cooldown_back_on_primary() -> list[str]:
    """Elapsed model cooldowns are purged -> run goes back to the primary
    model on the first account (the automatic switch-back)."""
    failures: list[str] = []
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    results = [RunResult(is_error=False, result_text="ok")]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"],
        model_cooldowns={0: past, 1: past},
    )
    primary, backup = accts
    if len(spawns) != 1:
        failures.append(f"expired: expected 1 spawn, got {len(spawns)}")
    else:
        if _model_flag(spawns[0][0]) is not None:
            failures.append("expired: cooldown lapsed — no --model flag expected")
        if spawns[0][1].get("CLAUDE_CONFIG_DIR") != primary:
            failures.append("expired: should route to the primary account again")
    if runner._model_cooldowns:
        failures.append("expired: lapsed cooldowns should be purged")
    return failures


async def _test_explicit_model_not_downgraded() -> list[str]:
    """An explicit non-primary model (EXPLORE_MODEL-style) is exempt from
    the downgrade — its own quota is unaffected by the Fable limit."""
    failures: list[str] = []
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    results = [RunResult(is_error=False, result_text="ok")]
    result, instance, runner, accts, spawns = await _run_with_streams(
        results, accounts=["primary", "backup"],
        model_cooldowns={0: future, 1: future},
        instance_model="sonnet",
    )
    if len(spawns) != 1:
        failures.append(f"explicit: expected 1 spawn, got {len(spawns)}")
    elif _model_flag(spawns[0][0]) != "sonnet":
        failures.append(
            f"explicit: --model sonnet must survive, got {_model_flag(spawns[0][0])!r}"
        )
    return failures


async def _amain() -> int:
    all_failures: list[tuple[str, list[str]]] = []

    all_failures.append(("parser", _test_parser()))
    all_failures.append(("hop-to-backup", await _test_model_limit_hops_account()))
    all_failures.append(("all-limited-downgrade", await _test_all_limited_downgrades()))
    all_failures.append(("dead-backup-downgrade", await _test_dead_backup_downgrades()))
    all_failures.append(("single-account-downgrade",
                         await _test_single_account_downgrades()))
    all_failures.append(("preemptive-downgrade", await _test_preemptive_downgrade()))
    all_failures.append(("route-to-free-account",
                         await _test_preemptive_routes_to_free_account()))
    all_failures.append(("expired-back-on-primary",
                         await _test_expired_cooldown_back_on_primary()))
    all_failures.append(("explicit-model-exempt",
                         await _test_explicit_model_not_downgraded()))

    total = sum(len(f) for _, f in all_failures)
    if total:
        print("FAIL: model-limit failover tests")
        for name, fails in all_failures:
            for f in fails:
                print(f"  [{name}] {f}")
        return 1

    print("PASS: model-limit failover tests")
    for name, _ in all_failures:
        print(f"  - {name}")
    return 0


def main() -> int:
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main())
