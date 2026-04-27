"""
ZoomInfo PKI authentication module.

Handles OAuth2 token exchange via ZoomInfo's official auth helper library.
Tokens are cached in `.token_cache.json` and auto-refreshed 5 minutes before expiry.

Usage:
    python -m scripts.auth --check
    python -m scripts.auth --token        # prints current token

Required env vars (loaded from .env):
    ZOOMINFO_USERNAME
    ZOOMINFO_CLIENT_ID
    ZOOMINFO_PRIVATE_KEY_PATH    (path to .pem file, e.g. ./zoominfo_private_key.pem)

Phase 1 status: STUB. Real implementation pending.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TOKEN_CACHE_PATH = Path(".token_cache.json")
SAFETY_WINDOW_SECONDS = 5 * 60  # refresh 5 min before expiry


def get_access_token(force_refresh: bool = False) -> str:
    """
    Return a valid ZoomInfo OAuth2 access token, refreshing if needed.

    Reads/writes a token cache at .token_cache.json:
        {"token": "...", "expires_at": <unix_timestamp>}

    Args:
        force_refresh: skip cache and fetch a new token.

    Returns:
        access_token string.

    Raises:
        RuntimeError: if env vars missing or auth fails.
    """
    raise NotImplementedError("auth.get_access_token: implement using ZoomInfo PKI helper")


def _load_cached_token() -> str | None:
    """Return cached token if still valid (>5 min remaining), else None."""
    if not TOKEN_CACHE_PATH.exists():
        return None
    data = json.loads(TOKEN_CACHE_PATH.read_text())
    if data.get("expires_at", 0) - time.time() > SAFETY_WINDOW_SECONDS:
        return data.get("token")
    return None


def _save_token(token: str, expires_at: int) -> None:
    """Persist token + expiry to cache file."""
    TOKEN_CACHE_PATH.write_text(json.dumps({"token": token, "expires_at": expires_at}))


def _check_env() -> list[str]:
    """Return list of missing required env vars."""
    required = ["ZOOMINFO_USERNAME", "ZOOMINFO_CLIENT_ID", "ZOOMINFO_PRIVATE_KEY_PATH"]
    return [v for v in required if not os.getenv(v)]


def main() -> int:
    p = argparse.ArgumentParser(description="ZoomInfo auth helper")
    p.add_argument("--check", action="store_true", help="Verify env + auth, print status")
    p.add_argument("--token", action="store_true", help="Print current access token")
    p.add_argument("--force-refresh", action="store_true", help="Bypass cache")
    args = p.parse_args()

    missing = _check_env()
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
        print("Set them in .env at the repo root.", file=sys.stderr)
        return 1

    if args.check:
        try:
            token = get_access_token(force_refresh=args.force_refresh)
            print(f"OK: ZoomInfo auth working. Token length: {len(token)}")
            return 0
        except NotImplementedError:
            print("STUB: auth not yet implemented")
            return 0
        except Exception as e:
            print(f"FAIL: {e}", file=sys.stderr)
            return 1

    if args.token:
        print(get_access_token(force_refresh=args.force_refresh))
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
