"""Secret redaction for anything the bot posts or logs, split out of formatting.py.

Add a new secret-bearing env var to ``_ENV_SECRET_NAMES``.

Order of passes (docs/design/secret-intake.md, "Outbound filter"):

1. Known values first: the vault, the bot's own env secrets, and the key
   files read by bot/keyfiles.py. No false positives by construction.
2. Shapes: vendor prefixes, Bearer, URL credentials, labelled and unlabelled
   seed phrases, secret-named ``name=value`` pairs, 64+ hex runs.

A public id is left alone where the text says it is public: a 64-hex in the
path of an allowlisted explorer link, or right after a label such as ``tx`` or
``pool id``. Every shape pass skips a span that already is a marker, so the
filter can run twice (call site, then the Discord boundary) with no change.

What the user sees in place of a value is a marker with no spaces:
``[hidden:private-key]``, or ``[hidden:NAME@repo/file]`` for a known key in an
owner's thread. Logs keep ``[REDACTED]``.
"""

from __future__ import annotations

import logging
import os
import re
import time
from urllib.parse import urlsplit

from bot import config, keyfiles, secret_detect, vault

log = logging.getLogger(__name__)

# --- Markers -----------------------------------------------------------------

LOG_MARKER = "[REDACTED]"
PLAIN_MARKER = "[hidden]"
_MARKER_PREFIXES = ("[hidden", "[REDACTED")

KIND_PRIVATE_KEY = "private-key"
KIND_PASSWORD = "password"
KIND_API_KEY = "api-key"
KIND_URL_CREDENTIAL = "url-credential"
KIND_SEED_PHRASE = "seed-phrase"
KIND_TOKEN = "token"
# A known key named only in an owner's thread; everywhere else it is this.
KIND_KNOWN_KEY = "known-key"


def marker(kind: str) -> str:
    return f"[hidden:{kind}]"


def _is_marker(value: str) -> bool:
    return value.lstrip("\"'`").startswith(_MARKER_PREFIXES)


# --- Shapes ------------------------------------------------------------------

# Well-known token prefixes (match standalone, no key name needed). The door
# and this file share the vendor list, so a new key shape only goes in one place.
_VENDOR_PATTERNS = list(secret_detect.REDACT_PATTERNS)
_BEARER_PATTERN = re.compile(r'(?i)Bearer\s+([a-zA-Z0-9_./+=-]{20,})')

# Anything sitting between the colon and the @ of a URL is, by definition of
# the userinfo field, a credential, so there is no length below which it is
# safe to print. This used to require 8+ characters, which let a short
# password through verbatim.
_CONN_STRING_PATTERN = re.compile(r'(://[^:\s]+:)([^@\s]+)(@)')

# A credential can also *be* the userinfo, with no password half at all:
# `https://<token>@github.com/o/r.git` is a form GitHub accepts, and a token
# in an unrecognised vendor format matches none of the patterns above. Length
# is what separates it from a real username, and the floor is set well clear
# of the longest ones actually in use: `git` (3), `oauth2` (6),
# `x-access-token` (14), `gitlab-ci-token` (15), so every SSH remote in the
# bot's own output stays readable.
_URL_USERINFO_PATTERN = re.compile(r'(://)([^:/\s@]{20,})(@)')

_SECRET_KEY_WORDS = (
    r'password|passwd|secret|mnemonic|private[_-]?key|seed[_-]?phrase|'
    r'api[_-]?key|access[_-]?key|auth[_-]?(?:key|token|secret)|'
    r'hmac|jwt|credential|client[_-]?secret|app[_-]?secret|'
    r'signing[_-]?key|encryption[_-]?key|master[_-]?key|'
    r'db[_-]?password|connection[_-]?string|'
    r'pinata|infura|alchemy|token'
)

