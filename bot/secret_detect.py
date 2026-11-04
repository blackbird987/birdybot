"""Recognising a credential in text a person pasted. Stdlib only, no bot import.

The secret door (``bot/discord/secret_door.py``) asks this module one question:
is there a secret in this text, and how sure are we? Three tiers come back:

  * SURE: a vendor prefix that nothing else carries. Stored without asking.
  * ASK: looks like a credential but might not be (a transaction hash, a
    labelled value, a long random token). The owner decides.
  * BURN: a key to money (seed phrase, PEM private key, xprv). Never stored.

Do not reuse ``redact._KV_PATTERN`` here. That one protects output, where a
false positive costs a word; here a false positive holds a message, and it
matches prose such as "token: limit". See ``docs/design/secret-intake.md``.

Everything is linear in the input: the door runs on the event path, against
attachment bodies of up to 500 KB.
"""

from __future__ import annotations

import bisect
import dataclasses
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SURE = "sure"
ASK = "ask"
BURN = "burn"

REMOVED = "[secret removed]"

_TIER_RANK = {BURN: 0, SURE: 1, ASK: 2}

# A key is never a tail of a longer word: "task-..." and "risk-..." hold "sk-".
_LB = r"(?<![A-Za-z0-9_-])"
_RB = r"(?![A-Za-z0-9_-])"


@dataclass(frozen=True)
class Finding:
    tier: str
    start: int
    end: int
    value: str
    shape: str
    suggested_name: str


