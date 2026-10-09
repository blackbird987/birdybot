"""Tests for the real plan usage figures (``bot/engine/plan_usage.py``).

The dashboard and Control Room bars used to be a ccusage dollar estimate
against a guessed limit ("93% est" for a session Anthropic had at 15%). They
now read session %, weekly % and per-model weekly % from Anthropic per
account, and fall back to ccusage only when no account has figures.

Covers:
  - parsing the ``limits`` list and the older ``five_hour`` / ``seven_day``
    fields, and a response with neither being an error rather than zeros
  - a signed-out account (no access token), an expired token (no request is
    made, we never refresh) and a 401 response
  - a network error serving the last good reading, marked stale
  - the TTL cache and the negative cache each preventing a second request,
    and a rewritten credentials file busting the cache
  - the embed bar staying under Discord's 1024-char field limit with two
    accounts, labelling accounts only when there is more than one
  - ``get_usage_bar_async`` falling back to the ccusage bar when every
    account fails, and preferring the real figures when one has data
  - the token only ever going to api.anthropic.com

No network unless ``--live``, which reads the real accounts and prints each
one's state and figures (never the token).

Run: ``python scripts/test_plan_usage.py [--live] [config_dir ...]``
(exit 0 on pass).
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.engine import plan_usage, usage

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILURES.append(msg)


LIMITS_BODY = {
    "five_hour": {"utilization": 99.0, "resets_at": "2026-10-09T15:00:00Z"},
    "seven_day": {"utilization": 99.0, "resets_at": "2026-10-12T09:00:00Z"},
    "limits": [
        {"kind": "session", "percent": 15, "resets_at": "2026-10-09T14:00:00+00:00"},
        {"kind": "weekly_all", "percent": 73, "resets_at": "2026-10-12T09:00:00+00:00"},
        {"kind": "weekly_scoped", "percent": 5, "resets_at": "2026-10-12T09:00:00+00:00",
         "scope": {"model": {"display_name": "Fable"}}},
        {"kind": "weekly_scoped", "percent": 0, "resets_at": None,
         "scope": {"model": {"display_name": "Opus"}}},
    ],
}

LEGACY_BODY = {
    "five_hour": {"utilization": 14.0, "resets_at": "2026-10-09T20:59:00.123456+00:00"},
    "seven_day": {"utilization": 9.0, "resets_at": "2026-10-10T20:59:00+00:00"},
    "seven_day_opus": {"utilization": 11.0, "resets_at": None},
}


class FakeHttp:
    """Stands in for ``plan_usage._http_get``; records every call."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.responses: dict[str, object] = {}  # token -> (status, body) | Exception

    async def __call__(self, token: str):
        self.calls.append(token)
        resp = self.responses[token]
        if isinstance(resp, Exception):
            raise resp
        return resp


def make_account(root: Path, name: str, token: str | None, *,
                 expires_in: float | None = 3600) -> str:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    oauth: dict = {"refreshToken": "r-" + name}
    if token is not None:
        oauth["accessToken"] = token
    if expires_in is not None:
        oauth["expiresAt"] = int((time.time() + expires_in) * 1000)
    (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))
    return str(d)


def test_parse() -> None:
    print("parse")
    a = plan_usage.parse_usage(LIMITS_BODY, label="main", config_dir="/x")
    check(a.state == "ok", "limits body parses as ok")
    check(a.session_pct == 15 and a.week_pct == 73,
          "limits list wins over the legacy fields")
    check(a.week_resets_at is not None and a.week_resets_at.day == 12,
          "weekly reset time parsed")
    check([(n, p) for n, p, _ in a.scoped] == [("Fable", 5.0), ("Opus", 0.0)],
          "per-model weekly limits parsed with display names")

    b = plan_usage.parse_usage(LEGACY_BODY, label="main", config_dir="/x")
    check(b.session_pct == 14 and b.week_pct == 9, "legacy fields parse")
    check(b.session_resets_at is not None and b.session_resets_at.minute == 59,
          "fractional-second timestamp parsed")
    check([(n, p) for n, p, _ in b.scoped] == [("Opus", 11.0)],
          "legacy per-model field parsed")

    c = plan_usage.parse_usage({"something_else": 1}, label="m", config_dir="/x")
    check(c.state == "error" and not c.has_data,
          "a response with no figures is an error, not zeros")
    d = plan_usage.parse_usage(["not", "a", "dict"], label="m", config_dir="/x")
    check(d.state == "error", "a non-object response is an error")


