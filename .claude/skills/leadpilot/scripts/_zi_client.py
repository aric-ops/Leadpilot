"""
Shared HTTP client for ZoomInfo's Enterprise API.

Why this exists:
  1. Cloudflare in front of api.zoominfo.com blocks default urllib User-Agents.
     Every request needs a non-default UA.
  2. Every module needs the same Bearer token auth, same base URL, same JSON
     handling — DRY it up here so callers stay readable.
  3. Tokens expire after 1 hour. On 401, refresh once and retry transparently.

Public API:
    zi_get(path, params=None)   -> dict
    zi_post(path, body=None)    -> dict

Both raise ZoomInfoError with the response body on any non-2xx after a single
auth-refresh retry.
"""

from __future__ import annotations

import time
from typing import Any

import requests

from .auth import get_access_token

BASE_URL = "https://api.zoominfo.com"
USER_AGENT = "LeadPilot/0.1 (+aric@harbingermarketing.com)"
DEFAULT_TIMEOUT = 30  # seconds


class ZoomInfoError(RuntimeError):
    """Non-2xx response from ZoomInfo (after auto-refresh retry)."""

    def __init__(self, status: int, url: str, body: str):
        super().__init__(f"ZoomInfo {status} on {url}: {body[:500]}")
        self.status = status
        self.url = url
        self.body = body


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def _request(method: str, path: str, *, params: dict | None = None,
             json_body: Any = None, _retried: bool = False) -> dict:
    url = path if path.startswith("http") else f"{BASE_URL}{path}"
    token = get_access_token()
    r = requests.request(
        method,
        url,
        headers=_headers(token),
        params=params,
        json=json_body,
        timeout=DEFAULT_TIMEOUT,
    )

    # If the cached token was rejected, force-refresh once and retry.
    if r.status_code == 401 and not _retried:
        get_access_token(force_refresh=True)
        return _request(method, path, params=params, json_body=json_body, _retried=True)

    if not r.ok:
        raise ZoomInfoError(r.status_code, url, r.text)

    if not r.content:
        return {}
    try:
        return r.json()
    except ValueError as e:
        raise ZoomInfoError(r.status_code, url, f"non-JSON body: {r.text[:200]}") from e


def zi_get(path: str, params: dict | None = None) -> dict:
    return _request("GET", path, params=params)


def zi_post(path: str, body: Any = None) -> dict:
    return _request("POST", path, json_body=body)


# ---- Naive rate-limiting helper ------------------------------------------- #

class RateLimiter:
    """Simple per-second token bucket. Default 25 req/sec matches ZoomInfo's
    default tier ceiling. Bump to 30 or 35 if your account has the add-on."""

    def __init__(self, rate_per_second: int = 20):
        self.min_interval = 1.0 / rate_per_second
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        delta = now - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()