# (shape, pattern, name, hint). The body group is what the placeholder check reads,
# so "sk-ant-xxxxxxxx" is judged on its x's and not on the vendor prefix. A None
# name means the prefix says nothing about the vendor. The hint is a substring
# every match holds: testing it first keeps the door cheap, since most messages
# carry none of them.
_SURE_TABLE: list[tuple[str, re.Pattern, str | None, str]] = [
    ("anthropic", re.compile(_LB + r"sk-ant-(?:api\d\d-)?(?P<body>[A-Za-z0-9_-]{20,})"), "PASTED_ANTHROPIC_KEY", "sk-ant-"),
    ("openai_project", re.compile(_LB + r"sk-proj-(?P<body>[A-Za-z0-9_-]{20,})"), "OPENAI_API_KEY", "sk-proj-"),
    ("sk", re.compile(_LB + r"sk-(?P<body>[A-Za-z0-9_-]{20,})"), None, "sk-"),
    ("github", re.compile(_LB + r"gh[pos]_(?P<body>[A-Za-z0-9]{20,})"), "GITHUB_TOKEN", "_"),
    ("github_pat", re.compile(_LB + r"github_pat_(?P<body>[A-Za-z0-9_]{20,})"), "GITHUB_TOKEN", "github_pat_"),
    ("aws", re.compile(_LB + r"AKIA(?P<body>[A-Z0-9]{16})" + _RB), "AWS_ACCESS_KEY_ID", "AKIA"),
    ("jwt", re.compile(_LB + r"eyJ(?P<body>[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})"), None, "eyJ"),
    ("stripe", re.compile(_LB + r"sk_live_(?P<body>[A-Za-z0-9]{16,})"), "STRIPE_SECRET_KEY", "sk_live_"),
    ("stripe_restricted", re.compile(_LB + r"rk_live_(?P<body>[A-Za-z0-9]{16,})"), "STRIPE_RESTRICTED_KEY", "rk_live_"),
    ("slack_bot", re.compile(_LB + r"xoxb-(?P<body>[A-Za-z0-9-]{20,})"), "SLACK_BOT_TOKEN", "xoxb-"),
    ("slack_user", re.compile(_LB + r"xoxp-(?P<body>[A-Za-z0-9-]{20,})"), "SLACK_USER_TOKEN", "xoxp-"),
    ("huggingface", re.compile(_LB + r"hf_(?P<body>[A-Za-z0-9]{20,})"), "HF_TOKEN", "hf_"),
    ("runpod", re.compile(_LB + r"rpa_(?P<body>[A-Za-z0-9]{20,})"), "RUNPOD_API_KEY", "rpa_"),
    ("gitlab", re.compile(_LB + r"glpat-(?P<body>[A-Za-z0-9_-]{20,})"), "GITLAB_TOKEN", "glpat-"),
    ("google", re.compile(_LB + r"AIza(?P<body>[A-Za-z0-9_-]{35})" + _RB), "GOOGLE_API_KEY", "AIza"),
    ("groq", re.compile(_LB + r"gsk_(?P<body>[A-Za-z0-9]{20,})"), "GROQ_API_KEY", "gsk_"),
    ("tavily", re.compile(_LB + r"tvly-(?P<body>[A-Za-z0-9_-]{20,})"), "TAVILY_API_KEY", "tvly-"),
    ("perplexity", re.compile(_LB + r"pplx-(?P<body>[A-Za-z0-9]{20,})"), "PERPLEXITY_API_KEY", "pplx-"),
    ("telegram", re.compile(_LB + r"\d{8,10}:(?P<body>[A-Za-z0-9_-]{35})" + _RB), "TELEGRAM_BOT_TOKEN", ":"),
    ("discord_bot", re.compile(_LB + r"[MNO][A-Za-z0-9_-]{23,27}\.[A-Za-z0-9_-]{6,7}\.(?P<body>[A-Za-z0-9_-]{27,})"), "PASTED_DISCORD_BOT_TOKEN", "."),
    ("discord_webhook", re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d{17,20}/(?P<body>[A-Za-z0-9_-]{50,})"), "DISCORD_WEBHOOK_URL", "/webhooks/"),
]

# Output gives up a word rather than a key, so redaction also matches a key glued
# to what comes before it. The generic row keeps its boundary past plain
# alphanumerics, or "task-fix-the-login-bug" would lose its tail in every log line.
REDACT_PATTERNS: list[re.Pattern] = [
    *(re.compile(row[1].pattern.removeprefix(_LB)) for row in _SURE_TABLE if row[0] != "sk"),
    *(row[1] for row in _SURE_TABLE if row[0] == "sk"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
]

_HEX64_0X_PATTERN = re.compile(_LB + r"0x[0-9a-fA-F]{64}" + _RB)
_BEARER_PATTERN = re.compile(r"(?i)(?<![A-Za-z0-9_-])Bearer\s+([A-Za-z0-9_./+=-]{20,})")

_HEX64_PATTERN = re.compile(_LB + r"[0-9a-fA-F]{64}" + _RB)
_URL_CRED_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,30}://[^/\s:@]{1,256}:([^/\s@]{3,512})@")
_URL_QUERY_CRED_PATTERN = re.compile(
    r"(?i)[?&](?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|password|key)=([^&\s#]{8,512})"
)
_ENV_ASSIGN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{2,63})\s*=\s*([\"']?)([^\s\"']{6,4096})\2"
)
# Config and code files write the label in any case, often quoted. Only used
# where the entropy pass is off, and only with a value that reads as random.
_LOOSE_ASSIGN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[\"']?([A-Za-z][A-Za-z0-9_-]{2,63})[\"']?[ \t]*[=:][ \t]*[\"']?([A-Za-z0-9_./+=-]{16,512})"
)
_PROSE_LABEL_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])([a-z][a-z0-9]{1,20})[ \t]+(?:api[ _-]?)?(?:key|token|secret)"
    r"[ \t]*(?:is|:|=)?[ \t]*[\"'`]?([A-Za-z0-9_./+=-]{6,512})"
)
_PASSWORD_LABEL_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:password|passwd|pwd|passphrase)[ \t]*(?:is\b|:|=)[ \t]*[\"'`]?([^\s\"'`]{6,512})"
)

_SECRET_NAME_SEGMENTS = frozenset({
    "KEY", "KEYS", "TOKEN", "TOKENS", "SECRET", "SECRETS", "PASS", "PASSWD",
    "PASSWORD", "PWD", "CREDENTIAL", "CREDENTIALS", "CRED", "CREDS", "AUTH",
    "APIKEY", "PASSPHRASE", "WEBHOOK", "BEARER", "COOKIE",
})
_BURN_NAME_SEGMENTS = frozenset({"PRIVATE", "PRIVKEY", "MNEMONIC", "SEED", "WALLET"})

_PLACEHOLDER_VALUES = frozenset({
    "password", "passwd", "pass", "pwd", "secret", "changeme", "example",
    "placeholder", "redacted", "yourpassword", "your_password", "xxxxxx",
    "token", "apikey", "api_key", "none", "null", "true", "false",
})

