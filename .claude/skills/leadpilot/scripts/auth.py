"""
ZoomInfo PKI authentication module.

Wraps ZoomInfo's official `zi_api_auth_client.pki_authentication()` helper.
Tokens are cached in `.token_cache.json` and auto-refreshed 5 minutes before
expiry (the JWT ZoomInfo returns is valid for 1 hour).

Usage:
    python -m scripts.auth --check
    python -m scripts.auth --token        # prints current token

Required env vars (loaded from .env):
    ZOOMINFO_USERNAME
    ZOOMINFO_CLIENT_ID
    ZOOMINFO_PRIVATE_KEY_PATH    (path to .pem file, e.g. ./zoominfo_private_key.pem)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# Find .env walking up from CWD (works whether you run from repo root or skill folder).
# Resolve all relative paths in .env against the directory where .env was found.
_DOTENV_PATH = find_dotenv(usecwd=True)
load_dotenv(_DOTENV_PATH)
REPO_ROOT = Path(_DOTENV_PATH).parent if _DOTENV_PATH else Path.cwd()

TOKEN_CACHE_PATH = REPO_ROOT / ".token_cache.json"
SAFETY_WINDOW_SECONDS = 5 * 60  # refresh 5 min before expiry


def _b64url_decode(s: str) -> bytes:
    """Decode a base64url string, padding as needed."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _jwt_exp(token: str) -> int | None:
    """Extract the `exp` claim (unix timestamp) from a JWT, or None if unparseable."""
    try:
        _, payload_b64, _ = token.split(".")
        payload = json.loads(_b64url_decode(payload_b64))
        return int(payload.get("exp")) if payload.get("exp") else None
    except Exception:
        return None


def _load_cached_token() -> str | None:
    """Return cached token if still valid (>5 min remaining), else None."""
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
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


def _read_private_key() -> str:
    """Read the private key .pem file contents as a multi-line string."""
    path = os.getenv("ZOOMINFO_PRIVATE_KEY_PATH", "")
    if not path:
        raise RuntimeError("ZOOMINFO_PRIVATE_KEY_PATH not set in .env")
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    if not p.exists():
        raise RuntimeError(f"private key file not found at: {p}")
    return p.read_text()


def get_access_token(force_refresh: bool = False) -> str:
    """
    Return a valid ZoomInfo OAuth2 access token, refreshing if needed.

    Calls zi_api_auth_client.pki_authentication() to mint a fresh JWT, parses
    its `exp` claim, and caches under .token_cache.json. Subsequent calls reuse
    the cached token until 5 minutes before it expires.

    Args:
        force_refresh: skip cache and fetch a new token.

    Returns:
        access_token string.

    Raises:
        RuntimeError: if env vars missing, key file missing, or auth fails.
    """
    if not force_refresh:
        cached = _load_cached_token()
        if cached:
            return cached

    missing = _check_env()
    if missing:
        raise RuntimeError(f"missing env vars: {', '.join(missing)}")

    try:
        from zi_api_auth_client import pki_authentication
    except ImportError as e:
        raise RuntimeError(
            "zi_api_auth_client not installed. Run: "
            "pip install -r .claude/skills/leadpilot/requirements.txt"
        ) from e

    username = os.environ["ZOOMINFO_USERNAME"]
    client_id = os.environ["ZOOMINFO_CLIENT_ID"]
    private_key = _read_private_key()

    token = pki_authentication(username, client_id, private_key)
    if not token or not isinstance(token, str):
        raise RuntimeError(f"PKI auth returned unexpected value: {type(token).__name__}")

    # Pull expiry from the JWT itself; fall back to a conservative 55-min window.
    expires_at = _jwt_exp(token) or int(time.time()) + 55 * 60
    _save_token(token, expires_at)
    return token


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
            exp = _jwt_exp(token)
            human_exp = (
                time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(exp)) if exp else "unknown"
            )
            print(f"OK: ZoomInfo auth working.")
            print(f"  token length: {len(token)}")
            print(f"  expires at:   {human_exp}")
            print(f"  cached at:    {TOKEN_CACHE_PATH}")
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
