"""Regression test for secret intake: the door, the vault and everything downstream.

The owner pastes keys straight into Discord. This pins that a value is out of
the text before anything else reads it, on every way text gets in, and that
only an owner's turn in a thread with no guest ever receives one.

Every key here is fake and built by concatenation, and the vault is a temp
directory: this never touches the real vault file.

Run: python scripts/test_secret_intake.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

import asyncio
import base64
import io
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import time
import traceback
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord  # noqa: E402

from bot import config, secret_detect, vault  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="secret_intake_")
config.VAULT_DIR = Path(_TMP) / "vault"

from bot.claude.provider import PROVIDERS  # noqa: E402
from bot.claude.runner.common import SESSION_STRIPPED_ENV_VARS  # noqa: E402
from bot.claude.runner.prompt_build import _build_secrets_block  # noqa: E402
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType  # noqa: E402
from bot.discord import attachments, interactions, modals, secret_door, wizard  # noqa: E402
from bot.discord.access import AccessResult  # noqa: E402
from bot.discord.bot import ClaudeBot, SecretDoorTree  # noqa: E402
from bot.discord.forums import ForumManager, ThreadInfo  # noqa: E402
from bot.engine import secret_scan, sessions  # noqa: E402
from bot.engine.directives.secret import _handle_secret_directives  # noqa: E402
from bot.platform import redact  # noqa: E402
from bot.secret_detect import ASK, BURN, REMOVED, SURE  # noqa: E402

FAILS: list[str] = []

OWNER = AccessResult(allowed=True, is_owner=True)
GUEST = AccessResult(allowed=True, is_owner=False, mode_ceiling="explore", bash_policy="none")

# Fake, and assembled here so no scanner reads this file as holding a key.
OPENAI = "sk-proj-" + "a1B2c3D4e5F6g7H8" * 3
ANTHROPIC = "sk-ant-api03-" + "Qw3rTy9Zx1Cv5Bn7" * 3
GITHUB = "ghp_" + "b7N2m4K9x3Q8w1E5r6T0" * 2
HEX64 = "9f" * 8 + "3c" * 8 + "a7" * 8 + "e1" * 8
SEED = " ".join(["abandon"] * 11 + ["about"])
PEM = "-----BEGIN " + "PRIVATE KEY-----\n" + "MIIEvQIBADANBgkq" * 4 + "\n-----END " + "PRIVATE KEY-----"
ENTROPIC = "xQ7mK2pL9vR4tY8wN3bF6hJ1sD5gZ0cA"
# Fake. The shape only: a long random body with a short tail after an "=".
ATLASSIAN = "ATATT" + "3xFfGF0" + "pQ8rS2tU6vW0xY4zA7bC1dE5fG9hJ3kL" * 5 + "=DEADBEEF"
UNKNOWN_VENDOR = "zK4" + "pQ8rS2tU6vW0xY4zA7bC1dE5fG9hJ3kL" * 5 + "=DEADBEEF"


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


def reset_vault() -> None:
    shutil.rmtree(config.VAULT_DIR, ignore_errors=True)
    vault._literal_cache = None
    redact._env_secrets_cache = None
    secret_door._holds.clear()
    secret_door._held_message_ids.clear()
    secret_door._not_secret.clear()


# --- Fake gateway ----------------------------------------------------------


class FakeMessenger:
    platform_name = "discord"

    def __init__(self):
        self.sent: list[tuple[str, str, object]] = []
        self.edits: list[tuple[str, str, str]] = []

    async def send_text(self, channel_id, text, buttons=None, silent=False):
        self.sent.append((channel_id, text, buttons))
        return f"m{len(self.sent)}"

    async def edit_text(self, channel_id, msg_id, text, buttons=None):
        self.edits.append((channel_id, msg_id, text))

    async def broadcast(self, text, **_kw):
        self.sent.append(("broadcast", text, None))

    @property
    def all_text(self) -> str:
        return "\n".join(t for _, t, _ in self.sent) + "\n".join(t for _, _, t in self.edits)


class FakeThread(discord.Thread):
    def __init__(self, id=100, name="thread", parent_id=50):  # noqa: A002
        self.id = id
        self.name = name
        self.parent_id = parent_id
        self.edited: list[str] = []

    async def edit(self, **kw):
        self.edited.append(kw.get("name"))

    async def send(self, *_a, **_kw):
        return None


class FakeAtt:
    def __init__(self, id, filename, body=b"", size=None):  # noqa: A002
        self.id = id
        self.filename = filename
        self.body = body
        self.size = len(body) if size is None else size
        self.reads = 0
        self.content_type = ""

    async def read(self):
        self.reads += 1
        return self.body


class FakeMessage:
    def __init__(self, content="", channel=None, attachments=(), fail_delete=False, id=7):  # noqa: A002
        self.id = id
        self.content = content
        self.channel = channel or FakeThread()
        self.attachments = list(attachments)
        self.deleted = False
        self.fail_delete = fail_delete
        self.author = SimpleNamespace(id=1, display_name="owner", bot=False)

    async def delete(self):
        if self.fail_delete:
            raise RuntimeError("no permission")
        self.deleted = True


class FakeForums:
    def __init__(self, repo="myrepo"):
        self.repo = repo
        self.archive_channel_ids: set[int] = set()
        self.current_message_gone: set[str] = set()

    def thread_to_project(self, _tid):
        if self.repo is None:
            return None
        return (SimpleNamespace(repo_name=self.repo), SimpleNamespace())

    def forum_by_channel_id(self, _cid):
        return None


class FakeBot:
    def __init__(self, repo="myrepo"):
        self._forums = FakeForums(repo)
        self.messenger = FakeMessenger()
        self._name_editing: set[str] = set()
        self.routed: list = []
        self.access = OWNER
        self.replays: list = []
        self._store = SimpleNamespace(get_autopilot_chain_meta=lambda _s: None, list_repos=lambda: {"myrepo": "/x"})

    async def _route_screened(self, message, access, door):
        self.routed.append((message, access, door))

    def _check_access(self, _uid, **_kw):
        return self.access

    def _is_owner(self, uid):
        return uid == 1

    async def _replay_to_thread(self, channel_id, prompt, **kw):
        self.replays.append((channel_id, prompt, kw))
        return True


async def settle_tasks() -> None:
    for _ in range(4):
        await asyncio.sleep(0)
    pending = list(secret_door._background)
    pending = [t for t in pending if not t.get_coro().__name__.startswith("_expire")]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# --- Detector --------------------------------------------------------------


def test_detector() -> None:
    print("detector")
    tiers = lambda t, **kw: [f.tier for f in secret_detect.scan(t, **kw)]  # noqa: E731
    check(SURE in tiers(f"here it is {OPENAI} thanks"), "openai key is SURE")
    check(SURE in tiers(ANTHROPIC), "anthropic key is SURE")
    check(SURE in tiers(GITHUB), "github token is SURE")
    check(BURN in tiers(SEED), "seed phrase burns")
    check(BURN in tiers(PEM), "PEM burns")
    check(ASK in tiers("0x" + HEX64), "tx hash shape goes to ASK")
    check(ASK in tiers(ENTROPIC), "high entropy token goes to ASK")
    check(not tiers("commit " + "a1b2c3d4" * 5), "commit SHA passes")
    check(not tiers("0x" + "ab12cd34" * 5), "address passes")
    check(not tiers("token: limit"), "'token: limit' passes")
    check(not tiers("task-" + "abc123" * 3), "task-... does not match sk-")
    check(not tiers("password: changeme"), "placeholder password passes")
    check(not tiers("sk-ant-" + "x" * 30), "x'd out vendor key passes")
    check(not tiers("/srv/shared/Programming/claude-telegram-bot/.worktrees/t-10147/bot/vault.py"), "path passes")
    check(not tiers(ENTROPIC, attachment=True), "entropy tier skipped in bodies")
    check(ASK in tiers(f"api_key = \"{ENTROPIC}\"", attachment=True), "lowercase labelled value in a body goes to ASK")
    check(ASK in tiers(f'{{"client-secret": "{ENTROPIC}"}}', attachment=True), "quoted JSON label in a body goes to ASK")
    check(not tiers(f"checksum = {ENTROPIC}", attachment=True), "a random value under a plain name passes in a body")
    check(not tiers("max_tokens = 4096\ntoken_limit: 100000000000000000", attachment=True), "numbers under token names pass in a body")
    check(not tiers("token = " + "a1b2c3d4" * 5, attachment=True), "a SHA under a label passes in a body")
    check(not secret_detect.scan("no secrets in this perfectly ordinary sentence"), "prose passes")
    big = ("lorem ipsum dolor sit amet " * 20000)[:500_000]
    start = time.perf_counter()
    secret_detect.scan(big)
    check(time.perf_counter() - start < 3.0, "500 KB scans in under 3 seconds")
    start = time.perf_counter()
    secret_detect.scan(("config_value = some-long-identifier-here\n" * 15000)[:500_000], attachment=True)
    check(time.perf_counter() - start < 3.0, "500 KB of assignments scans in under 3 seconds")
    check(secret_detect.is_private_key_candidate(HEX64), "hex is a private key candidate")
    check(not secret_detect.is_private_key_candidate("password-with-dashes-in-it"), "words are not")
    check(secret_detect.is_placeholder_value("<your key>"), "template value is a placeholder")
    check(not secret_detect.is_placeholder_value(ENTROPIC), "real value is not a placeholder")
    names = {f.suggested_name for f in secret_detect.scan(ANTHROPIC)}
    check(names == {"PASTED_ANTHROPIC_KEY"}, "sk-ant- is named so it cannot become ANTHROPIC_API_KEY")


# --- Vault -----------------------------------------------------------------


def test_vault() -> None:
    print("vault")
    reset_vault()
    a = vault.add(OPENAI, "OPENAI_API_KEY", "r1")
    check(a.status == "new" and a.tail == OPENAI[-4:], "new entry, tail for a long value")
    b = vault.add(OPENAI, "OTHER", "r2")
    check(b.status == "existing" and b.name == "OPENAI_API_KEY", "same value is idempotent")
    check(set(vault.list_entries()[0]["repos"]) == {"r1", "r2"}, "re-paste adds the grant")
    c = vault.add(GITHUB, "OPENAI_API_KEY", "r1")
    check(c.status == "conflict" and c.name == "OPENAI_API_KEY_2" and c.conflict_with == "OPENAI_API_KEY", "same name, new value: NAME_2")
    check(vault.add("shortval", "SHORT", "r1").tail == "", "no tail under 12 characters")
    check("value" not in json.dumps(vault.list_entries()), "list_entries holds no value")

    path = vault._path()
    check(stat.S_IMODE(path.stat().st_mode) == 0o600, "vault file is 0600")
    check(stat.S_IMODE(path.parent.stat().st_mode) == 0o700, "vault dir is 0700")

    check(vault.safe_name("ANTHROPIC_API_KEY") == "PASTED_ANTHROPIC_API_KEY", "denied name is prefixed")
    check(vault.safe_name("PATH") == "PASTED_PATH", "PATH cannot be set")
    check(vault.safe_name("GITHUB_TOKEN") == "PASTED_GITHUB_TOKEN", "GITHUB_TOKEN cannot be set")
    check(vault.safe_name("ld_preload").startswith("PASTED_"), "LD_ glob is denied, any case in")
    check(vault.safe_name("my-key") == "MY_KEY", "names are normalized")
    for name in SESSION_STRIPPED_ENV_VARS:
        check(vault.is_denied(name), f"stripped var {name} is denied")
    for p in PROVIDERS.values():
        if p is None:
            continue
        check(vault.is_denied(p.config_dir_env), f"{p.config_dir_env} is denied")
        for var in p.nested_env_vars:
            check(vault.is_denied(var), f"nested {var} is denied")

    # A hand edit cannot put a denied name into a session.
    data = json.loads(path.read_text())
    data["entries"].append({"name": "ANTHROPIC_API_KEY", "value": "x" * 20, "repos": ["r1"]})
    path.write_text(json.dumps(data))
    env = vault.env_for_repo("r1")
    check("ANTHROPIC_API_KEY" not in env and env.get("OPENAI_API_KEY") == OPENAI, "denylist rechecked at injection")
    check("OPENAI_API_KEY" not in vault.env_for_repo("nope"), "ungranted repo gets nothing")

    reset_vault()
    vault.add(OPENAI, "OPENAI_API_KEY", "r1")
    check(OPENAI not in redact.redact_secrets(f"a {OPENAI} b"), "redact strips a stored value")
    custom = "plainvalue-with-no-known-shape"
    vault.add(custom, "CUSTOM", "r1")
    check(custom not in redact.redact_secrets(f"a {custom} b"), "redact cache sees a new value at once")
    check(vault.strip_literals(f"x {custom}") == f"x {REMOVED}", "strip_literals replaces it")
    vault.forget("CUSTOM")
    check(custom in vault.strip_literals(f"x {custom}"), "forget clears the literal cache")
    check(custom in redact.scrub_known(f"x {custom}"), "forget clears the redact cache")
    vault.add("short", "TINY", "r1")
    check(vault.strip_literals("a short word") == "a short word", "values under 8 are not stripped")

    vault.rename("TINY", "TINY_NEW")
    check(vault.has_name("TINY_NEW"), "rename works")
    try:
        vault.rename("TINY_NEW", "bad name")
        check(False, "bad rename refused")
    except ValueError:
        check(True, "bad rename refused")
    vault.add(GITHUB, "OPENAI_API_KEY", "r2")
    check(vault.replace("OPENAI_API_KEY", "OPENAI_API_KEY_2"), "replace works")
    check(vault.env_for_repo("r2").get("OPENAI_API_KEY") == GITHUB, "replace moves the value and grant")

    # A corrupt file is never overwritten.
    original = "{not json " + OPENAI
    path.write_text(original)
    vault._literal_cache = None
    try:
        vault.add(GITHUB, "X_KEY")
        check(False, "corrupt vault raises")
    except vault.VaultUnavailable:
        check(True, "corrupt vault raises")
    check(path.read_text() == original, "corrupt vault left untouched")
    check(not vault.is_available(), "is_available is False")
    check(vault.env_for_repo("r1") == {}, "corrupt vault injects nothing")


# --- The door --------------------------------------------------------------


async def test_owner_sure() -> None:
    print("owner, certain key")
    reset_vault()
    bot = FakeBot()
    msg = FakeMessage(f"use this {OPENAI} please")
    door = await secret_door.screen_message(bot, msg, OWNER)
    check(door.proceed and door.deleted and msg.deleted, "routes on and deletes")
    check(OPENAI not in door.text, "value out of the text")
    check(f"[secret OPENAI_API_KEY stored, ending {OPENAI[-4:]}]" in door.text, "placeholder names it")
    check(vault.env_for_repo("myrepo").get("OPENAI_API_KEY") == OPENAI, "stored and granted to the repo")
    check(OPENAI not in bot.messenger.all_text, "report holds no value")
    _, _, rows = bot.messenger.sent[0]
    check(rows[0][0].callback_data == "secret_rename:OPENAI_API_KEY", "Rename button")
    check(rows[0][1].callback_data == "secret_forget:OPENAI_API_KEY", "Forget button")

    reset_vault()
    bot = FakeBot()
    door = await secret_door.screen_message(bot, FakeMessage(OPENAI), OWNER)
    check(door.proceed and door.text.startswith("[secret"), "key-only message still routes")

    reset_vault()
    bot = FakeBot(repo=None)
    door = await secret_door.screen_message(bot, FakeMessage(OPENAI, channel=SimpleNamespace(id=9, name="x")), OWNER)
    check("/secret grant" in bot.messenger.all_text, "no repo: the line says to grant")
    check(vault.list_entries()[0]["repos"] == [], "no repo: no grant")

    reset_vault()
    bot = FakeBot()
    bot._forums.repo = "_default"
    await secret_door.screen_message(bot, FakeMessage(OPENAI), OWNER)
    check(vault.list_entries()[0]["repos"] == [], "_default gets no grant")

    reset_vault()
    vault.add(GITHUB, "OPENAI_API_KEY", "other")
    bot = FakeBot()
    await secret_door.screen_message(bot, FakeMessage(OPENAI), OWNER)
    _, text, rows = bot.messenger.sent[0]
    check("OPENAI_API_KEY_2" in text, "name taken: stored as NAME_2")
    check(rows[0][0].callback_data == "secret_replace:OPENAI_API_KEY|OPENAI_API_KEY_2", "Replace button")
    check(rows[0][1].callback_data == "secret_keep:OPENAI_API_KEY_2", "Keep both button")

    reset_vault()
    bot = FakeBot()
    keys = " ".join(f"sk-proj-{i}a1B2c3D4e5F6g7H8j9K0m1N2p3Q4r5S6t7" for i in range(6))
    await secret_door.screen_message(bot, FakeMessage(keys), OWNER)
    _, text, rows = bot.messenger.sent[0]
    check(len(rows) == 4 and "/secret list" in text, "over 4 entries: 4 rows and a pointer")

    reset_vault()
    bot = FakeBot()
    msg = FakeMessage(OPENAI, fail_delete=True)
    door = await secret_door.screen_message(bot, msg, OWNER)
    check(OPENAI not in door.text and "could not delete" in bot.messenger.all_text, "delete failure: loud, value still out")
    check(not door.deleted, "delete failure is not recorded as deleted")

    path = vault._path()
    path.write_text("{broken")
    vault._literal_cache = None
    bot = FakeBot()
    door = await secret_door.screen_message(bot, FakeMessage(GITHUB), OWNER)
    check(GITHUB not in door.text and REMOVED in door.text, "broken vault: value still removed")
    check("can't be read" in bot.messenger.all_text, "broken vault: owner told")
    check(path.read_text() == "{broken", "broken vault: file untouched")


async def test_holds() -> None:
    print("owner, uncertain value")
    reset_vault()
    bot = FakeBot()
    msg = FakeMessage(f"try {ENTROPIC} now")
    door = await secret_door.screen_message(bot, msg, OWNER)
    check(not door.proceed and not msg.deleted, "held: not routed, not deleted")
    hold_id = next(iter(secret_door._holds))
    _, text, rows = bot.messenger.sent[-1]
    check(ENTROPIC not in text and ENTROPIC[-4:] in text, "shows the last 4 at most")
    labels = [b.label for b in rows[0]]
    check(labels[:2] == ["Secret", "Not a secret"], "Secret and Not a secret buttons")
    check("Private key" not in labels or secret_detect.is_private_key_candidate(ENTROPIC), "Private key only for key-shaped values")
    raw = vault._path().read_text()
    check(ENTROPIC not in raw and hold_id not in raw and "hold_msg_id" in raw, "marker holds ids only")
    check(secret_door.is_held(msg.id), "message is marked held")
    line, new_rows = await secret_door.resolve_hold(bot, hold_id, "yes")
    await settle_tasks()
    check(bot.routed and bot.routed[0][2].deleted and msg.deleted, "Secret deletes and routes")
    routed = bot.routed[0][2].text
    check(ENTROPIC not in routed and "[secret " in routed, "routed with a placeholder")
    check(vault.literal_values() == (ENTROPIC,), "Secret stores it")
    check(new_rows and "secret_rename" in new_rows[0][0].callback_data, "the answer offers Rename")
    check("hold_msg_id" not in vault._path().read_text(), "marker cleared")
    check(not secret_door.is_held(msg.id), "no longer held")

    reset_vault()
    bot = FakeBot()
    msg = FakeMessage(f"try {ENTROPIC} now")
    door = await secret_door.screen_message(bot, msg, OWNER)
    await secret_door.resolve_hold(bot, next(iter(secret_door._holds)), "no")
    await settle_tasks()
    check(bot.routed and bot.routed[0][2].text == f"try {ENTROPIC} now" and not msg.deleted, "Not a secret routes unchanged")
    check(not vault.list_entries(), "Not a secret stores nothing")
    again = await secret_door.screen_message(bot, FakeMessage(f"again {ENTROPIC}"), OWNER)
    check(again.proceed and not secret_door._holds, "resend does not re-ask")
    check(not secret_door.edit_needs_door(f"edit {ENTROPIC}"), "edit does not re-ask")
    check(ENTROPIC not in json.dumps(list(secret_door._not_secret)), "remembered as a hash")

    reset_vault()
    bot = FakeBot()
    hexkey = "0x" + HEX64
    msg = FakeMessage(hexkey)
    await secret_door.screen_message(bot, msg, OWNER)
    _, _, rows = bot.messenger.sent[-1]
    check("Private key" in [b.label for b in rows[0]], "hex offers Private key")
    line, _ = await secret_door.resolve_hold(bot, next(iter(secret_door._holds)), "pk")
    await settle_tasks()
    check(not vault.list_entries() and msg.deleted, "Private key: deleted, stored nothing")
    check("exposed" in line and "funds" in line, "Private key: says exposed, move the funds")
    check(hexkey not in bot.routed[0][2].text and REMOVED in bot.routed[0][2].text, "Private key: value gone from the route")

    reset_vault()
    bot = FakeBot()
    msg = FakeMessage(ENTROPIC)
    await secret_door.screen_message(bot, msg, OWNER)
    hold_id = next(iter(secret_door._holds))
    await secret_door.expire_hold(bot, hold_id)
    check(not secret_door._holds and not msg.deleted and not bot.routed, "timeout drops it, message stays")
    check("Paste it again" in bot.messenger.all_text and "delete it" in bot.messenger.all_text, "timeout tells the owner")
    check("hold_msg_id" not in vault._path().read_text(), "timeout clears the marker")

    line, rows = await secret_door.resolve_hold(bot, "unknown", "yes")
    check("expired" in line and rows == [], "unknown hold settles as expired")

    reset_vault()
    vault.add_hold_marker("55", "66")
    bot = FakeBot()
    await secret_door.notify_leftover_holds(bot)
    check(bot.messenger.edits and bot.messenger.edits[0][:2] == ("55", "66"), "restart: leftover hold line edited")
    check("restarted" in bot.messenger.edits[0][2], "restart: says so")
    check("hold_msg_id" not in vault._path().read_text(), "restart: markers cleared")
    for task in list(secret_door._background):
        task.cancel()


async def test_burn_and_guest() -> None:
    print("burn and guest")
    reset_vault()
    bot = FakeBot()
    msg = FakeMessage(f"my seed is {SEED}")
    door = await secret_door.screen_message(bot, msg, OWNER)
    check(door.proceed and msg.deleted and "abandon" not in door.text, "seed removed and deleted")
    check(not vault.list_entries(), "seed stored nowhere")
    check("exposed" in bot.messenger.all_text and "Move the funds" in bot.messenger.all_text, "seed warning")

    for value, label in ((OPENAI, "SURE"), (ENTROPIC, "ASK"), (SEED, "BURN")):
        reset_vault()
        bot = FakeBot()
        msg = FakeMessage(f"guest says {value}")
        door = await secret_door.screen_message(bot, msg, GUEST)
        check(door.proceed and msg.deleted and door.text == f"guest says {REMOVED}", f"guest {label}: deleted, [secret removed]")
        check(not vault.list_entries() and not secret_door._holds, f"guest {label}: nothing stored, no hold")
        check("replace that key" in bot.messenger.all_text, f"guest {label}: warned")


async def test_title() -> None:
    print("thread title")
    reset_vault()
    bot = FakeBot()
    thread = FakeThread(name=f"deploy {OPENAI}")
    msg = FakeMessage("hello", channel=thread)
    door = await secret_door.screen_message(bot, msg, OWNER)
    check(OPENAI not in thread.name and REMOVED in thread.name, "name scrubbed locally at once")
    check(door.proceed and door.text == "hello" and not msg.deleted, "the message itself is left alone")
    check(vault.has_name("OPENAI_API_KEY"), "owner: stored")
    await settle_tasks()
    check(thread.edited and OPENAI not in thread.edited[0], "real edit runs in the background")

    bot = FakeBot()
    bot._name_editing.add(str(thread.id))
    thread2 = FakeThread(name=f"x {GITHUB}")
    await secret_door.screen_message(bot, FakeMessage("hi", channel=thread2), GUEST)
    check(GITHUB not in thread2.name and not thread2.edited, "waits for a rename in flight")
    bot._name_editing.clear()
    await settle_tasks()
    check(thread2.edited, "then renames")


def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


async def test_attachments() -> None:
    print("attachments")
    reset_vault()
    bot = FakeBot()
    txt = FakeAtt(1, "notes.txt", f"the key is {OPENAI}\n".encode())
    img = FakeAtt(2, "shot.png", b"\x89PNG")
    pdf = FakeAtt(3, "a.pdf", b"%PDF")
    big = FakeAtt(4, "huge.png", b"x", size=attachments.ATTACH_IMAGE_MAX + 1)
    msg = FakeMessage("see file", attachments=[txt, img, pdf, big])
    door = await secret_door.screen_message(bot, msg, OWNER)
    check(OPENAI not in door.sanitized[1] and "[secret OPENAI_API_KEY" in door.sanitized[1], "text body sanitized")
    check(msg.deleted and door.prefetched.get(2) == b"\x89PNG" and door.prefetched.get(3) == b"%PDF", "others fetched before the delete")
    check(4 not in door.prefetched, "oversize is not fetched")
    check(txt.reads == 1, "body read once")

    with tempfile.TemporaryDirectory() as tmp:
        saved = config.PENDING_IMAGES_DIR
        config.PENDING_IMAGES_DIR = Path(tmp)
        try:
            msg.channel = SimpleNamespace(id=100, name="t", send=_noop_send)
            text, paths, go_on = await attachments.ingest_attachments(
                msg, door.text, voice_enabled=False, quiet=True,
                prefetched=door.prefetched, sanitized=door.sanitized, consumed=door.consumed,
            )
        finally:
            config.PENDING_IMAGES_DIR = saved
    check(go_on and OPENAI not in text and "[secret OPENAI_API_KEY" in text, "ingest uses the sanitized body")
    check(txt.reads == 1 and img.reads == 1 and pdf.reads == 1, "ingest never re-reads a body")
    check(len(paths) == 2, "image and PDF still ingest")

    reset_vault()
    bot = FakeBot()
    word = FakeAtt(5, "plan.docx", _docx(f"token {GITHUB} end"))
    door = await secret_door.screen_message(bot, FakeMessage("doc", attachments=[word]), OWNER)
    check(GITHUB not in door.sanitized[5] and vault.has_name("GITHUB_TOKEN") is False and vault.list_entries(), ".docx body scanned, GitHub name kept out")

    reset_vault()
    bot = FakeBot()
    named = FakeAtt(6, f"{OPENAI}.txt", b"hello")
    msg = FakeMessage("x", attachments=[named])
    door = await secret_door.screen_message(bot, msg, OWNER)
    check(OPENAI not in named.filename and named.filename.endswith(".txt"), "filename scrubbed, extension kept")
    check(msg.deleted and vault.list_entries(), "key in a filename: stored and deleted")

    reset_vault()
    bot = FakeBot()
    named = FakeAtt(8, "password=hunter2hunter2.txt", b"hello")
    msg = FakeMessage("x", attachments=[named])
    door = await secret_door.screen_message(bot, msg, OWNER)
    check("hunter2hunter2" not in named.filename and door.proceed and not secret_door._holds and not msg.deleted, "uncertain filename scrubbed with no hold")

    reset_vault()
    bot = FakeBot()
    broken = FakeAtt(9, "n.txt", b"x")
    async def fail_read():
        raise RuntimeError("cdn gone")
    broken.read = fail_read
    door = await secret_door.screen_message(bot, FakeMessage("x", attachments=[broken]), OWNER)
    check(9 in door.consumed and "couldn't open" in bot.messenger.all_text, "unreadable body is consumed, never passed on")

    print(".env files")
    for name in (".env", "env", ".env.local", "env.production", "prod.env", ".env.txt", "ENV"):
        check(secret_door.is_env_file(name), f"{name} is an env file")
    for name in ("env.py", "env.json", "environment.txt", "readme.md", "myenv"):
        check(not secret_door.is_env_file(name), f"{name} is not")
    body = "\n".join([
        "# comment",
        f"export STRIPE_API_KEY={OPENAI}",
        'DB_PASSWORD="hunter2hunter2"',
        "ANTHROPIC_API_KEY=" + ANTHROPIC,
        "WALLET_PRIVATE_KEY=" + HEX64,
        f"RECOVERY={SEED}",
        "DEBUG=true",
        "PORT=8080",
        "API_KEY=changeme",
        "EMPTY_TOKEN=",
        "VALUE_ONLY=" + GITHUB,
    ]).encode()
    reset_vault()
    bot = FakeBot()
    env = FakeAtt(10, ".env", body)
    msg = FakeMessage("", attachments=[env])
    door = await secret_door.screen_message(bot, msg, OWNER)
    stored = {e["name"] for e in vault.list_entries()}
    check({"STRIPE_API_KEY", "DB_PASSWORD", "PASTED_ANTHROPIC_API_KEY", "VALUE_ONLY"} <= stored, "secret-looking lines stored under their own names")
    check(not stored & {"WALLET_PRIVATE_KEY", "RECOVERY", "DEBUG", "PORT", "API_KEY", "EMPTY_TOKEN"}, "burned and plain lines not stored")
    check(msg.deleted and 10 in door.consumed, "message deleted, file consumed")
    for value in (OPENAI, ANTHROPIC, HEX64, "abandon", "hunter2hunter2", GITHUB):
        check(value not in door.text and value not in bot.messenger.all_text, f"no value in the session text: {value[:6]}...")
    check("WALLET_PRIVATE_KEY" in door.text and "PORT" in door.text and "STRIPE_API_KEY" in door.text, "names reach the session")
    check("exposed" in bot.messenger.all_text, "burned lines warn")

    reset_vault()
    bot = FakeBot()
    env = FakeAtt(11, ".env", body)
    msg = FakeMessage("", attachments=[env])
    door = await secret_door.screen_message(bot, msg, GUEST)
    check(not vault.list_entries() and msg.deleted and OPENAI not in door.text, "guest .env: deleted, names only, stored nothing")
    check("STRIPE_API_KEY" in door.text, "guest .env: names reach the session")

    reset_vault()
    bot = FakeBot()
    py = FakeAtt(12, "env.py", b"import os\n")
    msg = FakeMessage("x", attachments=[py])
    door = await secret_door.screen_message(bot, msg, OWNER)
    check(not msg.deleted and py.reads == 0, "env.py is left alone")


async def _noop_send(*_a, **_kw):
    return None


async def test_voice() -> None:
    print("voice")
    reset_vault()
    bot = FakeBot()
    msg = FakeMessage("")
    door = secret_door.DoorResult(text="")
    screen = secret_door.transcript_screen(bot, msg, door)
    check(await screen("just a normal voice note") == "just a normal voice note", "clean words pass")
    check(not msg.deleted, "clean: message stays")
    out = await screen(f"the key is {OPENAI}")
    check(out == REMOVED and msg.deleted and door.deleted, "hit: deleted, [secret removed]")
    check(not vault.list_entries(), "hit: vaults nothing")
    check("OpenAI already has" in bot.messenger.all_text, "hit: says OpenAI has it")

    import bot.services.audio as audio
    saved = audio.transcribe

    async def fake_transcribe(_b, filename=None):
        return f"secret is {OPENAI}"

    audio.transcribe = fake_transcribe
    sent: list[str] = []

    async def capture(content=None, **_kw):
        sent.append(content or "")
        return SimpleNamespace(edit=_noop_send)

    try:
        b = FakeBot()
        msg = FakeMessage("")
        msg.channel = SimpleNamespace(id=100, name="t", send=capture, typing=lambda: _NullCtx())
        msg.attachments = [FakeAtt(20, "note.ogg", b"ogg")]
        door = secret_door.DoorResult(text="")
        text, _, go_on = await attachments.ingest_attachments(
            msg, "", voice_enabled=True, quiet=True,
            transcript_screen=secret_door.transcript_screen(b, msg, door),
        )
        check(OPENAI not in text and REMOVED in text, "ingest runs the screen on the transcript")
        check(all(OPENAI not in s for s in sent), "the echo never shows it")
    finally:
        audio.transcribe = saved


class _NullCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class FakeResponse:
    def __init__(self):
        self.messages: list[str] = []
        self.modal = None
        self.edited = None

    async def send_message(self, text, **_kw):
        self.messages.append(text)

    async def send_modal(self, modal):
        self.modal = modal

    async def defer(self, **_kw):
        return None

    async def edit_message(self, **kw):
        self.edited = kw

    def is_done(self):
        return False


class FakeFollowup:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, text=None, **_kw):
        self.sent.append(text)
        return SimpleNamespace(edit=_noop_send)


def _slash(options, *, user_id=1, itype=discord.InteractionType.application_command, channel=None):
    return SimpleNamespace(
        type=itype, data={"options": options}, channel_id=100,
        channel=channel or FakeThread(), user=SimpleNamespace(id=user_id, display_name="o"),
        response=FakeResponse(), followup=FakeFollowup(),
    )


async def test_slash() -> None:
    print("slash commands")
    reset_vault()
    bot = FakeBot()
    opts = [{"type": 1, "name": "sub", "options": [
        {"type": 2, "name": "grp", "options": [{"type": 3, "name": "p", "value": f"do {OPENAI}"}]},
        {"type": 4, "name": "n", "value": 3},
    ]}]
    i = _slash(opts)
    check(await secret_door.screen_slash(bot, i), "owner SURE: runs")
    value = opts[0]["options"][0]["options"][0]["value"]
    check(OPENAI not in value and "[secret OPENAI_API_KEY" in value, "rewritten in place through types 1 and 2")
    await settle_tasks()
    check(vault.has_name("OPENAI_API_KEY") and bot.messenger.sent, "stored, channel line posted")

    for bad, label in ((ENTROPIC, "ASK"), (SEED, "BURN")):
        reset_vault()
        i = _slash([{"type": 3, "name": "p", "value": bad}])
        check(not await secret_door.screen_slash(FakeBot(), i), f"owner {label}: refused")
        check("/secret add" in i.response.messages[0], f"owner {label}: points at /secret add")
        check(not vault.list_entries(), f"owner {label}: stored nothing")

    reset_vault()
    bot = FakeBot()
    bot.access = GUEST
    opts = [{"type": 3, "name": "p", "value": f"x {OPENAI}"}]
    check(await secret_door.screen_slash(bot, _slash(opts)), "guest: runs")
    check(opts[0]["value"] == f"x {REMOVED}" and not vault.list_entries(), "guest: rewritten, stored nothing")

    bot = FakeBot()
    bot.access = AccessResult(allowed=False, is_owner=False)
    opts = [{"type": 3, "name": "p", "value": OPENAI}]
    check(await secret_door.screen_slash(bot, _slash(opts)) and not vault.list_entries(), "no access: left to the handler, nothing stored")
    auto = _slash([{"type": 3, "name": "p", "value": OPENAI}], itype=discord.InteractionType.autocomplete)
    check(await secret_door.screen_slash(FakeBot(), auto), "autocomplete passes at once")

    saved = secret_detect.scan
    def boom(*_a, **_kw):
        raise RuntimeError("bug")
    secret_detect.scan = boom
    try:
        i = _slash([{"type": 3, "name": "p", "value": "x"}])
        check(not await secret_door.screen_slash(FakeBot(), i), "a bug in the screen fails closed")
    finally:
        secret_detect.scan = saved

    tree = SecretDoorTree(discord.Client(intents=discord.Intents.none()))
    check(type(tree).interaction_check is not discord.app_commands.CommandTree.interaction_check, "the tree overrides interaction_check")


async def test_modals() -> None:
    print("modals")
    reset_vault()
    bot = FakeBot()
    ran: list[str] = []

    async def run(text):
        ran.append(text)

    i = _slash([])
    await secret_door.screen_modal_text(bot, i, OWNER, "plain prompt", "myrepo", run)
    check(ran == ["plain prompt"], "clean text runs as is")

    ran.clear()
    i = _slash([])
    await secret_door.screen_modal_text(bot, i, OWNER, f"go {OPENAI}", "myrepo", run)
    check(len(ran) == 1 and OPENAI not in ran[0] and vault.has_name("OPENAI_API_KEY"), "SURE: stored, runs clean")

    ran.clear()
    i = _slash([])
    await secret_door.screen_modal_text(bot, i, OWNER, f"try {ENTROPIC}", "myrepo", run)
    check(not ran and secret_door._holds, "ASK: holds, does not run")
    hold_id = next(iter(secret_door._holds))
    check(secret_door._holds[hold_id].message is None, "modal hold has no message")
    check(ENTROPIC not in vault._path().read_text(), "modal hold writes nothing")
    await secret_door.resolve_hold(bot, hold_id, "yes")
    await settle_tasks()
    check(ran and ENTROPIC not in ran[0], "Secret continues with the placeholder")

    ran.clear()
    i = _slash([])
    await secret_door.screen_modal_text(bot, i, GUEST, f"x {OPENAI}", "myrepo", run)
    check(ran == [f"x {REMOVED}"], "guest: runs with [secret removed]")

    # QuickTaskModal screens before prompt[:60] names a thread.
    reset_vault()
    bot = FakeBot()
    named: list[str] = []
    prompts: list[str] = []

    async def get_thread(_repo, _x, title):
        named.append(title)
        return None

    bot._forums.get_or_create_session_thread = get_thread
    modal = modals.QuickTaskModal(bot, "myrepo", OWNER)
    modal.prompt_input._value = f"{OPENAI} deploy"
    await modal.on_submit(_slash([]))
    check(named and OPENAI not in named[0] and OPENAI[:12] not in named[0], "thread title never sees the key")
    check(vault.has_name("OPENAI_API_KEY"), "QuickTask stored it")

    bot = FakeBot()
    m = wizard.RepoNameModal(bot, "create")
    m.name._value = OPENAI
    i = _slash([])
    await m.on_submit(i)
    check(i.response.messages and "secret" in i.response.messages[0], "repo name refuses a hit")

    reset_vault()
    bot = FakeBot()
    add = modals.SecretAddModal(bot, "myrepo")
    add.secret_name._value = ""
    add.value._value = OPENAI
    i = _slash([])
    await add.on_submit(i)
    check(vault.env_for_repo("myrepo").get("OPENAI_API_KEY") == OPENAI, "/secret add stores under the shape's name")
    check(OPENAI not in i.response.messages[0], "/secret add reply holds no value")
    add = modals.SecretAddModal(bot, "myrepo")
    add.secret_name._value = ""
    add.value._value = SEED
    i = _slash([])
    await add.on_submit(i)
    check(len(vault.list_entries()) == 1 and "never stored" in i.response.messages[0], "/secret add refuses a seed")
    add = modals.SecretAddModal(bot, "myrepo")
    add.secret_name._value = OPENAI
    add.value._value = "somevalue1234"
    i = _slash([])
    await add.on_submit(i)
    check(len(vault.list_entries()) == 1, "/secret add refuses a key as a name")
    add = modals.SecretAddModal(bot, None)
    add.secret_name._value = "PATH"
    add.value._value = "/evil/bin:/also/evil"
    await add.on_submit(_slash([]))
    check(vault.has_name("PASTED_PATH") and not vault.has_name("PATH"), "/secret add cannot set PATH")
    add = modals.SecretAddModal(bot, "myrepo")
    add.value._value = "v" * 20
    add.secret_name._value = "OTHER_KEY"
    i = _slash([], user_id=2)
    await add.on_submit(i)
    check(not vault.has_name("OTHER_KEY") and "Owner" in i.response.messages[0], "/secret add is owner only")


# --- on_message plumbing ---------------------------------------------------


def _route_self():
    return SimpleNamespace(
        _voice_enabled=False,
        _forums=SimpleNamespace(archive_channel_ids=set()),
    )


async def test_routing() -> None:
    print("routing after the door")
    from bot.discord import bot as bot_mod

    seen: list = []
    handled: list = []

    async def fake_ingest(message, text, **kw):
        seen.append((message.content, text, kw))
        return text, [], False

    async def fake_memes(message):
        handled.append(message.id)
        return False

    saved = (attachments.ingest_attachments, bot_mod.memes.handle, bot_mod.memes._channel_id)
    attachments.ingest_attachments = fake_ingest
    bot_mod.memes.handle = fake_memes
    bot_mod.memes._channel_id = lambda: 555
    try:
        fake = _route_self()
        msg = FakeMessage("raw")
        door = secret_door.DoorResult(text="clean", deleted=True, prefetched={1: b"x"}, consumed={2}, sanitized={3: "s"})
        await ClaudeBot._route_screened(fake, msg, OWNER, door)
        check(msg.content == "clean" and seen[-1][1] == "clean", "the sanitized text is what continues")
        check(not handled, "a deleted message skips memes")
        kw = seen[-1][2]
        check(kw["prefetched"] == {1: b"x"} and kw["consumed"] == {2} and kw["sanitized"] == {3: "s"}, "door data reaches ingest")
        check(callable(kw["transcript_screen"]), "transcript screen reaches ingest")

        seen.clear()
        msg = FakeMessage("raw", channel=SimpleNamespace(id=555, name="memes"))
        await ClaudeBot._route_screened(fake, msg, OWNER, secret_door.DoorResult(text="clean", deleted=True))
        check(not seen and not handled, "deleted in the meme channel: stops there")

        msg = FakeMessage("hello")
        await ClaudeBot._route_screened(fake, msg, OWNER, secret_door.DoorResult(text="hello"))
        check(handled == [msg.id], "an untouched message still reaches memes")
    finally:
        attachments.ingest_attachments, bot_mod.memes.handle, bot_mod.memes._channel_id = saved

    # on_message fails closed: a door bug never routes.
    routed: list = []
    sends: list = []

    async def boom(*_a):
        raise RuntimeError("bug")

    async def route(*a):
        routed.append(a)

    async def send(text, **_kw):
        sends.append(text)

    saved_screen = secret_door.screen_message
    secret_door.screen_message = boom
    try:
        user = object()
        fake = SimpleNamespace(
            user=user, _in_scope=lambda *_a: True, _check_access=lambda *_a, **_k: OWNER,
            _route_screened=route,
        )
        msg = FakeMessage(OPENAI, channel=SimpleNamespace(id=1, send=send))
        msg.guild = None
        msg.webhook_id = None
        await ClaudeBot.on_message(fake, msg)
        check(not routed and sends and "screen" in sends[0], "a door failure does not route and says so")
    finally:
        secret_door.screen_message = saved_screen


async def test_edits() -> None:
    print("edits")
    reset_vault()
    fetches: list = []
    screened: list = []

    class Channel:
        id = 100
        guild = None

        async def fetch_message(self, mid):
            fetches.append(mid)
            return FakeMessage(OPENAI, channel=self)

    async def fake_screen(_bot, message, access):
        screened.append(access)

    saved = secret_door.screen_edit
    secret_door.screen_edit = fake_screen
    me = SimpleNamespace(id=999)
    fake = SimpleNamespace(
        user=me, _in_scope=lambda *_a: True, get_channel=lambda _i: Channel(),
        _check_access=lambda *_a, **_k: OWNER,
    )

    def payload(content=OPENAI, author="1", webhook=None, bot=False, mid=7):
        data = {"author": {"id": author, "bot": bot}}
        if content is not None:
            data["content"] = content
        if webhook:
            data["webhook_id"] = webhook
        return SimpleNamespace(data=data, channel_id=100, message_id=mid)

    try:
        await ClaudeBot.on_raw_message_edit(fake, payload(author="999"))
        check(not fetches, "the bot's own card is skipped")
        await ClaudeBot.on_raw_message_edit(fake, payload(bot=True, author="5"))
        check(not fetches, "another bot is skipped")
        await ClaudeBot.on_raw_message_edit(fake, payload(webhook="123", author="5"))
        check(not fetches, "a non-test webhook is skipped")
        await ClaudeBot.on_raw_message_edit(fake, payload(content=None))
        check(not fetches, "no content is skipped")
        await ClaudeBot.on_raw_message_edit(fake, payload(content="just text"))
        check(not fetches, "no hit means no fetch")
        secret_door._held_message_ids.add(77)
        await ClaudeBot.on_raw_message_edit(fake, payload(mid=77))
        check(not fetches, "already held is skipped")
        secret_door._held_message_ids.clear()
        fake._check_access = lambda *_a, **_k: AccessResult(allowed=False, is_owner=False)
        await ClaudeBot.on_raw_message_edit(fake, payload())
        check(not fetches, "no access is skipped")
        fake._check_access = lambda *_a, **_k: OWNER
        await ClaudeBot.on_raw_message_edit(fake, payload())
        check(fetches == [7] and screened, "a hit fetches and screens")
    finally:
        secret_door.screen_edit = saved

    reset_vault()
    bot = FakeBot()
    msg = FakeMessage(f"edited {OPENAI}")
    await secret_door.screen_edit(bot, msg, OWNER)
    check(msg.deleted and vault.has_name("OPENAI_API_KEY") and not bot.routed, "edit: stored, deleted, never routed")

    reset_vault()
    bot = FakeBot()
    msg = FakeMessage(f"edited {ENTROPIC}")
    await secret_door.screen_edit(bot, msg, OWNER)
    hold_id = next(iter(secret_door._holds))
    check(not msg.deleted and secret_door.is_held(msg.id), "edit: uncertain value is held")
    await secret_door.resolve_hold(bot, hold_id, "no")
    await settle_tasks()
    check(not bot.routed and not msg.deleted, "edit: Not a secret does nothing")
    for task in list(secret_door._background):
        task.cancel()


# --- History ---------------------------------------------------------------


class FakeHistoryMsg:
    def __init__(self, content, *, is_bot=False):
        self.content = content
        self.webhook_id = None
        self.author = SimpleNamespace(id=999 if is_bot else 1, bot=is_bot)


async def test_priming() -> None:
    print("priming and /ref")
    reset_vault()
    literal = "plainvalue-with-no-known-shape"
    vault.add(literal, "PLAIN", "r")
    thread = FakeThread(id=300)
    history = [
        FakeHistoryMsg(f"old paste {OPENAI}"),
        FakeHistoryMsg(f"bot quoted {literal}", is_bot=True),
        FakeHistoryMsg("earlier question"),
    ]

    def hist(limit=None, oldest_first=False):
        async def gen():
            for m in reversed(history):
                yield m
        return gen()

    fm = ForumManager.__new__(ForumManager)
    fm._client = SimpleNamespace(user=SimpleNamespace(id=999))
    fm._store = SimpleNamespace(get_autopilot_chain=lambda _s: None)
    fm._prime_cache = {}
    fm.current_message_gone = set()
    fm.thread_to_project = lambda _t: (None, SimpleNamespace(session_id=None, topic="t"))

    class Hist(FakeThread):
        def history(self, limit=None, oldest_first=False):
            return hist(limit, oldest_first)

    async def get(_tid):
        return Hist(id=300)

    fm._get_or_fetch = get
    fm.current_message_gone.add("300")
    out = await ForumManager.build_prime_briefing(fm, "300", use_cache=False)
    check(out and "earlier question" in out, "deleted current message: the earlier one is kept")
    check(OPENAI not in out and literal not in out, "history is scrubbed, both roles")
    fm.current_message_gone.clear()
    out = await ForumManager.build_prime_briefing(fm, "300", use_cache=False)
    check(not out or "earlier question" not in out, "normal turn still drops the newest message")

    ref = ForumManager.build_ref_context(
        SimpleNamespace(repo_name="r"), SimpleNamespace(topic="t"),
        [{"role": "user", "text": f"a {literal} b {OPENAI}"}], "300",
    )
    check(literal not in ref and OPENAI not in ref, "/ref context is scrubbed")


# --- Who gets a key --------------------------------------------------------


def _instance(**kw) -> Instance:
    base = dict(
        id="t-1", name=None, instance_type=InstanceType.QUERY, prompt="p", repo_name="myrepo",
        repo_path="/tmp", session_id="sess", status=InstanceStatus.RUNNING,
    )
    base.update(kw)
    return Instance(**base)


def test_block_and_flags() -> None:
    print("who gets a key")
    reset_vault()
    vault.add(OPENAI, "OPENAI_API_KEY", "myrepo")
    vault.add(GITHUB, "OTHER_KEY", "elsewhere")
    inst = _instance()
    check(inst.vault_eligible is False, "vault_eligible defaults to False")
    check(_build_secrets_block(inst) == "", "not eligible: no block")
    inst.vault_eligible = True
    block = _build_secrets_block(inst)
    check("OPENAI_API_KEY" in block and "OTHER_KEY" in block and "/secret request" in block, "block lists names and how to ask")
    check(OPENAI not in block and GITHUB not in block, "block holds no value")
    inst.is_owner_session = False
    check(_build_secrets_block(inst) == "", "guest: no block")
    reset_vault()
    inst.is_owner_session = True
    check(_build_secrets_block(inst) == "", "empty vault: no block")

    d = _instance(vault_eligible=True).to_dict()
    check(Instance.from_dict(d).vault_eligible is True, "the flag survives a round trip")
    d.pop("vault_eligible")
    check(Instance.from_dict(d).vault_eligible is False, "old records stay ineligible")

    root = Path(__file__).resolve().parents[1] / "bot"
    query = (root / "engine/query.py").read_text()
    check("inst.vault_eligible = ctx.is_owner and not ctx.thread_has_guest" in query, "a typed turn: owner with no guest")
    check("vault.strip_literals(prompt)" in query, "query strips literals before the instance exists")
    core = (root / "claude/runner/core.py").read_text()
    check("instance.is_owner_session and instance.vault_eligible and instance.repo_name" in core, "env injection has the full condition")
    check("vault.strip_literals(prompt_text)" in core, "runner strips literals again")
    spawn = (root / "engine/step_spawn.py").read_text()
    check(spawn.count("vault_eligible") >= 2, "chain steps inherit the flag")
    check("is_owner" in (root / "discord/usage_gate.py").read_text(), "usage queue remembers who asked")


async def test_env_injection() -> None:
    print("env injection at the subprocess boundary")
    from bot.claude.runner import ClaudeRunner

    class _Stdin:
        def write(self, _d):
            return None
        async def drain(self):
            return None
        def close(self):
            return None
        async def wait_closed(self):
            return None

    class _Proc:
        pid = 96123
        returncode = 0
        stdin = _Stdin()
        stdout = None
        stderr = None
        def kill(self):
            return None
        async def wait(self):
            return 0

    reset_vault()
    vault.add(OPENAI, "PROJECT_API_KEY", "myrepo")
    vault.add(ANTHROPIC, "PASTED_ANTHROPIC_KEY", "myrepo")
    saved_spawn = asyncio.create_subprocess_exec
    saved_accounts = list(config.CLAUDE_ACCOUNTS)
    config.CLAUDE_ACCOUNTS[:] = []
    calls: list[dict] = []
    stdin_seen: list = []

    async def fake_spawn(*args, **kw):
        calls.append(dict(kw.get("env") or {}))
        return _Proc()

    asyncio.create_subprocess_exec = fake_spawn

    async def run(inst):
        runner = ClaudeRunner()
        from bot.claude.types import RunResult

        async def stream(_proc, _inst, *_a, **_k):
            return RunResult(result_text="ok", session_id="s", is_error=False, cost_usd=0.0, duration_ms=1, num_turns=1)

        runner._stream_output = stream
        try:
            await runner.run(inst)
        except Exception as exc:
            print(f"    (run raised {type(exc).__name__})")

    try:
        with tempfile.TemporaryDirectory() as repo:
            await run(_instance(repo_path=repo, vault_eligible=True, session_id=None))
            check(bool(calls), "the runner spawned")
            env = calls[-1] if calls else {}
            check(env.get("PROJECT_API_KEY") == OPENAI, "owner, no guest: gets the key")
            check(env.get("ANTHROPIC_API_KEY") != ANTHROPIC, "pasted sk-ant- never becomes ANTHROPIC_API_KEY")
            check(env.get("PASTED_ANTHROPIC_KEY") == ANTHROPIC, "it sits under its own name")
            calls.clear()
            await run(_instance(repo_path=repo, vault_eligible=False, session_id=None))
            check(calls and "PROJECT_API_KEY" not in calls[-1], "not eligible: no key")
            calls.clear()
            await run(_instance(repo_path=repo, vault_eligible=True, is_owner_session=False, session_id=None))
            # A guest in a repo that cannot be sandboxed (a temp dir) does not start at all.
            check(not calls or "PROJECT_API_KEY" not in calls[-1], "guest session: no key")
            calls.clear()
            await run(_instance(repo_path=repo, vault_eligible=True, repo_name="other", session_id=None))
            check(calls and "PROJECT_API_KEY" not in calls[-1], "ungranted repo: no key")
    finally:
        asyncio.create_subprocess_exec = saved_spawn
        config.CLAUDE_ACCOUNTS[:] = saved_accounts


async def test_guest_routes() -> None:
    print("guest across replay, retry, Steer, drain")
    from bot.discord.thread_ctx import ThreadContextMixin
    from bot.store.state import StateStore

    mixin = SimpleNamespace(
        _discord_user_id=1,
        _check_access=lambda uid, **_k: OWNER if uid == 1 else GUEST,
    )
    access = ThreadContextMixin._guest_access(mixin, "2", "100")
    check(not access.is_owner, "a guest's stored id resolves to guest access")
    access = ThreadContextMixin._guest_access(mixin, "1", "100")
    check(not access.is_owner, "nothing resolves up to the owner through this path")
    access = ThreadContextMixin._guest_access(mixin, None, "100")
    check(not access.is_owner and access.mode_ceiling == "explore", "unknown id: the narrowest run")
    guest_id = ThreadContextMixin._thread_guest_id(
        mixin, SimpleNamespace(user_id="2", user_ids=set()),
    )
    check(guest_id == "2", "a guest's thread is seen as having a guest")
    owner_only = ThreadContextMixin._thread_guest_id(
        mixin, SimpleNamespace(user_id="1", user_ids={"1"}),
    )
    check(owner_only is None, "the owner's thread has none")

    # A child reports back to its parent, so the parent's guest counts for keys.
    guest_parent = ThreadInfo(thread_id="10", user_id="2")
    clean_parent = ThreadInfo(thread_id="20", user_id="1")
    child = ThreadInfo(thread_id="11", user_id="1", parent_thread_id="10")
    grandchild = ThreadInfo(thread_id="12", user_id="1", parent_thread_id="11")
    clean_child = ThreadInfo(thread_id="21", user_id="1", parent_thread_id="20")
    loop_a = ThreadInfo(thread_id="30", user_id="1", parent_thread_id="31")
    loop_b = ThreadInfo(thread_id="31", user_id="1", parent_thread_id="30")
    orphan = ThreadInfo(thread_id="40", user_id="1", parent_thread_id="999")
    family = {t.thread_id: (None, t) for t in (guest_parent, clean_parent, child, grandchild, clean_child, loop_a, loop_b, orphan)}
    fam = SimpleNamespace(_discord_user_id=1, _forums=SimpleNamespace(thread_to_project=family.get))
    fam._thread_guest_id = lambda i: ThreadContextMixin._thread_guest_id(fam, i)
    has_guest = lambda i: ThreadContextMixin._family_has_guest(fam, i)  # noqa: E731
    check(has_guest(child), "an owner's child of a guest's thread gets no keys")
    check(has_guest(grandchild), "nor does its child")
    check(fam._thread_guest_id(child) is None, "but the child's own runs keep its own rights")
    check(not has_guest(clean_child), "an owner's family with no guest is clean")
    check(not has_guest(loop_a), "a parent loop ends")
    check(not has_guest(orphan), "a forgotten parent is not a guest")
    check(has_guest(None) is False, "no thread at all has no family")
    interactive = ClaudeBot._ctx.__code__.co_names
    check("_family_has_guest" in interactive, "every context asks about the family")

    with tempfile.TemporaryDirectory() as tmp:
        store = StateStore(Path(tmp) / "state.json", Path(tmp) / "results")
        inst = store.create_instance(InstanceType.QUERY, "hi")
        inst.repo_name, inst.repo_path = "myrepo", "/tmp"
        inst.is_owner_session, inst.vault_eligible = False, False
        inst.user_id, inst.user_name = "2", "guest"
        clone = store.clone_instance_for_rerun(inst, origin_platform="discord", effort="low", model=None)
        check(not clone.is_owner_session and not clone.vault_eligible and clone.user_id == "2", "a retry stays a guest's")
        inst.is_owner_session, inst.vault_eligible = True, True
        clone = store.clone_instance_for_rerun(inst, origin_platform="discord", effort="low", model=None)
        check(clone.vault_eligible, "an owner's retry keeps eligibility")

    root = Path(__file__).resolve().parents[1] / "bot"
    # A guest can arrive after the first turn, so every handoff asks again.
    guarded = "vault_eligible = {} and not ctx.thread_has_guest"
    check((root / "engine/step_spawn.py").read_text().count(guarded.format("source.vault_eligible")) == 2, "chain steps and resolvers re-check for a guest")
    check(guarded.format("inst.vault_eligible") in (root / "engine/instance_cmds.py").read_text(), "/retry re-checks for a guest")
    check(guarded.format("inst.vault_eligible") in (root / "app.py").read_text(), "a cooldown retry re-checks for a guest")
    interactions_src = (root / "discord/interactions.py").read_text()
    check("bot._guest_access(pending.user_id, channel_id)" in interactions_src, "Steer on a guest's prompt keeps the guest's access")
    usage = (root / "discord/usage_gate.py").read_text()
    check('"is_owner": ctx.is_owner' in usage, "the usage queue stores who asked")
    base = (root / "platform/base.py").read_text()
    check("thread_has_guest: bool = True" in base, "a context that does not know assumes a guest")


# --- Persisted files -------------------------------------------------------


async def _gate_send(**_kw):
    return SimpleNamespace(id=5)


async def test_persistence() -> None:
    print("persisted text")
    from bot.store.state import StateStore

    reset_vault()
    bot = FakeBot()
    door = await secret_door.screen_message(bot, FakeMessage(f"run this {OPENAI}"), OWNER)
    clean = door.text
    check(OPENAI not in clean, "the door's text is clean")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "state.json"
        store = StateStore(path, Path(tmp) / "results")
        inst = store.create_instance(InstanceType.QUERY, clean)
        inst.repo_name, inst.repo_path = "myrepo", "/tmp"
        store.set_platform_state("discord", {"pending_refs": {"100": [clean, 0.0]}}, persist=True)
        store.save()
        check(OPENAI not in path.read_text(), "state.json holds only sanitized text")
        files = "\n".join(p.read_text(errors="replace") for p in Path(tmp).rglob("*") if p.is_file())
        check(OPENAI not in files, "nothing else in the data dir holds it")
    # Everything else that queues a prompt is fed `text`, which comes after the door.
    from bot.discord import usage_notifier
    from bot.discord.usage_gate import UsageGateMixin
    with tempfile.TemporaryDirectory() as tmp:
        queue_file = Path(tmp) / "usage_queue.json"
        saved_file, saved_gate = config.USAGE_QUEUE_FILE, config.USAGE_PEAK_GATE_ENABLED
        saved_active, saved_end = usage_notifier.is_usage_limit_active, usage_notifier.next_window_end_utc
        config.USAGE_QUEUE_FILE, config.USAGE_PEAK_GATE_ENABLED = queue_file, True
        usage_notifier.is_usage_limit_active = lambda: True
        usage_notifier.next_window_end_utc = lambda: datetime.now(timezone.utc)
        try:
            gate = UsageGateMixin()
            gate._usage_gate_bypass = {}
            gate._usage_queue_lock = asyncio.Lock()
            gate._forums = FakeForums()
            gate.get_channel = lambda _id: SimpleNamespace(send=_gate_send)
            ctx = SimpleNamespace(channel_id="100", repo_name="myrepo", user_id="1", user_name="o", is_owner=True, pending_image_paths=[])
            check(await gate._offer_usage_limit_choice(ctx, clean), "the usage gate queues the prompt")
        finally:
            config.USAGE_QUEUE_FILE, config.USAGE_PEAK_GATE_ENABLED = saved_file, saved_gate
            usage_notifier.is_usage_limit_active, usage_notifier.next_window_end_utc = saved_active, saved_end
        queued = queue_file.read_text()
        check("[secret " in queued and OPENAI not in queued, "the usage queue file holds the placeholder")
    bot_src = (Path(__file__).resolve().parents[1] / "bot/discord/bot.py").read_text()
    route = bot_src[bot_src.index("async def _route_screened"):]
    check("message.content = door.text" in route[:400], "routing starts from the door's text")
    check(route.index("message.content = door.text") < route.index("message.content.strip()"), "and reads no earlier copy")
    screen_at = bot_src.index("secret_door.screen_message(self, message, msg_access)")
    check(bot_src.index("await self._route_screened(message, msg_access, door)") > screen_at, "the door comes first")
    raw = vault._path().read_text()
    check(OPENAI in raw, "the vault is the one place it lives")


def test_formatter() -> None:
    print("log formatter")
    reset_vault()
    literal = "plainvalue-with-no-known-shape"
    vault.add(literal, "PLAIN", "r")
    fmt = redact.RedactingFormatter("%(message)s")
    record = logging.LogRecord("x", logging.INFO, "f", 1, "saw %s and %s", (literal, OPENAI), None)
    line = fmt.format(record)
    check(literal not in line and OPENAI not in line and "[REDACTED]" in line, "a logged value is scrubbed")
    try:
        raise ValueError(f"boom {GITHUB}")
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, "f", 1, "failed", (), sys.exc_info())
    check(GITHUB not in fmt.format(record), "a traceback is scrubbed")
    record = logging.LogRecord("x", logging.INFO, "f", 1, "token: limit reached", (), None)
    check(fmt.format(record) == "token: limit reached", "ordinary words survive")
    # The one the log actually caught: bot.log cuts a message at 80 characters.
    record = logging.LogRecord("x", logging.INFO, "f", 1, "Discord msg in: %s", (ATLASSIAN[:80],), None)
    check("[REDACTED]" in fmt.format(record) and ATLASSIAN[10:60] not in fmt.format(record), "an Atlassian key cut short in a log is scrubbed")
    record = logging.LogRecord("x", logging.INFO, "f", 1, "saw %s", (ATLASSIAN,), None)
    check("DEADBEEF" not in fmt.format(record), "whole, its tail goes too")


# --- Directive -------------------------------------------------------------


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(channel_id="100", messenger=FakeMessenger(), store=SimpleNamespace())


async def test_directive() -> None:
    print("/secret request")
    reset_vault()
    vault.add(OPENAI, "STRIPE_KEY", "elsewhere")
    vault.add(GITHUB, "OTHER_KEY", "elsewhere")
    vault.add(ANTHROPIC, "THIRD_KEY", "elsewhere")
    vault.add(ENTROPIC, "FOURTH_KEY", "elsewhere")
    vault.add("alreadyhere-value", "HERE_KEY", "myrepo")
    good = '[BOT_CMD: /secret request STRIPE_KEY reason="to charge cards"]'

    def inst(**kw):
        return _instance(vault_eligible=True, **kw)

    ctx = _ctx()
    await _handle_secret_directives(ctx, good, inst())
    check(len(ctx.messenger.sent) == 1, "posts one line")
    _, text, rows = ctx.messenger.sent[0]
    check("STRIPE_KEY" in text and "charge cards" in text and OPENAI not in text, "names and reason, no value")
    check(rows[0][0].callback_data.startswith("secret_allow:") and rows[0][1].callback_data.startswith("secret_deny:"), "Allow and Deny")
    token = rows[0][0].callback_data.split(":", 1)[1]

    ctx2 = _ctx()
    await _handle_secret_directives(ctx2, good, inst())
    check(not ctx2.messenger.sent, "same pending request: deduped")
    check(OPENAI not in json.dumps(vault._safe_read()["requests"]), "the token holds no value")

    ctx3 = _ctx()
    await _handle_secret_directives(ctx3, f"~~~spawn\n{good.replace('STRIPE', 'OTHER')}\n~~~", inst(session_id="b"))
    check(not ctx3.messenger.sent, "inside a tilde body: ignored")
    ctx3 = _ctx()
    await _handle_secret_directives(ctx3, f"For example: `{good.replace('STRIPE', 'OTHER')}`", inst(session_id="b"))
    check(not ctx3.messenger.sent, "a quoted example: ignored")
    ctx3 = _ctx()
    await _handle_secret_directives(ctx3, good.replace("STRIPE", "OTHER"), None)
    check(not ctx3.messenger.sent, "no source instance: ignored")
    ctx3 = _ctx()
    await _handle_secret_directives(ctx3, good.replace("STRIPE", "OTHER"), _instance(is_owner_session=False, vault_eligible=True, session_id="b"))
    check(not ctx3.messenger.sent, "guest session: ignored")
    ctx3 = _ctx()
    await _handle_secret_directives(ctx3, good.replace("STRIPE", "OTHER"), _instance(vault_eligible=False, session_id="b"))
    check(not ctx3.messenger.sent, "ineligible session: ignored")
    for origin in (InstanceOrigin.PROMPT_REVIEW, InstanceOrigin.TLDR):
        ctx3 = _ctx()
        await _handle_secret_directives(ctx3, good.replace("STRIPE", "OTHER"), inst(session_id="b", origin=origin))
        check(not ctx3.messenger.sent, f"{origin.name}: ignored")
    ctx3 = _ctx()
    await _handle_secret_directives(ctx3, "[BOT_CMD: /secret request MISSING_KEY]", inst(session_id="b"))
    check(not ctx3.messenger.sent, "unknown name: ignored")
    ctx3 = _ctx()
    await _handle_secret_directives(ctx3, "[BOT_CMD: /secret forget OTHER_KEY]", inst(session_id="b"))
    check(not ctx3.messenger.sent and vault.has_name("OTHER_KEY"), "any other action: ignored")
    ctx3 = _ctx()
    await _handle_secret_directives(ctx3, "[BOT_CMD: /secret request HERE_KEY]", inst(session_id="b"))
    check(not ctx3.messenger.sent, "already granted: ignored")
    ctx3 = _ctx()
    await _handle_secret_directives(ctx3, f'[BOT_CMD: /secret request OTHER_KEY reason="{ANTHROPIC}"]', inst(session_id="z"))
    check(ctx3.messenger.sent and ANTHROPIC not in ctx3.messenger.sent[0][1], "a value in the reason is removed")

    many = "\n".join(f"[BOT_CMD: /secret request {n}]" for n in ("OTHER_KEY", "STRIPE_KEY", "THIRD_KEY", "FOURTH_KEY"))
    ctx4 = _ctx()
    await _handle_secret_directives(ctx4, many, inst(session_id="c"))
    check(len(ctx4.messenger.sent) == 3, "capped at 3 per response")

    print("Allow and Deny")
    bot = FakeBot()
    edits: list = []

    async def edit(**kw):
        edits.append(kw)

    i = SimpleNamespace(
        edit_original_response=edit, user=SimpleNamespace(id=1, display_name="o"),
        response=FakeResponse(), followup=FakeFollowup(),
    )
    await interactions._handle_secret_request(bot, i, "secret_allow", token)
    check("STRIPE_KEY" in vault.env_for_repo("myrepo") or "STRIPE_KEY" in vault.names_for_repo("myrepo"), "Allow grants")
    check(bot.replays and bot.replays[0][0] == "100" and OPENAI not in bot.replays[0][1], "Allow resumes the thread, names only")
    await interactions._handle_secret_request(bot, i, "secret_allow", token)
    check("gone" in edits[-1]["content"], "a used token is gone")

    t2 = vault.add_request("OTHER_KEY", "fresh", "100", "chained")
    bot = FakeBot()
    bot._store.get_autopilot_chain_meta = lambda s: {"status": "running"} if s == "chained" else None
    await interactions._handle_secret_request(bot, i, "secret_allow", t2)
    check(not bot.replays and "OTHER_KEY" in vault.names_for_repo("fresh"), "a chain owns the thread: grants, no replay")

    t3 = vault.add_request("THIRD_KEY", "fresh", "100", "s")
    bot = FakeBot()
    await interactions._handle_secret_request(bot, i, "secret_deny", t3)
    check(not bot.replays and "THIRD_KEY" not in vault.names_for_repo("fresh") and "Denied" in edits[-1]["content"], "Deny posts one line")

    owner_only = interactions._OWNER_ONLY_ACTIONS
    check(all(a in owner_only for a in interactions._ACTIONS if a.startswith("secret_")), "every secret button is owner only")


async def test_row_buttons() -> None:
    print("report buttons")
    reset_vault()
    vault.add(OPENAI, "A_KEY", "r")
    vault.add(GITHUB, "A_KEY", "r")
    edits: list = []

    async def edit(**kw):
        edits.append(kw)

    msg = SimpleNamespace(content="Stored `A_KEY`.")
    i = SimpleNamespace(edit_original_response=edit, message=msg, response=FakeResponse(), followup=FakeFollowup())
    saved = interactions._message_without_row
    interactions._message_without_row = lambda _m, _c: None
    try:
        await interactions._handle_secret_replace(FakeBot(), i, "A_KEY|A_KEY_2")
        check(vault.env_for_repo("r").get("A_KEY") == GITHUB and not vault.has_name("A_KEY_2"), "Replace moves the value")
        await interactions._handle_secret_forget(FakeBot(), i, "A_KEY")
        check(not vault.has_name("A_KEY") and "Forgot" in edits[-1]["content"], "Forget deletes it")
        await interactions._handle_secret_keep(FakeBot(), i, "B_KEY")
        check("Kept both" in edits[-1]["content"], "Keep both says so")
    finally:
        interactions._message_without_row = saved

    # One row goes, the others stay, with real components.
    view = discord.ui.View(timeout=None)
    for row, name in enumerate(("A", "B")):
        view.add_item(discord.ui.Button(label="Rename", custom_id=f"secret_rename:{name}", row=row))
        view.add_item(discord.ui.Button(label="Forget", custom_id=f"secret_forget:{name}", row=row))
    fake = SimpleNamespace(components=view.to_components())
    saved_from = discord.ui.View.from_message
    discord.ui.View.from_message = staticmethod(lambda _m, timeout=None: view)
    try:
        left = modals._message_without_row(fake, "secret_forget:A")
        check(left and {c.custom_id for c in left.children} == {"secret_rename:B", "secret_forget:B"}, "only that row goes")
    finally:
        discord.ui.View.from_message = saved_from

    i = SimpleNamespace(
        response=FakeResponse(), user=SimpleNamespace(id=1), message=None,
    )
    reset_vault()
    vault.add(OPENAI, "B_KEY", "r")
    m = modals.SecretRenameModal(FakeBot(), "B_KEY", "secret_rename:B_KEY")
    m.new_name._value = "STRIPE_KEY"
    await m.on_submit(i)
    check(vault.has_name("STRIPE_KEY") and "Renamed" in i.response.messages[0], "rename modal works")
    m = modals.SecretRenameModal(FakeBot(), "STRIPE_KEY", "secret_rename:STRIPE_KEY")
    m.new_name._value = OPENAI
    i.response = FakeResponse()
    await m.on_submit(i)
    check(vault.has_name("STRIPE_KEY") and "value rather than a name" in i.response.messages[0], "a key as a new name is refused")


async def test_commands() -> None:
    print("/secret commands")
    from bot.discord import slash_commands

    client = discord.Client(intents=discord.Intents.none())
    tree = SecretDoorTree(client)
    bot = SimpleNamespace(
        tree=tree, _guild_id=1, _is_owner=lambda uid: uid == 1,
        _check_access=lambda *_a, **_k: GUEST,
        _forums=FakeForums(), _store=SimpleNamespace(list_repos=lambda: {"myrepo": "/x"}),
        _run_slash=None,
    )
    slash_commands.setup(bot)
    group = tree.get_command("secret", guild=discord.Object(id=1))
    check(group is not None, "/secret is registered")
    names = {c.name for c in group.commands}
    check(names == {"list", "add", "forget", "rename", "grant"}, "all five subcommands")
    cmd = {c.name: c for c in group.commands}

    def inter(user_id=1):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id), channel_id=100, channel=FakeThread(),
            response=FakeResponse(), followup=FakeFollowup(),
        )

    reset_vault()
    vault.add(OPENAI, "A_KEY", "myrepo")
    for name, args in (("list", ()), ("add", ()), ("forget", ("A_KEY",)), ("rename", ("A_KEY", "B_KEY")), ("grant", ("A_KEY",))):
        i = inter(user_id=2)
        await cmd[name].callback(i, *args)
        check(i.response.messages == ["Unauthorized"], f"/secret {name}: guests refused")
    check(vault.has_name("A_KEY"), "a refused command changed nothing")

    i = inter()
    await cmd["list"].callback(i)
    check("A_KEY" in i.followup.sent[0] and OPENAI[-4:] in i.followup.sent[0], "list shows name and last 4")
    check(OPENAI not in i.followup.sent[0], "list never shows the value")

    i = inter()
    await cmd["add"].callback(i)
    check(isinstance(i.response.modal, modals.SecretAddModal), "add opens a form")

    i = inter()
    await cmd["rename"].callback(i, "A_KEY", "B_KEY")
    check(vault.has_name("B_KEY"), "rename works")

    i = inter()
    await cmd["grant"].callback(i, "B_KEY", "myrepo")
    check("myrepo" in vault.list_entries()[0]["repos"], "grant works")
    i = inter()
    await cmd["grant"].callback(i, "B_KEY", "not-a-repo")
    check("registered repo" in i.response.messages[0], "grant refuses an unknown repo")

    i = inter()
    await cmd["forget"].callback(i, "B_KEY")
    check(not vault.has_name("B_KEY"), "forget works")


# --- Transcript scan -------------------------------------------------------


def test_scan() -> None:
    print("transcript scan")
    reset_vault()
    value = 'plain"value/with+odd=chars?' + "x" * 8
    vault.add(value, "ODD_KEY", "r")
    vault.add(OPENAI, "SAFE_KEY", "r")
    forms = {
        "literal": value.encode(),
        "json": json.dumps(value)[1:-1].encode(),
        "b64": base64.b64encode(value.encode()),
        "urlsafe": base64.urlsafe_b64encode(value.encode()),
        "unpadded": base64.b64encode(value.encode()).rstrip(b"="),
    }
    check(all(n in secret_scan.needles_for(value) for n in forms.values()), "every encoding is a needle")
    with tempfile.TemporaryDirectory() as tmp:
        session = Path(tmp) / "abc.jsonl"
        saved = sessions.find_session_file
        sessions.find_session_file = lambda sid: session if sid.startswith("s") else None
        try:
            for label, needle in forms.items():
                reset_vault()
                vault.add(value, "ODD_KEY", "r")
                secret_scan._offsets.clear()
                session.write_bytes(b'{"x":"' + needle + b'"}\n')
                check(secret_scan.scan_session(f"s-{label}") == ["ODD_KEY"], f"finds it: {label}")

            reset_vault()
            vault.add(value, "ODD_KEY", "r")
            secret_scan._offsets.clear()
            session.write_bytes(b"clean\n")
            check(secret_scan.scan_session("s1") == [], "a clean transcript flags nothing")
            size = session.stat().st_size
            check(secret_scan._offsets[str(session)] == size, "offset advances")
            with session.open("ab") as f:
                f.write(value.encode())
            check(secret_scan.scan_session("s1") == ["ODD_KEY"], "picks up what was appended")
            check(secret_scan.scan_session("s1") == [], "flags once per session and name")
            secret_scan._offsets.clear()
            check(secret_scan.scan_session("s1") == [], "stays flagged after a restart")
            check(secret_scan.scan_session("s2") == ["ODD_KEY"], "another session flags again")

            # A value split across the read boundary is still found.
            reset_vault()
            vault.add(value, "ODD_KEY", "r")
            secret_scan._offsets.clear()
            half = len(value) // 2
            session.write_bytes(b"a" * (secret_scan._CHUNK_BYTES - half) + value.encode() + b"z")
            check(secret_scan.scan_session("s3") == ["ODD_KEY"], "chunk boundary is covered")

            reset_vault()
            vault.add(value, "ODD_KEY", "r")
            secret_scan._offsets.clear()
            session.write_bytes(b"clean")
            sub = session.with_suffix("") / "subagents"
            sub.mkdir(parents=True)
            (sub / "agent-1.jsonl").write_bytes(value.encode())
            check(secret_scan.scan_session("s4") == ["ODD_KEY"], "subagent transcripts are read")

            reset_vault()
            vault.add(value, "ODD_KEY", "r")
            secret_scan._offsets.clear()
            shutil.rmtree(session.with_suffix(""))
            saved_cap = secret_scan.SCAN_CAP_BYTES
            secret_scan.SCAN_CAP_BYTES = 100
            session.write_bytes(b"a" * 200 + value.encode())
            try:
                check(secret_scan.scan_session("s5") == [], "the cap stops a pass")
                check(secret_scan._offsets[str(session)] <= 100, "and the offset stays where it stopped")
                while secret_scan._offsets[str(session)] < session.stat().st_size:
                    found = secret_scan.scan_session("s5")
                    if found:
                        break
                check(found == ["ODD_KEY"], "later passes finish the file")
            finally:
                secret_scan.SCAN_CAP_BYTES = saved_cap
        finally:
            sessions.find_session_file = saved
    text = secret_scan.flag_text(["ODD_KEY"])
    check("ODD_KEY" in text and "transcript" in text and "Rotate" in text, "the flag names it and says rotate")
    reset_vault()
    check(secret_scan.scan_session("s1") == [], "empty vault is a no-op")


# --- What the fresh-eyes review found --------------------------------------


def _log_with_formatter(*lines: str) -> str | None:
    """Log through the redacting formatter. None means the call never came back."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(redact.RedactingFormatter("%(message)s"))
    logger = vault.log
    saved = logger.propagate
    logger.propagate = False
    logger.addHandler(handler)

    def work():
        for line in lines:
            logger.error(line)
        vault.has_name("ANY")  # logs from under the vault's own lock when the file is bad

    worker = threading.Thread(target=work, daemon=True)
    try:
        worker.start()
        worker.join(5)
        return None if worker.is_alive() else stream.getvalue()
    finally:
        logger.removeHandler(handler)
        logger.propagate = saved


