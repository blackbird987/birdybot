"""Offline checks for scripts/discord_audit.py.

  * permission math: roles add up over @everyone, Administrator implies every
    other power, the server owner is labelled, members with no power are left out
  * the bot running here is "this bot", the owner is "me", other bots are "bot"
  * webhook tokens and URLs never reach the report, the JSON or the text
  * diff names new, changed and gone holders and webhooks, and settings changes
  * the source can only GET: no other HTTP method appears in the file
  * a missing token exits 2 without touching the network

Run: python scripts/test_discord_audit.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import discord_audit as audit  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(cond: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        FAILS.append(label)


GUILD = "100"
OWNER = "1"
APP = "2"
WEBHOOK_TOKEN = "tok_SECRET_abcdefghijklmnopqrstuvwxyz"
WEBHOOK_URL = f"https://discord.com/api/webhooks/900/{WEBHOOK_TOKEN}"


def _raw() -> dict:
    return {
        "guild": {"id": GUILD, "name": "Home", "owner_id": OWNER, "mfa_level": 0,
                  "verification_level": 1, "approximate_member_count": 6},
        "roles": [
            {"id": GUILD, "name": "@everyone", "permissions": str(1 << 10)},
            {"id": "r_admin", "name": "Admin", "permissions": str(audit.ADMINISTRATOR)},
            {"id": "r_hooks", "name": "Hooks", "permissions": str(audit.MANAGE_WEBHOOKS)},
            {"id": "r_roles", "name": "Roles", "permissions": str(audit.MANAGE_ROLES | (1 << 11))},
        ],
        "members": [
            {"user": {"id": OWNER, "username": "quincy"}, "roles": []},
            {"user": {"id": APP, "username": "TT-GPT", "bot": True}, "roles": ["r_admin"]},
            {"user": {"id": "3", "username": "awas"}, "roles": ["r_hooks", "r_roles"]},
            {"user": {"id": "4", "username": "OtherBot", "bot": True}, "roles": ["r_admin"]},
            {"user": {"id": "5", "username": "plain"}, "roles": []},
        ],
        "webhooks": [
            {"id": "900", "name": "Tester", "channel_id": "c1", "token": WEBHOOK_TOKEN,
             "url": WEBHOOK_URL, "user": {"username": "quincy"}},
            {"id": "901", "name": "Feed", "channel_id": "c2", "token": WEBHOOK_TOKEN + "2",
             "url": WEBHOOK_URL + "2", "user": {"username": "awas"}},
        ],
        "channels": [{"id": "c1", "name": "general"}, {"id": "c2", "name": "news"}],
        "app": {"id": APP, "bot_public": True, "bot_require_code_grant": False},
        "guilds": [{"id": GUILD, "name": "Home"}, {"id": "200", "name": "Elsewhere"}],
    }


def _report(raw=None, armed=False) -> dict:
    return audit.analyze(raw or _raw(), guild_id=GUILD, owner_id=OWNER,
                         test_webhook_ids={"900"}, armed=armed)


def test_analysis() -> None:
    print("analysis")
    r = _report()
    by = {h["name"]: h for h in r["holders"]}
    check(set(by) == {"quincy", "TT-GPT", "awas", "OtherBot"}, "members with no power are left out")
    check(by["quincy"]["kind"] == "me" and by["quincy"]["powers"] == ["owner"], "the owner is 'me' and the server owner")
    check(by["TT-GPT"]["kind"] == "this bot", "the bot running here is 'this bot'")
    check(set(by["TT-GPT"]["powers"]) == {"admin", "manage_webhooks", "manage_server", "manage_roles"},
          "Administrator implies every other power")
    check(by["OtherBot"]["kind"] == "bot", "another bot is 'bot'")
    check(by["awas"]["kind"] == "human" and set(by["awas"]["powers"]) == {"manage_webhooks", "manage_roles"},
          "powers add up across roles")
    check([h["kind"] for h in r["holders"]] == ["me", "this bot", "human", "bot"], "holders sort me, this bot, human, bot")
    check(r["everyone_powers"] == [], "@everyone with no listed power reports none")
    check(r["other_guilds"] == ["Elsewhere"], "other servers exclude this one")
    check(r["guild"]["owner_is_me"] is True, "owner_is_me")
    check(r["webhooks"][0]["test_webhook"] and r["webhooks"][0]["channel"] == "general",
          "the test webhook is flagged and sorted first, with its channel name")

    raw = _raw()
    raw["roles"][0]["permissions"] = str(audit.MANAGE_WEBHOOKS)
    r2 = _report(raw)
    check(r2["everyone_powers"] == ["manage_webhooks"], "@everyone powers are reported")
    check(any(h["name"] == "plain" and h["powers"] == ["manage_webhooks"] for h in r2["holders"]),
          "an @everyone power reaches every member")

    raw = _raw()
    raw["webhooks"] = {"_error": "HTTP 403 on /guilds/100/webhooks"}
    r3 = _report(raw)
    check(r3["webhooks"] == [] and any("webhooks" in e for e in r3["errors"]),
          "an unreadable section is reported as an error, not a crash")


def test_no_secrets() -> None:
    print("no tokens or urls")
    r = _report(armed=True)
    out = json.dumps(r) + "\n" + audit.render(r)
    check(WEBHOOK_TOKEN not in out, "no webhook token in the JSON or the text")
    check("api/webhooks" not in out and "url" not in json.dumps(r["webhooks"]),
          "no webhook url in the JSON or the text")
    check(all(set(w) == {"id", "name", "channel", "creator", "test_webhook"} for w in r["webhooks"]),
          "webhook entries carry only the five listed fields")
    check("ARMED" in audit.render(r), "the text says when test mode is armed")


def test_diff() -> None:
    print("diff")
    old = _report()
    check(audit.diff(old, copy.deepcopy(old)) == [], "an unchanged report diffs to nothing")
    raw = _raw()
    raw["members"].append({"user": {"id": "6", "username": "newmod"}, "roles": ["r_admin"]})
    raw["members"] = [m for m in raw["members"] if m["user"]["id"] != "4"]
    raw["members"][2]["roles"] = ["r_hooks"]  # awas loses Manage Roles
    raw["webhooks"].append({"id": "902", "name": "Sneaky", "channel_id": "c1", "token": "t", "user": {"username": "newmod"}})
    raw["webhooks"] = [w for w in raw["webhooks"] if w["id"] != "901"]
    raw["guild"]["mfa_level"] = 1
    raw["app"]["bot_public"] = False
    lines = audit.diff(old, _report(raw))
    text = "\n".join(lines)
    check(any(l.startswith("New: human newmod has Administrator") for l in lines), "a new holder is named")
    check(any(l.startswith("Gone: bot OtherBot") for l in lines), "a gone holder is named")
    check(any(l.startswith("Changed: awas now has Manage Webhooks") for l in lines), "a changed holder is named")
    check("New webhook: Sneaky in #general, made by newmod" in text, "a new webhook is named")
    check("Webhook removed: Feed" in text, "a removed webhook is named")
    check("Changed: 2FA level 0 -> 1" in text, "a 2FA change is named")
    check("Changed: bot is public True -> False" in text, "a public-bot change is named")


def test_source_is_get_only() -> None:
    print("read-only source")
    src = (ROOT / "scripts" / "discord_audit.py").read_text(encoding="utf-8")
    for verb in ("POST", "PATCH", "PUT", "DELETE"):
        check(verb not in src, f"no {verb} anywhere in the file")
    check(src.count('method="GET"') == 1 and src.count("method=") == 1, "the one request sets method=\"GET\"")
    check(src.count("urlopen(") == 1 and src.count("Request(") == 1, "there is exactly one request site")
    check("print(token" not in src and "print(env" not in src, "the token and env are never printed")


def test_missing_token() -> None:
    print("missing token")
    calls: list = []
    real_get = audit._get
    audit._get = lambda *a, **k: calls.append(a) or {}
    try:
        try:
            audit.fetch({"DISCORD_GUILD_ID": GUILD})
            raised = False
        except audit.AuditError:
            raised = True
        check(raised and not calls, "no token: AuditError before any request")
        real_read, real_root = audit.read_env, audit.install_root
        audit.read_env = lambda _root: {}
        audit.install_root = lambda p: p
        try:
            check(audit.main([]) == 2, "main exits 2 with no token")
        finally:
            audit.read_env, audit.install_root = real_read, real_root
    finally:
        audit._get = real_get


def main() -> int:
    test_analysis()
    test_no_secrets()
    test_diff()
    test_source_is_get_only()
    test_missing_token()
    if FAILS:
        print(f"\nFAIL: {len(FAILS)} of {CHECKS} checks failed")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print(f"\nPASS: discord_audit reads only and leaks no tokens ({CHECKS} checks).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
