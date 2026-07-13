# Stock Price Alerts — Setup

A single-user, local stock price alert app. Set a target price for any
yfinance ticker; when the price crosses it, you get a WhatsApp message
and/or an email. Prices are checked every 60 seconds.

## 1. Install

```bash
pip install -r requirements.txt --break-system-packages
```

## 2. Credentials (`.env`)

Copy the template and fill it in:

```bash
cp .env.example .env
```

The app works without credentials — alerts still trigger and are logged —
but no messages are sent until you configure at least one channel below.

### Whapi (WhatsApp)

1. Sign up at https://whapi.cloud (free trial channel available).
2. Create a channel and link your WhatsApp by scanning the QR code shown
   in the Whapi dashboard.
3. Copy the channel's **API token** into `.env`:
   - `WHAPI_TOKEN` — the token (keep it in `.env` only, never in
     `.env.example` or version control)
   - `WHAPI_URL` — leave as `https://gate.whapi.cloud/`
4. Messages are sent to the alert's phone number with country code,
   e.g. `+91XXXXXXXXXX`.

### Gmail app password (free)

1. Enable 2-Step Verification on your Google account (required for app passwords).
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password (name it anything, e.g. "stock-alerts").
4. Put it in `.env`:
   - `GMAIL_USER` — your Gmail address
   - `GMAIL_APP_PASSWORD` — the 16-character app password (no spaces)

## 3. Run

Terminal 1 — backend (API + 60 s price-check scheduler):

```bash
uvicorn main:app --port 8600
```

Terminal 2 — frontend:

```bash
streamlit run dashboard.py --server.port 8620
```

Open http://localhost:8620

> Ports: the spec's defaults (8000/8501) were busy on this machine, so the
> docs use 8600/8620. Any free ports work — if you change the backend port,
> point the frontend at it with `API_URL`, e.g.
> `API_URL=http://localhost:9000 streamlit run dashboard.py`.

## 4. Supported ticker formats

Anything Yahoo Finance knows, via yfinance (free, no API key):

| Type | Examples |
|------|----------|
| Indian stocks (NSE) | `RELIANCE.NS`, `TCS.NS`, `INFY.NS` |
| Indian stocks (BSE) | `RELIANCE.BO` |
| Indian indices | `^NSEI` (Nifty 50), `^BSESN` (Sensex) |
| US stocks | `AAPL`, `TSLA`, `NVDA` |
| Crypto | `BTC-USD`, `ETH-USD` |

The dashboard shows a live price preview as soon as you type a ticker —
if it shows "No price data", the symbol is wrong.

## 5. How it behaves

- Alerts fire once: after a successful notification the alert is marked
  `triggered` and disappears from the active list.
- If every enabled channel fails to send (bad credentials, network), the
  alert stays active and is retried on the next 60 s cycle.
- A failed price fetch never crashes the checker — it logs a warning and
  skips that alert until the next cycle.
- Check frequency is configurable: set `CHECK_INTERVAL_SECONDS` in `.env`
  (default 60).
- Data lives in `alerts.db` (SQLite) next to `main.py`.