async def test_states(root: Path, http: FakeHttp) -> None:
    print("account states")
    plan_usage._reset_cache()
    out = make_account(root, ".claude-klerk", None)
    a = await plan_usage.fetch_account_usage(out)
    check(a.state == "signed_out" and a.label == "klerk",
          "no access token reads as signed out, labelled 'klerk'")

    empty = make_account(root, ".claude-blank", "   ")
    check((await plan_usage.fetch_account_usage(empty)).state == "signed_out",
          "a blank access token reads as signed out")

    missing = str(root / ".claude-nowhere")
    check((await plan_usage.fetch_account_usage(missing)).state == "signed_out",
          "a missing credentials file reads as signed out")

    expired = make_account(root, ".claude-old", "tok-old", expires_in=-60)
    before = len(http.calls)
    e = await plan_usage.fetch_account_usage(expired)
    check(e.state == "expired", "a past expiresAt reads as expired")
    check(len(http.calls) == before, "an expired token is never sent (no refresh either)")

    rejected = make_account(root, ".claude-rej", "tok-401")
    http.responses["tok-401"] = (401, {"type": "error", "error": {
        "type": "authentication_error", "message": "OAuth token has expired"}})
    r = await plan_usage.fetch_account_usage(rejected)
    check(r.state == "expired" and "401" in r.error, "a 401 reads as expired")

    limited = make_account(root, ".claude-429", "tok-429")
    http.responses["tok-429"] = (429, None)
    check((await plan_usage.fetch_account_usage(limited)).state == "error",
          "a 429 reads as an error")


async def test_cache(root: Path, http: FakeHttp) -> None:
    print("cache")
    plan_usage._reset_cache()
    main = make_account(root, ".claude-main", "tok-main")
    http.responses["tok-main"] = (200, LIMITS_BODY)

    n0 = len(http.calls)
    a = await plan_usage.fetch_account_usage(main)
    b = await plan_usage.fetch_account_usage(main)
    check(a.week_pct == 73 and b.week_pct == 73, "good reading returned")
    check(len(http.calls) == n0 + 1, "a second read inside the TTL makes no request")

    await plan_usage.fetch_account_usage(main, force=True)
    check(len(http.calls) == n0 + 2, "force bypasses the cache")

    # A network failure serves the last good reading, marked stale.
    http.responses["tok-main"] = ConnectionError("network down")
    s = await plan_usage.fetch_account_usage(main, force=True)
    check(s.state == "ok" and s.stale and s.week_pct == 73,
          "a network error serves the last good reading, marked stale")
    check("ConnectionError" in s.error, "the stale reading carries why")
    n1 = len(http.calls)
    await plan_usage.fetch_account_usage(main)
    check(len(http.calls) == n1, "the failure is negatively cached (no retry storm)")

    # A rewritten credentials file (the CLI refreshed or a /login) busts it.
    http.responses["tok-main2"] = (200, LEGACY_BODY)
    time.sleep(0.01)
    make_account(root, ".claude-main", "tok-main2")
    c = await plan_usage.fetch_account_usage(main)
    check(c.week_pct == 9 and not c.stale and http.calls[-1] == "tok-main2",
          "a rewritten credentials file busts the cache")

    # Concurrent callers share one request through the per-account lock.
    plan_usage._reset_cache()
    n2 = len(http.calls)
    await asyncio.gather(*(plan_usage.fetch_account_usage(main) for _ in range(5)))
    check(len(http.calls) == n2 + 1, "five concurrent reads make one request")

    # Last good with no history: the error state comes through as-is.
    plan_usage._reset_cache()
    http.responses["tok-main2"] = ConnectionError("down")
    e = await plan_usage.fetch_account_usage(main)
    check(e.state == "error" and not e.has_data,
          "an error with no last good reading reports the error")


async def test_format(root: Path, http: FakeHttp) -> None:
    print("format")
    plan_usage._reset_cache()
    main = make_account(root, ".claude-main", "tok-fmt")
    klerk = make_account(root, ".claude-klerk", None)
    http.responses["tok-fmt"] = (200, LIMITS_BODY)

    old = (config.CLAUDE_ACCOUNTS, config.PLAN_USAGE_API_ENABLED, config.PROVIDER)
    try:
        config.CLAUDE_ACCOUNTS = [main, klerk]
        config.PLAN_USAGE_API_ENABLED = True
        config.PROVIDER = "claude"
        accounts = await plan_usage.get_plan_usage()
        check([a.label for a in accounts] == ["main", "klerk"],
              "every configured account reported, in rotation order")
        bar = plan_usage.format_plan_usage_bar(accounts)
        print("    " + (bar or "").replace("\n", "\n    "))
        check(bar is not None and len(bar) <= 1024, "two-account bar fits a field")
        check("**main**" in bar and "`klerk` signed out" in bar,
              "accounts labelled, signed-out one is a single line")
        check("week **73%**" in bar and "session 15%" in bar,
              "real week and session figures shown")
        check("<t:" in bar and ":R>" in bar, "reset times are Discord timestamps")
        check("Fable 5%" in bar and "Opus" not in bar,
              "per-model line only for models with usage")
        check("est" not in bar and "$" not in bar, "no dollar estimate in the real bar")
        check("\u2014" not in bar, "no em dash")
        check("-# " not in bar and "_Fable 5%_" in bar,
              "model line is italic, not subtext (embed fields do not render -#)")

        solo = plan_usage.format_plan_usage_bar(accounts[:1])
        check("**main**" not in solo, "a single account is not labelled")

        details = plan_usage.format_plan_usage_details(accounts)
        check("73%" in details and ":f>" in details and "Fable (week): 5%" in details,
              "/usage block carries absolute reset times and per-model lines")

        # A pathological account list still fits the field.
        many = [plan_usage.AccountUsage(
            label=f"acct{i:02d}-" + "x" * 30, config_dir=f"/a{i}", state="ok",
            session_pct=50, week_pct=50,
            scoped=[(f"Model{j}", 10.0, None) for j in range(6)])
            for i in range(20)]
        big = plan_usage.format_plan_usage_bar(many)
        check(big is not None and len(big) <= 1024, "twenty accounts truncate to the field limit")

        check(plan_usage.format_plan_usage_bar([accounts[1]]) is None,
              "no figures at all returns None (caller falls back)")

        config.PROVIDER = "cursor"
        check(await plan_usage.get_plan_usage() == [], "non-claude provider reports nothing")
        config.PROVIDER = "claude"
        config.PLAN_USAGE_API_ENABLED = False
        check(await plan_usage.get_plan_usage() == [], "disabled reports nothing")
    finally:
        config.CLAUDE_ACCOUNTS, config.PLAN_USAGE_API_ENABLED, config.PROVIDER = old