# Common word before "key" that names no vendor.
_NAME_STOPWORDS = frozenset({
    "a", "an", "the", "my", "your", "our", "his", "her", "their", "this",
    "that", "new", "old", "api", "secret", "here", "is", "and", "or", "for",
    "with", "same", "one", "another", "some", "any", "of", "to", "in", "on",
    "access", "private", "public", "auth", "bearer", "hash", "primary",
    "foreign", "unique", "sort", "dict", "map", "cache", "lookup", "hot",
    "ssh", "session", "user", "bot", "test", "input", "next", "last", "first",
})
# The word before "key" when the id after it is a row or a request, not a credential.
_ID_KEY_WORDS = frozenset({
    "primary", "foreign", "unique", "composite", "partition", "sort", "hash",
    "idempotency", "cache", "lookup", "dedup", "object", "item", "record", "row",
    "map", "dict", "session", "user", "public", "test",
})

# Where something is rather than what it unlocks. Only a value that reads as
# random gets past these, however it is labelled.
_LOCATION_PATTERNS = [
    re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,30}://[^\s<>\"'`)\]]+"),
    # A path starts at a boundary. After "+" or "/" this is the middle of base64.
    re.compile(r"(?<![A-Za-z0-9_+/])(?:~|\.{1,2})?/[\w.@+-]+(?:/[\w.@+-]*)+"),
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:\\[^\s\"'`]+"),
    re.compile(r"(?i)\bsha(?:1|224|256|384|512)[:-][A-Za-z0-9+/=_-]+"),
]
# An id, a commit or an address on its own. Under a label it can be a real key:
# RunPod and others hand out UUIDs, and old GitHub tokens are 40 hex.
_ID_PATTERNS = [
    re.compile(r"(?i)(?<![0-9a-f-])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f-])"),
    re.compile(_LB + r"0x[0-9a-fA-F]{40}" + _RB),
    re.compile(_LB + r"[0-9a-fA-F]{40}" + _RB),
    re.compile(_LB + r"\d{17,20}" + _RB),
]

_ENTROPY_TOKEN = re.compile(r"(?<![A-Za-z0-9_+/=.-])[A-Za-z0-9_+/=.-]{24,}")
_ENTROPY_TOKEN_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_+/=.-"
)
_ENTROPY_MIN = 3.5
_NAMED_FINDINGS_MAX = 50
_ENTROPY_TOKEN_MIN = 24

_WORD = re.compile(r"[A-Za-z]+")
# What may sit between two words of a seed phrase: spaces, commas, numbering.
_SEED_GAP = re.compile(r"[\s,;.:)(\[\]#\d-]{1,12}")
_SEED_RUN_MIN = 12
_SEED_LABELLED_MIN = 11
_SEED_LABEL = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])(?:seed(?:[ _-]?(?:phrase|words))?|mnemonic(?:[ _-]?phrase)?|"
    r"recovery[ _-]?(?:phrase|words)|wallet[ _-]?(?:phrase|words)|secret[ _-]?phrase)"
    r"[ \t]*(?:is\b|:|=|-)?[\s\"'`]*"
)
# A labelled phrase tolerates typos, so most words must still be on the list.
_SEED_LABELLED_RATIO = 0.8

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----")
_PEM_END = "-----END "
_PEM_BODY_MIN = 40
_XPRV_PATTERN = re.compile(_LB + r"xprv[1-9A-HJ-NP-Za-km-z]{100,}" + _RB)

_BASE58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]+")
_HEX = re.compile(r"[0-9a-fA-F]+")

_bip39_words: frozenset[str] | None = None


def bip39_words() -> frozenset[str]:
    """The BIP39 English list, read once. An unreadable file means an empty set.

    Failing to read it must not take the door down: the labelled and PEM checks
    still run, and every other tier is unaffected.
    """
    global _bip39_words
    if _bip39_words is None:
        try:
            path = Path(__file__).with_name("bip39_english.txt")
            _bip39_words = frozenset(path.read_text(encoding="utf-8").split())
        except OSError:
            _bip39_words = frozenset()
    return _bip39_words


def is_low_variety(body: str) -> bool:
    """A placeholder: too few distinct characters, or one character dominating."""
    if len(set(body)) < 6:
        return True
    return Counter(body).most_common(1)[0][1] * 2 > len(body)


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    n = len(value)
    return -sum((c / n) * math.log2(c / n) for c in Counter(value).values())


def _segment_is_wordlike(seg: str) -> bool:
    if len(seg) < 3 or not seg.isalpha():
        return False
    if not (seg.islower() or seg.isupper() or seg.istitle()):
        return False
    vowels = sum(ch in "aeiouyAEIOUY" for ch in seg)
    return vowels * 5 >= len(seg)


