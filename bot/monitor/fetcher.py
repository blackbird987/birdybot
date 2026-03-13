"""Async HTTP client for fetching monitor endpoints."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 15.0  # seconds per request

# AIAgent only needs the summary endpoint — it contains everything
AIAGENT_ENDPOINTS = {
    "summary": "/api/admin/monitor/summary",
}


def _parse_auth(auth_str: str) -> httpx.BasicAuth | dict[str, str] | None:
    """Parse auth string: 'basic:user:pass' or 'apikey:the-key'."""
    if not auth_str:
        return None
    parts = auth_str.split(":", 2)
    if parts[0] == "basic" and len(parts) == 3:
        return httpx.BasicAuth(parts[1], parts[2])
    if parts[0] == "apikey" and len(parts) >= 2:
        return {"X-API-Key": ":".join(parts[1:])}
    log.warning("Unknown auth format: %s", parts[0])
    return None


async def fetch_all(
    base_url: str,
    auth_str: str,
    endpoints: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fetch multiple endpoints in parallel. Returns {name: json_or_None}."""
    if endpoints is None:
        endpoints = AIAGENT_ENDPOINTS

    auth = _parse_auth(auth_str)
    headers = {}
    basic_auth = None
    if isinstance(auth, httpx.BasicAuth):
        basic_auth = auth
    elif isinstance(auth, dict):
        headers = auth

    results: dict[str, Any] = {}

    async with httpx.AsyncClient(
        base_url=base_url,
        auth=basic_auth,
        headers=headers,
        timeout=TIMEOUT,
        verify=True,
    ) as client:
        async def _fetch(name: str, path: str) -> tuple[str, Any]:
            try:
                resp = await client.get(path)
                if resp.status_code in (401, 403):
                    log.warning("Auth failed for %s%s: %s", base_url, path, resp.status_code)
                    return name, {"_error": "auth_failed", "_status": resp.status_code}
                resp.raise_for_status()
                return name, resp.json()
            except httpx.TimeoutException:
                log.warning("Timeout fetching %s%s", base_url, path)
                return name, None
            except Exception:
                log.warning("Failed to fetch %s%s", base_url, path, exc_info=True)
                return name, None

        tasks = [_fetch(name, path) for name, path in endpoints.items()]
        for name, data in await asyncio.gather(*tasks):
            results[name] = data

    return results
