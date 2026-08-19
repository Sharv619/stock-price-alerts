"""Fetch normalized prices and evaluate alert conditions."""

import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from app import market_feed
from app.database import get_active_alerts, mark_notified
from app.notifier import format_message, send_email, send_whatsapp

load_dotenv()
logger = logging.getLogger(__name__)
NOTIFY_COOLDOWN_MINUTES = int(os.getenv("NOTIFY_COOLDOWN_MINUTES", "0"))
last_check = {"time": None, "checked": 0}


def _is_crypto(symbol):
    return symbol.upper().strip().endswith("-USD")


def _is_indian(symbol):
    s = symbol.upper().strip()
    return s.endswith((".NS", ".BO")) or s in ("^NSEI", "^BSESN")


def is_market_open(symbol, now=None):
    """Check exchange hours; crypto is open continuously."""
    if _is_crypto(symbol):
        return True
    if _is_indian(symbol):
        tz, opening, closing = ZoneInfo("Asia/Kolkata"), (9, 15), (15, 30)
    else:
        tz, opening, closing = ZoneInfo("America/New_York"), (9, 30), (16, 0)
    t = (now or datetime.now(tz)).astimezone(tz)
    if t.weekday() >= 5:
        return False
    minute = t.hour * 60 + t.minute
    return opening[0] * 60 + opening[1] <= minute <= closing[0] * 60 + closing[1]


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


def get_current_price(symbol):
    return market_feed.get_price(symbol)


def check_all_alerts():
    alerts = [a for a in get_active_alerts() if is_market_open(a["ticker"])]
    logger.info("Checking %d alert(s)", len(alerts))
    if not alerts:
        last_check.update(time=datetime.now().isoformat(), checked=0)
        return
    prices = market_feed.get_prices(dict.fromkeys(a["ticker"] for a in alerts))
    for alert in alerts:
        current = prices.get(alert["ticker"].strip().upper())
        if current is None or not check_condition(current, alert["target_price"], alert["condition"]):
            continue
        if in_cooldown(alert):
            continue
        message = format_message(alert["ticker"], current, alert["target_price"], alert["condition"])
        sent = False
        if alert["whatsapp_on"] and alert["phone"]:
            sent = send_whatsapp(alert["phone"], message) or sent
        if alert["email_on"] and alert["email"]:
            sent = send_email(alert["email"], f"🚨 Stock Alert: {alert['ticker']} hit your target", message) or sent
        mark_notified(alert["id"])
        if not sent:
            logger.warning("Alert %d: no notification delivered", alert["id"])
    last_check.update(time=datetime.now().isoformat(), checked=len(alerts))