def _test_broken_vault_file() -> None:
    print("a broken vault file")
    literal = "plainliteralvalue12345"
    reset_vault()
    vault.add(literal, "LIT_KEY", "r")
    check(literal not in redact.scrub_known(f"x {literal}"), "a stored value is stripped")
    vault._path().write_text("{not json " + literal)
    out = _log_with_formatter(f"line {literal}")
    check(out is not None, "a corrupt file does not hang the first log line")
    check(out is not None and "line" in out and literal not in out, "the last good values are still stripped")
    other = "anotherliteralvalue99"
    vault._path().write_text(json.dumps({"entries": [{"name": "O_KEY", "value": other, "repos": []}]}))
    check(other not in redact.scrub_known(f"x {other}"), "a repaired or hand-edited file counts at once")
    check(literal in redact.scrub_known(f"x {literal}"), "and what it no longer holds stops counting")
    for label, bad in (
        ("not an object", "[]"),
        ("entries not a list", '{"entries": {"a": 1}}'),
        ("an entry with no name", '{"entries": [{"value": "x"}]}'),
        ("requests not an object", '{"requests": []}'),
        ("hold markers not a list", '{"hold_markers": {}}'),
    ):
        vault._path().write_text(bad)
        try:
            vault.add(OPENAI, "X_KEY", "r")
            check(False, f"valid JSON, {label}: refused")
        except vault.VaultUnavailable as exc:
            check(bad not in str(exc), f"valid JSON, {label}: refused")
        check(vault._path().read_text() == bad, f"valid JSON, {label}: left untouched")


