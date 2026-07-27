"""Fetch prices (Kite for NSE/BSE, yfinance for everything else) and
evaluate alert conditions."""

import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf
from dotenv import load_dotenv

from app.database import get_active_alerts, mark_notified
from app.kite_auth import get_kite_client, refresh_client
from app.notifier import format_message, send_email, send_whatsapp

load_dotenv()

logger = logging.getLogger(__name__)

# Re-notify at most once per cooldown while the condition still holds.
NOTIFY_COOLDOWN_MINUTES = int(os.getenv("NOTIFY_COOLDOWN_MINUTES", "0"))

# Written by the scheduler loop, read by the API for the dashboard sidebar.
last_check = {"time": None, "checked": 0}


def _to_kite_symbol(symbol: str) -> str:
    """Convert yfinance-style symbol to Kite format.

    "TCS.NS"  -> "NSE:TCS"
    "TCS.BO"  -> "BSE:TCS"
    "RELIANCE" -> "NSE:RELIANCE"
    """
    s = symbol.upper().strip()
    if s.endswith(".NS"):
        return f"NSE:{s[:-3]}"
    if s.endswith(".BO"):
        return f"BSE:{s[:-3]}"
    return f"NSE:{s}"


def _is_indian(symbol: str) -> bool:
    """NSE/BSE tickers route to Kite; everything else (US, crypto) to yfinance."""
    s = symbol.upper().strip()
    return s.endswith(".NS") or s.endswith(".BO")


def _get_kite_price(symbol):
    """Return last traded price via Kite Connect, or None on failure."""
    kite = get_kite_client()
    if kite is None:
        return None
    try:
        ks = _to_kite_symbol(symbol)
        data = kite.ltp([ks])
        if ks not in data:
            logger.warning("Symbol %s not found in Kite response", symbol)
            return None
        return float(data[ks]["last_price"])
    except Exception as e:
        logger.warning("Kite price fetch failed for %s: %s", symbol, e)
        refresh_client()
        return None


def _get_yfinance_price(symbol):
    """Return last price via yfinance, or None on failure."""
    try:
        info = yf.Ticker(symbol.strip()).fast_info
        price = info.last_price
        if price is None:
            logger.warning("yfinance returned no price for %s", symbol)
            return None
        return float(price)
    except Exception as e:
        logger.warning("yfinance price fetch failed for %s: %s", symbol, e)
        return None


def get_current_price(symbol):
    """Return last traded price. Kite for NSE/BSE, yfinance otherwise;
    falls back to yfinance if Kite fails (e.g. no token yet)."""
    if _is_indian(symbol):
        price = _get_kite_price(symbol)
        if price is not None:
            return price
        return _get_yfinance_price(symbol)  # fallback while Kite unauthed
    return _get_yfinance_price(symbol)


def is_market_open(symbol, now=None):
    """True if the exchange for `symbol` is open. NSE/BSE for .NS/.BO
    (09:15-15:30 IST Mon-Fri), US markets otherwise (09:30-16:00 ET Mon-Fri).
    Public holidays are not accounted for."""
    if _is_indian(symbol):
        tz, open_t, close_t = ZoneInfo("Asia/Kolkata"), (9, 15), (15, 30)
    else:
        tz, open_t, close_t = ZoneInfo("America/New_York"), (9, 30), (16, 0)
    t = (now or datetime.now(tz)).astimezone(tz)
    if t.weekday() >= 5:  # Sat/Sun
        return False
    mins = t.hour * 60 + t.minute
    return open_t[0] * 60 + open_t[1] <= mins <= close_t[0] * 60 + close_t[1]


def check_condition(current, target, condition):
    if condition == "above":
        return current >= target
    if condition == "below":
        return current <= target
    return False


def in_cooldown(alert):
    if not alert["last_notified"]:
        return False
    last = datetime.fromisoformat(alert["last_notified"])
    return datetime.now() - last < timedelta(minutes=NOTIFY_COOLDOWN_MINUTES)


def check_all_alerts():
    """Check every alert; notify matches that are out of cooldown."""
    alerts = get_active_alerts()
    logger.info("Checking %d alert(s)", len(alerts))

    # Skip alerts whose market is closed — don't fetch or notify.
    alerts = [a for a in alerts if is_market_open(a["ticker"])]
    if not alerts:
        logger.info("All markets closed — nothing to check")
        last_check["time"] = datetime.now().isoformat()
        last_check["checked"] = 0
        return

    # Fetch each unique ticker once per cycle.
    prices = {}
    for alert in alerts:
        symbol = alert["ticker"]
        if symbol not in prices:
            prices[symbol] = get_current_price(symbol)

    for alert in alerts:
        current = prices.get(alert["ticker"])
        if current is None:
            continue  # fetch failed — skip, retry next cycle

        if not check_condition(current, alert["target_price"], alert["condition"]):
            continue

        if in_cooldown(alert):
            logger.debug("Alert %d in cooldown, skipping", alert["id"])
            continue

        logger.info(
            "Alert %d triggered: %s at %.2f (%s %.2f)",
            alert["id"], alert["ticker"], current,
            alert["condition"], alert["target_price"],
        )
        message = format_message(
            alert["ticker"], current, alert["target_price"], alert["condition"]
        )

        sent = False
        if alert["whatsapp_on"] and alert["phone"]:
            sent = send_whatsapp(alert["phone"], message) or sent
        if alert["email_on"] and alert["email"]:
            subject = f"🚨 Stock Alert: {alert['ticker']} hit your target"
            sent = send_email(alert["email"], subject, message) or sent

        # Stamp the attempt either way so cooldown gates the next try.
        # Without this, a failing send (bad creds, quota) retries every
        # cycle and hammers the provider.
        mark_notified(alert["id"])
        if not sent:
            logger.warning(
                "Alert %d: no notification delivered, retry after cooldown (%dm)",
                alert["id"], NOTIFY_COOLDOWN_MINUTES,
            )

    last_check["time"] = datetime.now().isoformat()
    last_check["checked"] = len(alerts)