_CAMEL_SPLIT = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def _looks_like_identifier(token: str) -> bool:
    """Mostly dictionary-like pieces: a branch, a slug, a long function name."""
    alpha_digit = sum(ch.isalnum() for ch in token)
    if not alpha_digit:
        return True
    covered = 0
    for piece in re.split(r"[_\-./+=]", token):
        for seg in _CAMEL_SPLIT.findall(piece):
            if seg.isdigit() or _segment_is_wordlike(seg):
                covered += len(seg)
    return covered >= alpha_digit * 0.6


def is_high_entropy(token: str) -> bool:
    if token.isalpha() or token.isdigit():
        return False
    if not any(ch.isdigit() for ch in token) or not any(ch.isalpha() for ch in token):
        return False
    if shannon_entropy(token) < _ENTROPY_MIN:
        return False
    return not _looks_like_identifier(token)


def is_private_key_candidate(value: str) -> bool:
    """Could this be a raw wallet key? Decides whether to offer the Private key button."""
    body = value[2:] if value[:2] in ("0x", "0X") else value
    if len(body) == 64 and _HEX.fullmatch(body):
        return True
    return len(body) >= 80 and bool(_HEX.fullmatch(body) or _BASE58.fullmatch(body))


def name_looks_secret(name: str) -> bool:
    segments = set(name.upper().split("_"))
    if segments & _SECRET_NAME_SEGMENTS:
        return True
    upper = name.upper()
    return "APIKEY" in upper or "PASSWORD" in upper


def name_burns(name: str) -> bool:
    return bool(set(name.upper().split("_")) & _BURN_NAME_SEGMENTS)


def default_name(value: str) -> str:
    """The name for a value with nothing around it to suggest a better one."""
    return _last4_name(value)


def is_placeholder_value(value: str) -> bool:
    """An unfilled template value: ``changeme``, ``<your key>``, ``${TOKEN}``, ``xxxx``."""
    if not value or value.lower() in _PLACEHOLDER_VALUES:
        return True
    return value[:1] in "[<${(%" or is_low_variety(value)


def _value_is_secretish(value: str) -> bool:
    """A labelled value worth asking about: not a placeholder, not a plain word."""
    if value.lower() in _PLACEHOLDER_VALUES:
        return False
    if value[:1] in "[<${(%" or "://" in value or is_low_variety(value):
        return False
    if value.isdigit():
        return False
    has_digit = any(ch.isdigit() for ch in value)
    has_symbol = any(not ch.isalnum() for ch in value)
    return has_digit or has_symbol


def _last4_name(value: str) -> str:
    tail = re.sub(r"[^A-Za-z0-9]", "", value)[-4:].upper() or "X"
    return f"SECRET_{tail}"


def _name_from_context(text: str, start: int) -> str | None:
    """Infer a name from the words just before a value: "runpod key" -> RUNPOD_API_KEY."""
    before = text[max(0, start - 80):start]
    assign = re.search(r"([A-Z][A-Z0-9_]{2,63})\s*[=:]\s*[\"']?$", before)
    if assign:
        return assign.group(1)
    label = re.search(
        r"(?i)([a-z][a-z0-9]{1,20})[ \t]+(?:api[ _-]?)?(key|token|secret)[ \t]*(?:is|:|=)?[ \t]*[\"'`]?$",
        before,
    )
    if label and label.group(1).lower() not in _NAME_STOPWORDS:
        word, kind = label.group(1).upper(), label.group(2).upper()
        return f"{word}_API_KEY" if kind == "KEY" else f"{word}_{kind}"
    return None


def _blank(text: str, patterns: list[re.Pattern]) -> str:
    """Blank what these patterns match, keeping every offset where it was."""
    for pattern in patterns:
        text = pattern.sub(lambda m: " " * len(m.group()), text)
    return text


def _scan_sure(text: str) -> list[Finding]:
    out: list[Finding] = []
    for shape, pattern, name, hint in _SURE_TABLE:
        if hint not in text:
            continue
        for m in pattern.finditer(text):
            if is_low_variety(m.group("body")):
                continue
            out.append(Finding(SURE, m.start(), m.end(), m.group(), shape, name or ""))
    return out


