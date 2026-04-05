"""Twitter/X URL detection and tweet fetching via Twitter API v2."""

from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

from bot import config

log = logging.getLogger(__name__)

_TWEET_URL_RE = re.compile(
    r"(?:https?://)?(?:twitter\.com|x\.com)/(?:\w+/)?status/(\d+)"
)

_API_BASE = "https://api.twitter.com/2"
_TWEET_FIELDS = "text,author_id,created_at"
_USER_FIELDS = "username,name"


def extract_tweet_ids(text: str) -> list[str]:
    """Return all tweet IDs found in text."""
    return _TWEET_URL_RE.findall(text)


async def fetch_tweet(tweet_id: str, timeout: float = 10.0) -> str | None:
    """Fetch tweet content via Twitter API v2, return formatted string or None."""
    token = config.TWITTER_BEARER_TOKEN
    if not token:
        return None
    url = (
        f"{_API_BASE}/tweets/{tweet_id}"
        f"?tweet.fields={_TWEET_FIELDS}"
        f"&expansions=author_id&user.fields={_USER_FIELDS}"
    )
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with aiohttp.ClientSession() as session:
            for attempt in range(2):
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status in (429, 503) and attempt == 0:
                        log.info("Twitter %d for tweet %s, retrying in 1s", resp.status, tweet_id)
                        await asyncio.sleep(1)
                        continue
                    if resp.status != 200:
                        log.warning("Twitter API %d for tweet %s", resp.status, tweet_id)
                        return None
                    data = await resp.json()
                break  # success, exit retry loop

        tweet = data.get("data")
        if not tweet:
            return None

        # Resolve author from includes
        handle = "unknown"
        name = ""
        includes = data.get("includes", {})
        users = includes.get("users", [])
        if users:
            handle = users[0].get("username", "unknown")
            name = users[0].get("name", "")

        text = tweet.get("text", "")
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
