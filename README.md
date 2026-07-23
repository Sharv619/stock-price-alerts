# 📈 Stock Price Alerts

Single-user local app: set a target price on any supported ticker,
get a WhatsApp and/or email alert when the price crosses it.

- **Backend**: FastAPI + APScheduler (price check every 60 s, configurable)
- **Frontend**: Streamlit dashboard
- **Data**: Kite Connect (Zerodha) for NSE/BSE stocks with yfinance fallback; yfinance for indices, US stocks, and crypto
- **Notifications**: Whapi (WhatsApp) + Gmail SMTP (email)
- **Storage**: SQLite (`alerts.db`)

## Quick start

```bash
pip install -r requirements.txt --break-system-packages
cp .env.example .env   # fill in credentials
uvicorn main:app --port 8600            # terminal 1
streamlit run dashboard.py --server.port 8620   # terminal 2
```

Open http://localhost:8620 — full instructions in [SETUP.md](SETUP.md).

> NSE/BSE prices need a quick daily Kite login — open
> http://127.0.0.1:8600/kite/login (or click the dashboard banner; CLI
> fallback `python kite_login.py`). See [SETUP.md](SETUP.md) for details.

## Layout

| File | Purpose |
|------|---------|
| `app/database.py` | SQLAlchemy model + CRUD for alerts |
| `app/price_checker.py` | Kite/yfinance price fetch + alert evaluation |
| `app/kite_auth.py` | Kite Connect token load, validation + OAuth login |
| `app/notifier.py` | Whapi WhatsApp + Gmail SMTP senders |
| `app/scheduler.py` | APScheduler background loop |
| `main.py` | FastAPI routes (`/alerts`, `/price/{ticker}`, `/kite/*`, `/health`) |
| `kite_login.py` | Headless CLI for Kite daily re-login |
| `dashboard.py` | Streamlit UI |
