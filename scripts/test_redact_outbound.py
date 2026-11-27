"""Regression test for the outbound secret filter (bot/platform/redact.py).

Pins the rules in docs/design/secret-intake.md, "Outbound filter": known keys
by value first (vault, bot env, key files), shapes second, public ids left
alone where the text says they are public, markers that survive a second pass,
the Discord exit point (bot/discord/adapter.py) filtering every text and text
upload, and the note the session gets on its next turn.

Every value here is FAKE, built in this file. The key files are temp dirs.

Run: python scripts/test_redact_outbound.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config, keyfiles, secret_detect, vault  # noqa: E402

_TMP = Path(tempfile.mkdtemp(prefix="redact_outbound_"))
config.VAULT_DIR = _TMP / "vault"
keyfiles.CONFIG_DIR = _TMP / "config"
keyfiles.RECHECK_SECONDS = 0.0

from bot.platform import redact  # noqa: E402
from bot.platform.redact import redact_report, redact_secrets  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str, got: object = None) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)
        if got is not None:
            print(f"        got: {got!r}")


# Fake values, assembled here so no scanner reads this file as holding keys.
HEX = ("7d" * 4 + "e2" * 4 + "19" * 4 + "c4" * 4) * 2          # 64 hex
HEX_B = ("a3" * 4 + "5f" * 4 + "0b" * 4 + "d8" * 4) * 2        # 64 hex, the key-file key
HEX_POOL = ("4e" * 4 + "91" * 4 + "c7" * 4 + "2a" * 4) * 2     # 64 hex, a public pool id
ADDR40 = "0x" + "3f9a" * 10
API_VAL = "kfz" + "Q7mK2pL9vR4tY8wN3bF6"                     # no vendor shape
CFG_VAL = "cfgv" + "Zx1Cv5Bn7Qw3rTy9"
WORDS = sorted(secret_detect.bip39_words())
SEED12 = " ".join(WORDS[300:312])


def hidden(text: str, value: str, **kw) -> bool:
    out = redact_secrets(text, **kw)
    return value not in out and value.lower() not in out.lower()


# --- Shapes: hidden ---------------------------------------------------------------------
print("Hidden")
check(hidden(f"key 0x{HEX} here", HEX), "a 0x key in plain text")
check(hidden(f"key {HEX} here", HEX), "a bare 64-hex in plain text")
check(hidden(f"see 0x{HEX.upper()}", HEX), "upper case")
check(hidden(f"https://example.com/wallet/0x{HEX}", HEX), "in a non-allowlisted URL path")
check(hidden(f"https://etherscan.io/tx/x?key=0x{HEX}", HEX), "in an explorer URL's query string")
check(hidden(f"https://dexscreener.com/x#0x{HEX}", HEX), "in an explorer URL's fragment")
check(hidden(f"https://user:pw@etherscan.io/tx/0x{HEX}", HEX), "explorer link with userinfo is not trusted")
out = redact_secrets(f"PRIVATE_KEY=0x{HEX}")
check(out == "PRIVATE_KEY=[hidden:private-key]", "PRIVATE_KEY=0x...", out)
out = redact_secrets("password: 123456")
check(out == "password: [hidden:password]", "password: 123456", out)
check(hidden(f"private key hash: 0x{HEX}", HEX), "private key hash: 0x... (label says key)")
check(hidden(f"pk tx: 0x{HEX}", HEX), "a pk label beats a tx label")
check(hidden(f"seed block: {HEX}", HEX), "a seed label beats a block label")
out = redact_secrets(f"backup: {SEED12} done")
check(SEED12 not in out and "[hidden:seed-phrase]" in out, "an unlabelled 12-word phrase", out)
_seed_words = SEED12.split()
SEED_ONE_REPEAT = " ".join(_seed_words[:11] + _seed_words[:1])
out = redact_secrets(f"backup: {SEED_ONE_REPEAT} done")
check("[hidden:seed-phrase]" in out, "a 12-word phrase with one repeat still hides", out)
out = redact_secrets(f"{' '.join(['word'] * 30)} {SEED12}")
check(SEED12 not in out, "a phrase right after filler words still hides", out)
filler = "\n".join(" ".join(["word"] * 12) for _ in range(5))
check(redact_secrets(filler) == filler, "filler of one repeated word shows")
yes_no = " ".join(["yes", "no"] * 10)
check(redact_secrets(yes_no) == yes_no, "a run of two alternating words shows")
out = redact_secrets(f"mnemonic: {SEED12}")
check(out == "mnemonic: [hidden:seed-phrase]", "a labelled phrase", out)
out = redact_secrets("Authorization: Bearer " + "abcDEF123456ghiJKL7890")
check(out.endswith("Bearer [hidden:token]"), "Bearer", out)
out = redact_secrets("postgres://app:hunter2@db:5432/x")
check(out == "postgres://app:[hidden:url-credential]@db:5432/x", "URL password", out)
out = redact_secrets("say `password: hunter22` now")
check(out == "say `password: [hidden:password]` now", "a backticked value keeps its backticks", out)
out = redact_secrets("auth_token: 4096")
check("4096" not in out, "a singular token name with a number stays hidden", out)
out = redact_secrets("api_key=12345678")
check("12345678" not in out, "a number under a key name stays hidden", out)
out = redact_secrets("PRIVATE_KEY_SET=false")
check("false" not in out, "a private name hides even a flag-shaped value", out)

# --- Shapes: visible --------------------------------------------------------------------
print("Visible")
for text, label in (
    (f"https://dexscreener.com/elysium/0x{HEX_POOL}", "a dexscreener v4 pool link"),
    (f"[pool](https://dexscreener.com/elysium/0x{HEX_POOL})", "the same link in markdown"),
    (f"<https://dexscreener.com/elysium/0x{HEX_POOL}>", "the same link in angle brackets"),
    (f"https://etherscan.io/tx/0x{HEX_POOL}", "an etherscan tx link"),
    (f"https://sepolia.etherscan.io/tx/0x{HEX_POOL}", "an etherscan subdomain"),
    (f"https://eth.blockscout.com/tx/0x{HEX_POOL}", "a blockscout subdomain"),
    (f"https://app.hyperliquid.xyz/explorer/tx/0x{HEX_POOL}", "hyperliquid explorer"),
    (f"Buy tx: `0x{HEX_POOL}`", "Buy tx: `0x<64>`"),
    (f"**Tx hash:** `0x{HEX_POOL}`", "bold tx hash label"),
    (f"pool id 0x{HEX_POOL}", "pool id label"),
    (f"block {HEX_POOL}", "block label, bare hex"),
    (f"topic0: 0x{HEX_POOL}", "topic label with a digit"),
    ("HasToken=False", "HasToken=False"),
    ("has_token: none", "a null flag"),
    ("`HasToken=False`-style", "a flag in backticks"),
    ("input_tokens: 1,234 and max_tokens=4096", "token counts"),
    (f"router {ADDR40}", "a 40-hex address"),
    (f'"tokenAddress": "{ADDR40}"', "a camelCase token address in JSON"),
    (f"token_address={ADDR40}", "a snake_case token address"),
    (f"token_contract: {ADDR40}", "an address under any token name"),
    ("token_id: 4815162342", "a token id"),
):
    out = redact_secrets(text)
    check(out == text, label, out)
check(redact_secrets(f"https://app.hyperliquid.xyz/trade/0x{HEX_POOL}") !=
      f"https://app.hyperliquid.xyz/trade/0x{HEX_POOL}",
      "hyperliquid outside /explorer stays filtered")

# --- Known keys from key files ----------------------------------------------------------
print("Key files")
repo = _TMP / "fakerepo"
(repo / ".worktrees" / "t-1").mkdir(parents=True)
(repo / ".env").write_text(
    f"export ARC_BURNER_PK=0x{HEX_B}\n"
    f"FAKE_API_KEY=\"{API_VAL}\"\n"
    f"POOL_ID=0x{HEX_POOL}\n"
    "TOKEN_ADDRESS=0x" + "3f9a" * 10 + "\n"
    "SOME_TOKEN=changeme\n"
    "# COMMENTED_SECRET=nope12345\n",
    encoding="utf-8",
)
(repo / ".env.example").write_text("EXAMPLE_SECRET=" + "e" * 4 + "x7" * 8 + "\n", encoding="utf-8")
(repo / ".worktrees" / "t-1" / ".env").write_text("WT_SECRET=" + "w0rk" * 4 + "\n", encoding="utf-8")
cfg = keyfiles.CONFIG_DIR / "someapp"
cfg.mkdir(parents=True)
(cfg / "app.env").write_text(f"SOMEAPP_SECRET='{CFG_VAL}'\n", encoding="utf-8")
keyfiles.set_repo_roots({"fakerepo": str(repo)})
keyfiles.reset()
origins = {o for o, _ in keyfiles.entries()}
check("ARC_BURNER_PK@fakerepo/.env" in origins, "ARC_BURNER_PK counts (PK segment)", origins)
check("FAKE_API_KEY@fakerepo/.env" in origins, "quoted value read")
check("SOMEAPP_SECRET@~/.config/someapp/app.env" in origins, "a ~/.config key file read")
check(not any(o.startswith("POOL_ID") for o in origins), "POOL_ID is not loaded")
check(not any(o.startswith("TOKEN_ADDRESS") for o in origins), "TOKEN_ADDRESS is not loaded")
check(not any(o.startswith("SOME_TOKEN") for o in origins), "a placeholder is not loaded")
check(not any(o.startswith(("EXAMPLE_SECRET", "WT_SECRET", "COMMENTED")) for o in origins),
      "templates, worktrees and comments are not read")
check(keyfiles.name_is_key("DISCORD_WEBHOOK_URL"), "a webhook URL name is a key")
for name in ("TEST_WEBHOOK_USER_ID", "TEST_WEBHOOK_IDS", "VAPID_PUBLIC_KEY", "WALLET_PUBKEY",
             "TOKEN_URL", "SIGNER_KEY_ADDRESSES", "apiKeyHost"):
    check(not keyfiles.name_is_key(name), f"{name} is not a key")
for name in ("ARC_BURNER_PK", "X_ACCESS_SECRET", "PLEX_TOKEN", "DELUGE_PASS", "apiKey"):
    check(keyfiles.name_is_key(name), f"{name} is a key")
out = redact_secrets(f"private_key_address={ADDR40}")
check(ADDR40 not in out, "a private-key name hides even an address-shaped value", out)

out = redact_secrets(f"https://dexscreener.com/elysium/0x{HEX_B}")
check(HEX_B not in out.lower(), "a real key in an explorer link is still hidden", out)
check(hidden(f"raw {HEX_B.upper()} x", HEX_B), "key-file hex, upper case, no 0x")
out = redact_secrets(f"value {API_VAL}.", detail=True)
check(out == "value [hidden:FAKE_API_KEY@fakerepo/.env].", "owner detail names the key and file", out)
out = redact_secrets(f"value {API_VAL}.")
check(out == "value [hidden].", "without detail a known key is plain [hidden]", out)
out = redact_secrets(f"x {CFG_VAL}", detail=True)
check("[hidden:SOMEAPP_SECRET@~/.config/someapp/app.env]" in out, "config key named", out)
out = redact_secrets(f"FAKE_API_KEY={API_VAL}", detail=True)
check(out == "FAKE_API_KEY=[hidden:FAKE_API_KEY@fakerepo/.env]", "KV does not re-hide a marker", out)
check(redact.scrub_known(f"log {API_VAL}") == "log [REDACTED]", "logs keep [REDACTED] and see key files")
check(redact.scrub_known(f"log {HEX_B.upper()}") == "log [REDACTED]", "logs match a hex key in any case")

# A file that becomes unreadable keeps its last good values.
keyfiles.reset()
keyfiles.entries()
(repo / ".env").chmod(0)
try:
    os.utime(repo / ".env", None)
    still = {o for o, _ in keyfiles.entries()}
finally:
    (repo / ".env").chmod(0o600)
check(os.geteuid() == 0 or "FAKE_API_KEY@fakerepo/.env" in still, "an unreadable file keeps its last good list")

# --- Vault names ------------------------------------------------------------------------
print("Vault")
VAULT_VAL = "vlt" + "Hj4Kl8Mn2Pq6Rs0T"
vault.add(VAULT_VAL, "MY_VAULT_TOKEN")
vault._literal_cache = None
out = redact_secrets(f"t={VAULT_VAL}", detail=True)
check(out == "t=[hidden:MY_VAULT_TOKEN]", "a vault key is named in owner detail", out)
check(dict(vault.literal_named()).get("MY_VAULT_TOKEN") == VAULT_VAL, "literal_named carries the name")
check(VAULT_VAL in vault.literal_values(), "literal_values still lists the value")

# --- Markers are stable -----------------------------------------------------------------
print("Idempotence")
for s in ("password=[hidden:password]", "DISCORD_BOT_TOKEN=[hidden:DISCORD_BOT_TOKEN]",
          "token: [hidden]", "mnemonic: [hidden:seed-phrase]", "secret=[REDACTED]",
          "postgres://a:[hidden:url-credential]@h/x", "Bearer [hidden:token]"):
    once = redact_secrets(s)
    check(once == s and redact_secrets(once) == s, f"unchanged: {s}", once)
samples = [
    f"PRIVATE_KEY=0x{HEX}", "password: 123456", f"key {HEX}", f"mnemonic: {SEED12}",
    f"backup {SEED12}", f"value {API_VAL}", f"FAKE_API_KEY={API_VAL}", f"Buy tx: 0x{HEX_POOL}",
    "postgres://app:hunter2@db/x", f"https://example.com/0x{HEX}?a=1",
]
for s in samples:
    for detail in (False, True):
        once = redact_secrets(s, detail=detail)
        check(redact_secrets(once, detail=detail) == once and redact_secrets(once) == once,
              f"second pass is a no-op (detail={detail}): {s[:24]}", once)
_t, hits = redact_report(f"a 0x{HEX} b password=hunter22 c {API_VAL}")
check(sorted(hits) == sorted(["private-key", "password", "known-key"]), "report lists what was hidden", hits)
check(redact.summarize_hits(["private-key", "private-key", "token"]) == "private-key x2, token",
      "summary counts kinds")

# --- The Discord exit point -------------------------------------------------------------
print("Adapter")
import discord  # noqa: E402

from bot.discord.adapter import DiscordMessenger  # noqa: E402
from bot.platform.base import CardSpec, MessageHandle  # noqa: E402


class FakeMsg:
    def __init__(self, ch, **kw):
        self.id = len(ch.sent) + 1000
        self.kw = kw
        self.embeds = [kw["embed"]] if kw.get("embed") else []
        self.edits: list[dict] = []

    async def edit(self, **kw):
        self.edits.append(kw)


class FakeChannel:
    def __init__(self):
        self.sent: list[FakeMsg] = []

    async def send(self, content=None, **kw):
        kw["content"] = content
        files = []
        if kw.get("file") is not None:
            files.append(kw["file"])
        files.extend(kw.get("files") or [])
        kw["_bytes"] = [f.fp.read() for f in files]
        m = FakeMsg(self, **kw)
        self.sent.append(m)
        return m

    async def fetch_message(self, mid):
        return self.sent[-1]


def all_text(msg: FakeMsg) -> str:
    parts = [str(msg.kw.get("content") or "")]
    embeds = list(msg.embeds) + [e["embed"] for e in msg.edits if e.get("embed")]
    for e in embeds:
        parts += [e.title or "", e.description or "", (e.footer.text if e.footer else "") or ""]
        parts += [f.name + " " + f.value for f in e.fields]
    parts += [str(e.get("content") or "") for e in msg.edits]
    return "\n".join(parts)


ch = FakeChannel()
m = DiscordMessenger.__new__(DiscordMessenger)


async def _resolve(_cid):
    return ch


m._resolve_channel = _resolve  # type: ignore[method-assign]
m._shown = lambda cid, b: None  # type: ignore[method-assign]
SECRET_LINE = f"PRIVATE_KEY=0x{HEX}"


async def adapter_checks() -> None:
    h = await m.send_thinking("c", "tool: " + SECRET_LINE)
    check(HEX not in all_text(ch.sent[-1]), "send_thinking redacts")
    await m.edit_thinking(h, "detail " + SECRET_LINE, footer="f " + HEX)
    check(HEX not in all_text(ch.sent[-1]), "edit_thinking redacts description and footer")
    await m.send_text("c", "text " + SECRET_LINE)
    check(HEX not in all_text(ch.sent[-1]), "send_text redacts")
    await m.edit_text("c", "1", "edited " + SECRET_LINE)
    check(HEX not in all_text(ch.sent[-1]), "edit_text redacts")
    await m.send_result("c", "result " + SECRET_LINE, metadata={"Branch": HEX, "_session_loc": HEX})
    check(HEX not in all_text(ch.sent[-1]), "send_result redacts description, fields, footer")
    await m.send_card("c", CardSpec(title="t " + HEX, description="d " + SECRET_LINE,
                                    fields=[("n", "v " + HEX)], footer="f " + HEX))
    check(HEX not in all_text(ch.sent[-1]), "send_card redacts title, description, fields, footer")

    md = _TMP / "result.md"
    raw = f"# Result\nPRIVATE_KEY=0x{HEX}\nkeep me\n"
    md.write_text(raw, encoding="utf-8")
    await m.send_file("c", str(md), "result.md", caption="cap " + HEX)
    up = ch.sent[-1].kw["_bytes"][0].decode("utf-8")
    check(HEX not in up and "keep me" in up, "an uploaded text file is redacted", up)
    check(md.read_text(encoding="utf-8") == raw, "the file on disk is unchanged")
    check(HEX not in str(ch.sent[-1].kw.get("content")), "the caption is redacted")

    diff = _TMP / "change.diff"
    diff.write_bytes(b"+KEY " + HEX.encode() + b"\n+\xff\xfe latin bytes\n")
    await m.send_files("c", [str(diff)])
    up = ch.sent[-1].kw["_bytes"][0]
    check(HEX.encode() not in up and b"\xff\xfe latin bytes" in up,
          "send_files redacts text and keeps undecodable bytes", up)

    png = _TMP / "pic.png"
    blob = b"\x89PNG\r\n\x1a\n" + HEX.encode() + bytes(range(256))
    png.write_bytes(blob)
    await m.send_file("c", str(png), "pic.png")
    check(ch.sent[-1].kw["_bytes"][0] == blob, "a binary file passes through untouched")

    odd = _TMP / "notes.weird"
    odd.write_text("k " + HEX, encoding="utf-8")
    await m.send_file("c", str(odd), "notes.weird")
    check(HEX.encode() not in ch.sent[-1].kw["_bytes"][0], "an unknown suffix that is UTF-8 is sniffed as text")

    blobby = _TMP / "dump.bin"
    blobby.write_bytes(bytes(range(256)) * 4 + b" k " + HEX.encode())
    await m.send_file("c", str(blobby), "dump.bin")
    check(ch.sent[-1].kw["_bytes"][0] == blobby.read_bytes(), "an unknown binary file passes untouched")

    import bot.discord.adapter as adapter_mod
    big = _TMP / "big.out"
    big.write_text(("x" * 99 + "\n") * 1000 + "k " + HEX + "\n", encoding="utf-8")
    old_max = adapter_mod._TEXT_UPLOAD_MAX
    adapter_mod._TEXT_UPLOAD_MAX = 200_000
    try:
        await m.send_file("c", str(big), "big.out")
        up = ch.sent[-1].kw["_bytes"][0]
        check(HEX.encode() not in up, "unknown-suffix text past the sniff sample is filtered")
        adapter_mod._TEXT_UPLOAD_MAX = 50_000
        await m.send_file("c", str(big), "big.out")
        up = ch.sent[-1].kw["_bytes"][0]
        check(b"cut here by the bot" in up and len(up) < 60_000,
              "an oversized text file is cut, never sent unfiltered")
    finally:
        adapter_mod._TEXT_UPLOAD_MAX = old_max


asyncio.run(adapter_checks())

# --- Owner detail and the session note --------------------------------------------------
print("Session note")
from bot.claude.runner import ClaudeRunner  # noqa: E402
from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType  # noqa: E402
from bot.engine import lifecycle  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

sdir = _TMP / "store"
sdir.mkdir()
store = StateStore(sdir / "state.json", sdir / "results")


def inst(**kw) -> Instance:
    base = dict(id="q-1", name=None, instance_type=InstanceType.QUERY, prompt="OWNER WORDS",
                repo_name="r", repo_path=str(repo), status=InstanceStatus.RUNNING, mode="explore")
    base.update(kw)
    return Instance(**base)


owner_ctx = SimpleNamespace(store=store, channel_id="chan-1", is_owner=True, thread_has_guest=False)
guest_ctx = SimpleNamespace(store=store, channel_id="chan-1", is_owner=False, thread_has_guest=True)
check(lifecycle._owner_detail(owner_ctx, inst(vault_eligible=True)), "an owner turn gets detail")
check(not lifecycle._owner_detail(owner_ctx, inst(vault_eligible=False)), "not vault-eligible: plain")
check(not lifecycle._owner_detail(guest_ctx, inst(vault_eligible=True)), "a guest in the thread: plain")
check(not lifecycle._owner_detail(owner_ctx, inst(vault_eligible=True, origin=InstanceOrigin.PEER_DELIVERY)),
      "peer output: plain")

_t, hits = redact_report(f"0x{HEX} and {API_VAL}")
lifecycle._remember_hidden(owner_ctx, inst(), hits)
check(len(store.get_hidden_note("chan-1")["hits"]) == 2, "the note is stored per thread")
store2 = StateStore(sdir / "state.json", sdir / "results")
check(store2.get_hidden_note("chan-1") is not None, "the note survives a restart")
lifecycle._remember_hidden(SimpleNamespace(store=store, channel_id="chan-2"),
                           inst(origin=InstanceOrigin.JUDGE), hits)
check(store.get_hidden_note("chan-2") is None, "a judge's output leaves no note")

i_card = inst(session_id="sess-1", origin=InstanceOrigin.RECAP)
lifecycle._take_hidden_note(owner_ctx, i_card)
check(bool(i_card._hidden_note) and store.get_hidden_note("chan-1") is not None,
      "the end card's recap turn reads the note and leaves it for the next real turn")
_t, hits1 = redact_report("password: hunter22x")
lifecycle._remember_hidden(owner_ctx, inst(), hits1)
check(len(store.get_hidden_note("chan-1")["hits"]) == 3, "a second reply adds to a pending note")
i_next = inst(session_id="sess-1")
lifecycle._take_hidden_note(owner_ctx, i_next)
check(store.get_hidden_note("chan-1") is None, "taking the note clears it")
note = i_next._hidden_note or ""
check(note.startswith("SYSTEM NOTICE: 3 value(s)") and "private-key" in note
      and "password" in note and API_VAL not in note,
      "the notice counts kinds and holds no value", note)

runner = ClaudeRunner()
runner._store = store
runner._build_location_block = lambda i: ""  # type: ignore[method-assign]
runner._build_master_context_block = lambda i: ""  # type: ignore[method-assign]


def build(i: Instance) -> str:
    out = runner._build_command(i)
    _cmd, prompt, *files = out
    for f in files:
        if f:
            try:
                os.unlink(f)
            except OSError:
                pass
    return prompt


p1 = build(i_next)
check(p1.startswith("SYSTEM NOTICE:") and p1.index("SYSTEM NOTICE") < p1.index("OWNER WORDS"),
      "the notice is prepended to the next turn", p1[:120])
p2 = build(i_next)
check("SYSTEM NOTICE" not in p2, "and only once")

p_fresh = inst(session_id="sess-1")
p_fresh._hidden_note = "SYSTEM NOTICE: stale"
p_fresh.session_id = None
check("SYSTEM NOTICE" not in build(p_fresh) and not p_fresh._hidden_note,
      "a turn whose session was cleared before the build gets no notice")

store.put_hidden_note("chan-1", {"hits": ["token"]})
i_fresh = inst(session_id=None)
lifecycle._take_hidden_note(owner_ctx, i_fresh)
check(not i_fresh._hidden_note and store.get_hidden_note("chan-1") is None,
      "a fresh session drops the note (it never wrote that reply)")

if FAILS:
    print(f"\nFAIL ({len(FAILS)}):")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("\nAll outbound filter checks passed.")