def _seed_runs(text: str) -> list[Finding]:
    """Runs of consecutive BIP39 words, broken by anything but a separator."""
    words = bip39_words()
    if not words:
        return []
    out: list[Finding] = []
    run_start = run_end = -1
    run_len = 0

    def flush() -> None:
        if run_len >= _SEED_RUN_MIN:
            out.append(Finding(BURN, run_start, run_end, text[run_start:run_end], "seed_phrase", ""))

    for m in _WORD.finditer(text):
        token = m.group().lower()
        if token not in words:
            flush()
            run_len = 0
            continue
        if run_len and not _SEED_GAP.fullmatch(text, run_end, m.start()):
            flush()
            run_len = 0
        if not run_len:
            run_start = m.start()
        run_end = m.end()
        run_len += 1
    flush()
    return out


def _labelled_seeds(text: str) -> list[Finding]:
    """A labelled phrase of 11+ words, where a typo may have left the list."""
    words = bip39_words()
    if not words:
        return []
    out: list[Finding] = []
    pos = 0
    for label in _SEED_LABEL.finditer(text):
        if label.start() < pos:
            continue
        cursor = label.end()
        count = known = 0
        end = cursor
        for m in _WORD.finditer(text, cursor):
            if m.start() != end and not _SEED_GAP.fullmatch(text, end, m.start()):
                break
            count += 1
            known += m.group().lower() in words
            end = m.end()
            if count >= 33:
                break
        if count >= _SEED_LABELLED_MIN and known >= count * _SEED_LABELLED_RATIO:
            out.append(Finding(BURN, cursor, end, text[cursor:end], "seed_phrase", ""))
            pos = end
    return out


def _pem_blocks(text: str) -> list[Finding]:
    out: list[Finding] = []
    pos = 0
    while True:
        begin = _PEM_BEGIN.search(text, pos)
        if not begin:
            return out
        end_at = text.find(_PEM_END, begin.end())
        if end_at < 0:
            stop = len(text)
        else:
            close = text.find("-----", end_at + len(_PEM_END))
            stop = close + 5 if close >= 0 else len(text)
        body_end = end_at if end_at >= 0 else stop
        body = sum(ch.isalnum() for ch in text[begin.end():body_end])
        if body >= _PEM_BODY_MIN:
            out.append(Finding(BURN, begin.start(), stop, text[begin.start():stop], "pem", ""))
        if end_at < 0:
            return out
        pos = stop


def _scan_burn(text: str) -> list[Finding]:
    out = _pem_blocks(text) + _seed_runs(text) + _labelled_seeds(text)
    for m in _XPRV_PATTERN.finditer(text):
        out.append(Finding(BURN, m.start(), m.end(), m.group(), "xprv", ""))
    return out


