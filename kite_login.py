"""Log in to Kite Connect and save the access token.

Usage:
    python kite_login.py
"""

import logging
import sys

from app.kite_auth import get_login_url, login

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

if __name__ == "__main__":
    print("\n=== Kite Connect Login ===\n")
    print("1. Open this URL in your browser:")
    print(f"\n   {get_login_url()}\n")
    print("2. Log in with your Zerodha credentials (2FA required).")
    print("3. Authorize the app if prompted.")
    print("4. You'll be redirected to a URL that looks like:")
    print("   http://127.0.0.1/?request_token=abc123...")
    print("5. Copy the request_token value from that URL.\n")

    raw = input("Paste the request_token here: ").strip()

    if not raw:
        print("No token provided. Exiting.")
        sys.exit(1)

    token = raw.split("request_token=")[-1].split("&")[0].strip()

    if not token or len(token) < 10:
        print(f"Invalid token: {token[:20]}... Exiting.")
        sys.exit(1)

    try:
        login(token)
        print("\n✓ Authentication successful! Token saved to .kite_token")
        print("  The server will pick it up on the next scheduler cycle.")
    except Exception as e:
        print(f"\n✗ Login failed: {e}")
        sys.exit(1)
