"""Secret redaction for anything the bot posts or logs, split out of formatting.py.

Add a new secret-bearing env var to ``_ENV_SECRET_NAMES``.
"""

from __future__ import annotations

import os
import re

from bot import config

# --- Secret Redaction ---

# Well-known token prefixes (match standalone, no key name needed)
_TOKEN_PATTERNS = [
    re.compile(r'sk-ant-[a-zA-Z0-9_-]{20,}'),
    re.compile(r'sk-[a-zA-Z0-9]{20,}'),
    re.compile(r'gh[pos]_[a-zA-Z0-9]{20,}'),
    re.compile(r'github_pat_[a-zA-Z0-9_]{20,}'),
    re.compile(r'AKIA[A-Z0-9]{16}'),
    re.compile(r'eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}'),
    re.compile(r'0x[0-9a-fA-F]{64}\b'),
    re.compile(r'(?i)Bearer\s+[a-zA-Z0-9_./-]{20,}'),
]

# Anything sitting between the colon and the @ of a URL is, by definition of
# the userinfo field, a credential — so there is no length below which it is
# safe to print. This used to require 8+ characters, which let a short
# password through verbatim.
_CONN_STRING_PATTERN = re.compile(r'(://[^:\s]+:)([^@\s]+)(@)')

# A credential can also *be* the userinfo, with no password half at all:
# `https://<token>@github.com/o/r.git` is a form GitHub accepts, and a token
# in an unrecognised vendor format matches none of the patterns above. Length
# is what separates it from a real username, and the floor is set well clear
# of the longest ones actually in use — `git` (3), `oauth2` (6),
# `x-access-token` (14), `gitlab-ci-token` (15) — so every SSH remote in the
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
    r'["\']?'
    r'(.+?)'
    r'["\']?'
    r'(?:[,;\s]|$)',
    re.MULTILINE,
)

_MNEMONIC_PATTERN = re.compile(
    r'(?i)(mnemonic|seed[_-]?phrase|recovery[_-]?phrase)\s*[=:"\']*\s*'
    r'([a-z]+(?:\s+[a-z]+){11,})',
)

_HEX_KEY_PATTERN = re.compile(r'(?<![a-zA-Z0-9])[0-9a-fA-F]{64,}(?![a-zA-Z0-9])')


# --- Literal .env secret stripping -------------------------------------------
#
# The patterns above recognise secret *shapes*, and an audit showed the two
# that matter most to this bot have no recognisable shape: a bare Discord bot
# token and a Discord webhook URL both survived every pattern here, bare and in
# `NAME=value` form alike. A session that prints `env` while debugging, or a
# traceback that carries the environment, would publish them into a thread
# verbatim.
#
# So match on the *value* instead of the shape: pull the real secrets out of
# config once and replace them literally. That has no false positives by
# construction — it can only ever match the actual secret — and it needs no new
# regex whenever a credential format changes. This is the pattern log_triage
# has used all along; it just lived in one module instead of the shared path.
_ENV_SECRET_NAMES = (
    "DISCORD_BOT_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "TWITTER_BEARER_TOKEN", "TEST_WEBHOOK_URL", "TEST_LOBBY_WEBHOOK_URL",
)

# Below this length a "secret" is more likely a placeholder than a credential,
# and blanket-replacing a 3-character string would gut unrelated text.
_ENV_SECRET_MIN_LEN = 8

_env_secrets_cache: tuple[str, ...] | None = None


def _env_secret_values() -> tuple[str, ...]:
    """The literal secret strings to strip, collected once.

    redact_secrets sits on the display hot path, so this must not re-read
    config per call. A failure here degrades to regex-only redaction rather
    than taking down every message the bot renders.
    """
    global _env_secrets_cache
    if _env_secrets_cache is not None:
        return _env_secrets_cache
    values: list[str] = []
    try:
        for name in _ENV_SECRET_NAMES:
            v = getattr(config, name, None) or os.getenv(name, "")
            if v and isinstance(v, str) and len(v) >= _ENV_SECRET_MIN_LEN:
                values.append(v)
    except Exception:  # pragma: no cover - defensive
        values = []
    # Longest first: a webhook URL contains its own id/token substrings, and
    # replacing the whole thing before its parts keeps the output readable.
    values.sort(key=len, reverse=True)
    _env_secrets_cache = tuple(dict.fromkeys(values))
    return _env_secrets_cache


def redact_secrets(text: str) -> str:
    """Scrub API keys, tokens, and secrets from text."""
    # Literal pass FIRST, while every known secret is still verbatim. Running
    # it last would leave a gap: a pattern below can chew a fragment out of a
    # secret (``_HEX_KEY_PATTERN`` on a long hex run inside it, say) without
    # removing the whole thing, and the surviving remainder would then no
    # longer match the literal we are looking for.
    for secret in _env_secret_values():
        if secret in text:
            text = text.replace(secret, '[REDACTED]')
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    text = _CONN_STRING_PATTERN.sub(r'\1[REDACTED]\3', text)
    text = _URL_USERINFO_PATTERN.sub(r'\1[REDACTED]\3', text)
    text = _MNEMONIC_PATTERN.sub(lambda m: m.group(1) + '=[REDACTED]', text)
    text = _KV_PATTERN.sub(lambda m: f'{m.group(1)}=[REDACTED] ', text)
    text = _HEX_KEY_PATTERN.sub('[REDACTED]', text)
    return text
