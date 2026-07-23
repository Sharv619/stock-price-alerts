"""Kite Connect authentication — OAuth login and token management.

Prices for NSE/BSE tickers are fetched via Kite; this module manages the
daily access token. The token is validated once (a live ``kite.profile()``
call) when the client is first built, not on every price fetch.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from kiteconnect import KiteConnect

load_dotenv()

logger = logging.getLogger(__name__)

API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")

# Absolute so the uvicorn server and any CLI (kite_login.py) share one file.
TOKEN_FILE = Path(__file__).resolve().parent.parent / ".kite_token"

_client = None


def get_kite_client():
    """Return an authenticated KiteConnect client, or None.

    Returns the cached client if present. Otherwise, with no API key or no
    saved token, returns None. Builds a client from the saved token and
    validates it with a single live ``profile()`` call — on success caches
    and returns it, on failure logs and returns None.
    """
    global _client
    if _client is not None:
        return _client

    if not API_KEY or not TOKEN_FILE.exists():
        return None

    try:
        kite = KiteConnect(api_key=API_KEY)
        kite.set_access_token(TOKEN_FILE.read_text().strip())
        kite.profile()  # live validation — only here, never per price fetch
        _client = kite
        logger.info("Kite client authenticated")
        return _client
    except Exception as e:
        logger.warning("Kite token invalid or expired: %s", e)
        return None


def refresh_client():
    """Drop the cached client so the next get_kite_client() re-validates."""
    global _client
    _client = None


def get_login_url() -> str:
    """Return the Kite OAuth login URL."""
    if not API_KEY:
        raise RuntimeError("KITE_API_KEY not set")
    return KiteConnect(api_key=API_KEY).login_url()


def login(request_token: str) -> bool:
    """Exchange request_token for an access token and persist it."""
    if not API_KEY or not API_SECRET:
        raise RuntimeError("KITE_API_KEY and KITE_API_SECRET must be set")
    kite = KiteConnect(api_key=API_KEY)
    data = kite.generate_session(request_token, api_secret=API_SECRET)
    TOKEN_FILE.write_text(data["access_token"])
    refresh_client()
    return True


def kite_status() -> dict:
    """Small dict for the /health endpoint and dashboard banner."""
    return {
        "authenticated": get_kite_client() is not None,
        "api_key_set": bool(API_KEY),
    }