async def test_fallback(root: Path, http: FakeHttp) -> None:
    print("fallback to ccusage")
    plan_usage._reset_cache()
    klerk = make_account(root, ".claude-klerk", None)
    main = make_account(root, ".claude-down", "tok-down")
    http.responses["tok-down"] = ConnectionError("down")

    async def fake_ccusage_bar() -> str:
        return "CCUSAGE BAR"

    async def fake_ccusage_details(force: bool = False) -> str:
        return "CCUSAGE DETAILS"

    old = (config.CLAUDE_ACCOUNTS, config.PLAN_USAGE_API_ENABLED, config.PROVIDER,
           usage._ccusage_bar_async, usage._ccusage_details)
    try:
        config.CLAUDE_ACCOUNTS = [main, klerk]
        config.PLAN_USAGE_API_ENABLED = True
        config.PROVIDER = "claude"
        usage._ccusage_bar_async = fake_ccusage_bar
        usage._ccusage_details = fake_ccusage_details
        check(await usage.get_usage_bar_async() == "CCUSAGE BAR",
              "every account failing falls back to the ccusage bar")

        plan_usage._reset_cache()
        http.responses["tok-down"] = (200, LIMITS_BODY)
        bar = await usage.get_usage_bar_async()
        check("week **73%**" in bar and "CCUSAGE" not in bar,
              "one account with data wins over ccusage")

        details = await usage.get_usage_details()
        check(details.startswith("**Plan usage**") and "Local cost estimate" in details
              and details.rstrip().endswith("CCUSAGE DETAILS"),
              "/usage leads with the real figures, ccusage labelled below")

        config.PLAN_USAGE_API_ENABLED = False
        check(await usage.get_usage_details() == "CCUSAGE DETAILS",
              "/usage is unchanged when the feature is off")
    finally:
        (config.CLAUDE_ACCOUNTS, config.PLAN_USAGE_API_ENABLED, config.PROVIDER,
         usage._ccusage_bar_async, usage._ccusage_details) = old


def test_endpoint_host() -> None:
    print("token destination")
    check(plan_usage.USAGE_URL.startswith("https://api.anthropic.com/"),
          "the token only ever goes to api.anthropic.com")
    src = Path(plan_usage.__file__).read_text()
    check("refreshToken" not in src and "oauth/token" not in src,
          "the module never touches the refresh token")


async def live(dirs: list[str]) -> None:
    print("live")
    plan_usage._reset_cache()
    for d in dirs:
        a = await plan_usage.fetch_account_usage(d, force=True)
        print(f"  {a.label:<10} state={a.state:<10} week={a.week_pct} "
              f"session={a.session_pct} scoped={[(n, p) for n, p, _ in a.scoped]} "
              f"week_resets={a.week_resets_at} {a.error}")
    print()
    print(plan_usage.format_plan_usage_bar(
        [await plan_usage.fetch_account_usage(d) for d in dirs]))


async def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--live"]
    if "--live" in sys.argv:
        dirs = args or list(config.CLAUDE_ACCOUNTS) or [
            os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")]
        await live(dirs)
        return 0

    root = Path(tempfile.mkdtemp(prefix="plan_usage_"))
    http = FakeHttp()
    real_get = plan_usage._http_get
    plan_usage._http_get = http
    try:
        test_parse()
        await test_states(root, http)
        await test_cache(root, http)
        await test_format(root, http)
        await test_fallback(root, http)
        test_endpoint_host()
    finally:
        plan_usage._http_get = real_get
        plan_usage._reset_cache()
        shutil.rmtree(root, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print("  - " + f)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
