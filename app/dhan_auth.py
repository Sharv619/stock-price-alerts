"""DhanHQ TOTP authentication and local 24-hour token storage."""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pyotp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)
TOKEN_FILE = Path(__file__).resolve().parent.parent / ".dhan_token"
AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"
_lock = threading.RLock()


def _config():
    return (os.getenv("DHAN_CLIENT_ID"), os.getenv("DHAN_PIN"), os.getenv("DHAN_TOTP_SECRET"))


def _parse_expiry(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _read_saved():
    try:
        data = json.loads(TOKEN_FILE.read_text())
        token, expiry = data.get("access_token"), _parse_expiry(data.get("expiry"))
        if token and expiry and expiry > datetime.now(timezone.utc):
            return token, expiry
    except (OSError, ValueError, TypeError):
        pass
    return None, None


def _save(token, expiry):
    TOKEN_FILE.write_text(json.dumps({"access_token": token, "expiry": expiry.isoformat()}))
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        logger.warning("Could not restrict permissions on %s", TOKEN_FILE)


def refresh_token():
    """Generate and persist a fresh token using Dhan's documented TOTP flow."""
    client_id, pin, secret = _config()
    missing = [name for name, value in (("DHAN_CLIENT_ID", client_id), ("DHAN_PIN", pin), ("DHAN_TOTP_SECRET", secret)) if not value]
    if missing:
        raise RuntimeError(f"missing env vars: {', '.join(missing)}")
    response = httpx.post(
        AUTH_URL,
        params={"dhanClientId": client_id, "pin": pin, "totp": pyotp.TOTP(secret).now()},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    token = data.get("accessToken")
    expiry = _parse_expiry(data.get("expiryTime"))
    if not token or not expiry:
        raise RuntimeError(f"Dhan authentication returned no usable token: {data}")
    _save(token, expiry)
    return token


def get_access_token(force_refresh=False):
    """Return a valid token, refreshing lazily and serializing refreshes."""
    with _lock:
        if not force_refresh:
            token, _ = _read_saved()
            if token:
                return token
        return refresh_token()


def dhan_status():
    """Return local auth state without reading the network or refreshing."""
    client_id, pin, secret = _config()
    token, expiry = _read_saved()
    return {
        "configured": all((client_id, pin, secret)),
        "authenticated": bool(token and all((client_id, pin, secret))),
        "token_expiry": expiry.isoformat() if expiry else None,
    }


def scheduled_token_refresh():
    if not all(_config()):
        return
    try:
        with _lock:
            refresh_token()
    except Exception as exc:
        logger.error("Scheduled Dhan token refresh failed: %s", exc)


# Friendly aliases for callers and tests.
get_token = get_access_token
refresh = refresh_token
status = dhan_status