def _scan_ask(text: str, *, attachment: bool) -> list[Finding]:
    """Everything worth a question.

    A label says what to look at, so labelled values are read from the raw text:
    reading them from masked text would cut ``API_KEY=abc+/def/ghi`` at the path
    and leave the rest of the key in the prompt. The masks then only decide
    whether a value that starts inside a location or an id still counts.
    """
    locations = _blank(text, _LOCATION_PATTERNS)
    masked = _blank(locations, _ID_PATTERNS)

    def is_location(start: int, value: str) -> bool:
        return locations[start] == " " and not is_high_entropy(value)

    out: list[Finding] = []
    # Credentials inside a URL are read from the raw text: the mask removes URLs.
    for pattern, shape in ((_URL_CRED_PATTERN, "url_userinfo"), (_URL_QUERY_CRED_PATTERN, "url_query")):
        for m in pattern.finditer(text):
            value = m.group(1)
            if _value_is_secretish(value) or (shape == "url_userinfo" and value.lower() not in _PLACEHOLDER_VALUES):
                out.append(Finding(ASK, m.start(1), m.end(1), value, shape, _last4_name(value)))
    for pattern in (_HEX64_0X_PATTERN, _HEX64_PATTERN):
        for m in pattern.finditer(masked):
            out.append(Finding(ASK, m.start(), m.end(), m.group(), "hex64", ""))
    for m in _BEARER_PATTERN.finditer(text):
        value = m.group(1)
        if not is_location(m.start(1), value) and not is_low_variety(value) and not _looks_like_identifier(value):
            out.append(Finding(ASK, m.start(1), m.end(1), value, "bearer", ""))
    for m in _ENV_ASSIGN_PATTERN.finditer(text):
        name, value = m.group(1), m.group(3)
        if name_looks_secret(name) and not is_location(m.start(3), value) and _value_is_secretish(value):
            out.append(Finding(ASK, m.start(3), m.end(3), value, "labelled", name))
    for m in _PROSE_LABEL_PATTERN.finditer(text):
        word, value = m.group(1).lower(), m.group(2)
        if word in ("is", "the", "a", "an") or is_location(m.start(2), value):
            continue
        # "primary key <uuid>" is a row, "runpod key <uuid>" is a credential.
        if masked[m.start(2)] == " " and word in _ID_KEY_WORDS:
            continue
        has_alpha = any(ch.isalpha() for ch in value)
        has_digit = any(ch.isdigit() for ch in value)
        if has_alpha and has_digit and _value_is_secretish(value):
            out.append(Finding(ASK, m.start(2), m.end(2), value, "labelled", ""))
    for m in _PASSWORD_LABEL_PATTERN.finditer(text):
        value = m.group(1)
        if not is_location(m.start(1), value) and _value_is_secretish(value):
            out.append(Finding(ASK, m.start(1), m.end(1), value, "password", _last4_name(value)))
    if attachment:
        for m in _LOOSE_ASSIGN_PATTERN.finditer(text):
            name, value = m.group(1), m.group(2)
            if name_looks_secret(name.replace("-", "_")) and is_high_entropy(value):
                out.append(Finding(ASK, m.start(2), m.end(2), value, "labelled", name.upper().replace("-", "_")))
    else:
        for m in _ENTROPY_TOKEN.finditer(masked):
            # A mask can blank part of a token. Take the whole token, or what the
            # mask cut off would stay in the prompt.
            start, end = m.start(), m.end()
            while start and text[start - 1] in _ENTROPY_TOKEN_CHARS:
                start -= 1
            while end < len(text) and text[end] in _ENTROPY_TOKEN_CHARS:
                end += 1
            # Sentence punctuation goes. "=" stays, it can be base64 padding.
            token = text[start:end].rstrip(".,;:")
            # FOO=value is a label and a value, not one long value.
            label_end = token.rstrip("=").rfind("=")
            if label_end >= 0:
                start += label_end + 1
                token = token[label_end + 1:]
            if len(token) >= _ENTROPY_TOKEN_MIN and is_high_entropy(token):
                out.append(Finding(ASK, start, start + len(token), token, "entropy", ""))
    return out


def _settle(findings: list[Finding]) -> list[Finding]:
    """Keep the strongest finding where they overlap. Result is in text order."""
    ordered = sorted(findings, key=lambda f: (_TIER_RANK[f.tier], f.start, f.start - f.end))
    kept: list[Finding] = []
    taken: list[tuple[int, int]] = []
    for f in ordered:
        # Taken spans are disjoint, so only the two neighbours can overlap.
        i = bisect.bisect_left(taken, (f.start, f.end))
        if i > 0 and taken[i - 1][1] > f.start:
            continue
        if i < len(taken) and taken[i][0] < f.end:
            continue
        taken.insert(i, (f.start, f.end))
        kept.append(f)
    kept.sort(key=lambda f: f.start)
    return kept


def _with_names(text: str, findings: list[Finding]) -> list[Finding]:
    """Fill in names. Reading context costs a regex, so only for the first few."""
    out: list[Finding] = []
    for i, f in enumerate(findings):
        if f.tier != BURN and not f.suggested_name:
            name = _name_from_context(text, f.start) if i < _NAMED_FINDINGS_MAX else None
            if not name and f.shape == "sk":
                name = "OPENAI_API_KEY"
            f = dataclasses.replace(f, suggested_name=name or _last4_name(f.value))
        out.append(f)
    return out


def scan(text: str, *, attachment: bool = False) -> list[Finding]:
    """Every secret in ``text``, in order, strongest tier on overlap.

    ``attachment`` skips the entropy tier: lockfiles and logs are full of long
    random-looking strings and the door would hold on all of them.
    """
    if not text:
        return []
    found = _scan_burn(text) + _scan_sure(text)
    found += _scan_ask(text, attachment=attachment)
    return _with_names(text, _settle(found))


def replace_findings(text: str, findings: list[Finding], replacement: str = REMOVED) -> str:
    """``text`` with each finding's span replaced. Findings must not overlap."""
    if not findings:
        return text
    parts: list[str] = []
    pos = 0
    for f in sorted(findings, key=lambda f: f.start):
        parts.append(text[pos:f.start])
        parts.append(replacement)
        pos = f.end
    parts.append(text[pos:])
    return "".join(parts)


def scrub(text: str) -> str:
    """``text`` with every tier replaced, for history replayed into a prompt."""
    return replace_findings(text, scan(text))
