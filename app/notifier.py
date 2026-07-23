"""Send alert notifications via Whapi (WhatsApp) and Gmail SMTP."""

import json
import logging
import os
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

WHAPI_TOKEN = os.getenv("WHAPI_TOKEN")
WHAPI_URL = os.getenv("WHAPI_URL", "https://gate.whapi.cloud/")
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")


def format_message(ticker, current_price, target_price, condition):
    return f"""
🚨 STOCK ALERT 🚨

{ticker} has hit your target!

Current Price: ₹{current_price:,.2f}
Your Target:   ₹{target_price:,.2f}
Condition:     Price went {condition} target

Time: {datetime.now().strftime('%d %b %Y %H:%M')}
"""


def send_whatsapp(phone, message):
    """Send WhatsApp message via Whapi. Returns True on success."""
    if not WHAPI_TOKEN:
        logger.warning("Whapi credentials missing — WhatsApp not sent")
        return False
    try:
        payload = {
            "to": phone.lstrip("+"),
            "body": message,
            "type": "text",
        }
        headers = {
            "Authorization": f"Bearer {WHAPI_TOKEN}",
            "Content-Type": "application/json",
        }
        r = httpx.post(
            f"{WHAPI_URL.rstrip('/')}/messages/text",
            json=payload,
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("sent"):
            logger.info("WhatsApp sent to %s", phone)
            return True
        logger.warning("Whapi returned not-sent for %s: %s", phone, data)
        return False
    except Exception as e:
        logger.error("WhatsApp send failed for %s: %s", phone, e)
        return False


def send_email(to_email, subject, body):
    """Send email via Gmail SMTP. Returns True on success."""
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        logger.warning("Gmail credentials missing — email not sent")
        return False
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = to_email

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        logger.info("Email sent to %s", to_email)
        return True
    except Exception as e:
        logger.error("Email send failed for %s: %s", to_email, e)
        return False