_KV_PATTERN = re.compile(
    r'(?i)'
    r'(?:^|(?<=[\s"\'`]))'
    r'((?=\w*(?:' + _SECRET_KEY_WORDS + r'))'
    r'[a-zA-Z_]\w*)'
    r'["\']?'
    r'\s*[=:]\s*'
    r'["\'`]?'
    r'(.+?)'
    r'["\'`]?'
    r'(?:[,;\s`]|$)',
    re.MULTILINE,
)
# A flag, not a value: `HasToken=False` names a token and holds none. Only
# these exact words; a number or a short word can be a real password.
_KV_NON_VALUES = frozenset({"true", "false", "none", "null", "nil"})
# A count of LLM tokens (`input_tokens: 1234`, `max_tokens=4096`): a plural
# name holding a plain number is a measurement, never a credential.
_KV_TOKEN_COUNT_NAME = re.compile(r'(?i)tokens$')
_KV_COUNT_VALUE = re.compile(r'[\d,._]+[kKmM]?')
# An EVM address is public by shape (a private key is 64 hex, not 40).
_KV_EVM_ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')
# A name that always hides its value, flag-shaped or not.
_KV_ALWAYS = re.compile(r'(?i)private|secret|password|(?:^|_)pk(?:_|$)')

_MNEMONIC_PATTERN = re.compile(
    r'(?i)(mnemonic|seed[_-]?phrase|recovery[_-]?phrase)\s*[=:"\']*\s*'
    r'([a-z]+(?:\s+[a-z]+){11,})',
)

# A raw key, with or without 0x. Also the shape of a tx hash, a pool id, a
# block hash and an event topic, which is what the exemptions below are for.
_HEX_PATTERN = re.compile(r'(?<![0-9A-Za-z])(?:0x)?[0-9a-fA-F]{64,}(?![0-9A-Za-z])')

# --- Public-id exemptions ----------------------------------------------------

# (host, path prefix). A host matches itself and its subdomains. Only the URL
# PATH is exempt: a query string is where a link carries a key, so it stays
# filtered, and so does every other host.
EXPLORER_HOSTS: tuple[tuple[str, str], ...] = (
    ("dexscreener.com", ""),
    ("etherscan.io", ""),
    ("basescan.org", ""),
    ("arbiscan.io", ""),
    ("bscscan.com", ""),
    ("polygonscan.com", ""),
    ("blockscout.com", ""),
    ("app.hyperliquid.xyz", "/explorer"),
    ("hypurrscan.io", ""),
    ("geckoterminal.com", ""),
    ("debank.com", ""),
)
_URL_PATTERN = re.compile(r'https?://[^\s<>"\'`)\]]+', re.IGNORECASE)

# What may sit between a label and its value.
_LABEL_TAIL = re.compile(r'[\s:`*=#"\'(]*$')
_LABEL_PHRASE = re.compile(r'[A-Za-z0-9 \t_-]{1,48}$')
_PUBLIC_LABEL = re.compile(
    r'(?i)(?:^|[^a-z])(?:tx|txid|txn|hash|txhash|transaction|tx[ _-]?hash|tx[ _-]?id|'
    r'pool|pool[ _-]?id|block|topic\d?)$'
)
_PRIVATE_LABEL = re.compile(r'(?i)private|secret|seed|(?:^|[^a-z])(?:key|keys|pk)(?:[^a-z]|$)')


def _host_allowed(host: str, path: str) -> bool:
    host = host.lower().rstrip(".")
    for allowed, prefix in EXPLORER_HOSTS:
        if host == allowed or host.endswith("." + allowed):
            if not prefix or path == prefix or path.startswith(prefix + "/"):
                return True
    return False


def _explorer_path_spans(text: str) -> list[tuple[int, int]]:
    """Spans of URL paths on allowlisted explorer hosts."""
    if "://" not in text:
        return []
    spans: list[tuple[int, int]] = []
    for m in _URL_PATTERN.finditer(text):
        url = m.group()
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.username or parts.password or not parts.hostname:
            continue
        if not _host_allowed(parts.hostname, parts.path):
            continue
        path_start = url.find("/", len(parts.scheme) + 3)
        if path_start < 0:
            continue
        cut = len(url)
        for sep in ("?", "#"):
            i = url.find(sep, path_start)
            if i >= 0:
                cut = min(cut, i)
        spans.append((m.start() + path_start, m.start() + cut))
    return spans


