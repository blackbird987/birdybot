"""Twitter/X URL detection and tweet fetching via DegenAI API."""

from __future__ import annotations

import asyncio
import base64
import logging
import re

import aiohttp

from bot import config

log = logging.getLogger(__name__)

_TWEET_URL_RE = re.compile(
    r"(?:https?://)?(?:twitter\.com|x\.com)/(?:\w+/)?status/(\d+)"
)

_API_BASE = "https://degenai.dev/api/twitter/admin/tweet"


def _auth_header() -> str | None:
    """Build Basic auth header from config, or None if not configured."""
    user = config.TWITTER_API_USER
    pw = config.TWITTER_API_PASS
    if not user or not pw:
        return None
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def extract_tweet_ids(text: str) -> list[str]:
    """Return all tweet IDs found in text."""
    return _TWEET_URL_RE.findall(text)


async def fetch_tweet(tweet_id: str, timeout: float = 5.0) -> str | None:
    """Fetch tweet content, return formatted string or None on failure."""
    auth = _auth_header()
    if not auth:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{_API_BASE}/{tweet_id}",
                headers={"Authorization": auth},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    log.warning("Tweet fetch %s returned %d", tweet_id, resp.status)
                    return None
                data = await resp.json()
                handle = data.get("authorHandle", "unknown")
                name = data.get("authorName", "")
                text = data.get("text", "")
                if not text:
                    return None
                return f'[Tweet by @{handle} ({name}): "{text}"]'
    except Exception:
        log.warning("Tweet fetch failed for %s", tweet_id, exc_info=True)
        return None


async def enrich_with_tweets(text: str) -> str:
    """Find tweet URLs in text, fetch content, prepend to message."""
    ids = extract_tweet_ids(text)
    if not ids:
        return text

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_ids: list[str] = []
    for tid in ids:
        if tid not in seen:
            seen.add(tid)
            unique_ids.append(tid)

    results = await asyncio.gather(
        *(fetch_tweet(tid) for tid in unique_ids),
        return_exceptions=True,
    )

    enrichments = [r for r in results if isinstance(r, str)]

    if not enrichments:
        return text

    log.info("Enriched message with %d tweet(s): %s",
             len(enrichments), ", ".join(unique_ids[:5]))

    prefix = "\n".join(enrichments)
    return f"{prefix}\n\n{text}"
