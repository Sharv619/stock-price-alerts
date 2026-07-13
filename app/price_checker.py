"""Fetch prices via yfinance and evaluate alert conditions."""

import logging
import os
from datetime import datetime, timedelta

import yfinance as yf
from dotenv import load_dotenv

from app.database import get_active_alerts, mark_notified
from app.notifier import format_message, send_email, send_whatsapp

load_dotenv()

logger = logging.getLogger(__name__)

# Re-notify at most once per cooldown while the condition still holds.
NOTIFY_COOLDOWN_MINUTES = int(os.getenv("NOTIFY_COOLDOWN_MINUTES", "60"))

# Written by the scheduler loop, read by the API for the dashboard sidebar.
last_check = {"time": None, "checked": 0}


def get_current_price(symbol):
    """Return last traded price for symbol, or None on failure."""
    try:
        ticker = yf.Ticker(symbol)
        price = ticker.fast_info["last_price"]
        if price is None:
            logger.warning("No price data for %s", symbol)
            return None
        return float(price)
    except Exception as e:
        logger.warning("Price fetch failed for %s: %s", symbol, e)
        return None


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

        if sent:
            mark_notified(alert["id"])
        else:
            logger.warning(
                "Alert %d: no notification delivered, will retry next cycle",
                alert["id"],
            )

    last_check["time"] = datetime.now().isoformat()
    last_check["checked"] = len(alerts)