def _labelled_public(text: str, start: int) -> bool:
    """Is the value at ``start`` labelled as a public id (and not as a key)?"""
    before = text[max(0, start - 80):start]
    tail = _LABEL_TAIL.search(before)
    head = before[:tail.start()] if tail else before
    phrase = _LABEL_PHRASE.search(head)
    if not phrase:
        return False
    words = phrase.group()
    return bool(_PUBLIC_LABEL.search(words)) and not _PRIVATE_LABEL.search(words)


# --- Literal secrets ---------------------------------------------------------
#
# The patterns above recognise secret *shapes*, and an audit showed the two
# that matter most to this bot have no recognisable shape: a bare Discord bot
# token and a Discord webhook URL both survived every pattern here, bare and in
# `NAME=value` form alike. So match on the *value* instead of the shape: pull
# the real secrets out of the vault, config and the key files, and replace them
# literally. That has no false positives by construction: it can only ever
# match the actual secret, and it needs no new regex when a format changes.
_ENV_SECRET_NAMES = (
    "DISCORD_BOT_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "TWITTER_BEARER_TOKEN", "TEST_WEBHOOK_URL", "TEST_LOBBY_WEBHOOK_URL",
)

# Below this length a "secret" is more likely a placeholder than a credential,
# and blanket-replacing a 3-character string would gut unrelated text.
_ENV_SECRET_MIN_LEN = 8
# A hex key is matched in any case, with or without 0x.
_HEX_LITERAL_MIN = 16
_HEX_BODY = re.compile(r'(?:0[xX])?([0-9a-fA-F]+)')


class _Literals:
    __slots__ = ("plain", "hex_re", "hex_names")

    def __init__(self, named: list[tuple[str, str]]):
        plain: list[tuple[str, str]] = []
        hex_names: dict[str, str] = {}
        for name, value in named:
            m = _HEX_BODY.fullmatch(value)
            if m and len(m.group(1)) >= _HEX_LITERAL_MIN:
                hex_names.setdefault(m.group(1).lower(), name)
            else:
                plain.append((name, value))
        plain.sort(key=lambda p: len(p[1]), reverse=True)
        self.plain = tuple(plain)
        self.hex_names = hex_names
        if hex_names:
            bodies = sorted(hex_names, key=len, reverse=True)
            self.hex_re = re.compile(
                r'(?i)(?:0x)?(?:' + "|".join(re.escape(b) for b in bodies) + r')'
            )
        else:
            self.hex_re = None


_literal_cache: tuple[tuple, _Literals] | None = None


def _named_literals() -> list[tuple[str, str]]:
    named: list[tuple[str, str]] = []
    try:
        named.extend(vault.literal_named())
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        for name in _ENV_SECRET_NAMES:
            v = getattr(config, name, None) or os.getenv(name, "")
            if v and isinstance(v, str) and len(v) >= _ENV_SECRET_MIN_LEN:
                named.append((name, v))
    except Exception:  # pragma: no cover - defensive
        pass
    try:
        named.extend(keyfiles.entries())
    except Exception:  # pragma: no cover - defensive
        pass
    return named


def _literals() -> _Literals:
    """The literal secret strings to strip.

    redact_secrets sits on the display hot path, so this must not re-read
    anything per call: the vault costs one stat and the key files are
    throttled in bot/keyfiles.py. A failure degrades to regex-only redaction
    rather than taking down every message the bot renders.
    """
    global _literal_cache
    named = _named_literals()
    key = tuple(named)
    cached = _literal_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for name, value in named:
        if value not in seen:
            seen.add(value)
            unique.append((name, value))
    lits = _Literals(unique)
    _literal_cache = (key, lits)
    return lits