def _test_door_state_bounds() -> None:
    print("door state stays small")
    reset_vault()
    tokens = [vault.add_request(f"K{i}_KEY", "r", "1", f"s{i}") for i in range(vault._REQUESTS_MAX + 5)]
    requests = vault._safe_read()["requests"]
    check(len(requests) == vault._REQUESTS_MAX, "requests are bounded")
    check(tokens[-1] in requests and tokens[0] not in requests, "the oldest request goes first")
    for i in range(vault._FLAGGED_SESSIONS_MAX + 5):
        vault.flag_once(f"session{i}", "A_KEY")
    flagged = vault._safe_read()["flagged"]
    check(len(flagged) == vault._FLAGGED_SESSIONS_MAX and "session0" not in flagged, "flagged sessions are bounded")


def _test_cut_values() -> None:
    print("a value is read whole")
    b64 = "Ab3dE5gH9jK+/mN0pQ/rS2tU4vW6xY8zA1cE3gI5"
    hex40 = "3f9a1c7e5b2d8046" + "c1e7a94f02b6d358" + "7d10e6f4"
    uuid = "7f3a9c1e-5b2d-4e8f-a046-c1e7a94f02b6"

    def values(text, **kw):
        return [f.value for f in secret_detect.scan(text, **kw)]

    check(values(f"API_KEY={b64}") == [b64], "base64 with slashes under an env label")
    check(values(f"API_KEY=/{b64}") == [f"/{b64}"], "and one that starts with a slash")
    check(values("password: lo9ngpa$$/w1/rd") == ["lo9ngpa$$/w1/rd"], "a password with slashes")
    dotted = f"prefix1.{hex40}.tail9"
    check(values(f"API_KEY={dotted}") == [dotted], "a value holding an id shape")
    check(values(f"Authorization: Bearer {b64}") == [b64], "a bearer token with slashes")
    check(values(f"glued {b64}") == [b64], "an unlabelled token the path mask would have cut")
    check(values(f"FOO={ENTROPIC}") == [ENTROPIC], "a label is not part of the value")
    tiers = lambda t, **kw: [f.tier for f in secret_detect.scan(t, **kw)]  # noqa: E731
    check(SURE in tiers(ATLASSIAN), "an Atlassian key alone is caught for sure")
    check(values(ATLASSIAN) == [ATLASSIAN], "whole, tail included")
    check(secret_detect.scan(ATLASSIAN)[0].suggested_name == "ATLASSIAN_API_TOKEN", "and named")
    check(not tiers("ATATT is a word in some note"), "a bare prefix is not a key")
    check(tiers(UNKNOWN_VENDOR) == [ASK], "a key of the same shape from anyone else asks")
    check(values(UNKNOWN_VENDOR) == [UNKNOWN_VENDOR], "and is not cut at its =")
    labelled = secret_detect.scan(f"MY_API_KEY={UNKNOWN_VENDOR}")
    check(values(f"MY_API_KEY={UNKNOWN_VENDOR}") == [UNKNOWN_VENDOR], "a label still comes off a value holding an =")
    check(labelled and labelled[0].suggested_name == "MY_API_KEY", "and still names it")
    check(values(f"--set=key={ENTROPIC}") == [ENTROPIC], "two labels both come off")
    check(values(f"FOO={ENTROPIC}==") == [f"{ENTROPIC}=="], "a label and padding together")
    check(values(f"abc123xyz789={ENTROPIC}") == [ENTROPIC], "a short odd name is still a label")
    check(not tiers("timeout=30 retries=5 mode=fast"), "ordinary settings pass")
    check(not tiers("name=some-ordinary-value-here"), "an ordinary name=value passes")
    check(values(f"--token={ENTROPIC}") == [ENTROPIC], "nor is a flag")
    check(values(f"try {ENTROPIC}==") == [f"{ENTROPIC}=="], "base64 padding is kept")
    check(values(f"try {ENTROPIC}.") == [ENTROPIC], "a full stop is not")
    check(values(f"RUNPOD_API_KEY={uuid}") == [uuid], "a UUID under an env label")
    check(values(f"my runpod key is {uuid}") == [uuid], "a UUID named as a key")
    check(values(f"API_KEY={hex40}") == [hex40], "40 hex under a label")
    for text in (
        f"session {uuid}", f"primary key {uuid}", f"session key {uuid}",
        f"cache key sha256:{hex40}", "API_BASE_KEY=https://example.com/v1/models",
        "TOKEN_PATH=/etc/app/tokens.txt", "password: /etc/app/pw.txt", f"commit {hex40}",
    ):
        check(not values(text), f"left alone: {text[:30]}")


