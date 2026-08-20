"""Streamlit dashboard for stock price alerts."""

import os
import re
from datetime import datetime

import httpx
import streamlit as st

API_URL = os.getenv(
    "API_URL", "https://stock-price-alerts-api.onrender.com"
).rstrip("/")

st.set_page_config(page_title="Stock Price Alerts", page_icon="📈", layout="wide")
page = st.sidebar.radio("Workspace", ("Stock Alerts", "StraddleLab"))
if page == "StraddleLab":
    from app.straddle.ui import render_straddlelab

    render_straddlelab(API_URL)
    st.stop()

st.title("📈 Stock Price Alerts")


def api():
    return httpx.Client(base_url=API_URL, timeout=30)


@st.cache_data(ttl=30)
def fetch_price(ticker: str):
    try:
        r = api().get(f"/price/{ticker}")
        if r.status_code == 200:
            return r.json()["price"]
    except httpx.HTTPError:
        pass
    return None


@st.cache_data(ttl=30)
def fetch_alerts():
    try:
        r = api().get("/alerts")
        if r.status_code == 200:
            return r.json()
    except httpx.HTTPError:
        pass
    return None


def fetch_health():
    try:
        r = api().get("/health")
        if r.status_code == 200:
            return r.json()
    except httpx.HTTPError:
        pass
    return None


# ── Section 1: Add new alert ────────────────────────────────────
st.header("Add New Alert")

POPULAR_TICKERS = {
    "RELIANCE.NS — Reliance Industries": "RELIANCE.NS",
    "TCS.NS — Tata Consultancy": "TCS.NS",
    "INFY.NS — Infosys": "INFY.NS",
    "HDFCBANK.NS — HDFC Bank": "HDFCBANK.NS",
    "^NSEI — Nifty 50": "^NSEI",
    "^BSESN — Sensex": "^BSESN",
    "AAPL — Apple": "AAPL",
    "TSLA — Tesla": "TSLA",
    "NVDA — Nvidia": "NVDA",
    "MSFT — Microsoft": "MSFT",
    "BTC-USD — Bitcoin": "BTC-USD",
    "ETH-USD — Ethereum": "ETH-USD",
    "Custom…": None,
}

choice = st.selectbox("Stock Ticker", list(POPULAR_TICKERS.keys()))
if POPULAR_TICKERS[choice] is None:
    ticker = st.text_input(
        "Custom ticker", placeholder="e.g. WIPRO.NS, AMZN, DOGE-USD"
    )
else:
    ticker = POPULAR_TICKERS[choice]

current_price = None
if ticker.strip():
    current_price = fetch_price(ticker.strip().upper())
    if current_price is not None:
        st.info(f"Current price: ₹{current_price:,.2f}")
    else:
        st.warning("No price data — check the ticker symbol.")

# Prefill target with the live price; per-ticker key resets it on change.
target = st.number_input(
    "Target Price",
    min_value=0.0,
    step=1.0,
    format="%.2f",
    value=float(current_price) if current_price is not None else 0.0,
    key=f"target_{ticker.strip().upper() or 'none'}",
)
condition = st.selectbox("Alert when price goes", ["above", "below"])

st.subheader("Notifications")
whatsapp_on = st.toggle("WhatsApp")
phone = ""
if whatsapp_on:
    phone = st.text_input(
        "WhatsApp Number (with country code)", placeholder="+61XXXXXXXXXX"
    )

email_on = st.toggle("Email")
email = ""
if email_on:
    email = st.text_input("Email Address")

if st.button("🔔 Set Alert"):
    if not ticker.strip():
        st.error("Enter a ticker.")
    elif target <= 0:
        st.error("Enter a target price above 0.")
    elif not whatsapp_on and not email_on:
        st.error("Enable at least one notification channel.")
    elif whatsapp_on and not phone.strip():
        st.error("Enter a WhatsApp number.")
    elif whatsapp_on and not re.fullmatch(r"\+[1-9]\d{6,14}", phone.replace(" ", "")):
        st.error("Invalid number — use format +61434069483 (country code, digits only).")
    elif whatsapp_on and phone.replace(" ", "").startswith("+610"):
        st.error(
            "Australian numbers drop the mobile's leading 0: "
            "use +61434… not +610434…"
        )
    elif email_on and not email.strip():
        st.error("Enter an email address.")
    else:
        try:
            r = api().post(
                "/alerts",
                json={
                    "ticker": ticker.strip().upper(),
                    "target_price": target,
                    "condition": condition,
                    "whatsapp_on": whatsapp_on,
                    "email_on": email_on,
                    "phone": phone.strip() or None,
                    "email": email.strip() or None,
                },
            )
            if r.status_code == 200:
                st.success("Alert set!")
                fetch_alerts.clear()
            else:
                st.error(f"Failed: {r.json().get('detail', r.text)}")
        except httpx.HTTPError as e:
            st.error(f"Backend unreachable: {e}")

# ── Section 2: Active alerts ────────────────────────────────────
st.header("Active Alerts")
st.caption(
    "Alerts re-notify while the condition holds, at most once per cooldown "
    f"({os.getenv('NOTIFY_COOLDOWN_MINUTES', '60')} min). Delete when done."
)

alerts = fetch_alerts()
if alerts is None:
    st.error(f"Cannot reach backend at {API_URL}. Is it running?")
elif not alerts:
    st.caption("No active alerts.")
else:
    header = st.columns([2, 2, 2, 2, 1, 1, 2, 1])
    for col, label in zip(
        header,
        ["Ticker", "Current", "Target", "Condition", "WhatsApp", "Email",
         "Last notified", ""],
    ):
        col.markdown(f"**{label}**")

    for a in alerts:
        cols = st.columns([2, 2, 2, 2, 1, 1, 2, 1])
        cols[0].write(a["ticker"])
        live = fetch_price(a["ticker"])
        cols[1].write(f"₹{live:,.2f}" if live is not None else "—")
        cols[2].write(f"₹{a['target_price']:,.2f}")
        cols[3].write(a["condition"])
        cols[4].write("✅" if a["whatsapp_on"] else "—")
        cols[5].write("✅" if a["email_on"] else "—")
        ln = a.get("last_notified")
        cols[6].write(
            datetime.fromisoformat(ln).strftime("%H:%M:%S") if ln else "not yet"
        )
        if cols[7].button("🗑️", key=f"del_{a['id']}"):
            api().delete(f"/alerts/{a['id']}")
            fetch_alerts.clear()
            st.rerun()

# ── Section 3: Sidebar ──────────────────────────────────────────
with st.sidebar:
    st.header("Status")
    health = fetch_health()
    if health:
        last = health.get("last_check")
        if last:
            last = datetime.fromisoformat(last).strftime("%d %b %Y %H:%M:%S")
        st.write(f"Last checked: {last or 'not yet'}")
        st.write(f"Active alerts: {health.get('active_alerts', 0)}")
        st.write(
            "Scheduler status: Running ✅"
            if health.get("scheduler_running")
            else "Scheduler status: Stopped ❌"
        )
        market_data = health.get("market_data", {})
        dhan = market_data.get("dhan", {})
        if not dhan.get("configured"):
            st.info("DhanHQ not configured — NSE prices use yfinance fallback.")
        elif not dhan.get("authenticated"):
            st.warning("DhanHQ token unavailable — NSE prices use yfinance fallback.")
    else:
        st.write("Backend: Unreachable ❌")
    if st.button("🔄 Refresh now"):
        fetch_alerts.clear()
        fetch_price.clear()
        st.rerun()
    st.caption("Data auto-refreshes every 30 s.")