def _strip_literals(text: str, label, hits: list[str] | None) -> str:
    """Replace every known value. ``label(name)`` gives (marker, reported kind)."""
    lits = _literals()
    for name, secret in lits.plain:
        if secret in text:
            mark, kind = label(name)
            n = text.count(secret)
            text = text.replace(secret, mark)
            if hits is not None:
                hits.extend([kind] * n)
    if lits.hex_re is not None:
        def _sub(m: re.Match) -> str:
            body = m.group()
            body = body[2:] if body[:2].lower() == "0x" else body
            name = lits.hex_names.get(body.lower(), "")
            mark, kind = label(name)
            if hits is not None:
                hits.append(kind)
            return mark
        text = lits.hex_re.sub(_sub, text)
    return text


def scrub_known(text: str) -> str:
    """Only what is certainly a secret: known values and vendor prefixes.

    For log lines, where the prose-shaped patterns below would eat ordinary
    words such as "token: limit" and make the log harder to read.
    """
    if not text:
        return text
    text = _strip_literals(text, lambda _n: (LOG_MARKER, ""), None)
    for pattern in _VENDOR_PATTERNS:
        text = pattern.sub(LOG_MARKER, text)
    return text


class RedactingFormatter(logging.Formatter):
    """Scrubs the finished line, so a traceback or a logged exception is covered too."""

    def format(self, record: logging.LogRecord) -> str:
        return scrub_known(super().format(record))


# --- The outbound filter -----------------------------------------------------


def _sub_group(pattern: re.Pattern, text: str, group: int, kind_of, hits: list[str],
               skip=None) -> str:
    """Replace one group of each match with a marker, keeping the rest as written."""
    def _repl(m: re.Match) -> str:
        value = m.group(group)
        if not value or _is_marker(value):
            return m.group()
        kind = kind_of(m)
        if kind is None or (skip is not None and skip(m)):
            return m.group()
        hits.append(kind)
        s, e = m.start(group) - m.start(), m.end(group) - m.start()
        whole = m.group()
        return whole[:s] + marker(kind) + whole[e:]
    return pattern.sub(_repl, text)


def _kv_kind(m: re.Match) -> str | None:
    name, value = m.group(1), m.group(2)
    always = bool(_KV_ALWAYS.search(name))
    bare = value.strip('`"\'()[]{}.,;:')
    if not always and bare.lower() in _KV_NON_VALUES:
        return None
    if not always:
        if _KV_TOKEN_COUNT_NAME.search(name) and _KV_COUNT_VALUE.fullmatch(bare):
            return None
        # `tokenAddress: 0x...`, `token_id=42`: the name says it is public, the
        # same rule the key files use (bot/keyfiles.py).
        if keyfiles.is_public_name(name) or _KV_EVM_ADDRESS.fullmatch(bare):
            return None
    low = name.lower()
    if "pass" in low:
        return KIND_PASSWORD
    if "private" in low or re.search(r'(?:^|_)pk(?:_|$)', low):
        return KIND_PRIVATE_KEY
    if "mnemonic" in low or "seed" in low:
        return KIND_SEED_PHRASE
    if "token" in low or "jwt" in low:
        return KIND_TOKEN
    return KIND_API_KEY


def _replace_spans(text: str, spans: list[tuple[int, int]], kind: str, hits: list[str]) -> str:
    for start, end in sorted(spans, reverse=True):
        if _is_marker(text[start:end]):
            continue
        text = text[:start] + marker(kind) + text[end:]
        hits.append(kind)
    return text


_SHAPE_KINDS = frozenset({
    KIND_PRIVATE_KEY, KIND_PASSWORD, KIND_API_KEY, KIND_URL_CREDENTIAL,
    KIND_SEED_PHRASE, KIND_TOKEN, KIND_KNOWN_KEY,
})


_last_log: tuple[str, float] = ("", 0.0)
_LOG_REPEAT_SECONDS = 30.0