async def test_review_fixes() -> None:
    _test_broken_vault_file()
    _test_door_state_bounds()
    _test_cut_values()

    print("a file name")
    reset_vault()
    jwt = "eyJ" + "a1B2c3D4e5F6" + "." + "g7H8i9J0k1L2" + "." + "m3N4o5P6q7R8"
    named = FakeAtt(20, jwt, b"hello")
    await secret_door.screen_message(FakeBot(), FakeMessage("x", attachments=[named]), OWNER)
    check("m3N4o5P6q7R8" not in named.filename, "no part of a JWT comes back as an extension")
    reset_vault()
    named = FakeAtt(21, f"key.{ANTHROPIC}", b"hello")
    await secret_door.screen_message(FakeBot(), FakeMessage("x", attachments=[named]), OWNER)
    check(ANTHROPIC not in named.filename, "a key after a dot is found")

    print("posting a hold")
    reset_vault()
    bot = FakeBot()

    async def no_post(*_a, **_kw):
        raise RuntimeError("discord down")
    bot.messenger.send_text = no_post
    msg = FakeMessage(f"try {ENTROPIC} now")
    door = await secret_door.screen_message(bot, msg, OWNER)
    check(not door.proceed and not secret_door._holds and not secret_door.is_held(msg.id), "a failed post leaves no hold behind")

    reset_vault()
    bot = FakeBot()

    async def tapped_at_once(channel_id, text, buttons=None, silent=False):
        await secret_door.resolve_hold(bot, next(iter(secret_door._holds)), "no")
        return "m1"
    bot.messenger.send_text = tapped_at_once
    await secret_door.screen_message(bot, FakeMessage(f"try {ENTROPIC} now"), OWNER)
    check(not vault._safe_read()["hold_markers"], "a hold answered during its post writes no marker")
    check(not [t for t in secret_door._background if t.get_coro().__name__.startswith("_expire")], "and starts no timer")

    print("a value already called not a secret")
    reset_vault()
    bot = FakeBot()
    await secret_door.screen_message(bot, FakeMessage(f"try {ENTROPIC} now"), OWNER)
    await secret_door.resolve_hold(bot, next(iter(secret_door._holds)), "no")
    await settle_tasks()
    opts = [{"type": 3, "name": "p", "value": f"try {ENTROPIC}"}]
    check(await secret_door.screen_slash(bot, _slash(opts)) and ENTROPIC in opts[0]["value"], "a slash command does not refuse it again")

    print(".env with a value across lines")
    reset_vault()
    bot = FakeBot()
    wrapped = PEM.replace("-----\n", "-----\n" + "QUJDREVGR0hJSktM=\n", 1)
    body = f'SIGNING="{wrapped}"\nZ_TOKEN={OPENAI}\nPORT=8080\n'.encode()
    door = await secret_door.screen_message(bot, FakeMessage("", attachments=[FakeAtt(22, ".env", body)]), OWNER)
    names = {e["name"] for e in vault.list_entries()}
    check("SIGNING" not in names and "Z_TOKEN" in names, "a quoted PEM is burned, the next line still stored")
    check("SIGNING" in door.text and "MIIE" not in door.text and "QUJD" not in door.text, "its lines never read as names")
    check("PRIVATE" not in json.dumps(vault._safe_read()), "not even its header is stored")

    print("a /secret request nobody saw")
    reset_vault()
    vault.add(OPENAI, "STRIPE_KEY", "elsewhere")
    ctx = _ctx()

    async def unsent(*_a, **_kw):
        raise RuntimeError("discord down")
    ctx.messenger.send_text = unsent
    good = "[BOT_CMD: /secret request STRIPE_KEY]"
    await _handle_secret_directives(ctx, good, _instance(vault_eligible=True))
    check(not vault._safe_read()["requests"], "a failed post leaves no pending request")
    ctx = _ctx()
    await _handle_secret_directives(ctx, good, _instance(vault_eligible=True))
    check(len(ctx.messenger.sent) == 1, "so the session can ask again")

    print("a broken vault against a gone request")
    reset_vault()
    edits: list = []

    async def edit(**kw):
        edits.append(kw)
    settled: list = []

    async def fake_settle(_i, text):
        settled.append(text)
    saved = interactions.settle
    interactions.settle = fake_settle
    try:
        i = SimpleNamespace(edit_original_response=edit)
        await interactions._handle_secret_request(FakeBot(), i, "secret_allow", "nosuchtoken")
        check("gone" in edits[-1]["content"] and not settled, "a gone request says so")
        edits.clear()
        vault.add(OPENAI, "GONE_KEY", "elsewhere")
        token = vault.add_request("GONE_KEY", "myrepo", "1", "s")
        vault.forget("GONE_KEY")
        await interactions._handle_secret_request(FakeBot(), i, "secret_allow", token)
        check(edits and "no longer" in edits[-1]["content"] and not settled, "a forgotten key says so")
        edits.clear()
        vault._path().write_text("{nope")
        await interactions._handle_secret_request(FakeBot(), i, "secret_allow", "anytoken")
        check(settled and "can't be read" in settled[0] and not edits, "a broken file says that, once")
    finally:
        interactions.settle = saved

    print("output redaction")
    reset_vault()
    check(ANTHROPIC not in redact.scrub_known(f"x-{ANTHROPIC}"), "a key glued to a word is still redacted in a log")
    check(GITHUB not in redact.redact_secrets(f"prefix{GITHUB}"), "and on screen")
    long_sk = "sk-" + "a1B2c3D4" * 3
    check(long_sk not in redact.redact_secrets(f"mask{long_sk}"), "a glued alphanumeric sk- key is redacted")
    slug = "task-fix-the-login-bug-right-now-please"
    check(slug in redact.scrub_known(slug) and slug in redact.redact_secrets(slug), "a hyphenated slug is left alone")
    check(vault.REMOVED is secret_detect.REMOVED, "one removed marker")


async def main() -> None:
    tests = (
        test_detector, test_vault, test_owner_sure, test_holds, test_burn_and_guest,
        test_title, test_attachments, test_voice, test_slash, test_modals, test_routing,
        test_edits, test_priming, test_block_and_flags, test_env_injection,
        test_guest_routes, test_persistence, test_formatter, test_directive,
        test_row_buttons, test_commands, test_scan, test_review_fixes,
    )
    for test in tests:
        # One crash should not hide what the later groups would have found.
        try:
            result = test()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            FAILS.append(f"{test.__name__} crashed: {exc!r}")
            traceback.print_exc()
        for task in list(secret_door._background):
            task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    if FAILS:
        print(f"\n{len(FAILS)} FAILED:")
        for label in FAILS:
            print(f"  - {label}")
        sys.exit(1)
    print("\nAll secret intake checks passed.")