def _log_hits(hits: list[str]) -> None:
    """One INFO line per hide: kinds and counts, never a value or a key name."""
    global _last_log
    counts: dict[str, int] = {}
    for kind in hits:
        k = kind if kind in _SHAPE_KINDS else KIND_KNOWN_KEY
        counts[k] = counts.get(k, 0) + 1
    summary = ", ".join(f"{k} x{n}" for k, n in sorted(counts.items()))
    now = time.monotonic()
    if _last_log[0] == summary and now - _last_log[1] < _LOG_REPEAT_SECONDS:
        return
    _last_log = (summary, now)
    log.info("Secret filter hid %d value(s): %s", len(hits), summary)


def redact_report(text: str, *, detail: bool = False) -> tuple[str, list[str]]:
    """``text`` with secrets hidden, and one label per value hidden.

    ``detail`` names a known key in its marker (``[hidden:NAME@repo/file]``);
    only an owner's own thread may ask for it. Without it a known key is a
    plain ``[hidden]``, so a guest or the other machine never learns a name.
    """
    if not text:
        return text, []
    hits: list[str] = []

    def _label(name: str) -> tuple[str, str]:
        if detail and name:
            return marker(name), name
        return PLAIN_MARKER, KIND_KNOWN_KEY

    # Literal pass FIRST, while every known secret is still verbatim. Running
    # it last would leave a gap: a pattern below can chew a fragment out of a
    # secret (the hex pass on a long hex run inside it, say) without removing
    # the whole thing, and the remainder would then no longer match.
    text = _strip_literals(text, _label, hits)

    def _whole(pattern: re.Pattern, kind: str, t: str) -> str:
        def _repl(m: re.Match) -> str:
            if _is_marker(m.group()):
                return m.group()
            hits.append(kind)
            return marker(kind)
        return pattern.sub(_repl, t)

    for pattern in _VENDOR_PATTERNS:
        text = _whole(pattern, KIND_API_KEY, text)
    text = _sub_group(_BEARER_PATTERN, text, 1, lambda m: KIND_TOKEN, hits)
    text = _sub_group(_CONN_STRING_PATTERN, text, 2, lambda m: KIND_URL_CREDENTIAL, hits)
    text = _sub_group(_URL_USERINFO_PATTERN, text, 2, lambda m: KIND_URL_CREDENTIAL, hits)
    text = _sub_group(_MNEMONIC_PATTERN, text, 2, lambda m: KIND_SEED_PHRASE, hits)
    text = _replace_spans(text, secret_detect.seed_run_spans(text), KIND_SEED_PHRASE, hits)
    text = _sub_group(_KV_PATTERN, text, 2, _kv_kind, hits)

    explorer = _explorer_path_spans(text)

    def _public(m: re.Match) -> bool:
        s, e = m.start(), m.end()
        if any(a <= s and e <= b for a, b in explorer):
            return True
        return _labelled_public(text, s)

    text = _sub_group(_HEX_PATTERN, text, 0, lambda m: KIND_PRIVATE_KEY, hits, skip=_public)
    if hits:
        _log_hits(hits)
    return text, hits


def redact_secrets(text: str, *, detail: bool = False) -> str:
    """Scrub API keys, tokens, and secrets from text."""
    return redact_report(text, detail=detail)[0]


def session_notice(count: int, kinds: str) -> str:
    """What a session is told on its next turn after the filter hid something."""
    return (
        f"SYSTEM NOTICE: {count} value(s) in your previous reply were hidden from "
        f"the user by the secret filter ({kinds}). The user saw [hidden:...] in "
        "their place. Don't reprint them; refer to a key by its name. A public id "
        "(tx hash, pool id) shows if given as an explorer link or labelled, e.g. "
        "'tx 0x...'."
    )


def summarize_hits(hits: list[str]) -> str:
    """``private-key x2, DISCORD_BOT_TOKEN`` style: what a session is told."""
    counts: dict[str, int] = {}
    for kind in hits:
        counts[kind] = counts.get(kind, 0) + 1
    return ", ".join(k if n == 1 else f"{k} x{n}" for k, n in counts.items())
